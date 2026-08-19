#!/usr/bin/env python3
"""Compare a grade Premiere sem Enhance sem contaminar o holdout.

O arquivo de entrada de cada variante deve ser o concat completo dos mesmos
20 clips do conjunto congelado ``FLUXO_APROVADO_20_20260814``. O programa:

* exige PCM24, estereo, 48 kHz e exatamente 25.181.516 frames;
* divide cada concat de forma sample-exact e prova a ordem dos 20 segmentos;
* analisa primeiro somente os seis sentinelas, na ordem 66/73/76/82/95/107;
* opcionalmente expande para os quatro clips de calibracao 68/75/84/94;
* nao possui modo de analise do holdout; ele continua fechado ate o freeze;
* compara cada variante ao CapCut e, temporalmente, ao baseline informado;
* publica tudo por rename atomico e nunca sobrescreve uma execucao anterior.

Nenhum projeto/preset Adobe e lido ou alterado. Os unicos arquivos criados
sao copias de trabalho e relatorios dentro da pasta de resultados da analise.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

import comparar_wavs_adobe as audio
import validar_concats_finais as common
import validar_fluxo_aprovado_20 as frozen


ROOT = Path(__file__).resolve().parents[2]
PRESET_ROOT = ROOT / "premiere_audition_native_preset"
BASE = PRESET_ROOT / "comparacao_ab" / "FLUXO_APROVADO_20_20260814"
OUTPUT_ROOT = BASE / "resultados_sem_enhance"

SENTINELS = ("66", "73", "76", "82", "95", "107")
CALIBRATION_EXTENSION = ("68", "75", "84", "94")
CALIBRATION = SENTINELS + CALIBRATION_EXTENSION
HOLDOUT = ("64", "65", "67", "69", "71", "72", "74", "81", "92", "101")

EXPECTED_TOTAL_FRAMES = 25_181_516
EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_CHANNELS = 2
EXPECTED_SAMPLE_WIDTH_BYTES = 3

TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}\Z")

CORE_BANDS = (
    "low_80_200",
    "body_200_500",
    "mid_500_1000",
    "presence_1k_2k",
    "clarity_2k_4k",
    "sibilance_4k_8k",
)
NON_SIBILANT_BANDS = (
    "low_80_200",
    "body_200_500",
    "mid_500_1000",
    "presence_1k_2k",
    "clarity_2k_4k",
)

# Gates da fase que escolhe Reduce Noise/DeEss antes do EQ final.
PHRASE_MAX_ABS_DB = 0.75
WINDOW_200MS_P95_ABS_DB = 0.75
TEMPORAL_ENVELOPE_MIN = 0.995
NON_SIBILANT_SHIFT_MAX_DB = 0.75
CORE_MAE_MEDIAN_REGRESSION_DB = 0.15
CORE_MAE_P90_REGRESSION_DB = 0.25
SIBILANCE_P90_MEDIAN_REGRESSION_DB = 0.25
QUIET_MAE_MEDIAN_REGRESSION_DB = 0.50
BACKGROUND_OVERSUPPRESSION_MIN_DB = -6.0

# Gates adicionais, usados somente com --phase final.
FINAL_CORE_MAE_MEDIAN_DB = 1.25
FINAL_CORE_MAE_P90_DB = 1.75
FINAL_BAND_MEDIAN_ABS_DB = 1.50
FINAL_LOUDNESS_MEDIAN_ABS_LU = 0.50
FINAL_LOUDNESS_MAX_ABS_LU = 1.00
FINAL_VOICE_MEDIAN_ABS_DB = 0.50
FINAL_VOICE_MAX_ABS_DB = 1.00
FINAL_BACKGROUND_MEDIAN_ABS_DB = 3.00
FINAL_SIBILANCE_MEDIAN_ABS_DB = 1.00
FINAL_SAMPLE_PEAK_MAX_DBFS = -0.29
FINAL_TRUE_PEAK_MAX_DBTP = 0.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"metrica numerica ausente: {role}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"metrica nao finita: {role}")
    return number


def nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def percentile(values: Iterable[float], quantile: float, role: str) -> float:
    numbers = np.asarray(list(values), dtype=np.float64)
    if not numbers.size or not np.all(np.isfinite(numbers)):
        raise ValueError(f"valores insuficientes/invalidos para {role}")
    return float(np.quantile(numbers, quantile))


def median(values: Iterable[float], role: str) -> float:
    return percentile(values, 0.50, role)


def parse_candidates(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for raw_value in values:
        label, separator, path_value = raw_value.partition("=")
        if not separator or not LABEL_PATTERN.fullmatch(label):
            raise ValueError(
                "--candidate deve usar LABEL=/caminho/arquivo.wav; "
                "LABEL aceita somente letras, numeros, ponto, hifen e underscore"
            )
        if label in result:
            raise ValueError(f"rotulo de candidato repetido: {label}")
        path = Path(path_value).expanduser()
        if path.is_symlink():
            raise ValueError(f"concat nao pode ser link simbolico: {path}")
        resolved = path.resolve()
        try:
            resolved.relative_to(PRESET_ROOT.resolve())
        except ValueError as error:
            raise ValueError(
                f"concat deve ficar dentro de {PRESET_ROOT.resolve()}: {resolved}"
            ) from error
        if not resolved.is_file():
            raise ValueError(f"concat inexistente: {resolved}")
        result[label] = resolved
    if not result:
        raise ValueError("informe pelo menos um --candidate")
    return result


def inspect_candidates(paths: Mapping[str, Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for label, path in paths.items():
        info = common.inspect_wave(path, f"concat {label}")
        common.require_candidate_format(info, f"concat {label}")
        if info.frames != EXPECTED_TOTAL_FRAMES:
            delta = info.frames - EXPECTED_TOTAL_FRAMES
            raise ValueError(
                f"concat {label} tem {info.frames} frames; esperado "
                f"{EXPECTED_TOTAL_FRAMES} (diferenca {delta:+d})"
            )
        file_hash = common.file_sha256(path)
        payload_hash, actual_frames = common.payload_sha256_and_frames(
            info, f"concat {label}"
        )
        if actual_frames != EXPECTED_TOTAL_FRAMES:
            raise ValueError(f"payload do concat {label} tem contagem divergente")
        if file_hash in hashes:
            raise ValueError(
                f"concats {hashes[file_hash]} e {label} sao byte-identicos; "
                "provavel export repetido/rotulado incorretamente"
            )
        hashes[file_hash] = label
        records[label] = {
            **asdict(info),
            "duration_seconds": info.duration_seconds,
            "file_sha256": file_hash,
            "pcm_payload_sha256": payload_hash,
        }
    return records


def selected_suite(
    full_suite: common.SuiteSpec, selected_ids: Sequence[str]
) -> common.SuiteSpec:
    by_id = {clip.clip_id: clip for clip in full_suite.clips}
    if set(by_id) != set(frozen.CLIP_IDS):
        raise ValueError("suite congelada nao contem exatamente os 20 IDs esperados")
    if set(selected_ids) & set(HOLDOUT):
        raise ValueError("falha de seguranca: holdout entrou na selecao de calibracao")
    return common.SuiteSpec(
        name="sem_enhance_" + "_".join(selected_ids),
        clips=tuple(by_id[clip_id] for clip_id in selected_ids),
        blind=False,
    )


def load_mono(path: Path, rate: int = EXPECTED_SAMPLE_RATE) -> np.ndarray:
    wav = audio.read_pcm_wav(path)
    return audio.sinc_resample(audio.mono_mean(wav.samples), wav.sample_rate, rate)


def rms_from_frames(envelope: np.ndarray, mask: np.ndarray) -> float:
    values = np.asarray(envelope, dtype=np.float64)[mask]
    if not values.size:
        raise ValueError("janela temporal sem frames ativos")
    return float(np.sqrt(np.mean(values * values)))


def gain_db(reference_rms: float, candidate_rms: float) -> float:
    return audio.amp_db(reference_rms / max(candidate_rms, audio.EPS))


def bridged_active_regions(
    active: np.ndarray, maximum_gap_frames: int, minimum_active_frames: int
) -> list[tuple[int, int, np.ndarray]]:
    indices = np.flatnonzero(active)
    if not indices.size:
        return []
    groups: list[list[int]] = [[int(indices[0])]]
    for index in indices[1:]:
        integer = int(index)
        if integer - groups[-1][-1] - 1 <= maximum_gap_frames:
            groups[-1].append(integer)
        else:
            groups.append([integer])
    result = []
    for group in groups:
        if len(group) < minimum_active_frames:
            continue
        start = group[0]
        stop = group[-1] + 1
        local = active[start:stop]
        result.append((start, stop, local))
    return result


def window_deltas(
    baseline_env: np.ndarray,
    candidate_env: np.ndarray,
    active: np.ndarray,
    window_frames: int,
    hop_frames: int,
    minimum_active_frames: int,
) -> list[dict[str, float]]:
    frame_count = min(baseline_env.size, candidate_env.size, active.size)
    rows: list[dict[str, float]] = []
    if frame_count < window_frames:
        return rows
    for start in range(0, frame_count - window_frames + 1, hop_frames):
        stop = start + window_frames
        local_mask = active[start:stop]
        if int(np.count_nonzero(local_mask)) < minimum_active_frames:
            continue
        baseline_rms = rms_from_frames(baseline_env[start:stop], local_mask)
        candidate_rms = rms_from_frames(candidate_env[start:stop], local_mask)
        rows.append(
            {
                "start_seconds": start * 0.020,
                "stop_seconds": stop * 0.020,
                "active_frames": int(np.count_nonzero(local_mask)),
                "candidate_minus_baseline_db": audio.amp_db(
                    candidate_rms / max(baseline_rms, audio.EPS)
                ),
            }
        )
    return rows


def temporal_vs_baseline(
    raw_path: Path,
    baseline_path: Path,
    candidate_path: Path,
    label: str,
) -> dict[str, Any]:
    rate = EXPECTED_SAMPLE_RATE
    raw_native = load_mono(raw_path, rate)
    baseline_native = load_mono(baseline_path, rate)
    candidate_native = load_mono(candidate_path, rate)
    maximum_lag = rate
    baseline_lag, baseline_alignment = audio.best_placement_lag(
        raw_native, baseline_native, maximum_lag
    )
    candidate_lag, candidate_alignment = audio.best_placement_lag(
        raw_native, candidate_native, maximum_lag
    )
    raw_common, baseline, candidate, start, stop = audio.common_overlap(
        raw_native,
        baseline_native,
        candidate_native,
        baseline_lag,
        candidate_lag,
    )
    masks, selection = audio.selection_masks(raw_common, rate, 20.0, 300.0, 0.25, 0.65)
    frame_size = int(selection["frame_samples"])
    baseline_env = audio.frame_rms(baseline, frame_size)
    candidate_env_native = audio.frame_rms(candidate, frame_size)
    frame_count = min(baseline_env.size, candidate_env_native.size, masks["active"].size)
    active = masks["active"][:frame_count]
    quiet = masks["quiet"][:frame_count]
    if int(np.count_nonzero(active)) < 10 or int(np.count_nonzero(quiet)) < 5:
        raise ValueError(f"frames ativos/quietos insuficientes em {label}")

    baseline_active_rms = rms_from_frames(baseline_env[:frame_count], active)
    candidate_active_rms = rms_from_frames(candidate_env_native[:frame_count], active)
    match_db = gain_db(baseline_active_rms, candidate_active_rms)
    match_linear = 10.0 ** (match_db / 20.0)
    candidate_matched = candidate[: baseline.size] * match_linear
    candidate_env = audio.frame_rms(candidate_matched, frame_size)[:frame_count]
    baseline_env = baseline_env[:frame_count]
    frame_delta = 20.0 * np.log10(
        np.maximum(candidate_env, audio.EPS) / np.maximum(baseline_env, audio.EPS)
    )
    active_delta = frame_delta[active]

    windows_200ms = window_deltas(
        baseline_env, candidate_env, active, 10, 5, 5
    )
    if not windows_200ms:
        raise ValueError(f"nenhuma janela ativa de 200 ms em {label}")
    windows_5s = window_deltas(
        baseline_env, candidate_env, active, 250, 50, 25
    )

    phrase_rows: list[dict[str, float]] = []
    for region_start, region_stop, local_mask in bridged_active_regions(active, 10, 10):
        baseline_rms = rms_from_frames(
            baseline_env[region_start:region_stop], local_mask
        )
        candidate_rms = rms_from_frames(
            candidate_env[region_start:region_stop], local_mask
        )
        phrase_rows.append(
            {
                "start_seconds": region_start * 0.020,
                "stop_seconds": region_stop * 0.020,
                "active_frames": int(np.count_nonzero(local_mask)),
                "candidate_minus_baseline_db": audio.amp_db(
                    candidate_rms / max(baseline_rms, audio.EPS)
                ),
            }
        )
    if not phrase_rows:
        phrase_rows = [
            {
                "start_seconds": 0.0,
                "stop_seconds": frame_count * 0.020,
                "active_frames": int(np.count_nonzero(active)),
                "candidate_minus_baseline_db": audio.amp_db(
                    rms_from_frames(candidate_env, active)
                    / max(rms_from_frames(baseline_env, active), audio.EPS)
                ),
            }
        ]

    active_samples = audio.samples_from_frame_mask(active, frame_size, baseline.size)
    baseline_power, baseline_windows = audio.accumulated_spectrum(
        baseline, active_samples
    )
    candidate_power, candidate_windows = audio.accumulated_spectrum(
        candidate, active_samples
    )
    spectral_shift = audio.spectral_comparison(
        candidate_power,
        baseline_power,
        rate,
        match_db,
        candidate_windows,
        baseline_windows,
    )

    baseline_quiet_rms = rms_from_frames(baseline_env, quiet)
    candidate_quiet_rms = rms_from_frames(candidate_env, quiet)
    background_delta = audio.amp_db(
        candidate_quiet_rms / max(baseline_quiet_rms, audio.EPS)
    )
    window_abs = [abs(row["candidate_minus_baseline_db"]) for row in windows_200ms]
    phrase_abs = [abs(row["candidate_minus_baseline_db"]) for row in phrase_rows]
    non_sibilant = [
        abs(finite(spectral_shift["band_delta_db"].get(name), f"shift {name}"))
        for name in NON_SIBILANT_BANDS
    ]
    strongest_windows = sorted(
        windows_200ms,
        key=lambda row: abs(row["candidate_minus_baseline_db"]),
        reverse=True,
    )[:10]
    strongest_phrases = sorted(
        phrase_rows,
        key=lambda row: abs(row["candidate_minus_baseline_db"]),
        reverse=True,
    )[:10]
    return {
        "schema_version": 1,
        "label": label,
        "alignment": {
            "baseline_to_raw_lag_ms": 1000.0 * baseline_lag / rate,
            "baseline_to_raw_correlation": baseline_alignment,
            "candidate_to_raw_lag_ms": 1000.0 * candidate_lag / rate,
            "candidate_to_raw_correlation": candidate_alignment,
            "common_start_frame": start,
            "common_stop_frame": stop,
        },
        "selection": selection,
        "active_match_gain_db_applied_to_candidate": match_db,
        "metrics": {
            "active_frame_delta_median_abs_db": float(np.median(np.abs(active_delta))),
            "active_frame_delta_p95_abs_db": float(np.quantile(np.abs(active_delta), 0.95)),
            "active_frame_delta_max_abs_db": float(np.max(np.abs(active_delta))),
            "active_envelope_correlation": audio.normalized_corr(
                np.log10(baseline_env[active] + audio.EPS),
                np.log10(candidate_env[active] + audio.EPS),
            ),
            "window_200ms_p95_abs_db": percentile(window_abs, 0.95, "janelas 200 ms"),
            "window_200ms_max_abs_db": max(window_abs),
            "phrase_p95_abs_db": percentile(phrase_abs, 0.95, "frases"),
            "phrase_max_abs_db": max(phrase_abs),
            "window_5s_max_abs_db": (
                max(abs(row["candidate_minus_baseline_db"]) for row in windows_5s)
                if windows_5s
                else None
            ),
            "background_change_after_voice_match_db": background_delta,
            "max_abs_non_sibilant_band_shift_db": max(non_sibilant),
        },
        "active_band_shift_after_voice_match_db": spectral_shift["band_delta_db"],
        "strongest_200ms_windows": strongest_windows,
        "strongest_phrases": strongest_phrases,
        "windows_5s": windows_5s,
    }


def comparison_row(
    clip_id: str,
    label: str,
    payload: Mapping[str, Any],
    temporal: Mapping[str, Any],
) -> dict[str, Any]:
    comparison = payload.get("comparison")
    sibilance = payload.get("sibilance")
    if not isinstance(comparison, Mapping) or not isinstance(sibilance, Mapping):
        raise ValueError(f"payload incompleto em {label}/{clip_id}")
    primary = nested(comparison, "comparisons", "candidate_vs_target")
    bands = nested(
        primary,
        "spectral_active_after_integrated_loudness_match",
        "band_delta_db",
    )
    if not isinstance(primary, Mapping) or not isinstance(bands, Mapping):
        raise ValueError(f"espectro ativo ausente em {label}/{clip_id}")
    core_values = [finite(bands.get(name), f"{label}/{clip_id}/{name}") for name in CORE_BANDS]
    metrics = nested(sibilance, "metrics")
    temporal_metrics = temporal.get("metrics")
    if not isinstance(metrics, Mapping) or not isinstance(temporal_metrics, Mapping):
        raise ValueError(f"metricas incompletas em {label}/{clip_id}")
    candidate_audit = nested(comparison, "files", "candidate")
    target_audit = nested(comparison, "files", "target")
    if not isinstance(candidate_audit, Mapping) or not isinstance(target_audit, Mapping):
        raise ValueError(f"auditoria de arquivos ausente em {label}/{clip_id}")

    result = {
        "candidate": label,
        "clip_id": clip_id,
        "core_voice_mae_db": float(np.mean(np.abs(core_values))),
        "active_full_spectral_mae_db": finite(
            nested(primary, "spectral_active_after_integrated_loudness_match", "mean_absolute_error_db"),
            f"MAE ativo {label}/{clip_id}",
        ),
        "quiet_spectral_mae_db": finite(
            nested(primary, "spectral_quiet_after_voice_match", "mean_absolute_error_db"),
            f"MAE quieto {label}/{clip_id}",
        ),
        "background_after_voice_match_db": finite(
            nested(primary, "background_after_voice_match", "quiet_rms_delta_db"),
            f"fundo {label}/{clip_id}",
        ),
        "target_active_envelope_correlation": finite(
            nested(primary, "envelope", "active_log_rms_correlation"),
            f"envelope alvo {label}/{clip_id}",
        ),
        "loudness_delta_lu": finite(
            nested(primary, "native_delivery_file_delta", "integrated_loudness_delta_lu"),
            f"loudness {label}/{clip_id}",
        ),
        "active_voice_delta_db": finite(
            nested(primary, "active", "candidate_minus_reference_rms_db"),
            f"voz {label}/{clip_id}",
        ),
        "sample_peak_dbfs": finite(
            nested(candidate_audit, "sample_peak", "mono_dbfs"),
            f"sample peak {label}/{clip_id}",
        ),
        "true_peak_dbtp": finite(
            nested(candidate_audit, "true_peak_approx", "mono_dbtp"),
            f"true peak {label}/{clip_id}",
        ),
        "sample_peak_delta_db": finite(
            nested(primary, "native_delivery_file_delta", "sample_peak_delta_db"),
            f"sample peak delta {label}/{clip_id}",
        ),
        "sibilance_broad_delta_db": finite(
            metrics.get("active_4k_8k_delta_after_loudness_match_db"),
            f"S amplo {label}/{clip_id}",
        ),
        "sibilance_event_median_delta_db": finite(
            metrics.get("target_sibilant_ratio_delta_median_db"),
            f"S mediano {label}/{clip_id}",
        ),
        "sibilance_event_p90_abs_db": finite(
            metrics.get("target_sibilant_ratio_delta_p90_absolute_db"),
            f"S P90 {label}/{clip_id}",
        ),
        "temporal_phrase_max_abs_db": finite(
            temporal_metrics.get("phrase_max_abs_db"),
            f"frase {label}/{clip_id}",
        ),
        "temporal_window_200ms_p95_abs_db": finite(
            temporal_metrics.get("window_200ms_p95_abs_db"),
            f"janela {label}/{clip_id}",
        ),
        "temporal_active_envelope_correlation": finite(
            temporal_metrics.get("active_envelope_correlation"),
            f"envelope baseline {label}/{clip_id}",
        ),
        "temporal_max_abs_non_sibilant_band_shift_db": finite(
            temporal_metrics.get("max_abs_non_sibilant_band_shift_db"),
            f"shift timbral {label}/{clip_id}",
        ),
        "background_change_vs_baseline_db": finite(
            temporal_metrics.get("background_change_after_voice_match_db"),
            f"fundo vs baseline {label}/{clip_id}",
        ),
        "band_delta_db": {name: finite(bands.get(name), f"banda {name}") for name in audio.BANDS_BY_NAME}
        if hasattr(audio, "BANDS_BY_NAME")
        else {name: finite(bands.get(name), f"banda {name}") for name, _low, _high in audio.BANDS},
    }
    return result


def aggregate_candidate(
    label: str,
    rows: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, float] | None,
    phase: str,
) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        return [finite(row.get(key), f"{label}/{key}") for row in rows]

    core = values("core_voice_mae_db")
    background = values("background_after_voice_match_db")
    loudness = values("loudness_delta_lu")
    voice = values("active_voice_delta_db")
    s_median = values("sibilance_event_median_delta_db")
    s_p90 = values("sibilance_event_p90_abs_db")
    quiet_mae = values("quiet_spectral_mae_db")
    phrase = values("temporal_phrase_max_abs_db")
    window = values("temporal_window_200ms_p95_abs_db")
    temporal_corr = values("temporal_active_envelope_correlation")
    non_s_shift = values("temporal_max_abs_non_sibilant_band_shift_db")
    sample_peaks = values("sample_peak_dbfs")
    true_peaks = values("true_peak_dbtp")

    band_medians = {
        name: median(
            [finite(row["band_delta_db"].get(name), f"{label}/{name}") for row in rows],
            f"mediana {label}/{name}",
        )
        for name in CORE_BANDS
    }
    summary = {
        "candidate": label,
        "clips": len(rows),
        "core_voice_mae_median_db": median(core, f"MAE core {label}"),
        "core_voice_mae_p90_db": percentile(core, 0.90, f"P90 core {label}"),
        "background_abs_median_db": median(map(abs, background), f"fundo {label}"),
        "background_signed_median_db": median(background, f"fundo assinado {label}"),
        "background_min_db": min(background),
        "quiet_spectral_mae_median_db": median(quiet_mae, f"quiet MAE {label}"),
        "loudness_abs_median_lu": median(map(abs, loudness), f"loudness {label}"),
        "loudness_abs_max_lu": max(map(abs, loudness)),
        "active_voice_abs_median_db": median(map(abs, voice), f"voz {label}"),
        "active_voice_abs_max_db": max(map(abs, voice)),
        "sibilance_event_median_abs_median_db": median(
            map(abs, s_median), f"S mediano {label}"
        ),
        "sibilance_event_p90_median_db": median(s_p90, f"S P90 {label}"),
        "temporal_phrase_max_abs_db": max(phrase),
        "temporal_window_200ms_p95_max_db": max(window),
        "temporal_active_envelope_min": min(temporal_corr),
        "temporal_non_sibilant_shift_max_db": max(non_s_shift),
        "sample_peak_max_dbfs": max(sample_peaks),
        "true_peak_max_dbtp": max(true_peaks),
        "core_band_median_delta_db": band_medians,
    }
    if baseline is None:
        processor_gates: dict[str, bool] = {}
    else:
        processor_gates = {
            "phrase_max_abs_le_0_75db": summary["temporal_phrase_max_abs_db"] <= PHRASE_MAX_ABS_DB,
            "window_200ms_p95_le_0_75db": summary["temporal_window_200ms_p95_max_db"] <= WINDOW_200MS_P95_ABS_DB,
            "temporal_envelope_ge_0_995": summary["temporal_active_envelope_min"] >= TEMPORAL_ENVELOPE_MIN,
            "non_sibilant_shift_le_0_75db": summary["temporal_non_sibilant_shift_max_db"] <= NON_SIBILANT_SHIFT_MAX_DB,
            "core_mae_median_regression_le_0_15db": summary["core_voice_mae_median_db"] <= baseline["core_voice_mae_median_db"] + CORE_MAE_MEDIAN_REGRESSION_DB,
            "core_mae_p90_regression_le_0_25db": summary["core_voice_mae_p90_db"] <= baseline["core_voice_mae_p90_db"] + CORE_MAE_P90_REGRESSION_DB,
            "sibilance_p90_regression_le_0_25db": summary["sibilance_event_p90_median_db"] <= baseline["sibilance_event_p90_median_db"] + SIBILANCE_P90_MEDIAN_REGRESSION_DB,
            "quiet_mae_regression_le_0_50db": summary["quiet_spectral_mae_median_db"] <= baseline["quiet_spectral_mae_median_db"] + QUIET_MAE_MEDIAN_REGRESSION_DB,
            "background_not_oversuppressed_below_minus6db": summary["background_min_db"] >= BACKGROUND_OVERSUPPRESSION_MIN_DB,
        }
    final_gates = {
        "core_mae_median_le_1_25db": summary["core_voice_mae_median_db"] <= FINAL_CORE_MAE_MEDIAN_DB,
        "core_mae_p90_le_1_75db": summary["core_voice_mae_p90_db"] <= FINAL_CORE_MAE_P90_DB,
        "all_core_band_medians_abs_le_1_50db": all(abs(value) <= FINAL_BAND_MEDIAN_ABS_DB for value in band_medians.values()),
        "loudness_median_abs_le_0_50lu": summary["loudness_abs_median_lu"] <= FINAL_LOUDNESS_MEDIAN_ABS_LU,
        "loudness_each_abs_le_1lu": summary["loudness_abs_max_lu"] <= FINAL_LOUDNESS_MAX_ABS_LU,
        "voice_median_abs_le_0_50db": summary["active_voice_abs_median_db"] <= FINAL_VOICE_MEDIAN_ABS_DB,
        "voice_each_abs_le_1db": summary["active_voice_abs_max_db"] <= FINAL_VOICE_MAX_ABS_DB,
        "background_median_abs_le_3db": summary["background_abs_median_db"] <= FINAL_BACKGROUND_MEDIAN_ABS_DB,
        "sibilance_event_median_abs_le_1db": summary["sibilance_event_median_abs_median_db"] <= FINAL_SIBILANCE_MEDIAN_ABS_DB,
        "sample_peak_le_minus0_29dbfs": summary["sample_peak_max_dbfs"] <= FINAL_SAMPLE_PEAK_MAX_DBFS,
        "true_peak_below_0dbtp": summary["true_peak_max_dbtp"] < FINAL_TRUE_PEAK_MAX_DBTP,
    }
    gates = dict(processor_gates)
    if phase == "final":
        gates.update(final_gates)
    summary["processor_selection_gates"] = processor_gates
    summary["final_chain_gates_information"] = final_gates
    summary["phase_evaluated"] = phase
    summary["hard_gate_passed"] = bool(gates) and all(gates.values())
    summary["failed_gates"] = [name for name, passed in gates.items() if not passed]
    return summary


def make_ranking(
    rows_by_candidate: Mapping[str, Sequence[Mapping[str, Any]]],
    baseline_label: str,
    phase: str,
) -> list[dict[str, Any]]:
    preliminary = {
        label: aggregate_candidate(label, rows, None, phase)
        for label, rows in rows_by_candidate.items()
    }
    baseline = preliminary[baseline_label]
    aggregated = [
        aggregate_candidate(label, rows, baseline, phase)
        for label, rows in rows_by_candidate.items()
    ]
    aggregated.sort(
        key=lambda row: (
            0 if row["hard_gate_passed"] else 1,
            row["core_voice_mae_median_db"],
            row["sibilance_event_median_abs_median_db"],
            row["background_abs_median_db"],
            row["temporal_phrase_max_abs_db"],
            row["loudness_abs_median_lu"],
            row["candidate"],
        )
    )
    for rank, row in enumerate(aggregated, start=1):
        row["rank"] = rank
        row["ranking_key_order"] = [
            "hard_gate_passed",
            "core_voice_mae_median_db",
            "sibilance_event_median_abs_median_db",
            "background_abs_median_db",
            "temporal_phrase_max_abs_db",
            "loudness_abs_median_lu",
        ]
    return aggregated


def render_markdown(
    tag: str,
    scope: str,
    phase: str,
    ranking: Sequence[Mapping[str, Any]],
    rows_by_candidate: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str:
    lines = [
        f"# Grade sem Enhance — {tag}",
        "",
        f"Escopo medido: **{scope}**; fase de gates: **{phase}**.",
        f"IDs medidos: `{','.join(row['clip_id'] for row in next(iter(rows_by_candidate.values())))}`.",
        f"Holdout preservado e nao analisado: `{','.join(HOLDOUT)}`.",
        "",
        "## Ranking lexicografico",
        "",
        "| # | Variante | Gates | MAE timbre core | S evento | Fundo | Frase max | Loudness |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in ranking:
        gate_text = "PASS" if row["hard_gate_passed"] else "REJECT"
        lines.append(
            f"| {row['rank']} | {row['candidate']} | {gate_text} | "
            f"{row['core_voice_mae_median_db']:.3f} dB | "
            f"{row['sibilance_event_median_abs_median_db']:.3f} dB | "
            f"{row['background_abs_median_db']:.3f} dB | "
            f"{row['temporal_phrase_max_abs_db']:.3f} dB | "
            f"{row['loudness_abs_median_lu']:.3f} LU |"
        )
    lines.extend(
        [
            "",
            "A ordem e deliberadamente: integridade temporal/timbral, timbre core, "
            "sibilancia, ruido/ambiencia e somente depois nivel de entrega.",
            "",
            "## Gates de selecao RN/DeEss",
            "",
            "- Frase mais alterada e P95 das janelas de 200 ms: no maximo 0,75 dB contra o baseline, apos um unico casamento global da voz.",
            "- Correlacao do envelope ativo contra baseline: pelo menos 0,995.",
            "- Mudanca maxima nas bandas nao sibilantes 80 Hz–4 kHz: no maximo 0,75 dB.",
            "- MAE core mediano/P90 nao pode piorar mais que 0,15/0,25 dB contra o baseline.",
            "- P90 sibilante mediano nao pode piorar mais que 0,25 dB; MAE do fundo nao pode piorar mais que 0,50 dB.",
            "- Nenhum clip pode ficar com fundo mais de 6 dB abaixo do CapCut apos casar a voz.",
            "",
            "## Leitura por clip",
            "",
            "| Variante | ID | MAE core | Fundo | Env alvo | S med. | S P90 | Frase max | Janela P95 | Voz | LU | Peak |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for label, rows in rows_by_candidate.items():
        for row in rows:
            lines.append(
                f"| {label} | {row['clip_id']} | {row['core_voice_mae_db']:.3f} | "
                f"{row['background_after_voice_match_db']:+.3f} | "
                f"{row['target_active_envelope_correlation']:.4f} | "
                f"{row['sibilance_event_median_delta_db']:+.3f} | "
                f"{row['sibilance_event_p90_abs_db']:.3f} | "
                f"{row['temporal_phrase_max_abs_db']:.3f} | "
                f"{row['temporal_window_200ms_p95_abs_db']:.3f} | "
                f"{row['active_voice_delta_db']:+.3f} | "
                f"{row['loudness_delta_lu']:+.3f} | "
                f"{row['sample_peak_dbfs']:.3f} |"
            )
    lines.extend(
        [
            "",
            "O codigo de saida 0 significa que a medicao foi concluida e publicada; "
            "a aprovacao sonora esta em `hard_gate_passed` e nunca e inferida do exit code.",
            "",
        ]
    )
    return "\n".join(lines)


CSV_COLUMNS = (
    "candidate",
    "clip_id",
    "core_voice_mae_db",
    "background_after_voice_match_db",
    "quiet_spectral_mae_db",
    "target_active_envelope_correlation",
    "sibilance_broad_delta_db",
    "sibilance_event_median_delta_db",
    "sibilance_event_p90_abs_db",
    "temporal_phrase_max_abs_db",
    "temporal_window_200ms_p95_abs_db",
    "temporal_active_envelope_correlation",
    "temporal_max_abs_non_sibilant_band_shift_db",
    "background_change_vs_baseline_db",
    "active_voice_delta_db",
    "loudness_delta_lu",
    "sample_peak_dbfs",
    "true_peak_dbtp",
)


def write_reports(
    staging: Path,
    tag: str,
    scope: str,
    phase: str,
    ranking: Sequence[Mapping[str, Any]],
    rows_by_candidate: Mapping[str, Sequence[Mapping[str, Any]]],
    manifest: Mapping[str, Any],
) -> None:
    reports = staging / "relatorios"
    reports.mkdir()
    (reports / "RANKING.json").write_text(
        json.dumps({"ranking": ranking, "per_clip": rows_by_candidate}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (reports / "RANKING.md").write_text(
        render_markdown(tag, scope, phase, ranking, rows_by_candidate),
        encoding="utf-8",
    )
    with (reports / "POR_CLIP.csv").open("x", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rows in rows_by_candidate.values():
            for row in rows:
                writer.writerow({key: row.get(key) for key in CSV_COLUMNS})
    (reports / "MANIFESTO_EXECUCAO.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="rotulo unico da execucao")
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="concat completo; repita uma vez por variante",
    )
    parser.add_argument(
        "--baseline",
        default="RN20_DE0",
        help="rotulo usado para medir mudancas temporais relativas",
    )
    parser.add_argument(
        "--scope",
        choices=("sentinels", "calibration"),
        default="sentinels",
        help="sentinels=6 primeiro; calibration=6+4, ainda sem holdout",
    )
    parser.add_argument(
        "--phase",
        choices=("processor", "final"),
        default="processor",
        help="processor escolhe RN/DeEss; final acrescenta gates de EQ/limiter",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="valida suite/concats sem dividir, analisar ou criar artefatos",
    )
    return parser


def execute(args: argparse.Namespace) -> int:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("use Python 3.12 do runtime canonico do workspace")
    if np.__version__ != "2.3.5":
        raise ValueError(f"use NumPy 2.3.5; recebido {np.__version__}")
    if not TAG_PATTERN.fullmatch(args.tag):
        raise ValueError("--tag invalido: use 1–80 caracteres seguros")
    if set(SENTINELS) & set(HOLDOUT) or set(CALIBRATION) & set(HOLDOUT):
        raise ValueError("constantes de selecao contaminam o holdout")
    if set(CALIBRATION) | set(HOLDOUT) != set(frozen.CLIP_IDS):
        raise ValueError("calibracao + holdout nao particionam os 20 IDs")
    if frozen.PREFLIGHT_MANIFEST.is_symlink():
        raise ValueError("manifesto de preflight nao pode ser link simbolico")
    preflight_hash = common.file_sha256(frozen.PREFLIGHT_MANIFEST)
    full_suite, raw_infos, target_infos, frozen_hashes = frozen.load_frozen_suite()
    expected_frames = sum(info.frames for info in raw_infos.values())
    if expected_frames != EXPECTED_TOTAL_FRAMES:
        raise ValueError(
            f"suite congelada soma {expected_frames}, nao {EXPECTED_TOTAL_FRAMES} frames"
        )

    candidates = parse_candidates(args.candidate)
    if args.baseline not in candidates:
        raise ValueError(f"--baseline {args.baseline} nao aparece em --candidate")
    candidate_records = inspect_candidates(candidates)
    if common.file_sha256(frozen.PREFLIGHT_MANIFEST) != preflight_hash:
        raise ValueError("manifesto mudou durante o preflight")
    if args.preflight_only:
        print(
            json.dumps(
                {
                    "status": "PREFLIGHT_PASS",
                    "suite": frozen.SUITE_NAME,
                    "total_frames": expected_frames,
                    "duration_seconds": expected_frames / EXPECTED_SAMPLE_RATE,
                    "candidates": candidate_records,
                    "next_scope": args.scope,
                    "holdout_not_analyzed": list(HOLDOUT),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if OUTPUT_ROOT.is_symlink():
        raise ValueError("pasta de resultados nao pode ser link simbolico")
    run_root = OUTPUT_ROOT / args.tag
    if run_root.exists():
        raise ValueError(f"execucao ja existe; use outro --tag: {run_root}")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if OUTPUT_ROOT.resolve().parent != BASE.resolve():
        raise ValueError("pasta de resultados escapou da base congelada")
    required_bytes = sum(
        Path(record["path"]).stat().st_size for record in candidate_records.values()
    )
    required_bytes += sum(
        Path(raw_infos[clip_id].path).stat().st_size
        + Path(target_infos[clip_id].path).stat().st_size
        for clip_id in (SENTINELS if args.scope == "sentinels" else CALIBRATION)
    )
    if shutil.disk_usage(OUTPUT_ROOT).free < required_bytes + 128 * 1024 * 1024:
        raise ValueError("espaco livre insuficiente para segmentos e analises")

    selected_ids = SENTINELS if args.scope == "sentinels" else CALIBRATION
    analysis_suite = selected_suite(full_suite, selected_ids)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{args.tag}.", dir=OUTPUT_ROOT))
    committed = False
    try:
        frozen_analysis_suite, frozen_input_records = frozen.freeze_suite_inputs(
            analysis_suite, frozen_hashes, temporary_root
        )
        split_by_candidate: dict[str, dict[str, Path]] = {}
        split_records: dict[str, list[dict[str, Any]]] = {}
        order_proofs: dict[str, Any] = {}
        for label, path in candidates.items():
            print(f"[split] {label}", file=sys.stderr, flush=True)
            split_dir = temporary_root / "candidatos_split" / label
            split_dir.mkdir(parents=True)
            info = common.inspect_wave(path, f"concat {label}")
            segment_paths, records = common.prepare_segments(full_suite, info, split_dir)
            order_proofs[label] = frozen.prove_order(
                full_suite,
                segment_paths,
                frozen.ORDER_MIN_CORRELATION,
                frozen.ORDER_MIN_MARGIN,
            )
            split_by_candidate[label] = segment_paths
            split_records[label] = records

        rows_by_candidate: dict[str, list[dict[str, Any]]] = {}
        baseline_segments = split_by_candidate[args.baseline]
        frozen_clips = {clip.clip_id: clip for clip in frozen_analysis_suite.clips}
        for label in candidates:
            print(f"[analise] {label}", file=sys.stderr, flush=True)
            analysis_dir = temporary_root / "analises" / label
            selected_candidates = {
                clip_id: split_by_candidate[label][clip_id]
                for clip_id in selected_ids
            }
            summaries, payloads = frozen.run_analyzers(
                frozen_analysis_suite,
                selected_candidates,
                analysis_dir,
                label,
            )
            if set(summaries) != set(selected_ids) or set(payloads) != set(selected_ids):
                raise ValueError(f"analisadores nao devolveram todos os IDs de {label}")
            rows: list[dict[str, Any]] = []
            for clip_id in selected_ids:
                temporal = temporal_vs_baseline(
                    frozen_clips[clip_id].raw_path(),
                    baseline_segments[clip_id],
                    split_by_candidate[label][clip_id],
                    f"{label}/{clip_id}",
                )
                temporal_path = analysis_dir / clip_id / f"{clip_id}_{label}_TEMPORAL_VS_{args.baseline}.json"
                temporal_path.write_text(
                    json.dumps(temporal, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                rows.append(comparison_row(clip_id, label, payloads[clip_id], temporal))
            rows_by_candidate[label] = rows

        ranking = make_ranking(rows_by_candidate, args.baseline, args.phase)
        manifest = {
            "schema_version": 1,
            "execution_status": "COMPLETE_MEASUREMENT",
            "exit_code_semantics": "0 = medicao integra; consulte hard_gate_passed",
            "tag": args.tag,
            "scope": args.scope,
            "phase": args.phase,
            "baseline": args.baseline,
            "full_concat_order_validated": list(frozen.CLIP_IDS),
            "analyzed_ids_in_order": list(selected_ids),
            "holdout_ids_not_analyzed": list(HOLDOUT),
            "holdout_policy": (
                "este runner nao possui opcao de holdout; revelar somente em runner "
                "separado depois do freeze documentado do candidato"
            ),
            "preflight_manifest": {
                "path": str(frozen.PREFLIGHT_MANIFEST),
                "file_sha256": preflight_hash,
            },
            "concat_inputs": candidate_records,
            "split_records": split_records,
            "order_proofs": order_proofs,
            "frozen_inputs_read_by_analyzers": frozen_input_records,
            "analysis_scripts": {
                "primary": str(frozen.ANALYZER),
                "sibilance": str(frozen.SIBILANCE_ANALYZER),
                "runner": str(Path(__file__).resolve()),
            },
            "preset_or_project_modified": False,
        }
        write_reports(
            temporary_root,
            args.tag,
            args.scope,
            args.phase,
            ranking,
            rows_by_candidate,
            manifest,
        )

        # Revalide todos os insumos depois da analise para fechar corrida/mutacao.
        if common.file_sha256(frozen.PREFLIGHT_MANIFEST) != preflight_hash:
            raise ValueError("manifesto mudou durante a analise")
        for label, path in candidates.items():
            if common.file_sha256(path) != candidate_records[label]["file_sha256"]:
                raise ValueError(f"concat {label} mudou durante a analise")
        for clip_id in frozen.CLIP_IDS:
            if common.file_sha256(Path(raw_infos[clip_id].path)) != frozen_hashes[clip_id]["raw_file_sha256"]:
                raise ValueError(f"raw {clip_id} mudou durante a analise")
            if common.file_sha256(Path(target_infos[clip_id].path)) != frozen_hashes[clip_id]["target_file_sha256"]:
                raise ValueError(f"alvo {clip_id} mudou durante a analise")

        frozen.rebase_generated_paths(temporary_root, temporary_root, run_root)
        if run_root.exists():
            raise ValueError("tag apareceu durante a execucao; commit cancelado")
        temporary_root.rename(run_root)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(temporary_root, ignore_errors=True)

    print(
        json.dumps(
            {
                "status": "COMPLETE_MEASUREMENT",
                "run_root": str(run_root),
                "ranking_json": str(run_root / "relatorios" / "RANKING.json"),
                "ranking_markdown": str(run_root / "relatorios" / "RANKING.md"),
                "per_clip_csv": str(run_root / "relatorios" / "POR_CLIP.csv"),
                "winner_by_objective_ranking": ranking[0]["candidate"],
                "winner_hard_gate_passed": ranking[0]["hard_gate_passed"],
                "holdout_not_analyzed": list(HOLDOUT),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return execute(args)
    except (OSError, ValueError, RuntimeError, wave.Error) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
