#!/usr/bin/env python3
"""Build a clean CapCut audio-only draft from a known-good local template."""

from __future__ import annotations

import copy
import csv
import json
import shutil
import time
import uuid
import wave
from pathlib import Path


WORKSPACE = Path(
    "/Users/lucas/.codex/.chatgpt-projects/"
    "g-p-6a67e7a43cb8819180b9733ac6523111"
)
SOURCE_AUDIO = WORKSPACE / "capcut_blackbox_audio_pack_female_v3"
CAPCUT_ROOT = Path(
    "/Users/lucas/Movies/CapCut/User Data/Projects/com.lveditor.draft"
)
TEMPLATE = CAPCUT_ROOT / "0727 (1)"
LOCAL_AUDIO_TEMPLATE_PROJECT = CAPCUT_ROOT / "0719"
PROJECT_NAME = "CapCut_Audio_Feminino_30_Clipes"
FINAL_DRAFT_DIR = CAPCUT_ROOT / PROJECT_NAME
OUTPUT_ROOT = WORKSPACE / "capcut_project_audio_female_30_clips"
OUTPUT_DRAFT = OUTPUT_ROOT / PROJECT_NAME
GAP_US = 500_000


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))


def wav_duration_us(path: Path) -> int:
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != 48_000:
            raise ValueError(f"Taxa inesperada em {path}: {audio.getframerate()}")
        return round(audio.getnframes() / audio.getframerate() * 1_000_000)


