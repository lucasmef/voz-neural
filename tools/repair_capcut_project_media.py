#!/usr/bin/env python3
"""Repair the installed CapCut draft by placing its WAVs inside the draft."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


WORKSPACE = Path(
    "/Users/lucas/.codex/.chatgpt-projects/"
    "g-p-6a67e7a43cb8819180b9733ac6523111"
)
PACKAGE = WORKSPACE / "capcut_project_audio_female_30_clips"
PROJECT_NAME = "CapCut_Audio_Feminino_30_Clipes"
SOURCE = PACKAGE / PROJECT_NAME
TARGET = (
    Path("/Users/lucas/Movies/CapCut/User Data/Projects/com.lveditor.draft")
    / PROJECT_NAME
)


def main() -> None:
    running = subprocess.run(
        ["pgrep", "-fl", "CapCut"],
        capture_output=True,
        text=True,
        check=False,
    )
    if running.stdout.strip():
        print("CAPCUT_RUNNING")
        print(running.stdout.strip())
        sys.exit(12)

    if not TARGET.is_dir():
        raise RuntimeError(f"Projeto instalado não encontrado: {TARGET}")
    if (TARGET / "Media").exists():
        raise RuntimeError("A pasta Media já existe; nada foi sobrescrito")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = PACKAGE / f"before_media_repair_{stamp}"
    backup.mkdir()
    for filename in (
        "draft_info.json",
        "draft_info.json.bak",
        "template-2.tmp",
        "draft_meta_info.json",
    ):
        shutil.copy2(TARGET / filename, backup / filename)

    shutil.copytree(SOURCE / "Media", TARGET / "Media")
    for filename in (
        "draft_info.json",
        "draft_info.json.bak",
        "template-2.tmp",
        "draft_meta_info.json",
    ):
        temporary = TARGET / f".{filename}.codex_{os.getpid()}"
        shutil.copy2(SOURCE / filename, temporary)
        os.replace(temporary, TARGET / filename)

    with (TARGET / "draft_info.json").open("r", encoding="utf-8") as handle:
        draft = json.load(handle)
    paths = [Path(item["path"]) for item in draft["materials"]["audios"]]
    if len(paths) != 30 or not all(path.is_file() for path in paths):
        raise RuntimeError("A validação dos 30 arquivos incorporados falhou")

    print(
        json.dumps(
            {
                "repaired": str(TARGET),
                "media_files": len(paths),
                "backup": str(backup),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
