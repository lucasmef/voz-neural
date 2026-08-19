#!/usr/bin/env python3
"""Meca e ranqueie a grade final N0/DN35/DN50/EXP do take DJI 95.

O programa e deliberadamente restrito ao ID 95: recusa caminhos cegos e
qualquer nome de arquivo cujo ID inicial nao seja 95. MOV fl32 e aceito apenas
como fonte de analise e e decodificado para um WAV temporario; nenhuma midia e
alterada. Os calculos reutilizam ``comparar_wavs_adobe.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

import comparar_wavs_adobe as base
import extrair_fl32_mov


NATIVE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).with_name("GRADE_FINAL_95_MANIFEST.json")
EXPECTED_IDS = ("N0", "DN35", "DN50", "EXP")
EPS = 1.0e-12


def finite(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def rms_db(samples: np.ndarray) -> float | None:
    if not samples.size:
        return None
    return base.amp_db(base.rms(samples))


def max_channel_peak_db(audit: dict[str, Any]) -> float | None:
    values = audit["sample_peak"]["per_channel_dbfs"]
    return max(map(float, values)) if values else None


def max_channel_true_peak_db(audit: dict[str, Any]) -> float | None:
    values = audit["true_peak_approx"]["per_channel_dbtp"]
    return max(map(float, values)) if values else None


def corr_log(left: np.ndarray, right: np.ndarray) -> float | None:
    if min(left.size, right.size) < 2:
        return None
    return finite(
        base.normalized_corr(
            np.log10(np.maximum(left, EPS)),
            np.log10(np.maximum(right, EPS)),
        )
    )


def assert_scope_95(path: Path, role: str) -> None:
    name = path.name
    if "blind" in str(path).lower():
        raise ValueError(f"{role}: caminho cego proibido: {path}")
    match = re.match(r"^0*(\d{2,3})(?:[-_.]|$)", name)
    if not match:
        raise ValueError(f"{role}: nome precisa comecar pelo ID 95: {name}")
    clip_id = int(match.group(1))
    if clip_id < 64:
        raise ValueError(f"{role}: ID abaixo de 64 proibido: {name}")
    if clip_id != 95:
        raise ValueError(f"{role}: este analisador aceita somente ID 95: {name}")


def resolve_path(options: list[str], role: str) -> Path | None:
    for item in options:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = NATIVE_ROOT / path
        path = path.resolve()
        if path.is_file():
            assert_scope_95(path, role)
            return path
    return None


def decode_audio(source: Path, temporary: Path) -> tuple[base.PcmWav, dict[str, Any]]:
    assert_scope_95(source, "audio")
    if source.suffix.lower() == ".wav":
        return base.read_pcm_wav(source), {
            "source_path": str(source),
            "decoded_from_mov_fl32": False,
        }
    if source.suffix.lower() not in {".mov", ".mp4"}:
        raise ValueError(f"formato nao suportado: {source}")
    output = temporary / f"{source.stem}_ANALYSIS_PCM24.wav"
    extraction = extrair_fl32_mov.extract(source, output)
    return base.read_pcm_wav(output), {
        "source_path": str(source),
        "decoded_from_mov_fl32": True,
        "decode": {key: value for key, value in extraction.items() if key != "output"},
    }


def source_audit(
    wav: base.PcmWav,
    source: Path,
    provenance: dict[str, Any],
    true_peak_factor: int,
) -> dict[str, Any]:
    audit = base.audit_file(wav, true_peak_factor)
    decoded_bytes = audit["bytes"]
    audit["path"] = str(source)
    audit["bytes"] = source.stat().st_size
    if provenance["decoded_from_mov_fl32"]:
        audit["analysis_decode_pcm24_bytes"] = decoded_bytes
    audit["analysis_provenance"] = provenance
    return audit


def analysis_mono(wav: base.PcmWav, rate: int) -> np.ndarray:
    return base.sinc_resample(base.mono_mean(wav.samples), wav.sample_rate, rate)


def frame_mask_to_samples(mask: np.ndarray, frame_size: int, length: int) -> np.ndarray:
    return np.repeat(mask, frame_size)[:length]


def interval_frame_mask(
    frame_times: np.ndarray, start_seconds: float, stop_seconds: float
) -> np.ndarray:
    return (frame_times >= start_seconds) & (frame_times < stop_seconds)


def spectrum_for_mask(
    candidate: np.ndarray,
    target: np.ndarray,
    sample_mask: np.ndarray,
    rate: int,
    match_gain_db: float,
) -> dict[str, Any]:
    candidate_power, candidate_windows = base.accumulated_spectrum(
        candidate, sample_mask
    )
    target_power, target_windows = base.accumulated_spectrum(target, sample_mask)
    result = base.spectral_comparison(
        candidate_power,
        target_power,
        rate,
        match_gain_db,
        candidate_windows,
        target_windows,
    )
    core_names = (
        "low_80_200",
        "body_200_500",
        "mid_500_1000",
        "presence_1k_2k",
        "clarity_2k_4k",
        "sibilance_4k_8k",
    )
    core = [
        abs(float(result["band_delta_db"][name]))
        for name in core_names
        if name in result["band_delta_db"]
    ]
    result["core_voice_80hz_8khz_mae_db"] = float(np.mean(core)) if core else None
    return result


def summarize_mask(
    name: str,
    frame_mask: np.ndarray,
    raw: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    raw_env: np.ndarray,
    candidate_env: np.ndarray,
    target_env: np.ndarray,
    frame_size: int,
    rate: int,
    voice_match_gain_db: float,
    include_spectrum: bool = False,
) -> dict[str, Any]:
    sample_mask = frame_mask_to_samples(frame_mask, frame_size, raw.size)
    selected_candidate = candidate[sample_mask]
    selected_target = target[sample_mask]
    selected_raw = raw[sample_mask]
    if not selected_candidate.size:
        return {"name": name, "frames": 0, "seconds": 0.0}
    candidate_levels = 20.0 * np.log10(np.maximum(candidate_env[frame_mask], EPS))
    target_levels = 20.0 * np.log10(np.maximum(target_env[frame_mask], EPS))
    candidate_rms = rms_db(selected_candidate)
    target_rms = rms_db(selected_target)
    absolute_delta = (
        float(candidate_rms - target_rms)
        if candidate_rms is not None and target_rms is not None
        else None
    )
    result: dict[str, Any] = {
        "name": name,
        "frames": int(np.count_nonzero(frame_mask)),
        "seconds": float(np.count_nonzero(frame_mask) * frame_size / rate),
        "raw_rms_dbfs": rms_db(selected_raw),
        "candidate_rms_dbfs": candidate_rms,
        "target_rms_dbfs": target_rms,
        "absolute_candidate_minus_target_rms_db": absolute_delta,
        "after_voice_match_candidate_minus_target_rms_db": (
            float(absolute_delta + voice_match_gain_db)
            if absolute_delta is not None
            else None
        ),
        "candidate_median_frame_dbfs": float(np.median(candidate_levels)),
        "target_median_frame_dbfs": float(np.median(target_levels)),
        "after_voice_match_candidate_minus_target_median_db": float(
            np.median(candidate_levels)
            + voice_match_gain_db
            - np.median(target_levels)
        ),
        "log_envelope_correlation": corr_log(
            candidate_env[frame_mask], target_env[frame_mask]
        ),
    }
    if include_spectrum:
        result["spectral_after_voice_match"] = spectrum_for_mask(
            candidate,
            target,
            sample_mask,
            rate,
            voice_match_gain_db,
        )
    return result


def focus_metrics(
    manifest: dict[str, Any],
    common_start: int,
    raw: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    raw_env: np.ndarray,
    candidate_env: np.ndarray,
    target_env: np.ndarray,
    frame_times: np.ndarray,
    rate: int,
    voice_match_gain_db: float,
) -> dict[str, Any]:
    config = manifest["analysis"]["focus"]
    start = float(config["start_seconds"])
    stop = float(config["stop_seconds"])
    window_seconds = float(config["window_ms"]) / 1000.0
    rows = []
    position = start
    while position < stop - 1.0e-9:
        end = min(position + window_seconds, stop)
        left = max(0, int(round(position * rate)) - common_start)
        right = min(raw.size, int(round(end * rate)) - common_start)
        if right <= left:
            rows.append(
                {
                    "start_seconds": position,
                    "stop_seconds": end,
                    "samples": 0,
                }
            )
        else:
            raw_level = rms_db(raw[left:right])
            candidate_level = rms_db(candidate[left:right])
            target_level = rms_db(target[left:right])
            production_delta = float(candidate_level - target_level)
            rows.append(
                {
                    "start_seconds": position,
                    "stop_seconds": end,
                    "samples": right - left,
                    "raw_rms_dbfs": raw_level,
                    "candidate_rms_dbfs": candidate_level,
                    "target_rms_dbfs": target_level,
                    "candidate_minus_target_production_db": production_delta,
                    "candidate_minus_target_after_voice_match_db": float(
                        production_delta + voice_match_gain_db
                    ),
                    "candidate_gain_over_raw_db": float(candidate_level - raw_level),
                    "target_gain_over_raw_db": float(target_level - raw_level),
                }
            )
        position = end

    focus_frames = interval_frame_mask(frame_times, start, stop)
    raw_levels = 20.0 * np.log10(np.maximum(raw_env, EPS))
    speech_frames = focus_frames & (
        raw_levels >= float(config["raw_speech_threshold_dbfs"])
    )
    candidate_gain = (
        20.0 * np.log10(np.maximum(candidate_env[speech_frames], EPS))
        - raw_levels[speech_frames]
    )
    target_gain = (
        20.0 * np.log10(np.maximum(target_env[speech_frames], EPS))
        - raw_levels[speech_frames]
    )

    def spread(values: np.ndarray) -> dict[str, Any]:
        if not values.size:
            return {"frames": 0, "p10_db": None, "p90_db": None, "p90_p10_db": None}
        p10 = float(np.quantile(values, 0.10))
        p90 = float(np.quantile(values, 0.90))
        return {
            "frames": int(values.size),
            "p10_db": p10,
            "median_db": float(np.median(values)),
            "p90_db": p90,
            "p90_p10_db": p90 - p10,
        }

    deltas = [
        abs(float(row["candidate_minus_target_after_voice_match_db"]))
        for row in rows
        if row.get("samples", 0) > 0
    ]
    return {
        "start_seconds": start,
        "stop_seconds": stop,
        "window_ms": float(config["window_ms"]),
        "voice_match_gain_db_applied_for_comparison": voice_match_gain_db,
        "expected_windows": int(round((stop - start) / window_seconds)),
        "measured_windows": len(deltas),
        "max_abs_window_delta_after_voice_match_db": max(deltas) if deltas else None,
        "mean_abs_window_delta_after_voice_match_db": (
            float(np.mean(deltas)) if deltas else None
        ),
        "candidate_gain_over_raw": spread(candidate_gain),
        "target_gain_over_raw": spread(target_gain),
        "rows": rows,
    }


def common_silence_metrics(
    manifest: dict[str, Any],
    frame_times: np.ndarray,
    raw: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    raw_env: np.ndarray,
    candidate_env: np.ndarray,
    target_env: np.ndarray,
    frame_size: int,
    rate: int,
    voice_match_gain_db: float,
) -> dict[str, Any]:
    records = []
    union = np.zeros(frame_times.size, dtype=bool)
    for interval in manifest["analysis"]["common_silences"]:
        mask = interval_frame_mask(
            frame_times,
            float(interval["start_seconds"]),
            float(interval["stop_seconds"]),
        )
        union |= mask
        summary = summarize_mask(
            str(interval["name"]),
            mask,
            raw,
            candidate,
            target,
            raw_env,
            candidate_env,
            target_env,
            frame_size,
            rate,
            voice_match_gain_db,
        )
        summary["start_seconds"] = float(interval["start_seconds"])
        summary["stop_seconds"] = float(interval["stop_seconds"])
        records.append(summary)
    aggregate = summarize_mask(
        "ALL_COMMON_SILENCES",
        union,
        raw,
        candidate,
        target,
        raw_env,
        candidate_env,
        target_env,
        frame_size,
        rate,
        voice_match_gain_db,
        include_spectrum=True,
    )
    rms_values = [
        abs(float(item["after_voice_match_candidate_minus_target_rms_db"]))
        for item in records
        if item.get("after_voice_match_candidate_minus_target_rms_db") is not None
    ]
    median_values = [
        abs(float(item["after_voice_match_candidate_minus_target_median_db"]))
        for item in records
        if item.get("after_voice_match_candidate_minus_target_median_db") is not None
    ]
    return {
        "intervals": records,
        "aggregate": aggregate,
        "max_abs_interval_rms_delta_after_voice_match_db": (
            max(rms_values) if len(rms_values) == len(records) else None
        ),
        "max_abs_interval_median_delta_after_voice_match_db": (
            max(median_values) if len(median_values) == len(records) else None
        ),
    }


def tail_metrics(
    manifest: dict[str, Any],
    frame_times: np.ndarray,
    active: np.ndarray,
    raw: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    raw_env: np.ndarray,
    candidate_env: np.ndarray,
    target_env: np.ndarray,
    frame_size: int,
    rate: int,
    voice_match_gain_db: float,
) -> dict[str, Any]:
    config = manifest["analysis"]["tails"]
    start_frames = int(round(float(config["start_after_speech_ms"]) / 1000.0 * rate / frame_size))
    stop_frames = int(round(float(config["stop_after_speech_ms"]) / 1000.0 * rate / frame_size))
    union = np.zeros(active.size, dtype=bool)
    events = []
    edges = np.flatnonzero(active[:-1] & ~active[1:])
    for index, edge in enumerate(edges, start=1):
        left = min(active.size, edge + 1 + start_frames)
        right = min(active.size, edge + 1 + stop_frames)
        mask = np.zeros(active.size, dtype=bool)
        if left < right:
            mask[left:right] = ~active[left:right]
        if not np.any(mask):
            continue
        union |= mask
        events.append(
            {
                "name": f"TAIL_{index}",
                "speech_end_seconds": float(frame_times[edge] + frame_size / (2.0 * rate)),
                "selected_frames": int(np.count_nonzero(mask)),
            }
        )
    aggregate = summarize_mask(
        "POST_SPEECH_TAILS",
        union,
        raw,
        candidate,
        target,
        raw_env,
        candidate_env,
        target_env,
        frame_size,
        rate,
        voice_match_gain_db,
        include_spectrum=True,
    )
    return {
        "definition_ms": [
            float(config["start_after_speech_ms"]),
            float(config["stop_after_speech_ms"]),
        ],
        "events": events,
        "aggregate": aggregate,
    }


def le_abs(value: Any, limit: float) -> bool:
    return value is not None and abs(float(value)) <= limit


def ge(value: Any, limit: float) -> bool:
    return value is not None and float(value) >= limit


def analyze_candidate(
    manifest: dict[str, Any],
    candidate_entry: dict[str, Any],
    candidate_path: Path,
    candidate_wav: base.PcmWav,
    candidate_provenance: dict[str, Any],
    raw_audio: np.ndarray,
    target_audio: np.ndarray,
    raw_lag: int,
    raw_alignment_corr: float,
    target_audit: dict[str, Any],
) -> dict[str, Any]:
    analysis = manifest["analysis"]
    gates_config = manifest["gates"]
    rate = int(analysis["sample_rate"])
    frame_size = int(round(rate * float(analysis["frame_ms"]) / 1000.0))
    max_lag = int(round(rate * float(analysis["max_lag_ms"]) / 1000.0))
    candidate_full = analysis_mono(candidate_wav, rate)
    candidate_lag, candidate_alignment_corr = base.best_placement_lag(
        target_audio, candidate_full, max_lag
    )
    target, raw, candidate, common_start, common_stop = base.common_overlap(
        target_audio,
        raw_audio,
        candidate_full,
        raw_lag,
        candidate_lag,
    )
    frame_count = min(raw.size, candidate.size, target.size) // frame_size
    common_length = frame_count * frame_size
    raw = raw[:common_length]
    candidate = candidate[:common_length]
    target = target[:common_length]
    raw_env = base.frame_rms(raw, frame_size)[:frame_count]
    candidate_env = base.frame_rms(candidate, frame_size)[:frame_count]
    target_env = base.frame_rms(target, frame_size)[:frame_count]
    raw_levels = 20.0 * np.log10(np.maximum(raw_env, EPS))
    active = raw_levels >= float(analysis["active_raw_threshold_dbfs"])
    active_samples = frame_mask_to_samples(active, frame_size, common_length)
    candidate_active_rms = rms_db(candidate[active_samples])
    target_active_rms = rms_db(target[active_samples])
    active_delta = float(candidate_active_rms - target_active_rms)
    voice_match_gain_db = -active_delta
    frame_times = (
        common_start / rate
        + (np.arange(frame_count, dtype=np.float64) + 0.5) * frame_size / rate
    )

    candidate_audit = source_audit(
        candidate_wav,
        candidate_path,
        candidate_provenance,
        int(analysis["true_peak_oversample"]),
    )
    candidate_lufs = candidate_audit["loudness"]["native_channels"]["integrated_lufs"]
    target_lufs = target_audit["loudness"]["native_channels"]["integrated_lufs"]
    loudness_delta = (
        float(candidate_lufs - target_lufs)
        if candidate_lufs is not None and target_lufs is not None
        else None
    )
    candidate_peak = max_channel_peak_db(candidate_audit)
    target_peak = max_channel_peak_db(target_audit)
    peak_delta = (
        float(candidate_peak - target_peak)
        if candidate_peak is not None and target_peak is not None
        else None
    )

    focus = focus_metrics(
        manifest,
        common_start,
        raw,
        candidate,
        target,
        raw_env,
        candidate_env,
        target_env,
        frame_times,
        rate,
        voice_match_gain_db,
    )
    silences = common_silence_metrics(
        manifest,
        frame_times,
        raw,
        candidate,
        target,
        raw_env,
        candidate_env,
        target_env,
        frame_size,
        rate,
        voice_match_gain_db,
    )
    tails = tail_metrics(
        manifest,
        frame_times,
        active,
        raw,
        candidate,
        target,
        raw_env,
        candidate_env,
        target_env,
        frame_size,
        rate,
        voice_match_gain_db,
    )
    active_spectrum = spectrum_for_mask(
        candidate,
        target,
        active_samples,
        rate,
        voice_match_gain_db,
    )
    global_envelope_corr = corr_log(candidate_env, target_env)
    active_envelope_corr = corr_log(candidate_env[active], target_env[active])
    tail_aggregate = tails["aggregate"]
    gate_values = {
        "focus_windows": le_abs(
            focus["max_abs_window_delta_after_voice_match_db"],
            float(gates_config["focus_window_max_abs_delta_db"]),
        ),
        "focus_gain_spread": le_abs(
            focus["candidate_gain_over_raw"]["p90_p10_db"],
            float(gates_config["focus_gain_spread_p90_p10_db"]),
        ),
        "active_rms": le_abs(active_delta, float(gates_config["active_rms_abs_delta_db"])),
        "integrated_loudness": le_abs(
            loudness_delta,
            float(gates_config["integrated_loudness_abs_delta_lu"]),
        ),
        "sample_peak_delta": le_abs(
            peak_delta, float(gates_config["sample_peak_abs_delta_db"])
        ),
        "sample_peak_ceiling": (
            candidate_peak is not None
            and candidate_peak
            <= float(gates_config["sample_peak_ceiling_dbfs_with_quantization_tolerance"])
        ),
        "common_silence_each_rms": le_abs(
            silences["max_abs_interval_rms_delta_after_voice_match_db"],
            float(gates_config["common_silence_each_abs_rms_delta_db"]),
        ),
        "common_silence_each_median": le_abs(
            silences["max_abs_interval_median_delta_after_voice_match_db"],
            float(gates_config["common_silence_each_abs_median_delta_db"]),
        ),
        "tails_rms": le_abs(
            tail_aggregate.get("after_voice_match_candidate_minus_target_rms_db"),
            float(gates_config["tail_abs_rms_delta_db"]),
        ),
        "tails_median": le_abs(
            tail_aggregate.get("after_voice_match_candidate_minus_target_median_db"),
            float(gates_config["tail_abs_median_delta_db"]),
        ),
        "tails_envelope": ge(
            tail_aggregate.get("log_envelope_correlation"),
            float(gates_config["tail_envelope_correlation_min"]),
        ),
        "active_spectrum": le_abs(
            active_spectrum.get("mean_absolute_error_db"),
            float(gates_config["active_spectral_mae_db"]),
        ),
        "global_envelope": ge(
            global_envelope_corr,
            float(gates_config["global_envelope_correlation_min"]),
        ),
    }
    passed = sum(gate_values.values())
    failed = [name for name, value in gate_values.items() if not value]
    return {
        "id": candidate_entry["id"],
        "label": candidate_entry["label"],
        "expected_chain": candidate_entry["expected_chain"],
        "status": "analyzed",
        "source_path": str(candidate_path),
        "file_audit": candidate_audit,
        "alignment": {
            "raw_to_target_lag_ms": raw_lag * 1000.0 / rate,
            "raw_to_target_correlation": raw_alignment_corr,
            "candidate_to_target_lag_ms": candidate_lag * 1000.0 / rate,
            "candidate_to_target_correlation": candidate_alignment_corr,
            "common_start_seconds_on_target": common_start / rate,
            "common_stop_seconds_on_target": common_stop / rate,
            "analyzed_duration_seconds": common_length / rate,
        },
        "delivery": {
            "candidate_integrated_lufs_native_channels": candidate_lufs,
            "target_integrated_lufs_native_channels": target_lufs,
            "candidate_minus_target_integrated_lu": loudness_delta,
            "candidate_sample_peak_dbfs": candidate_peak,
            "target_sample_peak_dbfs": target_peak,
            "candidate_minus_target_sample_peak_db": peak_delta,
            "candidate_true_peak_approx_dbtp": max_channel_true_peak_db(candidate_audit),
            "target_true_peak_approx_dbtp": max_channel_true_peak_db(target_audit),
        },
        "voice": {
            "selection_basis": f"raw 20 ms >= {analysis['active_raw_threshold_dbfs']} dBFS",
            "active_frames": int(np.count_nonzero(active)),
            "active_seconds": float(np.count_nonzero(active) * frame_size / rate),
            "candidate_active_rms_dbfs": candidate_active_rms,
            "target_active_rms_dbfs": target_active_rms,
            "candidate_minus_target_active_rms_db": active_delta,
            "voice_match_gain_db_used_only_for_similarity_metrics": voice_match_gain_db,
            "active_log_envelope_correlation": active_envelope_corr,
            "global_log_envelope_correlation": global_envelope_corr,
            "active_spectrum_after_voice_match": active_spectrum,
        },
        "focus_5p8_6p8": focus,
        "common_silences": silences,
        "tails_100_600ms": tails,
        "gates": gate_values,
        "gate_summary": {
            "passed": passed,
            "total": len(gate_values),
            "failed": failed,
            "eligible": not failed,
        },
    }


def normalized_abs(value: Any, limit: float, missing: float = 10.0) -> float:
    return abs(float(value)) / limit if value is not None else missing


def rank_candidates(candidates: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    gates = manifest["gates"]
    ranked = []
    for source_order, item in enumerate(candidates):
        if item["status"] != "analyzed":
            continue
        focus = item["focus_5p8_6p8"]
        silence = item["common_silences"]
        tail = item["tails_100_600ms"]["aggregate"]
        voice = item["voice"]
        delivery = item["delivery"]
        gate_summary = item["gate_summary"]
        preservation_names = (
            "focus_windows",
            "focus_gain_spread",
            "active_rms",
            "tails_rms",
            "tails_median",
            "tails_envelope",
        )
        preservation_failures = sum(
            not item["gates"][name] for name in preservation_names
        )
        tail_corr = tail.get("log_envelope_correlation")
        global_corr = voice.get("global_log_envelope_correlation")
        score = (
            5.0
            * normalized_abs(
                focus.get("max_abs_window_delta_after_voice_match_db"),
                float(gates["focus_window_max_abs_delta_db"]),
            )
            + 2.0
            * normalized_abs(
                focus["candidate_gain_over_raw"].get("p90_p10_db"),
                float(gates["focus_gain_spread_p90_p10_db"]),
            )
            + 3.0
            * normalized_abs(
                silence.get("max_abs_interval_rms_delta_after_voice_match_db"),
                float(gates["common_silence_each_abs_rms_delta_db"]),
            )
            + 3.0
            * normalized_abs(
                silence.get("max_abs_interval_median_delta_after_voice_match_db"),
                float(gates["common_silence_each_abs_median_delta_db"]),
            )
            + 2.0
            * normalized_abs(
                tail.get("after_voice_match_candidate_minus_target_rms_db"),
                float(gates["tail_abs_rms_delta_db"]),
            )
            + normalized_abs(
                tail.get("after_voice_match_candidate_minus_target_median_db"),
                float(gates["tail_abs_median_delta_db"]),
            )
            + 2.0
            * max(
                0.0,
                (
                    float(gates["tail_envelope_correlation_min"])
                    - (float(tail_corr) if tail_corr is not None else 0.0)
                )
                / max(1.0 - float(gates["tail_envelope_correlation_min"]), EPS),
            )
            + normalized_abs(
                voice.get("candidate_minus_target_active_rms_db"),
                float(gates["active_rms_abs_delta_db"]),
            )
            + normalized_abs(
                delivery.get("candidate_minus_target_integrated_lu"),
                float(gates["integrated_loudness_abs_delta_lu"]),
            )
            + normalized_abs(
                delivery.get("candidate_minus_target_sample_peak_db"),
                float(gates["sample_peak_abs_delta_db"]),
            )
            + normalized_abs(
                voice["active_spectrum_after_voice_match"].get("mean_absolute_error_db"),
                float(gates["active_spectral_mae_db"]),
            )
            + max(
                0.0,
                (
                    float(gates["global_envelope_correlation_min"])
                    - (float(global_corr) if global_corr is not None else 0.0)
                )
                / max(1.0 - float(gates["global_envelope_correlation_min"]), EPS),
            )
        )
        item["ranking"] = {
            "score_lower_is_better": float(score),
            "preservation_failures": int(preservation_failures),
            "all_gate_failures": len(gate_summary["failed"]),
        }
        key = (
            preservation_failures > 0,
            preservation_failures,
            len(gate_summary["failed"]),
            normalized_abs(
                focus.get("max_abs_window_delta_after_voice_match_db"),
                float(gates["focus_window_max_abs_delta_db"]),
            ),
            max(
                normalized_abs(
                    silence.get("max_abs_interval_rms_delta_after_voice_match_db"),
                    float(gates["common_silence_each_abs_rms_delta_db"]),
                ),
                normalized_abs(
                    silence.get("max_abs_interval_median_delta_after_voice_match_db"),
                    float(gates["common_silence_each_abs_median_delta_db"]),
                ),
            ),
            score,
            source_order,
        )
        ranked.append((key, item))
    ranked.sort(key=lambda pair: pair[0])
    result = []
    for rank, (_key, item) in enumerate(ranked, start=1):
        item["ranking"]["rank"] = rank
        result.append(item)
    return result


def fmt(value: Any, decimals: int = 2, signed: bool = False) -> str:
    if value is None:
        return "n/d"
    prefix = "+" if signed and float(value) >= 0 else ""
    return f"{prefix}{float(value):.{decimals}f}"


def markdown_report(result: dict[str, Any]) -> str:
    analyzed = [item for item in result["candidates"] if item["status"] == "analyzed"]
    missing = [item for item in result["candidates"] if item["status"] == "missing"]
    lines = [
        "# Grade final automatica — take 95",
        "",
        "Analise restrita ao take DJI 95. Valores de nivel sao candidato menos CapCut;",
        "as metricas de fundo, caudas, frase e espectro usam um unico casamento global",
        "de voz apenas para avaliacao. O WAV exportado nao e alterado.",
        "",
        "## Ranking",
        "",
        "| Rank | ID | Elegivel | Gates | Frase max. | Spread ganho | Voz | LUFS | Pico | Silencio RMS/med. | Cauda RMS/med./corr. | MAE espectral | Score |",
        "|---:|---|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in sorted(analyzed, key=lambda value: value["ranking"]["rank"]):
        focus = item["focus_5p8_6p8"]
        silence = item["common_silences"]
        tail = item["tails_100_600ms"]["aggregate"]
        voice = item["voice"]
        delivery = item["delivery"]
        summary = item["gate_summary"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item["ranking"]["rank"]),
                    str(item["id"]),
                    "sim" if summary["eligible"] else "nao",
                    f"{summary['passed']}/{summary['total']}",
                    fmt(focus["max_abs_window_delta_after_voice_match_db"]),
                    fmt(focus["candidate_gain_over_raw"]["p90_p10_db"]),
                    fmt(voice["candidate_minus_target_active_rms_db"], signed=True),
                    fmt(delivery["candidate_minus_target_integrated_lu"], signed=True),
                    fmt(delivery["candidate_minus_target_sample_peak_db"], signed=True),
                    fmt(silence["max_abs_interval_rms_delta_after_voice_match_db"])
                    + "/"
                    + fmt(silence["max_abs_interval_median_delta_after_voice_match_db"]),
                    fmt(tail.get("after_voice_match_candidate_minus_target_rms_db"), signed=True)
                    + "/"
                    + fmt(tail.get("after_voice_match_candidate_minus_target_median_db"), signed=True)
                    + "/"
                    + fmt(tail.get("log_envelope_correlation"), 3),
                    fmt(voice["active_spectrum_after_voice_match"]["mean_absolute_error_db"]),
                    fmt(item["ranking"]["score_lower_is_better"]),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Veredito", "", result["decision"]["message"], ""])
    if missing:
        lines.extend(
            [
                "Exports ainda ausentes: " + ", ".join(item["id"] for item in missing) + ".",
                "O ranking permanece provisório ate os quatro candidatos existirem.",
                "",
            ]
        )
    for item in sorted(analyzed, key=lambda value: value["ranking"]["rank"]):
        lines.extend(
            [
                f"## {item['id']} — {item['label']}",
                "",
                f"Arquivo: `{item['source_path']}`",
                "",
                "Gates que falharam: "
                + (", ".join(item["gate_summary"]["failed"]) or "nenhum")
                + ".",
                "",
                "### Frase 5,8–6,8 s (janelas de 200 ms)",
                "",
                "| Janela | Producao | Depois de casar voz |",
                "|---|---:|---:|",
            ]
        )
        for row in item["focus_5p8_6p8"]["rows"]:
            lines.append(
                f"| {row['start_seconds']:.1f}–{row['stop_seconds']:.1f} s | "
                + fmt(row.get("candidate_minus_target_production_db"), signed=True)
                + " dB | "
                + fmt(row.get("candidate_minus_target_after_voice_match_db"), signed=True)
                + " dB |"
            )
        lines.extend(
            [
                "",
                "### Silencios comuns",
                "",
                "| Regiao | RMS depois de casar voz | Mediana depois de casar voz |",
                "|---|---:|---:|",
            ]
        )
        for record in item["common_silences"]["intervals"]:
            lines.append(
                f"| {record['name']} ({record['start_seconds']:.2f}–{record['stop_seconds']:.2f} s) | "
                + fmt(record.get("after_voice_match_candidate_minus_target_rms_db"), signed=True)
                + " dB | "
                + fmt(record.get("after_voice_match_candidate_minus_target_median_db"), signed=True)
                + " dB |"
            )
        lines.append("")
    lines.extend(
        [
            "## Criterio de ranking",
            "",
            "A ordem elimina primeiro desvios na frase fraca, voz e caudas. Depois",
            "prioriza os silencios comuns, entrega e espectro. Score menor e melhor;",
            "ele nao transforma um candidato com gate falho em aprovado.",
            "",
            "A aprovacao perceptual (pumping, musical noise, respirações e finais",
            "cortados) continua obrigatoria depois deste filtro objetivo.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_overrides(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--candidate exige ID=/caminho/95_arquivo.wav")
        candidate_id, path = value.split("=", 1)
        candidate_id = candidate_id.upper()
        if candidate_id not in EXPECTED_IDS:
            raise ValueError(f"ID de candidato invalido: {candidate_id}")
        result[candidate_id] = path
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="Sobrescreve N0, DN35, DN50 ou EXP sem editar o manifesto.",
    )
    parser.add_argument("--require-all", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise SystemExit("schema_version do manifesto nao suportado")
    if manifest.get("scope", {}).get("clip_id") != 95:
        raise SystemExit("falha de escopo: manifesto precisa ser exclusivamente do ID 95")
    ids = tuple(item.get("id") for item in manifest.get("candidates", []))
    if ids != EXPECTED_IDS:
        raise SystemExit(f"grade precisa ser exatamente {EXPECTED_IDS}; recebida {ids}")
    overrides = parse_overrides(args.candidate)
    raw_path = resolve_path(manifest["inputs"]["raw_paths"], "raw")
    target_path = resolve_path(manifest["inputs"]["target_paths"], "target")
    if raw_path is None or target_path is None:
        raise SystemExit("raw ou alvo 95 nao encontrado nos caminhos do manifesto")

    with tempfile.TemporaryDirectory(prefix="grade-final-95-") as temporary_name:
        temporary = Path(temporary_name)
        raw_wav, raw_provenance = decode_audio(raw_path, temporary)
        target_wav, target_provenance = decode_audio(target_path, temporary)
        analysis = manifest["analysis"]
        rate = int(analysis["sample_rate"])
        true_peak_factor = int(analysis["true_peak_oversample"])
        raw_audit = source_audit(
            raw_wav, raw_path, raw_provenance, true_peak_factor
        )
        target_audit = source_audit(
            target_wav, target_path, target_provenance, true_peak_factor
        )
        raw_audio = analysis_mono(raw_wav, rate)
        target_audio = analysis_mono(target_wav, rate)
        max_lag = int(round(rate * float(analysis["max_lag_ms"]) / 1000.0))
        raw_lag, raw_alignment_corr = base.best_placement_lag(
            target_audio, raw_audio, max_lag
        )
        candidates = []
        for entry in manifest["candidates"]:
            options = [overrides[entry["id"]]] if entry["id"] in overrides else entry["paths"]
            path = resolve_path(options, f"candidate {entry['id']}")
            if path is None:
                candidates.append(
                    {
                        "id": entry["id"],
                        "label": entry["label"],
                        "expected_chain": entry["expected_chain"],
                        "status": "missing",
                        "searched_paths": options,
                    }
                )
                continue
            candidate_wav, candidate_provenance = decode_audio(path, temporary)
            candidates.append(
                analyze_candidate(
                    manifest,
                    entry,
                    path,
                    candidate_wav,
                    candidate_provenance,
                    raw_audio,
                    target_audio,
                    raw_lag,
                    raw_alignment_corr,
                    target_audit,
                )
            )

        ranked = rank_candidates(candidates, manifest)
        rank_by_id = {item["id"]: item for item in ranked}
        candidates = [rank_by_id.get(item["id"], item) for item in candidates]
        missing_ids = [item["id"] for item in candidates if item["status"] == "missing"]
        winner = ranked[0] if ranked else None
        complete = not missing_ids
        if winner is None:
            message = "Nenhum export candidato foi encontrado; nao ha ranking."
        elif not complete:
            message = (
                f"Ranking provisorio: {winner['id']} esta em primeiro entre os exports presentes, "
                f"mas faltam {', '.join(missing_ids)}."
            )
        elif winner["gate_summary"]["eligible"]:
            message = (
                f"Vencedor objetivo: {winner['id']}; passou todos os gates automaticos. "
                "Falta somente a escuta A/B perceptual obrigatoria."
            )
        else:
            message = (
                f"Melhor relativo: {winner['id']}, mas nenhum candidato passou todos os gates; "
                "nao congelar preset como equivalente ao CapCut."
            )
        result = base.sanitize_json(
            {
                "schema_version": 1,
                "scope": manifest["scope"],
                "manifest_path": str(args.manifest.resolve()),
                "inputs": {
                    "raw": raw_audit,
                    "target": target_audit,
                },
                "analysis_configuration": manifest["analysis"],
                "gate_configuration": manifest["gates"],
                "candidates": candidates,
                "ranking_order": [item["id"] for item in ranked],
                "decision": {
                    "complete_grid": complete,
                    "missing_candidate_ids": missing_ids,
                    "winner_id": winner["id"] if winner else None,
                    "winner_eligible": (
                        winner["gate_summary"]["eligible"] if winner else False
                    ),
                    "message": message,
                    "perceptual_ab_still_required": True,
                },
                "safety": {
                    "audio_modified": False,
                    "only_clip_id_read": 95,
                    "blind_targets_read": False,
                    "clip_ids_below_64_read": False,
                },
            }
        )

    output_dir = args.output_dir
    if output_dir is None:
        output_dir = NATIVE_ROOT / manifest["output"]["directory"]
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / manifest["output"]["json"]
    markdown_path = output_dir / manifest["output"]["markdown"]
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(markdown_report(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(json_path),
                "markdown": str(markdown_path),
                "ranking_order": result["ranking_order"],
                "decision": result["decision"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.require_all and not result["decision"]["complete_grid"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
