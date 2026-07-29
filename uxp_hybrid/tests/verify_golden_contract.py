#!/usr/bin/env python3
"""Bit-exact release gate for the UXP Hybrid offline audio runtime.

The gate intentionally executes the production runner twice, in two isolated
temporary jobs:

* default Premiere-safe stereo dual-mono output;
* explicit mono output.

It never edits or copies the checked-in/local golden media.  The only WAVs it
creates live inside a TemporaryDirectory and are removed at the end.

The canonical packaged runtime is ``uxp_hybrid/runtime``.  Development source
fallback is deliberately opt-in with ``--allow-development-fallback``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


EXPECTED_SHA256 = {
    "runner": "6d4a902cff80ccbbffcdc94bc0a484dd34160186588fb61326386b8f6ff1e955",
    "runner_core": "3de2e11bd4f071fdeab437c4f09e9f59b6c855f9cef07e052e9ba18a5b62d244",
    "input_frontend": "88ecb44e60de587cd671de9ffb084d76f0d39805f521e967b60534f4b07321c7",
    "runtime_dsp": "e7c9d68837b22b9d58124b8eb6188328c74479abfcc3aed3974a56eb594e2249",
    "deep_filter": "4601e7f4e4c03e59a4c5b5000216ef3add3e808799cfccd95e14e83ea4611081",
    "rnnoise_vad": "b12848f27892a77e0b454b62ff5ce8e28d23d754848f142960a8f85756457357",
    "python": "eb9d74b9c7cfdfb2c9b91614edb2c3607360ba46c5aa7fc4557b3a4a23e97cff",
    "input_fixture": "ddf325a8c92204b1aa489112fabf5e1e3428341816f6e382f9e69a6686a98131",
    "golden_mono": "4407cb2ff0d78e7e19c54bdd85a9c3e046765e6e275b9ce4640fd9d17e7f39ba",
    "golden_stereo": "97504319359c70c3dbe81737931b0a48cc5ebfa185014d1dd84d5059497e259b",
}

EXPECTED_NUMPY_VERSION = "2.3.5"
EXPECTED_NUMPY_TREE_SHA256 = (
    "ce72d714eaf4c9055ecd273bb2b0b35e51a33da2e87bce95293e6f51cc25f20e"
)
EXPECTED_PYTHON_VERSION = (3, 12, 13)
EXPECTED_MACHINE = "arm64"
EXPECTED_SYSTEM = "Darwin"
EXPECTED_CHANNEL_PCM24_SHA256 = (
    "1b84148dffc8c69aa36e27cd438747ac97b8e7f728cfcfb36ff4d96ed5836d38"
)

EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_FRAMES = 1_098_627
EXPECTED_SAMPLE_WIDTH = 3

CANONICAL_SETTINGS: Dict[str, Any] = {
    "first_wet": 0.95,
    "second_wet": 0.75,
    "focus_pregain": 3.0,
    "frame_ms": 20.0,
    "edge_exclusion_ms": 300.0,
    "quiet_quantile": 0.25,
    "active_quantile": 0.65,
    "vad_residual_threshold": 0.1,
    "vad_high_speech_threshold": 0.7,
    "vad_hangover_ms": 50.0,
    "vad_hangover_voice_floor": 0.15,
    "vad_mix_at_015": 0.1,
    "vad_mix_at_040": 0.45,
    "vad_mix_at_070": 0.8,
    "vad_mix_at_090": 1.0,
    "residual_minimum_frames": 16,
    "residual_suppression_min_db": 2.5,
    "residual_suppression_max_db": 6.0,
    "residual_stationarity_intercept_db": 11.0,
    "residual_stationarity_slope": 1.2,
    "residual_dfn_confidence_scale": 0.45,
    "residual_neural_weight": 0.65,
    "residual_transition_ms": 20.0,
    "low_suppression_voice_floor": 0.15,
    "target_active_median_dbfs": -15.5,
    "final_ceiling_dbfs": -0.3,
    "limiter_release_ms": 500.0,
    "max_final_peak_boost_db": 0.0,
}

DEFAULT_DEVELOPMENT_PYTHON = (
    Path.home()
    / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)


class ContractFailure(RuntimeError):
    """A concise, user-actionable golden-contract failure."""


@dataclass(frozen=True)
class RuntimePaths:
    mode: str
    python: Path
    runner: Path
    runner_core: Path
    input_frontend: Path
    runtime_dsp: Path
    deep_filter: Path
    rnnoise_vad: Path


@dataclass(frozen=True)
class FixturePaths:
    input_fixture: Path
    golden_mono: Path
    golden_stereo: Path


@dataclass(frozen=True)
class WaveData:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    compression: str
    payload: bytes


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    path: Optional[str] = None
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            key: value
            for key, value in {
                "name": self.name,
                "status": self.status,
                "detail": self.detail,
                "path": self.path,
                "expected_sha256": self.expected_sha256,
                "actual_sha256": self.actual_sha256,
            }.items()
            if value is not None
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_runtime(
    project_root: Path,
    runtime_root: Path,
    python_override: Optional[Path],
    allow_development_fallback: bool,
) -> RuntimePaths:
    canonical = RuntimePaths(
        mode="packaged",
        python=(python_override or runtime_root / "python/bin/python3").resolve(),
        runner=(runtime_root / "app/independent_voice_runner.py").resolve(),
        runner_core=(
            runtime_root / "app/independent_voice_core.py"
        ).resolve(),
        input_frontend=(
            runtime_root / "app/premiere_input_frontend.py"
        ).resolve(),
        runtime_dsp=(runtime_root / "app/runtime_dsp.py").resolve(),
        deep_filter=(
            runtime_root / "bin/deep-filter-0.5.6-aarch64-apple-darwin"
        ).resolve(),
        rnnoise_vad=(runtime_root / "bin/rnnoise_vad_arm64").resolve(),
    )
    canonical_files = (
        canonical.python,
        canonical.runner,
        canonical.runner_core,
        canonical.input_frontend,
        canonical.runtime_dsp,
        canonical.deep_filter,
        canonical.rnnoise_vad,
    )
    if all(path.is_file() for path in canonical_files):
        return canonical
    if not allow_development_fallback:
        return canonical

    development_root = project_root / "independent_neural_flow_20260729"
    development_python = python_override
    if development_python is None:
        environment_python = os.environ.get("VOZ_NEURAL_TEST_PYTHON", "")
        development_python = (
            Path(environment_python)
            if environment_python
            else DEFAULT_DEVELOPMENT_PYTHON
        )
    return RuntimePaths(
        mode="development-fallback",
        python=development_python.resolve(),
        runner=(
            project_root
            / "uxp_hybrid/runtime_source/independent_voice_runner.py"
        ).resolve(),
        runner_core=(
            development_root / "independent_voice_runner.py"
        ).resolve(),
        input_frontend=(
            project_root
            / "uxp_hybrid/runtime_source/premiere_input_frontend.py"
        ).resolve(),
        runtime_dsp=(development_root / "runtime_dsp.py").resolve(),
        deep_filter=(
            development_root
            / "bin/deep-filter-0.5.6-aarch64-apple-darwin"
        ).resolve(),
        rnnoise_vad=(development_root / "bin/rnnoise_vad_arm64").resolve(),
    )


def resolve_fixtures(
    project_root: Path,
    input_override: Optional[Path],
    mono_override: Optional[Path],
    stereo_override: Optional[Path],
) -> FixturePaths:
    return FixturePaths(
        input_fixture=(
            input_override
            or project_root
            / "audition_neural_runner/real_pair65_validation/"
            "65_A_BRUTO_PCM16_48K.wav"
        ).resolve(),
        golden_mono=(
            mono_override
            or project_root
            / "independent_neural_flow_20260729/"
            "65_INDEPENDENT_FINAL_48K.wav"
        ).resolve(),
        golden_stereo=(
            stereo_override
            or project_root / "B_NOSSO_FLUXO_65_PREMIERE_STEREO.wav"
        ).resolve(),
    )


def file_check(
    name: str,
    path: Path,
    expected_sha256: str,
    executable: bool = False,
) -> CheckResult:
    if not path.exists():
        return CheckResult(
            name,
            "missing",
            "arquivo ausente",
            str(path),
            expected_sha256,
        )
    if not path.is_file():
        return CheckResult(
            name,
            "fail",
            "o caminho não é um arquivo regular",
            str(path),
            expected_sha256,
        )
    actual = sha256_file(path)
    if actual != expected_sha256:
        return CheckResult(
            name,
            "fail",
            "SHA-256 divergente",
            str(path),
            expected_sha256,
            actual,
        )
    if executable and not os.access(str(path), os.X_OK):
        return CheckResult(
            name,
            "fail",
            "arquivo sem permissão de execução",
            str(path),
            expected_sha256,
            actual,
        )
    return CheckResult(
        name,
        "pass",
        "SHA-256 confirmado",
        str(path),
        expected_sha256,
        actual,
    )


PYTHON_DEPENDENCY_PROBE = r"""
import hashlib
import json
import platform
import sys
from pathlib import Path

