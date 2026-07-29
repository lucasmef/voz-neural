#!/usr/bin/env python3
"""Compare the isolated full-chain re-export with an earlier CapCut export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from analyze_isolated_exports import (
    CHUNK_FRAMES,
    load_timecodes,
    open_pcm16_wav,
    relation,
    rms_peak,
)


def equality_fraction(left, right) -> float:
    matching = 0
    count = 0
    stop = min(left.frames, right.frames)
    for start in range(0, stop, CHUNK_FRAMES):
        end = min(start + CHUNK_FRAMES, stop)
        a = np.asarray(left.samples[start:end])
        b = np.asarray(right.samples[start:end])
        matching += int(np.count_nonzero(a == b))
        count += a.size
    return matching / max(count, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("previous_export", type=Path)
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    root = base.parent
    previous = open_pcm16_wav(args.previous_export.resolve())
    recheck = open_pcm16_wav(base / "CAPCUT_FULL_CHAIN_75_RECHECK.WAV")
    if (previous.sample_rate, previous.channels, previous.frames) != (
        recheck.sample_rate,
        recheck.channels,
        recheck.frames,
    ):
        raise ValueError("The two exports do not share the same format and duration")

    previous_rms, previous_peak = rms_peak(previous)
    recheck_rms, recheck_peak = rms_peak(recheck)
    result: dict[str, object] = {
        "previous_export": str(previous.path),
        "recheck_export": str(recheck.path),
        "frames": previous.frames,
        "sample_rate": previous.sample_rate,
        "channels": previous.channels,
        "global_relation_previous_to_recheck": relation(previous, recheck),
        "exact_sample_equality_fraction": equality_fraction(previous, recheck),
        "previous_rms_lsb": previous_rms,
        "previous_peak_lsb": previous_peak,
        "recheck_rms_lsb": recheck_rms,
        "recheck_peak_lsb": recheck_peak,
        "per_clip": [],
    }

    timecodes = load_timecodes(
        root / "capcut_project_audio_female_30_clips" / "MAPA_TIMECODES.csv"
    )
    for clip in timecodes:
        start = int(clip["start_frame"])
        stop = int(clip["stop_frame"])
        result["per_clip"].append(
            {
                "order": clip["order"],
                "category": clip["category"],
                "file": clip["file"],
                **relation(previous, recheck, start, stop),
            }
        )

    output = base / "FULL_REEXPORT_COMPARISON.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
