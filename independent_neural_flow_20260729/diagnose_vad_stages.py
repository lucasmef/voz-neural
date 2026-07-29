#!/usr/bin/env python3
"""Measure raw-only RNNoise VAD regions through each independent stage."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "audition_neural_runner"))
sys.path.insert(0, str(ROOT / "premiere_voice_match_plugin/tools"))

from calibrate_dfn_capcut import db, frame_rms, rms  # noqa: E402
from evaluate_premiere_ai_focus import (  # noqa: E402
    apply_limiter,
    apply_voice_focus_eq,
)
from independent_voice_runner import (  # noqa: E402
    FlowSettings,
    run_deep_filter,
)


PAIR_IDS = ("64", "65", "67", "71", "73", "76")
DFN = ROOT / "audition_neural_runner/deep-filter-0.5.6-aarch64-apple-darwin"
VAD = HERE / "bin/rnnoise_vad_arm64"


def run_vad(raw: np.ndarray, temporary: Path) -> np.ndarray:
    source = temporary / "raw.f32"
    output = temporary / "vad.f32"
    np.asarray(raw, dtype="<f4").tofile(source)
    subprocess.run([str(VAD), str(source), str(output)], check=True)
    return np.fromfile(output, dtype="<f4").astype(np.float64)


def metric(audio: np.ndarray, mask: np.ndarray, frame_size: int) -> dict:
    envelope = frame_rms(audio, frame_size)
    count = min(envelope.size, mask.size)
    selected = envelope[:count][mask[:count]]
    return {
        "frames": int(selected.size),
        "median_dbfs": db(float(np.median(selected))),
        "rms_dbfs": db(rms(selected)),
        "p10_dbfs": db(float(np.quantile(selected, 0.1))),
        "p90_dbfs": db(float(np.quantile(selected, 0.9))),
    }


def main() -> None:
    settings = FlowSettings()
    result = {}
    for pair_id in PAIR_IDS:
        raw = np.fromfile(
            ROOT / f"analysis_capcut_pairs/decoded/raw/{pair_id}.f32",
            dtype="<f4",
        ).astype(np.float64)
        with tempfile.TemporaryDirectory(
            prefix=f"vad_stages_{pair_id}_", dir=HERE
        ) as temporary_name:
            temporary = Path(temporary_name)
            vad_10 = run_vad(raw, temporary)
            first, _ = run_deep_filter(
                raw, 48000, DFN, temporary, "pass1"
            )
            first_mix = (
                settings.first_wet * first
                + (1.0 - settings.first_wet) * raw
            )
            second, _ = run_deep_filter(
                first_mix, 48000, DFN, temporary, "pass2"
            )
            second_mix = (
                settings.second_wet * second
                + (1.0 - settings.second_wet) * first_mix
            )
            focused = apply_voice_focus_eq(second_mix, 48000)
            focused_limited = apply_limiter(
                settings.focus_pregain * focused,
                48000,
                ceiling_db=settings.focus_ceiling_dbfs,
                release_ms=settings.limiter_release_ms,
            )
        # Two 10 ms probabilities become one 20 ms control frame.  Max keeps
        # even a half-frame speech onset out of the non-speech statistics.
        if vad_10.size % 2:
            vad_10 = np.pad(vad_10, (0, 1))
        vad_20_max = vad_10.reshape(-1, 2).max(axis=1)
        masks = {
            "non_speech_lt_0.15": vad_20_max < 0.15,
            "transition_0.15_to_0.70": (
                (vad_20_max >= 0.15) & (vad_20_max < 0.70)
            ),
            "speech_ge_0.70": vad_20_max >= 0.70,
        }
        stages = {
            "raw": raw,
            "dfn1": first,
            "mix1": first_mix,
            "dfn2": second,
            "mix2": second_mix,
            "focus_limited": focused_limited,
        }
        result[pair_id] = {
            name: {
                stage: metric(audio, mask, 960)
                for stage, audio in stages.items()
            }
            for name, mask in masks.items()
        }
        print(pair_id)
    output = HERE / "VAD_STAGE_DIAGNOSTICS.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
