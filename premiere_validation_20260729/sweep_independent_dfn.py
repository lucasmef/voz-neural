#!/usr/bin/env python3
"""Test a fully independent DFN + reconstructed Voice Focus chain."""

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

from calibrate_dfn_capcut import (  # noqa: E402
    best_placement_lag,
    place_on_reference,
    write_pcm24_wav,
)
from evaluate_premiere_ai_focus import (  # noqa: E402
    apply_limiter,
    apply_voice_focus_eq,
)


HERE = Path(__file__).resolve().parent
DFN = (
    ROOT
    / "audition_neural_runner/real_pair65_validation/dfn_vocal/"
    "65_A_BRUTO_PCM16_48K.wav"
)
SAMPLE_RATE = 48_000
FRAME_SIZE = 960


def main() -> None:
    target = mono(read_pcm_wav(CAPCUT).samples)
    raw_native = mono(read_pcm_wav(RAW).samples)
    raw, _, _ = aligned_to_target(raw_native, target, SAMPLE_RATE)
    dfn_native = mono(read_pcm_wav(DFN).samples)
    dfn_lag, dfn_corr = best_placement_lag(
        raw_native, dfn_native, max_lag=4_800
    )
    vocal_native = place_on_reference(
        raw_native, dfn_native, dfn_lag, edge_fallback=True
    )
    vocal, _, _ = aligned_to_target(vocal_native, target, SAMPLE_RATE)

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

    # Voice Focus EQ is linear, so it only needs to be calculated once for
    # each stem. The limiter remains after the 75% blend, matching CapCut order.
    focused_raw = apply_voice_focus_eq(raw, SAMPLE_RATE)
    focused_vocal = apply_voice_focus_eq(vocal, SAMPLE_RATE)
    candidates: dict[str, object] = {}
    audio: dict[str, np.ndarray] = {}
    for wet in (0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        focused_mix = (1.0 - wet) * focused_raw + wet * focused_vocal
        for pregain in (1.5, 2.0, 2.5, 3.0, 3.5):
            processed = apply_limiter(
                pregain * focused_mix,
                SAMPLE_RATE,
                ceiling_db=-0.3,
                release_ms=500.0,
            )
            name = f"wet{wet:.2f}_gain{pregain:.2f}"
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
            noise = result["noise_after_voice_match"]
            contrast = result["quiet_to_active_contrast"]
            active_result = result["active"]
            quiet_result = result["quiet"]
            envelope = result["envelope"]
            score = (
                abs(float(noise["quiet_rms_delta_db"]))
                + abs(float(contrast["candidate_minus_target_db"]))
                + float(active_result["spectral_rmse_db"])
                + 0.35 * float(noise["spectral_rmse_db"])
                + 3.0 * (1.0 - float(envelope["all_log_rms_correlation"]))
                + 1.5 * (1.0 - max(0.0, float(quiet_result["correlation"])))
            )
            result["selection_score"] = score
            candidates[name] = result
            audio[name] = processed

    ranking = sorted(
        candidates,
        key=lambda name: float(candidates[name]["selection_score"]),
    )
    best_name = ranking[0]
    write_pcm24_wav(
        HERE / "DFN_INDEPENDENTE_MELHOR_65.wav",
        audio[best_name],
        SAMPLE_RATE,
    )
    summary = {
        "dfn_alignment": {
            "lag_samples": dfn_lag,
            "correlation": dfn_corr,
        },
        "selection": {
            "quiet_threshold_dbfs": db(quiet_threshold),
            "active_threshold_dbfs": db(active_threshold),
        },
        "best": best_name,
        "ranking": ranking,
        "candidates": candidates,
    }
    (HERE / "DFN_INDEPENDENT_SWEEP.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    compact = {}
    for name in ranking[:10]:
        result = candidates[name]
        compact[name] = {
            "score": result["selection_score"],
            "overall_corr": result["overall"]["correlation"],
            "envelope_corr": result["envelope"]["all_log_rms_correlation"],
            "active_delta_db": result["active"][
                "candidate_minus_target_rms_db"
            ],
            "active_spectral_rmse_db": result["active"]["spectral_rmse_db"],
            "quiet_after_voice_match_db": result[
                "noise_after_voice_match"
            ]["quiet_rms_delta_db"],
            "quiet_median_after_voice_match_db": result[
                "quiet_to_active_contrast"
            ]["candidate_minus_target_db"],
            "quiet_corr": result["quiet"]["correlation"],
            "quiet_spectral_rmse_db": result[
                "noise_after_voice_match"
            ]["spectral_rmse_db"],
        }
    print(json.dumps({"best": best_name, "top": compact}, indent=2))


if __name__ == "__main__":
    main()
