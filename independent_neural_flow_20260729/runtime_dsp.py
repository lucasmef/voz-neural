#!/usr/bin/env python3
"""Self-contained NumPy audio I/O, alignment, resampling, EQ, and limiting."""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


EPS = 1.0e-12


@dataclass(frozen=True)
class PcmWav:
    sample_rate: int
    channels: int
    sample_width: int
    samples: np.ndarray

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])


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
        flat = (
            np.frombuffer(payload, dtype="<i4").astype(np.float32)
            / 2147483648.0
        )
    else:
        raise ValueError(f"{path}: unsupported PCM width {sample_width * 8} bit")
    expected = frames * channels
    if flat.size != expected:
        raise ValueError(f"{path}: expected {expected} samples, read {flat.size}")
    return PcmWav(
        sample_rate,
        channels,
        sample_width,
        flat.reshape(frames, channels),
    )


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
    return audio if audio.ndim == 1 else np.mean(audio, axis=1, dtype=np.float32)


def db(value: float) -> float:
    return 20.0 * math.log10(max(abs(value), EPS))


def rms(audio: np.ndarray) -> float:
    data = np.asarray(audio, dtype=np.float64)
    return float(np.sqrt(np.mean(data * data))) if data.size else 0.0


def frame_rms(audio: np.ndarray, frame_size: int) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float64)
    if not data.size:
        return np.empty(0, dtype=np.float64)
    frames = int(math.ceil(data.size / frame_size))
    padded = np.pad(data, (0, frames * frame_size - data.size))
    shaped = padded.reshape(frames, frame_size)
    return np.sqrt(np.mean(shaped * shaped, axis=1) + EPS)


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
    lag = int(lags[int(np.argmax(cross[indices]))])
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
    source = np.asarray(audio, dtype=np.float64)
    if source.ndim != 1:
        raise ValueError("sinc_resample expects mono audio")
    if output_frames is None:
        output_frames = int(round(source.size * target_rate / source_rate))
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
        window = np.i0(
            8.6 * np.sqrt(np.maximum(0.0, 1.0 - window_position**2))
        )
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
    if filter_type == "peak":
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
            a0 = amplitude + 1.0 + (amplitude - 1.0) * cosine + beta
            a1 = -2.0 * (
                (amplitude - 1.0) + (amplitude + 1.0) * cosine
            )
            a2 = amplitude + 1.0 + (amplitude - 1.0) * cosine - beta
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
            a0 = amplitude + 1.0 - (amplitude - 1.0) * cosine + beta
            a1 = 2.0 * (
                (amplitude - 1.0) - (amplitude + 1.0) * cosine
            )
            a2 = amplitude + 1.0 - (amplitude - 1.0) * cosine - beta
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
    output = np.asarray(samples, dtype=np.float64).copy()
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
        gain = (
            required
            if required < gain
            else release * gain + (1.0 - release) * required
        )
        output[index] = sample * gain
    return output
