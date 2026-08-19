#!/usr/bin/env python3
"""Mede somente os dez pares de calibracao CRU -> CapCut congelados.

As entradas sao dumps mono float32 ja decodificados, usados apenas em leitura.
Nenhum ID do conjunto cego aparece neste programa.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = ROOT / "premiere_audition_native_preset" / "analises"
OUTPUT_DIR = ANALYSIS_DIR / "resultados" / "perfil_calibracao"
sys.path.insert(0, str(ANALYSIS_DIR))

import comparar_wavs_adobe as adobe  # noqa: E402


RAW_RATE = 48_000
TARGET_RATE = 44_100
ANALYSIS_RATE = 48_000
PAIR_PATHS = {
    "65": (
        ROOT / "analysis_capcut_pairs/decoded/raw/65.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/65.f32",
    ),
    "66": (
        ROOT / "analysis_capcut_pairs/decoded/candidates/66_base.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/66.f32",
    ),
    "67": (
        ROOT / "analysis_capcut_pairs/decoded/raw/67.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/67.f32",
    ),
    "68": (
        ROOT / "analysis_capcut_pairs/decoded/candidates/68_montagem.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/68.f32",
    ),
    "71": (
        ROOT / "analysis_capcut_pairs/decoded/raw/71.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/71.f32",
    ),
    "72": (
        ROOT / "analysis_capcut_pairs/decoded/candidates/72_base.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/72.f32",
    ),
    "74": (
        ROOT / "analysis_capcut_pairs/decoded/candidates/74_montagem.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/74.f32",
    ),
    "76": (
        ROOT / "analysis_capcut_pairs/decoded/raw/76.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/76.f32",
    ),
    "81": (
        ROOT / "analysis_capcut_pairs/decoded/candidates/81_base.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/81.f32",
    ),
    "84": (
        ROOT / "analysis_capcut_pairs/decoded/candidates/84_base.f32",
        ROOT / "analysis_capcut_pairs/decoded/treated/84.f32",
    ),
}


def read_f32(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype="<f4")
    if not data.size or not np.all(np.isfinite(data)):
        raise ValueError(f"audio float32 invalido: {path}")
    return data


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def aligned_overlap(
    target: np.ndarray, raw: np.ndarray, lag: int
) -> tuple[np.ndarray, np.ndarray]:
    start = max(0, lag)
    stop = min(target.size, lag + raw.size)
    if stop - start < ANALYSIS_RATE:
        raise ValueError("sobreposicao alinhada curta demais")
    return target[start:stop], raw[start - lag : stop - lag]


def repeated_mask(frame_mask: np.ndarray, frame_size: int, length: int) -> np.ndarray:
    return np.repeat(frame_mask, frame_size)[:length]


def db_levels(envelope: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(envelope[mask], adobe.EPS))


def quantiles(values: np.ndarray) -> dict[str, float]:
    return {
        "p10": float(np.quantile(values, 0.10)),
        "p25": float(np.quantile(values, 0.25)),
        "median": float(np.median(values)),
        "p75": float(np.quantile(values, 0.75)),
        "p90": float(np.quantile(values, 0.90)),
    }


def compression_slope(raw_db: np.ndarray, target_db: np.ndarray) -> float:
    if raw_db.size < 3:
        return float("nan")
    x = raw_db - float(np.mean(raw_db))
    y = target_db - float(np.mean(target_db))
    denominator = float(np.dot(x, x))
    return float(np.dot(x, y) / denominator) if denominator > adobe.EPS else 0.0


def measure_pair(pair_id: str, raw_path: Path, target_path: Path) -> dict:
    raw_native = read_f32(raw_path)
    target_native = read_f32(target_path)

    raw = raw_native.astype(np.float32, copy=False)
    target = adobe.sinc_resample(target_native, TARGET_RATE, ANALYSIS_RATE)
    lag, alignment_correlation = adobe.best_placement_lag(
        target, raw, ANALYSIS_RATE
    )
    target, raw = aligned_overlap(target, raw, lag)

    masks, selection = adobe.selection_masks(
        raw,
        ANALYSIS_RATE,
        frame_ms=20.0,
        edge_ms=300.0,
        quiet_quantile=0.25,
        active_quantile=0.65,
    )
    frame_size = int(selection["frame_samples"])
    active_samples = repeated_mask(masks["active"], frame_size, raw.size)
    quiet_samples = repeated_mask(masks["quiet"], frame_size, raw.size)
    eligible_samples = repeated_mask(masks["eligible"], frame_size, raw.size)

    raw_env = adobe.frame_rms(raw, frame_size)
    target_env = adobe.frame_rms(target, frame_size)
    frame_count = min(raw_env.size, target_env.size, masks["eligible"].size)
    raw_env = raw_env[:frame_count]
    target_env = target_env[:frame_count]
    eligible_frames = masks["eligible"][:frame_count]
    active_frames = masks["active"][:frame_count]
    quiet_frames = masks["quiet"][:frame_count]

    raw_active_rms = adobe.rms(raw[active_samples])
    target_active_rms = adobe.rms(target[active_samples])
    raw_quiet_rms = adobe.rms(raw[quiet_samples])
    target_quiet_rms = adobe.rms(target[quiet_samples])
    active_gain = adobe.amp_db(target_active_rms / max(raw_active_rms, adobe.EPS))
    quiet_gain = adobe.amp_db(target_quiet_rms / max(raw_quiet_rms, adobe.EPS))

    raw_active_db = db_levels(raw_env, active_frames)
    target_active_db = db_levels(target_env, active_frames)
    raw_quiet_db = db_levels(raw_env, quiet_frames)
    target_quiet_db = db_levels(target_env, quiet_frames)
    active_median_gain = float(np.median(target_active_db) - np.median(raw_active_db))
    quiet_median_gain = float(np.median(target_quiet_db) - np.median(raw_quiet_db))

    raw_active_power, raw_active_windows = adobe.accumulated_spectrum(
        raw, active_samples
    )
    target_active_power, target_active_windows = adobe.accumulated_spectrum(
        target, active_samples
    )
    active_spectrum = adobe.spectral_comparison(
        target_active_power,
        raw_active_power,
        ANALYSIS_RATE,
        match_gain_db=-active_gain,
        candidate_windows=target_active_windows,
        reference_windows=raw_active_windows,
    )

    raw_quiet_power, raw_quiet_windows = adobe.accumulated_spectrum(
        raw, quiet_samples
    )
    target_quiet_power, target_quiet_windows = adobe.accumulated_spectrum(
        target, quiet_samples
    )
    quiet_spectrum = adobe.spectral_comparison(
        target_quiet_power,
        raw_quiet_power,
        ANALYSIS_RATE,
        match_gain_db=-active_gain,
        candidate_windows=target_quiet_windows,
        reference_windows=raw_quiet_windows,
    )

    raw_loudness = adobe.mono_loudness(raw_native, RAW_RATE)
    target_loudness = adobe.mono_loudness(target_native, TARGET_RATE)
    raw_lufs = raw_loudness["integrated_lufs"]
    target_lufs = target_loudness["integrated_lufs"]

    raw_peak = float(np.max(np.abs(raw_native)))
    target_peak = float(np.max(np.abs(target_native)))
    raw_true_peak = adobe.approximate_true_peak_channel(raw_native, factor=4)
    target_true_peak = adobe.approximate_true_peak_channel(target_native, factor=4)
    raw_rms = adobe.rms(raw_native)
    target_rms = adobe.rms(target_native)

    eligible_raw_db = db_levels(raw_env, eligible_frames)
    eligible_target_db = db_levels(target_env, eligible_frames)
    mid_voice = eligible_raw_db >= float(np.quantile(eligible_raw_db, 0.35))

    return {
        "pair_id": pair_id,
        "files": {
            "raw": {
                "path": str(raw_path.resolve()),
                "sha256": sha256(raw_path),
                "bytes": raw_path.stat().st_size,
                "sample_rate": RAW_RATE,
                "frames": int(raw_native.size),
                "duration_seconds": raw_native.size / RAW_RATE,
            },
            "target_capcut": {
                "path": str(target_path.resolve()),
                "sha256": sha256(target_path),
                "bytes": target_path.stat().st_size,
                "sample_rate": TARGET_RATE,
                "frames": int(target_native.size),
                "duration_seconds": target_native.size / TARGET_RATE,
            },
        },
        "alignment": {
            "lag_samples_at_48k": int(lag),
            "lag_ms": 1000.0 * lag / ANALYSIS_RATE,
            "preemphasized_correlation": alignment_correlation,
            "waveform_correlation": adobe.normalized_corr(raw, target),
            "common_duration_seconds": raw.size / ANALYSIS_RATE,
        },
        "loudness": {
            "raw_integrated_lufs_mono": raw_lufs,
            "target_integrated_lufs_mono": target_lufs,
            "target_minus_raw_lu": float(target_lufs - raw_lufs),
            "target_short_term_median_lufs": target_loudness["short_term"]["median_lufs"],
            "target_short_term_p90_lufs": target_loudness["short_term"]["p90_lufs"],
            "target_short_term_max_lufs": target_loudness["short_term"]["max_lufs"],
        },
        "level_and_peak": {
            "raw_rms_dbfs": adobe.amp_db(raw_rms),
            "target_rms_dbfs": adobe.amp_db(target_rms),
            "target_minus_raw_rms_db": adobe.amp_db(target_rms / max(raw_rms, adobe.EPS)),
            "raw_sample_peak_dbfs": adobe.amp_db(raw_peak),
            "target_sample_peak_dbfs": adobe.amp_db(target_peak),
            "raw_true_peak_4x_dbtp": adobe.amp_db(raw_true_peak),
            "target_true_peak_4x_dbtp": adobe.amp_db(target_true_peak),
            "raw_crest_db": adobe.amp_db(raw_peak / max(raw_rms, adobe.EPS)),
            "target_crest_db": adobe.amp_db(target_peak / max(target_rms, adobe.EPS)),
            "target_clip_fraction_ge_minus_0_01dbfs": float(
                np.mean(np.abs(target_native) >= 10.0 ** (-0.01 / 20.0))
            ),
        },
        "voice_and_dynamics": {
            "active_voice_gain_db": active_gain,
            "active_median_gain_db": active_median_gain,
            "compression_slope_db_per_db": compression_slope(
                eligible_raw_db[mid_voice], eligible_target_db[mid_voice]
            ),
            "target_active_frame_dbfs": quantiles(target_active_db),
            "target_active_dynamic_iqr_db": float(
                np.quantile(target_active_db, 0.75)
                - np.quantile(target_active_db, 0.25)
            ),
            "raw_active_dynamic_iqr_db": float(
                np.quantile(raw_active_db, 0.75) - np.quantile(raw_active_db, 0.25)
            ),
        },
        "noise_and_ambience": {
            "quiet_gain_db": quiet_gain,
            "quiet_rms_after_voice_match_db": quiet_gain - active_gain,
            "quiet_median_after_voice_match_db": quiet_median_gain - active_median_gain,
            "raw_quiet_frame_dbfs": quantiles(raw_quiet_db),
            "target_quiet_frame_dbfs": quantiles(target_quiet_db),
            "target_quiet_dynamic_iqr_db": float(
                np.quantile(target_quiet_db, 0.75) - np.quantile(target_quiet_db, 0.25)
            ),
            "log_envelope_correlation_all": adobe.normalized_corr(
                np.log10(raw_env[eligible_frames] + adobe.EPS),
                np.log10(target_env[eligible_frames] + adobe.EPS),
            ),
            "log_envelope_correlation_active": adobe.normalized_corr(
                np.log10(raw_env[active_frames] + adobe.EPS),
                np.log10(target_env[active_frames] + adobe.EPS),
            ),
            "log_envelope_correlation_quiet": adobe.normalized_corr(
                np.log10(raw_env[quiet_frames] + adobe.EPS),
                np.log10(target_env[quiet_frames] + adobe.EPS),
            ),
        },
        "spectrum_active_target_minus_raw_after_voice_match_db": active_spectrum,
        "spectrum_quiet_target_minus_raw_after_voice_match_db": quiet_spectrum,
        "selection": selection,
    }


def distribution(items: list[dict], getter) -> dict[str, float]:
    values = np.asarray([getter(item) for item in items], dtype=np.float64)
    result = quantiles(values)
    result["min"] = float(np.min(values))
    result["max"] = float(np.max(values))
    result["mean"] = float(np.mean(values))
    return result


def aggregate(items: list[dict]) -> dict:
    scalar_fields = {
        "target_integrated_lufs_mono": lambda x: x["loudness"]["target_integrated_lufs_mono"],
        "target_minus_raw_lu": lambda x: x["loudness"]["target_minus_raw_lu"],
        "target_sample_peak_dbfs": lambda x: x["level_and_peak"]["target_sample_peak_dbfs"],
        "target_true_peak_4x_dbtp": lambda x: x["level_and_peak"]["target_true_peak_4x_dbtp"],
        "target_crest_db": lambda x: x["level_and_peak"]["target_crest_db"],
        "active_voice_gain_db": lambda x: x["voice_and_dynamics"]["active_voice_gain_db"],
        "compression_slope_db_per_db": lambda x: x["voice_and_dynamics"]["compression_slope_db_per_db"],
        "quiet_rms_after_voice_match_db": lambda x: x["noise_and_ambience"]["quiet_rms_after_voice_match_db"],
        "quiet_median_after_voice_match_db": lambda x: x["noise_and_ambience"]["quiet_median_after_voice_match_db"],
        "envelope_correlation_all": lambda x: x["noise_and_ambience"]["log_envelope_correlation_all"],
        "envelope_correlation_active": lambda x: x["noise_and_ambience"]["log_envelope_correlation_active"],
        "envelope_correlation_quiet": lambda x: x["noise_and_ambience"]["log_envelope_correlation_quiet"],
        "target_active_median_dbfs": lambda x: x["voice_and_dynamics"]["target_active_frame_dbfs"]["median"],
        "target_quiet_median_dbfs": lambda x: x["noise_and_ambience"]["target_quiet_frame_dbfs"]["median"],
    }
    result = {name: distribution(items, getter) for name, getter in scalar_fields.items()}
    band_names = list(
        items[0]["spectrum_active_target_minus_raw_after_voice_match_db"]["band_delta_db"]
    )
    result["active_spectrum_band_delta_db"] = {
        band: distribution(
            items,
            lambda x, band=band: x[
                "spectrum_active_target_minus_raw_after_voice_match_db"
            ]["band_delta_db"][band],
        )
        for band in band_names
    }
    result["quiet_spectrum_band_delta_db"] = {
        band: distribution(
            items,
            lambda x, band=band: x[
                "spectrum_quiet_target_minus_raw_after_voice_match_db"
            ]["band_delta_db"][band],
        )
        for band in band_names
    }
    return result


def write_csv(items: list[dict], path: Path) -> None:
    rows = []
    for item in items:
        row = {
            "pair_id": item["pair_id"],
            "target_lufs_mono": item["loudness"]["target_integrated_lufs_mono"],
            "target_minus_raw_lu": item["loudness"]["target_minus_raw_lu"],
            "active_gain_db": item["voice_and_dynamics"]["active_voice_gain_db"],
            "compression_slope": item["voice_and_dynamics"]["compression_slope_db_per_db"],
            "quiet_after_voice_match_db": item["noise_and_ambience"]["quiet_rms_after_voice_match_db"],
            "quiet_median_after_voice_match_db": item["noise_and_ambience"]["quiet_median_after_voice_match_db"],
            "envelope_corr_all": item["noise_and_ambience"]["log_envelope_correlation_all"],
            "envelope_corr_quiet": item["noise_and_ambience"]["log_envelope_correlation_quiet"],
            "target_sample_peak_dbfs": item["level_and_peak"]["target_sample_peak_dbfs"],
            "target_true_peak_4x_dbtp": item["level_and_peak"]["target_true_peak_4x_dbtp"],
            "target_crest_db": item["level_and_peak"]["target_crest_db"],
        }
        for band, value in item[
            "spectrum_active_target_minus_raw_after_voice_match_db"
        ]["band_delta_db"].items():
            row[f"active_{band}_db"] = value
        rows.append(row)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    measurements = [
        measure_pair(pair_id, raw_path, target_path)
        for pair_id, (raw_path, target_path) in PAIR_PATHS.items()
    ]
    result = {
        "schema_version": 1,
        "scope": {
            "pair_ids": list(PAIR_PATHS),
            "excluded_blind_ids": "intentionally not represented",
            "read_only": True,
            "analysis_rate": ANALYSIS_RATE,
            "raw_native_rate": RAW_RATE,
            "target_native_rate": TARGET_RATE,
            "frame_ms": 20.0,
            "quiet_quantile": 0.25,
            "active_quantile": 0.65,
            "edge_exclusion_ms": 300.0,
            "true_peak_method": "sinc interpolation 4x, approximate",
        },
        "pairs": measurements,
        "aggregate": aggregate(measurements),
    }
    json_path = OUTPUT_DIR / "metricas_calibracao.json"
    csv_path = OUTPUT_DIR / "metricas_calibracao.csv"
    files_path = OUTPUT_DIR / "ARQUIVOS_AUDIO_LIDOS.txt"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(measurements, csv_path)
    lines = []
    for pair_id, (raw_path, target_path) in PAIR_PATHS.items():
        lines.append(f"{pair_id}\tCRU\t{raw_path.resolve()}")
        lines.append(f"{pair_id}\tCAPCUT\t{target_path.resolve()}")
    files_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "files": str(files_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
