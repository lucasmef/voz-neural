#!/usr/bin/env python3
"""Evaluate Premiere Enhance Speech followed by CapCut's Voice Focus chain."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

import analyze_cli_output as analysis


ROOT = Path(__file__).resolve().parents[2]
PAIR_ANALYSIS = ROOT / "analysis_capcut_pairs"
EPS = 1e-12


def biquad_coefficients(
    filter_type: str,
    sample_rate: float,
    frequency: float,
    q: float,
    gain_db: float,
) -> tuple[float, float, float, float, float]:
    amplitude = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * frequency / sample_rate
    cosine = math.cos(w0)
    sine = math.sin(w0)
    alpha = sine / (2.0 * q)

    if filter_type == "high_pass":
        b0 = (1.0 + cosine) * 0.5
        b1 = -(1.0 + cosine)
        b2 = (1.0 + cosine) * 0.5
        a0 = 1.0 + alpha
        a1 = -2.0 * cosine
        a2 = 1.0 - alpha
    elif filter_type == "peak":
        b0 = 1.0 + alpha * amplitude
        b1 = -2.0 * cosine
        b2 = 1.0 - alpha * amplitude
        a0 = 1.0 + alpha / amplitude
        a1 = -2.0 * cosine
        a2 = 1.0 - alpha / amplitude
    else:
        beta = 2.0 * math.sqrt(amplitude) * alpha
        if filter_type == "low_shelf":
            b0 = amplitude * (
                (amplitude + 1.0) - (amplitude - 1.0) * cosine + beta
            )
            b1 = 2.0 * amplitude * (
                (amplitude - 1.0) - (amplitude + 1.0) * cosine
            )
            b2 = amplitude * (
                (amplitude + 1.0) - (amplitude - 1.0) * cosine - beta
            )
            a0 = (
                (amplitude + 1.0)
                + (amplitude - 1.0) * cosine
                + beta
            )
            a1 = -2.0 * (
                (amplitude - 1.0) + (amplitude + 1.0) * cosine
            )
            a2 = (
                (amplitude + 1.0)
                + (amplitude - 1.0) * cosine
                - beta
            )
        elif filter_type == "high_shelf":
            b0 = amplitude * (
                (amplitude + 1.0) + (amplitude - 1.0) * cosine + beta
            )
            b1 = -2.0 * amplitude * (
                (amplitude - 1.0) + (amplitude + 1.0) * cosine
            )
            b2 = amplitude * (
                (amplitude + 1.0) + (amplitude - 1.0) * cosine - beta
            )
            a0 = (
                (amplitude + 1.0)
                - (amplitude - 1.0) * cosine
                + beta
            )
            a1 = 2.0 * (
                (amplitude - 1.0) - (amplitude + 1.0) * cosine
            )
            a2 = (
                (amplitude + 1.0)
                - (amplitude - 1.0) * cosine
                - beta
            )
        else:
            raise ValueError(filter_type)

    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def apply_biquad(
    samples: np.ndarray,
    coefficients: tuple[float, float, float, float, float],
) -> np.ndarray:
    b0, b1, b2, a1, a2 = coefficients
    output = np.empty_like(samples)
    z1 = 0.0
    z2 = 0.0
    for index, sample in enumerate(samples):
        value = b0 * sample + z1
        z1 = b1 * sample - a1 * value + z2
        z2 = b2 * sample - a2 * value
        output[index] = value
    return output


def apply_voice_focus_eq(samples: np.ndarray, sample_rate: float) -> np.ndarray:
    filters = (
        ("low_shelf", 150.0, 1.1, 6.8),
        ("peak", 250.0, 1.0, 9.24),
        ("peak", 500.0, 1.0, 5.26),
        ("peak", 1000.0, 1.0, 2.92),
        ("peak", 2000.0, 1.0, 3.12),
        ("peak", 3000.0, 1.0, 1.92),
        ("high_shelf", 8000.0, 1.0, 3.56),
    )
    output = samples.astype(np.float64, copy=True)
    for filter_type, frequency, q, gain_db in filters:
        output = apply_biquad(
            output,
            biquad_coefficients(
                filter_type, sample_rate, frequency, q, gain_db
            ),
        )
    return output


def apply_premiere_match_eq(
    samples: np.ndarray, sample_rate: float
) -> np.ndarray:
    """Correct the repeatable difference after Enhance Speech + Auto-Match.

    These bands were fitted to the median difference of female pairs 64, 65,
    and 67. The two high-pass sections reproduce the steep sub-bass rejection
    seen in the CapCut outputs without thinning the 160-315 Hz voice body.
    """

    filters = (
        ("high_pass", 75.0, 0.707, 0.0),
        ("high_pass", 75.0, 0.707, 0.0),
        ("peak", 800.0, 0.75, 1.0),
        ("peak", 1500.0, 0.85, 2.2),
        ("peak", 3200.0, 0.80, -2.8),
        ("peak", 6500.0, 0.90, -1.2),
        ("high_shelf", 9000.0, 0.707, 1.8),
    )
    output = samples.astype(np.float64, copy=True)
    for filter_type, frequency, q, gain_db in filters:
        output = apply_biquad(
            output,
            biquad_coefficients(
                filter_type, sample_rate, frequency, q, gain_db
            ),
        )
    return output


def apply_premiere_match70_eq(
    samples: np.ndarray, sample_rate: float
) -> np.ndarray:
    """Correction fitted for Enhance Speech at 7.0 plus -23 LUFS Auto-Match."""

    filters = (
        ("high_pass", 75.0, 0.707, 0.0),
        ("high_pass", 75.0, 0.707, 0.0),
        ("peak", 800.0, 0.75, 1.0),
        ("peak", 3200.0, 0.80, -4.0),
        ("peak", 6500.0, 0.90, 1.8),
        ("high_shelf", 9000.0, 0.707, 4.8),
    )
    output = samples.astype(np.float64, copy=True)
    for filter_type, frequency, q, gain_db in filters:
        output = apply_biquad(
            output,
            biquad_coefficients(
                filter_type, sample_rate, frequency, q, gain_db
            ),
        )
    return output


def apply_limiter(
    samples: np.ndarray,
    sample_rate: float,
    ceiling_db: float = -0.3,
    release_ms: float = 500.0,
) -> np.ndarray:
    ceiling = 10.0 ** (ceiling_db / 20.0)
    release = math.exp(-1.0 / (sample_rate * release_ms / 1000.0))
    output = np.empty_like(samples)
    gain = 1.0
    for index, sample in enumerate(samples):
        absolute = abs(sample)
        required = min(1.0, ceiling / max(absolute, EPS))
        if required < gain:
            gain = required
        else:
            gain = release * gain + (1.0 - release) * required
        output[index] = sample * gain
    return output


def compare(
    raw: np.ndarray,
    candidate: np.ndarray,
    capcut_metrics: dict[str, float],
) -> dict[str, float | dict[str, float]]:
    correlation, delay = analysis.best_delay(raw, candidate)
    raw_aligned, candidate_aligned = analysis.align(raw, candidate, delay)
    metrics = analysis.metrics(raw_aligned, candidate_aligned)
    spectral_errors = {
        name: metrics[f"{name}_relative_db"]
        - capcut_metrics[f"{name}_relative_db"]
        for name, _, _ in analysis.BANDS
    }
    return {
        "envelope_correlation_to_raw": correlation,
        "delay_samples": delay * 480,
        "rms_dbfs": metrics["rms_dbfs"],
        "rms_error_db": metrics["rms_dbfs"] - capcut_metrics["rms_dbfs"],
        "peak_dbfs": metrics["peak_dbfs"],
        "peak_error_db": metrics["peak_dbfs"] - capcut_metrics["peak_dbfs"],
        "active_gain_error_db": (
            metrics["active_gain_db"] - capcut_metrics["active_gain_db"]
        ),
        "quiet_relative_error_db": (
            metrics["quiet_relative_db"] - capcut_metrics["quiet_relative_db"]
        ),
        "compression_slope_error": (
            metrics["compression_slope"] - capcut_metrics["compression_slope"]
        ),
        "spectral_error_db": spectral_errors,
        "spectral_mean_abs_error_db": float(
            np.mean(np.abs(list(spectral_errors.values())))
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pair_id")
    parser.add_argument("premiere_f32", type=Path)
    parser.add_argument(
        "--gains",
        default="0,2,4,6,8,10,12",
        help="Comma-separated gains in dB before the limiter.",
    )
    parser.add_argument(
        "--mode",
        choices=("focus", "match", "match70", "none"),
        default="focus",
        help="EQ chain to test before gain and limiting.",
    )
    args = parser.parse_args()

    pair_rows = json.loads(
        (PAIR_ANALYSIS / "ANALYSIS_RESULT.json").read_text()
    )["pairs"]
    pair = next(row for row in pair_rows if row["pair_id"] == args.pair_id)
    raw = analysis.load(ROOT / pair["raw_source"])
    premiere = analysis.load(args.premiere_f32)
    capcut_metrics = {
        "rms_dbfs": pair["treated_rms_dbfs"],
        "peak_dbfs": pair["treated_peak_dbfs"],
        "active_gain_db": pair["active_voice_gain_db"],
        "quiet_relative_db": pair["quiet_relative_to_voice_db"],
        "compression_slope": pair["compression_slope"],
        **{
            f"{name}_relative_db": pair[f"{name}_relative_db"]
            for name, _, _ in analysis.BANDS
        },
    }

    if args.mode == "focus":
        focused = apply_voice_focus_eq(premiere, analysis.RAW_RATE)
    elif args.mode == "match":
        focused = apply_premiere_match_eq(premiere, analysis.RAW_RATE)
    elif args.mode == "match70":
        focused = apply_premiere_match70_eq(premiere, analysis.RAW_RATE)
    else:
        focused = premiere
    results = []
    for gain_db in (float(value) for value in args.gains.split(",")):
        gained = focused * (10.0 ** (gain_db / 20.0))
        limited = apply_limiter(gained, analysis.RAW_RATE)
        results.append(
            {
                "gain_db": gain_db,
                **compare(raw, limited, capcut_metrics),
            }
        )

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
