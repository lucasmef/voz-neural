#!/usr/bin/env python3
"""Validate the independent runner on fifteen aligned real pairs."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "audition_neural_runner"))
sys.path.insert(0, str(ROOT / "premiere_validation_20260729"))

from analyze_premiere_export import (  # noqa: E402
    aligned_to_target,
    analyze_candidate,
)
from calibrate_dfn_capcut import (  # noqa: E402
    EPS,
    db,
    frame_rms,
    normalized_corr,
    rms,
    sinc_resample,
    spectral_band_distance,
    write_pcm24_wav,
)
from independent_voice_runner import (  # noqa: E402
    DEFAULT_DEEP_FILTER_BINARY,
    DEFAULT_VAD_BINARY,
    FlowSettings,
    process_array,
    run_rnnoise_vad,
)


PAIR_IDS = (
    "64",
    "65",
    "66",
    "67",
    "68",
    "69",
    "71",
    "72",
    "73",
    "74",
    "75",
    "76",
    "81",
    "82",
    "84",
)
RAW_RATE = 48000
TARGET_RATE = 44100
DEEP_FILTER = DEFAULT_DEEP_FILTER_BINARY
WAV_DIRECTORY = HERE / "validation_wavs"
PAIR_METRICS = ROOT / "analysis_capcut_pairs/PAIR_METRICS.csv"
VAD_BANDS = (
    ("lt_0.15", 0.0, 0.15),
    ("0.15_to_0.40", 0.15, 0.40),
    ("0.40_to_0.70", 0.40, 0.70),
    ("ge_0.70", 0.70, 1.01),
)


def pair_sources() -> dict[str, dict[str, str]]:
    with PAIR_METRICS.open("r", encoding="utf-8-sig", newline="") as source:
        rows = csv.DictReader(source)
        return {
            row["pair_id"]: {
                "raw_source": row["raw_source"],
                "source_type": row["source_type"],
                "source_match_envelope_corr": row["match_envelope_corr"],
                "source_alignment_ms": row["alignment_ms"],
            }
            for row in rows
            if row["pair_id"] in PAIR_IDS
        }


def vad_band_metrics(
    probabilities: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
) -> dict[str, dict[str, float | int]]:
    frame_size = 441
    candidate_envelope = frame_rms(candidate, frame_size)
    target_envelope = frame_rms(target, frame_size)
    count = min(
        probabilities.size,
        candidate_envelope.size,
        target_envelope.size,
    )
    probabilities = probabilities[:count]
    candidate_envelope = candidate_envelope[:count]
    target_envelope = target_envelope[:count]
    result: dict[str, dict[str, float | int]] = {}
    for name, lower, upper in VAD_BANDS:
        selected = (probabilities >= lower) & (probabilities < upper)
        selected_count = int(np.count_nonzero(selected))
        if selected_count == 0:
            result[name] = {"frames": 0}
            continue
        candidate_selected = candidate_envelope[selected]
        target_selected = target_envelope[selected]
        candidate_db = 20.0 * np.log10(
            np.maximum(candidate_selected, EPS)
        )
        target_db = 20.0 * np.log10(np.maximum(target_selected, EPS))
        result[name] = {
            "frames": selected_count,
            "candidate_median_dbfs": float(np.median(candidate_db)),
            "target_median_dbfs": float(np.median(target_db)),
            "candidate_minus_target_median_db": float(
                np.median(candidate_db - target_db)
            ),
            "candidate_minus_target_rms_db": db(
                rms(candidate_selected) / max(rms(target_selected), EPS)
            ),
            "log_envelope_correlation": normalized_corr(
                candidate_db,
                target_db,
            ),
        }
    return result


def rms_normalized_active_spectral_rmse(
    candidate: np.ndarray,
    target: np.ndarray,
    active: np.ndarray,
    frame_size: int,
) -> float:
    selected = np.repeat(active, frame_size)[: target.size]
    candidate_selected = candidate[selected]
    target_selected = target[selected]
    rms_gain = rms(target_selected) / max(rms(candidate_selected), EPS)
    spectral_rmse, _ = spectral_band_distance(
        candidate_selected,
        target_selected,
        TARGET_RATE,
        rms_gain,
    )
    return float(spectral_rmse)


def score(result: dict[str, object]) -> float:
    active = result["active"]
    noise = result["noise_after_voice_match"]
    contrast = result["quiet_to_active_contrast"]
    envelope = result["envelope"]
    return (
        abs(float(active["candidate_minus_target_rms_db"]))
        + float(active["spectral_rmse_db"])
        + abs(float(noise["quiet_rms_delta_db"]))
        + 0.4 * float(noise["spectral_rmse_db"])
        + abs(float(contrast["candidate_minus_target_db"]))
        + 2.0 * (1.0 - float(envelope["all_log_rms_correlation"]))
    )


def aggregate(pairs: dict[str, dict[str, object]]) -> dict[str, float]:
    def values(path: tuple[str, ...]) -> list[float]:
        result = []
        for pair in pairs.values():
            value: object = pair["comparison"]
            for key in path:
                value = value[key]
            result.append(float(value))
        return result

    active_delta = values(("active", "candidate_minus_target_rms_db"))
    quiet_delta = values(
        ("noise_after_voice_match", "quiet_rms_delta_db")
    )
    median_delta = values(
        ("quiet_to_active_contrast", "candidate_minus_target_db")
    )
    result: dict[str, float] = {
        "pairs": len(pairs),
        "mean_overall_correlation": float(
            np.mean(values(("overall", "correlation")))
        ),
        "mean_envelope_correlation": float(
            np.mean(values(("envelope", "all_log_rms_correlation")))
        ),
        "mean_abs_active_rms_delta_db": float(np.mean(np.abs(active_delta))),
        "median_abs_active_rms_delta_db": float(
            np.median(np.abs(active_delta))
        ),
        "mean_active_spectral_rmse_db": float(
            np.mean(values(("active", "spectral_rmse_db")))
        ),
        "mean_active_spectral_rmse_rms_normalized_db": float(
            np.mean(
                [
                    float(pair["active_spectral_rmse_rms_normalized_db"])
                    for pair in pairs.values()
                ]
            )
        ),
        "mean_abs_quiet_rms_after_voice_match_db": float(
            np.mean(np.abs(quiet_delta))
        ),
        "median_abs_quiet_rms_after_voice_match_db": float(
            np.median(np.abs(quiet_delta))
        ),
        "mean_abs_quiet_median_after_voice_match_db": float(
            np.mean(np.abs(median_delta))
        ),
        "median_abs_quiet_median_after_voice_match_db": float(
            np.median(np.abs(median_delta))
        ),
        "mean_quiet_waveform_correlation": float(
            np.mean(values(("quiet", "correlation")))
        ),
        "mean_quiet_spectral_rmse_after_voice_match_db": float(
            np.mean(
                values(("noise_after_voice_match", "spectral_rmse_db"))
            )
        ),
        "pairs_active_within_2db": int(
            np.count_nonzero(np.abs(active_delta) <= 2.0)
        ),
        "pairs_quiet_rms_within_3db": int(
            np.count_nonzero(np.abs(quiet_delta) <= 3.0)
        ),
        "pairs_quiet_median_within_3db": int(
            np.count_nonzero(np.abs(median_delta) <= 3.0)
        ),
    }
    for band_name, _, _ in VAD_BANDS:
        median_errors = []
        rms_errors = []
        correlations = []
        for pair in pairs.values():
            band = pair["vad_bands"][band_name]
            if int(band["frames"]) == 0:
                continue
            median_errors.append(
                float(band["candidate_minus_target_median_db"])
            )
            rms_errors.append(
                float(band["candidate_minus_target_rms_db"])
            )
            correlations.append(float(band["log_envelope_correlation"]))
        result[f"vad_{band_name}_mean_abs_median_error_db"] = float(
            np.mean(np.abs(median_errors))
        )
        result[f"vad_{band_name}_mean_abs_rms_error_db"] = float(
            np.mean(np.abs(rms_errors))
        )
        result[f"vad_{band_name}_mean_log_envelope_correlation"] = float(
            np.mean(correlations)
        )
    return result


def main() -> None:
    WAV_DIRECTORY.mkdir(parents=True, exist_ok=True)
    pairs: dict[str, dict[str, object]] = {}
    settings = FlowSettings()
    sources = pair_sources()
    missing = set(PAIR_IDS) - set(sources)
    if missing:
        raise RuntimeError(f"PAIR_METRICS.csv is missing pairs: {missing}")
    for index, pair_id in enumerate(PAIR_IDS, start=1):
        source_metadata = sources[pair_id]
        raw_path = ROOT / source_metadata["raw_source"]
        target_path = (
            ROOT / f"analysis_capcut_pairs/decoded/treated/{pair_id}.f32"
        )
        raw = np.fromfile(raw_path, dtype="<f4")
        target = np.fromfile(target_path, dtype="<f4")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(
            prefix=f"independent_{pair_id}_", dir=HERE
        ) as temporary:
            candidate_48, runner = process_array(
                raw,
                RAW_RATE,
                DEEP_FILTER,
                Path(temporary),
                settings,
            )
            probabilities, _ = run_rnnoise_vad(
                raw,
                RAW_RATE,
                DEFAULT_VAD_BINARY,
                Path(temporary),
            )
        elapsed = time.monotonic() - started
        raw_44 = sinc_resample(
            raw,
            RAW_RATE,
            TARGET_RATE,
            output_frames=target.size,
        )
        candidate_44 = sinc_resample(
            candidate_48,
            RAW_RATE,
            TARGET_RATE,
            output_frames=target.size,
        )
        candidate_aligned, lag, _ = aligned_to_target(
            candidate_44, target, TARGET_RATE
        )

        frame_size = int(round(0.020 * TARGET_RATE))
        raw_envelope = frame_rms(raw_44, frame_size)
        eligible = np.ones(raw_envelope.size, dtype=bool)
        edge_frames = int(round(0.300 / 0.020))
        if raw_envelope.size > 2 * edge_frames:
            eligible[:edge_frames] = False
            eligible[-edge_frames:] = False
        values = raw_envelope[eligible]
        quiet_threshold = float(np.quantile(values, 0.25))
        active_threshold = float(np.quantile(values, 0.65))
        quiet = eligible & (raw_envelope <= quiet_threshold)
        active = eligible & (raw_envelope >= active_threshold)
        comparison = analyze_candidate(
            pair_id,
            candidate_44,
            target,
            raw_44,
            active,
            quiet,
            frame_size,
            TARGET_RATE,
        )
        comparison["selection_score"] = score(comparison)
        active_spectral_rms_normalized = (
            rms_normalized_active_spectral_rmse(
                candidate_aligned,
                target,
                active,
                frame_size,
            )
        )
        vad_bands = vad_band_metrics(
            probabilities,
            candidate_aligned,
            target,
        )
        pairs[pair_id] = {
            "files": {
                "raw_f32": str(raw_path),
                "capcut_f32": str(target_path),
            },
            "source": {
                "source_type": source_metadata["source_type"],
                "match_envelope_corr": float(
                    source_metadata["source_match_envelope_corr"]
                ),
                "alignment_ms": float(
                    source_metadata["source_alignment_ms"]
                ),
            },
            "formats": {
                "raw_rate": RAW_RATE,
                "raw_frames": int(raw.size),
                "target_rate": TARGET_RATE,
                "target_frames": int(target.size),
                "candidate_48_frames": int(candidate_48.size),
                "candidate_44_frames": int(candidate_44.size),
            },
            "validation_selection": {
                "quiet_threshold_dbfs": db(quiet_threshold),
                "active_threshold_dbfs": db(active_threshold),
                "quiet_frames": int(np.count_nonzero(quiet)),
                "active_frames": int(np.count_nonzero(active)),
            },
            "runtime_seconds": elapsed,
            "runner": runner,
            "comparison": comparison,
            "active_spectral_rmse_rms_normalized_db": (
                active_spectral_rms_normalized
            ),
            "vad_bands": vad_bands,
        }
        write_pcm24_wav(
            WAV_DIRECTORY / f"{pair_id}_A_CAPCUT_44K1.wav",
            target,
            TARGET_RATE,
        )
        write_pcm24_wav(
            WAV_DIRECTORY / f"{pair_id}_B_INDEPENDENT_44K1.wav",
            candidate_aligned,
            TARGET_RATE,
        )
        if pair_id == "65":
            write_pcm24_wav(
                HERE / "65_INDEPENDENT_ALIGNED_44K1.wav",
                candidate_aligned,
                TARGET_RATE,
            )
            write_pcm24_wav(
                HERE / "65_CAPCUT_REFERENCE_44K1.wav",
                target,
                TARGET_RATE,
            )
        print(
            f"[{index}/{len(PAIR_IDS)}] {pair_id}: "
            f"active {comparison['active']['candidate_minus_target_rms_db']:+.2f} "
            f"dB, quiet "
            f"{comparison['noise_after_voice_match']['quiet_rms_delta_db']:+.2f} "
            f"dB, median "
            f"{comparison['quiet_to_active_contrast']['candidate_minus_target_db']:+.2f} "
            f"dB, active spectrum RMS-normalized "
            f"{active_spectral_rms_normalized:.2f} dB, {elapsed:.2f}s"
        )

    result = {
        "independent_runtime": True,
        "uses_capcut_runtime_or_model": False,
        "settings": settings.__dict__,
        "pair_ids": list(PAIR_IDS),
        "aggregate": aggregate(pairs),
        "pairs": pairs,
    }
    (HERE / "VALIDATION.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
