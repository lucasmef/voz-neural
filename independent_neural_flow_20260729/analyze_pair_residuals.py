#!/usr/bin/env python3
"""Inspect how CapCut's retained ambience varies across the real pairs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "audition_neural_runner"))

from calibrate_dfn_capcut import (  # noqa: E402
    EPS,
    db,
    frame_rms,
    normalized_corr,
    sinc_resample,
)


PAIR_IDS = ("64", "65", "67", "71", "73", "76")
RAW_RATE = 48_000
TARGET_RATE = 44_100
VAD_BINARY = (
    ROOT / "premiere_voice_match_plugin/build/bin/Release/rnnoise_vad_cli"
)


def summary(values: np.ndarray) -> dict[str, float]:
    levels = 20.0 * np.log10(np.maximum(values, EPS))
    return {
        "p10_dbfs": float(np.quantile(levels, 0.10)),
        "median_dbfs": float(np.median(levels)),
        "p90_dbfs": float(np.quantile(levels, 0.90)),
        "rms_dbfs": db(float(np.sqrt(np.mean(values * values)))),
    }


def main() -> None:
    result: dict[str, object] = {}
    for pair_id in PAIR_IDS:
        raw = np.fromfile(
            ROOT / f"analysis_capcut_pairs/decoded/raw/{pair_id}.f32",
            dtype="<f4",
        )
        target = np.fromfile(
            ROOT / f"analysis_capcut_pairs/decoded/treated/{pair_id}.f32",
            dtype="<f4",
        )
        raw_44 = sinc_resample(
            raw,
            RAW_RATE,
            TARGET_RATE,
            output_frames=target.size,
        )
        frame_size = int(round(0.020 * TARGET_RATE))
        raw_envelope = frame_rms(raw_44, frame_size)
        target_envelope = frame_rms(target, frame_size)
        count = min(raw_envelope.size, target_envelope.size)
        raw_envelope = raw_envelope[:count]
        target_envelope = target_envelope[:count]
        eligible = np.ones(count, dtype=bool)
        edge_frames = int(round(0.300 / 0.020))
        if count > 2 * edge_frames:
            eligible[:edge_frames] = False
            eligible[-edge_frames:] = False
        quiet = eligible & (
            raw_envelope <= np.quantile(raw_envelope[eligible], 0.25)
        )
        active = eligible & (
            raw_envelope >= np.quantile(raw_envelope[eligible], 0.65)
        )
        quiet_gain_db = 20.0 * np.log10(
            np.maximum(target_envelope[quiet], EPS)
            / np.maximum(raw_envelope[quiet], EPS)
        )
        active_gain_db = 20.0 * np.log10(
            np.maximum(target_envelope[active], EPS)
            / np.maximum(raw_envelope[active], EPS)
        )
        with tempfile.TemporaryDirectory(prefix=f"vad_{pair_id}_") as temporary:
            vad_path = Path(temporary) / "vad.f32"
            subprocess.run(
                [
                    str(VAD_BINARY),
                    str(
                        ROOT
                        / f"analysis_capcut_pairs/decoded/raw/{pair_id}.f32"
                    ),
                    str(vad_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            vad_10ms = np.fromfile(vad_path, dtype="<f4")
        vad_20ms = np.maximum(
            vad_10ms[0::2][: vad_10ms.size // 2],
            vad_10ms[1::2][: vad_10ms.size // 2],
        )
        vad_count = min(count, vad_20ms.size)
        speech = np.zeros(count, dtype=bool)
        nonspeech = np.zeros(count, dtype=bool)
        speech[:vad_count] = vad_20ms[:vad_count] >= 0.80
        nonspeech[:vad_count] = vad_20ms[:vad_count] <= 0.10
        speech &= eligible
        nonspeech &= eligible
        nonspeech_gain_db = 20.0 * np.log10(
            np.maximum(target_envelope[nonspeech], EPS)
            / np.maximum(raw_envelope[nonspeech], EPS)
        )
        speech_gain_db = 20.0 * np.log10(
            np.maximum(target_envelope[speech], EPS)
            / np.maximum(raw_envelope[speech], EPS)
        )
        result[pair_id] = {
            "raw_quiet": summary(raw_envelope[quiet]),
            "target_quiet": summary(target_envelope[quiet]),
            "raw_active": summary(raw_envelope[active]),
            "target_active": summary(target_envelope[active]),
            "quiet_target_over_raw": {
                "p10_db": float(np.quantile(quiet_gain_db, 0.10)),
                "median_db": float(np.median(quiet_gain_db)),
                "p90_db": float(np.quantile(quiet_gain_db, 0.90)),
            },
            "active_target_over_raw": {
                "p10_db": float(np.quantile(active_gain_db, 0.10)),
                "median_db": float(np.median(active_gain_db)),
                "p90_db": float(np.quantile(active_gain_db, 0.90)),
            },
            "quiet_log_envelope_correlation": normalized_corr(
                np.log10(raw_envelope[quiet] + EPS),
                np.log10(target_envelope[quiet] + EPS),
            ),
            "active_log_envelope_correlation": normalized_corr(
                np.log10(raw_envelope[active] + EPS),
                np.log10(target_envelope[active] + EPS),
            ),
            "rnnoise_vad": {
                "speech_frames": int(np.count_nonzero(speech)),
                "nonspeech_frames": int(np.count_nonzero(nonspeech)),
                "speech_target_over_raw_median_db": float(
                    np.median(speech_gain_db)
                ),
                "nonspeech_target_over_raw_median_db": float(
                    np.median(nonspeech_gain_db)
                ),
                "speech_raw": summary(raw_envelope[speech]),
                "speech_target": summary(target_envelope[speech]),
                "nonspeech_raw": summary(raw_envelope[nonspeech]),
                "nonspeech_target": summary(target_envelope[nonspeech]),
            },
        }
    output = HERE / "PAIR_RESIDUAL_ANALYSIS.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
