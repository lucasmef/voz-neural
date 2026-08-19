#!/usr/bin/env python3
"""Separa piso continuo, trechos baixos e caudas pos-fala em um trio de audio."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import comparar_wavs_adobe as base


def db(value: float) -> float:
    return float(20.0 * np.log10(max(float(value), 1e-12)))


def rms(values: np.ndarray) -> float:
    return float(np.sqrt(np.mean(values.astype(np.float64) ** 2)))


def load(path: Path, rate: int) -> np.ndarray:
    wav = base.read_pcm_wav(path)
    return base.sinc_resample(base.mono_mean(wav.samples), wav.sample_rate, rate)


def contiguous_regions(mask: np.ndarray, frame_seconds: float, minimum_seconds: float) -> list[dict[str, float]]:
    padded = np.pad(mask.astype(np.int8), (1, 1))
    edges = np.diff(padded)
    starts = np.flatnonzero(edges == 1)
    stops = np.flatnonzero(edges == -1)
    regions = []
    for start, stop in zip(starts, stops, strict=True):
        if (stop - start) * frame_seconds >= minimum_seconds:
            regions.append(
                {
                    "start_seconds": float(start * frame_seconds),
                    "stop_seconds": float(stop * frame_seconds),
                    "duration_seconds": float((stop - start) * frame_seconds),
                }
            )
    return regions


def summarize(
    name: str,
    frame_mask: np.ndarray,
    raw: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    frame_size: int,
    rate: int,
    voice_match_gain_db: float,
) -> dict:
    sample_mask = np.repeat(frame_mask, frame_size)[: raw.size]
    cand = candidate[sample_mask]
    ref = target[sample_mask]
    raw_selected = raw[sample_mask]
    if not cand.size:
        return {"name": name, "frames": 0}
    candidate_env = base.frame_rms(candidate, frame_size)[frame_mask]
    target_env = base.frame_rms(target, frame_size)[frame_mask]
    candidate_power, candidate_windows = base.accumulated_spectrum(candidate, sample_mask)
    target_power, target_windows = base.accumulated_spectrum(target, sample_mask)
    spectrum = base.spectral_comparison(
        candidate_power,
        target_power,
        rate,
        voice_match_gain_db,
        candidate_windows,
        target_windows,
    )
    candidate_levels = 20.0 * np.log10(np.maximum(candidate_env, 1e-12))
    target_levels = 20.0 * np.log10(np.maximum(target_env, 1e-12))
    absolute_delta = db(rms(cand)) - db(rms(ref))
    return {
        "name": name,
        "frames": int(frame_mask.sum()),
        "seconds": float(frame_mask.sum() * frame_size / rate),
        "raw_rms_dbfs": db(rms(raw_selected)),
        "candidate_rms_dbfs": db(rms(cand)),
        "target_rms_dbfs": db(rms(ref)),
        "absolute_candidate_minus_target_db": absolute_delta,
        "after_voice_match_candidate_minus_target_db": absolute_delta + voice_match_gain_db,
        "candidate_median_frame_dbfs": float(np.median(candidate_levels)),
        "target_median_frame_dbfs": float(np.median(target_levels)),
        "median_after_voice_match_delta_db": float(
            np.median(candidate_levels) + voice_match_gain_db - np.median(target_levels)
        ),
        "candidate_p10_frame_dbfs": float(np.quantile(candidate_levels, 0.10)),
        "candidate_p90_frame_dbfs": float(np.quantile(candidate_levels, 0.90)),
        "target_p10_frame_dbfs": float(np.quantile(target_levels, 0.10)),
        "target_p90_frame_dbfs": float(np.quantile(target_levels, 0.90)),
        "log_envelope_correlation": float(
            base.normalized_corr(np.log10(candidate_env + 1e-12), np.log10(target_env + 1e-12))
        ),
        "spectral_after_voice_match": spectrum,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rate = 48_000
    frame_size = 960
    frame_seconds = frame_size / rate
    raw = load(args.raw, rate)
    candidate = load(args.candidate, rate)
    target = load(args.target, rate)
    raw_lag, _raw_corr = base.best_placement_lag(raw, candidate, rate)
    target_lag, _target_corr = base.best_placement_lag(target, candidate, rate)
    raw, candidate, _copy, _start, _stop = base.common_overlap(
        raw, candidate, candidate, raw_lag, raw_lag
    )
    target, candidate_for_target, _copy, _start, _stop = base.common_overlap(
        target, candidate, candidate, target_lag, target_lag
    )
    common = min(raw.size, candidate.size, target.size, candidate_for_target.size)
    frame_count = common // frame_size
    common = frame_count * frame_size
    raw = raw[:common]
    candidate = candidate[:common]
    target = target[:common]

    raw_env = base.frame_rms(raw, frame_size)
    raw_db = 20.0 * np.log10(np.maximum(raw_env, 1e-12))
    candidate_env = base.frame_rms(candidate, frame_size)
    target_env = base.frame_rms(target, frame_size)
    active = raw_db >= -42.0
    voice_match_gain_db = db(rms(target[np.repeat(active, frame_size)])) - db(
        rms(candidate[np.repeat(active, frame_size)])
    )

    near_voice = np.convolve(active.astype(np.int16), np.ones(31, dtype=np.int16), mode="same") > 0
    deep_silence = (raw_db <= -58.0) & ~near_voice
    low_level = (raw_db > -58.0) & (raw_db <= -45.0)

    post_speech = np.zeros(frame_count, dtype=bool)
    active_edges = np.flatnonzero(active[:-1] & ~active[1:])
    for edge in active_edges:
        start = edge + 1 + 5
        stop = min(frame_count, edge + 1 + 30)
        if start < stop:
            post_speech[start:stop] = raw_db[start:stop] < -42.0

    times = (np.arange(frame_count) + 0.5) * frame_seconds
    explicit_silence = (times >= 6.80) & (times < 7.75)

    deep_regions = contiguous_regions(deep_silence, frame_seconds, 0.30)
    deep_region_summaries = []
    for index, region in enumerate(deep_regions, start=1):
        region_mask = np.zeros(frame_count, dtype=bool)
        region_start = int(round(region["start_seconds"] / frame_seconds))
        region_stop = int(round(region["stop_seconds"] / frame_seconds))
        region_mask[region_start:region_stop] = True
        summary = summarize(
            f"deep_silence_{index}",
            region_mask,
            raw,
            candidate,
            target,
            frame_size,
            rate,
            voice_match_gain_db,
        )
        summary.update(region)
        deep_region_summaries.append(summary)

    result = {
        "inputs": {
            "raw": str(args.raw.resolve()),
            "candidate": str(args.candidate.resolve()),
            "target": str(args.target.resolve()),
        },
        "voice_match_gain_db_applied_to_candidate_for_comparison": voice_match_gain_db,
        "selection": {
            "frame_ms": frame_seconds * 1000.0,
            "active_raw_threshold_dbfs": -42.0,
            "deep_silence_raw_threshold_dbfs": -58.0,
            "deep_silence_minimum_distance_from_active_ms": 300.0,
            "post_speech_window_ms": [100.0, 600.0],
        },
        "deep_silence_regions": deep_regions,
        "deep_silence_region_summaries": deep_region_summaries,
        "regions": [
            summarize("deep_silence", deep_silence, raw, candidate, target, frame_size, rate, voice_match_gain_db),
            summarize("low_level_raw", low_level, raw, candidate, target, frame_size, rate, voice_match_gain_db),
            summarize("post_speech_100_600ms", post_speech, raw, candidate, target, frame_size, rate, voice_match_gain_db),
            summarize("explicit_silence_6p8_7p75", explicit_silence, raw, candidate, target, frame_size, rate, voice_match_gain_db),
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
