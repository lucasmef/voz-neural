#!/usr/bin/env python3
"""Install the generated audio test draft into the local CapCut project library."""

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
CAPCUT_ROOT = Path(
    "/Users/lucas/Movies/CapCut/User Data/Projects/com.lveditor.draft"
)
TARGET = CAPCUT_ROOT / PROJECT_NAME
ROOT_META = CAPCUT_ROOT / "root_meta_info.json"


def main() -> None:
    if not SOURCE.is_dir():
        raise RuntimeError(f"Pacote não encontrado: {SOURCE}")
    if TARGET.exists():
        raise RuntimeError(
            f"O projeto de destino já existe; nada foi sobrescrito: {TARGET}"
        )

    process_check = subprocess.run(
        ["pgrep", "-fl", "CapCut"],
        capture_output=True,
        text=True,
        check=False,
    )
    if process_check.stdout.strip():
        print("CAPCUT_RUNNING")
        print(process_check.stdout.strip())
        sys.exit(12)

    with ROOT_META.open("r", encoding="utf-8") as handle:
        root_meta = json.load(handle)
    with (PACKAGE / "root_meta_entry.json").open("r", encoding="utf-8") as handle:
        root_entry = json.load(handle)

    existing = root_meta.get("all_draft_store", [])
    for entry in existing:
        if (
            entry.get("draft_id") == root_entry["draft_id"]
            or entry.get("draft_fold_path") == str(TARGET)
            or entry.get("draft_name") == PROJECT_NAME
        ):
            raise RuntimeError("Já existe uma entrada equivalente; nada foi alterado")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = PACKAGE / f"root_meta_info.before_install_{stamp}.json"
    shutil.copy2(ROOT_META, backup)

    shutil.copytree(SOURCE, TARGET)
    root_meta["all_draft_store"].append(root_entry)
    root_meta["draft_ids"] = int(root_meta.get("draft_ids", 0)) + 1

    temporary = CAPCUT_ROOT / f".root_meta_info.codex_{os.getpid()}.json"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(root_meta, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ROOT_META)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        if TARGET.exists():
            shutil.rmtree(TARGET)
        raise

    with ROOT_META.open("r", encoding="utf-8") as handle:
        verified = json.load(handle)
    matches = [
        entry
        for entry in verified["all_draft_store"]
        if entry.get("draft_fold_path") == str(TARGET)
    ]
    if len(matches) != 1:
        raise RuntimeError("Falha na verificação final do índice do CapCut")

    print(
        json.dumps(
            {
                "installed": str(TARGET),
                "project_name": PROJECT_NAME,
                "backup": str(backup),
                "root_entries": len(verified["all_draft_store"]),
                "duration_us": root_entry["tm_duration"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
