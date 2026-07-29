#!/usr/bin/env python3
"""Compare the two plausible local-stage orders against CapCut's full export."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from analyze_isolated_exports import load_timecodes, open_pcm16_wav
from verify_denoise_runtime import compare_float_candidate


def run(
    executable: Path,
    source: Path,
    target: Path,
    sample_rate: int,
    channels: int,
) -> None:
    if "denoise" in executable.name:
        arguments = [
            str(executable),
            str(source),
            str(target),
            str(sample_rate),
            str(channels),
            "0.85",
            "1",
        ]
    else:
        arguments = [
            str(executable),
            str(source),
            str(target),
            str(sample_rate),
            str(channels),
        ]
    subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    base = Path(__file__).resolve().parent
    root = base.parent
    wavs = {
        "raw": open_pcm16_wav(base / "CAPCUT_RAW_CONTROL_ALL_OFF.WAV"),
        "focus": open_pcm16_wav(base / "CAPCUT_FOCUS_ONLY_100.WAV"),
        "enhance75": open_pcm16_wav(base / "CAPCUT_ENHANCE_ONLY_75.WAV"),
        "full": open_pcm16_wav(base / "CAPCUT_FULL_CHAIN_75_RECHECK.WAV"),
    }
    formats = {
        (wav.sample_rate, wav.channels, wav.frames) for wav in wavs.values()
    }
    if len(formats) != 1:
        raise ValueError(f"Export formats differ: {formats}")

    denoise = root / "capcut_reverse_engineering_audio/build/capcut_denoise_v2_runner"
    focus = root / "capcut_reverse_engineering_audio/build/capcut_voice_focus_runner"
    timecodes = load_timecodes(
        root / "capcut_project_audio_female_30_clips/MAPA_TIMECODES.csv"
    )
    sample_rate = wavs["raw"].sample_rate
    channels = wavs["raw"].channels
    frames = wavs["raw"].frames

    with tempfile.TemporaryDirectory(prefix="capcut_order_verify_") as temp:
        temp_path = Path(temp)
        candidates = {
            name: np.memmap(
                temp_path / f"{name}.f32",
                dtype="<f4",
                mode="w+",
                shape=(frames, channels),
            )
            for name in (
                "focus_from_raw",
                "focus_then_denoise",
                "denoise_then_focus",
            )
        }
        for candidate in candidates.values():
            candidate[:] = 0.0

        for clip in timecodes:
            order = int(clip["order"])
            start = int(clip["start_frame"])
            stop = min(int(clip["stop_frame"]), frames)
            raw_input = temp_path / f"{order:02d}_raw.f32"
            enhance_input = temp_path / f"{order:02d}_enhance.f32"
            raw_float = (
                np.asarray(wavs["raw"].samples[start:stop], dtype=np.float32)
                / 32768.0
            )
            enhance_float = (
                np.asarray(
                    wavs["enhance75"].samples[start:stop], dtype=np.float32
                )
                / 32768.0
            )
            raw_float.astype("<f4", copy=False).tofile(raw_input)
            enhance_float.astype("<f4", copy=False).tofile(enhance_input)

            focus_raw = temp_path / f"{order:02d}_focus_raw.f32"
            run(focus, raw_input, focus_raw, sample_rate, channels)

            focus_enhance = temp_path / f"{order:02d}_focus_enhance.f32"
            focus_then_denoise = (
                temp_path / f"{order:02d}_focus_then_denoise.f32"
            )
            run(focus, enhance_input, focus_enhance, sample_rate, channels)
            run(
                denoise,
                focus_enhance,
                focus_then_denoise,
                sample_rate,
                channels,
            )

            denoise_enhance = temp_path / f"{order:02d}_denoise_enhance.f32"
            denoise_then_focus = (
                temp_path / f"{order:02d}_denoise_then_focus.f32"
            )
            run(
                denoise,
                enhance_input,
                denoise_enhance,
                sample_rate,
                channels,
            )
            run(
                focus,
                denoise_enhance,
                denoise_then_focus,
                sample_rate,
                channels,
            )

            outputs = {
                "focus_from_raw": focus_raw,
                "focus_then_denoise": focus_then_denoise,
                "denoise_then_focus": denoise_then_focus,
            }
            for name, path in outputs.items():
                processed = np.memmap(
                    path,
                    dtype="<f4",
                    mode="r",
                    shape=(stop - start, channels),
                )
                candidates[name][start:stop] = processed
                del processed

        for candidate in candidates.values():
            candidate.flush()

        result: dict[str, object] = {
            "settings": {
                "focus": "exposed MDSP EQ + limiter preset",
                "denoise": "installed denoise_v2, rate 0.85, mode 1",
                "scope": "each timeline clip processed with fresh state",
            },
            "focus_runner_validation": {
                "global": compare_float_candidate(
                    candidates["focus_from_raw"], wavs["focus"]
                ),
                "per_clip": [],
            },
            "stage_order_trials": {},
        }

        for clip in timecodes:
            start = int(clip["start_frame"])
            stop = min(int(clip["stop_frame"]), frames)
            result["focus_runner_validation"]["per_clip"].append(
                {
                    "order": clip["order"],
                    "category": clip["category"],
                    "file": clip["file"],
                    **compare_float_candidate(
                        candidates["focus_from_raw"],
                        wavs["focus"],
                        start,
                        stop,
                    ),
                }
            )

        for name in ("focus_then_denoise", "denoise_then_focus"):
            trial: dict[str, object] = {
                "global": compare_float_candidate(
                    candidates[name], wavs["full"]
                ),
                "per_clip": [],
            }
            for clip in timecodes:
                start = int(clip["start_frame"])
                stop = min(int(clip["stop_frame"]), frames)
                trial["per_clip"].append(
                    {
                        "order": clip["order"],
                        "category": clip["category"],
                        "file": clip["file"],
                        **compare_float_candidate(
                            candidates[name],
                            wavs["full"],
                            start,
                            stop,
                        ),
                    }
                )
            result["stage_order_trials"][name] = trial

    output = base / "STAGE_ORDER_VERIFICATION.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "focus_runner_validation": result["focus_runner_validation"][
                    "global"
                ],
                "stage_order_trials": {
                    name: trial["global"]
                    for name, trial in result["stage_order_trials"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
