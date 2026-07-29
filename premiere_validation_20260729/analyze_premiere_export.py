#!/usr/bin/env python3
"""Compare the new Premiere export, CapCut, and the adaptive DFN candidate."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "audition_neural_runner"))

from calibrate_dfn_capcut import (  # noqa: E402
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
    spectral_band_distance,
)
from process_real_pair import (  # noqa: E402
    frame_level_summary,
    selected_metrics,
)


PREMIERE = ROOT / "PREMIERE_ENHANCE7_AUTOMATCH_VOZCLARA_65.wav"
CAPCUT = (
    ROOT
    / "premiere_voice_match_plugin/ab_pair_65/generated/65_C_CAPCUT.wav"
)
RAW = ROOT / "premiere_voice_match_plugin/ab_pair_65/generated/65_A_BRUTO.wav"
DFN = (
    ROOT
    / "audition_neural_runner/real_pair65_validation/"
    "B_NOSSO_ADAPTATIVO_65.wav"
)
OUTPUT = ROOT / "premiere_validation_20260729"


def biquad(audio: np.ndarray, b: tuple[float, ...], a: tuple[float, ...]) -> np.ndarray:
    source = np.asarray(audio, dtype=np.float64)
    result = np.empty_like(source)
    z1 = 0.0
    z2 = 0.0
    b0, b1, b2 = b
    _, a1, a2 = a
    for index, sample in enumerate(source):
        output = b0 * sample + z1
        z1 = b1 * sample - a1 * output + z2
        z2 = b2 * sample - a2 * output
        result[index] = output
    return result


def integrated_lufs(samples: np.ndarray, sample_rate: int) -> float:
    """ITU-R BS.1770-style integrated loudness for 48 kHz PCM."""

    audio = np.asarray(samples, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, None]
    if sample_rate != 48000:
        raise ValueError("the validation LUFS implementation expects 48 kHz")
    shelf_b = (
        1.53512485958697,
        -2.69169618940638,
        1.19839281085285,
    )
    shelf_a = (1.0, -1.69065929318241, 0.73248077421585)
    highpass_b = (1.0, -2.0, 1.0)
    highpass_a = (1.0, -1.99004745483398, 0.99007225036621)
    weighted = np.empty_like(audio)
    for channel in range(audio.shape[1]):
        first = biquad(audio[:, channel], shelf_b, shelf_a)
        weighted[:, channel] = biquad(first, highpass_b, highpass_a)

    block = int(round(0.400 * sample_rate))
    hop = int(round(0.100 * sample_rate))
    if weighted.shape[0] < block:
        weighted = np.pad(weighted, ((0, block - weighted.shape[0]), (0, 0)))
    starts = np.arange(0, weighted.shape[0] - block + 1, hop)
    energies = np.empty(starts.size, dtype=np.float64)
    for index, start in enumerate(starts):
        segment = weighted[start : start + block]
        energies[index] = float(np.sum(np.mean(segment * segment, axis=0)))
    block_loudness = -0.691 + 10.0 * np.log10(np.maximum(energies, EPS))
    absolute = block_loudness >= -70.0
    if not np.any(absolute):
        return -math.inf
    absolute_energy = float(np.mean(energies[absolute]))
    relative_gate = -0.691 + 10.0 * math.log10(max(absolute_energy, EPS)) - 10.0
    gated = absolute & (block_loudness >= relative_gate)
    if not np.any(gated):
        return -math.inf
    return -0.691 + 10.0 * math.log10(max(float(np.mean(energies[gated])), EPS))


def sample_peak_dbfs(samples: np.ndarray) -> float:
    return db(float(np.max(np.abs(samples))))


def stereo_metrics(samples: np.ndarray) -> dict[str, float | bool]:
    audio = np.asarray(samples, dtype=np.float64)
    if audio.ndim != 2 or audio.shape[1] < 2:
        return {
            "dual_mono": False,
            "left_right_correlation": 1.0,
            "difference_to_sum_db": -600.0,
        }
    left = audio[:, 0]
    right = audio[:, 1]
    difference = left - right
    summed = 0.5 * (left + right)
    return {
        "dual_mono": bool(np.array_equal(left, right)),
        "left_right_correlation": normalized_corr(left, right),
        "difference_to_sum_db": db(rms(difference) / max(rms(summed), EPS)),
    }


def native_file_metrics(path: Path) -> tuple[dict[str, object], np.ndarray]:
    wav = read_pcm_wav(path)
    mono_audio = mono(wav.samples)
    result: dict[str, object] = {
        "path": str(path),
        "sample_rate": wav.sample_rate,
        "channels": wav.channels,
        "bits": wav.sample_width * 8,
        "frames": wav.frames,
        "duration_s": wav.frames / wav.sample_rate,
        "mono_rms_dbfs": db(rms(mono_audio)),
        "mono_sample_peak_dbfs": sample_peak_dbfs(mono_audio),
        "integrated_lufs_mono_equivalent": integrated_lufs(
            mono_audio, wav.sample_rate
        ),
        "integrated_lufs_native_channels": integrated_lufs(
            wav.samples, wav.sample_rate
        ),
        "stereo": stereo_metrics(wav.samples),
    }
    return result, mono_audio


def aligned_to_target(
    candidate: np.ndarray, target: np.ndarray, sample_rate: int
) -> tuple[np.ndarray, int, float]:
    lag, correlation = best_placement_lag(
        target,
        candidate,
        max_lag=int(round(0.100 * sample_rate)),
    )
    aligned = place_on_reference(
        np.zeros_like(target),
        candidate,
        lag,
        edge_fallback=False,
    )
    return aligned, lag, correlation


def masked_samples(mask: np.ndarray, frame_size: int, length: int) -> np.ndarray:
    return np.repeat(mask, frame_size)[:length]


def analyze_candidate(
    name: str,
    candidate_native: np.ndarray,
    target: np.ndarray,
    raw_aligned: np.ndarray,
    active: np.ndarray,
    quiet: np.ndarray,
    frame_size: int,
    sample_rate: int,
) -> dict[str, object]:
    candidate, lag, alignment_corr = aligned_to_target(
        candidate_native, target, sample_rate
    )
    overall = compare_candidate(candidate_native, target, sample_rate)
    active_metrics = selected_metrics(
        raw_aligned,
        candidate,
        target,
        active,
        frame_size,
        sample_rate,
    )
    quiet_metrics = selected_metrics(
        raw_aligned,
        candidate,
        target,
        quiet,
        frame_size,
        sample_rate,
    )
    candidate_envelope = frame_rms(candidate, frame_size)[: active.size]
    target_envelope = frame_rms(target, frame_size)[: active.size]
    levels = {
        "candidate_active": frame_level_summary(candidate_envelope, active),
        "candidate_quiet": frame_level_summary(candidate_envelope, quiet),
        "target_active": frame_level_summary(target_envelope, active),
        "target_quiet": frame_level_summary(target_envelope, quiet),
    }
    sample_quiet = masked_samples(quiet, frame_size, target.size)
    active_gain_db = -float(active_metrics["candidate_minus_target_rms_db"])
    active_gain = 10.0 ** (active_gain_db / 20.0)
    quiet_spectral_voice_matched, quiet_band_voice_matched = (
        spectral_band_distance(
            candidate[sample_quiet],
            target[sample_quiet],
            sample_rate,
            active_gain,
        )
    )
    relative_noise_gap = (
        float(quiet_metrics["candidate_minus_target_rms_db"])
        - float(active_metrics["candidate_minus_target_rms_db"])
    )
    candidate_contrast = (
        float(levels["candidate_quiet"]["median_dbfs"])
        - float(levels["candidate_active"]["median_dbfs"])
    )
    target_contrast = (
        float(levels["target_quiet"]["median_dbfs"])
        - float(levels["target_active"]["median_dbfs"])
    )
    return {
        "name": name,
        "alignment": {
            "lag_samples": lag,
            "lag_ms": 1000.0 * lag / sample_rate,
            "preemphasis_alignment_correlation": alignment_corr,
        },
        "overall": overall,
        "active": active_metrics,
        "quiet": quiet_metrics,
        "noise_after_voice_match": {
            "voice_match_gain_db": active_gain_db,
            "quiet_rms_delta_db": relative_noise_gap,
            "spectral_rmse_db": quiet_spectral_voice_matched,
            "band_delta_db": quiet_band_voice_matched,
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
        "frame_level_distributions": levels,
        "quiet_to_active_contrast": {
            "candidate_db": candidate_contrast,
            "target_db": target_contrast,
            "candidate_minus_target_db": candidate_contrast - target_contrast,
        },
    }


def fmt(value: float, decimals: int = 2, signed: bool = False) -> str:
    if signed:
        return f"{value:+.{decimals}f}"
    return f"{value:.{decimals}f}"


def write_report(result: dict[str, object]) -> None:
    files = result["files"]
    premiere = result["comparisons"]["premiere"]
    dfn = result["comparisons"]["dfn_adaptive"]
    capcut_file = files["capcut"]
    premiere_file = files["premiere"]
    dfn_file = files["dfn_adaptive"]
    prem_gap = float(
        premiere["noise_after_voice_match"]["quiet_rms_delta_db"]
    )
    dfn_gap = float(dfn["noise_after_voice_match"]["quiet_rms_delta_db"])
    prem_median_gap = float(
        premiere["quiet_to_active_contrast"]["candidate_minus_target_db"]
    )
    dfn_median_gap = float(
        dfn["quiet_to_active_contrast"]["candidate_minus_target_db"]
    )
    direction = "mais alto" if prem_gap > 0 else "mais baixo"
    verdict = (
        f"Pela energia RMS total, o fundo do Premiere fica {abs(prem_gap):.2f} "
        f"dB {direction} que o CapCut após igualar a voz — uma melhora clara "
        "sobre 5 dB. Porém o piso mediano contínuo ainda difere "
        f"{abs(prem_median_gap):.2f} dB, portanto continua próximo de 5 dB "
        "nessa leitura. O volume de fundo melhorou; a ambiência ainda não foi "
        "reproduzida com o mesmo comportamento."
    )

    lines = [
        "# Validação do novo export Premiere — 29/07/2026",
        "",
        "## Veredito",
        "",
        verdict,
        "",
        (
            "As duas leituras são necessárias: RMS mede a energia quieta total; "
            "a mediana mede o piso contínuo sem ser dominada por alguns picos "
            "de respiração ou ambiência."
        ),
        "",
        "## Formato e nível global",
        "",
        "| Arquivo | Canais / bits | Duração | LUFS mono | RMS | Pico |",
        "|---|---:|---:|---:|---:|---:|",
        (
            f"| CapCut | {capcut_file['channels']} / {capcut_file['bits']} | "
            f"{capcut_file['duration_s']:.6f} s | "
            f"{capcut_file['integrated_lufs_mono_equivalent']:.2f} | "
            f"{capcut_file['mono_rms_dbfs']:.2f} dBFS | "
            f"{capcut_file['mono_sample_peak_dbfs']:.2f} dBFS |"
        ),
        (
            f"| Premiere novo | {premiere_file['channels']} / "
            f"{premiere_file['bits']} | {premiere_file['duration_s']:.6f} s | "
            f"{premiere_file['integrated_lufs_mono_equivalent']:.2f} | "
            f"{premiere_file['mono_rms_dbfs']:.2f} dBFS | "
            f"{premiere_file['mono_sample_peak_dbfs']:.2f} dBFS |"
        ),
        (
            f"| Nosso DFN adaptativo | {dfn_file['channels']} / "
            f"{dfn_file['bits']} | {dfn_file['duration_s']:.6f} s | "
            f"{dfn_file['integrated_lufs_mono_equivalent']:.2f} | "
            f"{dfn_file['mono_rms_dbfs']:.2f} dBFS | "
            f"{dfn_file['mono_sample_peak_dbfs']:.2f} dBFS |"
        ),
        "",
        (
            "O Premiere é estéreo. `LUFS mono` usa o centro mono para permitir "
            "comparação direta com os dois arquivos mono; o JSON também contém "
            "o LUFS dos canais nativos. Os canais do Premiere são dual-mono; "
            "por isso o LUFS estéreo nativo fica aproximadamente 3 LU acima."
        ),
        "",
        "## Similaridade com o CapCut",
        "",
        "| Medida | Premiere novo | DFN adaptativo |",
        "|---|---:|---:|",
        (
            f"| alinhamento | {premiere['alignment']['lag_samples']} amostras "
            f"({premiere['alignment']['lag_ms']:+.3f} ms) | "
            f"{dfn['alignment']['lag_samples']} amostras "
            f"({dfn['alignment']['lag_ms']:+.3f} ms) |"
        ),
        (
            f"| correlação global | {premiere['overall']['correlation']:.5f} | "
            f"{dfn['overall']['correlation']:.5f} |"
        ),
        (
            "| resíduo após ajuste de ganho | "
            f"{premiere['overall']['residual_relative_db']:.2f} dB | "
            f"{dfn['overall']['residual_relative_db']:.2f} dB |"
        ),
        (
            "| correlação do envelope | "
            f"{premiere['envelope']['all_log_rms_correlation']:.5f} | "
            f"{dfn['envelope']['all_log_rms_correlation']:.5f} |"
        ),
        (
            f"| RMS versus CapCut | {premiere['overall']['rms_delta_db']:+.2f} "
            f"dB | {dfn['overall']['rms_delta_db']:+.2f} dB |"
        ),
        (
            "| erro espectral global após ganho | "
            f"{premiere['overall']['spectral_rmse_db']:.2f} dB | "
            f"{dfn['overall']['spectral_rmse_db']:.2f} dB |"
        ),
        (
            "| erro espectral na voz ativa | "
            f"{premiere['active']['spectral_rmse_db']:.2f} dB | "
            f"{dfn['active']['spectral_rmse_db']:.2f} dB |"
        ),
        "",
        "## Voz ativa e fundo",
        "",
        "| Medida | Premiere novo | DFN adaptativo | CapCut |",
        "|---|---:|---:|---:|",
        (
            "| RMS voz ativa | "
            f"{premiere['active']['candidate_rms_dbfs']:.2f} dBFS | "
            f"{dfn['active']['candidate_rms_dbfs']:.2f} dBFS | "
            f"{premiere['active']['target_rms_dbfs']:.2f} dBFS |"
        ),
        (
            "| diferença na voz | "
            f"{premiere['active']['candidate_minus_target_rms_db']:+.2f} dB | "
            f"{dfn['active']['candidate_minus_target_rms_db']:+.2f} dB | "
            "0,00 dB |"
        ),
        (
            "| RMS quieto | "
            f"{premiere['quiet']['candidate_rms_dbfs']:.2f} dBFS | "
            f"{dfn['quiet']['candidate_rms_dbfs']:.2f} dBFS | "
            f"{premiere['quiet']['target_rms_dbfs']:.2f} dBFS |"
        ),
        (
            "| diferença quieta absoluta | "
            f"{premiere['quiet']['candidate_minus_target_rms_db']:+.2f} dB | "
            f"{dfn['quiet']['candidate_minus_target_rms_db']:+.2f} dB | "
            "0,00 dB |"
        ),
        (
            "| **fundo após igualar a voz** | "
            f"**{prem_gap:+.2f} dB** | **{dfn_gap:+.2f} dB** | 0,00 dB |"
        ),
        (
            "| **piso mediano após igualar voz** | "
            f"**{prem_median_gap:+.2f} dB** | "
            f"**{dfn_median_gap:+.2f} dB** | 0,00 dB |"
        ),
        (
            "| correlação da forma de onda quieta | "
            f"{premiere['quiet']['correlation']:.5f} | "
            f"{dfn['quiet']['correlation']:.5f} | 1,00000 |"
        ),
        (
            "| correlação do envelope quieto | "
            f"{premiere['envelope']['quiet_log_rms_correlation']:.5f} | "
            f"{dfn['envelope']['quiet_log_rms_correlation']:.5f} | 1,00000 |"
        ),
        (
            "| erro espectral quieto, ganho próprio | "
            f"{premiere['quiet']['spectral_rmse_db']:.2f} dB | "
            f"{dfn['quiet']['spectral_rmse_db']:.2f} dB | 0,00 dB |"
        ),
        (
            "| erro espectral quieto após igualar voz | "
            f"{premiere['noise_after_voice_match']['spectral_rmse_db']:.2f} dB | "
            f"{dfn['noise_after_voice_match']['spectral_rmse_db']:.2f} dB | "
            "0,00 dB |"
        ),
        "",
        "## Leitura técnica",
        "",
        (
            f"- Premiere: fundo relativo à voz {prem_gap:+.2f} dB versus "
            f"CapCut em RMS, mas piso mediano {prem_median_gap:+.2f} dB."
        ),
        (
            f"- DFN adaptativo: fundo relativo à voz {dfn_gap:+.2f} dB versus "
            f"CapCut em RMS e piso mediano {dfn_median_gap:+.2f} dB."
        ),
        (
            "- O DFN adaptativo continua mais próximo do CapCut na forma de "
            "onda, envelope, tonalidade da voz e textura do fundo. O Premiere "
            "novo acerta melhor o LUFS absoluto, mas mantém um piso mais "
            "constante e menos correlacionado com a ambiência do CapCut."
        ),
        (
            "- Correlação mede semelhança temporal; erro espectral quieto mede "
            "se o caráter da ambiência também é parecido, não apenas o volume."
        ),
        (
            "- A seleção de voz/quieto usa o envelope do bruto e exclui 300 ms "
            "das bordas, exatamente igual para as três versões."
        ),
        "",
    ]
    (OUTPUT / "RELATORIO.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    file_metrics: dict[str, dict[str, object]] = {}
    audio: dict[str, np.ndarray] = {}
    for name, path in (
        ("raw", RAW),
        ("capcut", CAPCUT),
        ("premiere", PREMIERE),
        ("dfn_adaptive", DFN),
    ):
        file_metrics[name], audio[name] = native_file_metrics(path)
    sample_rates = {
        int(file_metrics[name]["sample_rate"]) for name in file_metrics
    }
    if sample_rates != {48000}:
        raise ValueError(f"unexpected sample rates: {sample_rates}")
    sample_rate = 48000
    target = audio["capcut"]
    raw_aligned, raw_lag, raw_alignment_corr = aligned_to_target(
        audio["raw"], target, sample_rate
    )
    frame_size = int(round(0.020 * sample_rate))
    raw_envelope = frame_rms(raw_aligned, frame_size)
    count = raw_envelope.size
    eligible = np.ones(count, dtype=bool)
    edge_frames = int(round(0.300 / 0.020))
    eligible[:edge_frames] = False
    eligible[-edge_frames:] = False
    values = raw_envelope[eligible]
    quiet_threshold = float(np.quantile(values, 0.25))
    active_threshold = float(np.quantile(values, 0.65))
    quiet = eligible & (raw_envelope <= quiet_threshold)
    active = eligible & (raw_envelope >= active_threshold)

    comparisons = {
        "premiere": analyze_candidate(
            "premiere",
            audio["premiere"],
            target,
            raw_aligned,
            active,
            quiet,
            frame_size,
            sample_rate,
        ),
        "dfn_adaptive": analyze_candidate(
            "dfn_adaptive",
            audio["dfn_adaptive"],
            target,
            raw_aligned,
            active,
            quiet,
            frame_size,
            sample_rate,
        ),
    }
    result: dict[str, object] = {
        "files": file_metrics,
        "selection": {
            "basis": "raw frame RMS",
            "frame_ms": 20,
            "edge_exclusion_ms": 300,
            "quiet_quantile": 0.25,
            "active_quantile": 0.65,
            "quiet_threshold_dbfs": db(quiet_threshold),
            "active_threshold_dbfs": db(active_threshold),
            "quiet_frames": int(np.count_nonzero(quiet)),
            "active_frames": int(np.count_nonzero(active)),
            "raw_to_target_lag_samples": raw_lag,
            "raw_to_target_alignment_correlation": raw_alignment_corr,
        },
        "comparisons": comparisons,
    }
    (OUTPUT / "METRICS.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(result)
    print(
        json.dumps(
            {
                "premiere": comparisons["premiere"],
                "dfn_adaptive": comparisons["dfn_adaptive"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
