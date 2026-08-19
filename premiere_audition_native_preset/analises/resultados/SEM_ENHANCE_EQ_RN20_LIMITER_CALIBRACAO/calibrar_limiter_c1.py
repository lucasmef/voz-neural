#!/usr/bin/env python3
"""Calibre o Input Boost do Hard Limiter C1 sem abrir o holdout.

A simulacao reconstrói o sinal pre-limiter a partir do render RN20 e da curva
C1, e modela o Hard Limiter ligado em estéreo com teto -0,30 dBFS, lookahead
5 ms e release 200 ms. As mudancas de nivel sao ancoradas no render Premiere
real de +8,1 dB; assim, pequenos erros do modelo de EQ/limiter se cancelam.

O programa somente lê os dez IDs de calibracao explicitamente enumerados e
imprime JSON. Nao lista diretorios, nao abre o holdout e nao altera projetos.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[4]
ANALYSES = ROOT / "premiere_audition_native_preset" / "analises"
sys.path.insert(0, str(ANALYSES))
import comparar_wavs_adobe as audio  # noqa: E402


BASE = (
    ROOT
    / "premiere_audition_native_preset"
    / "comparacao_ab"
    / "FLUXO_APROVADO_20_20260814"
    / "resultados_sem_enhance"
    / "RN20_C1_EQ_LIMITER_CALIBRATION_REAL_v1"
)
SPLIT = BASE / "candidatos_split"
ANALYSIS = BASE / "analises" / "C1_EQ_LIM"

CALIBRATION = ("66", "68", "73", "75", "76", "82", "84", "94", "95", "107")
HOLDOUT = {"64", "65", "67", "69", "71", "72", "74", "81", "92", "101"}
GAINS_DB = (8.1, 8.6, 8.9, 9.2, 9.5)
REFERENCE_GAIN_DB = 8.1
RATE = 48_000
CEILING_DBFS = -0.30
CEILING = 10.0 ** (CEILING_DBFS / 20.0)
BLOCK = 48  # 1 ms
LOOKAHEAD_BLOCKS = 5
RELEASE_SECONDS = 0.200
EPS = 1.0e-20

if set(CALIBRATION) & HOLDOUT:
    raise RuntimeError("falha de seguranca: holdout entrou na calibracao")


def _biquad_h(
    frequencies: np.ndarray, b: tuple[float, float, float], a: tuple[float, float, float]
) -> np.ndarray:
    w = 2.0 * np.pi * frequencies / RATE
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    return (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)


def _peaking(frequencies: np.ndarray, f0: float, gain_db: float, q: float) -> np.ndarray:
    amp = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / RATE
    alpha = math.sin(w0) / (2.0 * q)
    cosine = math.cos(w0)
    return _biquad_h(
        frequencies,
        (1.0 + alpha * amp, -2.0 * cosine, 1.0 - alpha * amp),
        (1.0 + alpha / amp, -2.0 * cosine, 1.0 - alpha / amp),
    )


def _low_shelf(frequencies: np.ndarray, f0: float, gain_db: float) -> np.ndarray:
    amp = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / RATE
    cosine = math.cos(w0)
    sine = math.sin(w0)
    alpha = sine / 2.0 * math.sqrt(2.0)  # shelf nativo, S=1
    beta = 2.0 * math.sqrt(amp) * alpha
    b = (
        amp * ((amp + 1.0) - (amp - 1.0) * cosine + beta),
        2.0 * amp * ((amp - 1.0) - (amp + 1.0) * cosine),
        amp * ((amp + 1.0) - (amp - 1.0) * cosine - beta),
    )
    a = (
        (amp + 1.0) + (amp - 1.0) * cosine + beta,
        -2.0 * ((amp - 1.0) + (amp + 1.0) * cosine),
        (amp + 1.0) + (amp - 1.0) * cosine - beta,
    )
    return _biquad_h(frequencies, b, a)


def _high_pass_24(frequencies: np.ndarray) -> np.ndarray:
    result = np.ones_like(frequencies, dtype=np.complex128)
    for q in (0.5411961001, 1.3065629649):
        w0 = 2.0 * math.pi * 100.0 / RATE
        cosine = math.cos(w0)
        alpha = math.sin(w0) / (2.0 * q)
        result *= _biquad_h(
            frequencies,
            ((1.0 + cosine) / 2.0, -(1.0 + cosine), (1.0 + cosine) / 2.0),
            (1.0 + alpha, -2.0 * cosine, 1.0 - alpha),
        )
    return result


def apply_c1_eq(samples: np.ndarray) -> np.ndarray:
    size = samples.size
    fft_size = 1 << (size + 8191).bit_length()
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / RATE)
    response = _high_pass_24(frequencies)
    response *= _low_shelf(frequencies, 315.0, 2.1)
    response *= _peaking(frequencies, 1000.0, -0.7, 1.0)
    response *= _peaking(frequencies, 1400.0, -1.6, 1.0)
    response *= _peaking(frequencies, 4000.0, -2.2, 0.65)
    response *= _peaking(frequencies, 6800.0, -4.0, 0.75)
    return np.fft.irfft(np.fft.rfft(samples, fft_size) * response, fft_size)[:size]


def hard_limiter(pre_limiter: np.ndarray, input_gain_db: float) -> tuple[np.ndarray, dict[str, float]]:
    boosted = pre_limiter * 10.0 ** (input_gain_db / 20.0)
    usable = (boosted.size // BLOCK) * BLOCK
    block_peaks = np.max(np.abs(boosted[:usable].reshape(-1, BLOCK)), axis=1)
    required_db = np.maximum(0.0, 20.0 * np.log10(np.maximum(block_peaks, EPS) / CEILING))

    padded = np.pad(required_db, (0, LOOKAHEAD_BLOCKS))
    lookahead_db = np.maximum.reduce(
        [padded[offset : offset + required_db.size] for offset in range(LOOKAHEAD_BLOCKS + 1)]
    )
    release_decay = math.exp(-(BLOCK / RATE) / RELEASE_SECONDS)
    applied_db = np.empty_like(lookahead_db)
    state = 0.0
    for index, demand in enumerate(lookahead_db):
        state = float(demand) if demand > state else state * release_decay
        applied_db[index] = state

    result = boosted.copy()
    result[:usable] *= np.repeat(10.0 ** (-applied_db / 20.0), BLOCK)
    return result, {
        "trigger_blocks_fraction": float(np.mean(required_db > 0.0)),
        "gain_reduction_gt_0_1db_fraction": float(np.mean(applied_db > 0.1)),
        "gain_reduction_median_db": float(np.median(applied_db)),
        "gain_reduction_p95_db": float(np.quantile(applied_db, 0.95)),
        "gain_reduction_max_db": float(np.max(applied_db)),
    }


def rms_db(values: np.ndarray) -> float:
    return 20.0 * math.log10(math.sqrt(float(np.mean(values * values))) + EPS)


def frame_levels(values: np.ndarray) -> np.ndarray:
    usable = (values.size // 960) * 960
    frames = values[:usable].reshape(-1, 960)
    return 20.0 * np.log10(np.sqrt(np.mean(frames * frames, axis=1)) + EPS)


def integrated_lufs(values: np.ndarray) -> float:
    weighted = audio.k_weight(values, RATE)
    result = audio.loudness_from_weighted(weighted[:, None])["integrated_lufs"]
    if result is None:
        raise RuntimeError("loudness integrada ausente")
    return float(result)


def load_real_metrics(clip_id: str) -> dict[str, float]:
    path = ANALYSIS / clip_id / f"{clip_id}_C1_EQ_LIM_COMPARACAO.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    comparison = data["comparisons"]["candidate_vs_target"]
    production = comparison["production_level"]
    active = comparison["active"]
    return {
        "voice_delta_db": float(active["candidate_minus_reference_rms_db"]),
        "loudness_delta_lu": float(production["candidate_minus_reference_lu"]),
        "crest_candidate_db": float(production["crest_candidate_db"]),
        "crest_target_db": float(production["crest_reference_db"]),
    }


def main() -> None:
    per_clip: dict[str, dict[str, object]] = {}
    for clip_id in CALIBRATION:
        rn_path = SPLIT / "RN20_DE0" / f"{clip_id}.wav"
        real_path = SPLIT / "C1_EQ_LIM" / f"{clip_id}.wav"
        rn = audio.read_pcm_wav(rn_path)
        real = audio.read_pcm_wav(real_path)
        if rn.sample_rate != RATE or real.sample_rate != RATE:
            raise RuntimeError(f"taxa inesperada no ID {clip_id}")
        rn_mono = audio.mono_mean(rn.samples)
        real_mono = audio.mono_mean(real.samples)
        pre_limiter = apply_c1_eq(rn_mono)
        real_metrics = load_real_metrics(clip_id)

        modeled: dict[str, tuple[np.ndarray, dict[str, float]]] = {}
        for gain_db in GAINS_DB:
            modeled[f"{gain_db:.1f}"] = hard_limiter(pre_limiter, gain_db)

        reference, reference_limiter = modeled[f"{REFERENCE_GAIN_DB:.1f}"]
        ref_levels = frame_levels(reference)
        real_levels = frame_levels(real_mono)
        active = real_levels >= np.quantile(real_levels, 0.60)
        ref_active_rms = rms_db(reference[: active.size * 960].reshape(-1, 960)[active])
        real_active_rms = rms_db(real_mono[: active.size * 960].reshape(-1, 960)[active])
        ref_lufs = integrated_lufs(reference)
        real_lufs = integrated_lufs(real_mono)
        ref_crest = 20.0 * math.log10(np.max(np.abs(reference)) + EPS) - rms_db(reference)

        candidates: dict[str, dict[str, float]] = {}
        for gain_key, (candidate, limiter_metrics) in modeled.items():
            levels = frame_levels(candidate)
            common = min(levels.size, ref_levels.size)
            active_common = active[:common]
            raw_delta = levels[:common] - ref_levels[:common]
            centered = raw_delta - float(np.median(raw_delta[active_common]))
            candidate_active_rms = rms_db(
                candidate[: active.size * 960].reshape(-1, 960)[active]
            )
            lufs = integrated_lufs(candidate)
            crest = 20.0 * math.log10(np.max(np.abs(candidate)) + EPS) - rms_db(candidate)
            candidates[gain_key] = {
                "predicted_voice_delta_vs_capcut_db": real_metrics["voice_delta_db"]
                + candidate_active_rms
                - ref_active_rms,
                "predicted_loudness_delta_vs_capcut_lu": real_metrics["loudness_delta_lu"]
                + lufs
                - ref_lufs,
                "predicted_crest_delta_vs_capcut_db": real_metrics["crest_candidate_db"]
                + crest
                - ref_crest
                - real_metrics["crest_target_db"],
                "added_envelope_correlation_vs_8_1": float(
                    np.corrcoef(levels[:common][active_common], ref_levels[:common][active_common])[0, 1]
                ),
                "added_envelope_p95_abs_db": float(
                    np.quantile(np.abs(centered[active_common]), 0.95)
                ),
                "added_envelope_max_abs_db": float(np.max(np.abs(centered[active_common]))),
                **limiter_metrics,
            }

        per_clip[clip_id] = {
            "validation_at_8_1": {
                "waveform_correlation_model_vs_real": float(np.corrcoef(reference, real_mono)[0, 1]),
                "active_rms_model_minus_real_db": ref_active_rms - real_active_rms,
                "integrated_lufs_model_minus_real_lu": ref_lufs - real_lufs,
                **reference_limiter,
            },
            "gains": candidates,
        }

    aggregates: dict[str, dict[str, float]] = {}
    for gain_db in GAINS_DB:
        key = f"{gain_db:.1f}"
        rows = [per_clip[clip_id]["gains"][key] for clip_id in CALIBRATION]
        voice = np.asarray([row["predicted_voice_delta_vs_capcut_db"] for row in rows])
        loudness = np.asarray([row["predicted_loudness_delta_vs_capcut_lu"] for row in rows])
        crest = np.asarray([row["predicted_crest_delta_vs_capcut_db"] for row in rows])
        aggregates[key] = {
            "voice_delta_median_db": float(np.median(voice)),
            "voice_abs_median_db": float(np.median(np.abs(voice))),
            "voice_abs_max_db": float(np.max(np.abs(voice))),
            "loudness_delta_median_lu": float(np.median(loudness)),
            "loudness_abs_median_lu": float(np.median(np.abs(loudness))),
            "loudness_abs_max_lu": float(np.max(np.abs(loudness))),
            "crest_delta_median_db": float(np.median(crest)),
            "crest_abs_median_db": float(np.median(np.abs(crest))),
            "envelope_correlation_median": float(
                np.median([row["added_envelope_correlation_vs_8_1"] for row in rows])
            ),
            "envelope_p95_abs_median_db": float(
                np.median([row["added_envelope_p95_abs_db"] for row in rows])
            ),
            "envelope_max_abs_worst_db": float(
                np.max([row["added_envelope_max_abs_db"] for row in rows])
            ),
            "limiter_trigger_fraction_median": float(
                np.median([row["trigger_blocks_fraction"] for row in rows])
            ),
            "gain_reduction_gt_0_1db_fraction_median": float(
                np.median([row["gain_reduction_gt_0_1db_fraction"] for row in rows])
            ),
            "gain_reduction_p95_median_db": float(
                np.median([row["gain_reduction_p95_db"] for row in rows])
            ),
            "gain_reduction_max_worst_db": float(
                np.max([row["gain_reduction_max_db"] for row in rows])
            ),
        }

    validations = [per_clip[clip_id]["validation_at_8_1"] for clip_id in CALIBRATION]
    result = {
        "schema_version": 1,
        "scope": "calibration_only",
        "calibration_ids": list(CALIBRATION),
        "holdout_opened": False,
        "holdout_ids": sorted(HOLDOUT),
        "model": {
            "eq": "C1: HP100/24, LS315 +2.1, B1 1k -0.7 Q1, B2 1.4k -1.6 Q1, B3 4k -2.2 Q0.65, B4 6.8k -4 Q0.75",
            "limiter": "stereo-linked block-peak approximation; ceiling -0.30 dBFS; lookahead 5 ms; exponential dB release 200 ms",
            "anchor": "all predicted deltas are changes from modeled +8.1 added to measured Premiere-real +8.1 deltas",
        },
        "validation_at_8_1": {
            "waveform_correlation_median": float(
                np.median([row["waveform_correlation_model_vs_real"] for row in validations])
            ),
            "waveform_correlation_min": float(
                np.min([row["waveform_correlation_model_vs_real"] for row in validations])
            ),
            "active_rms_error_abs_median_db": float(
                np.median(np.abs([row["active_rms_model_minus_real_db"] for row in validations]))
            ),
            "active_rms_error_abs_max_db": float(
                np.max(np.abs([row["active_rms_model_minus_real_db"] for row in validations]))
            ),
            "integrated_loudness_error_abs_median_lu": float(
                np.median(np.abs([row["integrated_lufs_model_minus_real_lu"] for row in validations]))
            ),
            "integrated_loudness_error_abs_max_lu": float(
                np.max(np.abs([row["integrated_lufs_model_minus_real_lu"] for row in validations]))
            ),
        },
        "aggregates": aggregates,
        "per_clip": per_clip,
        "recommendation": {
            "single_real_render_input_gain_db": 9.2,
            "reason": "best balance: it removes the median ~1.1 dB deficit while adding less limiting and envelope change than +9.5 dB",
        },
        "limitations": [
            "The Hard Limiter is an approximation at 1 ms resolution, not Adobe DSP code.",
            "The EQ is reconstructed by equivalent biquads/FFT; phase and internal coefficient rounding can differ slightly.",
            "True-peak behavior is not simulated because True Peak Limiting is off in the approved preset.",
            "Only one Premiere-real render at the recommended gain can validate the exact result.",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
