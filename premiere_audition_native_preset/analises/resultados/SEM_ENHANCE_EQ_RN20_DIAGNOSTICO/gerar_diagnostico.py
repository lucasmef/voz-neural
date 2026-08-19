#!/usr/bin/env python3
"""Diagnostico tonal RN20_DE0 x CapCut somente na calibracao fechada.

O programa e deliberadamente incapaz de abrir o holdout. Ele usa apenas os
dez IDs declarados em ``CALIBRATION_IDS`` e publica espectro de voz ativa,
fundo, eventos de S e uma simulacao conservadora de EQ estatico.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
ANALYSES = ROOT / "premiere_audition_native_preset" / "analises"
sys.path.insert(0, str(ANALYSES))

import comparar_wavs_adobe as audio  # noqa: E402


BASE = (
    ROOT
    / "premiere_audition_native_preset"
    / "comparacao_ab"
    / "FLUXO_APROVADO_20_20260814"
)
CANDIDATES = (
    BASE
    / "resultados_sem_enhance"
    / "RN20_DE0_VS_DE20_SENTINELS_v1"
    / "candidatos_split"
    / "RN20_DE0"
)
CALIBRATION_IDS = ("66", "68", "73", "75", "76", "82", "84", "94", "95", "107")
FORBIDDEN_HOLDOUT = frozenset(("64", "65", "67", "69", "71", "72", "74", "81", "92", "101"))
RATE = 48_000
N_FFT = 8192
HOP = 2048
EPS = 1.0e-18

COARSE_BANDS = (
    ("sub_20_80", 20.0, 80.0),
    ("low_80_200", 80.0, 200.0),
    ("body_200_500", 200.0, 500.0),
    ("mid_500_1000", 500.0, 1000.0),
    ("presence_1k_2k", 1000.0, 2000.0),
    ("clarity_2k_4k", 2000.0, 4000.0),
    ("sibilance_4k_8k", 4000.0, 8000.0),
    ("air_8k_16k", 8000.0, 16000.0),
)
CORE_NAMES = tuple(name for name, _low, _high in COARSE_BANDS[1:7])
LOG_CENTERS = np.asarray(
    [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
     1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
     10000, 12500, 16000],
    dtype=np.float64,
)


def db_power(value: np.ndarray | float) -> np.ndarray | float:
    return 10.0 * np.log10(np.maximum(value, EPS))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_mono(path: Path) -> np.ndarray:
    wav = audio.read_pcm_wav(path)
    return audio.sinc_resample(audio.mono_mean(wav.samples), wav.sample_rate, RATE)


def active_power(signal: np.ndarray, sample_mask: np.ndarray) -> tuple[np.ndarray, int]:
    return audio.accumulated_spectrum(
        signal,
        sample_mask,
        n_fft=N_FFT,
        hop=HOP,
        minimum_mask_fraction=0.50,
    )


def energy(power: np.ndarray, frequencies: np.ndarray, low: float, high: float) -> float:
    mask = (frequencies >= low) & (frequencies < min(high, RATE / 2.0))
    return float(np.sum(power[mask]))


def band_deltas(
    candidate_power: np.ndarray,
    target_power: np.ndarray,
    frequencies: np.ndarray,
    match_gain_db: float,
    bands: tuple[tuple[str, float, float], ...] = COARSE_BANDS,
) -> dict[str, float]:
    return {
        name: float(
            db_power(
                energy(candidate_power, frequencies, low, high)
                / max(energy(target_power, frequencies, low, high), EPS)
            )
            + match_gain_db
        )
        for name, low, high in bands
    }


def log_band_deltas(
    candidate_power: np.ndarray,
    target_power: np.ndarray,
    frequencies: np.ndarray,
    match_gain_db: float,
) -> dict[str, float]:
    rows: dict[str, float] = {}
    half = 2.0 ** (1.0 / 6.0)
    for center in LOG_CENTERS:
        low, high = center / half, center * half
        rows[str(int(center))] = float(
            db_power(
                energy(candidate_power, frequencies, low, high)
                / max(energy(target_power, frequencies, low, high), EPS)
            )
            + match_gain_db
        )
    return rows


def frame_spectral_rows(
    candidate: np.ndarray,
    target: np.ndarray,
    active_frames: np.ndarray,
    frame_size: int,
) -> dict[str, Any]:
    count = min(candidate.size, target.size) // frame_size
    count = min(count, active_frames.size)
    if count < 10:
        raise ValueError("frames insuficientes para eventos de S")
    active = np.flatnonzero(active_frames[:count])
    if active.size < 10:
        raise ValueError("voz ativa insuficiente para eventos de S")
    frame_nfft = 1024
    window = np.hanning(frame_size)
    cand = candidate[: count * frame_size].reshape(count, frame_size)[active]
    targ = target[: count * frame_size].reshape(count, frame_size)[active]
    cand_power = np.abs(np.fft.rfft(cand * window, n=frame_nfft, axis=1)) ** 2
    targ_power = np.abs(np.fft.rfft(targ * window, n=frame_nfft, axis=1)) ** 2
    frequencies = np.fft.rfftfreq(frame_nfft, 1.0 / RATE)
    mid = (frequencies >= 1000.0) & (frequencies < 4000.0)
    high = (frequencies >= 4000.0) & (frequencies < 8000.0)
    ratio_c = db_power(np.sum(cand_power[:, high], axis=1) / np.maximum(np.sum(cand_power[:, mid], axis=1), EPS))
    ratio_t = db_power(np.sum(targ_power[:, high], axis=1) / np.maximum(np.sum(targ_power[:, mid], axis=1), EPS))
    event_count = max(1, int(round(active.size * 0.15)))
    event_local = np.argsort(ratio_t)[-event_count:]
    nons_local = np.setdiff1d(np.arange(active.size), event_local, assume_unique=False)
    event_delta = ratio_c[event_local] - ratio_t[event_local]
    nons_delta = ratio_c[nons_local] - ratio_t[nons_local]
    return {
        "frequencies": frequencies,
        "candidate_power": cand_power,
        "target_power": targ_power,
        "event_local": event_local,
        "nons_local": nons_local,
        "baseline": {
            "active_frames": int(active.size),
            "s_event_frames": int(event_count),
            "s_event_delta_median_db": float(np.median(event_delta)),
            "s_event_delta_p90_abs_db": float(np.quantile(np.abs(event_delta), 0.90)),
            "non_s_event_delta_median_db": float(np.median(nons_delta)),
        },
    }


def rbj_response(
    frequencies: np.ndarray,
    kind: str,
    center: float,
    q: float,
    gain_db: float,
) -> np.ndarray:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * center / RATE
    cosine, sine = math.cos(w0), math.sin(w0)
    alpha = sine / (2.0 * q)
    if kind == "peak":
        b0, b1, b2 = 1.0 + alpha * a, -2.0 * cosine, 1.0 - alpha * a
        a0, a1, a2 = 1.0 + alpha / a, -2.0 * cosine, 1.0 - alpha / a
    elif kind in ("low_shelf", "high_shelf"):
        beta = 2.0 * math.sqrt(a) * alpha
        if kind == "low_shelf":
            b0 = a * ((a + 1.0) - (a - 1.0) * cosine + beta)
            b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cosine)
            b2 = a * ((a + 1.0) - (a - 1.0) * cosine - beta)
            a0 = (a + 1.0) + (a - 1.0) * cosine + beta
            a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cosine)
            a2 = (a + 1.0) + (a - 1.0) * cosine - beta
        else:
            b0 = a * ((a + 1.0) + (a - 1.0) * cosine + beta)
            b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cosine)
            b2 = a * ((a + 1.0) + (a - 1.0) * cosine - beta)
            a0 = (a + 1.0) - (a - 1.0) * cosine + beta
            a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cosine)
            a2 = (a + 1.0) - (a - 1.0) * cosine - beta
    else:
        raise ValueError(kind)
    z1 = np.exp(-1j * 2.0 * math.pi * frequencies / RATE)
    z2 = z1 * z1
    return np.abs((b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2))


def highpass24_response(frequencies: np.ndarray, cutoff: float) -> np.ndarray:
    # Magnitude Butterworth de quarta ordem; aproxima a opcao 24 dB/oct do Premiere.
    safe = np.maximum(frequencies, 1.0e-6)
    return 1.0 / np.sqrt(1.0 + (cutoff / safe) ** 8)


def chain_response(
    frequencies: np.ndarray,
    cutoff: float,
    gains: np.ndarray,
    terminal_kind: str,
) -> np.ndarray:
    result = highpass24_response(frequencies, cutoff)
    fixed = ((250.0, 0.85), (800.0, 0.90), (1600.0, 1.00), (3200.0, 0.90))
    for gain, (center, q) in zip(gains[:4], fixed):
        result *= rbj_response(frequencies, "peak", center, q, float(gain))
    if terminal_kind == "wide_bell":
        result *= rbj_response(frequencies, "peak", 5500.0, 0.70, float(gains[4]))
    elif terminal_kind == "high_shelf":
        result *= rbj_response(frequencies, "high_shelf", 5500.0, 0.80, float(gains[4]))
    else:
        raise ValueError(terminal_kind)
    return result


def focus_response(frequencies: np.ndarray) -> np.ndarray:
    result = np.ones_like(frequencies, dtype=np.float64)
    for kind, center, q, gain in (
        ("low_shelf", 150.0, 1.10, 6.80),
        ("peak", 250.0, 1.00, 9.24),
        ("peak", 500.0, 1.00, 5.26),
        ("peak", 1000.0, 1.00, 2.92),
        ("peak", 2000.0, 1.00, 3.12),
        ("peak", 3000.0, 1.00, 1.92),
        ("high_shelf", 8000.0, 1.00, 3.56),
    ):
        result *= rbj_response(frequencies, kind, center, q, gain)
    return result


def post_eq_band_deltas(record: dict[str, Any], response: np.ndarray) -> dict[str, float]:
    power = record["candidate_active_power"]
    h2 = response * response
    frequencies = record["frequencies"]
    total_mask = (frequencies >= 20.0) & (frequencies < 20000.0)
    overall_gain_db = float(
        db_power(np.sum(power[total_mask] * h2[total_mask]) / max(np.sum(power[total_mask]), EPS))
    )
    result: dict[str, float] = {}
    for name, low, high in COARSE_BANDS:
        mask = (frequencies >= low) & (frequencies < high)
        band_gain = float(db_power(np.sum(power[mask] * h2[mask]) / max(np.sum(power[mask]), EPS)))
        result[name] = record["candidate_active_bands"][name] + band_gain - overall_gain_db
    return result


def objective(records: list[dict[str, Any]], cutoff: float, gains: np.ndarray, terminal_kind: str) -> float:
    per_clip = []
    band_weights = {
        "low_80_200": 0.8,
        "body_200_500": 1.25,
        "mid_500_1000": 1.0,
        "presence_1k_2k": 1.2,
        "clarity_2k_4k": 1.2,
        "sibilance_4k_8k": 1.35,
    }
    for record in records:
        response = chain_response(record["frequencies"], cutoff, gains, terminal_kind)
        bands = post_eq_band_deltas(record, response)
        weighted = []
        for name in CORE_NAMES:
            value = abs(bands[name])
            huber = 0.5 * value * value if value <= 2.0 else 2.0 * (value - 1.0)
            weighted.append(band_weights[name] * huber)
        # Subgrave deve cair, mas nao domina o ajuste por ser muito dependente do ambiente.
        sub = abs(bands["sub_20_80"])
        weighted.append(0.18 * (0.5 * sub * sub if sub <= 3.0 else 3.0 * (sub - 1.5)))
        per_clip.append(float(np.mean(weighted)))
    values = np.asarray(per_clip)
    regularization = 0.025 * float(np.mean(gains * gains))
    return float(np.median(values) + 0.35 * np.quantile(values, 0.90) + regularization)


def fit_structure(records: list[dict[str, Any]], terminal_kind: str) -> dict[str, Any]:
    # Limites deliberadamente conservadores: a curva e uma primeira prova real,
    # nao uma tentativa de zerar cada residuo da calibracao.
    bounds = np.asarray(((0.0, 3.0), (-2.0, 1.0), (-3.5, 0.5), (-3.5, 0.5), (-4.25, 0.5)))
    best: tuple[float, float, np.ndarray] | None = None
    for cutoff in (70.0, 75.0, 80.0, 85.0, 90.0):
        gains = np.asarray((2.0, -0.7, -1.8, -2.0, -3.5), dtype=np.float64)
        for step in (0.75, 0.35, 0.15, 0.05):
            improved = True
            while improved:
                improved = False
                current = objective(records, cutoff, gains, terminal_kind)
                for index in range(gains.size):
                    for direction in (-1.0, 1.0):
                        trial = gains.copy()
                        trial[index] = np.clip(trial[index] + direction * step, bounds[index, 0], bounds[index, 1])
                        score = objective(records, cutoff, trial, terminal_kind)
                        if score + 1.0e-9 < current:
                            gains, current, improved = trial, score, True
            gains = np.round(gains / step) * step
            gains = np.clip(gains, bounds[:, 0], bounds[:, 1])
        gains = np.round(gains * 4.0) / 4.0
        score = objective(records, cutoff, gains, terminal_kind)
        if best is None or score < best[0]:
            best = (score, cutoff, gains.copy())
    assert best is not None
    score, cutoff, gains = best
    return {
        "terminal_kind": terminal_kind,
        "score": score,
        "cutoff_hz": cutoff,
        "gains_db": [float(value) for value in gains],
    }


def s_event_after_eq(spectral: dict[str, Any], cutoff: float, gains: np.ndarray, terminal_kind: str) -> dict[str, float]:
    response = chain_response(spectral["frequencies"], cutoff, gains, terminal_kind)
    h2 = response * response
    frequencies = spectral["frequencies"]
    mid = (frequencies >= 1000.0) & (frequencies < 4000.0)
    high = (frequencies >= 4000.0) & (frequencies < 8000.0)
    cand = spectral["candidate_power"]
    targ = spectral["target_power"]
    ratio_c = db_power(np.sum(cand[:, high] * h2[high], axis=1) / np.maximum(np.sum(cand[:, mid] * h2[mid], axis=1), EPS))
    ratio_t = db_power(np.sum(targ[:, high], axis=1) / np.maximum(np.sum(targ[:, mid], axis=1), EPS))
    event_delta = ratio_c[spectral["event_local"]] - ratio_t[spectral["event_local"]]
    nons_delta = ratio_c[spectral["nons_local"]] - ratio_t[spectral["nons_local"]]
    return {
        "s_event_delta_median_db": float(np.median(event_delta)),
        "s_event_delta_p90_abs_db": float(np.quantile(np.abs(event_delta), 0.90)),
        "non_s_event_delta_median_db": float(np.median(nons_delta)),
    }


def summarize(values: list[float], ids: tuple[str, ...]) -> dict[str, Any]:
    data = np.asarray(values, dtype=np.float64)
    median = float(np.median(data))
    q1, q3 = (float(value) for value in np.quantile(data, (0.25, 0.75)))
    iqr = q3 - q1
    sign = 1.0 if median >= 0.0 else -1.0
    sign_agreement = float(np.mean(data * sign > 0.0))
    mad = float(np.median(np.abs(data - median)))
    threshold = max(2.5, 2.5 * 1.4826 * mad)
    outliers = [clip_id for clip_id, value in zip(ids, data) if abs(float(value) - median) > threshold]
    universal = abs(median) >= 0.75 and sign_agreement >= 0.80 and iqr <= 2.5
    return {
        "median_db": median,
        "q1_db": q1,
        "q3_db": q3,
        "iqr_db": iqr,
        "min_db": float(np.min(data)),
        "max_db": float(np.max(data)),
        "sign_agreement_fraction": sign_agreement,
        "universal_by_rule": bool(universal),
        "outlier_ids": outliers,
    }


def safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        internal_arrays = {
            "frequencies",
            "candidate_active_power",
            "target_active_power",
            "candidate_power",
            "target_power",
            "event_local",
            "nons_local",
        }
        return {
            str(key): safe_json(item)
            for key, item in value.items()
            if not str(key).startswith("_") and str(key) not in internal_arrays
        }
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    return value


def main() -> int:
    if set(CALIBRATION_IDS) & FORBIDDEN_HOLDOUT:
        raise SystemExit("falha de seguranca: holdout na calibracao")
    records: list[dict[str, Any]] = []
    frequencies = np.fft.rfftfreq(N_FFT, 1.0 / RATE)
    for clip_id in CALIBRATION_IDS:
        raw_path = BASE / "raws" / f"{clip_id}_RAW.wav"
        target_path = BASE / "targets" / f"{clip_id}_CAPCUT.wav"
        candidate_path = CANDIDATES / f"{clip_id}.wav"
        for path in (raw_path, target_path, candidate_path):
            if not path.is_file():
                raise FileNotFoundError(path)

        raw_native, target_native, candidate_native = (load_mono(path) for path in (raw_path, target_path, candidate_path))
        raw_lag, raw_corr = audio.best_placement_lag(target_native, raw_native, RATE)
        cand_lag, cand_corr = audio.best_placement_lag(target_native, candidate_native, RATE)
        target, raw, candidate, start, stop = audio.common_overlap(
            target_native, raw_native, candidate_native, raw_lag, cand_lag
        )
        masks, selection = audio.selection_masks(raw, RATE, 20.0, 300.0, 0.25, 0.65)
        frame_size = int(selection["frame_samples"])
        active_samples = audio.samples_from_frame_mask(masks["active"], frame_size, target.size)
        quiet_samples = audio.samples_from_frame_mask(masks["quiet"], frame_size, target.size)

        raw_power, raw_windows = active_power(raw, active_samples)
        cand_power, cand_windows = active_power(candidate, active_samples)
        target_power, target_windows = active_power(target, active_samples)
        cand_quiet_power, cand_quiet_windows = active_power(candidate, quiet_samples)
        target_quiet_power, target_quiet_windows = active_power(target, quiet_samples)

        active_rms_c = audio.rms(candidate[active_samples])
        active_rms_t = audio.rms(target[active_samples])
        active_rms_r = audio.rms(raw[active_samples])
        voice_gain_c = audio.amp_db(active_rms_t / max(active_rms_c, audio.EPS))
        voice_gain_r = audio.amp_db(active_rms_t / max(active_rms_r, audio.EPS))
        quiet_rms_c = audio.rms(candidate[quiet_samples])
        quiet_rms_t = audio.rms(target[quiet_samples])
        background_delta = audio.amp_db(quiet_rms_c / max(quiet_rms_t, audio.EPS)) + voice_gain_c

        record = {
            "clip_id": clip_id,
            "files": {
                "raw": {"path": str(raw_path), "sha256": sha256(raw_path)},
                "candidate": {"path": str(candidate_path), "sha256": sha256(candidate_path)},
                "target": {"path": str(target_path), "sha256": sha256(target_path)},
            },
            "alignment": {
                "raw_lag_ms": 1000.0 * raw_lag / RATE,
                "raw_correlation": raw_corr,
                "candidate_lag_ms": 1000.0 * cand_lag / RATE,
                "candidate_correlation": cand_corr,
                "common_start_frame": start,
                "common_stop_frame": stop,
            },
            "selection": selection,
            "active_voice_match_gain_db": voice_gain_c,
            "background_after_voice_match_db": background_delta,
            "candidate_active_bands": band_deltas(cand_power, target_power, frequencies, voice_gain_c),
            "raw_active_bands": band_deltas(raw_power, target_power, frequencies, voice_gain_r),
            "candidate_log_bands": log_band_deltas(cand_power, target_power, frequencies, voice_gain_c),
            "raw_log_bands": log_band_deltas(raw_power, target_power, frequencies, voice_gain_r),
            "quiet_bands_after_voice_match": band_deltas(cand_quiet_power, target_quiet_power, frequencies, voice_gain_c),
            "s_events": frame_spectral_rows(candidate, target, masks["active"], frame_size),
            "windows": {
                "raw_active": raw_windows,
                "candidate_active": cand_windows,
                "target_active": target_windows,
                "candidate_quiet": cand_quiet_windows,
                "target_quiet": target_quiet_windows,
            },
            "frequencies": frequencies,
            "candidate_active_power": cand_power,
            "target_active_power": target_power,
        }
        records.append(record)

    structures = [fit_structure(records, kind) for kind in ("wide_bell", "high_shelf")]
    for structure in structures:
        cutoff = float(structure["cutoff_hz"])
        gains = np.asarray(structure["gains_db"], dtype=np.float64)
        kind = str(structure["terminal_kind"])
        per_clip = []
        for record in records:
            response = chain_response(record["frequencies"], cutoff, gains, kind)
            bands = post_eq_band_deltas(record, response)
            s_metrics = s_event_after_eq(record["s_events"], cutoff, gains, kind)
            per_clip.append({"clip_id": record["clip_id"], "bands": bands, "s_events": s_metrics})
        structure["per_clip"] = per_clip
        structure["aggregate"] = {
            "core_mae_median_db": float(np.median([
                np.mean([abs(row["bands"][name]) for name in CORE_NAMES]) for row in per_clip
            ])),
            "core_mae_p90_db": float(np.quantile([
                np.mean([abs(row["bands"][name]) for name in CORE_NAMES]) for row in per_clip
            ], 0.90)),
            "s_event_median_abs_median_db": float(np.median([
                abs(row["s_events"]["s_event_delta_median_db"]) for row in per_clip
            ])),
            "s_event_p90_abs_median_db": float(np.median([
                row["s_events"]["s_event_delta_p90_abs_db"] for row in per_clip
            ])),
            "air_abs_median_db": float(np.median([
                abs(row["bands"]["air_8k_16k"]) for row in per_clip
            ])),
        }

    # Escolha conservadora: combina centro, mediana/P90 de S e ar. A mediana de
    # S tem mais peso que poucos centesimos de P90 porque o objetivo e nao
    # "forcar" os S usuais; extremos dependentes do clipe nao justificam shelf.
    chosen = min(
        structures,
        key=lambda row: (
            0.80 * row["aggregate"]["s_event_median_abs_median_db"]
            + 0.30 * row["aggregate"]["s_event_p90_abs_median_db"]
            + 0.50 * row["aggregate"]["core_mae_p90_db"]
            + 0.15 * row["aggregate"]["air_abs_median_db"],
            0 if row["terminal_kind"] == "wide_bell" else 1,
        ),
    )

    band_summary = {
        name: summarize([record["candidate_active_bands"][name] for record in records], CALIBRATION_IDS)
        for name, _low, _high in COARSE_BANDS
    }
    raw_band_summary = {
        name: summarize([record["raw_active_bands"][name] for record in records], CALIBRATION_IDS)
        for name, _low, _high in COARSE_BANDS
    }
    log_summary = {
        str(int(center)): summarize([record["candidate_log_bands"][str(int(center))] for record in records], CALIBRATION_IDS)
        for center in LOG_CENTERS
    }
    quiet_summary = {
        name: summarize([record["quiet_bands_after_voice_match"][name] for record in records], CALIBRATION_IDS)
        for name, _low, _high in COARSE_BANDS
    }
    baseline_core = [
        float(np.mean([abs(record["candidate_active_bands"][name]) for name in CORE_NAMES]))
        for record in records
    ]
    s_baseline_median = [record["s_events"]["baseline"]["s_event_delta_median_db"] for record in records]
    s_baseline_p90 = [record["s_events"]["baseline"]["s_event_delta_p90_abs_db"] for record in records]
    background = [record["background_after_voice_match_db"] for record in records]
    background_summary = summarize(background, CALIBRATION_IDS)

    focus_freqs = LOG_CENTERS.copy()
    focus_db = 20.0 * np.log10(np.maximum(focus_response(focus_freqs), EPS))
    # 8,56 dB e o ganho ativo medido do estagio isolado; removê-lo revela a forma.
    focus_normalized = focus_db - 8.56
    result = {
        "schema_version": 1,
        "scope": {
            "calibration_ids": list(CALIBRATION_IDS),
            "holdout_read_or_analyzed": False,
            "holdout_ids_forbidden_by_code": sorted(FORBIDDEN_HOLDOUT),
            "candidate": "RN20_DE0, Reduce Noise Essential Sound ~20%, DeEss OFF, Auto-Match individual",
        },
        "baseline_aggregate": {
            "core_voice_mae_median_db": float(np.median(baseline_core)),
            "core_voice_mae_p90_db": float(np.quantile(baseline_core, 0.90)),
            "background_after_voice_match_abs_median_db": float(np.median(np.abs(background))),
            "background_after_voice_match_signed_median_db": float(np.median(background)),
            "s_event_delta_abs_median_of_clips_db": float(np.median(np.abs(s_baseline_median))),
            "s_event_p90_abs_median_db": float(np.median(s_baseline_p90)),
        },
        "active_voice_band_summary": band_summary,
        "raw_to_capcut_active_band_summary": raw_band_summary,
        "active_voice_log_band_summary": log_summary,
        "quiet_band_summary_after_voice_match": quiet_summary,
        "background_after_voice_match_summary": background_summary,
        "voice_focus_prior": {
            "exact_stage_active_gain_db": 8.56,
            "known_filters": [
                {"type": "low_shelf", "frequency_hz": 150, "q": 1.10, "gain_db": 6.80},
                {"type": "bell", "frequency_hz": 250, "q": 1.00, "gain_db": 9.24},
                {"type": "bell", "frequency_hz": 500, "q": 1.00, "gain_db": 5.26},
                {"type": "bell", "frequency_hz": 1000, "q": 1.00, "gain_db": 2.92},
                {"type": "bell", "frequency_hz": 2000, "q": 1.00, "gain_db": 3.12},
                {"type": "bell", "frequency_hz": 3000, "q": 1.00, "gain_db": 1.92},
                {"type": "high_shelf", "frequency_hz": 8000, "q": 1.00, "gain_db": 3.56},
            ],
            "normalized_response_db_at_log_centers": {
                str(int(freq)): float(value) for freq, value in zip(focus_freqs, focus_normalized)
            },
            "interpretation": (
                "O prior do Foco confirma a direcao corpo positivo e medios/agudos relativos negativos, "
                "mas seus ganhos absolutos nao podem ser colados no RN20: o alvo ja passou por Vocal "
                "Beautify, denoise, Foco e limiter, enquanto o candidato tem outro redutor e Auto-Match."
            ),
        },
        "eq_structures_tested_frequency_domain_only": structures,
        "recommended_conservative_eq_frequency_domain": chosen,
        "robustness_limits": {
            "do_not_raise_reduce_noise_above_20_percent_from_this_test": True,
            "deess_should_remain_off": True,
            "eq_is_static_and_cannot_reproduce_content_dependent_vocal_beautify": True,
            "maximum_recommended_absolute_bell_gain_db": 4.0,
            "maximum_recommended_high_frequency_cut_db": 4.5,
            "premiere_real_render_required_before_freeze": True,
            "holdout_must_stay_closed_until_curve_and_limiter_are_frozen": True,
        },
        "per_clip": records,
    }

    json_path = HERE / "diagnostico.json"
    json_path.write_text(json.dumps(safe_json(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with (HERE / "RESIDUO_POR_CLIP.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = ["clip_id", "background_after_voice_match_db", "s_event_median_db", "s_event_p90_abs_db"] + [name for name, _low, _high in COARSE_BANDS]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({
                "clip_id": record["clip_id"],
                "background_after_voice_match_db": record["background_after_voice_match_db"],
                "s_event_median_db": record["s_events"]["baseline"]["s_event_delta_median_db"],
                "s_event_p90_abs_db": record["s_events"]["baseline"]["s_event_delta_p90_abs_db"],
                **record["candidate_active_bands"],
            })
    with (HERE / "RESUMO_BANDAS.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = ["band", "median_db", "q1_db", "q3_db", "iqr_db", "min_db", "max_db", "sign_agreement_fraction", "universal_by_rule", "outlier_ids"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for name, summary in band_summary.items():
            writer.writerow({"band": name, **{key: summary[key] for key in fields if key not in ("band", "outlier_ids")}, "outlier_ids": ",".join(summary["outlier_ids"])})

    chosen_gains = chosen["gains_db"]
    recommended_rows = [
        f"HPF 24 dB/oct: {chosen['cutoff_hz']:.0f} Hz",
        f"Bell 1: 250 Hz, Q 0,85, {chosen_gains[0]:+.2f} dB",
        f"Bell 2: 800 Hz, Q 0,90, {chosen_gains[1]:+.2f} dB",
        f"Bell 3: 1,60 kHz, Q 1,00, {chosen_gains[2]:+.2f} dB",
    ]
    if abs(chosen_gains[3]) >= 0.05:
        recommended_rows.append(
            f"Bell 4: 3,20 kHz, Q 0,90, {chosen_gains[3]:+.2f} dB"
        )
    else:
        recommended_rows.append("Banda em 3,20 kHz: desligada (0 dB na simulacao)")
    recommended_rows.append(
        (
            f"Bell largo final: 5,50 kHz, Q 0,70, {chosen_gains[4]:+.2f} dB"
            if chosen["terminal_kind"] == "wide_bell"
            else f"High shelf: 5,50 kHz, Q 0,80, {chosen_gains[4]:+.2f} dB"
        )
    )
    universal_rows = [name for name, summary in band_summary.items() if summary["universal_by_rule"]]
    non_universal_rows = [name for name, summary in band_summary.items() if not summary["universal_by_rule"]]
    markdown = [
        "# Diagnostico RN20_DE0 x CapCut — calibracao sem Enhance",
        "",
        "## Escopo e seguranca",
        "",
        "Foram lidos e analisados somente os IDs de calibracao 66, 68, 73, 75, 76, 82, 84, 94, 95 e 107. O codigo bloqueia explicitamente 64, 65, 67, 69, 71, 72, 74, 81, 92 e 101; o holdout nao foi lido.",
        "",
        "Cadeia de entrada: Reduce Noise do Essential Sound em aproximadamente 20%, DeEss desligado e Auto-Match individual. Nenhum projeto, preset ou fonte foi alterado.",
        "",
        "## Resultado principal",
        "",
        f"Sem EQ, a mediana do erro tonal nas seis bandas centrais foi {np.median(baseline_core):.3f} dB e o P90 {np.quantile(baseline_core, 0.90):.3f} dB. O RN20 fica sistematicamente magro em 200–500 Hz e excessivo de 1–8 kHz. Eventos de S ficam acima do CapCut em mediana absoluta de {np.median(np.abs(s_baseline_median)):.3f} dB, com P90 mediano de {np.median(s_baseline_p90):.3f} dB.",
        "",
        "A correção universal e, portanto: retirar subgrave, devolver corpo perto de 250 Hz, reduzir medios/presenca e aplicar um corte estatico largo em 4–8 kHz. Nao ha suporte para um de-esser dinamico universal; ele ja havia piorado os eventos de S no render real.",
        "",
        "## Resíduo de voz ativa por banda",
        "",
        "Valores positivos significam que o Premiere/RN20 tem mais energia que o CapCut depois de casar o nivel de voz; negativos significam menos.",
        "",
        "| Banda | Mediana | IQR | Concordancia de sinal | Universal | Outliers |",
        "|---|---:|---:|---:|:---:|---|",
    ]
    for name, summary in band_summary.items():
        markdown.append(
            f"| {name} | {summary['median_db']:+.3f} dB | {summary['iqr_db']:.3f} dB | {100*summary['sign_agreement_fraction']:.0f}% | {'sim' if summary['universal_by_rule'] else 'nao'} | {', '.join(summary['outlier_ids']) or '—'} |"
        )
    markdown += [
        "",
        f"Classificadas como universais pela regra robusta: {', '.join(universal_rows) or 'nenhuma'}. Dependentes do clipe/ambiente: {', '.join(non_universal_rows) or 'nenhuma'}.",
        "",
        "## Fundo/ambiencia",
        "",
        f"Depois de casar o nivel da voz, a diferenca absoluta mediana de fundo foi {np.median(np.abs(background)):.3f} dB e a mediana assinada {np.median(background):+.3f} dB. Os clips 73 e 75 sao outliers claros, com aproximadamente +12 dB de fundo no RN20; 94 e 107 tambem ficam acima, mas bem menos. Isso confirma que a separacao neural vocal/residual do CapCut e dependente do conteudo e nao pode ser recuperada por EQ. O EQ final deve ser derivado da voz ativa, sem perseguir esses outliers de ambiente.",
        "",
        "## Reavaliacao do prior Foco na voz",
        "",
        "O prior continua valido como explicacao do bloco tonal local do CapCut: low shelf 150/+6,80; bells 250/+9,24, 500/+5,26, 1k/+2,92, 2k/+3,12 e 3k/+1,92; high shelf 8k/+3,56; limiter -0,30 dB. O proprio estagio acrescenta cerca de +8,56 dB. Depois de retirar esse ganho, sua forma relativa privilegia corpo e deixa medios/agudos abaixo do corpo — exatamente a direcao observada no residual RN20.",
        "",
        "Os numeros absolutos do Foco nao devem ser copiados para o Parametric EQ do Premiere: o alvo final ja inclui Vocal Beautify, denoise, Foco e limiter. Colar +9,24 dB em 250 Hz duplicaria o Foco e criaria grave excessivo. O residual medido nos dez clips e o criterio correto para a nova curva.",
        "",
        "## Curva conservadora proposta para render real",
        "",
    ]
    markdown.extend([f"- {row}" for row in recommended_rows])
    markdown += [
        "",
        f"A simulacao em dominio de frequencia reduziu a mediana estimada do erro central de {np.median(baseline_core):.3f} para {chosen['aggregate']['core_mae_median_db']:.3f} dB e o P90 para {chosen['aggregate']['core_mae_p90_db']:.3f} dB. Nos eventos de S, a mediana absoluta estimada ficou em {chosen['aggregate']['s_event_median_abs_median_db']:.3f} dB e o P90 mediano em {chosen['aggregate']['s_event_p90_abs_median_db']:.3f} dB.",
        "",
        "Esses numeros sao somente previsao linear do Parametric EQ. A curva ainda precisa de round-trip e export real do Premiere antes de virar preset; depois disso, deve ser congelada antes de abrir o holdout.",
        "",
        "## Limites de robustez",
        "",
        "- Manter Reduce Noise em aproximadamente 20%; RN30 nao trouxe ganho liquido e piorou S/timbre.",
        "- Manter DeEss do Essential Sound desligado no preset universal. Se um clipe isolado exigir correção, tratar como excecao manual.",
        "- Nao ultrapassar aproximadamente 4 dB em bells nem 4,5 dB de corte amplo nos agudos sem novo render real. A proposta para 5,5 kHz para em -4,25 dB.",
        "- Nao usar Dynamics, Clarity ou Enhance Speech nesta cadeia; eles podem alterar frase/volume de modo dependente do conteudo.",
        "- EQ estatico aproxima timbre; nao substitui a separacao neural vocal/background do CapCut.",
        "- Hard Limiter deve ser testado depois do EQ, com teto -0,30 dB, sem usar o limiter para corrigir timbre.",
        "",
        "## Arquivos",
        "",
        "- `diagnostico.json`: metricas completas e hashes dos 30 arquivos efetivamente lidos; matrizes FFT internas nao sao publicadas.",
        "- `RESIDUO_POR_CLIP.csv`: residual, fundo e S por clipe.",
        "- `RESUMO_BANDAS.csv`: robustez/universalidade por banda.",
        "- `gerar_diagnostico.py`: auditor reproduzivel com bloqueio do holdout.",
    ]
    (HERE / "RELATORIO.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(HERE / "RELATORIO.md"),
        "baseline_core_median_db": float(np.median(baseline_core)),
        "chosen": {key: value for key, value in chosen.items() if key != "per_clip"},
        "universal_bands": universal_rows,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
