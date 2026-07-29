#!/usr/bin/env python3
"""Score light RNNoise blends before the measured Premiere-match stage."""

from __future__ import annotations

import json
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


HERE = Path(__file__).resolve().parent
SAMPLE_RATE = 48_000
FRAME_SIZE = 960


def main() -> None:
    target = mono(read_pcm_wav(CAPCUT).samples)
    raw = mono(read_pcm_wav(RAW).samples)
    raw_aligned, _, _ = aligned_to_target(raw, target, SAMPLE_RATE)

    raw_envelope = frame_rms(raw_aligned, FRAME_SIZE)
    eligible = np.ones(raw_envelope.size, dtype=bool)
    edge_frames = int(round(0.300 * SAMPLE_RATE / FRAME_SIZE))
    eligible[:edge_frames] = False
    eligible[-edge_frames:] = False
    values = raw_envelope[eligible]
    quiet_threshold = float(np.quantile(values, 0.25))
    active_threshold = float(np.quantile(values, 0.65))
    quiet = eligible & (raw_envelope <= quiet_threshold)
    active = eligible & (raw_envelope >= active_threshold)

    results: dict[str, object] = {
        "selection": {
            "quiet_threshold_dbfs": db(quiet_threshold),
            "active_threshold_dbfs": db(active_threshold),
        },
        "candidates": {},
    }
    paths = sorted(HERE.glob("rn_*_then_match.f32"))
    paths += sorted(HERE.glob("residual_*_then_match.f32"))
    for path in paths:
        candidate = np.fromfile(path, dtype="<f4").astype(np.float64)
        results["candidates"][path.stem] = analyze_candidate(
            path.stem,
            candidate,
            target,
            raw_aligned,
            active,
            quiet,
            FRAME_SIZE,
            SAMPLE_RATE,
        )

    (HERE / "RNNOISE_HYBRID_SWEEP.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {}
    for name, candidate in results["candidates"].items():
        summary[name] = {
            "overall_corr": candidate["overall"]["correlation"],
            "envelope_corr": candidate["envelope"]["all_log_rms_correlation"],
            "active_delta_db": candidate["active"][
                "candidate_minus_target_rms_db"
            ],
            "quiet_after_voice_match_db": candidate[
                "noise_after_voice_match"
            ]["quiet_rms_delta_db"],
            "quiet_median_after_voice_match_db": candidate[
                "quiet_to_active_contrast"
            ]["candidate_minus_target_db"],
            "quiet_waveform_corr": candidate["quiet"]["correlation"],
            "quiet_spectral_rmse_after_voice_match_db": candidate[
                "noise_after_voice_match"
            ]["spectral_rmse_db"],
            "active_spectral_rmse_db": candidate["active"][
                "spectral_rmse_db"
            ],
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
