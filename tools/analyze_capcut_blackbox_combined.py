#!/usr/bin/env python3
"""Analyze the combined CapCut treatment exported from the 30-clip test draft."""

from __future__ import annotations

import csv
import json
import math
import wave
from pathlib import Path

import numpy as np


BASE = Path(__file__).resolve().parents[1]
PACK = BASE / "capcut_blackbox_audio_pack_female_v3"
TIMECODES = BASE / "capcut_project_audio_female_30_clips" / "MAPA_TIMECODES.csv"
TREATED = Path(
    "/Users/lucas/Movies/Vídeos/Edit/CapCut_Audio_Feminino_30_Clipes.WAV"
)
OUTPUT = BASE / "analysis_capcut_blackbox_combined"
EPS = 1e-12
BANDS = [
    ("sub_20_80", 20, 80),
    ("low_80_200", 80, 200),
    ("body_200_500", 200, 500),
    ("mid_500_1000", 500, 1000),
    ("presence_1k_2k", 1000, 2000),
    ("clarity_2k_4k", 2000, 4000),
    ("sibilance_4k_8k", 4000, 8000),
    ("air_8k_16k", 8000, 16000),
    ("ultra_16k_20k", 16000, 20000),
]


def read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        rate = handle.getframerate()
        width = handle.getsampwidth()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        bytes_ = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        integers = (
            bytes_[:, 0].astype(np.int32)
            | (bytes_[:, 1].astype(np.int32) << 8)
            | (bytes_[:, 2].astype(np.int32) << 16)
        )
        integers = np.where(integers >= (1 << 23), integers - (1 << 24), integers)
        data = integers.astype(np.float64) / float(1 << 23)
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    else:
        raise ValueError(f"PCM de {width * 8} bits não suportado: {path}")
    return data.reshape(-1, channels), rate, width * 8


def write_wav_16(path: Path, samples: np.ndarray, rate: int) -> None:
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    pcm = np.clip(np.round(samples * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(samples.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def mono(samples: np.ndarray) -> np.ndarray:
    return np.mean(samples, axis=1)


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples.copy()
    output_length = int(round(len(samples) * target_rate / source_rate))
    source_positions = np.arange(len(samples), dtype=np.float64)
    target_positions = np.arange(output_length, dtype=np.float64) * source_rate / target_rate
    if samples.ndim == 1:
        return np.interp(target_positions, source_positions, samples)
    return np.column_stack(
        [
            np.interp(target_positions, source_positions, samples[:, channel])
            for channel in range(samples.shape[1])
        ]
    )


def amp_db(value: float) -> float:
    return 20.0 * math.log10(max(float(value), EPS))


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(samples) ** 2) + EPS))


def frame_rms(samples: np.ndarray, frame: int) -> np.ndarray:
    count = len(samples) // frame
    if count == 0:
        return np.array([], dtype=np.float64)
    blocks = samples[: count * frame].reshape(count, frame)
    return np.sqrt(np.mean(blocks * blocks, axis=1) + EPS)


def align_envelope(
    source: np.ndarray, treated: np.ndarray, rate: int
) -> tuple[float, int, int, int]:
    hop = max(1, round(rate * 0.005))
    source_env = np.clip(20 * np.log10(frame_rms(source, hop) + EPS), -100, 0)
    treated_env = np.clip(20 * np.log10(frame_rms(treated, hop) + EPS), -100, 0)
    best_corr, best_lag = -2.0, 0
    for lag in range(-80, 81):
        if lag >= 0:
            length = min(len(source_env) - lag, len(treated_env))
            a = source_env[lag : lag + length]
            b = treated_env[:length]
        else:
            length = min(len(source_env), len(treated_env) + lag)
            a = source_env[:length]
            b = treated_env[-lag : -lag + length]
        if length < 80:
            continue
        a = a - np.mean(a)
        b = b - np.mean(b)
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        corr = float(np.dot(a, b) / denom) if denom > EPS else -1.0
        if corr > best_corr:
            best_corr, best_lag = corr, lag
    if best_lag >= 0:
        return best_corr, best_lag * hop, 0, best_lag
    return best_corr, 0, -best_lag * hop, best_lag


