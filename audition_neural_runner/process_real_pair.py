#!/usr/bin/env python3
"""Process one real raw/CapCut pair with the calibrated offline chain."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from calibrate_dfn_capcut import (
    EPS,
    best_placement_lag,
    compare_candidate,
    db,
    frame_rms,
    mono,
    normalized_corr,
    place_on_reference,
    read_pcm_wav,
    rms,
    run_chain,
    spectral_band_distance,
    write_pcm24_wav,
)


def write_pcm16_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    samples = np.asarray(audio, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    clipped = np.clip(samples, -1.0, np.nextafter(1.0, 0.0))
    integer = np.clip(
        np.rint(clipped * 32768.0), -32768, 32767
    ).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(samples.shape[1])
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(integer.tobytes())


def mask_to_samples(mask: np.ndarray, frame_size: int, length: int) -> np.ndarray:
    return np.repeat(mask, frame_size)[:length]


def selected_metrics(
    raw: np.ndarray,
    candidate: np.ndarray,
    target: np.ndarray,
    mask: np.ndarray,
    frame_size: int,
    sample_rate: int,
) -> dict[str, object]:
    sample_mask = mask_to_samples(mask, frame_size, target.size)
    if not np.any(sample_mask):
        return {}
    raw_selected = raw[sample_mask]
    candidate_selected = candidate[sample_mask]
    target_selected = target[sample_mask]
    denominator = float(np.dot(candidate_selected, candidate_selected))
    fitted_gain = (
        float(np.dot(candidate_selected, target_selected)) / denominator
        if denominator > EPS
        else 0.0
    )
    residual = candidate_selected * fitted_gain - target_selected
    spectral_rmse, band_delta = spectral_band_distance(
        candidate_selected,
        target_selected,
        sample_rate,
        fitted_gain,
    )
    raw_rms = rms(raw_selected)
    candidate_rms = rms(candidate_selected)
    target_rms = rms(target_selected)
    return {
        "frames": int(np.count_nonzero(mask)),
        "duration_s": float(np.count_nonzero(sample_mask) / sample_rate),
        "raw_rms_dbfs": db(raw_rms),
        "candidate_rms_dbfs": db(candidate_rms),
        "target_rms_dbfs": db(target_rms),
        "candidate_minus_target_rms_db": db(
            candidate_rms / max(target_rms, EPS)
        ),
        "candidate_suppression_vs_raw_db": db(
            candidate_rms / max(raw_rms, EPS)
        ),
        "target_suppression_vs_raw_db": db(target_rms / max(raw_rms, EPS)),
        "correlation": normalized_corr(candidate_selected, target_selected),
        "fitted_gain_db": db(fitted_gain),
        "residual_relative_db": db(rms(residual) / max(target_rms, EPS)),
        "spectral_rmse_db": spectral_rmse,
        "band_delta_db_after_gain": band_delta,
    }


def frame_level_summary(envelope: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    selected = 20.0 * np.log10(np.maximum(envelope[mask], EPS))
    if not selected.size:
        return {}
    return {
        "p10_dbfs": float(np.quantile(selected, 0.10)),
        "median_dbfs": float(np.median(selected)),
        "p90_dbfs": float(np.quantile(selected, 0.90)),
    }


def process(
    root: Path,
    raw_path: Path,
    target_path: Path,
    output: Path,
    wet: float,
    pregain: float,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    raw_wav = read_pcm_wav(raw_path)
    target_wav = read_pcm_wav(target_path)
    raw = mono(raw_wav.samples)
    target = mono(target_wav.samples)
    if raw_wav.sample_rate != 48000:
        raise ValueError(
            f"expected a 48 kHz raw file, got {raw_wav.sample_rate} Hz"
        )
    if target_wav.sample_rate != raw_wav.sample_rate:
        raise ValueError("raw and CapCut target sample rates differ")

    raw_pcm16 = output / "65_A_BRUTO_PCM16_48K.wav"
    dfn_directory = output / "dfn_vocal"
    dfn_directory.mkdir(parents=True, exist_ok=True)
    write_pcm16_wav(raw_pcm16, raw, raw_wav.sample_rate)
    deep_filter = (
        root / "audition_neural_runner/deep-filter-0.5.6-aarch64-apple-darwin"
    )
    completed = subprocess.run(
        [str(deep_filter), "-D", "-o", str(dfn_directory), str(raw_pcm16)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"DeepFilterNet failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    dfn_path = dfn_directory / raw_pcm16.name
    dfn_wav = read_pcm_wav(dfn_path)
    dfn_short = mono(dfn_wav.samples)
    dfn_lag, dfn_corr = best_placement_lag(
        raw, dfn_short, max_lag=int(round(raw_wav.sample_rate * 0.10))
    )
    vocal = place_on_reference(raw, dfn_short, dfn_lag, edge_fallback=True)
    mixed = pregain * ((1.0 - wet) * raw + wet * vocal)

    denoise = (
        root / "capcut_reverse_engineering_audio/build/capcut_denoise_v2_runner"
    )
    focus = (
        root / "capcut_reverse_engineering_audio/build/capcut_voice_focus_runner"
    )
    with tempfile.TemporaryDirectory(prefix="pair65_", dir=output) as temporary:
        processed = run_chain(
            mixed,
            raw_wav.sample_rate,
            denoise,
            focus,
            Path(temporary),
            "pair65",
        )

    candidate_lag, _ = best_placement_lag(
        target,
        processed,
        max_lag=int(round(raw_wav.sample_rate * 0.10)),
    )
    raw_lag, raw_target_corr = best_placement_lag(
        target,
        raw,
        max_lag=int(round(raw_wav.sample_rate * 0.10)),
    )
    candidate_aligned = place_on_reference(
        np.zeros_like(target), processed, candidate_lag, edge_fallback=False
    )
    raw_aligned = place_on_reference(
        np.zeros_like(target), raw, raw_lag, edge_fallback=False
    )

    capcut_export = output / "A_CAPCUT_65.wav"
    candidate_export = output / "B_NOSSO_PRESET75_65.wav"
    write_pcm24_wav(capcut_export, target, target_wav.sample_rate)
    write_pcm24_wav(candidate_export, candidate_aligned, target_wav.sample_rate)

    overall = compare_candidate(processed, target, raw_wav.sample_rate)
    raw_to_target = compare_candidate(raw, target, raw_wav.sample_rate)
    frame_size = int(round(raw_wav.sample_rate * 0.02))
    raw_envelope = frame_rms(raw_aligned, frame_size)
    candidate_envelope = frame_rms(candidate_aligned, frame_size)
    target_envelope = frame_rms(target, frame_size)
    count = min(
        raw_envelope.size, candidate_envelope.size, target_envelope.size
    )
    raw_envelope = raw_envelope[:count]
    candidate_envelope = candidate_envelope[:count]
    target_envelope = target_envelope[:count]

    # Use the raw recording to select speech and room/noise gaps.  Edge frames
    # are excluded so export padding cannot make the quiet result look better.
    eligible = np.ones(count, dtype=bool)
    edge_frames = int(round(0.30 / 0.02))
    eligible[:edge_frames] = False
    eligible[-edge_frames:] = False
    eligible_values = raw_envelope[eligible]
    quiet_threshold = float(np.quantile(eligible_values, 0.25))
    active_threshold = float(np.quantile(eligible_values, 0.65))
    quiet = eligible & (raw_envelope <= quiet_threshold)
    active = eligible & (raw_envelope >= active_threshold)

    active_metrics = selected_metrics(
        raw_aligned,
        candidate_aligned,
        target,
        active,
        frame_size,
        raw_wav.sample_rate,
    )
    quiet_metrics = selected_metrics(
        raw_aligned,
        candidate_aligned,
        target,
        quiet,
        frame_size,
        raw_wav.sample_rate,
    )
    levels = {
        "raw_active": frame_level_summary(raw_envelope, active),
        "raw_quiet": frame_level_summary(raw_envelope, quiet),
        "candidate_active": frame_level_summary(candidate_envelope, active),
        "candidate_quiet": frame_level_summary(candidate_envelope, quiet),
        "target_active": frame_level_summary(target_envelope, active),
        "target_quiet": frame_level_summary(target_envelope, quiet),
    }
    candidate_contrast = (
        levels["candidate_quiet"]["median_dbfs"]
        - levels["candidate_active"]["median_dbfs"]
    )
    target_contrast = (
        levels["target_quiet"]["median_dbfs"]
        - levels["target_active"]["median_dbfs"]
    )

    result: dict[str, object] = {
        "settings": {
            "wet": wet,
            "pregain": pregain,
            "denoise_rate": 0.85,
            "denoise_mode": 1,
            "order": "DeepFilterNet mix -> denoise_v2 -> Voice Focus",
            "sample_rate": raw_wav.sample_rate,
        },
        "files": {
            "raw": str(raw_path),
            "target": str(target_path),
            "raw_pcm16_copy": str(raw_pcm16),
            "dfn_vocal": str(dfn_path),
            "capcut_listening_copy": str(capcut_export),
            "candidate": str(candidate_export),
        },
        "lengths": {
            "raw_frames": raw_wav.frames,
            "target_frames": target_wav.frames,
            "dfn_frames": dfn_wav.frames,
            "candidate_frames_before_alignment": int(processed.size),
            "candidate_export_frames": int(candidate_aligned.size),
        },
        "alignment": {
            "dfn_to_raw_lag_samples": dfn_lag,
            "dfn_to_raw_lag_ms": 1000.0 * dfn_lag / raw_wav.sample_rate,
            "dfn_to_raw_correlation": dfn_corr,
            "raw_to_target_lag_samples": raw_lag,
            "raw_to_target_lag_ms": 1000.0 * raw_lag / raw_wav.sample_rate,
            "raw_to_target_correlation": raw_target_corr,
            "candidate_to_target_lag_samples": candidate_lag,
            "candidate_to_target_lag_ms": (
                1000.0 * candidate_lag / raw_wav.sample_rate
            ),
        },
        "overall_candidate_to_target": overall,
        "overall_raw_to_target": raw_to_target,
        "selection": {
            "frame_ms": 20,
            "edge_exclusion_ms": 300,
            "basis": "raw frame RMS",
            "quiet_quantile": 0.25,
            "active_quantile": 0.65,
            "quiet_threshold_dbfs": db(quiet_threshold),
            "active_threshold_dbfs": db(active_threshold),
        },
        "active": active_metrics,
        "quiet": quiet_metrics,
        "frame_level_distributions": levels,
        "contrast": {
            "candidate_quiet_to_active_db": candidate_contrast,
            "target_quiet_to_active_db": target_contrast,
            "candidate_minus_target_db": candidate_contrast - target_contrast,
        },
        "envelope": {
            "all_log_rms_correlation": normalized_corr(
                np.log10(candidate_envelope + EPS),
                np.log10(target_envelope + EPS),
            ),
            "active_log_rms_correlation": normalized_corr(
                np.log10(candidate_envelope[active] + EPS),
                np.log10(target_envelope[active] + EPS),
            ),
            "quiet_log_rms_correlation": normalized_corr(
                np.log10(candidate_envelope[quiet] + EPS),
                np.log10(target_envelope[quiet] + EPS),
            ),
        },
    }
    (output / "RESULTS.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(output / "RELATORIO.md", result)
    return result


def write_report(path: Path, result: dict[str, object]) -> None:
    settings = result["settings"]
    alignment = result["alignment"]
    overall = result["overall_candidate_to_target"]
    active = result["active"]
    quiet = result["quiet"]
    envelope = result["envelope"]
    contrast = result["contrast"]
    lines = [
        "# Validação real — par 65",
        "",
        "## Cadeia",
        "",
        f"- wet neural: **{settings['wet']:.2f}**",
        f"- ganho antes do denoise: **{settings['pregain']:.2f}×**",
        "- denoise: **0,85**, modo 1",
        "- finalização: **Foco na voz reconstruído**",
        "",
        "## Resultado global",
        "",
        f"- correlação: **{overall['correlation']:.5f}**",
        f"- correlação de envelope: **{overall['envelope_correlation']:.5f}**",
        f"- diferença de RMS: **{overall['rms_delta_db']:+.2f} dB**",
        f"- resíduo após ajuste de ganho: **{overall['residual_relative_db']:.2f} dB**",
        f"- erro espectral: **{overall['spectral_rmse_db']:.2f} dB**",
        (
            "- alinhamento candidato→CapCut: "
            f"**{alignment['candidate_to_target_lag_samples']} amostras "
            f"({alignment['candidate_to_target_lag_ms']:+.3f} ms)**"
        ),
        "",
        "## Voz ativa",
        "",
        f"- nosso RMS: **{active['candidate_rms_dbfs']:.2f} dBFS**",
        f"- CapCut RMS: **{active['target_rms_dbfs']:.2f} dBFS**",
        (
            "- diferença nosso−CapCut: "
            f"**{active['candidate_minus_target_rms_db']:+.2f} dB**"
        ),
        f"- correlação: **{active['correlation']:.5f}**",
        "",
        "## Trechos quietos / ruído",
        "",
        f"- bruto: **{quiet['raw_rms_dbfs']:.2f} dBFS**",
        f"- nosso: **{quiet['candidate_rms_dbfs']:.2f} dBFS**",
        f"- CapCut: **{quiet['target_rms_dbfs']:.2f} dBFS**",
        (
            "- diferença de ruído nosso−CapCut: "
            f"**{quiet['candidate_minus_target_rms_db']:+.2f} dB**"
        ),
        (
            "- redução nossa contra bruto: "
            f"**{quiet['candidate_suppression_vs_raw_db']:.2f} dB**"
        ),
        (
            "- redução CapCut contra bruto: "
            f"**{quiet['target_suppression_vs_raw_db']:.2f} dB**"
        ),
        "",
        "## Envelope e contraste",
        "",
        (
            "- correlação de envelope total: "
            f"**{envelope['all_log_rms_correlation']:.5f}**"
        ),
        (
            "- correlação de envelope na voz: "
            f"**{envelope['active_log_rms_correlation']:.5f}**"
        ),
        (
            "- correlação de envelope no quieto: "
            f"**{envelope['quiet_log_rms_correlation']:.5f}**"
        ),
        (
            "- quieto relativo à voz, nosso: "
            f"**{contrast['candidate_quiet_to_active_db']:.2f} dB**"
        ),
        (
            "- quieto relativo à voz, CapCut: "
            f"**{contrast['target_quiet_to_active_db']:.2f} dB**"
        ),
        (
            "- diferença de contraste: "
            f"**{contrast['candidate_minus_target_db']:+.2f} dB**"
        ),
        "",
        "A seleção de voz/quieto usa o envelope do bruto, excluindo 300 ms de",
        "cada borda, para não favorecer nenhum resultado com silêncio de export.",
        "",
        "O runtime/modelo de denoise do CapCut é usado apenas como oráculo local",
        "de validação e não pode integrar um plug-in distribuído.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--raw",
        type=Path,
        default=root
        / "premiere_voice_match_plugin/ab_pair_65/generated/65_A_BRUTO.wav",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=root
        / "premiere_voice_match_plugin/ab_pair_65/generated/65_C_CAPCUT.wav",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "audition_neural_runner/real_pair65_validation",
    )
    parser.add_argument("--wet", type=float, default=0.75)
    parser.add_argument("--pregain", type=float, default=1.15)
    arguments = parser.parse_args()
    result = process(
        root,
        arguments.raw.resolve(),
        arguments.target.resolve(),
        arguments.output.resolve(),
        arguments.wet,
        arguments.pregain,
    )
    print(
        json.dumps(
            {
                "overall": result["overall_candidate_to_target"],
                "active": result["active"],
                "quiet": result["quiet"],
                "contrast": result["contrast"],
                "envelope": result["envelope"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
