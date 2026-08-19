#!/usr/bin/env python3
"""Avalia uma ablação DeNoise do ensaio 107 sem processar o áudio.

O alvo CapCut é usado para ruído, ambiência e assinatura tonal. O controle
Premiere E100 + Auto-Match + limiter é usado para verificar que o DeNoise não
remodelou o volume das frases. Todos os sinais são alinhados ao bruto DJI e
comparados em trechos fixos definidos antes do teste.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

import comparar_wavs_adobe as base


RATE = 48_000
FRAME_MS = 20.0
FRAME = int(round(RATE * FRAME_MS / 1000.0))
EPS = 1.0e-12

NOISE_INTERVALS = (
    (0.88, 1.86),
    (4.42, 4.62),
    (5.92, 6.22),
    (7.02, 8.12),
    (12.90, 13.78),
    (17.82, 19.42),
    (21.56, 21.86),
    (22.88, 23.16),
    (24.18, 24.90),
    (29.18, 29.92),
)

VOICE_PRESERVATION_INTERVALS = (
    (0.28, 0.68),
    (3.80, 4.16),
    (4.88, 5.90),
    (8.24, 9.28),
    (19.44, 20.62),
    (24.98, 26.10),
    (28.10, 29.16),
)

SIBILANCE_ATTACKS = (9.75, 10.49, 11.51, 19.65, 23.81, 27.33)
ACTIVE_BANDS = (
    "low_80_200",
    "body_200_500",
    "mid_500_1000",
    "presence_1k_2k",
    "clarity_2k_4k",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path) -> np.ndarray:
    wav = base.read_pcm_wav(path)
    return base.sinc_resample(base.mono_mean(wav.samples), wav.sample_rate, RATE)


def db_rms(audio: np.ndarray) -> float:
    return base.amp_db(base.rms(audio))


def finite(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def align_four(
    raw: np.ndarray,
    baseline: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    max_lag: int,
) -> tuple[dict[str, np.ndarray], dict[str, Any], int]:
    lags: dict[str, int] = {"raw": 0}
    correlations: dict[str, float] = {"raw": 1.0}
    for name, audio in (
        ("baseline", baseline),
        ("candidate", candidate),
        ("target", target),
    ):
        lag, corr = base.best_placement_lag(raw, audio, max_lag)
        lags[name] = lag
        correlations[name] = corr

    start = max(lags.values())
    stop = min(
        raw.size,
        lags["baseline"] + baseline.size,
        lags["candidate"] + candidate.size,
        lags["target"] + target.size,
    )
    stop = start + ((stop - start) // FRAME) * FRAME
    if stop - start < FRAME * 10:
        raise ValueError("sobreposição insuficiente depois do alinhamento")

    aligned = {
        "raw": raw[start:stop],
        "baseline": baseline[start - lags["baseline"] : stop - lags["baseline"]],
        "candidate": candidate[start - lags["candidate"] : stop - lags["candidate"]],
        "target": target[start - lags["target"] : stop - lags["target"]],
    }
    audit = {
        name: {
            "lag_samples_on_raw_timeline": lags[name],
            "lag_ms": 1000.0 * lags[name] / RATE,
            "correlation": correlations[name],
        }
        for name in lags
    }
    audit["common"] = {
        "start_sample_on_raw_timeline": start,
        "stop_sample_on_raw_timeline": stop,
        "duration_seconds": (stop - start) / RATE,
    }
    return aligned, audit, start


def frame_mask_from_intervals(
    intervals: tuple[tuple[float, float], ...],
    frame_count: int,
    common_start: int,
) -> np.ndarray:
    centers = (common_start + (np.arange(frame_count) + 0.5) * FRAME) / RATE
    mask = np.zeros(frame_count, dtype=bool)
    for begin, end in intervals:
        mask |= (centers >= begin) & (centers < end)
    return mask


def sample_mask(frame_mask: np.ndarray, length: int) -> np.ndarray:
    return np.repeat(frame_mask, FRAME)[:length]


def band_ratios(audio: np.ndarray) -> np.ndarray:
    frame_count = audio.size // FRAME
    frames = audio[: frame_count * FRAME].reshape(frame_count, FRAME)
    power = np.abs(np.fft.rfft(frames * np.hanning(FRAME), axis=1)) ** 2
    frequencies = np.fft.rfftfreq(FRAME, 1.0 / RATE)
    middle = np.sum(power[:, (frequencies >= 1_000) & (frequencies < 4_000)], axis=1)
    high = np.sum(power[:, (frequencies >= 4_000) & (frequencies < 8_000)], axis=1)
    return 10.0 * np.log10((high + EPS) / (middle + EPS))


def interval_metrics(
    audio: np.ndarray,
    reference: np.ndarray,
    interval: tuple[float, float],
    common_start: int,
    match_gain_db: float,
) -> dict[str, Any]:
    begin, end = interval
    first = max(0, int(round(begin * RATE)) - common_start)
    stop = min(audio.size, int(round(end * RATE)) - common_start)
    if stop <= first:
        return {"start_seconds": begin, "stop_seconds": end, "samples": 0}
    candidate_db = db_rms(audio[first:stop]) + match_gain_db
    reference_db = db_rms(reference[first:stop])
    return {
        "start_seconds": begin,
        "stop_seconds": end,
        "samples": stop - first,
        "candidate_rms_dbfs_after_match": candidate_db,
        "reference_rms_dbfs": reference_db,
        "candidate_minus_reference_db": candidate_db - reference_db,
    }


def spectral_delta(
    audio: np.ndarray,
    reference: np.ndarray,
    active_samples: np.ndarray,
    match_gain_db: float,
) -> dict[str, Any]:
    audio_power, audio_windows = base.accumulated_spectrum(audio, active_samples)
    reference_power, reference_windows = base.accumulated_spectrum(reference, active_samples)
    return base.spectral_comparison(
        audio_power,
        reference_power,
        RATE,
        match_gain_db,
        audio_windows,
        reference_windows,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="107 DN35 ALL")
    parser.add_argument("--max-lag-ms", type=float, default=1_000.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = {
        "raw": args.raw.expanduser().resolve(),
        "baseline": args.baseline.expanduser().resolve(),
        "candidate": args.candidate.expanduser().resolve(),
        "target": args.target.expanduser().resolve(),
    }
    native = {name: base.read_pcm_wav(path) for name, path in paths.items()}
    audio = {name: load(path) for name, path in paths.items()}
    aligned, alignment, common_start = align_four(
        audio["raw"],
        audio["baseline"],
        audio["candidate"],
        audio["target"],
        int(round(args.max_lag_ms * RATE / 1000.0)),
    )
    raw = aligned["raw"]
    baseline = aligned["baseline"]
    candidate = aligned["candidate"]
    target = aligned["target"]
    frame_count = raw.size // FRAME
    raw_envelope = base.frame_rms(raw, FRAME)[:frame_count]
    baseline_envelope = base.frame_rms(baseline, FRAME)[:frame_count]
    candidate_envelope = base.frame_rms(candidate, FRAME)[:frame_count]
    target_envelope = base.frame_rms(target, FRAME)[:frame_count]
    raw_db = 20.0 * np.log10(np.maximum(raw_envelope, EPS))
    active_frames = raw_db >= -42.0
    noise_frames = frame_mask_from_intervals(NOISE_INTERVALS, frame_count, common_start)
    active_samples = sample_mask(active_frames, raw.size)
    noise_samples = sample_mask(noise_frames, raw.size)
    if not np.any(active_samples) or not np.any(noise_samples):
        raise ValueError("máscaras de voz ou fundo ficaram vazias")

    candidate_to_target_gain = db_rms(target[active_samples]) - db_rms(
        candidate[active_samples]
    )
    baseline_to_target_gain = db_rms(target[active_samples]) - db_rms(
        baseline[active_samples]
    )
    candidate_to_baseline_gain = db_rms(baseline[active_samples]) - db_rms(
        candidate[active_samples]
    )

    candidate_noise_levels = 20.0 * np.log10(
        np.maximum(candidate_envelope[noise_frames], EPS)
    )
    target_noise_levels = 20.0 * np.log10(
        np.maximum(target_envelope[noise_frames], EPS)
    )
    background_rms_delta = (
        db_rms(candidate[noise_samples])
        + candidate_to_target_gain
        - db_rms(target[noise_samples])
    )
    background_median_delta = (
        float(np.median(candidate_noise_levels))
        + candidate_to_target_gain
        - float(np.median(target_noise_levels))
    )
    noise_by_interval = [
        interval_metrics(candidate, target, interval, common_start, candidate_to_target_gain)
        for interval in NOISE_INTERVALS
    ]

    phrase_rows = [
        interval_metrics(
            candidate,
            baseline,
            interval,
            common_start,
            candidate_to_baseline_gain,
        )
        for interval in VOICE_PRESERVATION_INTERVALS
    ]
    phrase_deltas = np.asarray(
        [row["candidate_minus_reference_db"] for row in phrase_rows if row["samples"]],
        dtype=np.float64,
    )
    baseline_phrase_levels = np.asarray(
        [row["reference_rms_dbfs"] for row in phrase_rows if row["samples"]],
        dtype=np.float64,
    )
    candidate_phrase_levels = np.asarray(
        [row["candidate_rms_dbfs_after_match"] for row in phrase_rows if row["samples"]],
        dtype=np.float64,
    )
    baseline_dispersion = float(
        np.quantile(baseline_phrase_levels, 0.90)
        - np.quantile(baseline_phrase_levels, 0.10)
    )
    candidate_dispersion = float(
        np.quantile(candidate_phrase_levels, 0.90)
        - np.quantile(candidate_phrase_levels, 0.10)
    )
    phrase_dispersion_increase = candidate_dispersion - baseline_dispersion
    active_envelope_corr = base.normalized_corr(
        np.log10(candidate_envelope[active_frames] + EPS),
        np.log10(baseline_envelope[active_frames] + EPS),
    )

    candidate_spectrum = spectral_delta(
        candidate, target, active_samples, candidate_to_target_gain
    )
    baseline_spectrum = spectral_delta(
        baseline, target, active_samples, baseline_to_target_gain
    )
    band_rows: dict[str, Any] = {}
    extra_errors = []
    for name in ACTIVE_BANDS:
        candidate_delta = float(candidate_spectrum["band_delta_db"][name])
        baseline_delta = float(baseline_spectrum["band_delta_db"][name])
        extra_error = abs(candidate_delta) - abs(baseline_delta)
        extra_errors.append(extra_error)
        band_rows[name] = {
            "candidate_minus_capcut_db": candidate_delta,
            "baseline_minus_capcut_db": baseline_delta,
            "candidate_minus_baseline_db": candidate_delta - baseline_delta,
            "extra_absolute_error_vs_capcut_db": extra_error,
        }
    max_active_band_extra_error = float(max(extra_errors))

    ratios_candidate = band_ratios(candidate)
    ratios_baseline = band_ratios(baseline)
    ratios_target = band_ratios(target)
    sibilance_rows = []
    for time_seconds in SIBILANCE_ATTACKS:
        center = (time_seconds * RATE - common_start) / FRAME - 0.5
        center_frame = int(round(center))
        radius = 2  # janela total aproximada de 100 ms
        first = max(0, center_frame - radius)
        stop = min(frame_count, center_frame + radius + 1)
        cand_delta = float(np.median(ratios_candidate[first:stop] - ratios_target[first:stop]))
        baseline_delta = float(np.median(ratios_baseline[first:stop] - ratios_target[first:stop]))
        sibilance_rows.append(
            {
                "time_seconds": time_seconds,
                "candidate_ratio_minus_capcut_db": cand_delta,
                "baseline_ratio_minus_capcut_db": baseline_delta,
                "candidate_minus_baseline_ratio_db": cand_delta - baseline_delta,
                "extra_absolute_error_vs_capcut_db": abs(cand_delta) - abs(baseline_delta),
            }
        )
    event_candidate = np.asarray(
        [row["candidate_ratio_minus_capcut_db"] for row in sibilance_rows]
    )
    event_baseline = np.asarray(
        [row["baseline_ratio_minus_capcut_db"] for row in sibilance_rows]
    )
    event_extra = np.abs(event_candidate) - np.abs(event_baseline)
    broad_candidate = float(
        candidate_spectrum["band_delta_db"]["sibilance_4k_8k"]
    )
    broad_baseline = float(
        baseline_spectrum["band_delta_db"]["sibilance_4k_8k"]
    )
    broad_extra = abs(broad_candidate) - abs(broad_baseline)

    gates = {
        "background_rms_abs_le_3db": abs(background_rms_delta) <= 3.0,
        "background_median_abs_le_3db": abs(background_median_delta) <= 3.0,
        "every_phrase_abs_change_le_0_7db": (
            phrase_deltas.size == len(VOICE_PRESERVATION_INTERVALS)
            and float(np.max(np.abs(phrase_deltas))) <= 0.7
        ),
        "phrase_dispersion_increase_le_0_3db": phrase_dispersion_increase <= 0.3,
        "active_envelope_corr_ge_0_99_vs_baseline": active_envelope_corr >= 0.99,
        "active_80hz_4khz_max_extra_error_le_0_5db": max_active_band_extra_error <= 0.5,
        "sibilance_broad_extra_error_le_0_5db": broad_extra <= 0.5,
        "sibilance_event_median_extra_error_le_0_5db": (
            float(np.median(event_extra)) <= 0.5
        ),
        "sibilance_event_p90_extra_error_le_0_5db": (
            float(np.quantile(event_extra, 0.90)) <= 0.5
        ),
    }
    failed = [name for name, passed in gates.items() if not passed]

    result = finite(
        {
            "schema_version": 1,
            "label": args.label,
            "method": {
                "analysis_rate_hz": RATE,
                "frame_ms": FRAME_MS,
                "active_definition": "raw DJI frame RMS >= -42 dBFS",
                "background_basis": "fixed predeclared noise intervals, after active-voice RMS match to CapCut",
                "phrase_basis": "fixed predeclared voice intervals, after active-voice RMS match to Premiere baseline",
                "phrase_dispersion": "P90-P10 of the seven phrase RMS levels",
                "active_band_basis": "active spectrum after active-voice RMS match; extra error is abs(candidate-CapCut)-abs(baseline-CapCut)",
                "sibilance_basis": "median 4-8 kHz / 1-4 kHz ratio in ~100 ms windows around six fixed attacks",
            },
            "files": {
                name: {
                    "path": str(path),
                    "sha256": sha256(path),
                    "sample_rate": native[name].sample_rate,
                    "channels": native[name].channels,
                    "bits_per_sample": native[name].sample_width * 8,
                    "duration_seconds": native[name].duration,
                }
                for name, path in paths.items()
            },
            "alignment": alignment,
            "selection": {
                "active_frames": int(np.count_nonzero(active_frames)),
                "active_seconds": float(np.count_nonzero(active_frames) * FRAME_MS / 1000.0),
                "noise_frames": int(np.count_nonzero(noise_frames)),
                "noise_seconds": float(np.count_nonzero(noise_frames) * FRAME_MS / 1000.0),
                "noise_intervals_seconds": NOISE_INTERVALS,
                "voice_preservation_intervals_seconds": VOICE_PRESERVATION_INTERVALS,
                "sibilance_attacks_seconds": SIBILANCE_ATTACKS,
            },
            "match_gains_db": {
                "candidate_to_capcut_active_voice": candidate_to_target_gain,
                "baseline_to_capcut_active_voice": baseline_to_target_gain,
                "candidate_to_baseline_active_voice": candidate_to_baseline_gain,
            },
            "background_vs_capcut": {
                "aggregate_rms_delta_db": background_rms_delta,
                "aggregate_median_frame_delta_db": background_median_delta,
                "by_interval": noise_by_interval,
            },
            "phrase_preservation_vs_premiere_baseline": {
                "by_interval": phrase_rows,
                "max_abs_phrase_change_db": float(np.max(np.abs(phrase_deltas))),
                "phrase_delta_median_db": float(np.median(phrase_deltas)),
                "baseline_phrase_p90_p10_db": baseline_dispersion,
                "candidate_phrase_p90_p10_db": candidate_dispersion,
                "dispersion_increase_db": phrase_dispersion_increase,
                "active_log_envelope_correlation": active_envelope_corr,
            },
            "active_tone_vs_capcut": {
                "bands_80hz_4khz": band_rows,
                "max_extra_absolute_error_db": max_active_band_extra_error,
                "candidate_all_bands": candidate_spectrum["band_delta_db"],
                "baseline_all_bands": baseline_spectrum["band_delta_db"],
            },
            "sibilance": {
                "active_4k_8k_candidate_minus_capcut_db": broad_candidate,
                "active_4k_8k_baseline_minus_capcut_db": broad_baseline,
                "active_4k_8k_extra_absolute_error_db": broad_extra,
                "events": sibilance_rows,
                "event_candidate_median_db": float(np.median(event_candidate)),
                "event_candidate_p90_abs_db": float(np.quantile(np.abs(event_candidate), 0.90)),
                "event_baseline_median_db": float(np.median(event_baseline)),
                "event_baseline_p90_abs_db": float(np.quantile(np.abs(event_baseline), 0.90)),
                "event_extra_error_median_db": float(np.median(event_extra)),
                "event_extra_error_p90_db": float(np.quantile(event_extra, 0.90)),
            },
            "gates": gates,
            "gate_summary": {
                "passed": len(gates) - len(failed),
                "total": len(gates),
                "failed": failed,
                "objective_grade_pass": not failed,
                "listening_still_required": [
                    "pumping or breathing in pauses",
                    "musical-noise artifacts",
                    "cut consonant tails and word endings",
                ],
            },
        }
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
