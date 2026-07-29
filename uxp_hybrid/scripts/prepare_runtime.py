#!/usr/bin/env python3
"""Monta, fora do Git, o runtime bit-exact usado pelo addon UXP Hybrid."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FLOW_ROOT = PROJECT_ROOT / "independent_neural_flow_20260729"
RUNTIME_SOURCE_ROOT = PROJECT_ROOT / "uxp_hybrid" / "runtime_source"
RUNTIME_ROOT = PROJECT_ROOT / "uxp_hybrid" / "runtime"
DEFAULT_PYTHON_ROOT = (
    Path.home()
    / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python"
)
PREMIERE_REFERENCE_PRESET = Path(
    "/Applications/Adobe Premiere Pro 2026/"
    "Adobe Premiere Pro 2026.app/Contents/Settings/EncoderPresets/"
    "Wave48mono24.epr"
)
PREMIERE_REFERENCE_PRESET_SHA256 = (
    "a4a5b7bedec474e76131a260185eecfc"
    "1b0dbcb7204e9ec687a4ce433b2d1486"
)
PREMIERE_STEREO_PRESET_SHA256 = (
    "0246e861e1ea1f1d63aa0bf94794fefb"
    "d654b51379ffd3a8a80647cf60256262"
)
PREMIERE_STEREO_PRESET_ID = "746f3f31-e3d9-4b84-9f7d-92136a4f6f3c"

EXPECTED = {
    "premiere_runner": (
        "6d4a902cff80ccbbffcdc94bc0a484dd"
        "34160186588fb61326386b8f6ff1e955"
    ),
    "independent_voice_core": (
        "3de2e11bd4f071fdeab437c4f09e9f59"
        "b6c855f9cef07e052e9ba18a5b62d244"
    ),
    "premiere_input_frontend": (
        "88ecb44e60de587cd671de9ffb084d76f"
        "0d39805f521e967b60534f4b07321c7"
    ),
    "runtime_dsp": (
        "e7c9d68837b22b9d58124b8eb618832"
        "8c74479abfcc3aed3974a56eb594e2249"
    ),
    "deep-filter-0.5.6-aarch64-apple-darwin": (
        "4601e7f4e4c03e59a4c5b5000216ef3"
        "add3e808799cfccd95e14e83ea4611081"
    ),
    "rnnoise_vad_arm64": (
        "b12848f27892a77e0b454b62ff5ce8e2"
        "8d23d754848f142960a8f85756457357"
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_source(path: Path, expected_key: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(path)
    expected = EXPECTED[expected_key]
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError(
            f"Hash inesperado para {path}: {actual}; esperado {expected}"
        )
    return path


def create_stereo_preset(source: Path, destination: Path) -> None:
    """Deriva, de forma determinística, o preset PCM estéreo oficial.

    O Premiere 26.3 inclui apenas ``Wave48mono24.epr``. O formato EPR é XML;
    preservamos todos os parâmetros oficiais e mudamos somente o UUID, o nome
    e ``ADBEAudioNumChannels`` de 1 para 2. Assim o AME mantém os canais
    físicos da mídia em vez de aplicar uma lei de pan durante o downmix.
    """

    if sha256(source) != PREMIERE_REFERENCE_PRESET_SHA256:
        raise RuntimeError(
            "O preset Wave48mono24 do Premiere mudou; build cancelado."
        )
    contents = source.read_text(encoding="utf-8")
    replacements = (
        (
            "ebb0275b-5e1c-4de3-b2db-48301bb1d614",
            PREMIERE_STEREO_PRESET_ID,
        ),
        ("Wave48mono24", "Wave48stereo24"),
        ("<ParamValue>1</ParamValue>", "<ParamValue>2</ParamValue>"),
    )
    for old, new in replacements:
        if contents.count(old) != 1:
            raise RuntimeError(
                f"O preset oficial não contém exatamente uma ocorrência de {old!r}."
            )
        contents = contents.replace(old, new)
    encoded = contents.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != PREMIERE_STEREO_PRESET_SHA256:
        raise RuntimeError(
            "O preset Wave48stereo24 derivado divergiu do contrato."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)


def make_symlinks_relocatable(root: Path) -> None:
    """Reescreve links absolutos do runtime e rejeita escapes da árvore."""

    canonical_root = root.resolve()
    for link in sorted(path for path in root.rglob("*") if path.is_symlink()):
        raw_target = os.readlink(link)
        if os.path.isabs(raw_target):
            sibling = link.parent / Path(raw_target).name
            candidates = (
                [sibling]
                if sibling.exists()
                else list(root.rglob(Path(raw_target).name))
            )
            candidates = [candidate for candidate in candidates if candidate.exists()]
            if len(candidates) != 1:
                raise RuntimeError(
                    f"Link absoluto não relocável: {link} -> {raw_target}"
                )
            relative_target = os.path.relpath(candidates[0], start=link.parent)
            link.unlink()
            link.symlink_to(relative_target)

        try:
            resolved = link.resolve(strict=True)
        except FileNotFoundError as error:
            raise RuntimeError(f"Link quebrado no runtime: {link}") from error
        if not resolved.is_relative_to(canonical_root):
            raise RuntimeError(
                f"Link escapa do runtime: {link} -> {os.readlink(link)}"
            )


def copy_runtime(python_root: Path, refresh: bool) -> dict[str, str]:
    if not python_root.is_dir():
        raise FileNotFoundError(python_root)
    python_executable = python_root / "bin" / "python3"
    if not python_executable.is_file():
        raise FileNotFoundError(python_executable)

    runtime_python = RUNTIME_ROOT / "python"
    if refresh and runtime_python.exists():
        shutil.rmtree(runtime_python)
    if not runtime_python.exists():
        shutil.copytree(python_root, runtime_python, symlinks=True)
    make_symlinks_relocatable(runtime_python)

    app = RUNTIME_ROOT / "app"
    binary = RUNTIME_ROOT / "bin"
    presets = RUNTIME_ROOT / "presets"
    app.mkdir(parents=True, exist_ok=True)
    binary.mkdir(parents=True, exist_ok=True)
    presets.mkdir(parents=True, exist_ok=True)

    sources = [
        (
            checked_source(
                RUNTIME_SOURCE_ROOT / "independent_voice_runner.py",
                "premiere_runner",
            ),
            app / "independent_voice_runner.py",
        ),
        (
            checked_source(
                FLOW_ROOT / "independent_voice_runner.py",
                "independent_voice_core",
            ),
            app / "independent_voice_core.py",
        ),
        (
            checked_source(
                RUNTIME_SOURCE_ROOT / "premiere_input_frontend.py",
                "premiere_input_frontend",
            ),
            app / "premiere_input_frontend.py",
        ),
        (
            checked_source(FLOW_ROOT / "runtime_dsp.py", "runtime_dsp"),
            app / "runtime_dsp.py",
        ),
    ]
    binaries = [
        checked_source(
            FLOW_ROOT
            / "bin"
            / "deep-filter-0.5.6-aarch64-apple-darwin",
            "deep-filter-0.5.6-aarch64-apple-darwin",
        ),
        checked_source(
            FLOW_ROOT / "bin" / "rnnoise_vad_arm64",
            "rnnoise_vad_arm64",
        ),
    ]
    for source, target in sources:
        shutil.copy2(source, target)
    for source in binaries:
        target = binary / source.name
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | 0o111)

    extraction_preset = presets / "Wave48stereo24.epr"
    create_stereo_preset(PREMIERE_REFERENCE_PRESET, extraction_preset)
    stale_mono_preset = presets / "Wave48mono24.epr"
    if stale_mono_preset.exists():
        stale_mono_preset.unlink()

    runtime_executable = runtime_python / "bin" / "python3"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    probe = subprocess.run(
        [
            str(runtime_executable),
            "-c",
            (
                "import json, platform, sys, numpy; "
                "print(json.dumps({'python': platform.python_version(), "
                "'machine': platform.machine(), 'numpy': numpy.__version__, "
                "'executable': sys.executable}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    versions = json.loads(probe.stdout)
    if versions["python"] != "3.12.13" or versions["numpy"] != "2.3.5":
        raise RuntimeError(f"Runtime incompatível: {versions}")
    if versions["machine"] != "arm64":
        raise RuntimeError(f"Arquitetura incompatível: {versions['machine']}")

    return {
        **versions,
        "runtimeRoot": str(RUNTIME_ROOT),
        "runner": str(app / "independent_voice_runner.py"),
        "deepFilter": str(
            binary / "deep-filter-0.5.6-aarch64-apple-darwin"
        ),
        "rnnoiseVad": str(binary / "rnnoise_vad_arm64"),
        "extractionPresetPath": str(extraction_preset),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python-root",
        type=Path,
        default=Path(
            os.environ.get("CODEX_VOICE_PYTHON_ROOT", DEFAULT_PYTHON_ROOT)
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Reconstrói a cópia local do Python canônico.",
    )
    arguments = parser.parse_args()
    result = copy_runtime(arguments.python_root.resolve(), arguments.refresh)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
