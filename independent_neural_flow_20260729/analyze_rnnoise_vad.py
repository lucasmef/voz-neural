#!/usr/bin/env python3
"""Inspect RNNoise VAD against the six real offline validation pairs."""

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

from calibrate_dfn_capcut import mono, read_pcm_wav  # noqa: E402

VAD = HERE / "bin/rnnoise_vad_arm64"
PAIR_IDS = ("64", "65", "67", "71", "73", "76")


def frames(audio: np.ndarray, size: int) -> np.ndarray:
    count = (audio.size + size - 1) // size
    padded = np.pad(audio, (0, count * size - audio.size))
    return padded.reshape(count, size)


def db(value: np.ndarray | float) -> np.ndarray | float:
    return 20.0 * np.log10(np.maximum(value, 1e-12))


def main() -> None:
    result: dict[str, object] = {}
    for pair_id in PAIR_IDS:
        raw = np.fromfile(
            ROOT / f"analysis_capcut_pairs/decoded/raw/{pair_id}.f32",
            dtype="<f4",
        ).astype(np.float64)
        target = np.fromfile(
            ROOT / f"analysis_capcut_pairs/decoded/treated/{pair_id}.f32",
            dtype="<f4",
        ).astype(np.float64)
        candidate = mono(
            read_pcm_wav(
                HERE
                / f"validation_wavs/{pair_id}_B_INDEPENDENT_44K1.wav"
            ).samples
        )
        # Target is 44.1 kHz.  This simple indexing is sufficient for
        # envelope-only diagnostic bins and avoids importing the runner.
        raw_10 = frames(raw, 480)
        target_10 = frames(target, 441)
        candidate_10 = frames(candidate, 441)
        count = min(
            raw_10.shape[0], target_10.shape[0], candidate_10.shape[0]
        )
        raw_rms = np.sqrt(np.mean(raw_10[:count] ** 2, axis=1))
        target_rms = np.sqrt(np.mean(target_10[:count] ** 2, axis=1))
        candidate_rms = np.sqrt(
            np.mean(candidate_10[:count] ** 2, axis=1)
        )
        with tempfile.TemporaryDirectory(prefix="rnnoise_vad_", dir=HERE) as td:
            source = Path(td) / "raw.f32"
            probabilities = Path(td) / "vad.f32"
            raw.astype("<f4").tofile(source)
            subprocess.run(
                [str(VAD), str(source), str(probabilities)],
                check=True,
            )
            vad = np.fromfile(probabilities, dtype="<f4")[:count]
        bins = {}
        for name, selected in {
            "lt_0.15": vad < 0.15,
            "0.15_to_0.40": (vad >= 0.15) & (vad < 0.40),
            "0.40_to_0.70": (vad >= 0.40) & (vad < 0.70),
            "ge_0.70": vad >= 0.70,
        }.items():
            bins[name] = {
                "frames": int(np.count_nonzero(selected)),
                "raw_rms_median_dbfs": float(
                    np.median(db(raw_rms[selected]))
                )
                if np.any(selected)
                else None,
                "target_rms_median_dbfs": float(
                    np.median(db(target_rms[selected]))
                )
                if np.any(selected)
                else None,
                "target_over_raw_median_db": float(
                    np.median(db(target_rms[selected]) - db(raw_rms[selected]))
                )
                if np.any(selected)
                else None,
                "candidate_rms_median_dbfs": float(
                    np.median(db(candidate_rms[selected]))
                )
                if np.any(selected)
                else None,
                "candidate_minus_target_median_db": float(
                    np.median(
                        db(candidate_rms[selected]) - db(target_rms[selected])
                    )
                )
                if np.any(selected)
                else None,
            }
        result[pair_id] = {
            "frames": int(count),
            "vad_quantiles": {
                str(q): float(np.quantile(vad, q))
                for q in (0.1, 0.25, 0.5, 0.75, 0.9)
            },
            "bins": bins,
        }
    output = HERE / "RNNOISE_VAD_ANALYSIS.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
