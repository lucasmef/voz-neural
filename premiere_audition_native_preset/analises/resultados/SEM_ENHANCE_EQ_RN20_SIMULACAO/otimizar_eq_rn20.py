#!/usr/bin/env python3
"""Busca offline de EQ estatico para o RN20_DE0, sem tocar em holdout.

O programa somente imprime JSON. Os artefatos de decisao sao consolidados
manualmente, depois da leitura do stdout, para que a busca nao altere projetos,
presets nem os WAVs congelados.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[4]
SUITE = (
    ROOT
    / "premiere_audition_native_preset"
    / "comparacao_ab"
    / "FLUXO_APROVADO_20_20260814"
)
COMPARATOR = (
    ROOT
    / "premiere_audition_native_preset"
    / "analises"
    / "comparar_wavs_adobe.py"
)
RN20_SPLIT = (
    SUITE
    / "resultados_sem_enhance"
    / "RN20_DE0_SENTINELS_v1"
    / "candidatos_split"
    / "RN20_DE0"
)
TARGETS = SUITE / "targets"

# Lista fechada: nenhum arquivo fora desses dez IDs e aberto pelo programa.
CAL_IDS = ("66", "68", "73", "75", "76", "82", "84", "94", "95", "107")
RATE = 48_000
N_FFT = 8192
HOP = 2048
EPS = 1.0e-20

# 1/6 de oitava entre 80 Hz e 8 kHz.
CENTERS = np.geomspace(80.0, 8000.0, 41)

# Forma normalizada do Voice Focus, ancorada perto de 500 Hz. E usada apenas
# como regularizador fraco; os dez pares reais dominam a decisao.
PRIOR_FREQ = np.asarray(
    [250.0, 500.0, 800.0, 1000.0, 2000.0, 3000.0, 4000.0, 6300.0, 8000.0],
    dtype=np.float64,
)
PRIOR_DB = np.asarray(
    [1.88, 0.49, -1.76, -2.22, -3.01, -4.43, -6.18, -7.43, -6.29],
    dtype=np.float64,
)


def load_comparator():
    spec = importlib.util.spec_from_file_location("comparar_wavs_adobe", COMPARATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("nao foi possivel carregar o comparador")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CMP = load_comparator()


def active_spectra(candidate_path: Path, target_path: Path):
    candidate_wav = CMP.read_pcm_wav(candidate_path)
    target_wav = CMP.read_pcm_wav(target_path)
    candidate = CMP.mono_mean(candidate_wav.samples)
    target = CMP.mono_mean(target_wav.samples)
    if candidate_wav.sample_rate != RATE:
        candidate = CMP.sinc_resample(candidate, candidate_wav.sample_rate, RATE)
    if target_wav.sample_rate != RATE:
        target = CMP.sinc_resample(target, target_wav.sample_rate, RATE)

    lag, alignment_corr = CMP.best_placement_lag(target, candidate, RATE)
    start = max(0, lag)
    stop = min(target.size, lag + candidate.size)
    if stop - start < RATE:
        raise RuntimeError(f"sobreposicao insuficiente: {candidate_path}")
    target = target[start:stop]
    candidate = candidate[start - lag : stop - lag]

    masks, selection = CMP.selection_masks(
        candidate,
        RATE,
        frame_ms=20.0,
        edge_ms=250.0,
        quiet_quantile=0.20,
        active_quantile=0.60,
    )
    active = CMP.samples_from_frame_mask(
        masks["active"], int(selection["frame_samples"]), candidate.size
    )
    pc, wc = CMP.accumulated_spectrum(
        candidate, active, n_fft=N_FFT, hop=HOP, minimum_mask_fraction=0.50
    )
    pt, wt = CMP.accumulated_spectrum(
        target, active, n_fft=N_FFT, hop=HOP, minimum_mask_fraction=0.50
    )
    return pc, pt, wc, wt, lag, alignment_corr, selection


def band_energies(power: np.ndarray) -> np.ndarray:
    frequencies = np.fft.rfftfreq(N_FFT, 1.0 / RATE)
    ratio = 2.0 ** (1.0 / 12.0)
    values = []
    for center in CENTERS:
        mask = (frequencies >= center / ratio) & (frequencies < center * ratio)
        values.append(float(np.sum(power[mask])) if np.any(mask) else EPS)
    return np.maximum(np.asarray(values, dtype=np.float64), EPS)


def anchor_mask() -> np.ndarray:
    return (CENTERS >= 500.0) & (CENTERS <= 1000.0)


def analysis_weights() -> np.ndarray:
    f = CENTERS
    w = np.ones_like(f)
    w[(f >= 80) & (f < 200)] = 0.60
    w[(f >= 200) & (f < 500)] = 1.50
    w[(f >= 500) & (f < 1000)] = 1.10
    w[(f >= 1000) & (f < 2000)] = 1.30
    w[(f >= 2000) & (f < 4000)] = 1.20
    w[(f >= 4000) & (f <= 8000)] = 1.60
    return w


def weighted_center(values: np.ndarray, mask: np.ndarray) -> float:
    return float(np.median(np.asarray(values)[mask]))


def normalized_curve(values: np.ndarray) -> np.ndarray:
    return np.asarray(values, dtype=np.float64) - weighted_center(values, anchor_mask())


def rbj_peaking(freq: np.ndarray, f0: float, gain_db: float, q: float) -> np.ndarray:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / RATE
    alpha = math.sin(w0) / (2.0 * q)
    b = np.asarray([1.0 + alpha * a, -2.0 * math.cos(w0), 1.0 - alpha * a])
    aa = np.asarray([1.0 + alpha / a, -2.0 * math.cos(w0), 1.0 - alpha / a])
    return biquad_magnitude_db(freq, b, aa)


def rbj_low_shelf(freq: np.ndarray, f0: float, gain_db: float, slope: float) -> np.ndarray:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / RATE
    cosw = math.cos(w0)
    sinw = math.sin(w0)
    alpha = sinw / 2.0 * math.sqrt((a + 1.0 / a) * (1.0 / slope - 1.0) + 2.0)
    beta = 2.0 * math.sqrt(a) * alpha
    b = np.asarray(
        [
            a * ((a + 1.0) - (a - 1.0) * cosw + beta),
            2.0 * a * ((a - 1.0) - (a + 1.0) * cosw),
            a * ((a + 1.0) - (a - 1.0) * cosw - beta),
        ]
    )
    aa = np.asarray(
        [
            (a + 1.0) + (a - 1.0) * cosw + beta,
            -2.0 * ((a - 1.0) + (a + 1.0) * cosw),
            (a + 1.0) + (a - 1.0) * cosw - beta,
        ]
    )
    return biquad_magnitude_db(freq, b, aa)


def biquad_magnitude_db(freq: np.ndarray, b: np.ndarray, a: np.ndarray) -> np.ndarray:
    w = 2.0 * math.pi * np.asarray(freq, dtype=np.float64) / RATE
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    h = (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)
    return 20.0 * np.log10(np.maximum(np.abs(h), 1.0e-12))


def butterworth_highpass_db(freq: np.ndarray, f0: float, order: int = 4) -> np.ndarray:
    # Magnitude Butterworth de ordem 4 = 24 dB/oitava; suficiente para a busca
    # da curva. O render final sera o HP nativo do Premiere.
    f = np.maximum(np.asarray(freq, dtype=np.float64), 1.0)
    return -10.0 * np.log10(1.0 + (f0 / f) ** (2 * order))


def response(params: dict[str, float]) -> np.ndarray:
    return response_at_frequencies(CENTERS, params)


def response_at_frequencies(freq: np.ndarray, params: dict[str, float]) -> np.ndarray:
    h = butterworth_highpass_db(freq, params["hp_freq"], 4)
    h += rbj_low_shelf(
        freq,
        params["ls_freq"],
        params["ls_gain"],
        params.get("ls_slope", 0.80),
    )
    for index in range(1, 5):
        h += rbj_peaking(
            freq,
            params[f"b{index}_freq"],
            params[f"b{index}_gain"],
            params[f"b{index}_q"],
        )
    return h


def frame_gain_summary(candidate_path: Path, params: dict[str, float]) -> dict[str, float | int]:
    """Estime quanto o EQ fixo muda o nivel de janelas de voz.

    Nao modela fase/transientes do IIR nativo; mede apenas a variacao de energia
    causada pela curva tonal. Isso detecta curvas que mudariam demais o envelope
    conforme o conteudo fonetico, mesmo sem qualquer ganho adaptativo.
    """

    wav = CMP.read_pcm_wav(candidate_path)
    audio = CMP.mono_mean(wav.samples)
    if wav.sample_rate != RATE:
        audio = CMP.sinc_resample(audio, wav.sample_rate, RATE)
    n_fft = 4096
    hop = 2048
    starts = np.arange(0, max(0, audio.size - n_fft + 1), hop, dtype=np.int64)
    if starts.size < 4:
        return {"windows": 0}
    window = np.hanning(n_fft)
    frame_rms_values = np.asarray(
        [math.sqrt(float(np.mean(audio[start : start + n_fft].astype(np.float64) ** 2)) + EPS) for start in starts]
    )
    active = starts[frame_rms_values >= np.quantile(frame_rms_values, 0.60)]
    freq = np.fft.rfftfreq(n_fft, 1.0 / RATE)
    power_multiplier = 10.0 ** (response_at_frequencies(freq, params) / 10.0)
    gains = []
    for start in active:
        spectrum = np.fft.rfft(audio[start : start + n_fft] * window)
        power = np.abs(spectrum) ** 2
        gains.append(10.0 * math.log10(max(float(np.sum(power * power_multiplier)), EPS) / max(float(np.sum(power)), EPS)))
    values = np.asarray(gains, dtype=np.float64)
    median = float(np.median(values))
    return {
        "windows": int(values.size),
        "median_gain_db": median,
        "p10_gain_db": float(np.quantile(values, 0.10)),
        "p90_gain_db": float(np.quantile(values, 0.90)),
        "p10_p90_spread_db": float(np.quantile(values, 0.90) - np.quantile(values, 0.10)),
        "p95_abs_deviation_from_median_db": float(np.quantile(np.abs(values - median), 0.95)),
    }


def objective(params: dict[str, float], residuals: np.ndarray, prior_strength: float) -> float:
    h = response(params)
    weights = analysis_weights()
    errors = []
    for residual in residuals:
        after = normalized_curve(residual + h)
        # Huber suave: MAE robusto com pequena penalidade quadratica.
        abs_error = np.abs(after)
        huber = np.where(abs_error <= 2.0, 0.5 * abs_error**2, 2.0 * abs_error - 2.0)
        errors.append(float(np.average(huber, weights=weights)))
    data_loss = float(np.median(errors)) + 0.20 * float(np.mean(errors))

    prior = np.interp(CENTERS, PRIOR_FREQ, PRIOR_DB, left=PRIOR_DB[0], right=PRIOR_DB[-1])
    prior = normalized_curve(prior)
    h_norm = normalized_curve(h)
    prior_loss = float(np.average(np.abs(h_norm - prior), weights=weights))
    regularity = 0.025 * sum(abs(params[f"b{i}_gain"]) for i in range(1, 5))
    regularity += 0.020 * abs(params["ls_gain"])
    return data_loss + prior_strength * prior_loss + regularity


def coordinate_search(residuals: np.ndarray, prior_strength: float, conservative: bool):
    if conservative:
        params = {
            "hp_freq": 90.0,
            "ls_freq": 250.0,
            "ls_gain": 1.8,
            "b1_freq": 800.0,
            "b1_gain": -0.7,
            "b1_q": 0.80,
            "b2_freq": 1600.0,
            "b2_gain": -1.6,
            "b2_q": 1.00,
            "b3_freq": 3200.0,
            "b3_gain": -2.2,
            "b3_q": 0.80,
            "b4_freq": 6200.0,
            "b4_gain": -3.5,
            "b4_q": 0.80,
        }
        gain_bounds = {"ls_gain": (0.0, 2.6), **{f"b{i}_gain": (-4.2, 0.5) for i in range(1, 5)}}
        gain_bounds["b4_gain"] = (-5.0, 0.0)
    else:
        params = {
            "hp_freq": 90.0,
            "ls_freq": 250.0,
            "ls_gain": 2.2,
            "b1_freq": 900.0,
            "b1_gain": -1.2,
            "b1_q": 0.80,
            "b2_freq": 1900.0,
            "b2_gain": -2.2,
            "b2_q": 0.90,
            "b3_freq": 3500.0,
            "b3_gain": -3.0,
            "b3_q": 0.80,
            "b4_freq": 6200.0,
            "b4_gain": -4.5,
            "b4_q": 0.75,
        }
        gain_bounds = {"ls_gain": (0.0, 3.5), **{f"b{i}_gain": (-6.5, 1.0) for i in range(1, 5)}}
        gain_bounds["b4_gain"] = (-7.5, 0.0)

    grids = {
        "hp_freq": [80.0, 85.0, 90.0, 95.0, 100.0],
        "ls_freq": [220.0, 250.0, 280.0, 315.0],
        "b1_freq": [700.0, 800.0, 900.0, 1000.0, 1100.0],
        "b2_freq": [1400.0, 1600.0, 1800.0, 2000.0, 2400.0],
        "b3_freq": [2800.0, 3200.0, 3500.0, 4000.0],
        "b4_freq": [5000.0, 5600.0, 6200.0, 6800.0],
        "b1_q": [0.65, 0.80, 1.00],
        "b2_q": [0.65, 0.80, 1.00],
        "b3_q": [0.65, 0.80, 1.00],
        "b4_q": [0.60, 0.75, 0.90, 1.10],
    }

    score = objective(params, residuals, prior_strength)
    for gain_step in (0.50, 0.25, 0.10):
        for _ in range(4):
            changed = False
            # Discretos: um passe por frequencias/Q a cada refinamento.
            for key, values in grids.items():
                old = params[key]
                best_value = old
                best_score = score
                for value in values:
                    params[key] = value
                    trial = objective(params, residuals, prior_strength)
                    if trial + 1.0e-9 < best_score:
                        best_score = trial
                        best_value = value
                params[key] = best_value
                if best_value != old:
                    changed = True
                    score = best_score

            for key, (low, high) in gain_bounds.items():
                old = params[key]
                best_value = old
                best_score = score
                candidates = np.arange(low, high + gain_step * 0.25, gain_step)
                for value in candidates:
                    params[key] = float(value)
                    trial = objective(params, residuals, prior_strength)
                    if trial + 1.0e-9 < best_score:
                        best_score = trial
                        best_value = float(value)
                params[key] = best_value
                if abs(best_value - old) > 1.0e-9:
                    changed = True
                    score = best_score
            if not changed:
                break
    return params, score


def band_summary(curve: np.ndarray) -> dict[str, float]:
    ranges = {
        "low_80_200": (80.0, 200.0),
        "body_200_500": (200.0, 500.0),
        "mid_500_1000": (500.0, 1000.0),
        "presence_1k_2k": (1000.0, 2000.0),
        "clarity_2k_4k": (2000.0, 4000.0),
        "sibilance_4k_8k": (4000.0, 8000.1),
    }
    return {
        name: float(np.median(curve[(CENTERS >= low) & (CENTERS < high)]))
        for name, (low, high) in ranges.items()
    }


def metrics_for(residual: np.ndarray, h: np.ndarray) -> dict[str, object]:
    after = normalized_curve(residual + h)
    weights = analysis_weights()
    return {
        "weighted_mae_80_8k_db": float(np.average(np.abs(after), weights=weights)),
        "rmse_80_8k_db": float(np.sqrt(np.average(after**2, weights=weights))),
        "bands_candidate_minus_target_db": band_summary(after),
    }


def aggregate(rows: list[dict[str, object]]) -> dict[str, object]:
    mae = np.asarray([row["weighted_mae_80_8k_db"] for row in rows], dtype=float)
    rmse = np.asarray([row["rmse_80_8k_db"] for row in rows], dtype=float)
    band_names = tuple(rows[0]["bands_candidate_minus_target_db"].keys())
    bands = {
        name: {
            "median_db": float(np.median([row["bands_candidate_minus_target_db"][name] for row in rows])),
            "p90_abs_db": float(np.quantile(np.abs([row["bands_candidate_minus_target_db"][name] for row in rows]), 0.90)),
        }
        for name in band_names
    }
    return {
        "weighted_mae_median_db": float(np.median(mae)),
        "weighted_mae_mean_db": float(np.mean(mae)),
        "weighted_mae_p90_db": float(np.quantile(mae, 0.90)),
        "rmse_median_db": float(np.median(rmse)),
        "bands": bands,
    }


def main():
    observations = []
    audit = {}
    for clip_id in CAL_IDS:
        candidate_path = RN20_SPLIT / f"{clip_id}.wav"
        target_path = TARGETS / f"{clip_id}_CAPCUT.wav"
        pc, pt, wc, wt, lag, corr, selection = active_spectra(candidate_path, target_path)
        residual = 10.0 * np.log10(band_energies(pc) / band_energies(pt))
        residual = normalized_curve(residual)
        observations.append(residual)
        audit[clip_id] = {
            "candidate": str(candidate_path),
            "target": str(target_path),
            "active_windows_candidate": wc,
            "active_windows_target": wt,
            "alignment_lag_samples_at_48k": lag,
            "alignment_preemphasis_correlation": corr,
            "selection": selection,
            "baseline_normalized_bands_db": band_summary(residual),
        }

    residuals = np.vstack(observations)
    baseline_h = np.zeros_like(CENTERS)
    candidates = {}
    for name, prior_strength, conservative in (
        ("C1_ROBUSTO", 0.10, True),
        ("C2_FOCO_FORTE", 0.20, False),
    ):
        params, score = coordinate_search(residuals, prior_strength, conservative)
        h = response(params)
        per_id = {
            clip_id: metrics_for(residuals[index], h)
            for index, clip_id in enumerate(CAL_IDS)
        }
        for clip_id in CAL_IDS:
            per_id[clip_id]["estimated_active_frame_gain"] = frame_gain_summary(
                RN20_SPLIT / f"{clip_id}.wav", params
            )
        spreads = np.asarray(
            [per_id[clip_id]["estimated_active_frame_gain"]["p10_p90_spread_db"] for clip_id in CAL_IDS],
            dtype=np.float64,
        )
        deviations = np.asarray(
            [per_id[clip_id]["estimated_active_frame_gain"]["p95_abs_deviation_from_median_db"] for clip_id in CAL_IDS],
            dtype=np.float64,
        )
        candidates[name] = {
            "params": params,
            "objective": score,
            "response_at_centers_db": {
                f"{freq:.2f}": float(value) for freq, value in zip(CENTERS, h)
            },
            "normalized_response_bands_db": band_summary(normalized_curve(h)),
            "per_id": per_id,
            "aggregate": aggregate(list(per_id.values())),
            "aggregate_envelope_proxy": {
                "p10_p90_spread_median_db": float(np.median(spreads)),
                "p10_p90_spread_p90_db": float(np.quantile(spreads, 0.90)),
                "p95_abs_deviation_median_db": float(np.median(deviations)),
                "p95_abs_deviation_p90_db": float(np.quantile(deviations, 0.90)),
                "interpretation": "variacao tonal por fonema; o EQ continua fixo e sem ganho dependente de nivel",
            },
        }

    baseline_per_id = {
        clip_id: metrics_for(residuals[index], baseline_h)
        for index, clip_id in enumerate(CAL_IDS)
    }
    output = {
        "schema": 1,
        "scope": {
            "calibration_ids_only": list(CAL_IDS),
            "holdout_opened": False,
            "candidate_source": str(RN20_SPLIT),
            "target_source": str(TARGETS),
        },
        "method": {
            "rate": RATE,
            "fft": N_FFT,
            "hop": HOP,
            "active_selection": "top 40% de RMS do RN20, bordas de 250 ms excluidas",
            "normalization": "mediana espectral 500-1000 Hz por par",
            "objective": "Huber robusto, pesos de corpo e sibilancia, prior Voice Focus fraco",
            "simulation": "soma da resposta de magnitude do EQ ao residuo espectral; sem dinamica",
        },
        "audit": audit,
        "baseline": {
            "per_id": baseline_per_id,
            "aggregate": aggregate(list(baseline_per_id.values())),
        },
        "candidates": candidates,
    }
    if "--compact" in sys.argv:
        output = {
            "scope": output["scope"],
            "method": output["method"],
            "baseline": output["baseline"],
            "candidates": {
                name: {
                    "params": item["params"],
                    "objective": item["objective"],
                    "normalized_response_bands_db": item["normalized_response_bands_db"],
                    "aggregate": item["aggregate"],
                    "aggregate_envelope_proxy": item["aggregate_envelope_proxy"],
                    "per_id": {
                        clip_id: {
                            "weighted_mae_80_8k_db": row["weighted_mae_80_8k_db"],
                            "estimated_active_frame_gain": row["estimated_active_frame_gain"],
                        }
                        for clip_id, row in item["per_id"].items()
                    },
                }
                for name, item in output["candidates"].items()
            },
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
