#!/usr/bin/env python3
"""Compara nivel temporal de cru, export Premiere e alvo em uma regiao curta."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import comparar_wavs_adobe as base


def rms_db(samples: np.ndarray) -> float:
    value = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    return float(20.0 * np.log10(max(float(value), 1e-12)))


def peak_db(samples: np.ndarray) -> float:
    return float(20.0 * np.log10(max(float(np.max(np.abs(samples))), 1e-12)))


def load_mono(path: Path, rate: int) -> np.ndarray:
    wav = base.read_pcm_wav(path)
    return base.sinc_resample(base.mono_mean(wav.samples), wav.sample_rate, rate)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--start", type=float, default=4.0)
    parser.add_argument("--stop", type=float, default=8.0)
    parser.add_argument("--window-ms", type=float, default=250.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rate = 48_000
    raw = load_mono(args.raw, rate)
    candidate = load_mono(args.candidate, rate)
    target = load_mono(args.target, rate)

    raw_lag, raw_corr = base.best_placement_lag(raw, candidate, rate)
    target_lag, target_corr = base.best_placement_lag(target, candidate, rate)
    raw, candidate_for_raw, _copy, _start, _stop = base.common_overlap(
        raw, candidate, candidate, raw_lag, raw_lag
    )
    target, candidate_for_target, _copy, _start, _stop = base.common_overlap(
        target, candidate, candidate, target_lag, target_lag
    )
    common = min(raw.size, candidate_for_raw.size, target.size, candidate_for_target.size)
    raw = raw[:common]
    candidate = candidate_for_raw[:common]
    target = target[:common]

    frame = max(1, round(args.window_ms * rate / 1000.0))
    left = max(0, round(args.start * rate))
    right = min(common, round(args.stop * rate))
    rows = []
    for pos in range(left, right, frame):
        end = min(pos + frame, right)
        raw_db = rms_db(raw[pos:end])
        candidate_db = rms_db(candidate[pos:end])
        target_db = rms_db(target[pos:end])
        rows.append(
            {
                "start_seconds": pos / rate,
                "stop_seconds": end / rate,
                "raw_rms_dbfs": raw_db,
                "premiere_rms_dbfs": candidate_db,
                "capcut_rms_dbfs": target_db,
                "premiere_minus_capcut_db": candidate_db - target_db,
                "premiere_gain_over_raw_db": candidate_db - raw_db,
                "capcut_gain_over_raw_db": target_db - raw_db,
                "raw_peak_dbfs": peak_db(raw[pos:end]),
                "premiere_peak_dbfs": peak_db(candidate[pos:end]),
                "capcut_peak_dbfs": peak_db(target[pos:end]),
            }
        )

    analysis_frame = round(0.02 * rate)
    focus_raw = raw[left:right]
    focus_candidate = candidate[left:right]
    focus_target = target[left:right]
    frame_count = min(focus_raw.size, focus_candidate.size, focus_target.size) // analysis_frame
    focus_raw = focus_raw[: frame_count * analysis_frame]
    focus_candidate = focus_candidate[: frame_count * analysis_frame]
    focus_target = focus_target[: frame_count * analysis_frame]
    raw_db = 20.0 * np.log10(np.maximum(base.frame_rms(focus_raw, analysis_frame), 1e-12))
    candidate_db = 20.0 * np.log10(np.maximum(base.frame_rms(focus_candidate, analysis_frame), 1e-12))
    target_db = 20.0 * np.log10(np.maximum(base.frame_rms(focus_target, analysis_frame), 1e-12))
    speech = raw_db >= -55.0

    def fit(output_db: np.ndarray) -> dict[str, float]:
        slope, intercept = np.polyfit(raw_db[speech], output_db[speech], 1)
        gain = output_db[speech] - raw_db[speech]
        return {
            "slope_output_vs_raw": float(slope),
            "intercept_db": float(intercept),
            "gain_over_raw_median_db": float(np.median(gain)),
            "gain_over_raw_p10_db": float(np.quantile(gain, 0.10)),
            "gain_over_raw_p90_db": float(np.quantile(gain, 0.90)),
            "correlation_output_vs_raw": float(np.corrcoef(raw_db[speech], output_db[speech])[0, 1]),
        }

    dynamic_fit = {
        "analysis_frame_ms": 20.0,
        "raw_threshold_dbfs": -55.0,
        "eligible_frames": int(speech.sum()),
        "premiere": fit(candidate_db),
        "capcut": fit(target_db),
    }

    result = {
        "alignment": {
            "raw_to_candidate_lag_ms": raw_lag * 1000.0 / rate,
            "raw_to_candidate_corr": raw_corr,
            "target_to_candidate_lag_ms": target_lag * 1000.0 / rate,
            "target_to_candidate_corr": target_corr,
        },
        "region_seconds": [args.start, args.stop],
        "window_ms": args.window_ms,
        "dynamic_fit": dynamic_fit,
        "rows": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
