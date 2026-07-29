#!/usr/bin/env python3
"""Offline calibration of a DeepFilterNet vocal estimate against CapCut.

The script intentionally works on workspace copies only.  It:

1. reads PCM WAV (16/24/32-bit integer);
2. aligns the shorter DeepFilterNet output to its raw input;
3. resamples both signals from 48 kHz to the exact 44.1 kHz target length;
4. tries ``(1-wet)*raw + wet*vocal`` around the nominal 75% mix;
5. runs the locally reconstructed CapCut denoise and Voice Focus stages;
6. compares every candidate with the corresponding CapCut full-chain export;
7. writes metrics plus listening candidates as 24-bit stereo WAV.

The installed CapCut denoise runtime is used only as a local calibration oracle.
It must be replaced by an independently licensed implementation before
distribution.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


EPS = 1.0e-12
DEFAULT_WETS = (0.0, 0.25, 0.4, 0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0)
TARGET_PREFIX = {
    "S01_female_clean_ptbr.wav": "06_",
    "S03_female_level_steps.wav": "08_",
    "S06_female_alt_room_reverb.wav": "11_",
    "M04_female_alt_fan_snr_0.wav": "21_",
    "F02_female_music_snr_m5.wav": "27_",
}


@dataclass(frozen=True)
class PcmWav:
    sample_rate: int
    channels: int
    sample_width: int
    samples: np.ndarray

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])


@dataclass(frozen=True)
class PreparedClip:
    name: str
    raw: np.ndarray
    vocal: np.ndarray
    target: np.ndarray
    sample_rate: int
    dfn_lag_48k: int
    dfn_alignment_corr: float
    source_rate: int
    source_frames: int
    vocal_frames: int


def parse_number_list(value: str) -> tuple[float, ...]:
    numbers = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    if not numbers:
        raise argparse.ArgumentTypeError("the list cannot be empty")
    return numbers


def read_pcm_wav(path: Path) -> PcmWav:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.getnframes()
        compression = source.getcomptype()
        payload = source.readframes(frames)
    if compression != "NONE":
        raise ValueError(f"{path}: compressed WAV is not supported")
    if channels < 1:
        raise ValueError(f"{path}: invalid channel count")

    if sample_width == 1:
        integer = np.frombuffer(payload, dtype=np.uint8).astype(np.float32)
        flat = (integer - 128.0) / 128.0
    elif sample_width == 2:
        flat = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        octets = np.frombuffer(payload, dtype=np.uint8)
        if octets.size % 3:
            raise ValueError(f"{path}: malformed PCM24 payload")
        triples = octets.reshape(-1, 3).astype(np.int32)
        integer = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        integer = np.where(integer & 0x800000, integer - 0x1000000, integer)
        flat = integer.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        flat = np.frombuffer(payload, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path}: unsupported PCM width {8 * sample_width} bit")

    expected = frames * channels
    if flat.size != expected:
        raise ValueError(f"{path}: expected {expected} samples, read {flat.size}")
    samples = flat.reshape(frames, channels)
    return PcmWav(sample_rate, channels, sample_width, samples)


def write_pcm24_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    audio = np.asarray(samples, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, None]
    if audio.ndim != 2 or audio.shape[1] < 1:
        raise ValueError("audio must have shape (frames, channels)")
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio, -1.0, np.nextafter(1.0, 0.0))
    integer = np.clip(
        np.rint(clipped * 8388608.0), -8388608, 8388607
    ).astype(np.int32)
    flat = integer.reshape(-1)
    payload = np.empty((flat.size, 3), dtype=np.uint8)
    payload[:, 0] = flat & 0xFF
    payload[:, 1] = (flat >> 8) & 0xFF
    payload[:, 2] = (flat >> 16) & 0xFF
    with wave.open(str(path), "wb") as target:
        target.setnchannels(audio.shape[1])
        target.setsampwidth(3)
        target.setframerate(sample_rate)
        target.writeframes(payload.tobytes())


def mono(samples: np.ndarray) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim == 1:
        return audio
    return np.mean(audio, axis=1, dtype=np.float32)


def db(value: float) -> float:
    return 20.0 * math.log10(max(abs(value), EPS))


def rms(audio: np.ndarray) -> float:
    data = np.asarray(audio, dtype=np.float64)
    return float(np.sqrt(np.mean(data * data))) if data.size else 0.0


def normalized_corr(left: np.ndarray, right: np.ndarray) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    count = min(a.size, b.size)
    if count == 0:
        return 0.0
    a = a[:count] - float(np.mean(a[:count]))
    b = b[:count] - float(np.mean(b[:count]))
    denominator = math.sqrt(float(np.dot(a, a) * np.dot(b, b)))
    return float(np.dot(a, b) / denominator) if denominator > EPS else 0.0


def preemphasis(audio: np.ndarray) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float64)
    if data.size < 2:
        return data.copy()
    result = np.empty_like(data)
    result[0] = data[0]
    result[1:] = data[1:] - 0.92 * data[:-1]
    result -= float(np.mean(result))
    return result


def best_placement_lag(
    reference: np.ndarray,
    moving: np.ndarray,
    max_lag: int,
) -> tuple[int, float]:
    """Return lag that places ``moving[0]`` at ``reference[lag]``.

    FFT cross-correlation keeps this practical for full-length clips.  The
    returned correlation is recomputed on the exact overlap.
    """

    x = preemphasis(reference)
    y = preemphasis(moving)
    if not x.size or not y.size:
        return 0, 0.0
    transform_size = 1 << (x.size + y.size - 2).bit_length()
    cross = np.fft.irfft(
        np.fft.rfft(x, transform_size)
        * np.conj(np.fft.rfft(y, transform_size)),
        transform_size,
    )
    lags = np.arange(-max_lag, max_lag + 1, dtype=np.int64)
    indices = np.where(lags >= 0, lags, transform_size + lags)
    candidate = cross[indices]
    lag = int(lags[int(np.argmax(candidate))])

    if lag >= 0:
        count = min(x.size - lag, y.size)
        a = x[lag : lag + count]
        b = y[:count]
    else:
        count = min(x.size, y.size + lag)
        a = x[:count]
        b = y[-lag : -lag + count]
    return lag, normalized_corr(a, b)


def place_on_reference(
    reference: np.ndarray,
    moving: np.ndarray,
    lag: int,
    edge_fallback: bool = True,
) -> np.ndarray:
    """Place a signal on the reference timeline and preserve uncovered edges."""

    base = np.asarray(reference, dtype=np.float32)
    source = np.asarray(moving, dtype=np.float32)
    result = base.copy() if edge_fallback else np.zeros_like(base)
    destination_start = max(lag, 0)
    source_start = max(-lag, 0)
    count = min(base.size - destination_start, source.size - source_start)
    if count > 0:
        result[destination_start : destination_start + count] = source[
            source_start : source_start + count
        ]
    return result


def sinc_resample(
    audio: np.ndarray,
    source_rate: int,
    target_rate: int,
    output_frames: int | None = None,
    half_width: int = 28,
    chunk_frames: int = 32768,
) -> np.ndarray:
    """Band-limited windowed-sinc resampler implemented with NumPy only."""

    source = np.asarray(audio, dtype=np.float64)
    if source.ndim != 1:
        raise ValueError("sinc_resample expects mono audio")
    if output_frames is None:
        output_frames = int(round(source.size * target_rate / source_rate))
    if output_frames < 0:
        raise ValueError("negative output length")
    if source.size == 0 or output_frames == 0:
        return np.zeros(output_frames, dtype=np.float32)
    if source_rate == target_rate and source.size == output_frames:
        return source.astype(np.float32, copy=True)

    ratio = source_rate / target_rate
    cutoff = min(1.0, target_rate / source_rate) * 0.97
    offsets = np.arange(-half_width + 1, half_width + 1, dtype=np.int64)
    result = np.empty(output_frames, dtype=np.float32)

    for first in range(0, output_frames, chunk_frames):
        stop = min(first + chunk_frames, output_frames)
        positions = np.arange(first, stop, dtype=np.float64) * ratio
        centers = np.floor(positions).astype(np.int64)
        indices = centers[:, None] + offsets[None, :]
        distance = indices.astype(np.float64) - positions[:, None]
        inside_window = np.abs(distance) < half_width
        window_position = np.clip(distance / half_width, -1.0, 1.0)
        window = np.i0(8.6 * np.sqrt(np.maximum(0.0, 1.0 - window_position**2)))
        window /= np.i0(8.6)
        weights = cutoff * np.sinc(cutoff * distance) * window * inside_window
        valid = (indices >= 0) & (indices < source.size)
        weights *= valid
        safe_indices = np.clip(indices, 0, source.size - 1)
        denominator = np.sum(weights, axis=1)
        denominator = np.where(np.abs(denominator) > EPS, denominator, 1.0)
        result[first:stop] = (
            np.sum(source[safe_indices] * weights, axis=1) / denominator
        ).astype(np.float32)
    return result


def aligned_pair(
    candidate: np.ndarray,
    target: np.ndarray,
    max_lag: int,
) -> tuple[np.ndarray, np.ndarray, int]:
    lag, _ = best_placement_lag(target, candidate, max_lag)
    if lag >= 0:
        count = min(target.size - lag, candidate.size)
        return candidate[:count], target[lag : lag + count], lag
    count = min(target.size, candidate.size + lag)
    return candidate[-lag : -lag + count], target[:count], lag


def frame_rms(audio: np.ndarray, frame_size: int) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float64)
    if not data.size:
        return np.empty(0, dtype=np.float64)
    frames = int(math.ceil(data.size / frame_size))
    padded = np.pad(data, (0, frames * frame_size - data.size))
    shaped = padded.reshape(frames, frame_size)
    return np.sqrt(np.mean(shaped * shaped, axis=1) + EPS)


def spectral_band_distance(
    candidate: np.ndarray,
    target: np.ndarray,
    sample_rate: int,
    fitted_gain: float,
) -> tuple[float, dict[str, float]]:
    count = min(candidate.size, target.size)
    if count < 32:
        return 0.0, {}
    window = np.hanning(count)
    candidate_spectrum = np.abs(np.fft.rfft(candidate[:count] * window)) ** 2
    target_spectrum = np.abs(np.fft.rfft(target[:count] * window)) ** 2
    frequencies = np.fft.rfftfreq(count, 1.0 / sample_rate)
    edges = (50, 120, 250, 500, 1000, 2000, 4000, 8000, 16000, 22050)
    differences: dict[str, float] = {}
    for low, high in zip(edges[:-1], edges[1:]):
        mask = (frequencies >= low) & (frequencies < min(high, sample_rate / 2))
        if not np.any(mask):
            continue
        candidate_energy = float(np.sum(candidate_spectrum[mask])) * fitted_gain**2
        target_energy = float(np.sum(target_spectrum[mask]))
        delta = 10.0 * math.log10(
            max(candidate_energy, EPS) / max(target_energy, EPS)
        )
        differences[f"{low}-{high}"] = delta
    if not differences:
        return 0.0, {}
    distance = math.sqrt(
        sum(value * value for value in differences.values()) / len(differences)
    )
    return distance, differences


def compare_candidate(
    candidate: np.ndarray,
    target: np.ndarray,
    sample_rate: int,
) -> dict[str, object]:
    aligned_candidate, aligned_target, lag = aligned_pair(
        candidate, target, max_lag=int(round(sample_rate * 0.05))
    )
    denominator = float(np.dot(aligned_candidate, aligned_candidate))
    fitted_gain = (
        float(np.dot(aligned_candidate, aligned_target)) / denominator
        if denominator > EPS
        else 0.0
    )
    fitted = aligned_candidate * fitted_gain
    residual = fitted - aligned_target
    correlation = normalized_corr(aligned_candidate, aligned_target)

    frame_size = max(1, int(round(sample_rate * 0.02)))
    candidate_envelope = frame_rms(fitted, frame_size)
    target_envelope = frame_rms(aligned_target, frame_size)
    envelope_correlation = normalized_corr(
        np.log10(candidate_envelope + EPS),
        np.log10(target_envelope + EPS),
    )
    threshold_low = float(np.quantile(target_envelope, 0.25))
    threshold_high = float(np.quantile(target_envelope, 0.60))
    quiet = target_envelope <= threshold_low
    active = target_envelope >= threshold_high

    def contrast(envelope: np.ndarray) -> float:
        if not np.any(quiet) or not np.any(active):
            return 0.0
        return db(float(np.median(envelope[quiet]))) - db(
            float(np.median(envelope[active]))
        )

    candidate_contrast = contrast(candidate_envelope)
    target_contrast = contrast(target_envelope)
    spectrum_distance, band_deltas = spectral_band_distance(
        aligned_candidate, aligned_target, sample_rate, fitted_gain
    )
    target_rms = rms(aligned_target)
    candidate_rms = rms(aligned_candidate)
    residual_relative_db = db(rms(residual) / max(target_rms, EPS))
    rms_delta_db = db(candidate_rms / max(target_rms, EPS))
    quiet_contrast_delta_db = candidate_contrast - target_contrast

    # The score is only a ranking aid.  Waveform/envelope similarity dominate;
    # absolute level, tonal balance and quiet/active contrast break ties.
    score = (
        0.55 * correlation
        + 0.25 * envelope_correlation
        - 0.010 * abs(rms_delta_db)
        - 0.006 * spectrum_distance
        - 0.003 * abs(quiet_contrast_delta_db)
    )
    return {
        "score": score,
        "lag_samples": lag,
        "lag_ms": 1000.0 * lag / sample_rate,
        "correlation": correlation,
        "envelope_correlation": envelope_correlation,
        "fitted_gain": fitted_gain,
        "fitted_gain_db": db(fitted_gain),
        "residual_relative_db": residual_relative_db,
        "candidate_rms_dbfs": db(candidate_rms),
        "target_rms_dbfs": db(target_rms),
        "rms_delta_db": rms_delta_db,
        "candidate_peak_dbfs": db(float(np.max(np.abs(aligned_candidate)))),
        "target_peak_dbfs": db(float(np.max(np.abs(aligned_target)))),
        "spectral_rmse_db": spectrum_distance,
        "band_delta_db_after_gain": band_deltas,
        "candidate_quiet_to_active_db": candidate_contrast,
        "target_quiet_to_active_db": target_contrast,
        "quiet_contrast_delta_db": quiet_contrast_delta_db,
    }


def run_stage(
    executable: Path,
    source: Path,
    target: Path,
    sample_rate: int,
    channels: int,
) -> None:
    if "denoise" in executable.name:
        arguments = [
            str(executable),
            str(source),
            str(target),
            str(sample_rate),
            str(channels),
            "0.85",
            "1",
        ]
    else:
        arguments = [
            str(executable),
            str(source),
            str(target),
            str(sample_rate),
            str(channels),
        ]
    completed = subprocess.run(
        arguments,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{executable.name} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )


def run_chain(
    audio: np.ndarray,
    sample_rate: int,
    denoise: Path,
    focus: Path,
    temporary: Path,
    label: str,
) -> np.ndarray:
    input_path = temporary / f"{label}_input.f32"
    denoised_path = temporary / f"{label}_denoised.f32"
    focused_path = temporary / f"{label}_focused.f32"
    np.asarray(audio, dtype="<f4").tofile(input_path)
    run_stage(denoise, input_path, denoised_path, sample_rate, 1)
    run_stage(focus, denoised_path, focused_path, sample_rate, 1)
    result = np.fromfile(focused_path, dtype="<f4")
    if result.size != audio.size:
        raise ValueError(
            f"{label}: chain changed frame count {audio.size} -> {result.size}"
        )
    return result


def find_target(target_directory: Path, name: str) -> Path:
    prefix = TARGET_PREFIX.get(name)
    if prefix:
        exact = target_directory / f"{prefix}{name}"
        if exact.exists():
            return exact
    matches = sorted(target_directory.glob(f"*_{name}"))
    if len(matches) != 1:
        raise FileNotFoundError(f"could not resolve one target for {name}: {matches}")
    return matches[0]


def prepare_clips(
    raw_directory: Path,
    vocal_directory: Path,
    target_directory: Path,
) -> list[PreparedClip]:
    clips: list[PreparedClip] = []
    available = {path.name for path in vocal_directory.glob("*.wav")}
    names = [name for name in TARGET_PREFIX if name in available]
    names.extend(sorted(available.difference(names)))
    if not names:
        raise FileNotFoundError(f"no DFN WAV files in {vocal_directory}")
    for name in names:
        raw_wav = read_pcm_wav(raw_directory / name)
        vocal_wav = read_pcm_wav(vocal_directory / name)
        target_wav = read_pcm_wav(find_target(target_directory, name))
        if raw_wav.sample_rate != vocal_wav.sample_rate:
            raise ValueError(f"{name}: raw/DFN sample rates differ")
        raw = mono(raw_wav.samples)
        vocal_short = mono(vocal_wav.samples)
        target = mono(target_wav.samples)
        lag, alignment_corr = best_placement_lag(
            raw, vocal_short, max_lag=int(round(raw_wav.sample_rate * 0.10))
        )
        vocal = place_on_reference(raw, vocal_short, lag, edge_fallback=True)
        raw_resampled = sinc_resample(
            raw,
            raw_wav.sample_rate,
            target_wav.sample_rate,
            output_frames=target.size,
        )
        vocal_resampled = sinc_resample(
            vocal,
            raw_wav.sample_rate,
            target_wav.sample_rate,
            output_frames=target.size,
        )
        clips.append(
            PreparedClip(
                name=name,
                raw=raw_resampled,
                vocal=vocal_resampled,
                target=target,
                sample_rate=target_wav.sample_rate,
                dfn_lag_48k=lag,
                dfn_alignment_corr=alignment_corr,
                source_rate=raw_wav.sample_rate,
                source_frames=raw_wav.frames,
                vocal_frames=vocal_wav.frames,
            )
        )
        print(
            f"prepared {name}: DFN lag {lag} samples "
            f"({1000.0 * lag / raw_wav.sample_rate:+.3f} ms), "
            f"corr {alignment_corr:.5f}"
        )
    return clips


def aggregate(rows: Iterable[dict[str, object]]) -> dict[str, float]:
    values = list(rows)

    def mean(field: str) -> float:
        return float(np.mean([float(row[field]) for row in values]))

    return {
        "mean_score": mean("score"),
        "mean_correlation": mean("correlation"),
        "mean_envelope_correlation": mean("envelope_correlation"),
        "mean_residual_relative_db": mean("residual_relative_db"),
        "mean_abs_rms_delta_db": float(
            np.mean([abs(float(row["rms_delta_db"])) for row in values])
        ),
        "mean_spectral_rmse_db": mean("spectral_rmse_db"),
        "mean_abs_quiet_contrast_delta_db": float(
            np.mean(
                [abs(float(row["quiet_contrast_delta_db"])) for row in values]
            )
        ),
    }


def stereo(audio: np.ndarray) -> np.ndarray:
    mono_audio = np.asarray(audio, dtype=np.float32)
    return np.column_stack((mono_audio, mono_audio))


def render_configuration(
    clips: list[PreparedClip],
    wet: float,
    pregain: float,
    output_directory: Path,
    denoise: Path,
    focus: Path,
    temporary: Path,
) -> list[Path]:
    paths: list[Path] = []
    for clip in clips:
        mixed = pregain * ((1.0 - wet) * clip.raw + wet * clip.vocal)
        processed = run_chain(
            mixed,
            clip.sample_rate,
            denoise,
            focus,
            temporary,
            f"render_{clip.name.removesuffix('.wav')}",
        )
        path = output_directory / clip.name
        write_pcm24_wav(path, stereo(processed), clip.sample_rate)
        paths.append(path)
    return paths


def write_listening_pair(
    clips: list[PreparedClip],
    candidate_paths: list[Path],
    output_directory: Path,
    gap_seconds: float = 0.5,
) -> tuple[Path, Path]:
    if not clips:
        raise ValueError("cannot build an empty listening reel")
    sample_rate = clips[0].sample_rate
    candidates = {path.name: mono(read_pcm_wav(path).samples) for path in candidate_paths}
    gap = np.zeros(int(round(sample_rate * gap_seconds)), dtype=np.float32)
    reference_parts: list[np.ndarray] = []
    candidate_parts: list[np.ndarray] = []
    for index, clip in enumerate(clips):
        if clip.sample_rate != sample_rate:
            raise ValueError("listening reel requires one sample rate")
        if index:
            reference_parts.append(gap)
            candidate_parts.append(gap)
        reference_parts.append(clip.target)
        candidate_parts.append(candidates[clip.name])
    reference = np.concatenate(reference_parts)
    candidate = np.concatenate(candidate_parts)
    reference_path = output_directory / "A_CAPCUT_REFERENCIA_5_CLIPES.wav"
    candidate_path = output_directory / "B_NOSSO_MELHOR_5_CLIPES.wav"
    write_pcm24_wav(reference_path, stereo(reference), sample_rate)
    write_pcm24_wav(candidate_path, stereo(candidate), sample_rate)
    return reference_path, candidate_path


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    flattened: list[dict[str, object]] = []
    for row in rows:
        flattened.append(
            {
                key: value
                for key, value in row.items()
                if key != "band_delta_db_after_gain"
            }
        )
    fieldnames = list(flattened[0].keys())
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened)


def write_summary(
    path: Path,
    clips: list[PreparedClip],
    global_trials: list[dict[str, object]],
    best: dict[str, object],
    per_clip_best: dict[str, dict[str, object]],
) -> None:
    lines = [
        "# Calibração DeepFilterNet → cadeia CapCut",
        "",
        "A calibração usa `vocal + residual` reconstruídos pela relação",
        "`mix = (1-wet) × raw + wet × vocal`. O valor nominal equivalente ao",
        "Aprimoramento 75 é `wet = 0,75`.",
        "",
        "## Melhor configuração global",
        "",
        f"- wet neural: **{float(best['wet']):.2f}**",
        f"- ganho antes do denoise: **{float(best['pregain']):.3f}×**",
        f"- correlação média: **{float(best['mean_correlation']):.5f}**",
        (
            "- correlação média de envelope: "
            f"**{float(best['mean_envelope_correlation']):.5f}**"
        ),
        (
            "- erro espectral médio após ajuste de ganho: "
            f"**{float(best['mean_spectral_rmse_db']):.2f} dB**"
        ),
        (
            "- diferença absoluta média de RMS: "
            f"**{float(best['mean_abs_rms_delta_db']):.2f} dB**"
        ),
        "",
        "## Alinhamento dos stems DeepFilterNet",
        "",
        "| Clipe | atraso DFN em 48 kHz | correlação de alinhamento |",
        "|---|---:|---:|",
    ]
    for clip in clips:
        lines.append(
            f"| {clip.name} | {clip.dfn_lag_48k} amostras "
            f"({1000.0 * clip.dfn_lag_48k / clip.source_rate:+.3f} ms) | "
            f"{clip.dfn_alignment_corr:.5f} |"
        )
    lines.extend(
        [
            "",
            "## Melhor wet por clipe",
            "",
            "| Clipe | wet | correlação | envelope | RMS Δ | espectro RMSE |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, row in sorted(per_clip_best.items()):
        lines.append(
            f"| {name} | {float(row['wet']):.2f} | "
            f"{float(row['correlation']):.5f} | "
            f"{float(row['envelope_correlation']):.5f} | "
            f"{float(row['rms_delta_db']):+.2f} dB | "
            f"{float(row['spectral_rmse_db']):.2f} dB |"
        )
    lines.extend(
        [
            "",
            "## Grade global",
            "",
            "| wet | ganho | score | correlação | envelope | |RMS Δ| | espectro |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for trial in sorted(
        global_trials,
        key=lambda item: (float(item["pregain"]), float(item["wet"])),
    ):
        lines.append(
            f"| {float(trial['wet']):.2f} | {float(trial['pregain']):.3f} | "
            f"{float(trial['mean_score']):.5f} | "
            f"{float(trial['mean_correlation']):.5f} | "
            f"{float(trial['mean_envelope_correlation']):.5f} | "
            f"{float(trial['mean_abs_rms_delta_db']):.2f} dB | "
            f"{float(trial['mean_spectral_rmse_db']):.2f} dB |"
        )
    lines.extend(
        [
            "",
            "## Observação de distribuição",
            "",
            "O runner `denoise_v2` e o modelo instalados do CapCut são usados aqui",
            "somente como oráculo local de calibração. Eles não devem ser incluídos",
            "em um plug-in distribuído.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    root_default = Path(__file__).resolve().parent.parent
    parser.add_argument("--root", type=Path, default=root_default)
    parser.add_argument("--wets", type=parse_number_list, default=DEFAULT_WETS)
    parser.add_argument("--pregains", type=parse_number_list, default=(1.0,))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "calibration_output",
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    raw_directory = root / "audition_neural_runner/dfn_input16"
    vocal_directory = root / "audition_neural_runner/dfn_full"
    target_directory = root / "analysis_capcut_blackbox_combined/split_treated"
    denoise = (
        root / "capcut_reverse_engineering_audio/build/capcut_denoise_v2_runner"
    )
    focus = (
        root / "capcut_reverse_engineering_audio/build/capcut_voice_focus_runner"
    )
    for required in (raw_directory, vocal_directory, target_directory, denoise, focus):
        if not required.exists():
            raise FileNotFoundError(required)

    clips = prepare_clips(raw_directory, vocal_directory, target_directory)
    rows: list[dict[str, object]] = []
    nominal_candidates: dict[str, np.ndarray] = {}
    best_per_clip_audio: dict[str, tuple[float, np.ndarray]] = {}

    with tempfile.TemporaryDirectory(prefix="dfn_capcut_", dir=output) as temporary_name:
        temporary = Path(temporary_name)
        total = len(arguments.wets) * len(arguments.pregains) * len(clips)
        completed = 0
        for pregain in arguments.pregains:
            for wet in arguments.wets:
                for clip in clips:
                    mixed = float(pregain) * (
                        (1.0 - float(wet)) * clip.raw + float(wet) * clip.vocal
                    )
                    label = (
                        f"{clip.name.removesuffix('.wav')}"
                        f"_w{float(wet):.3f}_g{float(pregain):.3f}"
                    )
                    processed = run_chain(
                        mixed,
                        clip.sample_rate,
                        denoise,
                        focus,
                        temporary,
                        label,
                    )
                    metrics = compare_candidate(
                        processed, clip.target, clip.sample_rate
                    )
                    row: dict[str, object] = {
                        "clip": clip.name,
                        "wet": float(wet),
                        "pregain": float(pregain),
                        **metrics,
                    }
                    rows.append(row)
                    if (
                        abs(float(wet) - 0.75) < 1.0e-9
                        and abs(float(pregain) - 1.0) < 1.0e-9
                    ):
                        nominal_candidates[clip.name] = processed.copy()
                    previous = best_per_clip_audio.get(clip.name)
                    if previous is None or float(metrics["score"]) > previous[0]:
                        best_per_clip_audio[clip.name] = (
                            float(metrics["score"]),
                            processed.copy(),
                        )
                    completed += 1
                    print(
                        f"[{completed:03d}/{total:03d}] {clip.name} "
                        f"wet={float(wet):.2f} gain={float(pregain):.3f} "
                        f"corr={float(metrics['correlation']):.5f} "
                        f"score={float(metrics['score']):.5f}"
                    )

        global_trials: list[dict[str, object]] = []
        for pregain in arguments.pregains:
            for wet in arguments.wets:
                selected = [
                    row
                    for row in rows
                    if abs(float(row["wet"]) - float(wet)) < 1.0e-9
                    and abs(float(row["pregain"]) - float(pregain)) < 1.0e-9
                ]
                global_trials.append(
                    {
                        "wet": float(wet),
                        "pregain": float(pregain),
                        **aggregate(selected),
                    }
                )
        best_global = max(global_trials, key=lambda item: float(item["mean_score"]))

        global_paths = render_configuration(
            clips,
            float(best_global["wet"]),
            float(best_global["pregain"]),
            output / "candidates_global_best",
            denoise,
            focus,
            temporary,
        )
        listening_pair = write_listening_pair(clips, global_paths, output)

    nominal_directory = output / "candidates_nominal_wet075"
    for clip in clips:
        if clip.name in nominal_candidates:
            write_pcm24_wav(
                nominal_directory / clip.name,
                stereo(nominal_candidates[clip.name]),
                clip.sample_rate,
            )
    per_clip_directory = output / "candidates_per_clip_best"
    for clip in clips:
        write_pcm24_wav(
            per_clip_directory / clip.name,
            stereo(best_per_clip_audio[clip.name][1]),
            clip.sample_rate,
        )

    per_clip_best: dict[str, dict[str, object]] = {}
    for clip in clips:
        per_clip_best[clip.name] = max(
            (row for row in rows if row["clip"] == clip.name),
            key=lambda item: float(item["score"]),
        )

    write_csv(output / "METRICS.csv", rows)
    report = {
        "scope": {
            "clips": [clip.name for clip in clips],
            "mix_law": "(1-wet)*raw + wet*DeepFilterNet_vocal",
            "nominal_wet": 0.75,
            "stage_order": "mix -> denoise_v2(0.85, mode 1) -> Voice Focus",
            "distribution_warning": (
                "CapCut runtime/model are a local calibration oracle only"
            ),
        },
        "prepared_clips": [
            {
                "clip": clip.name,
                "source_rate": clip.source_rate,
                "source_frames": clip.source_frames,
                "dfn_frames": clip.vocal_frames,
                "dfn_lag_samples_48k": clip.dfn_lag_48k,
                "dfn_lag_ms": 1000.0 * clip.dfn_lag_48k / clip.source_rate,
                "dfn_alignment_corr": clip.dfn_alignment_corr,
                "target_rate": clip.sample_rate,
                "target_frames": int(clip.target.size),
            }
            for clip in clips
        ],
        "global_trials": global_trials,
        "best_global": best_global,
        "best_per_clip": per_clip_best,
        "rendered_global_best": [str(path) for path in global_paths],
        "listening_pair": [str(path) for path in listening_pair],
    }
    (output / "RESULTS.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary(
        output / "RELATORIO.md",
        clips,
        global_trials,
        best_global,
        per_clip_best,
    )
    print()
    print(json.dumps({"best_global": best_global}, ensure_ascii=False, indent=2))
    print(f"results: {output}")


if __name__ == "__main__":
    main()
