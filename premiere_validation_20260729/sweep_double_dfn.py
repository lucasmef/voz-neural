#!/usr/bin/env python3
"""Test a distributable two-pass DeepFilterNet replacement for CapCut denoise."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from analyze_premiere_export import (
    CAPCUT,
    RAW,
    aligned_to_target,
    analyze_candidate,
    db,
    frame_rms,
    mono,
    read_pcm_wav,
)


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "audition_neural_runner"))
sys.path.insert(0, str(ROOT / "premiere_voice_match_plugin/tools"))

from calibrate_dfn_capcut import write_pcm24_wav  # noqa: E402
from evaluate_premiere_ai_focus import (  # noqa: E402
    apply_limiter,
    apply_voice_focus_eq,
)


HERE = Path(__file__).resolve().parent
SAMPLE_RATE = 48_000
FRAME_SIZE = 960


def score(result: dict[str, object]) -> float:
    noise = result["noise_after_voice_match"]
    contrast = result["quiet_to_active_contrast"]
    active = result["active"]
    quiet = result["quiet"]
    envelope = result["envelope"]
    return (
        abs(float(active["candidate_minus_target_rms_db"]))
        + abs(float(noise["quiet_rms_delta_db"]))
        + abs(float(contrast["candidate_minus_target_db"]))
        + float(active["spectral_rmse_db"])
        + 0.35 * float(noise["spectral_rmse_db"])
        + 3.0 * (1.0 - float(envelope["all_log_rms_correlation"]))
        + 1.5 * (1.0 - max(0.0, float(quiet["correlation"])))
    )


def main() -> None:
    target = mono(read_pcm_wav(CAPCUT).samples)
    raw_native = mono(read_pcm_wav(RAW).samples)
    raw, _, _ = aligned_to_target(raw_native, target, SAMPLE_RATE)
    raw_envelope = frame_rms(raw, FRAME_SIZE)
    eligible = np.ones(raw_envelope.size, dtype=bool)
    edge_frames = int(round(0.300 * SAMPLE_RATE / FRAME_SIZE))
    eligible[:edge_frames] = False
    eligible[-edge_frames:] = False
    values = raw_envelope[eligible]
    quiet_threshold = float(np.quantile(values, 0.25))
    active_threshold = float(np.quantile(values, 0.65))
    quiet = eligible & (raw_envelope <= quiet_threshold)
    active = eligible & (raw_envelope >= active_threshold)

    candidates: dict[str, object] = {}
    audio: dict[str, np.ndarray] = {}
    for wet in (0.75, 0.85, 0.90, 0.95):
        first_path = HERE / "dfn_second_pass_inputs" / f"mix_wet{wet:.2f}.wav"
        second_path = HERE / f"dfn_second_pass_wet{wet:.2f}" / first_path.name
        first_native = mono(read_pcm_wav(first_path).samples)
        second_native = mono(read_pcm_wav(second_path).samples)
        first, _, _ = aligned_to_target(first_native, target, SAMPLE_RATE)
        second, _, _ = aligned_to_target(second_native, target, SAMPLE_RATE)
        focused_first = apply_voice_focus_eq(first, SAMPLE_RATE)
        focused_second = apply_voice_focus_eq(second, SAMPLE_RATE)
        for second_wet in (0.25, 0.50, 0.75, 1.00):
            focused_mix = (
                (1.0 - second_wet) * focused_first
                + second_wet * focused_second
            )
            for pregain in (2.0, 2.5, 3.0):
                processed = apply_limiter(
                    pregain * focused_mix,
                    SAMPLE_RATE,
                    ceiling_db=-0.3,
                    release_ms=500.0,
                )
                name = (
                    f"wet1_{wet:.2f}_wet2_{second_wet:.2f}"
                    f"_gain_{pregain:.2f}"
                )
                result = analyze_candidate(
                    name,
                    processed,
                    target,
                    raw,
                    active,
                    quiet,
                    FRAME_SIZE,
                    SAMPLE_RATE,
                )
                result["selection_score"] = score(result)
                candidates[name] = result
                audio[name] = processed

    ranking = sorted(
        candidates,
        key=lambda name: float(candidates[name]["selection_score"]),
    )
    best_name = ranking[0]
    write_pcm24_wav(
        HERE / "DFN_DUPLO_MELHOR_65.wav",
        audio[best_name],
        SAMPLE_RATE,
    )
    result = {
        "selection": {
            "quiet_threshold_dbfs": db(quiet_threshold),
            "active_threshold_dbfs": db(active_threshold),
        },
        "best": best_name,
        "ranking": ranking,
        "candidates": candidates,
    }
    (HERE / "DOUBLE_DFN_SWEEP.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    compact = {}
    for name in ranking[:10]:
        candidate = candidates[name]
        compact[name] = {
            "score": candidate["selection_score"],
            "overall_corr": candidate["overall"]["correlation"],
            "envelope_corr": candidate["envelope"][
                "all_log_rms_correlation"
            ],
            "active_delta_db": candidate["active"][
                "candidate_minus_target_rms_db"
            ],
            "active_spectral_rmse_db": candidate["active"][
                "spectral_rmse_db"
            ],
            "quiet_after_voice_match_db": candidate[
                "noise_after_voice_match"
            ]["quiet_rms_delta_db"],
            "quiet_median_after_voice_match_db": candidate[
                "quiet_to_active_contrast"
            ]["candidate_minus_target_db"],
            "quiet_corr": candidate["quiet"]["correlation"],
            "quiet_spectral_rmse_db": candidate[
                "noise_after_voice_match"
            ]["spectral_rmse_db"],
        }
    print(json.dumps({"best": best_name, "top": compact}, indent=2))


if __name__ == "__main__":
    main()