def spectral_band_gains(
    source: np.ndarray, treated: np.ndarray, rate: int, active_only: bool
) -> dict[str, float]:
    fft_size = 4096
    hop = round(rate * 0.050)
    maximum = min(len(source), len(treated)) - fft_size
    if maximum <= 0:
        return {name: float("nan") for name, _, _ in BANDS}
    starts = np.arange(0, maximum + 1, hop)
    levels = np.array([rms(source[start : start + fft_size]) for start in starts])
    if active_only:
        threshold = max(10 ** (-48 / 20), float(np.percentile(levels, 55)))
        starts = starts[levels >= threshold]
    if len(starts) > 220:
        starts = starts[np.linspace(0, len(starts) - 1, 220).astype(int)]
    window = np.hanning(fft_size)
    source_power = np.zeros(fft_size // 2 + 1)
    treated_power = np.zeros(fft_size // 2 + 1)
    for start in starts:
        source_power += np.abs(np.fft.rfft(source[start : start + fft_size] * window)) ** 2
        treated_power += np.abs(np.fft.rfft(treated[start : start + fft_size] * window)) ** 2
    frequencies = np.fft.rfftfreq(fft_size, 1 / rate)
    result = {}
    for name, low, high in BANDS:
        mask = (frequencies >= low) & (frequencies < min(high, rate / 2))
        input_energy = float(np.sum(source_power[mask])) + EPS
        output_energy = float(np.sum(treated_power[mask])) + EPS
        result[name] = 10 * math.log10(output_energy / input_energy)
    return result


def waveform_correlation(source: np.ndarray, treated: np.ndarray) -> float:
    source = source - np.mean(source)
    treated = treated - np.mean(treated)
    denominator = np.linalg.norm(source) * np.linalg.norm(treated)
    return float(np.dot(source, treated) / denominator) if denominator > EPS else 0.0


def analyze_clip(
    row: dict[str, str], treated_full: np.ndarray, treated_rate: int
) -> tuple[dict[str, object], np.ndarray]:
    source_path = PACK / row["categoria"] / row["arquivo"]
    source_multichannel, source_rate, source_bits = read_wav(source_path)
    source_multichannel = resample_linear(source_multichannel, source_rate, treated_rate)
    source = mono(source_multichannel)

    start = round(int(row["inicio_us"]) * treated_rate / 1_000_000)
    nominal_length = round(int(row["duracao_us"]) * treated_rate / 1_000_000)
    treated_multichannel = treated_full[start : start + nominal_length]
    treated = mono(treated_multichannel)

    corr_env, source_start, treated_start, lag_hops = align_envelope(
        source, treated, treated_rate
    )
    length = min(len(source) - source_start, len(treated) - treated_start)
    source_aligned = source[source_start : source_start + length]
    treated_aligned = treated[treated_start : treated_start + length]

    frame = round(treated_rate * 0.020)
    source_frames_db = 20 * np.log10(frame_rms(source_aligned, frame) + EPS)
    treated_frames_db = 20 * np.log10(frame_rms(treated_aligned, frame) + EPS)
    frame_count = min(len(source_frames_db), len(treated_frames_db))
    source_frames_db = source_frames_db[:frame_count]
    treated_frames_db = treated_frames_db[:frame_count]
    active_threshold = max(-50.0, float(np.percentile(source_frames_db, 55)))
    active_mask = source_frames_db >= active_threshold
    quiet_threshold = min(-55.0, float(np.percentile(source_frames_db, 25)))
    quiet_mask = source_frames_db <= quiet_threshold
    if np.count_nonzero(active_mask) < 10:
        active_mask = source_frames_db >= np.percentile(source_frames_db, 70)

    active_gain = float(
        np.median(treated_frames_db[active_mask] - source_frames_db[active_mask])
    )
    quiet_gain = (
        float(np.median(treated_frames_db[quiet_mask] - source_frames_db[quiet_mask]))
        if np.count_nonzero(quiet_mask) >= 5
        else float("nan")
    )
    slope = (
        float(np.polyfit(source_frames_db[active_mask], treated_frames_db[active_mask], 1)[0])
        if np.count_nonzero(active_mask) >= 20
        and np.std(source_frames_db[active_mask]) > 0.2
        else float("nan")
    )

    spectral = spectral_band_gains(
        source_aligned, treated_aligned, treated_rate, active_only=True
    )
    relative_spectral = {
        f"{name}_relative_db": value - active_gain for name, value in spectral.items()
    }
    source_rms = rms(source_aligned)
    treated_rms = rms(treated_aligned)
    source_peak = float(np.max(np.abs(source_aligned)))
    treated_peak = float(np.max(np.abs(treated_aligned)))
    stereo_difference = (
        rms(treated_multichannel[:, 0] - treated_multichannel[:, 1])
        if treated_multichannel.shape[1] == 2
        else 0.0
    )
    stereo_sum = (
        rms(treated_multichannel[:, 0] + treated_multichannel[:, 1])
        if treated_multichannel.shape[1] == 2
        else rms(treated_multichannel[:, 0])
    )

    metrics: dict[str, object] = {
        "ordem": int(row["ordem"]),
        "categoria": row["categoria"],
        "arquivo": row["arquivo"],
        "source_channels": source_multichannel.shape[1],
        "source_rate": source_rate,
        "source_bits": source_bits,
        "treated_channels": treated_multichannel.shape[1],
        "treated_rate": treated_rate,
        "nominal_duration_s": int(row["duracao_us"]) / 1_000_000,
        "actual_treated_duration_s": len(treated_multichannel) / treated_rate,
        "alignment_lag_ms": lag_hops * 5.0,
        "envelope_corr": corr_env,
        "waveform_corr": waveform_correlation(source_aligned, treated_aligned),
        "source_rms_dbfs": amp_db(source_rms),
        "treated_rms_dbfs": amp_db(treated_rms),
        "global_gain_db": amp_db(treated_rms / source_rms),
        "source_peak_dbfs": amp_db(source_peak),
        "treated_peak_dbfs": amp_db(treated_peak),
        "crest_source_db": amp_db(source_peak / source_rms),
        "crest_treated_db": amp_db(treated_peak / treated_rms),
        "active_gain_db": active_gain,
        "quiet_gain_db": quiet_gain,
        "quiet_relative_to_active_db": quiet_gain - active_gain,
        "compression_slope": slope,
        "treated_active_median_dbfs": float(np.median(treated_frames_db[active_mask])),
        "treated_active_p90_dbfs": float(np.percentile(treated_frames_db[active_mask], 90)),
        "treated_quiet_p25_dbfs": float(np.percentile(treated_frames_db, 25)),
        "treated_clip_fraction": float(np.mean(np.abs(treated_aligned) >= 0.999)),
        "stereo_difference_to_sum_db": amp_db(stereo_difference / stereo_sum),
    }
    metrics.update(relative_spectral)
    return metrics, treated_multichannel


def finite(values: list[float]) -> list[float]:
    return [float(value) for value in values if math.isfinite(float(value))]


def summary_for(rows: list[dict[str, object]], predicate) -> dict[str, float]:
    selected = [row for row in rows if predicate(row)]
    keys = [
        "global_gain_db",
        "treated_rms_dbfs",
        "treated_peak_dbfs",
        "active_gain_db",
        "quiet_relative_to_active_db",
        "compression_slope",
        "waveform_corr",
        "treated_active_median_dbfs",
        "treated_active_p90_dbfs",
    ] + [f"{name}_relative_db" for name, _, _ in BANDS]
    result = {"count": len(selected)}
    for key in keys:
        values = finite([row[key] for row in selected])
        result[f"median_{key}"] = float(np.median(values)) if values else float("nan")
    return result


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    split_dir = OUTPUT / "split_treated"
    split_dir.mkdir(exist_ok=True)
    treated_full, treated_rate, treated_bits = read_wav(TREATED)
    with TIMECODES.open("r", encoding="utf-8-sig") as handle:
        timecode_rows = list(csv.DictReader(handle))

    metrics = []
    for row in timecode_rows:
        clip_metrics, treated_clip = analyze_clip(row, treated_full, treated_rate)
        metrics.append(clip_metrics)
        write_wav_16(
            split_dir / f"{int(row['ordem']):02d}_{row['arquivo']}",
            treated_clip,
            treated_rate,
        )

    aggregate = {
        "treated_file": str(TREATED),
        "treated_rate": treated_rate,
        "treated_bits": treated_bits,
        "treated_channels": treated_full.shape[1],
        "treated_duration_s": len(treated_full) / treated_rate,
        "clip_count": len(metrics),
        "all": summary_for(metrics, lambda row: True),
        "calibration": summary_for(
            metrics, lambda row: row["categoria"] == "00_calibration"
        ),
        "clean_speech": summary_for(
            metrics, lambda row: row["arquivo"].startswith(("S01_", "S02_"))
        ),
        "speech_quality": summary_for(
            metrics, lambda row: row["categoria"] == "10_speech_quality"
        ),
        "noise_only": summary_for(
            metrics, lambda row: row["categoria"] == "20_noise_only"
        ),
        "speech_noise_mixes": summary_for(
            metrics, lambda row: row["categoria"] == "30_speech_noise_mixes"
        ),
        "voice_focus": summary_for(
            metrics, lambda row: row["categoria"] == "40_voice_focus"
        ),
    }

    with (OUTPUT / "CLIP_METRICS.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(metrics[0].keys()))
        writer.writeheader()
        writer.writerows(metrics)
    (OUTPUT / "ANALYSIS.json").write_text(
        json.dumps({"clips": metrics, "aggregate": aggregate}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
