#!/usr/bin/env python3
"""Compare um WAV Adobe real com o bruto e o alvo CapCut.

O analisador usa somente a biblioteca padrao do Python e NumPy. Ele nao
processa o audio de producao e nao depende dos antigos plug-ins/runtimes.

Principios da medicao:

* audita primeiro os arquivos nativos (taxa, canais, bits e duracao);
* faz downmix aritmetico para mono, preservando o nivel de dual-mono;
* reamostra para uma taxa comum com sinc janelado;
* alinha bruto e candidato ao alvo antes de selecionar voz/fundo;
* separa nivel de entrega de comparacao com loudness/voz igualados;
* gera JSON estruturado e um relatorio Markdown legivel.

O true peak e uma aproximacao por interpolacao sinc 4x por padrao. Para uma
medicao de conformidade, confirme o valor num medidor Adobe/ITU dedicado.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import numpy as np
except ModuleNotFoundError as error:
    raise SystemExit(
        "NumPy nao esta disponivel neste Python. Use o ambiente Python do "
        "projeto (requirements.txt: Python 3.12 + NumPy)."
    ) from error


EPS = 1.0e-12
LOUDNESS_RATE = 48_000
BANDS = (
    ("sub_20_80", 20.0, 80.0),
    ("low_80_200", 80.0, 200.0),
    ("body_200_500", 200.0, 500.0),
    ("mid_500_1000", 500.0, 1_000.0),
    ("presence_1k_2k", 1_000.0, 2_000.0),
    ("clarity_2k_4k", 2_000.0, 4_000.0),
    ("sibilance_4k_8k", 4_000.0, 8_000.0),
    ("air_8k_16k", 8_000.0, 16_000.0),
    ("ultra_16k_20k", 16_000.0, 20_000.0),
)


@dataclass(frozen=True)
class PcmWav:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    samples: np.ndarray

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


def read_pcm_wav(path: Path) -> PcmWav:
    """Leia WAV PCM inteiro de 8/16/24/32 bits como float32 por canal."""

    resolved = path.expanduser().resolve()
    with wave.open(str(resolved), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.getnframes()
        compression = source.getcomptype()
        payload = source.readframes(frames)
    if compression != "NONE":
        raise ValueError(f"{resolved}: WAV comprimido nao e suportado")
    if channels < 1 or sample_rate < 1:
        raise ValueError(f"{resolved}: formato WAV invalido")

    if sample_width == 1:
        integer = np.frombuffer(payload, dtype=np.uint8).astype(np.float32)
        flat = (integer - 128.0) / 128.0
    elif sample_width == 2:
        flat = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        octets = np.frombuffer(payload, dtype=np.uint8)
        if octets.size % 3:
            raise ValueError(f"{resolved}: payload PCM24 malformado")
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
        raise ValueError(
            f"{resolved}: PCM de {sample_width * 8} bits nao suportado"
        )

    expected = frames * channels
    if flat.size != expected:
        raise ValueError(
            f"{resolved}: esperadas {expected} amostras, recebidas {flat.size}"
        )
    return PcmWav(
        path=resolved,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        samples=flat.reshape(frames, channels),
    )


def mono_mean(samples: np.ndarray) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim == 1:
        return audio.copy()
    return np.mean(audio, axis=1, dtype=np.float32)


def rms(audio: np.ndarray) -> float:
    data = np.asarray(audio, dtype=np.float64)
    return float(np.sqrt(np.mean(data * data))) if data.size else 0.0


def amp_db(value: float) -> float:
    return 20.0 * math.log10(max(abs(float(value)), EPS))


def normalized_corr(left: np.ndarray, right: np.ndarray) -> float:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    count = min(a.size, b.size)
    if count < 2:
        return 0.0
    a = a[:count] - float(np.mean(a[:count]))
    b = b[:count] - float(np.mean(b[:count]))
    denominator = math.sqrt(float(np.dot(a, a) * np.dot(b, b)))
    return float(np.dot(a, b) / denominator) if denominator > EPS else 0.0


def frame_rms(audio: np.ndarray, frame_size: int) -> np.ndarray:
    data = np.asarray(audio, dtype=np.float64)
    if not data.size:
        return np.empty(0, dtype=np.float64)
    count = int(math.ceil(data.size / frame_size))
    padded = np.pad(data, (0, count * frame_size - data.size))
    shaped = padded.reshape(count, frame_size)
    return np.sqrt(np.mean(shaped * shaped, axis=1) + EPS)


def sinc_resample(
    audio: np.ndarray,
    source_rate: int,
    target_rate: int,
    output_frames: int | None = None,
    half_width: int = 28,
    chunk_frames: int = 32_768,
) -> np.ndarray:
    """Reamostrador mono sinc/Kaiser em blocos, sem dependencia externa."""

    source = np.asarray(audio, dtype=np.float64)
    if source.ndim != 1:
        raise ValueError("sinc_resample espera audio mono")
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
    i0_beta = np.i0(8.6)
    for first in range(0, output_frames, chunk_frames):
        stop = min(first + chunk_frames, output_frames)
        positions = np.arange(first, stop, dtype=np.float64) * ratio
        centers = np.floor(positions).astype(np.int64)
        indices = centers[:, None] + offsets[None, :]
        distance = indices.astype(np.float64) - positions[:, None]
        inside = np.abs(distance) < half_width
        window_position = np.clip(distance / half_width, -1.0, 1.0)
        window = np.i0(
            8.6 * np.sqrt(np.maximum(0.0, 1.0 - window_position**2))
        ) / i0_beta
        weights = cutoff * np.sinc(cutoff * distance) * window * inside
        valid = (indices >= 0) & (indices < source.size)
        weights *= valid
        safe_indices = np.clip(indices, 0, source.size - 1)
        denominator = np.sum(weights, axis=1)
        denominator = np.where(np.abs(denominator) > EPS, denominator, 1.0)
        result[first:stop] = (
            np.sum(source[safe_indices] * weights, axis=1) / denominator
        ).astype(np.float32)
    return result


def resample_channels(
    samples: np.ndarray, source_rate: int, target_rate: int
) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio[:, None]
    output_frames = int(round(audio.shape[0] * target_rate / source_rate))
    if source_rate == target_rate:
        return audio.copy()
    channels = [
        sinc_resample(
            audio[:, index],
            source_rate,
            target_rate,
            output_frames=output_frames,
        )
        for index in range(audio.shape[1])
    ]
    return np.column_stack(channels)


def approximate_true_peak_channel(
    samples: np.ndarray, factor: int = 4, half_width: int = 24
) -> float:
    """Aproxime pico interamostra com fases sinc/Kaiser entre amostras."""

    audio = np.asarray(samples, dtype=np.float64)
    if not audio.size:
        return 0.0
    peak = float(np.max(np.abs(audio)))
    if factor <= 1:
        return peak
    offsets = np.arange(-half_width + 1, half_width + 1, dtype=np.float64)
    padded = np.pad(audio, (half_width - 1, half_width))
    i0_beta = np.i0(8.6)
    for phase in range(1, factor):
        fraction = phase / factor
        distance = fraction - offsets
        window_position = np.clip(distance / half_width, -1.0, 1.0)
        window = np.i0(
            8.6 * np.sqrt(np.maximum(0.0, 1.0 - window_position**2))
        ) / i0_beta
        weights = np.sinc(distance) * window
        weights /= max(abs(float(np.sum(weights))), EPS)
        interpolated = np.correlate(padded, weights, mode="valid")
        peak = max(peak, float(np.max(np.abs(interpolated))))
    return peak


def biquad(
    audio: np.ndarray,
    b: tuple[float, float, float],
    a: tuple[float, float, float],
) -> np.ndarray:
    source = np.asarray(audio, dtype=np.float64)
    result = np.empty_like(source)
    b0, b1, b2 = b
    _, a1, a2 = a
    z1 = 0.0
    z2 = 0.0
    for index, sample in enumerate(source):
        output = b0 * sample + z1
        z1 = b1 * sample - a1 * output + z2
        z2 = b2 * sample - a2 * output
        result[index] = output
    return result


def k_weight(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate != LOUDNESS_RATE:
        raise ValueError("o filtro K interno espera 48 kHz")
    audio = np.asarray(samples, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, None]
    shelf_b = (1.53512485958697, -2.69169618940638, 1.19839281085285)
    shelf_a = (1.0, -1.69065929318241, 0.73248077421585)
    highpass_b = (1.0, -2.0, 1.0)
    highpass_a = (1.0, -1.99004745483398, 0.99007225036621)
    weighted = np.empty_like(audio)
    for channel in range(audio.shape[1]):
        first = biquad(audio[:, channel], shelf_b, shelf_a)
        weighted[:, channel] = biquad(first, highpass_b, highpass_a)
    return weighted


def window_energies(
    weighted: np.ndarray, block: int, hop: int
) -> np.ndarray:
    audio = np.asarray(weighted, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, None]
    if audio.shape[0] < block:
        audio = np.pad(audio, ((0, block - audio.shape[0]), (0, 0)))
    power = np.sum(audio * audio, axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    starts = np.arange(0, audio.shape[0] - block + 1, hop, dtype=np.int64)
    return (cumulative[starts + block] - cumulative[starts]) / block


def energy_loudness(energy: np.ndarray | float) -> np.ndarray | float:
    return -0.691 + 10.0 * np.log10(np.maximum(energy, EPS))


def loudness_from_weighted(weighted: np.ndarray) -> dict[str, Any]:
    momentary_energy = window_energies(
        weighted, int(0.400 * LOUDNESS_RATE), int(0.100 * LOUDNESS_RATE)
    )
    momentary_loudness = np.asarray(energy_loudness(momentary_energy))
    absolute = momentary_loudness >= -70.0
    if np.any(absolute):
        absolute_mean = float(np.mean(momentary_energy[absolute]))
        relative_gate = float(energy_loudness(absolute_mean)) - 10.0
        gated = absolute & (momentary_loudness >= relative_gate)
        integrated = (
            float(energy_loudness(float(np.mean(momentary_energy[gated]))))
            if np.any(gated)
            else None
        )
    else:
        relative_gate = None
        gated = absolute
        integrated = None

    short_energy = window_energies(
        weighted, int(3.0 * LOUDNESS_RATE), int(1.0 * LOUDNESS_RATE)
    )
    short = np.asarray(energy_loudness(short_energy))
    return {
        "integrated_lufs": integrated,
        "absolute_gate_lufs": -70.0,
        "relative_gate_lufs": relative_gate,
        "gated_momentary_blocks": int(np.count_nonzero(gated)),
        "momentary_max_lufs": float(np.max(momentary_loudness)),
        "short_term": {
            "windows": int(short.size),
            "min_lufs": float(np.min(short)),
            "median_lufs": float(np.median(short)),
            "p90_lufs": float(np.quantile(short, 0.90)),
            "max_lufs": float(np.max(short)),
        },
    }


def file_loudness(samples: np.ndarray, sample_rate: int) -> dict[str, Any]:
    native = resample_channels(samples, sample_rate, LOUDNESS_RATE)
    weighted = k_weight(native, LOUDNESS_RATE)
    mono_weighted = np.mean(weighted, axis=1, keepdims=True)
    return {
        "native_channels": loudness_from_weighted(weighted),
        "mono_equivalent": loudness_from_weighted(mono_weighted),
        "method": "ITU-R BS.1770-style, 48 kHz K-weighting",
        "channel_weighting_note": (
            "Todos os canais recebem peso 1; adequado para mono/estereo sem LFE."
        ),
    }


def channel_audit(samples: np.ndarray) -> dict[str, Any]:
    audio = np.asarray(samples, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, None]
    if audio.shape[1] < 2:
        return {
            "dual_mono_exact": False,
            "left_right_correlation": None,
            "left_minus_right_peak": None,
            "left_minus_right_rms_dbfs": None,
            "native_loudness_minus_mono_expected_lu": 0.0,
        }
    left = audio[:, 0]
    right = audio[:, 1]
    difference = left - right
    return {
        "dual_mono_exact": bool(np.array_equal(left, right)),
        "left_right_correlation": normalized_corr(left, right),
        "left_minus_right_peak": float(np.max(np.abs(difference))),
        "left_minus_right_rms_dbfs": amp_db(rms(difference)),
        "native_loudness_minus_mono_expected_lu": 10.0 * math.log10(2.0),
    }


def audit_file(wav: PcmWav, true_peak_factor: int) -> dict[str, Any]:
    mono = mono_mean(wav.samples)
    channel_sample_peaks = [
        float(np.max(np.abs(wav.samples[:, channel])))
        for channel in range(wav.channels)
    ]
    channel_true_peaks = [
        approximate_true_peak_channel(wav.samples[:, channel], true_peak_factor)
        for channel in range(wav.channels)
    ]
    mono_sample_peak = float(np.max(np.abs(mono)))
    mono_true_peak = approximate_true_peak_channel(mono, true_peak_factor)
    mono_rms = rms(mono)
    loudness = file_loudness(wav.samples, wav.sample_rate)
    native_i = loudness["native_channels"]["integrated_lufs"]
    mono_i = loudness["mono_equivalent"]["integrated_lufs"]
    return {
        "path": str(wav.path),
        "bytes": os.path.getsize(wav.path),
        "sample_rate": wav.sample_rate,
        "channels": wav.channels,
        "bits_per_sample": wav.sample_width * 8,
        "frames": wav.frames,
        "duration_seconds": wav.duration,
        "downmix": "mean of channels (dual-mono preserving)",
        "sample_peak": {
            "per_channel_linear": channel_sample_peaks,
            "per_channel_dbfs": [amp_db(value) for value in channel_sample_peaks],
            "mono_linear": mono_sample_peak,
            "mono_dbfs": amp_db(mono_sample_peak),
        },
        "true_peak_approx": {
            "oversample_factor": true_peak_factor,
            "per_channel_linear": channel_true_peaks,
            "per_channel_dbtp": [amp_db(value) for value in channel_true_peaks],
            "mono_linear": mono_true_peak,
            "mono_dbtp": amp_db(mono_true_peak),
        },
        "mono_rms_dbfs": amp_db(mono_rms),
        "mono_crest_sample_peak_db": amp_db(mono_sample_peak / max(mono_rms, EPS)),
        "mono_crest_true_peak_db": amp_db(mono_true_peak / max(mono_rms, EPS)),
        "loudness": loudness,
        "native_minus_mono_integrated_lu": (
            float(native_i - mono_i)
            if native_i is not None and mono_i is not None
            else None
        ),
        "channel_audit": channel_audit(wav.samples),
    }


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
    reference: np.ndarray, moving: np.ndarray, max_lag: int
) -> tuple[int, float]:
    """Retorne onde moving[0] deve cair na timeline de reference."""

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


def common_overlap(
    target: np.ndarray,
    raw: np.ndarray,
    candidate: np.ndarray,
    raw_lag: int,
    candidate_lag: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    start = max(0, raw_lag, candidate_lag)
    stop = min(
        target.size,
        raw_lag + raw.size,
        candidate_lag + candidate.size,
    )
    if stop - start < 32:
        raise ValueError("nao ha sobreposicao suficiente depois do alinhamento")
    target_common = target[start:stop]
    raw_common = raw[start - raw_lag : stop - raw_lag]
    candidate_common = candidate[
        start - candidate_lag : stop - candidate_lag
    ]
    return target_common, raw_common, candidate_common, start, stop


def selection_masks(
    raw: np.ndarray,
    sample_rate: int,
    frame_ms: float,
    edge_ms: float,
    quiet_quantile: float,
    active_quantile: float,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    frame_size = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    envelope = frame_rms(raw, frame_size)
    eligible = np.ones(envelope.size, dtype=bool)
    edge_frames = int(round(edge_ms / frame_ms))
    if envelope.size > 2 * edge_frames and edge_frames > 0:
        eligible[:edge_frames] = False
        eligible[-edge_frames:] = False
    values = envelope[eligible]
    if not values.size:
        raise ValueError("audio curto demais para a exclusao de bordas")
    quiet_threshold = float(np.quantile(values, quiet_quantile))
    active_threshold = float(np.quantile(values, active_quantile))
    quiet = eligible & (envelope <= quiet_threshold)
    active = eligible & (envelope >= active_threshold)
    return (
        {"eligible": eligible, "quiet": quiet, "active": active},
        {
            "basis": "raw mono frame RMS",
            "frame_ms": frame_ms,
            "frame_samples": frame_size,
            "edge_exclusion_ms": edge_ms,
            "quiet_quantile": quiet_quantile,
            "active_quantile": active_quantile,
            "quiet_threshold_dbfs": amp_db(quiet_threshold),
            "active_threshold_dbfs": amp_db(active_threshold),
            "eligible_frames": int(np.count_nonzero(eligible)),
            "quiet_frames": int(np.count_nonzero(quiet)),
            "active_frames": int(np.count_nonzero(active)),
            "quiet_seconds": float(np.count_nonzero(quiet) * frame_ms / 1000.0),
            "active_seconds": float(np.count_nonzero(active) * frame_ms / 1000.0),
        },
    )


def samples_from_frame_mask(
    frame_mask: np.ndarray, frame_size: int, length: int
) -> np.ndarray:
    return np.repeat(frame_mask, frame_size)[:length]


def frame_level_summary(envelope: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    selected = np.asarray(envelope)[mask]
    if not selected.size:
        return {"frames": 0}
    levels = 20.0 * np.log10(np.maximum(selected, EPS))
    return {
        "frames": int(selected.size),
        "p10_dbfs": float(np.quantile(levels, 0.10)),
        "median_dbfs": float(np.median(levels)),
        "p90_dbfs": float(np.quantile(levels, 0.90)),
        "envelope_rms_dbfs": amp_db(rms(selected)),
    }


def region_metrics(
    audio: np.ndarray,
    reference: np.ndarray,
    sample_mask: np.ndarray,
    envelope: np.ndarray,
    reference_envelope: np.ndarray,
    frame_mask: np.ndarray,
) -> dict[str, Any]:
    selected = audio[sample_mask]
    selected_reference = reference[sample_mask]
    if not selected.size or not selected_reference.size:
        return {
            "samples": 0,
            "candidate_rms_dbfs": None,
            "reference_rms_dbfs": None,
            "candidate_minus_reference_rms_db": None,
            "waveform_correlation": None,
            "log_envelope_correlation": None,
            "candidate_frame_levels": frame_level_summary(
                envelope, frame_mask
            ),
            "reference_frame_levels": frame_level_summary(
                reference_envelope, frame_mask
            ),
        }
    audio_rms = rms(selected)
    reference_rms = rms(selected_reference)
    return {
        "samples": int(selected.size),
        "candidate_rms_dbfs": amp_db(audio_rms),
        "reference_rms_dbfs": amp_db(reference_rms),
        "candidate_minus_reference_rms_db": amp_db(
            audio_rms / max(reference_rms, EPS)
        ),
        "waveform_correlation": normalized_corr(selected, selected_reference),
        "log_envelope_correlation": normalized_corr(
            np.log10(envelope[frame_mask] + EPS),
            np.log10(reference_envelope[frame_mask] + EPS),
        ),
        "candidate_frame_levels": frame_level_summary(envelope, frame_mask),
        "reference_frame_levels": frame_level_summary(
            reference_envelope, frame_mask
        ),
    }


def accumulated_spectrum(
    audio: np.ndarray,
    sample_mask: np.ndarray | None,
    n_fft: int = 4096,
    hop: int = 2048,
    minimum_mask_fraction: float = 0.50,
) -> tuple[np.ndarray, int]:
    data = np.asarray(audio, dtype=np.float64)
    if data.size < n_fft:
        padded = np.pad(data, (0, n_fft - data.size))
        return np.abs(np.fft.rfft(padded * np.hanning(n_fft))) ** 2, 1
    starts = np.arange(0, data.size - n_fft + 1, hop, dtype=np.int64)
    if sample_mask is not None:
        mask = np.asarray(sample_mask, dtype=np.float64)
        cumulative = np.concatenate(([0.0], np.cumsum(mask)))
        fractions = (cumulative[starts + n_fft] - cumulative[starts]) / n_fft
        starts = starts[fractions >= minimum_mask_fraction]
    if not starts.size:
        if sample_mask is None or not np.any(sample_mask):
            return np.zeros(n_fft // 2 + 1), 0
        selected = data[np.asarray(sample_mask, dtype=bool)]
        if selected.size < n_fft:
            selected = np.pad(selected, (0, n_fft - selected.size))
        starts = np.arange(0, selected.size - n_fft + 1, hop, dtype=np.int64)
        data = selected
    window = np.hanning(n_fft)
    power = np.zeros(n_fft // 2 + 1, dtype=np.float64)
    for start in starts:
        spectrum = np.fft.rfft(data[start : start + n_fft] * window)
        power += np.abs(spectrum) ** 2
    power /= max(int(starts.size), 1)
    return power, int(starts.size)


def spectral_comparison(
    candidate_power: np.ndarray,
    reference_power: np.ndarray,
    sample_rate: int,
    match_gain_db: float,
    candidate_windows: int,
    reference_windows: int,
) -> dict[str, Any]:
    if candidate_windows < 1 or reference_windows < 1:
        return {
            "match_gain_db": match_gain_db,
            "candidate_windows": candidate_windows,
            "reference_windows": reference_windows,
            "mean_absolute_error_db": None,
            "rmse_db": None,
            "band_delta_db": {},
        }
    frequencies = np.fft.rfftfreq((candidate_power.size - 1) * 2, 1.0 / sample_rate)
    band_deltas: dict[str, float] = {}
    for name, low, high in BANDS:
        top = min(high, sample_rate / 2.0)
        mask = (frequencies >= low) & (frequencies < top)
        if top <= low or not np.any(mask):
            continue
        candidate_energy = float(np.sum(candidate_power[mask]))
        reference_energy = float(np.sum(reference_power[mask]))
        delta = (
            10.0
            * math.log10(
                max(candidate_energy, EPS) / max(reference_energy, EPS)
            )
            + match_gain_db
        )
        band_deltas[name] = delta
    values = np.asarray(list(band_deltas.values()), dtype=np.float64)
    return {
        "match_gain_db": match_gain_db,
        "candidate_windows": candidate_windows,
        "reference_windows": reference_windows,
        "mean_absolute_error_db": (
            float(np.mean(np.abs(values))) if values.size else None
        ),
        "rmse_db": (
            float(np.sqrt(np.mean(values * values))) if values.size else None
        ),
        "band_delta_db": band_deltas,
    }


def mono_loudness(audio: np.ndarray, sample_rate: int) -> dict[str, Any]:
    samples = np.asarray(audio, dtype=np.float32)[:, None]
    if sample_rate != LOUDNESS_RATE:
        samples = resample_channels(samples, sample_rate, LOUDNESS_RATE)
    return loudness_from_weighted(k_weight(samples, LOUDNESS_RATE))


def pair_comparison(
    candidate_name: str,
    reference_name: str,
    candidate: np.ndarray,
    reference: np.ndarray,
    masks: dict[str, np.ndarray],
    frame_size: int,
    sample_rate: int,
    loudness_cache: dict[str, dict[str, Any]],
    spectrum_cache: dict[str, dict[str, tuple[np.ndarray, int]]],
) -> dict[str, Any]:
    length = min(candidate.size, reference.size)
    candidate = candidate[:length]
    reference = reference[:length]
    envelope_candidate = frame_rms(candidate, frame_size)
    envelope_reference = frame_rms(reference, frame_size)
    frame_count = min(envelope_candidate.size, envelope_reference.size)
    active_frames = masks["active"][:frame_count]
    quiet_frames = masks["quiet"][:frame_count]
    eligible_frames = masks["eligible"][:frame_count]
    active_samples = samples_from_frame_mask(active_frames, frame_size, length)
    quiet_samples = samples_from_frame_mask(quiet_frames, frame_size, length)
    eligible_samples = samples_from_frame_mask(eligible_frames, frame_size, length)

    active = region_metrics(
        candidate,
        reference,
        active_samples,
        envelope_candidate,
        envelope_reference,
        active_frames,
    )
    quiet = region_metrics(
        candidate,
        reference,
        quiet_samples,
        envelope_candidate,
        envelope_reference,
        quiet_frames,
    )
    overall = region_metrics(
        candidate,
        reference,
        eligible_samples,
        envelope_candidate,
        envelope_reference,
        eligible_frames,
    )

    candidate_lufs = loudness_cache[candidate_name]["integrated_lufs"]
    reference_lufs = loudness_cache[reference_name]["integrated_lufs"]
    loudness_match_gain_db = (
        float(reference_lufs - candidate_lufs)
        if candidate_lufs is not None and reference_lufs is not None
        else 0.0
    )
    active_delta = active["candidate_minus_reference_rms_db"]
    quiet_delta = quiet["candidate_minus_reference_rms_db"]
    voice_match_gain_db = (
        -float(active_delta) if active_delta is not None else 0.0
    )
    background_after_voice_match = (
        float(quiet_delta) - float(active_delta)
        if quiet_delta is not None and active_delta is not None
        else None
    )

    candidate_active_levels = active["candidate_frame_levels"]
    reference_active_levels = active["reference_frame_levels"]
    candidate_quiet_levels = quiet["candidate_frame_levels"]
    reference_quiet_levels = quiet["reference_frame_levels"]
    median_values = (
        candidate_quiet_levels.get("median_dbfs"),
        reference_quiet_levels.get("median_dbfs"),
        candidate_active_levels.get("median_dbfs"),
        reference_active_levels.get("median_dbfs"),
    )
    quiet_median_after_voice_match = (
        float(median_values[0])
        - float(median_values[1])
        - (float(median_values[2]) - float(median_values[3]))
        if all(value is not None for value in median_values)
        else None
    )

    candidate_active_power, candidate_active_windows = spectrum_cache[
        candidate_name
    ]["active"]
    reference_active_power, reference_active_windows = spectrum_cache[
        reference_name
    ]["active"]
    candidate_quiet_power, candidate_quiet_windows = spectrum_cache[
        candidate_name
    ]["quiet"]
    reference_quiet_power, reference_quiet_windows = spectrum_cache[
        reference_name
    ]["quiet"]

    candidate_rms = rms(candidate[eligible_samples])
    reference_rms = rms(reference[eligible_samples])
    candidate_peak = float(np.max(np.abs(candidate[eligible_samples])))
    reference_peak = float(np.max(np.abs(reference[eligible_samples])))
    return {
        "candidate": candidate_name,
        "reference": reference_name,
        "production_level": {
            "integrated_lufs_candidate": candidate_lufs,
            "integrated_lufs_reference": reference_lufs,
            "candidate_minus_reference_lu": (
                float(candidate_lufs - reference_lufs)
                if candidate_lufs is not None and reference_lufs is not None
                else None
            ),
            "eligible_rms_delta_db": amp_db(
                candidate_rms / max(reference_rms, EPS)
            ),
            "eligible_sample_peak_candidate_dbfs": amp_db(candidate_peak),
            "eligible_sample_peak_reference_dbfs": amp_db(reference_peak),
            "sample_peak_delta_db": amp_db(
                candidate_peak / max(reference_peak, EPS)
            ),
            "crest_candidate_db": amp_db(
                candidate_peak / max(candidate_rms, EPS)
            ),
            "crest_reference_db": amp_db(
                reference_peak / max(reference_rms, EPS)
            ),
        },
        "overall": overall,
        "active": active,
        "quiet": quiet,
        "background_after_voice_match": {
            "voice_match_gain_db": voice_match_gain_db,
            "quiet_rms_delta_db": background_after_voice_match,
            "quiet_median_delta_db": quiet_median_after_voice_match,
            "interpretation": (
                "positivo = candidato deixa mais fundo que a referencia apos casar a voz"
            ),
        },
        "envelope": {
            "all_log_rms_correlation": normalized_corr(
                np.log10(envelope_candidate[:frame_count] + EPS),
                np.log10(envelope_reference[:frame_count] + EPS),
            ),
            "active_log_rms_correlation": active["log_envelope_correlation"],
            "quiet_log_rms_correlation": quiet["log_envelope_correlation"],
        },
        "spectral_active_after_integrated_loudness_match": spectral_comparison(
            candidate_active_power,
            reference_active_power,
            sample_rate,
            loudness_match_gain_db,
            candidate_active_windows,
            reference_active_windows,
        ),
        "spectral_quiet_after_voice_match": spectral_comparison(
            candidate_quiet_power,
            reference_quiet_power,
            sample_rate,
            voice_match_gain_db,
            candidate_quiet_windows,
            reference_quiet_windows,
        ),
    }


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def fmt(value: Any, decimals: int = 2, signed: bool = False) -> str:
    if value is None:
        return "n/d"
    number = float(value)
    prefix = "+" if signed and number >= 0 else ""
    return f"{prefix}{number:.{decimals}f}"


def optional_difference(left: Any, right: Any) -> float | None:
    if left is None or right is None:
        return None
    return float(left) - float(right)


def gate_abs_le(value: Any, limit: float) -> bool | None:
    if value is None:
        return None
    return abs(float(value)) <= limit


def gate_ge(value: Any, limit: float) -> bool | None:
    if value is None:
        return None
    return float(value) >= limit


def markdown_report(result: dict[str, Any]) -> str:
    files = result["files"]
    comparison = result["comparisons"]["candidate_vs_target"]
    lines = [
        f"# Comparacao Adobe real — {result['label']}",
        "",
        "## Resumo",
        "",
        "Valores positivos indicam o candidato acima do CapCut.",
        "",
        "| Medida | Resultado |",
        "|---|---:|",
        "| Loudness integrado | "
        + fmt(
            comparison["production_level"]["candidate_minus_reference_lu"],
            signed=True,
        )
        + " LU |",
        "| RMS da voz ativa | "
        + fmt(
            comparison["active"]["candidate_minus_reference_rms_db"],
            signed=True,
        )
        + " dB |",
        "| Fundo RMS apos casar voz | "
        + fmt(
            comparison["background_after_voice_match"]["quiet_rms_delta_db"],
            signed=True,
        )
        + " dB |",
        "| Piso mediano apos casar voz | "
        + fmt(
            comparison["background_after_voice_match"]["quiet_median_delta_db"],
            signed=True,
        )
        + " dB |",
        "| Correlacao de envelope | "
        + fmt(comparison["envelope"]["all_log_rms_correlation"], 4)
        + " |",
        "| Erro espectral ativo (MAE) | "
        + fmt(
            comparison[
                "spectral_active_after_integrated_loudness_match"
            ]["mean_absolute_error_db"]
        )
        + " dB |",
        "| Diferenca de sample peak | "
        + fmt(
            comparison["native_delivery_file_delta"]["sample_peak_delta_db"],
            signed=True,
        )
        + " dB |",
        "| Diferenca de true peak aprox. | "
        + fmt(
            comparison["native_delivery_file_delta"]["true_peak_delta_db"],
            signed=True,
        )
        + " dB |",
        "",
        "## Arquivos nativos",
        "",
        "| Arquivo | Taxa | Canais/bits | Duracao | RMS mono | Sample peak | True peak aprox. | LUFS mono | LUFS nativo | Crest | Dual-mono |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("raw", "candidate", "target"):
        item = files[name]
        lines.append(
            f"| {name} | {item['sample_rate']} Hz | "
            f"{item['channels']}/{item['bits_per_sample']} | "
            f"{item['duration_seconds']:.6f} s | "
            f"{item['mono_rms_dbfs']:.2f} dBFS | "
            f"{item['sample_peak']['mono_dbfs']:.2f} dBFS | "
            f"{item['true_peak_approx']['mono_dbtp']:.2f} dBTP | "
            f"{fmt(item['loudness']['mono_equivalent']['integrated_lufs'])} | "
            f"{fmt(item['loudness']['native_channels']['integrated_lufs'])} | "
            f"{item['mono_crest_sample_peak_db']:.2f} dB | "
            f"{'sim' if item['channel_audit']['dual_mono_exact'] else 'nao'} |"
        )

    if comparison["quiet"]["samples"] == 0:
        lines.extend(
            [
                "",
                "> Nenhuma janela quieta valida foi selecionada. As metricas "
                "de fundo, piso e envelope quieto estao indisponiveis e "
                "aparecem como `n/d`.",
            ]
        )

    lines.extend(
        [
            "",
            "## Alinhamento e selecao",
            "",
            "| Sinal movido | Lag | Correlacao de alinhamento |",
            "|---|---:|---:|",
            "| bruto → CapCut | "
            + fmt(result["alignment"]["raw_to_target"]["lag_ms"], 3, True)
            + " ms | "
            + fmt(result["alignment"]["raw_to_target"]["correlation"], 4)
            + " |",
            "| Adobe → CapCut | "
            + fmt(
                result["alignment"]["candidate_to_target"]["lag_ms"],
                3,
                True,
            )
            + " ms | "
            + fmt(
                result["alignment"]["candidate_to_target"]["correlation"],
                4,
            )
            + " |",
            "",
            f"Regiao comum: `{result['analysis']['common_duration_seconds']:.6f} s`; "
            f"voz ativa: `{result['selection']['active_seconds']:.2f} s`; "
            f"fundo: `{result['selection']['quiet_seconds']:.2f} s`.",
            "",
            "## Nivel, dinamica e ambiente",
            "",
            "| Medida | Adobe | CapCut | Diferenca |",
            "|---|---:|---:|---:|",
            "| RMS ativo | "
            + fmt(comparison["active"]["candidate_rms_dbfs"])
            + " | "
            + fmt(comparison["active"]["reference_rms_dbfs"])
            + " | "
            + fmt(
                comparison["active"]["candidate_minus_reference_rms_db"],
                signed=True,
            )
            + " dB |",
            "| RMS quieto | "
            + fmt(comparison["quiet"]["candidate_rms_dbfs"])
            + " | "
            + fmt(comparison["quiet"]["reference_rms_dbfs"])
            + " | "
            + fmt(
                comparison["quiet"]["candidate_minus_reference_rms_db"],
                signed=True,
            )
            + " dB |",
            "| Crest na regiao elegivel | "
            + fmt(comparison["production_level"]["crest_candidate_db"])
            + " | "
            + fmt(comparison["production_level"]["crest_reference_db"])
            + " | — |",
            "| Correlacao de envelope quieto | "
            + fmt(comparison["envelope"]["quiet_log_rms_correlation"], 4)
            + " | 1,0000 | — |",
            "",
            "## Espectro da voz apos loudness-match",
            "",
            "O ganho de avaliacao foi `"
            + fmt(
                comparison[
                    "spectral_active_after_integrated_loudness_match"
                ]["match_gain_db"],
                signed=True,
            )
            + " dB`. Ele serve apenas para comparar timbre e nao altera o nivel de entrega.",
            "",
            "| Banda | Adobe menos CapCut |",
            "|---|---:|",
        ]
    )
    for band, value in comparison[
        "spectral_active_after_integrated_loudness_match"
    ]["band_delta_db"].items():
        lines.append(f"| {band} | {fmt(value, signed=True)} dB |")

    lines.extend(
        [
            "",
            "## Loudness short-term",
            "",
            "| Arquivo | Mediana | P90 | Maximo |",
            "|---|---:|---:|---:|",
        ]
    )
    for name in ("raw", "candidate", "target"):
        short = files[name]["loudness"]["mono_equivalent"]["short_term"]
        lines.append(
            f"| {name} | {fmt(short['median_lufs'])} | "
            f"{fmt(short['p90_lufs'])} | {fmt(short['max_lufs'])} |"
        )

    lines.extend(
        [
            "",
            "## Comparacoes secundarias",
            "",
            "| Comparacao | RMS ativo | Fundo apos casar voz | Env. corr. | Espectro ativo MAE |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for key in ("raw_vs_target", "candidate_vs_raw"):
        item = result["comparisons"][key]
        lines.append(
            f"| {key} | "
            f"{fmt(item['active']['candidate_minus_reference_rms_db'], signed=True)} dB | "
            f"{fmt(item['background_after_voice_match']['quiet_rms_delta_db'], signed=True)} dB | "
            f"{fmt(item['envelope']['all_log_rms_correlation'], 4)} | "
            f"{fmt(item['spectral_active_after_integrated_loudness_match']['mean_absolute_error_db'])} dB |"
        )

    lines.extend(
        [
            "",
            "## Notas metodologicas",
            "",
            "- Toda metrica do candidato usa o WAV realmente renderizado pelo Adobe.",
            "- O downmix e a media aritmetica dos canais; dual-mono nao perde 3,01 dB.",
            f"- True peak aproximado por interpolacao sinc {result['analysis']['true_peak_oversample']}x; confirme em medidor dedicado para conformidade.",
            "- LUFS usa filtro K e gates no estilo ITU-R BS.1770; o short-term usa janelas de 3 s.",
            "- A selecao ativa/quieta vem exclusivamente do bruto, depois do alinhamento.",
            "- Processos neurais podem alterar fase; envelope, espectro e ambiente devem ser lidos junto da correlacao de waveform.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True, help="WAV bruto")
    parser.add_argument(
        "--candidate", type=Path, required=True, help="WAV exportado pelo Adobe"
    )
    parser.add_argument(
        "--target", type=Path, required=True, help="WAV alvo exportado pelo CapCut"
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Pasta para JSON e Markdown"
    )
    parser.add_argument(
        "--output-prefix",
        help="Prefixo dos arquivos; por padrao usa o nome do candidato",
    )
    parser.add_argument("--label", help="Rotulo legivel no relatorio")
    parser.add_argument("--analysis-rate", type=int, default=48_000)
    parser.add_argument("--max-lag-ms", type=float, default=1_000.0)
    parser.add_argument("--frame-ms", type=float, default=20.0)
    parser.add_argument("--edge-ms", type=float, default=300.0)
    parser.add_argument("--quiet-quantile", type=float, default=0.25)
    parser.add_argument("--active-quantile", type=float, default=0.65)
    parser.add_argument("--true-peak-oversample", type=int, default=4)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.analysis_rate < 8_000:
        raise SystemExit("--analysis-rate deve ser pelo menos 8000")
    if args.true_peak_oversample < 1 or args.true_peak_oversample > 16:
        raise SystemExit("--true-peak-oversample deve ficar entre 1 e 16")
    if not 0.0 <= args.quiet_quantile < args.active_quantile <= 1.0:
        raise SystemExit("quantis invalidos")

    raw_wav = read_pcm_wav(args.raw)
    candidate_wav = read_pcm_wav(args.candidate)
    target_wav = read_pcm_wav(args.target)
    audits = {
        "raw": audit_file(raw_wav, args.true_peak_oversample),
        "candidate": audit_file(candidate_wav, args.true_peak_oversample),
        "target": audit_file(target_wav, args.true_peak_oversample),
    }

    analysis_audio: dict[str, np.ndarray] = {}
    for name, wav in (
        ("raw", raw_wav),
        ("candidate", candidate_wav),
        ("target", target_wav),
    ):
        mono = mono_mean(wav.samples)
        analysis_audio[name] = sinc_resample(
            mono, wav.sample_rate, args.analysis_rate
        )

    max_lag = int(round(args.analysis_rate * args.max_lag_ms / 1000.0))
    raw_lag, raw_alignment_corr = best_placement_lag(
        analysis_audio["target"], analysis_audio["raw"], max_lag
    )
    candidate_lag, candidate_alignment_corr = best_placement_lag(
        analysis_audio["target"], analysis_audio["candidate"], max_lag
    )
    target, raw, candidate, common_start, common_stop = common_overlap(
        analysis_audio["target"],
        analysis_audio["raw"],
        analysis_audio["candidate"],
        raw_lag,
        candidate_lag,
    )

    masks, selection = selection_masks(
        raw,
        args.analysis_rate,
        args.frame_ms,
        args.edge_ms,
        args.quiet_quantile,
        args.active_quantile,
    )
    frame_size = int(selection["frame_samples"])
    active_samples = samples_from_frame_mask(
        masks["active"], frame_size, target.size
    )
    quiet_samples = samples_from_frame_mask(
        masks["quiet"], frame_size, target.size
    )

    common_audio = {"raw": raw, "candidate": candidate, "target": target}
    loudness_cache = {
        name: mono_loudness(audio, args.analysis_rate)
        for name, audio in common_audio.items()
    }
    spectrum_cache: dict[str, dict[str, tuple[np.ndarray, int]]] = {}
    for name, audio in common_audio.items():
        spectrum_cache[name] = {
            "active": accumulated_spectrum(audio, active_samples),
            "quiet": accumulated_spectrum(audio, quiet_samples),
        }

    comparisons = {
        "candidate_vs_target": pair_comparison(
            "candidate",
            "target",
            candidate,
            target,
            masks,
            frame_size,
            args.analysis_rate,
            loudness_cache,
            spectrum_cache,
        ),
        "raw_vs_target": pair_comparison(
            "raw",
            "target",
            raw,
            target,
            masks,
            frame_size,
            args.analysis_rate,
            loudness_cache,
            spectrum_cache,
        ),
        "candidate_vs_raw": pair_comparison(
            "candidate",
            "raw",
            candidate,
            raw,
            masks,
            frame_size,
            args.analysis_rate,
            loudness_cache,
            spectrum_cache,
        ),
    }
    primary = comparisons["candidate_vs_target"]
    candidate_integrated_lufs = audits["candidate"]["loudness"][
        "mono_equivalent"
    ]["integrated_lufs"]
    target_integrated_lufs = audits["target"]["loudness"][
        "mono_equivalent"
    ]["integrated_lufs"]
    primary["native_delivery_file_delta"] = {
        "duration_delta_ms": 1000.0
        * (
            float(audits["candidate"]["duration_seconds"])
            - float(audits["target"]["duration_seconds"])
        ),
        "sample_peak_delta_db": (
            float(audits["candidate"]["sample_peak"]["mono_dbfs"])
            - float(audits["target"]["sample_peak"]["mono_dbfs"])
        ),
        "true_peak_delta_db": (
            float(audits["candidate"]["true_peak_approx"]["mono_dbtp"])
            - float(audits["target"]["true_peak_approx"]["mono_dbtp"])
        ),
        "integrated_loudness_delta_lu": optional_difference(
            candidate_integrated_lufs,
            target_integrated_lufs,
        ),
        "basis": "full native files before resampling/alignment",
    }
    quality_gates = {
        "active_rms_abs_le_1db": gate_abs_le(
            primary["active"]["candidate_minus_reference_rms_db"], 1.0
        ),
        "active_spectral_mae_le_1_5db": gate_abs_le(
            primary["spectral_active_after_integrated_loudness_match"][
                "mean_absolute_error_db"
            ],
            1.5,
        ),
        "envelope_corr_ge_0_98": gate_ge(
            primary["envelope"]["all_log_rms_correlation"], 0.98
        ),
        "sample_peak_abs_delta_le_0_5db": gate_abs_le(
            primary["native_delivery_file_delta"]["sample_peak_delta_db"],
            0.5,
        ),
        "background_rms_abs_delta_le_3db": gate_abs_le(
            primary["background_after_voice_match"]["quiet_rms_delta_db"],
            3.0,
        ),
    }

    label = args.label or args.candidate.stem
    result = sanitize_json(
        {
            "schema_version": 1,
            "label": label,
            "files": audits,
            "analysis": {
                "sample_rate": args.analysis_rate,
                "common_start_frame_on_target": common_start,
                "common_stop_frame_on_target": common_stop,
                "common_frames": int(target.size),
                "common_duration_seconds": target.size / args.analysis_rate,
                "max_lag_ms": args.max_lag_ms,
                "true_peak_oversample": args.true_peak_oversample,
                "resampler": "windowed sinc/Kaiser, half-width 28",
                "spectral_fft": 4096,
                "spectral_hop": 2048,
            },
            "alignment": {
                "raw_to_target": {
                    "lag_samples": raw_lag,
                    "lag_ms": 1000.0 * raw_lag / args.analysis_rate,
                    "correlation": raw_alignment_corr,
                },
                "candidate_to_target": {
                    "lag_samples": candidate_lag,
                    "lag_ms": 1000.0 * candidate_lag / args.analysis_rate,
                    "correlation": candidate_alignment_corr,
                },
                "lag_definition": (
                    "positivo = inicio do sinal movido colocado depois do inicio do alvo"
                ),
            },
            "selection": selection,
            "comparisons": comparisons,
            "initial_quality_gates": quality_gates,
            "limitations": [
                "True peak e aproximado por interpolacao sinc; nao e certificacao.",
                "Todos os canais recebem peso de loudness 1; nao usar com LFE.",
                "Metricas objetivas nao substituem A/B cego equalizado em loudness.",
            ],
        }
    )

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.output_prefix or args.candidate.stem
    json_path = output_dir / f"{prefix}_COMPARACAO.json"
    markdown_path = output_dir / f"{prefix}_COMPARACAO.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(markdown_report(result), encoding="utf-8")

    summary = {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "candidate_vs_target": {
            "integrated_loudness_delta_lu": primary["production_level"][
                "candidate_minus_reference_lu"
            ],
            "active_rms_delta_db": primary["active"][
                "candidate_minus_reference_rms_db"
            ],
            "background_after_voice_match_db": primary[
                "background_after_voice_match"
            ]["quiet_rms_delta_db"],
            "envelope_correlation": primary["envelope"][
                "all_log_rms_correlation"
            ],
            "active_spectral_mae_db": primary[
                "spectral_active_after_integrated_loudness_match"
            ]["mean_absolute_error_db"],
            "sample_peak_delta_db": primary["native_delivery_file_delta"][
                "sample_peak_delta_db"
            ],
            "true_peak_delta_db": primary["native_delivery_file_delta"][
                "true_peak_delta_db"
            ],
        },
        "quality_gates": quality_gates,
    }
    print(json.dumps(sanitize_json(summary), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
