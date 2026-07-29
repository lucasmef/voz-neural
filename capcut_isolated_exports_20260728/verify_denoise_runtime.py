#!/usr/bin/env python3
"""Run CapCut's installed denoise_v2 model per clip and compare its export."""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from analyze_isolated_exports import (
    CHUNK_FRAMES,
    db,
    load_timecodes,
    open_pcm16_wav,
)


def compare_float_candidate(
    candidate: np.memmap,
    target,
    start: int = 0,
    stop: int | None = None,
) -> dict[str, float]:
    stop = target.frames if stop is None else min(stop, target.frames)
    xx = yy = xy = error_sq = 0.0
    count = 0
    for position in range(start, stop, CHUNK_FRAMES):
        end = min(position + CHUNK_FRAMES, stop)
        x = np.asarray(candidate[position:end], dtype=np.float64)
        y = np.asarray(target.samples[position:end], dtype=np.float64) / 32768.0
        xx += float(np.sum(x * x))
        yy += float(np.sum(y * y))
        xy += float(np.sum(x * y))
        count += x.size
    gain = xy / max(xx, 1e-30)
    for position in range(start, stop, CHUNK_FRAMES):
        end = min(position + CHUNK_FRAMES, stop)
        x = np.asarray(candidate[position:end], dtype=np.float64)
        y = np.asarray(target.samples[position:end], dtype=np.float64) / 32768.0
        error = y - gain * x
        error_sq += float(np.sum(error * error))
    target_rms = math.sqrt(yy / max(count, 1))
    error_rms = math.sqrt(error_sq / max(count, 1))
    return {
        "fitted_gain": gain,
        "fitted_gain_db": db(abs(gain)),
        "cosine_correlation": xy / math.sqrt(max(xx * yy, 1e-30)),
        "target_rms_dbfs": db(target_rms),
        "error_rms_dbfs": db(error_rms),
        "error_relative_to_target_db": db(error_rms / max(target_rms, 1e-30)),
    }


def main() -> None:
    base = Path(__file__).resolve().parent
    root = base.parent
    raw = open_pcm16_wav(base / "CAPCUT_RAW_CONTROL_ALL_OFF.WAV")
    target = open_pcm16_wav(base / "CAPCUT_DENOISE_ONLY_085.WAV")
    runner = root / "capcut_reverse_engineering_audio/build/capcut_denoise_v2_runner"
    model = Path(
        "/Applications/CapCut.app/Contents/Resources/audiosami/"
        "unet_denoise_44k_music_model_v1.0.model"
    )
    timecodes = load_timecodes(
        root / "capcut_project_audio_female_30_clips/MAPA_TIMECODES.csv"
    )

    if (raw.sample_rate, raw.channels, raw.frames) != (
        target.sample_rate,
        target.channels,
        target.frames,
    ):
        raise ValueError("Raw and CapCut denoise export formats differ")

    with tempfile.TemporaryDirectory(prefix="capcut_denoise_verify_") as tmp:
        tmp_path = Path(tmp)
        result: dict[str, object] = {
            "runtime": str(runner),
            "model": str(model),
            "settings": {
                "sample_rate": raw.sample_rate,
                "channels": raw.channels,
                "sami_type": 2,
                "denoise_rate": 0.85,
                "denoise_mode": 1,
                "processing_scope": "reset once per timeline clip",
            },
            "block_size_trials": {},
        }

        for block_size in (512, 1024):
            candidate_path = tmp_path / f"candidate_{block_size}.f32"
            candidate = np.memmap(
                candidate_path,
                dtype="<f4",
                mode="w+",
                shape=(raw.frames, raw.channels),
            )
            candidate[:] = 0.0

            for clip in timecodes:
                start = int(clip["start_frame"])
                stop = min(int(clip["stop_frame"]), raw.frames)
                input_path = tmp_path / f'{int(clip["order"]):02d}_input.f32'
                output_path = (
                    tmp_path
                    / f'{int(clip["order"]):02d}_output_{block_size}.f32'
                )
                source = (
                    np.asarray(raw.samples[start:stop], dtype=np.float32)
                    / 32768.0
                )
                source.astype("<f4", copy=False).tofile(input_path)
                subprocess.run(
                    [
                        str(runner),
                        str(input_path),
                        str(output_path),
                        str(raw.sample_rate),
                        str(raw.channels),
                        "0.85",
                        "1",
                        str(model),
                        str(block_size),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                processed = np.memmap(
                    output_path,
                    dtype="<f4",
                    mode="r",
                    shape=(stop - start, raw.channels),
                )
                candidate[start:stop] = processed
                del processed
            candidate.flush()

            trial: dict[str, object] = {
                "global": compare_float_candidate(candidate, target),
                "per_clip": [],
            }
            for clip in timecodes:
                start = int(clip["start_frame"])
                stop = min(int(clip["stop_frame"]), raw.frames)
                trial["per_clip"].append(
                    {
                        "order": clip["order"],
                        "category": clip["category"],
                        "file": clip["file"],
                        **compare_float_candidate(
                            candidate, target, start, stop
                        ),
                    }
                )
            result["block_size_trials"][str(block_size)] = trial
            del candidate

    output = base / "DENOISE_RUNTIME_ISOLATED_VERIFICATION.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                size: trial["global"]
                for size, trial in result["block_size_trials"].items()
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
