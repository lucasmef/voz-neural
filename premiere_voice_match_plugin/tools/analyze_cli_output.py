#!/usr/bin/env python3
"""Compare DSP CLI output with the local raw/CapCut pairs."""

import json
import math
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "premiere_voice_match_plugin"
PAIR_ANALYSIS = ROOT / "analysis_capcut_pairs"
RAW_RATE = 48_000
TREATED_RATE = 44_100
EPS = 1e-12
BANDS = [
    ("sub_20_80", 20, 80),
    ("low_80_200", 80, 200),
    ("lowmid_200_500", 200, 500),
    ("mid_500_1000", 500, 1000),
    ("presence_1k_2k", 1000, 2000),
    ("clarity_2k_4k", 2000, 4000),
    ("sibilance_4k_8k", 4000, 8000),
    ("air_8k_16k", 8000, 16000),
]
DETAIL_CENTERS = [
    63,
    80,
    100,
    125,
    160,
    200,
    250,
    315,
    400,
    500,
    630,
    800,
    1000,
    1250,
    1600,
    2000,
    2500,
    3150,
    4000,
    5000,
    6300,
    8000,
    10000,
    12500,
    16000,
]
THIRD_OCTAVE_RATIO = 2 ** (1 / 6)
DETAIL_BANDS = [
    (f"{center:g}Hz", center / THIRD_OCTAVE_RATIO, center * THIRD_OCTAVE_RATIO)
    for center in DETAIL_CENTERS
]


def db(value):
    return 20.0 * math.log10(max(float(value), EPS))


def load(path):
    return np.fromfile(path, dtype="<f4").astype(np.float64)


def resample(samples, source_rate, target_rate):
    output_length = int(round(len(samples) * target_rate / source_rate))
    positions = np.arange(output_length, dtype=np.float64) * source_rate / target_rate
    return np.interp(positions, np.arange(len(samples)), samples)


def frame_db(samples, frame=480):
    count = len(samples) // frame
    data = samples[: count * frame].reshape(count, frame)
    return 20.0 * np.log10(np.sqrt(np.mean(data * data, axis=1) + EPS) + EPS)


def best_delay(reference, candidate, max_frames=10):
    reference_env = np.clip(frame_db(reference), -90.0, 0.0)
    candidate_env = np.clip(frame_db(candidate), -90.0, 0.0)
    best = (-2.0, 0)
    for delay in range(-max_frames, max_frames + 1):
        if delay >= 0:
            length = min(len(reference_env), len(candidate_env) - delay)
            a = reference_env[:length]
            b = candidate_env[delay : delay + length]
        else:
            length = min(len(reference_env) + delay, len(candidate_env))
            a = reference_env[-delay : -delay + length]
            b = candidate_env[:length]
        if length < 50:
            continue
        a = a - np.mean(a)
        b = b - np.mean(b)
        correlation = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + EPS))
        if correlation > best[0]:
            best = (correlation, delay)
    return best


def align(reference, candidate, delay_frames):
    delay = delay_frames * 480
    if delay >= 0:
        length = min(len(reference), len(candidate) - delay)
        return reference[:length], candidate[delay : delay + length]
    length = min(len(reference) + delay, len(candidate))
    return reference[-delay : -delay + length], candidate[:length]


