#!/usr/bin/env python3
"""Deterministic input frontends used before every neural analysis stage."""

from __future__ import annotations

import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


INPUT_COMPENSATION_NONE = "none"
PREMIERE_MONO_CENTER_V1 = "premiere_mono_center_v1"
SUPPORTED_INPUT_COMPENSATION_MODES = frozenset(
    {INPUT_COMPENSATION_NONE, PREMIERE_MONO_CENTER_V1}
)

_PCM16_SCALE = 32768.0
_PCM24_SCALE = 8_388_608.0
_PCM16_TO_PCM24 = 256
_PREMIERE_ATTENUATION_FLOAT32 = np.float32(1.0 / math.sqrt(2.0))
_PREMIERE_CENTER_GAIN = math.sqrt(2.0)
_PREMIERE_CENTER_GAIN_DB = 20.0 * math.log10(_PREMIERE_CENTER_GAIN)
_MAX_ATTENUATED_PEAK = (
    float(_PREMIERE_ATTENUATION_FLOAT32) + 1.0 / _PCM24_SCALE
)


@dataclass(frozen=True)
class CompensationDiagnostics:
    mode: str
    gain_linear: float
    gain_db: float
    canonical_pcm_bits: int
    input_peak: float
    output_peak: float
    clipped_samples: int
    projection_max_error_lsb24: int
    projection_mismatched_samples: int


def restore_premiere_mono_center_v1(
    samples: np.ndarray,
) -> tuple[np.ndarray, CompensationDiagnostics]:
    """Invert Premiere/AME's mono center attenuation deterministically.

    ``EncoderManager.encodeFile`` first multiplies mono PCM by float32
    ``1/sqrt(2)`` and floors it into PCM24.  Multiplication by ``sqrt(2)``
    restores the level, while canonicalizing to PCM16 removes the irrecoverable
    PCM24 least-significant-bit error.  The operation is deliberately applied
    before VAD, RNNoise, DeepFilterNet, masks, and residual reconstruction.
    """

    source = np.asarray(samples, dtype=np.float32)
    if source.ndim != 1:
        raise ValueError(
            "premiere_mono_center_v1 requires exactly one mono channel"
        )
    if not source.size:
        raise ValueError("premiere_mono_center_v1 rejects empty audio")
    if not np.all(np.isfinite(source)):
        raise ValueError("premiere_mono_center_v1 rejects NaN/Inf samples")

    input_peak = float(np.max(np.abs(source)))
    if input_peak > _MAX_ATTENUATED_PEAK:
        raise ValueError(
            "premiere_mono_center_v1 input exceeds the maximum possible "
            "peak after Premiere's -3.0103 dB mono-center attenuation"
        )

    observed_pcm24 = np.rint(
        source.astype(np.float64) * _PCM24_SCALE
    ).astype(np.int64)
    restored = source.astype(np.float64) * _PREMIERE_CENTER_GAIN
    integer = np.rint(restored * _PCM16_SCALE)
    clipped = (integer < -32768.0) | (integer > 32767.0)
    if np.any(clipped):
        raise ValueError(
            "premiere_mono_center_v1 candidate exceeds the PCM16 range"
        )
    integer = integer.astype(np.int64)

    # Prove that every observed PCM24 value can come from a canonical PCM16
    # sample through the exact float32 multiply and floor measured in AME.
    reprojected_pcm24 = np.floor(
        (integer * _PCM16_TO_PCM24).astype(np.float32)
        * _PREMIERE_ATTENUATION_FLOAT32
    ).astype(np.int64)
    projection_error = np.abs(reprojected_pcm24 - observed_pcm24)
    maximum_projection_error = int(np.max(projection_error))
    mismatched_projection = int(np.count_nonzero(projection_error))
    if maximum_projection_error > 1:
        raise ValueError(
            "premiere_mono_center_v1 rejected audio that does not match "
            "the measured AME PCM16 center-pan transform"
        )

    canonical = (integer / _PCM16_SCALE).astype(np.float32)
    diagnostics = CompensationDiagnostics(
        mode=PREMIERE_MONO_CENTER_V1,
        gain_linear=_PREMIERE_CENTER_GAIN,
        gain_db=_PREMIERE_CENTER_GAIN_DB,
        canonical_pcm_bits=16,
        input_peak=input_peak,
        output_peak=float(np.max(np.abs(canonical))),
        clipped_samples=0,
        projection_max_error_lsb24=maximum_projection_error,
        projection_mismatched_samples=mismatched_projection,
    )
    return canonical, diagnostics


def write_pcm16_mono(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    """Write the canonical frontend signal without another rounding rule."""

    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim != 1:
        raise ValueError("write_pcm16_mono expects one mono channel")
    integer = np.clip(
        np.rint(audio.astype(np.float64) * _PCM16_SCALE),
        -32768,
        32767,
    ).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(integer.tobytes())


def diagnostics_dict(
    diagnostics: CompensationDiagnostics,
) -> dict[str, object]:
    return asdict(diagnostics)
