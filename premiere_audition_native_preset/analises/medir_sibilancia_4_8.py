#!/usr/bin/env python3
"""Meça brilho amplo e eventos sibilantes entre 4 e 8 kHz.

A seleção de voz vem do bruto. Os eventos sibilantes são os 15% de frames
ativos com maior razão de energia 4–8 kHz / 1–4 kHz no próprio alvo CapCut.
Isso separa excesso tonal amplo de redução excessiva somente nos sons de S.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

import comparar_wavs_adobe as base


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_mono(path: Path, rate: int) -> np.ndarray:
    wav = base.read_pcm_wav(path)
    return base.sinc_resample(base.mono_mean(wav.samples), wav.sample_rate, rate)


def frame_band_ratio_db(
    audio: np.ndarray, frame_size: int, sample_rate: int
) -> np.ndarray:
    frame_count = audio.size // frame_size
    framed = audio[: frame_count * frame_size].reshape(frame_count, frame_size)
    window = np.hanning(frame_size)
    power = np.abs(np.fft.rfft(framed * window, axis=1)) ** 2
    frequencies = np.fft.rfftfreq(frame_size, 1.0 / sample_rate)
    mid = np.sum(power[:, (frequencies >= 1000) & (frequencies < 4000)], axis=1)
    high = np.sum(power[:, (frequencies >= 4000) & (frequencies < 8000)], axis=1)
    return 10.0 * np.log10((high + base.EPS) / (mid + base.EPS))


def finite(value: Any) -> Any:
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite(item) for item in value]
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--raw", type=Path, required=True)
    result.add_argument("--candidate", type=Path, required=True)
    result.add_argument("--target", type=Path, required=True)
    result.add_argument("--label")
    result.add_argument("--output", type=Path)
    result.add_argument("--analysis-rate", type=int, default=48000)
    result.add_argument("--max-lag-ms", type=float, default=1000.0)
    return result


def main() -> int:
    args = parser().parse_args()
    rate = args.analysis_rate
    raw_native = load_mono(args.raw, rate)
    candidate_native = load_mono(args.candidate, rate)
    target_native = load_mono(args.target, rate)

    max_lag = int(round(rate * args.max_lag_ms / 1000.0))
    raw_lag, raw_corr = base.best_placement_lag(target_native, raw_native, max_lag)
    candidate_lag, candidate_corr = base.best_placement_lag(
        target_native, candidate_native, max_lag
    )
    target, raw, candidate, start, stop = base.common_overlap(
        target_native,
        raw_native,
        candidate_native,
        raw_lag,
        candidate_lag,
    )

    masks, selection = base.selection_masks(raw, rate, 20.0, 300.0, 0.25, 0.65)
    frame_size = int(selection["frame_samples"])
    ratios_candidate = frame_band_ratio_db(candidate, frame_size, rate)
    ratios_target = frame_band_ratio_db(target, frame_size, rate)
    frame_count = min(
        ratios_candidate.size,
        ratios_target.size,
        masks["active"].size,
    )
    active_indices = np.flatnonzero(masks["active"][:frame_count])
    if active_indices.size < 10:
        raise SystemExit("frames ativos insuficientes")

    sibilant_count = max(1, int(round(active_indices.size * 0.15)))
    target_order = np.argsort(ratios_target[active_indices])
    sibilant_indices = np.sort(active_indices[target_order[-sibilant_count:]])
    event_delta = (
        ratios_candidate[sibilant_indices] - ratios_target[sibilant_indices]
    )

    active_samples = base.samples_from_frame_mask(
        masks["active"], frame_size, target.size
    )
    candidate_power, candidate_windows = base.accumulated_spectrum(
        candidate, active_samples
    )
    target_power, target_windows = base.accumulated_spectrum(target, active_samples)
    candidate_loudness = base.mono_loudness(candidate, rate)["integrated_lufs"]
    target_loudness = base.mono_loudness(target, rate)["integrated_lufs"]
    match_gain = (
        float(target_loudness - candidate_loudness)
        if candidate_loudness is not None and target_loudness is not None
        else 0.0
    )
    spectrum = base.spectral_comparison(
        candidate_power,
        target_power,
        rate,
        match_gain,
        candidate_windows,
        target_windows,
    )
    broad_delta = spectrum["band_delta_db"]["sibilance_4k_8k"]
    event_median = float(np.median(event_delta))
    event_p90_abs = float(np.quantile(np.abs(event_delta), 0.90))

    top_times = [
        round((int(index) + 0.5) * frame_size / rate, 3)
        for index in sibilant_indices[
            np.argsort(ratios_target[sibilant_indices])[-10:][::-1]
        ]
    ]
    result = finite(
        {
            "schema_version": 1,
            "label": args.label or args.candidate.stem,
            "files": {
                "raw": {"path": str(args.raw.resolve()), "sha256": sha256(args.raw)},
                "candidate": {
                    "path": str(args.candidate.resolve()),
                    "sha256": sha256(args.candidate),
                },
                "target": {
                    "path": str(args.target.resolve()),
                    "sha256": sha256(args.target),
                },
            },
            "alignment": {
                "raw_lag_ms": 1000.0 * raw_lag / rate,
                "raw_correlation": raw_corr,
                "candidate_lag_ms": 1000.0 * candidate_lag / rate,
                "candidate_correlation": candidate_corr,
                "common_start_frame": start,
                "common_stop_frame": stop,
            },
            "selection": {
                **selection,
                "sibilant_definition": (
                    "top 15% of raw-active frames by target 4-8 kHz / 1-4 kHz ratio"
                ),
                "sibilant_frames": int(sibilant_indices.size),
                "strongest_target_sibilance_times_seconds": top_times,
            },
            "metrics": {
                "active_4k_8k_delta_after_loudness_match_db": broad_delta,
                "target_sibilant_ratio_delta_median_db": event_median,
                "target_sibilant_ratio_delta_p90_absolute_db": event_p90_abs,
                "interpretation": (
                    "positive = candidate is brighter/more sibilant than CapCut; "
                    "negative = possible over-de-essing or lisp"
                ),
            },
            "gates": {
                "active_4k_8k_abs_le_1_5db": abs(broad_delta) <= 1.5,
                "sibilant_event_median_abs_le_1db": abs(event_median) <= 1.0,
                "sibilant_event_p90_abs_le_2db": event_p90_abs <= 2.0,
            },
        }
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
