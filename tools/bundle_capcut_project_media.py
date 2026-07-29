#!/usr/bin/env python3
"""Bundle the 30 WAV files inside the generated CapCut project package."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


WORKSPACE = Path(
    "/Users/lucas/.codex/.chatgpt-projects/"
    "g-p-6a67e7a43cb8819180b9733ac6523111"
)
PACKAGE = WORKSPACE / "capcut_project_audio_female_30_clips"
PROJECT_NAME = "CapCut_Audio_Feminino_30_Clipes"
DRAFT = PACKAGE / PROJECT_NAME
SOURCE_AUDIO = WORKSPACE / "capcut_blackbox_audio_pack_female_v3"
FINAL_MEDIA = (
    Path("/Users/lucas/Movies/CapCut/User Data/Projects/com.lveditor.draft")
    / PROJECT_NAME
    / "Media"
)


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))


def main() -> None:
    sources = sorted(SOURCE_AUDIO.rglob("*.wav"))
    if len(sources) != 30 or len({path.name for path in sources}) != 30:
        raise RuntimeError("O pacote precisa ter 30 WAVs com nomes únicos")

    media = DRAFT / "Media"
    if media.exists():
        raise RuntimeError(f"A pasta Media já existe; nada foi sobrescrito: {media}")
    media.mkdir()

    expected_paths = {}
    for source in sources:
        destination = media / source.name
        shutil.copy2(source, destination)
        expected_paths[source.name] = str(FINAL_MEDIA / source.name)

    for filename in ("draft_info.json", "draft_info.json.bak", "template-2.tmp"):
        path = DRAFT / filename
        draft = read_json(path)
        for audio in draft["materials"]["audios"]:
            name = audio["name"]
            if name not in expected_paths:
                raise RuntimeError(f"Áudio inesperado no projeto: {name}")
            audio["path"] = expected_paths[name]
        write_json(path, draft)

    meta_path = DRAFT / "draft_meta_info.json"
    meta = read_json(meta_path)
    found = 0
    for group in meta["draft_materials"]:
        if group["type"] != 0:
            continue
        for item in group["value"]:
            name = item["extra_info"]
            if name not in expected_paths:
                raise RuntimeError(f"Mídia inesperada nos metadados: {name}")
            item["file_Path"] = expected_paths[name]
            found += 1
    if found != 30:
        raise RuntimeError(f"Esperadas 30 mídias nos metadados, encontradas {found}")
    write_json(meta_path, meta)

    readme = (PACKAGE / "LEIA-ME.md").read_text(encoding="utf-8")
    readme += (
        "\nOs WAVs foram incorporados à pasta `Media` do próprio projeto, "
        "evitando a janela de mídia não vinculada do macOS.\n"
    )
    (PACKAGE / "LEIA-ME.md").write_text(readme, encoding="utf-8")

    print(
        json.dumps(
            {
                "draft": str(DRAFT),
                "bundled_media": len(sources),
                "final_media_path": str(FINAL_MEDIA),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