try:
    import numpy
except Exception as exc:
    print(json.dumps({"import_error": repr(exc)}))
    raise SystemExit(20)

root = Path(numpy.__file__).resolve().parent
files = []
for candidate in root.rglob("*"):
    if (
        candidate.is_file()
        and "__pycache__" not in candidate.parts
        and candidate.suffix not in {".pyc", ".pyo"}
    ):
        files.append(candidate)

tree = hashlib.sha256()
total_size = 0
for candidate in sorted(
    files, key=lambda item: item.relative_to(root).as_posix()
):
    relative = candidate.relative_to(root).as_posix().encode("utf-8")
    payload = candidate.read_bytes()
    total_size += len(payload)
    tree.update(relative)
    tree.update(b"\0")
    tree.update(hashlib.sha256(payload).digest())
    tree.update(b"\n")

print(json.dumps({
    "python_version": list(sys.version_info[:3]),
    "numpy_version": numpy.__version__,
    "numpy_tree_sha256": tree.hexdigest(),
    "numpy_files": len(files),
    "numpy_bytes": total_size,
    "machine": platform.machine(),
    "system": platform.system(),
}))
"""


def python_dependency_check(runtime: RuntimePaths) -> Tuple[CheckResult, Dict[str, Any]]:
    if not runtime.python.is_file():
        return (
            CheckResult(
                "python_dependencies",
                "missing",
                "Python empacotado ausente; NumPy não pôde ser verificado",
                str(runtime.python),
            ),
            {},
        )
    completed = subprocess.run(
        [str(runtime.python), "-c", PYTHON_DEPENDENCY_PROBE],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return (
            CheckResult(
                "python_dependencies",
                "fail",
                "Python/NumPy não executou: " + detail,
                str(runtime.python),
            ),
            {},
        )
    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return (
            CheckResult(
                "python_dependencies",
                "fail",
                "resposta inválida do diagnóstico: " + str(exc),
                str(runtime.python),
            ),
            {},
        )

    mismatches = []
    if tuple(probe.get("python_version", ())) != EXPECTED_PYTHON_VERSION:
        mismatches.append(
            "Python "
            f"{probe.get('python_version')} != {list(EXPECTED_PYTHON_VERSION)}"
        )
    if probe.get("numpy_version") != EXPECTED_NUMPY_VERSION:
        mismatches.append(
            f"NumPy {probe.get('numpy_version')} != {EXPECTED_NUMPY_VERSION}"
        )
    if probe.get("numpy_tree_sha256") != EXPECTED_NUMPY_TREE_SHA256:
        mismatches.append("SHA da árvore NumPy divergente")
    if probe.get("machine") != EXPECTED_MACHINE:
        mismatches.append(
            f"arquitetura {probe.get('machine')} != {EXPECTED_MACHINE}"
        )
    if probe.get("system") != EXPECTED_SYSTEM:
        mismatches.append(
            f"sistema {probe.get('system')} != {EXPECTED_SYSTEM}"
        )
    if mismatches:
        return (
            CheckResult(
                "python_dependencies",
                "fail",
                "; ".join(mismatches),
                str(runtime.python),
                EXPECTED_NUMPY_TREE_SHA256,
                probe.get("numpy_tree_sha256"),
            ),
            probe,
        )
    return (
        CheckResult(
            "python_dependencies",
            "pass",
            (
                f"Python {'.'.join(map(str, EXPECTED_PYTHON_VERSION))}, "
                f"NumPy {EXPECTED_NUMPY_VERSION} e árvore NumPy confirmados"
            ),
            str(runtime.python),
            EXPECTED_NUMPY_TREE_SHA256,
            probe["numpy_tree_sha256"],
        ),
        probe,
    )


def preflight_checks(
    runtime: RuntimePaths,
    fixtures: FixturePaths,
) -> Tuple[List[CheckResult], Dict[str, Any]]:
    checks = [
        file_check("python", runtime.python, EXPECTED_SHA256["python"], True),
        file_check("runner", runtime.runner, EXPECTED_SHA256["runner"]),
        file_check(
            "runner_core",
            runtime.runner_core,
            EXPECTED_SHA256["runner_core"],
        ),
        file_check(
            "input_frontend",
            runtime.input_frontend,
            EXPECTED_SHA256["input_frontend"],
        ),
        file_check(
            "runtime_dsp",
            runtime.runtime_dsp,
            EXPECTED_SHA256["runtime_dsp"],
        ),
        file_check(
            "deep_filter",
            runtime.deep_filter,
            EXPECTED_SHA256["deep_filter"],
            True,
        ),
        file_check(
            "rnnoise_vad",
            runtime.rnnoise_vad,
            EXPECTED_SHA256["rnnoise_vad"],
            True,
        ),
        file_check(
            "input_fixture",
            fixtures.input_fixture,
            EXPECTED_SHA256["input_fixture"],
        ),
        file_check(
            "golden_mono",
            fixtures.golden_mono,
            EXPECTED_SHA256["golden_mono"],
        ),
        file_check(
            "golden_stereo",
            fixtures.golden_stereo,
            EXPECTED_SHA256["golden_stereo"],
        ),
    ]
    dependency_check, dependency_probe = python_dependency_check(runtime)
    checks.append(dependency_check)
    return checks, dependency_probe


def read_wave(path: Path) -> WaveData:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.getnframes()
            compression = source.getcomptype()
            payload = source.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise ContractFailure(f"{path}: WAV inválido: {exc}") from exc
    expected_bytes = frames * channels * sample_width
    require(
        len(payload) == expected_bytes,
        f"{path}: payload tem {len(payload)} bytes; esperado {expected_bytes}",
    )
    return WaveData(
        path,
        sample_rate,
        channels,
        sample_width,
        frames,
        compression,
        payload,
    )


def assert_wave_format(
    data: WaveData,
    *,
    channels: int,
    sample_width: int,
    sample_rate: int,
    frames: int,
    label: str,
) -> None:
    require(data.compression == "NONE", f"{label}: WAV não é PCM")
    require(
        data.channels == channels,
        f"{label}: {data.channels} canais; esperado {channels}",
    )
    require(
        data.sample_width == sample_width,
        f"{label}: PCM {data.sample_width * 8}; esperado PCM {sample_width * 8}",
    )
    require(
        data.sample_rate == sample_rate,
        f"{label}: {data.sample_rate} Hz; esperado {sample_rate} Hz",
    )
    require(
        data.frames == frames,
        f"{label}: {data.frames} frames; esperado {frames}",
    )


def split_pcm24_stereo(payload: bytes) -> Tuple[bytes, bytes]:
    require(
        len(payload) % 6 == 0,
        "payload PCM24 estéreo não é divisível por um frame de 6 bytes",
    )
    channel_bytes = len(payload) // 2
    left = bytearray(channel_bytes)
    right = bytearray(channel_bytes)
    left[0::3] = payload[0::6]
    left[1::3] = payload[1::6]
    left[2::3] = payload[2::6]
    right[0::3] = payload[3::6]
    right[1::3] = payload[4::6]
    right[2::3] = payload[5::6]
    return bytes(left), bytes(right)


def stable_runtime_hashes(runtime: RuntimePaths) -> Dict[str, str]:
    return {
        "runner": sha256_file(runtime.runner),
        "runner_core": sha256_file(runtime.runner_core),
        "input_frontend": sha256_file(runtime.input_frontend),
        "runtime_dsp": sha256_file(runtime.runtime_dsp),
        "deep_filter": sha256_file(runtime.deep_filter),
        "rnnoise_vad": sha256_file(runtime.rnnoise_vad),
        "python": sha256_file(runtime.python),
    }


def parse_runner_stdout(stdout: str, label: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ContractFailure(
            f"{label}: stdout do runner não é um JSON único: {exc}"
        ) from exc
    require(isinstance(parsed, dict), f"{label}: stdout não é um objeto JSON")
    return parsed


def run_isolated_job(
    runtime: RuntimePaths,
    fixture: Path,
    suite_root: Path,
    label: str,
    mono: bool,
) -> Dict[str, Any]:
    job_root = suite_root / label
    private_tmp = job_root / "tmp"
    job_root.mkdir()
    private_tmp.mkdir()
    output = job_root / f"{label}.wav"
    diagnostics_path = job_root / f"{label}.json"
    command = [
        str(runtime.python),
        str(runtime.runner),
        str(fixture),
        str(output),
        "--deep-filter-bin",
        str(runtime.deep_filter),
        "--rnnoise-vad-bin",
        str(runtime.rnnoise_vad),
        "--diagnostics",
        str(diagnostics_path),
    ]
    if mono:
        command.append("--mono")
    environment = {
        **os.environ,
        "TMPDIR": str(private_tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LC_ALL": "C",
        "LANG": "C",
    }
    completed = subprocess.run(
        command,
        cwd=str(job_root),
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )
    require(
        completed.returncode == 0,
        (
            f"{label}: runner saiu com {completed.returncode}\n"
            f"stdout:\n{completed.stdout.strip()}\n"
            f"stderr:\n{completed.stderr.strip()}"
        ),
    )
    require(output.is_file(), f"{label}: WAV de saída não foi criado")
    require(
        diagnostics_path.is_file(),
        f"{label}: diagnóstico JSON não foi criado",
    )
    stdout = parse_runner_stdout(completed.stdout, label)
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))

    expected_files = {output.resolve(), diagnostics_path.resolve()}
    actual_files = {
        candidate.resolve()
        for candidate in job_root.rglob("*")
        if candidate.is_file()
    }
    require(
        actual_files == expected_files,
        (
            f"{label}: job escreveu arquivos inesperados: "
            f"{sorted(map(str, actual_files - expected_files))}"
        ),
    )
    require(
        not any(private_tmp.iterdir()),
        f"{label}: temporários internos não foram limpos",
    )
    require(
        Path(str(stdout.get("output", ""))).resolve() == output.resolve(),
        f"{label}: stdout aponta para outra saída",
    )
    require(
        Path(str(stdout.get("diagnostics", ""))).resolve()
        == diagnostics_path.resolve(),
        f"{label}: stdout aponta para outro diagnóstico",
    )
    files = diagnostics.get("files", {})
    require(
        Path(str(files.get("input", ""))).resolve() == fixture.resolve(),
        f"{label}: diagnóstico aponta para outra entrada",
    )
    require(
        Path(str(files.get("output", ""))).resolve() == output.resolve(),
        f"{label}: diagnóstico aponta para outra saída",
    )
    return {
        "label": label,
        "job_root": job_root,
        "output": output,
        "diagnostics_path": diagnostics_path,
        "stdout": stdout,
        "diagnostics": diagnostics,
        "sha256": sha256_file(output),
    }


def assert_diagnostics(
    job: Mapping[str, Any],
    *,
    channels: int,
    layout: str,
) -> None:
    label = str(job["label"])
    diagnostics = job["diagnostics"]
    require(
        diagnostics.get("independent_runtime") is True,
        f"{label}: independent_runtime não é true",
    )
    require(
        diagnostics.get("uses_capcut_runtime_or_model") is False,
        f"{label}: contrato de independência do CapCut foi violado",
    )
    require(
        diagnostics.get("settings") == CANONICAL_SETTINGS,
        f"{label}: FlowSettings divergiu do preset canônico",
    )
    input_info = diagnostics.get("input", {})
    output_info = diagnostics.get("output", {})
    require(
        input_info.get("sample_rate") == EXPECTED_SAMPLE_RATE
        and input_info.get("frames") == EXPECTED_FRAMES
        and input_info.get("file_channels") == 1,
        f"{label}: metadados da entrada divergentes",
    )
    require(
        output_info.get("sample_rate") == EXPECTED_SAMPLE_RATE
        and output_info.get("frames") == EXPECTED_FRAMES,
        f"{label}: duração/taxa do diagnóstico divergiu",
    )
    require(
        output_info.get("file_channels") == channels,
        f"{label}: canais do diagnóstico divergiram",
    )
    require(
        output_info.get("channel_layout") == layout,
        f"{label}: layout do diagnóstico divergiu",
    )
    require(
        output_info.get("channels_are_sample_identical") is (channels == 2),
        f"{label}: flag de canais idênticos divergiu",
    )


def normalized_signal_diagnostics(
    diagnostics: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = copy.deepcopy(dict(diagnostics))
    normalized.pop("files", None)
    output = normalized.get("output", {})
    for key in (
        "file_channels",
        "channel_layout",
        "channels_are_sample_identical",
    ):
        output.pop(key, None)
    return normalized


def verify_fixtures(fixtures: FixturePaths) -> Dict[str, Any]:
    input_wave = read_wave(fixtures.input_fixture)
    golden_mono = read_wave(fixtures.golden_mono)
    golden_stereo = read_wave(fixtures.golden_stereo)
    assert_wave_format(
        input_wave,
        channels=1,
        sample_width=2,
        sample_rate=EXPECTED_SAMPLE_RATE,
        frames=EXPECTED_FRAMES,
        label="fixture de entrada",
    )
    assert_wave_format(
        golden_mono,
        channels=1,
        sample_width=EXPECTED_SAMPLE_WIDTH,
        sample_rate=EXPECTED_SAMPLE_RATE,
        frames=EXPECTED_FRAMES,
        label="golden mono",
    )
    assert_wave_format(
        golden_stereo,
        channels=2,
        sample_width=EXPECTED_SAMPLE_WIDTH,
        sample_rate=EXPECTED_SAMPLE_RATE,
        frames=EXPECTED_FRAMES,
        label="golden estéreo",
    )
    left, right = split_pcm24_stereo(golden_stereo.payload)
    require(left == right, "golden estéreo não é dual-mono bit-exact")
    require(
        left == golden_mono.payload,
        "canais do golden estéreo diferem do golden mono",
    )
    return {
        "input": {
            "sample_rate": input_wave.sample_rate,
            "sample_width_bits": input_wave.sample_width * 8,
            "channels": input_wave.channels,
            "frames": input_wave.frames,
        },
        "output": {
            "sample_rate": golden_mono.sample_rate,
            "sample_width_bits": golden_mono.sample_width * 8,
            "mono_channels": golden_mono.channels,
            "stereo_channels": golden_stereo.channels,
            "frames": golden_mono.frames,
            "channel_pcm24_sha256": hashlib.sha256(left).hexdigest(),
        },
    }


def verify_generated_outputs(
    stereo_job: Mapping[str, Any],
    mono_job: Mapping[str, Any],
) -> Dict[str, Any]:
    require(
        stereo_job["sha256"] == EXPECTED_SHA256["golden_stereo"],
        (
            "saída estéreo não é bit-exact: "
            f"{stereo_job['sha256']} != {EXPECTED_SHA256['golden_stereo']}"
        ),
    )
    require(
        mono_job["sha256"] == EXPECTED_SHA256["golden_mono"],
        (
            "saída mono não é bit-exact: "
            f"{mono_job['sha256']} != {EXPECTED_SHA256['golden_mono']}"
        ),
    )
    stereo = read_wave(stereo_job["output"])
    mono = read_wave(mono_job["output"])
    assert_wave_format(
        stereo,
        channels=2,
        sample_width=EXPECTED_SAMPLE_WIDTH,
        sample_rate=EXPECTED_SAMPLE_RATE,
        frames=EXPECTED_FRAMES,
        label="saída estéreo",
    )
    assert_wave_format(
        mono,
        channels=1,
        sample_width=EXPECTED_SAMPLE_WIDTH,
        sample_rate=EXPECTED_SAMPLE_RATE,
        frames=EXPECTED_FRAMES,
        label="saída mono",
    )
    left, right = split_pcm24_stereo(stereo.payload)
    require(left == right, "saída estéreo: L e R não são bit-exact")
    require(left == mono.payload, "saída estéreo e mono divergem por amostra")
    channel_hash = hashlib.sha256(left).hexdigest()
    require(
        channel_hash == EXPECTED_CHANNEL_PCM24_SHA256,
        (
            f"hash PCM24 por canal {channel_hash} != "
            f"{EXPECTED_CHANNEL_PCM24_SHA256}"
        ),
    )
    assert_diagnostics(
        stereo_job,
        channels=2,
        layout="dual_mono_stereo",
    )
    assert_diagnostics(mono_job, channels=1, layout="mono")
    require(
        normalized_signal_diagnostics(stereo_job["diagnostics"])
        == normalized_signal_diagnostics(mono_job["diagnostics"]),
        "jobs estéreo e mono produziram diagnósticos de sinal diferentes",
    )
    require(
        Path(stereo_job["job_root"]).resolve()
        != Path(mono_job["job_root"]).resolve(),
        "jobs compartilharam o mesmo diretório",
    )
    return {
        "stereo_wav_sha256": stereo_job["sha256"],
        "mono_wav_sha256": mono_job["sha256"],
        "channel_pcm24_sha256": channel_hash,
        "channels_are_sample_identical": True,
        "stereo_matches_mono_sample_for_sample": True,
        "sample_rate": stereo.sample_rate,
        "sample_width_bits": stereo.sample_width * 8,
        "frames": stereo.frames,
    }


def print_preflight(checks: Iterable[CheckResult], runtime_mode: str) -> None:
    print(f"Runtime selecionado: {runtime_mode}")
    for check in checks:
        marker = "OK" if check.status == "pass" else "FALTA/ERRO"
        print(f"[{marker}] {check.name}: {check.detail}")
        if check.status != "pass" and check.path:
            print(f"  caminho: {check.path}")
        if (
            check.status != "pass"
            and check.expected_sha256
            and check.actual_sha256
        ):
            print(f"  esperado: {check.expected_sha256}")
            print(f"  obtido:   {check.actual_sha256}")


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=project_root_from_script(),
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        help="Padrão: PROJECT_ROOT/uxp_hybrid/runtime.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        help="Override explícito do Python congelado.",
    )
    parser.add_argument("--input-fixture", type=Path)
    parser.add_argument("--golden-mono", type=Path)
    parser.add_argument("--golden-stereo", type=Path)
    parser.add_argument(
        "--allow-development-fallback",
        action="store_true",
        help=(
            "Se o runtime empacotado estiver ausente, usa explicitamente "
            "independent_neural_flow_20260729 e o Python de desenvolvimento."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Valida runtime, dependências e fixtures sem renderizar.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime somente o relatório JSON.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parse_arguments(argv)
    project_root = arguments.project_root.resolve()
    runtime_root = (
        arguments.runtime_root.resolve()
        if arguments.runtime_root
        else project_root / "uxp_hybrid/runtime"
    )
    runtime = resolve_runtime(
        project_root,
        runtime_root,
        arguments.python,
        arguments.allow_development_fallback,
    )
    fixtures = resolve_fixtures(
        project_root,
        arguments.input_fixture,
        arguments.golden_mono,
        arguments.golden_stereo,
    )
    checks, dependency_probe = preflight_checks(runtime, fixtures)
    preflight_ok = all(check.status == "pass" for check in checks)
    report: Dict[str, Any] = {
        "status": "pass" if preflight_ok else "fail",
        "mode": "preflight" if arguments.preflight_only else "golden-contract",
        "runtime_mode": runtime.mode,
        "runtime": {
            "python": str(runtime.python),
            "runner": str(runtime.runner),
            "runner_core": str(runtime.runner_core),
            "input_frontend": str(runtime.input_frontend),
            "runtime_dsp": str(runtime.runtime_dsp),
            "deep_filter": str(runtime.deep_filter),
            "rnnoise_vad": str(runtime.rnnoise_vad),
        },
        "checks": [check.as_dict() for check in checks],
        "python_dependencies": dependency_probe,
    }
    if not arguments.json:
        print_preflight(checks, runtime.mode)
    if not preflight_ok:
        if arguments.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("\nRuntime incompleto ou divergente; nenhum render foi iniciado.")
        return 2

    try:
        report["fixtures"] = verify_fixtures(fixtures)
        if not arguments.preflight_only:
            hashes_before = stable_runtime_hashes(runtime)
            fixture_hashes_before = {
                "input": sha256_file(fixtures.input_fixture),
                "golden_mono": sha256_file(fixtures.golden_mono),
                "golden_stereo": sha256_file(fixtures.golden_stereo),
            }
            with tempfile.TemporaryDirectory(
                prefix="voz_neural_uxp_golden_"
            ) as temporary:
                suite_root = Path(temporary)
                stereo_job = run_isolated_job(
                    runtime,
                    fixtures.input_fixture,
                    suite_root,
                    "stereo",
                    mono=False,
                )
                mono_job = run_isolated_job(
                    runtime,
                    fixtures.input_fixture,
                    suite_root,
                    "mono",
                    mono=True,
                )
                report["outputs"] = verify_generated_outputs(
                    stereo_job,
                    mono_job,
                )
            hashes_after = stable_runtime_hashes(runtime)
            fixture_hashes_after = {
                "input": sha256_file(fixtures.input_fixture),
                "golden_mono": sha256_file(fixtures.golden_mono),
                "golden_stereo": sha256_file(fixtures.golden_stereo),
            }
            require(
                hashes_before == hashes_after,
                "runner/dependências foram modificados durante os jobs",
            )
            require(
                fixture_hashes_before == fixture_hashes_after,
                "uma fixture/golden foi modificada durante os jobs",
            )
            report["jobs_isolated"] = True
            report["runtime_unchanged_after_jobs"] = True
            report["fixtures_unchanged_after_jobs"] = True
    except (ContractFailure, json.JSONDecodeError, OSError) as exc:
        report["status"] = "fail"
        report["failure"] = str(exc)
        if arguments.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"\n[FALHA] {exc}")
        return 1

    report["status"] = "pass"
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif arguments.preflight_only:
        print("\nPreflight concluído: runtime e goldens estão íntegros.")
    else:
        print("\nContrato bit-exact aprovado.")
        print(
            "Estéreo: "
            f"{report['outputs']['stereo_wav_sha256']}"
        )
        print(f"Mono:    {report['outputs']['mono_wav_sha256']}")
        print("Jobs temporários isolados e removidos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
