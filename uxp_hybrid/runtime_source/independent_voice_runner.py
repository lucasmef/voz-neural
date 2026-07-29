#!/usr/bin/env python3
"""Premiere-safe launcher for the frozen independent neural voice runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

import numpy as np

from premiere_input_frontend import (
    INPUT_COMPENSATION_NONE,
    PREMIERE_MONO_CENTER_V1,
    SUPPORTED_INPUT_COMPENSATION_MODES,
    diagnostics_dict,
    restore_premiere_mono_center_v1,
    write_pcm16_mono,
)


HERE = Path(__file__).resolve().parent
PACKAGED_CORE = HERE / "independent_voice_core.py"
DEVELOPMENT_CORE = (
    HERE.parents[1]
    / "independent_neural_flow_20260729"
    / "independent_voice_runner.py"
)


def resolve_core() -> Path:
    for candidate in (PACKAGED_CORE, DEVELOPMENT_CORE):
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("independent_voice_core.py is missing")


def extract_compensation_mode(
    arguments: Sequence[str],
) -> tuple[str, list[str]]:
    """Remove the wrapper-only option before invoking the frozen core."""

    mode = INPUT_COMPENSATION_NONE
    found = False
    forwarded: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--input-compensation-mode":
            if found or index + 1 >= len(arguments):
                raise ValueError(
                    "--input-compensation-mode must appear exactly once "
                    "with a value"
                )
            mode = arguments[index + 1]
            found = True
            index += 2
            continue
        prefix = "--input-compensation-mode="
        if argument.startswith(prefix):
            if found:
                raise ValueError(
                    "--input-compensation-mode must not be repeated"
                )
            mode = argument[len(prefix) :]
            found = True
            index += 1
            continue
        forwarded.append(argument)
        index += 1
    if mode not in SUPPORTED_INPUT_COMPENSATION_MODES:
        raise ValueError(f"unsupported inputCompensationMode: {mode!r}")
    return mode, forwarded


def diagnostics_path_from_arguments(arguments: Sequence[str]) -> Path:
    for index, argument in enumerate(arguments):
        if argument == "--diagnostics":
            if index + 1 >= len(arguments):
                raise ValueError("--diagnostics requires a path")
            return Path(arguments[index + 1]).resolve()
        prefix = "--diagnostics="
        if argument.startswith(prefix):
            return Path(argument[len(prefix) :]).resolve()
    if len(arguments) < 2:
        raise ValueError("runner requires INPUT and OUTPUT paths")
    return Path(arguments[1]).resolve().with_suffix(".json")


def prepare_compensated_input(
    core: Path,
    input_path: Path,
    destination: Path,
) -> dict[str, object]:
    sys.path.insert(0, str(core.parent))
    try:
        from runtime_dsp import read_pcm_wav
    finally:
        sys.path.pop(0)

    source = read_pcm_wav(input_path)
    if source.sample_rate != 48_000 or source.sample_width != 3:
        raise ValueError(
            "premiere_mono_center_v1 requires PCM24 at 48 kHz "
            "from the locked Premiere extraction preset"
        )
    if source.channels == 1:
        mono_source = source.samples[:, 0]
        extracted_layout = "mono"
    elif source.channels == 2 and np.array_equal(
        source.samples[:, 0], source.samples[:, 1]
    ):
        mono_source = source.samples[:, 0]
        extracted_layout = "stereo_dual_mono"
    else:
        raise ValueError(
            "premiere_mono_center_v1 requires mono or bit-exact dual-mono "
            "output from the locked Premiere extraction preset"
        )
    restored, compensation = restore_premiere_mono_center_v1(
        mono_source
    )
    write_pcm16_mono(destination, restored, source.sample_rate)
    return {
        **diagnostics_dict(compensation),
        "source_sample_rate": source.sample_rate,
        "source_channels": source.channels,
        "source_pcm_bits": source.sample_width * 8,
        "source_layout": extracted_layout,
    }


def run(arguments: Sequence[str]) -> int:
    mode, forwarded = extract_compensation_mode(arguments)
    if len(forwarded) < 2 or forwarded[0].startswith("-"):
        raise ValueError("runner requires INPUT and OUTPUT as first arguments")

    core = resolve_core()
    original_input = Path(forwarded[0]).resolve()
    diagnostics_path = diagnostics_path_from_arguments(forwarded)
    compensation: dict[str, object] = {"mode": INPUT_COMPENSATION_NONE}

    with tempfile.TemporaryDirectory(
        prefix="voz_neural_input_",
        dir=os.environ.get("TMPDIR"),
    ) as temporary:
        core_arguments = list(forwarded)
        if mode == PREMIERE_MONO_CENTER_V1:
            compensated_input = Path(temporary) / "canonical_pcm16.wav"
            compensation = prepare_compensated_input(
                core,
                original_input,
                compensated_input,
            )
            core_arguments[0] = str(compensated_input)

        completed = subprocess.run(
            [sys.executable, str(core), *core_arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    if completed.returncode:
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        return completed.returncode
    if completed.stderr:
        sys.stderr.write(completed.stderr)

    runner_stdout = json.loads(completed.stdout)
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    diagnostics["inputCompensationMode"] = mode
    diagnostics["inputCompensation"] = compensation
    diagnostics.setdefault("files", {})["input"] = str(original_input)
    diagnostics_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    runner_stdout["inputCompensationMode"] = mode
    sys.stdout.write(
        json.dumps(runner_stdout, ensure_ascii=False, indent=2) + "\n"
    )
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"input frontend error: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