def spectral_gains(reference, candidate, bands=BANDS):
    n_fft = 4096
    hop = 4800
    max_start = min(len(reference), len(candidate)) - n_fft
    starts = np.arange(0, max_start + 1, hop)
    levels = np.array(
        [
            np.sqrt(np.mean(reference[start : start + n_fft] ** 2) + EPS)
            for start in starts
        ]
    )
    threshold = max(10 ** (-42 / 20), float(np.percentile(levels, 55)))
    active_starts = starts[levels >= threshold]
    if len(active_starts) > 120:
        active_starts = active_starts[
            np.linspace(0, len(active_starts) - 1, 120).astype(int)
        ]
    window = np.hanning(n_fft)
    reference_power = np.zeros(n_fft // 2 + 1)
    candidate_power = np.zeros(n_fft // 2 + 1)
    for start in active_starts:
        reference_power += np.abs(
            np.fft.rfft(reference[start : start + n_fft] * window)
        ) ** 2
        candidate_power += np.abs(
            np.fft.rfft(candidate[start : start + n_fft] * window)
        ) ** 2
    frequencies = np.fft.rfftfreq(n_fft, 1.0 / RAW_RATE)
    gains = {}
    for name, low, high in bands:
        mask = (frequencies >= low) & (frequencies < high)
        gains[name] = 10.0 * math.log10(
            (float(np.sum(candidate_power[mask])) + EPS)
            / (float(np.sum(reference_power[mask])) + EPS)
        )
    return gains


def metrics(reference, candidate):
    reference_frames = frame_db(reference)
    candidate_frames = frame_db(candidate)
    count = min(len(reference_frames), len(candidate_frames))
    reference_frames = reference_frames[:count]
    candidate_frames = candidate_frames[:count]
    active = reference_frames >= max(-45.0, float(np.percentile(reference_frames, 55)))
    quiet = reference_frames <= np.percentile(reference_frames, 25)
    result = {
        "rms_dbfs": db(np.sqrt(np.mean(candidate * candidate) + EPS)),
        "peak_dbfs": db(np.max(np.abs(candidate))),
        "active_gain_db": float(np.median(candidate_frames[active] - reference_frames[active])),
        "quiet_gain_db": float(np.median(candidate_frames[quiet] - reference_frames[quiet])),
        "quiet_relative_db": float(
            np.median(candidate_frames[quiet] - reference_frames[quiet])
            - np.median(candidate_frames[active] - reference_frames[active])
        ),
        "compression_slope": float(np.polyfit(reference_frames[active], candidate_frames[active], 1)[0]),
    }
    spectral = spectral_gains(reference, candidate)
    result.update(
        {
            f"{name}_relative_db": gain - result["active_gain_db"]
            for name, gain in spectral.items()
        }
    )
    return result


def main():
    output_directory = PLUGIN / "analysis_cli"
    if "--output-dir" in sys.argv:
        index = sys.argv.index("--output-dir")
        output_directory = Path(sys.argv[index + 1]).resolve()

    analysis = json.loads((PAIR_ANALYSIS / "ANALYSIS_RESULT.json").read_text())
    by_id = {row["pair_id"]: row for row in analysis["pairs"]}
    rows = []
    for output_path in sorted(output_directory.glob("*.f32")):
        pair_id = output_path.stem
        if pair_id not in by_id:
            continue
        pair = by_id[pair_id]
        raw = load(ROOT / pair["raw_source"])
        treated = resample(
            load(PAIR_ANALYSIS / "decoded" / "treated" / f"{pair_id}.f32"),
            TREATED_RATE,
            RAW_RATE,
        )
        output = load(output_path)

        raw_corr, raw_delay = best_delay(raw, output)
        raw_aligned, output_aligned = align(raw, output, raw_delay)
        treated_corr, treated_delay = best_delay(treated, output)
        treated_aligned, _ = align(treated, output, treated_delay)
        capcut_corr, capcut_delay = best_delay(raw, treated)
        raw_for_capcut, capcut_aligned = align(raw, treated, capcut_delay)

        plugin_metrics = metrics(raw_aligned, output_aligned)
        capcut_metrics = metrics(raw_for_capcut, capcut_aligned)
        plugin_detail = spectral_gains(raw_aligned, output_aligned, DETAIL_BANDS)
        capcut_detail = spectral_gains(raw_for_capcut, capcut_aligned, DETAIL_BANDS)
        rows.append(
            {
                "pair_id": pair_id,
                "raw_delay_samples": raw_delay * 480,
                "raw_envelope_corr": raw_corr,
                "treated_delay_samples": treated_delay * 480,
                "treated_envelope_corr": treated_corr,
                "plugin": plugin_metrics,
                "plugin_detail_relative_db": {
                    name: gain - plugin_metrics["active_gain_db"]
                    for name, gain in plugin_detail.items()
                },
                "capcut": {
                    "rms_dbfs": pair["treated_rms_dbfs"],
                    "peak_dbfs": pair["treated_peak_dbfs"],
                    "active_gain_db": pair["active_voice_gain_db"],
                    "quiet_relative_db": pair["quiet_relative_to_voice_db"],
                    "compression_slope": pair["compression_slope"],
                    **{
                        f"{name}_relative_db": pair[f"{name}_relative_db"]
                        for name, _, _ in BANDS
                    },
                },
                "capcut_detail_relative_db": {
                    name: gain - capcut_metrics["active_gain_db"]
                    for name, gain in capcut_detail.items()
                },
                "plugin_vs_capcut_rms_error_db": plugin_metrics["rms_dbfs"]
                - pair["treated_rms_dbfs"],
            }
        )

    if "--summary" in sys.argv:
        errors = np.array([row["plugin_vs_capcut_rms_error_db"] for row in rows])
        peak_errors = np.array(
            [row["plugin"]["peak_dbfs"] - row["capcut"]["peak_dbfs"] for row in rows]
        )
        active_errors = np.array(
            [
                row["plugin"]["active_gain_db"] - row["capcut"]["active_gain_db"]
                for row in rows
            ]
        )
        quiet_errors = np.array(
            [
                row["plugin"]["quiet_relative_db"] - row["capcut"]["quiet_relative_db"]
                for row in rows
            ]
        )
        summary = {
            "pair_count": len(rows),
            "rms_error_median_db": float(np.median(errors)),
            "rms_error_mean_abs_db": float(np.mean(np.abs(errors))),
            "rms_error_range_db": [float(np.min(errors)), float(np.max(errors))],
            "peak_error_median_db": float(np.median(peak_errors)),
            "active_gain_error_median_db": float(np.median(active_errors)),
            "quiet_relative_error_median_db": float(np.median(quiet_errors)),
            "compression_slope_error_median": float(
                np.median(
                    [
                        row["plugin"]["compression_slope"]
                        - row["capcut"]["compression_slope"]
                        for row in rows
                    ]
                )
            ),
            "envelope_corr_to_capcut_median": float(
                np.median([row["treated_envelope_corr"] for row in rows])
            ),
            "spectral_error_median_db": {
                name: round(
                    float(
                        np.median(
                            [
                                row["plugin"][f"{name}_relative_db"]
                                - row["capcut"][f"{name}_relative_db"]
                                for row in rows
                            ]
                        )
                    ),
                    3,
                )
                for name, _, _ in BANDS
            },
            "third_octave_error_median_db": {
                name: round(
                    float(
                        np.median(
                            [
                                row["plugin_detail_relative_db"][name]
                                - row["capcut_detail_relative_db"][name]
                                for row in rows
                            ]
                        )
                    ),
                    3,
                )
                for name, _, _ in DETAIL_BANDS
            },
            "third_octave_plugin_median_db": {
                name: round(
                    float(np.median([row["plugin_detail_relative_db"][name] for row in rows])),
                    3,
                )
                for name, _, _ in DETAIL_BANDS
            },
            "third_octave_capcut_median_db": {
                name: round(
                    float(np.median([row["capcut_detail_relative_db"][name] for row in rows])),
                    3,
                )
                for name, _, _ in DETAIL_BANDS
            },
            "per_pair_rms_error_db": {
                row["pair_id"]: round(row["plugin_vs_capcut_rms_error_db"], 3)
                for row in rows
            },
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
