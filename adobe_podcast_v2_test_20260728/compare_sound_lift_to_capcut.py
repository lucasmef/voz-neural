#!/usr/bin/env python3
"""Rank Audition Sound Lift stem mixes against isolated CapCut references.

The analysis is deliberately independent from the existing CapCut reports.  It
uses the project's AVFoundation decoder, converts every input to mono float32,
resamples to the CapCut reference rate, aligns each candidate by RMS envelope,
and reports both waveform- and perceptually-oriented proxy metrics.

Sound Lift mix law:

    candidate = speech*S + background*B + noise*N + reverb*R

This is a measurement tool, not a claim that a linear stem mix is sufficient to
reproduce CapCut's nonlinear denoise/EQ/dynamics stages.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - environment diagnostic
    raise SystemExit(
        "NumPy is required. Run this script with the Codex workspace Python "
        "runtime (or another Python 3 environment containing NumPy)."
    ) from exc


EPS = 1e-12
STEM_NAMES = ("speech", "background", "noise", "reverb")
DEFAULT_VARIANTS = {
    "speech_only": (1.0, 0.0, 0.0, 0.0),
    "speech_bg12": (1.0, 0.125, 0.0, 0.0),
    "speech_bg25": (1.0, 0.25, 0.0, 0.0),
    "speech_bg50": (1.0, 0.50, 0.0, 0.0),
    "speech_reverb12": (1.0, 0.0, 0.0, 0.125),
    "speech_reverb25": (1.0, 0.0, 0.0, 0.25),
    "speech_bg12_reverb12": (1.0, 0.125, 0.0, 0.125),
    "speech_bg25_reverb12": (1.0, 0.25, 0.0, 0.125),
    "speech_bg25_reverb25": (1.0, 0.25, 0.0, 0.25),
    "speech_residual12": (1.0, 0.125, 0.125, 0.125),
    "speech_residual25": (1.0, 0.25, 0.25, 0.25),
}
BANDS = (
    ("sub_20_80", 20.0, 80.0),
    ("low_80_200", 80.0, 200.0),
    ("body_200_500", 200.0, 500.0),
    ("mid_500_1000", 500.0, 1000.0),
    ("presence_1k_2k", 1000.0, 2000.0),
    ("clarity_2k_4k", 2000.0, 4000.0),
    ("sibilance_4k_8k", 4000.0, 8000.0),
    ("air_8k_16k", 8000.0, 16000.0),
    ("ultra_16k_20k", 16000.0, 20000.0),
)
SCORE_FORMULA = {
    "scope": (
        "voice-relevant clips: speech_quality, speech_noise_mixes, and "
        "voice_focus except music-only"
    ),
    "shape_cost_lower_is_better": (
        "50*global_level_matched_error_ratio + "
        "10*(1-global_cosine_correlation) + "
        "15*(1-median_envelope_correlation) + "
        "1.5*median_spectral_rmse_db + "
        "1.0*median_abs_dynamics_contrast_error_db + "
        "0.5*std_per_clip_fitted_gain_db"
    ),
    "delivery_cost_lower_is_better": (
        "shape_cost + abs(global_unmatched_level_delta_db)"
    ),
    "warning": (
        "The score is an engineering ranking proxy, not a listening-test MOS. "
        "Inspect the component metrics and exported top candidates."
    ),
}


@dataclass
class Audio:
    name: str
    source: Path
    sample_rate: int
    samples: np.memmap

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


@dataclass(frozen=True)
class Clip:
    order: int
    category: str
    file: str
    start: int
    stop: int

    @property
    def voice_relevant(self) -> bool:
        if self.file.startswith("F05_"):
            return False
        return self.category.startswith(("10_", "30_", "40_"))


def amp_db(value: float) -> float:
    return 20.0 * math.log10(max(abs(float(value)), 1e-30))


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def median(values: Iterable[object], default: float = 0.0) -> float:
    selected = [number for value in values if (number := finite_float(value)) is not None]
    return float(np.median(selected)) if selected else default


def stddev(values: Iterable[object], default: float = 0.0) -> float:
    selected = [number for value in values if (number := finite_float(value)) is not None]
    return float(np.std(selected)) if selected else default


def sanitize_json(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, Path):
        return str(value)
    return value


def parse_decoder_stdout(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "sample_rate" in value and "frames" in value:
            return value
    raise ValueError(f"Decoder did not emit metadata JSON:\n{stdout}")


def decode_native(
    name: str,
    source: Path,
    temp_dir: Path,
    decoder: Path,
) -> Audio:
    if not source.is_file():
        raise FileNotFoundError(f"{name}: input not found: {source}")
    output = temp_dir / f"{name}_native.f32"
    completed = subprocess.run(
        [str(decoder), str(source), str(output)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    metadata = parse_decoder_stdout(completed.stdout)
    frames = int(metadata["frames"])
    rate = int(round(float(metadata["sample_rate"])))
    expected_bytes = frames * np.dtype("<f4").itemsize
    if output.stat().st_size != expected_bytes:
        raise ValueError(
            f"{name}: decoded size mismatch ({output.stat().st_size} != {expected_bytes})"
        )
    samples = np.memmap(output, dtype="<f4", mode="r", shape=(frames,))
    return Audio(name, source, rate, samples)


def resample_audio(
    audio: Audio,
    target_rate: int,
    temp_dir: Path,
    chunk_frames: int = 1_000_000,
) -> Audio:
    if audio.sample_rate == target_rate:
        return audio
    output_frames = int(round(audio.frames * target_rate / audio.sample_rate))
    output_path = temp_dir / f"{audio.name}_{target_rate}.f32"
    output = np.memmap(output_path, dtype="<f4", mode="w+", shape=(output_frames,))
    ratio = audio.sample_rate / target_rate
    for start in range(0, output_frames, chunk_frames):
        stop = min(start + chunk_frames, output_frames)
        positions = np.arange(start, stop, dtype=np.float64) * ratio
        lower = np.floor(positions).astype(np.int64)
        lower = np.minimum(lower, max(audio.frames - 1, 0))
        upper = np.minimum(lower + 1, max(audio.frames - 1, 0))
        fraction = positions - lower
        left = np.asarray(audio.samples[lower], dtype=np.float64)
        right = np.asarray(audio.samples[upper], dtype=np.float64)
        output[start:stop] = left + fraction * (right - left)
    output.flush()
    return Audio(audio.name, audio.source, target_rate, output)


def decode_at_rate(
    name: str,
    source: Path,
    target_rate: int,
    temp_dir: Path,
    decoder: Path,
) -> Audio:
    return resample_audio(
        decode_native(name, source, temp_dir, decoder), target_rate, temp_dir
    )


def load_timecodes(path: Path, sample_rate: int) -> list[Clip]:
    clips: list[Clip] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            start = round(int(row["inicio_us"]) * sample_rate / 1_000_000)
            stop = round(
                (int(row["inicio_us"]) + int(row["duracao_us"]))
                * sample_rate
                / 1_000_000
            )
            clips.append(
                Clip(
                    order=int(row["ordem"]),
                    category=row["categoria"],
                    file=row["arquivo"],
                    start=start,
                    stop=stop,
                )
            )
    if not clips:
        raise ValueError(f"No timecodes in {path}")
    return clips


def parse_variant(specification: str) -> tuple[str, tuple[float, float, float, float]]:
    if "=" not in specification:
        raise argparse.ArgumentTypeError(
            "Variant must be NAME=SPEECH,BACKGROUND,NOISE,REVERB"
        )
    name, raw_values = specification.split("=", 1)
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    try:
        values = tuple(float(value.strip()) for value in raw_values.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid variant: {specification}") from exc
    if not name or len(values) != 4 or any(not math.isfinite(value) for value in values):
        raise argparse.ArgumentTypeError(
            "Variant must be NAME=SPEECH,BACKGROUND,NOISE,REVERB"
        )
    return name, values  # type: ignore[return-value]


def mix_to_memmap(
    name: str,
    coefficients: Sequence[float],
    stems: dict[str, Audio | None],
    output_path: Path,
    chunk_frames: int = 750_000,
) -> Audio:
    available = [stem for stem in stems.values() if stem is not None]
    if not available:
        raise ValueError("No Sound Lift stems were provided")
    rate = available[0].sample_rate
    frames = max(stem.frames for stem in available)
    output = np.memmap(output_path, dtype="<f4", mode="w+", shape=(frames,))
    for start in range(0, frames, chunk_frames):
        stop = min(start + chunk_frames, frames)
        block = np.zeros(stop - start, dtype=np.float64)
        for coefficient, stem_name in zip(coefficients, STEM_NAMES):
            stem = stems[stem_name]
            if stem is None or abs(coefficient) < EPS or start >= stem.frames:
                continue
            stem_stop = min(stop, stem.frames)
            block[: stem_stop - start] += coefficient * np.asarray(
                stem.samples[start:stem_stop], dtype=np.float64
            )
        output[start:stop] = block
    output.flush()
    return Audio(name, output_path, rate, output)


def log_rms_envelope(samples: np.ndarray, hop: int) -> np.ndarray:
    count = len(samples) // hop
    if count <= 0:
        return np.empty(0, dtype=np.float64)
    blocks = np.asarray(samples[: count * hop], dtype=np.float32).reshape(count, hop)
    rms = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1) + EPS)
    return np.clip(20.0 * np.log10(rms + EPS), -100.0, 3.0)


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3 or len(b) != len(a):
        return 0.0
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    x -= np.mean(x)
    y -= np.mean(y)
    denominator = float(np.linalg.norm(x) * np.linalg.norm(y))
    return float(np.dot(x, y) / denominator) if denominator > EPS else 0.0


def lag_score(
    reference_envelope: np.ndarray,
    candidate_envelope: np.ndarray,
    clips: Sequence[Clip],
    hop: int,
    lag_hops: int,
) -> float:
    reference_parts: list[np.ndarray] = []
    candidate_parts: list[np.ndarray] = []
    for clip in clips:
        start = max(0, clip.start // hop)
        stop = min(len(reference_envelope), clip.stop // hop)
        candidate_start = start + lag_hops
        candidate_stop = stop + lag_hops
        if candidate_start < 0:
            start -= candidate_start
            candidate_start = 0
        if candidate_stop > len(candidate_envelope):
            overflow = candidate_stop - len(candidate_envelope)
            stop -= overflow
            candidate_stop -= overflow
        if stop - start < 8:
            continue
        reference_parts.append(reference_envelope[start:stop])
        candidate_parts.append(candidate_envelope[candidate_start:candidate_stop])
    if not reference_parts:
        return -1.0
    return correlation(np.concatenate(reference_parts), np.concatenate(candidate_parts))


def estimate_lag_hops(
    reference_envelope: np.ndarray,
    candidate_envelope: np.ndarray,
    clips: Sequence[Clip],
    hop: int,
    center: int,
    radius: int,
) -> tuple[int, float]:
    best_lag = center
    best_score = -2.0
    for lag in range(center - radius, center + radius + 1):
        score = lag_score(reference_envelope, candidate_envelope, clips, hop, lag)
        if score > best_score:
            best_lag, best_score = lag, score
    return best_lag, best_score


def aligned_views(
    reference: Audio,
    candidate: Audio,
    start: int,
    stop: int,
    lag_samples: int,
) -> tuple[np.ndarray, np.ndarray]:
    reference_start = max(0, start, -lag_samples)
    reference_stop = min(stop, reference.frames, candidate.frames - lag_samples)
    if reference_stop <= reference_start:
        return np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32)
    target = np.asarray(
        reference.samples[reference_start:reference_stop], dtype=np.float64
    )
    test = np.asarray(
        candidate.samples[
            reference_start + lag_samples : reference_stop + lag_samples
        ],
        dtype=np.float64,
    )
    return target, test


def spectral_band_deltas(
    reference: np.ndarray,
    candidate: np.ndarray,
    sample_rate: int,
) -> dict[str, float | None]:
    fft_size = 2048
    if len(reference) < fft_size or len(candidate) < fft_size:
        return {name: None for name, _, _ in BANDS}
    hop = max(fft_size, round(sample_rate * 0.08))
    starts = np.arange(0, min(len(reference), len(candidate)) - fft_size + 1, hop)
    if len(starts) == 0:
        return {name: None for name, _, _ in BANDS}
    levels = np.array(
        [
            math.sqrt(float(np.mean(reference[start : start + fft_size] ** 2)) + EPS)
            for start in starts
        ]
    )
    threshold = max(10 ** (-48.0 / 20.0), float(np.percentile(levels, 55)))
    starts = starts[levels >= threshold]
    if len(starts) == 0:
        starts = np.array([int(np.argmax(levels)) * hop])
    if len(starts) > 24:
        starts = starts[np.linspace(0, len(starts) - 1, 24).astype(int)]
    window = np.hanning(fft_size)
    target_power = np.zeros(fft_size // 2 + 1)
    test_power = np.zeros(fft_size // 2 + 1)
    for start in starts:
        target_power += np.abs(
            np.fft.rfft(reference[start : start + fft_size] * window)
        ) ** 2
        test_power += np.abs(
            np.fft.rfft(candidate[start : start + fft_size] * window)
        ) ** 2
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    result: dict[str, float | None] = {}
    for name, low, high in BANDS:
        mask = (frequencies >= low) & (
            frequencies < min(high, sample_rate / 2.0)
        )
        if not np.any(mask):
            result[name] = None
            continue
        target_energy = float(np.sum(target_power[mask])) + EPS
        test_energy = float(np.sum(test_power[mask])) + EPS
        result[name] = 10.0 * math.log10(test_energy / target_energy)
    return result


def clip_metrics(
    reference: Audio,
    candidate: Audio,
    clip: Clip,
    lag_samples: int,
) -> tuple[dict[str, object], tuple[float, float, float, int]]:
    target, test = aligned_views(
        reference, candidate, clip.start, clip.stop, lag_samples
    )
    count = min(len(target), len(test))
    if count < max(256, reference.sample_rate // 20):
        empty = {
            "order": clip.order,
            "category": clip.category,
            "file": clip.file,
            "lag_ms": lag_samples * 1000.0 / reference.sample_rate,
            "samples": count,
            "valid": False,
        }
        return empty, (0.0, 0.0, 0.0, 0)

    target = target[:count]
    test = test[:count]
    xx = float(np.dot(test, test))
    yy = float(np.dot(target, target))
    xy = float(np.dot(test, target))
    gain = xy / max(xx, EPS)
    residual_sq = max(yy - 2.0 * gain * xy + gain * gain * xx, 0.0)
    target_rms = math.sqrt(yy / count)
    test_rms = math.sqrt(xx / count)
    error_ratio = math.sqrt(residual_sq / max(yy, EPS))
    cosine = xy / math.sqrt(max(xx * yy, EPS))

    frame = max(1, round(reference.sample_rate * 0.020))
    target_env = log_rms_envelope(target, frame)
    test_env = log_rms_envelope(test, frame)
    frame_count = min(len(target_env), len(test_env))
    target_env = target_env[:frame_count]
    test_env = test_env[:frame_count]
    envelope_correlation = correlation(target_env, test_env)
    if frame_count >= 8:
        active = target_env >= np.percentile(target_env, 55)
        quiet = target_env <= np.percentile(target_env, 25)
        target_contrast = float(np.median(target_env[active]) - np.median(target_env[quiet]))
        test_contrast = float(np.median(test_env[active]) - np.median(test_env[quiet]))
        contrast_error = test_contrast - target_contrast
    else:
        target_contrast = test_contrast = contrast_error = float("nan")

    bands = spectral_band_deltas(target, test, reference.sample_rate)
    valid_bands = [value for value in bands.values() if value is not None]
    if valid_bands:
        broadband_offset = float(np.median(valid_bands))
        spectral_rmse = math.sqrt(
            float(np.mean([(value - broadband_offset) ** 2 for value in valid_bands]))
        )
    else:
        broadband_offset = spectral_rmse = float("nan")

    metrics: dict[str, object] = {
        "order": clip.order,
        "category": clip.category,
        "file": clip.file,
        "voice_relevant": clip.voice_relevant,
        "lag_ms": lag_samples * 1000.0 / reference.sample_rate,
        "samples": count,
        "valid": True,
        "target_rms_dbfs": amp_db(target_rms),
        "candidate_rms_dbfs": amp_db(test_rms),
        "unmatched_level_delta_db": amp_db(test_rms / max(target_rms, EPS)),
        "fitted_gain": gain,
        "fitted_gain_db": amp_db(gain),
        "cosine_correlation": cosine,
        "level_matched_error_ratio": error_ratio,
        "level_matched_error_relative_db": amp_db(error_ratio),
        "envelope_correlation": envelope_correlation,
        "target_active_quiet_contrast_db": target_contrast,
        "candidate_active_quiet_contrast_db": test_contrast,
        "active_quiet_contrast_error_db": contrast_error,
        "spectral_broadband_offset_db": broadband_offset,
        "spectral_shape_rmse_db": spectral_rmse,
        "band_delta_db": bands,
    }
    return metrics, (xx, yy, xy, count)


def compare_candidate(
    reference_name: str,
    reference: Audio,
    candidate: Audio,
    clips: Sequence[Clip],
    max_lag_ms: float,
    refine_lag_ms: float,
) -> dict[str, object]:
    hop = max(1, round(reference.sample_rate * 0.010))
    target_envelope = log_rms_envelope(reference.samples, hop)
    candidate_envelope = log_rms_envelope(candidate.samples, hop)
    voice_clips = [clip for clip in clips if clip.voice_relevant]
    radius = max(1, round(max_lag_ms * reference.sample_rate / 1000.0 / hop))
    global_lag_hops, global_envelope_corr = estimate_lag_hops(
        target_envelope,
        candidate_envelope,
        voice_clips,
        hop,
        center=0,
        radius=radius,
    )
    refine_radius = max(
        0, round(refine_lag_ms * reference.sample_rate / 1000.0 / hop)
    )

    rows: list[dict[str, object]] = []
    global_xx = global_yy = global_xy = 0.0
    global_count = 0
    for clip in clips:
        local_lag_hops, local_envelope_corr = estimate_lag_hops(
            target_envelope,
            candidate_envelope,
            [clip],
            hop,
            center=global_lag_hops,
            radius=refine_radius,
        )
        metrics, sufficient = clip_metrics(
            reference,
            candidate,
            clip,
            local_lag_hops * hop,
        )
        metrics["alignment_envelope_correlation"] = local_envelope_corr
        rows.append(metrics)
        xx, yy, xy, count = sufficient
        global_xx += xx
        global_yy += yy
        global_xy += xy
        global_count += count

    global_gain = global_xy / max(global_xx, EPS)
    global_error_sq = max(
        global_yy
        - 2.0 * global_gain * global_xy
        + global_gain * global_gain * global_xx,
        0.0,
    )
    global_error_ratio = math.sqrt(global_error_sq / max(global_yy, EPS))
    global_cosine = global_xy / math.sqrt(max(global_xx * global_yy, EPS))
    global_level_delta = amp_db(
        math.sqrt(global_xx / max(global_yy, EPS))
    )
    selected = [row for row in rows if row.get("valid") and row.get("voice_relevant")]
    median_envelope = median(
        (row.get("envelope_correlation") for row in selected), default=0.0
    )
    median_spectral = median(
        (row.get("spectral_shape_rmse_db") for row in selected), default=30.0
    )
    median_dynamics = median(
        (
            abs(float(row["active_quiet_contrast_error_db"]))
            for row in selected
            if finite_float(row.get("active_quiet_contrast_error_db")) is not None
        ),
        default=30.0,
    )
    gain_std = stddev((row.get("fitted_gain_db") for row in selected), default=0.0)
    median_abs_level = median(
        (
            abs(float(row["unmatched_level_delta_db"]))
            for row in selected
            if finite_float(row.get("unmatched_level_delta_db")) is not None
        ),
        default=30.0,
    )
    shape_cost = (
        50.0 * global_error_ratio
        + 10.0 * (1.0 - max(min(global_cosine, 1.0), -1.0))
        + 15.0 * (1.0 - max(min(median_envelope, 1.0), -1.0))
        + 1.5 * median_spectral
        + median_dynamics
        + 0.5 * gain_std
    )
    delivery_cost = shape_cost + abs(global_level_delta)

    category_summary: dict[str, object] = {}
    for category in sorted({clip.category for clip in clips}):
        category_rows = [
            row for row in rows if row.get("valid") and row["category"] == category
        ]
        category_summary[category] = {
            "count": len(category_rows),
            "median_level_matched_error_relative_db": median(
                (row.get("level_matched_error_relative_db") for row in category_rows),
                default=float("nan"),
            ),
            "median_cosine_correlation": median(
                (row.get("cosine_correlation") for row in category_rows),
                default=float("nan"),
            ),
            "median_unmatched_level_delta_db": median(
                (row.get("unmatched_level_delta_db") for row in category_rows),
                default=float("nan"),
            ),
            "median_spectral_shape_rmse_db": median(
                (row.get("spectral_shape_rmse_db") for row in category_rows),
                default=float("nan"),
            ),
        }

    return {
        "reference": reference_name,
        "global_alignment_lag_ms": (
            global_lag_hops * hop * 1000.0 / reference.sample_rate
        ),
        "global_alignment_envelope_correlation": global_envelope_corr,
        "global_samples_compared": global_count,
        "global_fitted_gain": global_gain,
        "global_fitted_gain_db": amp_db(global_gain),
        "global_unmatched_level_delta_db": global_level_delta,
        "global_cosine_correlation": global_cosine,
        "global_level_matched_error_ratio": global_error_ratio,
        "global_level_matched_error_relative_db": amp_db(global_error_ratio),
        "voice_median_envelope_correlation": median_envelope,
        "voice_median_spectral_shape_rmse_db": median_spectral,
        "voice_median_abs_dynamics_contrast_error_db": median_dynamics,
        "voice_std_per_clip_fitted_gain_db": gain_std,
        "voice_median_abs_unmatched_level_delta_db": median_abs_level,
        "shape_cost": shape_cost,
        "delivery_cost": delivery_cost,
        "category_summary": category_summary,
        "per_clip": rows,
    }


def accumulate_normal_equations(
    stems: dict[str, Audio | None],
    target: Audio,
    lag_samples: int,
    clips: Sequence[Clip],
    chunk_frames: int = 300_000,
) -> tuple[np.ndarray, np.ndarray]:
    xtx = np.zeros((4, 4), dtype=np.float64)
    xty = np.zeros(4, dtype=np.float64)
    for clip in clips:
        start = max(clip.start, 0, -lag_samples)
        stop = min(
            clip.stop,
            target.frames,
            max(
                (
                    stem.frames - lag_samples
                    for stem in stems.values()
                    if stem is not None
                ),
                default=0,
            ),
        )
        for position in range(start, stop, chunk_frames):
            end = min(position + chunk_frames, stop)
            columns: list[np.ndarray] = []
            for stem_name in STEM_NAMES:
                stem = stems[stem_name]
                if stem is None:
                    columns.append(np.zeros(end - position, dtype=np.float64))
                    continue
                candidate_start = position + lag_samples
                candidate_stop = end + lag_samples
                if candidate_start < 0 or candidate_stop > stem.frames:
                    columns.append(np.zeros(end - position, dtype=np.float64))
                    continue
                columns.append(
                    np.asarray(
                        stem.samples[candidate_start:candidate_stop],
                        dtype=np.float64,
                    )
                )
            matrix = np.column_stack(columns)
            target_part = np.asarray(target.samples[position:end], dtype=np.float64)
            xtx += matrix.T @ matrix
            xty += matrix.T @ target_part
    return xtx, xty


def bounded_coordinate_descent(
    xtx: np.ndarray,
    xty: np.ndarray,
    lower: Sequence[float] = (0.0, 0.0, 0.0, 0.0),
    upper: Sequence[float] = (2.0, 1.5, 1.5, 1.5),
) -> tuple[float, float, float, float]:
    lower_array = np.asarray(lower, dtype=np.float64)
    upper_array = np.asarray(upper, dtype=np.float64)
    coefficients = np.clip(
        np.divide(
            xty,
            np.maximum(np.diag(xtx), EPS),
            out=np.zeros_like(xty),
        ),
        lower_array,
        upper_array,
    )
    for _ in range(300):
        previous = coefficients.copy()
        for index in range(4):
            diagonal = xtx[index, index]
            if diagonal <= EPS:
                coefficients[index] = 0.0
                continue
            contribution = float(np.dot(xtx[index], coefficients))
            unconstrained = (
                xty[index] - contribution + diagonal * coefficients[index]
            ) / diagonal
            coefficients[index] = np.clip(
                unconstrained, lower_array[index], upper_array[index]
            )
        if float(np.max(np.abs(coefficients - previous))) < 1e-9:
            break
    return tuple(float(value) for value in coefficients)  # type: ignore[return-value]


def fit_variants(
    stems: dict[str, Audio | None],
    references: dict[str, Audio],
    clips: Sequence[Clip],
    max_lag_ms: float,
) -> tuple[dict[str, tuple[float, float, float, float]], dict[str, object]]:
    speech = stems["speech"]
    if speech is None:
        return {}, {}
    hop = max(1, round(speech.sample_rate * 0.010))
    speech_envelope = log_rms_envelope(speech.samples, hop)
    voice_clips = [clip for clip in clips if clip.voice_relevant]
    radius = max(1, round(max_lag_ms * speech.sample_rate / 1000.0 / hop))
    equations: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    diagnostics: dict[str, object] = {}
    for reference_name, reference in references.items():
        target_envelope = log_rms_envelope(reference.samples, hop)
        lag_hops, envelope_corr = estimate_lag_hops(
            target_envelope,
            speech_envelope,
            voice_clips,
            hop,
            center=0,
            radius=radius,
        )
        equations[reference_name] = accumulate_normal_equations(
            stems,
            reference,
            lag_hops * hop,
            voice_clips,
        )
        diagnostics[reference_name] = {
            "fit_scope": "voice_relevant_clips",
            "alignment_lag_ms": lag_hops * hop * 1000.0 / speech.sample_rate,
            "alignment_envelope_correlation": envelope_corr,
        }

    variants: dict[str, tuple[float, float, float, float]] = {}
    if "enhance75" in equations:
        variants["fit_enhance75_voice"] = bounded_coordinate_descent(
            *equations["enhance75"]
        )
    if "full" in equations:
        variants["fit_full_voice"] = bounded_coordinate_descent(*equations["full"])
    if len(equations) >= 2:
        total_xtx = sum((value[0] for value in equations.values()), np.zeros((4, 4)))
        total_xty = sum((value[1] for value in equations.values()), np.zeros(4))
        variants["fit_joint_voice"] = bounded_coordinate_descent(
            total_xtx, total_xty
        )
    for name, coefficients in variants.items():
        diagnostics[name] = {
            stem_name: coefficient
            for stem_name, coefficient in zip(STEM_NAMES, coefficients)
        }
    return variants, diagnostics


def write_pcm16_mono(
    path: Path,
    audio: Audio,
    chunk_frames: int = 750_000,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(audio.sample_rate)
        for start in range(0, audio.frames, chunk_frames):
            stop = min(start + chunk_frames, audio.frames)
            samples = np.asarray(audio.samples[start:stop], dtype=np.float64)
            pcm = np.clip(np.round(samples * 32767.0), -32768, 32767).astype("<i2")
            output.writeframes(pcm.tobytes())


def assemble_soundlift_directory(
    directory: Path,
    clip_ids: Sequence[str],
    clips: Sequence[Clip],
    target_rate: int,
    target_frames: int,
    temp_dir: Path,
    decoder: Path,
) -> tuple[
    dict[str, Audio | None],
    list[Clip],
    dict[str, object],
]:
    """Place independently processed UXP stems on their original timeline."""
    if not directory.is_dir():
        raise FileNotFoundError(f"Sound Lift output directory not found: {directory}")
    by_id = {clip.file.split("_", 1)[0].upper(): clip for clip in clips}
    requested = [clip_id.strip().upper() for clip_id in clip_ids if clip_id.strip()]
    if not requested:
        raise ValueError("No clip IDs were supplied for --soundlift-dir")
    unknown = [clip_id for clip_id in requested if clip_id not in by_id]
    if unknown:
        raise ValueError(f"Unknown clip ID(s): {', '.join(unknown)}")
    selected = [by_id[clip_id] for clip_id in requested]

    assembled: dict[str, Audio | None] = {}
    writable: dict[str, np.memmap] = {}
    for stem_name in STEM_NAMES:
        path = temp_dir / f"assembled_{stem_name}.f32"
        samples = np.memmap(path, dtype="<f4", mode="w+", shape=(target_frames,))
        samples[:] = 0.0
        writable[stem_name] = samples
        assembled[stem_name] = Audio(
            f"assembled_{stem_name}",
            directory,
            target_rate,
            samples,
        )

    manifest: dict[str, object] = {
        "directory": directory,
        "clip_ids": requested,
        "clips": {},
    }
    found_by_stem = {stem_name: 0 for stem_name in STEM_NAMES}
    canonical_file_names = {
        "speech": "Speech",
        "background": "Background",
        "noise": "Noise",
        "reverb": "Reverb",
    }
    for clip_id, clip in zip(requested, selected):
        clip_manifest: dict[str, object] = {
            "timeline_start_frame": clip.start,
            "timeline_stop_frame": clip.stop,
            "nominal_duration_seconds": (clip.stop - clip.start) / target_rate,
            "stems": {},
        }
        for stem_name, canonical_name in canonical_file_names.items():
            source = directory / f"{clip_id}_{canonical_name}.wav"
            if not source.is_file():
                clip_manifest["stems"][stem_name] = {
                    "path": source,
                    "status": "missing",
                }
                continue
            decoded = decode_at_rate(
                f"batch_{clip_id}_{stem_name}",
                source,
                target_rate,
                temp_dir,
                decoder,
            )
            maximum = min(
                decoded.frames,
                clip.stop - clip.start,
                target_frames - clip.start,
            )
            if maximum > 0:
                writable[stem_name][clip.start : clip.start + maximum] = (
                    decoded.samples[:maximum]
                )
            found_by_stem[stem_name] += 1
            clip_manifest["stems"][stem_name] = {
                "path": source,
                "status": "loaded",
                "decoded_duration_seconds": decoded.duration,
                "copied_duration_seconds": maximum / target_rate,
                "duration_delta_ms": (
                    decoded.frames - (clip.stop - clip.start)
                )
                * 1000.0
                / target_rate,
            }
        manifest["clips"][clip_id] = clip_manifest

    for stem_name, samples in writable.items():
        samples.flush()
        if found_by_stem[stem_name] == 0:
            assembled[stem_name] = None
    incomplete = {
        stem_name: len(selected) - count
        for stem_name, count in found_by_stem.items()
        if count != len(selected)
    }
    if incomplete:
        description = ", ".join(
            f"{stem_name}: {missing_count} ausente(s)"
            for stem_name, missing_count in incomplete.items()
        )
        raise FileNotFoundError(
            f"Incomplete UXP stem batch in {directory} ({description})"
        )
    manifest["loaded_clip_count_by_stem"] = found_by_stem
    return assembled, selected, manifest


def build_markdown_summary(
    rankings: dict[str, list[str]],
    results: dict[str, dict[str, object]],
    raw_baseline: dict[str, dict[str, object]],
    variants: dict[str, tuple[float, float, float, float]],
    inputs: dict[str, object],
) -> str:
    lines = [
        "# Resultado Sound Lift × CapCut",
        "",
        "Relatório gerado automaticamente pelo comparador. Custos menores são melhores.",
        "",
        "## Referências",
        "",
        f"- cru: `{inputs['raw']}`",
        f"- CapCut aprimoramento 75%: `{inputs['references']['enhance75']}`",
        f"- CapCut cadeia completa: `{inputs['references']['full']}`",
        "",
    ]
    for target_name, title in (
        ("enhance75", "CapCut — aprimoramento 75%"),
        ("full", "CapCut — cadeia completa"),
    ):
        raw = raw_baseline[target_name]
        lines.extend(
            [
                f"## {title}",
                "",
                (
                    "Baseline cru: "
                    f"delivery `{raw['delivery_cost']:.3f}`, "
                    f"erro ajustado `{raw['global_level_matched_error_relative_db']:.2f} dB`, "
                    f"correlação `{raw['global_cosine_correlation']:.4f}`."
                ),
                "",
                "| rank | variante | S | B | N | R | delivery | shape | erro ajustado | espectro |",
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rank, name in enumerate(rankings[target_name][:8], 1):
            target = results[name]["targets"][target_name]
            coefficients = variants[name]
            lines.append(
                f"| {rank} | `{name}` | "
                f"{coefficients[0]:.4f} | {coefficients[1]:.4f} | "
                f"{coefficients[2]:.4f} | {coefficients[3]:.4f} | "
                f"{target['delivery_cost']:.3f} | {target['shape_cost']:.3f} | "
                f"{target['global_level_matched_error_relative_db']:.2f} dB | "
                f"{target['voice_median_spectral_shape_rmse_db']:.2f} dB |"
            )
        lines.append("")

    lines.extend(
        [
            "## Compromisso entre as duas metas",
            "",
            "| rank | variante | custo normalizado médio |",
            "|---:|---|---:|",
        ]
    )
    for rank, name in enumerate(rankings["combined"][:8], 1):
        lines.append(
            f"| {rank} | `{name}` | "
            f"{results[name]['combined_normalized_delivery_cost']:.4f} |"
        )
    lines.extend(
        [
            "",
            "## Como interpretar",
            "",
            (
                "As variantes `fit_*` são o melhor limite linear encontrado com pesos "
                "positivos nos quatro stems. Se mesmo elas permanecerem distantes, mexer "
                "apenas nos quatro volumes não resolve a diferença; faltam estágios não "
                "lineares, como denoise, EQ/dinâmica ou reconstrução neural."
            ),
            "",
            (
                "Este relatório mede mono. A decisão final deve combinar estas métricas "
                "com escuta cega nivelada nas amostras de voz feminina."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def build_parser(root: Path) -> argparse.ArgumentParser:
    isolated = root / "capcut_isolated_exports_20260728"
    parser = argparse.ArgumentParser(
        description=(
            "Mix Audition Sound Lift stems and rank them against CapCut's "
            "Enhance 75% and full-chain references."
        )
    )
    parser.add_argument("--speech", type=Path, help="Sound Lift Speech stem")
    parser.add_argument("--background", type=Path, help="Sound Lift Background stem")
    parser.add_argument("--noise", type=Path, help="Sound Lift Noise stem")
    parser.add_argument("--reverb", type=Path, help="Sound Lift Reverb stem")
    parser.add_argument(
        "--soundlift-dir",
        type=Path,
        help=(
            "Directory produced by the UXP panel (for example S01_Speech.wav). "
            "The selected clips are assembled on the original timeline."
        ),
    )
    parser.add_argument(
        "--clip-ids",
        default="S01,S03,S06,M04,F02",
        help="Comma-separated UXP clip IDs used with --soundlift-dir",
    )
    parser.add_argument(
        "--raw",
        type=Path,
        default=isolated / "CAPCUT_RAW_CONTROL_ALL_OFF.WAV",
        help="Raw 30-clip control (used for provenance/baseline)",
    )
    parser.add_argument(
        "--capcut-enhance75",
        type=Path,
        default=isolated / "CAPCUT_ENHANCE_ONLY_75.WAV",
    )
    parser.add_argument(
        "--capcut-full",
        type=Path,
        default=isolated / "CAPCUT_FULL_CHAIN_75_RECHECK.WAV",
    )
    parser.add_argument(
        "--timecodes",
        type=Path,
        default=root / "capcut_project_audio_female_30_clips/MAPA_TIMECODES.csv",
    )
    parser.add_argument(
        "--decoder",
        type=Path,
        default=root / "capcut_reverse_engineering_audio/build/decode_audio_f32",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="NAME=S,B,N,R",
        help="Add/replace a stem mix; may be repeated",
    )
    parser.add_argument(
        "--no-default-variants",
        action="store_true",
        help="Only analyze --variant and automatically fitted mixes",
    )
    parser.add_argument(
        "--no-fit",
        action="store_true",
        help="Disable target-fitted positive stem-weight variants",
    )
    parser.add_argument("--max-lag-ms", type=float, default=2000.0)
    parser.add_argument("--refine-lag-ms", type=float, default=250.0)
    parser.add_argument(
        "--write-top",
        type=int,
        default=3,
        help="Write up to N unique top-ranked candidate WAVs (0 disables)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate dependencies/default references without requiring stems",
    )
    return parser


def check_environment(args: argparse.Namespace) -> int:
    checks = {
        "numpy_version": np.__version__,
        "decoder": {
            "path": str(args.decoder),
            "exists": args.decoder.is_file(),
            "executable": os.access(args.decoder, os.X_OK),
        },
        "raw": {"path": str(args.raw), "exists": args.raw.is_file()},
        "capcut_enhance75": {
            "path": str(args.capcut_enhance75),
            "exists": args.capcut_enhance75.is_file(),
        },
        "capcut_full": {
            "path": str(args.capcut_full),
            "exists": args.capcut_full.is_file(),
        },
        "timecodes": {
            "path": str(args.timecodes),
            "exists": args.timecodes.is_file(),
        },
        "default_soundlift_output": {
            "path": str(
                Path(__file__).resolve().parent / "soundlift_uxp_output"
            ),
            "exists": (
                Path(__file__).resolve().parent / "soundlift_uxp_output"
            ).is_dir(),
            "optional": True,
        },
    }
    print(json.dumps(checks, indent=2, ensure_ascii=False))
    all_ok = all(
        item.get("exists", True) and item.get("executable", True)
        for item in checks.values()
        if isinstance(item, dict)
        and not item.get("optional", False)
    )
    return 0 if all_ok else 1


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent
    parser = build_parser(root)
    args = parser.parse_args()
    if args.check:
        return check_environment(args)
    if args.speech is None and args.soundlift_dir is None:
        parser.error("--speech or --soundlift-dir is required unless --check is used")
    if args.speech is not None and args.soundlift_dir is not None:
        parser.error("--speech and --soundlift-dir are mutually exclusive")
    if not args.decoder.is_file() or not os.access(args.decoder, os.X_OK):
        parser.error(f"decoder is missing or not executable: {args.decoder}")

    variants: dict[str, tuple[float, float, float, float]] = {}
    if not args.no_default_variants:
        variants.update(DEFAULT_VARIANTS)
    for specification in args.variant:
        name, values = parse_variant(specification)
        variants[name] = values

    supplied_paths = {
        "speech": args.speech,
        "background": args.background,
        "noise": args.noise,
        "reverb": args.reverb,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sound_lift_compare_") as temporary:
        temp_dir = Path(temporary)
        enhance_native = decode_native(
            "capcut_enhance75",
            args.capcut_enhance75,
            temp_dir,
            args.decoder,
        )
        target_rate = enhance_native.sample_rate
        references = {
            "enhance75": resample_audio(enhance_native, target_rate, temp_dir),
            "full": decode_at_rate(
                "capcut_full", args.capcut_full, target_rate, temp_dir, args.decoder
            ),
        }
        raw = decode_at_rate("raw", args.raw, target_rate, temp_dir, args.decoder)
        all_clips = load_timecodes(args.timecodes, target_rate)
        stem_manifest: object
        if args.soundlift_dir is not None:
            stems, clips, stem_manifest = assemble_soundlift_directory(
                args.soundlift_dir,
                args.clip_ids.split(","),
                all_clips,
                target_rate,
                max(reference.frames for reference in references.values()),
                temp_dir,
                args.decoder,
            )
        else:
            stems = {}
            for stem_name, path in supplied_paths.items():
                stems[stem_name] = (
                    decode_at_rate(
                        f"stem_{stem_name}",
                        path,
                        target_rate,
                        temp_dir,
                        args.decoder,
                    )
                    if path is not None
                    else None
                )
            clips = all_clips
            stem_manifest = supplied_paths

        for name, coefficients in list(variants.items()):
            unavailable = [
                stem_name
                for coefficient, stem_name in zip(coefficients, STEM_NAMES)
                if abs(coefficient) > EPS and stems[stem_name] is None
            ]
            if unavailable:
                print(
                    f"Skipping {name}: missing stem(s) {', '.join(unavailable)}",
                    file=sys.stderr,
                )
                del variants[name]
        if not variants and args.no_fit:
            parser.error("No usable variants remain")

        duration_diagnostics = {
            "raw": raw.duration,
            **{
                name: stem.duration if stem is not None else None
                for name, stem in stems.items()
            },
            **{
                f"capcut_{name}": reference.duration
                for name, reference in references.items()
            },
        }

        fit_diagnostics: dict[str, object] = {}
        if not args.no_fit:
            fitted, fit_diagnostics = fit_variants(
                stems, references, clips, args.max_lag_ms
            )
            variants.update(fitted)
        if not variants:
            parser.error("No usable variants remain after stem availability checks")

        results: dict[str, dict[str, object]] = {}
        raw_baseline = {
            reference_name: compare_candidate(
                reference_name,
                reference,
                raw,
                clips,
                args.max_lag_ms,
                args.refine_lag_ms,
            )
            for reference_name, reference in references.items()
        }
        candidate_path = temp_dir / "candidate.f32"
        for index, (variant_name, coefficients) in enumerate(variants.items(), 1):
            if candidate_path.exists():
                candidate_path.unlink()
            candidate = mix_to_memmap(
                variant_name, coefficients, stems, candidate_path
            )
            target_results = {
                reference_name: compare_candidate(
                    reference_name,
                    reference,
                    candidate,
                    clips,
                    args.max_lag_ms,
                    args.refine_lag_ms,
                )
                for reference_name, reference in references.items()
            }
            results[variant_name] = {
                "coefficients": {
                    stem_name: float(coefficient)
                    for stem_name, coefficient in zip(STEM_NAMES, coefficients)
                },
                "targets": target_results,
            }
            print(
                f"[{index}/{len(variants)}] {variant_name}: "
                + ", ".join(
                    f"{name} delivery={target_result['delivery_cost']:.3f}"
                    for name, target_result in target_results.items()
                ),
                file=sys.stderr,
            )
            del candidate

        rankings: dict[str, list[str]] = {}
        for reference_name in references:
            rankings[reference_name] = sorted(
                variants,
                key=lambda name: float(
                    results[name]["targets"][reference_name]["delivery_cost"]
                ),
            )
            for rank, name in enumerate(rankings[reference_name], 1):
                results[name]["targets"][reference_name]["delivery_rank"] = rank
        best_cost = {
            reference_name: min(
                float(results[name]["targets"][reference_name]["delivery_cost"])
                for name in variants
            )
            for reference_name in references
        }
        for name in variants:
            normalized = [
                float(results[name]["targets"][reference_name]["delivery_cost"])
                / max(best_cost[reference_name], EPS)
                for reference_name in references
            ]
            results[name]["combined_normalized_delivery_cost"] = float(
                np.mean(normalized)
            )
        rankings["combined"] = sorted(
            variants,
            key=lambda name: float(results[name]["combined_normalized_delivery_cost"]),
        )
        for rank, name in enumerate(rankings["combined"], 1):
            results[name]["combined_rank"] = rank

        output = {
            "schema_version": 1,
            "purpose": (
                "Audition Sound Lift stem-mix comparison against CapCut isolated "
                "Enhance 75% and full-chain exports"
            ),
            "audio_domain": (
                "mono float32 analysis after AVFoundation decode; candidate WAV "
                "exports are mono PCM16"
            ),
            "sample_rate": target_rate,
            "inputs": {
                "raw": args.raw,
                "stems": stem_manifest,
                "analysis_clip_ids": [
                    clip.file.split("_", 1)[0] for clip in clips
                ],
                "references": {
                    "enhance75": args.capcut_enhance75,
                    "full": args.capcut_full,
                },
                "timecodes": args.timecodes,
                "decoder": args.decoder,
            },
            "duration_seconds": duration_diagnostics,
            "alignment": {
                "global_max_lag_ms": args.max_lag_ms,
                "per_clip_refinement_ms": args.refine_lag_ms,
                "method": "10 ms log-RMS envelope correlation on voice-relevant clips",
            },
            "score_formula": SCORE_FORMULA,
            "fit_diagnostics": fit_diagnostics,
            "raw_baseline": raw_baseline,
            "rankings": rankings,
            "variants": results,
            "limitations": [
                (
                    "AVFoundation decoder downmixes to mono, so spatial/stereo "
                    "differences require a separate channel-aware audit."
                ),
                (
                    "Linear stem weights cannot reproduce nonlinear denoise, EQ, "
                    "limiting, or content-dependent neural reconstruction."
                ),
                (
                    "Automatically fitted weights are an attainable linear upper "
                    "bound on this test pack, not a general-purpose preset."
                ),
                (
                    "Final acceptance should include level-matched blind listening "
                    "on the target female-voice Reels material."
                ),
            ],
        }

        json_path = args.output_dir / "COMPARISON.json"
        json_path.write_text(
            json.dumps(sanitize_json(output), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary_path = args.output_dir / "SUMMARY.md"
        summary_path.write_text(
            build_markdown_summary(
                rankings,
                results,
                raw_baseline,
                variants,
                output["inputs"],
            ),
            encoding="utf-8",
        )

        ranking_path = args.output_dir / "VARIANT_RANKING.csv"
        with ranking_path.open("w", encoding="utf-8", newline="") as stream:
            fieldnames = [
                "variant",
                *STEM_NAMES,
                "combined_rank",
                "combined_normalized_delivery_cost",
                "target",
                "delivery_rank",
                "delivery_cost",
                "shape_cost",
                "global_lag_ms",
                "global_level_delta_db",
                "global_fitted_gain_db",
                "global_cosine_correlation",
                "global_level_matched_error_relative_db",
                "voice_median_envelope_correlation",
                "voice_median_spectral_shape_rmse_db",
                "voice_median_abs_dynamics_contrast_error_db",
            ]
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            for name in rankings["combined"]:
                coefficients = results[name]["coefficients"]
                for reference_name in references:
                    target = results[name]["targets"][reference_name]
                    writer.writerow(
                        {
                            "variant": name,
                            **coefficients,
                            "combined_rank": results[name]["combined_rank"],
                            "combined_normalized_delivery_cost": results[name][
                                "combined_normalized_delivery_cost"
                            ],
                            "target": reference_name,
                            "delivery_rank": target["delivery_rank"],
                            "delivery_cost": target["delivery_cost"],
                            "shape_cost": target["shape_cost"],
                            "global_lag_ms": target["global_alignment_lag_ms"],
                            "global_level_delta_db": target[
                                "global_unmatched_level_delta_db"
                            ],
                            "global_fitted_gain_db": target["global_fitted_gain_db"],
                            "global_cosine_correlation": target[
                                "global_cosine_correlation"
                            ],
                            "global_level_matched_error_relative_db": target[
                                "global_level_matched_error_relative_db"
                            ],
                            "voice_median_envelope_correlation": target[
                                "voice_median_envelope_correlation"
                            ],
                            "voice_median_spectral_shape_rmse_db": target[
                                "voice_median_spectral_shape_rmse_db"
                            ],
                            "voice_median_abs_dynamics_contrast_error_db": target[
                                "voice_median_abs_dynamics_contrast_error_db"
                            ],
                        }
                    )

        clip_path = args.output_dir / "PER_CLIP_METRICS.csv"
        with clip_path.open("w", encoding="utf-8", newline="") as stream:
            fieldnames = [
                "variant",
                "target",
                "order",
                "category",
                "file",
                "voice_relevant",
                "lag_ms",
                "alignment_envelope_correlation",
                "target_rms_dbfs",
                "candidate_rms_dbfs",
                "unmatched_level_delta_db",
                "fitted_gain_db",
                "cosine_correlation",
                "level_matched_error_relative_db",
                "envelope_correlation",
                "active_quiet_contrast_error_db",
                "spectral_shape_rmse_db",
            ]
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            for name in variants:
                for reference_name in references:
                    for row in results[name]["targets"][reference_name]["per_clip"]:
                        writer.writerow(
                            {
                                "variant": name,
                                "target": reference_name,
                                **{key: row.get(key) for key in fieldnames[2:]},
                            }
                        )

        unique_top: list[str] = []
        export_limit = max(0, args.write_top)
        if export_limit:
            for ranking_name in ("combined", "enhance75", "full"):
                for name in rankings[ranking_name]:
                    if name not in unique_top:
                        unique_top.append(name)
                    if len(unique_top) >= export_limit:
                        break
                if len(unique_top) >= export_limit:
                    break
        exported: list[Path] = []
        for rank, name in enumerate(unique_top, 1):
            if candidate_path.exists():
                candidate_path.unlink()
            candidate = mix_to_memmap(
                name, variants[name], stems, candidate_path
            )
            wav_path = args.output_dir / f"TOP_{rank:02d}_{name}.wav"
            write_pcm16_mono(wav_path, candidate)
            exported.append(wav_path)
            del candidate

    print(
        json.dumps(
            {
                "comparison": str(json_path),
                "summary": str(summary_path),
                "ranking": str(ranking_path),
                "per_clip": str(clip_path),
                "top_candidates": [str(path) for path in exported],
                "best": {
                    ranking_name: rankings[ranking_name][0]
                    for ranking_name in rankings
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
