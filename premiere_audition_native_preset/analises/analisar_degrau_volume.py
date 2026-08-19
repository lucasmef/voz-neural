#!/usr/bin/env python3
"""Mede mudanca temporal de nivel entre um candidato e uma referencia."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import comparar_wavs_adobe as base


def db(value):
    return float(20.0 * np.log10(max(float(value), 1e-12)))


def region_summary(candidate, target, frame_mask, frame_size):
    sample_mask = np.repeat(frame_mask, frame_size)
    size = min(sample_mask.size, candidate.size, target.size)
    sample_mask = sample_mask[:size]
    cand = candidate[:size][sample_mask]
    ref = target[:size][sample_mask]
    if cand.size == 0:
        return None
    return {
        "active_seconds": float(cand.size / 48_000.0),
        "candidate_rms_dbfs": db(np.sqrt(np.mean(cand.astype(np.float64) ** 2))),
        "target_rms_dbfs": db(np.sqrt(np.mean(ref.astype(np.float64) ** 2))),
        "candidate_minus_target_db": db(np.sqrt(np.mean(cand.astype(np.float64) ** 2)))
        - db(np.sqrt(np.mean(ref.astype(np.float64) ** 2))),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rate = 48_000
    frame_ms = 20.0
    frame_size = int(rate * frame_ms / 1000.0)
    candidate_wav = base.read_pcm_wav(args.candidate)
    target_wav = base.read_pcm_wav(args.target)
    candidate = base.sinc_resample(base.mono_mean(candidate_wav.samples), candidate_wav.sample_rate, rate)
    target = base.sinc_resample(base.mono_mean(target_wav.samples), target_wav.sample_rate, rate)
    lag, corr = base.best_placement_lag(target, candidate, rate)
    target, candidate, _duplicate, start, stop = base.common_overlap(target, candidate, candidate, lag, lag)

    frame_count = min(candidate.size, target.size) // frame_size
    candidate = candidate[: frame_count * frame_size]
    target = target[: frame_count * frame_size]
    cand_env = base.frame_rms(candidate, frame_size)
    target_env = base.frame_rms(target, frame_size)
    cand_db = 20.0 * np.log10(np.maximum(cand_env, 1e-12))
    target_db = 20.0 * np.log10(np.maximum(target_env, 1e-12))
    target_threshold = max(float(np.quantile(target_db, 0.55)), -45.0)
    active = (target_db >= target_threshold) & (cand_db >= -55.0)
    delta = cand_db - target_db
    times = (np.arange(frame_count) + 0.5) * frame_size / rate

    windows = []
    for left in np.arange(0.0, times[-1] + 0.001, 5.0):
        right = min(float(times[-1] + frame_ms / 2000.0), left + 5.0)
        region = (times >= left) & (times < right) & active
        summary = region_summary(candidate, target, region, frame_size)
        if summary:
            values = delta[region]
            summary.update(
                {
                    "start_seconds": float(left),
                    "stop_seconds": float(right),
                    "active_frames": int(region.sum()),
                    "frame_delta_median_db": float(np.median(values)),
                    "frame_delta_p10_db": float(np.quantile(values, 0.10)),
                    "frame_delta_p90_db": float(np.quantile(values, 0.90)),
                }
            )
            windows.append(summary)

    step_candidates = []
    for split in np.arange(4.0, times[-1] - 4.0, 0.1):
        before = active & (times >= split - 4.0) & (times < split)
        after = active & (times >= split) & (times < split + 4.0)
        if before.sum() < 25 or after.sum() < 25:
            continue
        med_before = float(np.median(delta[before]))
        med_after = float(np.median(delta[after]))
        candidate_before = float(np.median(cand_db[before]))
        candidate_after = float(np.median(cand_db[after]))
        target_before = float(np.median(target_db[before]))
        target_after = float(np.median(target_db[after]))
        step_candidates.append(
            {
                "split_seconds": float(split),
                "before_median_db": med_before,
                "after_median_db": med_after,
                "step_db": med_after - med_before,
                "candidate_before_dbfs": candidate_before,
                "candidate_after_dbfs": candidate_after,
                "candidate_step_db": candidate_after - candidate_before,
                "target_before_dbfs": target_before,
                "target_after_dbfs": target_after,
                "target_step_db": target_after - target_before,
                "before_frames": int(before.sum()),
                "after_frames": int(after.sum()),
            }
        )
    max_positive = max(step_candidates, key=lambda item: item["step_db"])
    max_negative = min(step_candidates, key=lambda item: item["step_db"])
    max_candidate_positive = max(step_candidates, key=lambda item: item["candidate_step_db"])
    max_candidate_negative = min(step_candidates, key=lambda item: item["candidate_step_db"])

    middle = float(times[-1] / 2.0)
    halves = {}
    for name, left, right in (("first_half", 0.0, middle), ("second_half", middle, float(times[-1] + 1.0))):
        region = active & (times >= left) & (times < right)
        summary = region_summary(candidate, target, region, frame_size)
        values = delta[region]
        summary.update(
            {
                "frame_delta_median_db": float(np.median(values)),
                "frame_delta_p10_db": float(np.quantile(values, 0.10)),
                "frame_delta_p90_db": float(np.quantile(values, 0.90)),
            }
        )
        halves[name] = summary

    result = {
        "candidate": str(args.candidate.resolve()),
        "target": str(args.target.resolve()),
        "analysis_rate": rate,
        "frame_ms": frame_ms,
        "alignment": {
            "lag_samples": int(lag),
            "lag_ms": float(lag * 1000.0 / rate),
            "correlation": float(corr),
            "common_start": int(start),
            "common_stop": int(stop),
        },
        "active_definition": {
            "target_frame_threshold_dbfs": target_threshold,
            "active_frames": int(active.sum()),
            "active_seconds": float(active.sum() * frame_size / rate),
        },
        "halves": halves,
        "best_positive_step": max_positive,
        "best_negative_step": max_negative,
        "best_candidate_positive_step": max_candidate_positive,
        "best_candidate_negative_step": max_candidate_negative,
        "windows_5s": windows,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "95_DEGRAU_VOLUME.json"
    md_path = args.output_dir / "95_DEGRAU_VOLUME.md"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = [
        "# Analise temporal de nivel — 95",
        "",
        f"Lag: `{lag * 1000.0 / rate:+.3f} ms`; correlacao de alinhamento: `{corr:.4f}`.",
        "",
        "## Metades",
        "",
        "| Regiao | Candidato - CapCut RMS | Mediana por frame |",
        "|---|---:|---:|",
    ]
    for name, summary in halves.items():
        rows.append(f"| {name} | {summary['candidate_minus_target_db']:+.3f} dB | {summary['frame_delta_median_db']:+.3f} dB |")
    rows.extend(
        [
            "",
            "## Maior degrau positivo candidato/CapCut",
            "",
            f"Em torno de `{max_positive['split_seconds']:.2f} s`: "
            f"`{max_positive['before_median_db']:+.3f}` → `{max_positive['after_median_db']:+.3f} dB`, "
            f"degrau `{max_positive['step_db']:+.3f} dB`.",
            "",
            "## Janelas de 5 s",
            "",
            "| Janela | RMS candidato-CapCut | Mediana por frame | P10 | P90 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for item in windows:
        rows.append(
            f"| {item['start_seconds']:.0f}–{item['stop_seconds']:.0f} s | "
            f"{item['candidate_minus_target_db']:+.3f} | {item['frame_delta_median_db']:+.3f} | "
            f"{item['frame_delta_p10_db']:+.3f} | {item['frame_delta_p90_db']:+.3f} |"
        )
    md_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "best_positive_step": max_positive, "best_candidate_positive_step": max_candidate_positive, "halves": halves}, indent=2))


if __name__ == "__main__":
    main()
