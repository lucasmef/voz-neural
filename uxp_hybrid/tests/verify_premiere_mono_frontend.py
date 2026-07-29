#!/usr/bin/env python3
"""Release gate for ``premiere_mono_center_v1`` and its golden output."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np


MODE = "premiere_mono_center_v1"
EXPECTED_GOLDEN_SHA256 = (
    "97504319359c70c3dbe81737931b0a48"
    "cc5ebfa185014d1dd84d5059497e259b"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_pcm16_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getcomptype() != "NONE"
        ):
            raise AssertionError(f"{path}: expected uncompressed PCM16 mono")
        sample_rate = source.getframerate()
        payload = source.readframes(source.getnframes())
    return np.frombuffer(payload, dtype="<i2").astype(np.int32), sample_rate


def write_pcm24(
    path: Path,
    integer: np.ndarray,
    sample_rate: int,
    channels: int = 1,
) -> None:
    values = np.asarray(integer, dtype=np.int32)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.shape[1] != channels:
        raise AssertionError("PCM24 fixture channel shape is invalid")
    values = values.reshape(-1)
    payload = np.empty((values.size, 3), dtype=np.uint8)
    payload[:, 0] = values & 0xFF
    payload[:, 1] = (values >> 8) & 0xFF
    payload[:, 2] = (values >> 16) & 0xFF
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(3)
        target.setframerate(sample_rate)
        target.writeframes(payload.tobytes())


def simulate_premiere_mono_center(pcm16: np.ndarray) -> np.ndarray:
    """Match the measured AME float32 multiply plus PCM24 floor."""

    pcm24 = np.asarray(pcm16, dtype=np.int32).astype(np.int64) * 256
    return np.floor(
        pcm24.astype(np.float32)
        * np.float32(1.0 / math.sqrt(2.0))
    ).astype(np.int32)


def exhaustive_pcm16_gate(frontend_module: object) -> dict[str, object]:
    original = np.arange(-32768, 32768, dtype=np.int32)
    attenuated_pcm24 = simulate_premiere_mono_center(original)
    attenuated = (
        attenuated_pcm24.astype(np.float32) / np.float32(8_388_608.0)
    )
    restored, diagnostics = (
        frontend_module.restore_premiere_mono_center_v1(attenuated)
    )
    recovered = np.rint(
        restored.astype(np.float64) * 32768.0
    ).astype(np.int32)
    if not np.array_equal(recovered, original):
        mismatch = np.flatnonzero(recovered != original)
        first = int(mismatch[0])
        raise AssertionError(
            "PCM16 exhaustive inversion failed at "
            f"{int(original[first])}: got {int(recovered[first])}"
        )
    if diagnostics.mode != MODE or diagnostics.clipped_samples != 0:
        raise AssertionError(f"unexpected frontend diagnostics: {diagnostics}")
    return {
        "input_values": int(original.size),
        "minimum": int(original[0]),
        "maximum": int(original[-1]),
        "all_recovered_exactly": True,
        "clipped_samples": diagnostics.clipped_samples,
    }


def strict_rejection_gate(frontend_module: object) -> dict[str, object]:
    rejected: list[str] = []
    for label, samples in (
        ("non_mono_shape", np.zeros((4, 2), dtype=np.float32)),
        ("impossible_attenuated_peak", np.array([0.8], dtype=np.float32)),
        ("non_finite", np.array([np.nan], dtype=np.float32)),
        ("off_grid_pcm24", np.array([0.1], dtype=np.float32)),
    ):
        try:
            frontend_module.restore_premiere_mono_center_v1(samples)
        except ValueError:
            rejected.append(label)
        else:
            raise AssertionError(f"strict frontend gate accepted {label}")
    return {"rejected": rejected, "all_invalid_inputs_rejected": True}


def golden_gate(
    project_root: Path,
    runtime_root: Path,
) -> dict[str, object]:
    python = runtime_root / "python/bin/python3"
    runner = runtime_root / "app/independent_voice_runner.py"
    deep_filter = (
        runtime_root / "bin/deep-filter-0.5.6-aarch64-apple-darwin"
    )
    rnnoise = runtime_root / "bin/rnnoise_vad_arm64"
    fixture = (
        project_root
        / "audition_neural_runner/real_pair65_validation/"
        "65_A_BRUTO_PCM16_48K.wav"
    )
    for required in (python, runner, deep_filter, rnnoise, fixture):
        if not required.is_file():
            raise FileNotFoundError(required)

    source, sample_rate = read_pcm16_mono(fixture)
    attenuated = simulate_premiere_mono_center(source)
    with tempfile.TemporaryDirectory(
        prefix="voz_neural_frontend_gate_"
    ) as temporary:
        root = Path(temporary)
        extracted = root / "ame_extracted_pcm24.wav"
        output = root / "frontend_golden.wav"
        diagnostics_path = root / "frontend_golden.json"
        dual_mono = np.column_stack((attenuated, attenuated))
        write_pcm24(extracted, dual_mono, sample_rate, channels=2)
        completed = subprocess.run(
            [
                str(python),
                str(runner),
                str(extracted),
                str(output),
                "--deep-filter-bin",
                str(deep_filter),
                "--rnnoise-vad-bin",
                str(rnnoise),
                "--diagnostics",
                str(diagnostics_path),
                "--input-compensation-mode",
                MODE,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "TMPDIR": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "LC_ALL": "C",
                "LANG": "C",
            },
            timeout=300,
        )
        if completed.returncode:
            raise AssertionError(
                f"runner exited {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        actual_hash = sha256(output)
        if actual_hash != EXPECTED_GOLDEN_SHA256:
            raise AssertionError(
                f"frontend golden {actual_hash} != "
                f"{EXPECTED_GOLDEN_SHA256}"
            )
        stdout = json.loads(completed.stdout)
        diagnostics = json.loads(diagnostics_path.read_text("utf-8"))
        if stdout.get("inputCompensationMode") != MODE:
            raise AssertionError("stdout omitted inputCompensationMode")
        if diagnostics.get("inputCompensationMode") != MODE:
            raise AssertionError("diagnostics omitted inputCompensationMode")
        compensation = diagnostics.get("inputCompensation", {})
        if (
            compensation.get("mode") != MODE
            or compensation.get("canonical_pcm_bits") != 16
            or compensation.get("source_pcm_bits") != 24
            or compensation.get("source_channels") != 2
            or compensation.get("source_layout") != "stereo_dual_mono"
            or compensation.get("projection_max_error_lsb24", 2) > 1
        ):
            raise AssertionError(
                f"invalid compensation diagnostics: {compensation}"
            )
        return {
            "sha256": actual_hash,
            "expected_sha256": EXPECTED_GOLDEN_SHA256,
            "inputCompensationMode": MODE,
            "canonical_pcm_bits": 16,
            "bit_exact": True,
        }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="Defaults to PROJECT_ROOT/uxp_hybrid/runtime.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    project_root = arguments.project_root.resolve()
    runtime_root = (
        arguments.runtime_root.resolve()
        if arguments.runtime_root
        else project_root / "uxp_hybrid/runtime"
    )
    sys.path.insert(0, str(runtime_root / "app"))
    try:
        import premiere_input_frontend
    finally:
        sys.path.pop(0)

    report = {
        "status": "pass",
        "mode": MODE,
        "exhaustive_pcm16": exhaustive_pcm16_gate(
            premiere_input_frontend
        ),
        "strict_rejections": strict_rejection_gate(
            premiere_input_frontend
        ),
        "golden": golden_gate(project_root, runtime_root),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
