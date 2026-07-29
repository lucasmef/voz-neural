#!/usr/bin/env python3
"""Analyze paired raw/CapCut audio decoded as mono float32 streams."""

import csv
import json
import math
from pathlib import Path

import numpy as np


BASE = Path(__file__).resolve().parents[1]
ANALYSIS = BASE / "analysis_capcut_pairs"
DECODED = ANALYSIS / "decoded"
RAW_SR = 48_000
TREATED_SR = 44_100
EPS = 1e-12

DIRECT_RAW = {"64", "65", "67", "71", "73", "76"}
CANDIDATES = {
    "66": ["66_base"],
    "68": ["68_montagem", "68_facetune"],
    "69": ["69_montagem", "69_facetune"],
    "72": ["72_base", "72_facetune"],
    "74": ["74_montagem", "74_facetune"],
    "75": ["75_montagem", "75_facetune"],
    "77": ["77_facetune"],
    "81": ["81_base", "81_facetune"],
    "82": ["82_base", "82_facetune"],
    "84": ["84_base", "84_facetune"],
}
PAIR_IDS = [
    "64", "65", "66", "67", "68", "69", "71", "72",
    "73", "74", "75", "76", "77", "81", "82", "84",
]

BANDS = [
    ("sub_20_80", 20, 80),
    ("low_80_200", 80, 200),
    ("lowmid_200_500", 200, 500),
    ("mid_500_1000", 500, 1000),
    ("presence_1k_2k", 1000, 2000),
    ("clarity_2k_4k", 2000, 4000),
    ("sibilance_4k_8k", 4000, 8000),
    ("air_8k_16k", 8000, 16000),
    ("ultra_16k_20k", 16000, 20000),
]


def load_f32(path):
    return np.fromfile(path, dtype="<f4").astype(np.float64)


def resample_linear(samples, source_rate, target_rate):
    if source_rate == target_rate:
        return samples
    output_length = int(round(len(samples) * target_rate / source_rate))
    positions = np.arange(output_length, dtype=np.float64) * source_rate / target_rate
    return np.interp(positions, np.arange(len(samples)), samples)


def db(value):
    return 20.0 * math.log10(max(float(value), EPS))


def frame_rms(samples, frame_size):
    count = len(samples) // frame_size
    if count == 0:
        return np.array([], dtype=np.float64)
    frames = samples[: count * frame_size].reshape(count, frame_size)
    return np.sqrt(np.mean(frames * frames, axis=1) + EPS)


def envelope_db(samples, sample_rate, hop_ms=10):
    hop = max(1, int(round(sample_rate * hop_ms / 1000)))
    return 20.0 * np.log10(frame_rms(samples, hop) + EPS)


def best_envelope_alignment(raw, treated):
    raw_env = np.clip(envelope_db(raw, TREATED_SR), -80.0, 0.0)
    treated_env = np.clip(envelope_db(treated, TREATED_SR), -80.0, 0.0)
    max_lag = 300
    best = (-2.0, 0)
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            length = min(len(raw_env) - lag, len(treated_env))
            a = raw_env[lag : lag + length]
            b = treated_env[:length]
        else:
            length = min(len(raw_env), len(treated_env) + lag)
            a = raw_env[:length]
            b = treated_env[-lag : -lag + length]
        if length < 100:
            continue
        a = a - np.mean(a)
        b = b - np.mean(b)
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        corr = float(np.dot(a, b) / denom) if denom > EPS else -1.0
        if corr > best[0]:
            best = (corr, lag)
    corr, lag = best
    samples_per_hop = int(round(TREATED_SR * 0.010))
    if lag >= 0:
        return corr, lag * samples_per_hop, 0
    return corr, 0, -lag * samples_per_hop


