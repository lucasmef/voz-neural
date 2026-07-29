#!/usr/bin/env python3
"""Monta o diretório carregável do Voz Neural sem publicar artefatos no Git."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HYBRID_ROOT = PROJECT_ROOT / "uxp_hybrid"
UXP_ROOT = HYBRID_ROOT / "uxp"
RUNTIME_ROOT = HYBRID_ROOT / "runtime"
ADDON = HYBRID_ROOT / "build" / "mac" / "arm64" / "voz-neural.uxpaddon"
OUTPUT = HYBRID_ROOT / "dist" / "Voz Neural"

EXPECTED = {
    "runtime/app/independent_voice_runner.py": (
        "6d4a902cff80ccbbffcdc94bc0a484dd"
        "34160186588fb61326386b8f6ff1e955"
    ),
    "runtime/app/independent_voice_core.py": (
        "3de2e11bd4f071fdeab437c4f09e9f59"
        "b6c855f9cef07e052e9ba18a5b62d244"
    ),
    "runtime/app/premiere_input_frontend.py": (
        "88ecb44e60de587cd671de9ffb084d76f"
        "0d39805f521e967b60534f4b07321c7"
    ),
    "runtime/app/runtime_dsp.py": (
        "e7c9d68837b22b9d58124b8eb618832"
        "8c74479abfcc3aed3974a56eb594e2249"
    ),
    "runtime/bin/deep-filter-0.5.6-aarch64-apple-darwin": (
        "4601e7f4e4c03e59a4c5b5000216ef3"
        "add3e808799cfccd95e14e83ea4611081"
    ),
    "runtime/bin/rnnoise_vad_arm64": (
        "b12848f27892a77e0b454b62ff5ce8e2"
        "8d23d754848f142960a8f85756457357"
    ),
    "runtime/presets/Wave48stereo24.epr": (
        "0246e861e1ea1f1d63aa0bf94794fefb"
        "d654b51379ffd3a8a80647cf60256262"
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)


def require_relocatable_tree(root: Path) -> None:
    canonical_root = root.resolve()
    for link in sorted(path for path in root.rglob("*") if path.is_symlink()):
        if link.readlink().is_absolute():
            raise RuntimeError(f"Link absoluto proibido no pacote: {link}")
        try:
            resolved = link.resolve(strict=True)
        except FileNotFoundError as error:
            raise RuntimeError(f"Link quebrado no pacote: {link}") from error
        if not resolved.is_relative_to(canonical_root):
            raise RuntimeError(f"Link escapa do pacote: {link}")


def copy_panel(destination: Path) -> None:
    require_file(UXP_ROOT / "manifest.json")
    require_file(UXP_ROOT / "index.html")
    require_file(UXP_ROOT / "main.js")
    shutil.copy2(UXP_ROOT / "manifest.json", destination / "manifest.json")
    shutil.copy2(UXP_ROOT / "index.html", destination / "index.html")
    shutil.copy2(UXP_ROOT / "main.js", destination / "main.js")
    shutil.copy2(UXP_ROOT / "styles.css", destination / "styles.css")
    shutil.copytree(UXP_ROOT / "js", destination / "js")


def main() -> None:
    require_file(ADDON)
    require_file(RUNTIME_ROOT / "presets" / "Wave48stereo24.epr")
    require_relocatable_tree(RUNTIME_ROOT)
    for relative, expected in EXPECTED.items():
        source = HYBRID_ROOT / relative
        require_file(source)
        actual = sha256(source)
        if actual != expected:
            raise RuntimeError(
                f"Hash inesperado para {source}: {actual}; esperado {expected}"
            )

    manifest = json.loads((UXP_ROOT / "manifest.json").read_text("utf-8"))
    addon_name = manifest.get("addon", {}).get("name")
    if addon_name != ADDON.name:
        raise RuntimeError(
            f"Manifesto pede {addon_name!r}, mas o addon é {ADDON.name!r}"
        )

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)
    copy_panel(OUTPUT)

    addon_destination = OUTPUT / "mac" / "arm64" / ADDON.name
    addon_destination.parent.mkdir(parents=True)
    shutil.copy2(ADDON, addon_destination)
    shutil.copytree(RUNTIME_ROOT, OUTPUT / "runtime", symlinks=True)

    build_info = {
        "pluginVersion": manifest["version"],
        "addonSha256": sha256(addon_destination),
        "runtime": {
            relative.removeprefix("runtime/"): expected
            for relative, expected in EXPECTED.items()
        },
    }
    (OUTPUT / "BUILD_INFO.json").write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"ok": True, "pluginPath": str(OUTPUT), **build_info},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