def tc(us: int) -> str:
    milliseconds = round(us / 1_000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def find_local_audio_prototypes() -> tuple[dict, dict]:
    draft = read_json(LOCAL_AUDIO_TEMPLATE_PROJECT / "draft_info.json")
    material = next(
        item
        for item in draft["materials"]["audios"]
        if item.get("path", "").endswith("/71.wav")
    )
    meta = read_json(LOCAL_AUDIO_TEMPLATE_PROJECT / "draft_meta_info.json")
    meta_material = next(
        item
        for group in meta["draft_materials"]
        for item in group.get("value", [])
        if item.get("file_Path", "").endswith("/71.wav")
    )
    return material, meta_material


def main() -> None:
    files = sorted(SOURCE_AUDIO.rglob("*.wav"))
    if len(files) != 30:
        raise RuntimeError(f"Esperados 30 WAVs, encontrados {len(files)}")

    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_DRAFT.mkdir(parents=True)

    draft = read_json(TEMPLATE / "draft_info.json")
    meta = read_json(TEMPLATE / "draft_meta_info.json")
    local_audio_proto, meta_audio_proto = find_local_audio_prototypes()

    template_audio_track = next(
        track for track in draft["tracks"] if track["type"] == "audio"
    )
    template_segment = template_audio_track["segments"][0]
    clean_ref_ids = template_segment["extra_material_refs"]
    reference_objects: dict[str, tuple[str, dict]] = {}
    for category, items in draft["materials"].items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id") in clean_ref_ids:
                reference_objects[item["id"]] = (category, item)

    expected_ref_categories = {
        "speeds",
        "placeholder_infos",
        "beats",
        "sound_channel_mappings",
        "vocal_separations",
    }
    if {value[0] for value in reference_objects.values()} != expected_ref_categories:
        raise RuntimeError("O conjunto de referências limpas do modelo mudou")

    for category, items in draft["materials"].items():
        if isinstance(items, list):
            draft["materials"][category] = []

    content_id = new_id()
    meta_id = new_id()
    audio_track = copy.deepcopy(template_audio_track)
    audio_track["id"] = new_id()
    audio_track["segments"] = []
    video_track = next(
        copy.deepcopy(track) for track in draft["tracks"] if track["type"] == "video"
    )
    video_track["id"] = new_id()
    video_track["segments"] = []
    draft["tracks"] = [video_track, audio_track]

    position_us = 0
    timeline_rows: list[dict[str, object]] = []
    meta_audio_items: list[dict] = []

    for index, audio_path in enumerate(files, start=1):
        duration_us = wav_duration_us(audio_path)
        material_id = new_id()
        local_material_id = new_id()

        audio_material = copy.deepcopy(local_audio_proto)
        audio_material.update(
            {
                "id": material_id,
                "type": "extract_music",
                "name": audio_path.name,
                "duration": duration_us,
                "path": str(audio_path),
                "category_name": "local",
                "wave_points": [],
                "music_id": str(uuid.uuid4()),
                "app_id": 0,
                "effect_id": "",
                "check_flag": 3,
                "local_material_id": local_material_id,
                "request_id": "",
            }
        )
        draft["materials"]["audios"].append(audio_material)

        refs: list[str] = []
        for old_ref_id in clean_ref_ids:
            category, reference = reference_objects[old_ref_id]
            cloned = copy.deepcopy(reference)
            cloned["id"] = new_id()
            if category == "beats":
                cloned["ai_beats"] = {
                    "melody_url": "",
                    "melody_path": "",
                    "beats_url": "",
                    "beats_path": "",
                    "melody_percents": [0.0],
                    "beat_speed_infos": [],
                }
            refs.append(cloned["id"])
            draft["materials"][category].append(cloned)

        segment = copy.deepcopy(template_segment)
        segment.update(
            {
                "id": new_id(),
                "source_timerange": {"start": 0, "duration": duration_us},
                "target_timerange": {
                    "start": position_us,
                    "duration": duration_us,
                },
                "material_id": material_id,
                "extra_material_refs": refs,
                "volume": 1.0,
                "last_nonzero_volume": 1.0,
                "track_render_index": 1,
                "segment_color_tag": "",
            }
        )
        audio_track["segments"].append(segment)

        meta_material = copy.deepcopy(meta_audio_proto)
        meta_material.update(
            {
                "duration": duration_us,
                "extra_info": audio_path.name,
                "file_Path": str(audio_path),
                "id": local_material_id,
                "roughcut_time_range": {"duration": duration_us, "start": 0},
            }
        )
        meta_audio_items.append(meta_material)

        timeline_rows.append(
            {
                "ordem": index,
                "categoria": audio_path.parent.name,
                "arquivo": audio_path.name,
                "inicio": tc(position_us),
                "fim": tc(position_us + duration_us),
                "inicio_us": position_us,
                "duracao_us": duration_us,
            }
        )
        position_us += duration_us
        if index != len(files):
            position_us += GAP_US

    now_us = time.time_ns() // 1_000
    draft.update(
        {
            "id": content_id,
            "name": "",
            "path": "",
            "duration": position_us,
            "create_time": 0,
            "update_time": 0,
            "source": "default",
        }
    )
    draft["config"]["record_audio_last_index"] = 1
    draft["config"]["extract_audio_last_index"] = len(files) + 1
    draft["config"]["original_sound_last_index"] = 1
    draft["group_container"] = {"groups": []}
    draft["relationships"] = []
    draft["keyframe_graph_list"] = []
    for key, value in draft["keyframes"].items():
        if isinstance(value, list):
            draft["keyframes"][key] = []

    meta.update(
        {
            "draft_fold_path": str(FINAL_DRAFT_DIR),
            "draft_id": meta_id,
            "draft_name": PROJECT_NAME,
            "draft_root_path": str(CAPCUT_ROOT),
            "draft_timeline_materials_size_": sum(
                path.stat().st_size for path in files
            ),
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
            "tm_duration": position_us,
        }
    )
    for group in meta["draft_materials"]:
        group["value"] = meta_audio_items if group["type"] == 0 else []

    write_json(OUTPUT_DRAFT / "draft_info.json", draft)
    write_json(OUTPUT_DRAFT / "draft_info.json.bak", draft)
    write_json(OUTPUT_DRAFT / "template-2.tmp", draft)
    write_json(OUTPUT_DRAFT / "draft_meta_info.json", meta)

    for filename in (
        "attachment_pc_common.json",
        "draft_agency_config.json",
        "draft_biz_config.json",
        "draft_settings",
        "performance_opt_info.json",
        "timeline_layout.json",
        "draft_cover.jpg",
    ):
        shutil.copy2(TEMPLATE / filename, OUTPUT_DRAFT / filename)
    write_json(OUTPUT_DRAFT / "key_value.json", {})

    root_entry = {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": str(FINAL_DRAFT_DIR / "draft_cover.jpg"),
        "draft_fold_path": str(FINAL_DRAFT_DIR),
        "draft_id": meta_id,
        "draft_is_ai_shorts": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_invisible": False,
        "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_json_file": str(FINAL_DRAFT_DIR / "draft_info.json"),
        "draft_name": PROJECT_NAME,
        "draft_new_version": "",
        "draft_root_path": str(CAPCUT_ROOT),
        "draft_timeline_materials_size": sum(path.stat().st_size for path in files),
        "draft_type": "",
        "draft_web_article_video_enter_from": "",
        "pippit_avatar_url": "",
        "pippit_extra_info": "",
        "pippit_id": "",
        "pippit_user_name": "",
        "streaming_edit_draft_ready": True,
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": position_us,
    }
    write_json(OUTPUT_ROOT / "root_meta_entry.json", root_entry)

    with (OUTPUT_ROOT / "MAPA_TIMECODES.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=timeline_rows[0].keys())
        writer.writeheader()
        writer.writerows(timeline_rows)

    readme = f"""# Projeto CapCut — teste de áudio feminino

- Projeto: `{PROJECT_NAME}`
- Clipes: {len(files)}, todos independentes
- Intervalo entre clipes: 0,5 s
- Duração total: {tc(position_us)}
- Estado inicial: nenhum dos quatro tratamentos está aplicado

No CapCut, selecione os 30 clipes de áudio e aplique:

1. Normalização de volume
2. Redução de ruído
3. Aprimoramento de voz
4. Foco na voz

Exporte em WAV sem mudar a ordem dos clipes. O arquivo `MAPA_TIMECODES.csv`
permite relacionar cada trecho exportado à amostra original.
"""
    (OUTPUT_ROOT / "LEIA-ME.md").write_text(readme, encoding="utf-8")

    print(
        json.dumps(
            {
                "output": str(OUTPUT_ROOT),
                "draft": str(OUTPUT_DRAFT),
                "project_name": PROJECT_NAME,
                "clips": len(files),
                "duration_us": position_us,
                "duration_tc": tc(position_us),
                "meta_id": meta_id,
                "content_id": content_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