def choose_raw(pair_id, treated):
    if pair_id in DIRECT_RAW:
        path = DECODED / "raw" / f"{pair_id}.f32"
        raw = resample_linear(load_f32(path), RAW_SR, TREATED_SR)
        corr, raw_start, treated_start = best_envelope_alignment(raw, treated)
        return path, raw, corr, raw_start, treated_start, "direct"

    scored = []
    for candidate in CANDIDATES[pair_id]:
        path = DECODED / "candidates" / f"{candidate}.f32"
        raw = resample_linear(load_f32(path), RAW_SR, TREATED_SR)
        corr, raw_start, treated_start = best_envelope_alignment(raw, treated)
        duration_difference = abs(len(raw) - len(treated)) / TREATED_SR
        score = corr - min(0.25, duration_difference * 0.02)
        scored.append(
            (score, corr, duration_difference, path, raw, raw_start, treated_start)
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    _, corr, _, path, raw, raw_start, treated_start = scored[0]
    return path, raw, corr, raw_start, treated_start, "candidate"


def spectral_band_gains(raw, treated, sample_rate):
    n_fft = 4096
    hop = int(round(sample_rate * 0.10))
    max_start = min(len(raw), len(treated)) - n_fft
    if max_start <= 0:
        return {name: float("nan") for name, _, _ in BANDS}

    starts = np.arange(0, max_start + 1, hop)
    raw_level = np.array(
        [
            np.sqrt(np.mean(raw[start : start + n_fft] ** 2) + EPS)
            for start in starts
        ]
    )
    threshold = max(10 ** (-42 / 20), float(np.percentile(raw_level, 55)))
    active_starts = starts[raw_level >= threshold]
    if len(active_starts) > 120:
        indices = np.linspace(0, len(active_starts) - 1, 120).astype(int)
        active_starts = active_starts[indices]

    window = np.hanning(n_fft)
    raw_power = np.zeros(n_fft // 2 + 1)
    treated_power = np.zeros(n_fft // 2 + 1)
    for start in active_starts:
        raw_fft = np.fft.rfft(raw[start : start + n_fft] * window)
        treated_fft = np.fft.rfft(treated[start : start + n_fft] * window)
        raw_power += np.abs(raw_fft) ** 2
        treated_power += np.abs(treated_fft) ** 2

    frequencies = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    gains = {}
    for name, low, high in BANDS:
        mask = (frequencies >= low) & (frequencies < high)
        raw_energy = float(np.sum(raw_power[mask])) + EPS
        treated_energy = float(np.sum(treated_power[mask])) + EPS
        gains[name] = 10.0 * math.log10(treated_energy / raw_energy)
    return gains


def analyze_pair(pair_id):
    treated_path = DECODED / "treated" / f"{pair_id}.f32"
    treated_full = load_f32(treated_path)
    raw_path, raw_full, match_corr, raw_start, treated_start, source_type = choose_raw(
        pair_id, treated_full
    )

    length = min(len(raw_full) - raw_start, len(treated_full) - treated_start)
    raw = raw_full[raw_start : raw_start + length]
    treated = treated_full[treated_start : treated_start + length]

    frame_size = int(round(TREATED_SR * 0.020))
    raw_frames = frame_rms(raw, frame_size)
    treated_frames = frame_rms(treated, frame_size)
    frame_count = min(len(raw_frames), len(treated_frames))
    raw_frame_db = 20.0 * np.log10(raw_frames[:frame_count] + EPS)
    treated_frame_db = 20.0 * np.log10(treated_frames[:frame_count] + EPS)

    voice_threshold = max(-45.0, float(np.percentile(raw_frame_db, 55)))
    voice_mask = raw_frame_db >= voice_threshold
    if np.count_nonzero(voice_mask) < 20:
        voice_mask = raw_frame_db >= np.percentile(raw_frame_db, 70)
    noise_threshold = float(np.percentile(raw_frame_db, 25))
    noise_mask = raw_frame_db <= noise_threshold

    active_gain = float(np.median(treated_frame_db[voice_mask] - raw_frame_db[voice_mask]))
    noise_gain = float(np.median(treated_frame_db[noise_mask] - raw_frame_db[noise_mask]))
    if np.std(raw_frame_db[voice_mask]) > 0.25:
        compression_slope = float(
            np.polyfit(raw_frame_db[voice_mask], treated_frame_db[voice_mask], 1)[0]
        )
    else:
        compression_slope = float("nan")

    raw_rms = float(np.sqrt(np.mean(raw * raw) + EPS))
    treated_rms = float(np.sqrt(np.mean(treated * treated) + EPS))
    raw_peak = float(np.max(np.abs(raw)))
    treated_peak = float(np.max(np.abs(treated)))

    centered_raw = raw - np.mean(raw)
    centered_treated = treated - np.mean(treated)
    denom = np.linalg.norm(centered_raw) * np.linalg.norm(centered_treated)
    waveform_corr = (
        float(np.dot(centered_raw, centered_treated) / denom) if denom > EPS else 0.0
    )

    spectral = spectral_band_gains(raw, treated, TREATED_SR)
    relative_spectral = {
        f"{name}_relative_db": gain - active_gain for name, gain in spectral.items()
    }

    row = {
        "pair_id": pair_id,
        "raw_source": str(raw_path.relative_to(BASE)),
        "source_type": source_type,
        "match_envelope_corr": match_corr,
        "alignment_ms": (treated_start - raw_start) * 1000.0 / TREATED_SR,
        "raw_duration_s": len(raw_full) / TREATED_SR,
        "treated_duration_s": len(treated_full) / TREATED_SR,
        "duration_delta_ms": (len(treated_full) - len(raw_full)) * 1000.0 / TREATED_SR,
        "raw_rms_dbfs": db(raw_rms),
        "treated_rms_dbfs": db(treated_rms),
        "global_rms_gain_db": db(treated_rms / raw_rms),
        "raw_peak_dbfs": db(raw_peak),
        "treated_peak_dbfs": db(treated_peak),
        "crest_raw_db": db(raw_peak / raw_rms),
        "crest_treated_db": db(treated_peak / treated_rms),
        "active_voice_gain_db": active_gain,
        "quiet_region_gain_db": noise_gain,
        "quiet_relative_to_voice_db": noise_gain - active_gain,
        "compression_slope": compression_slope,
        "waveform_corr": waveform_corr,
        "treated_active_median_dbfs": float(np.median(treated_frame_db[voice_mask])),
        "treated_active_p90_dbfs": float(np.percentile(treated_frame_db[voice_mask], 90)),
        "treated_quiet_p25_dbfs": float(np.percentile(treated_frame_db, 25)),
        "treated_clip_fraction": float(np.mean(np.abs(treated) >= 0.999)),
    }
    row.update(relative_spectral)
    return row


def finite_median(rows, key, minimum_match=0.55):
    values = [
        float(row[key])
        for row in rows
        if row["match_envelope_corr"] >= minimum_match
        and math.isfinite(float(row[key]))
    ]
    return float(np.median(values)) if values else float("nan")


def finite_iqr(rows, key, minimum_match=0.55):
    values = [
        float(row[key])
        for row in rows
        if row["match_envelope_corr"] >= minimum_match
        and math.isfinite(float(row[key]))
    ]
    if not values:
        return [float("nan"), float("nan")]
    return [float(np.percentile(values, 25)), float(np.percentile(values, 75))]


def main():
    rows = [analyze_pair(pair_id) for pair_id in PAIR_IDS]
    trusted = [row for row in rows if row["match_envelope_corr"] >= 0.55]
    keys = [
        "raw_rms_dbfs",
        "treated_rms_dbfs",
        "global_rms_gain_db",
        "raw_peak_dbfs",
        "treated_peak_dbfs",
        "crest_raw_db",
        "crest_treated_db",
        "active_voice_gain_db",
        "quiet_region_gain_db",
        "quiet_relative_to_voice_db",
        "compression_slope",
        "waveform_corr",
        "treated_active_median_dbfs",
        "treated_active_p90_dbfs",
        "treated_quiet_p25_dbfs",
    ] + [f"{name}_relative_db" for name, _, _ in BANDS]

    aggregate = {
        "pair_count": len(rows),
        "trusted_pair_count": len(trusted),
        "trusted_pair_ids": [row["pair_id"] for row in trusted],
        "excluded_low_match_ids": [
            row["pair_id"] for row in rows if row["match_envelope_corr"] < 0.55
        ],
        "median": {key: finite_median(rows, key) for key in keys},
        "iqr": {key: finite_iqr(rows, key) for key in keys},
    }

    csv_path = ANALYSIS / "PAIR_METRICS.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    json_path = ANALYSIS / "ANALYSIS_RESULT.json"
    json_path.write_text(
        json.dumps({"pairs": rows, "aggregate": aggregate}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(aggregate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
