#!/usr/bin/env python3
"""Reproducible check for Premiere-safe dual-mono delivery.

This test does not run the neural stages.  It takes an already processed mono
WAV, exercises the exact layout helper and PCM24 writer used by the production
runner, then verifies both layouts sample by sample.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

import numpy as np

from independent_voice_runner import render_output_channels
from runtime_dsp import read_pcm_wav, write_pcm24_wav


HERE = Path(__file__).resolve().parent
DEFAULT_FIXTURE = HERE / "65_INDEPENDENT_FINAL_48K.wav"


def channel_hash(channel: np.ndarray) -> str:
    """Hash canonical three-byte PCM24 samples, independent of WAV headers."""

    samples = np.asarray(channel, dtype=np.float64)
    integer = np.clip(
        np.rint(samples * 8388608.0), -8388608, 8388607
    ).astype(np.int32)
    payload = np.empty((integer.size, 3), dtype=np.uint8)
    payload[:, 0] = integer & 0xFF
    payload[:, 1] = (integer >> 8) & 0xFF
    payload[:, 2] = (integer >> 16) & 0xFF
    return hashlib.sha256(payload.tobytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fixture",
        type=Path,
        nargs="?",
        default=DEFAULT_FIXTURE,
        help="An already processed PCM WAV; defaults to the final pair-65 WAV.",
    )
    parser.add_argument(
        "--stereo-output",
        type=Path,
        help="Verify an actual default runner/wrapper render.",
    )
    parser.add_argument(
        "--mono-output",
        type=Path,
        help="Compare with an actual runner render made with --mono.",
    )
    arguments = parser.parse_args()
    if (arguments.stereo_output is None) != (arguments.mono_output is None):
        parser.error("--stereo-output and --mono-output must be used together")

    if arguments.stereo_output is not None:
        stereo = read_pcm_wav(arguments.stereo_output.resolve())
        mono = read_pcm_wav(arguments.mono_output.resolve())
        test_mode = "actual_runner_outputs"
    else:
        fixture = read_pcm_wav(arguments.fixture.resolve())
        processed = np.mean(fixture.samples, axis=1, dtype=np.float32)
        stereo_samples = render_output_channels(processed)
        mono_samples = render_output_channels(processed, force_mono=True)
        with tempfile.TemporaryDirectory(
            prefix="premiere_dual_mono_test_"
        ) as tmp:
            temporary = Path(tmp)
            stereo_path = temporary / "default_dual_mono.wav"
            mono_path = temporary / "explicit_mono.wav"
            write_pcm24_wav(
                stereo_path, stereo_samples, fixture.sample_rate
            )
            write_pcm24_wav(
                mono_path, mono_samples, fixture.sample_rate
            )
            stereo = read_pcm_wav(stereo_path)
            mono = read_pcm_wav(mono_path)
        test_mode = "layout_helper_and_pcm24_writer"

    if stereo.channels != 2:
        raise AssertionError(f"default output has {stereo.channels} channels")
    if mono.channels != 1:
        raise AssertionError(f"--mono output has {mono.channels} channels")
    if stereo.sample_rate != mono.sample_rate:
        raise AssertionError("default and mono outputs have different rates")
    if stereo.sample_width != mono.sample_width:
        raise AssertionError("default and mono outputs have different bit depth")
    if stereo.frames != mono.frames:
        raise AssertionError("default and mono outputs have different duration")

    left = stereo.samples[:, 0]
    right = stereo.samples[:, 1]
    mono_channel = mono.samples[:, 0]
    left_right_difference = float(np.max(np.abs(left - right)))
    left_mono_difference = float(np.max(np.abs(left - mono_channel)))
    right_mono_difference = float(np.max(np.abs(right - mono_channel)))
    hashes = {
        "left_pcm24_sha256": channel_hash(left),
        "right_pcm24_sha256": channel_hash(right),
        "mono_pcm24_sha256": channel_hash(mono_channel),
    }
    if left_right_difference != 0.0:
        raise AssertionError("left and right are not sample-identical")
    if left_mono_difference != 0.0 or right_mono_difference != 0.0:
        raise AssertionError("dual-mono channels differ from explicit mono")
    if len(set(hashes.values())) != 1:
        raise AssertionError("per-channel PCM hashes differ")

    print(
        json.dumps(
            {
                "status": "pass",
                "test_mode": test_mode,
                "fixture": str(arguments.fixture.resolve()),
                "sample_rate": stereo.sample_rate,
                "sample_width_bits": stereo.sample_width * 8,
                "frames": stereo.frames,
                "default_channels": stereo.channels,
                "explicit_mono_channels": mono.channels,
                "max_abs_left_minus_right": left_right_difference,
                "max_abs_left_minus_mono": left_mono_difference,
                "max_abs_right_minus_mono": right_mono_difference,
                **hashes,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
