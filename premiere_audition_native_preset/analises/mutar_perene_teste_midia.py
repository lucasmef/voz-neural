#!/usr/bin/env python3
"""Cria uma copia descartavel do PERENE com uma unica midia substituida.

O mutador e deliberadamente estreito: aceita somente o baseline PERENE e o WAV
94 congelados pelos hashes abaixo, escreve apenas em projetos_descartaveis e
interrompe se qualquer ID, referencia, valor antigo ou estrutura esperada nao
for exatamente o previsto. O projeto de entrada nunca e regravado.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, MutableMapping, Tuple


BASELINE_SHA256 = "9985c1397318f3fb62a370ad566a04c1bc8b9849954209790eb9f625f33ae895"
MEDIA_SHA256 = "33c7e79b9b39034492a516ad6c21edcca023816d306c1266caa5858776a6a175"

OLD_DURATION_TICKS = "6000429456000"
PREMIERE_TICKS_PER_SECOND = 254_016_000_000
EXPECTED_WAV = {
    "format": "WAVE_FORMAT_EXTENSIBLE_PCM",
    "channels": 2,
    "sample_rate": 48_000,
    "bits_per_sample": 24,
    "block_align": 6,
    "frames": 1_156_800,
}

TEST_NAME = "TEST_94"
MEDIA_FILENAME = "94_RAW_DJI_NEW.wav"
MEDIA_RELATIVE_PATH = f"../comparacao_ab/{MEDIA_FILENAME}"

IDS = {
    "sequence_uuid": "50726d5c-d418-4bef-bef8-5ca28787257d",
    "sequence_project_item_uid": "3aad6dad-32d7-4e59-a3f4-794a0ecb4aa7",
    "sequence_master_clip_uid": "f60a3488-abf3-4563-ad95-6fc6218ce917",
    "media_project_item_uid": "b4f84336-9311-4448-81eb-b1a9047fc0f7",
    "media_master_clip_uid": "f79ae388-70b7-4689-9a03-231b2a6cc043",
    "media_uid": "95abf2fc-1f46-4204-9e11-e1d9f76bf2b3",
    "clip_logging_info_object_id": "50",
    "audio_media_source_object_id": "163",
    "audio_sequence_source_object_id": "169",
    "video_sequence_source_object_id": "172",
    "audio_stream_object_id": "343",
    "audio_clip_track_item_object_id": "407",
    "timeline_component_chain_object_id": "484",
    "subclip_object_id": "485",
    "timeline_audio_clip_object_id": "670",
    "intrinsic_volume_component_object_id": "665",
    "intrinsic_channel_volume_component_object_id": "666",
    "enhance_speech_component_object_id": "487",
    "essential_sound_limiter_component_object_id": "667",
    "parametric_eq_component_object_id": "668",
    "final_limiter_component_object_id": "669",
    "raw_master_audio_clip_object_id": "52",
    "stale_transcript_clip_object_id": "53",
}

TARGET_BLOCKS: Tuple[Tuple[str, str, str], ...] = (
    ("ClipProjectItem", "ObjectUID", IDS["media_project_item_uid"]),
    ("ClipProjectItem", "ObjectUID", IDS["sequence_project_item_uid"]),
    ("MasterClip", "ObjectUID", IDS["media_master_clip_uid"]),
    ("MasterClip", "ObjectUID", IDS["sequence_master_clip_uid"]),
    ("ClipLoggingInfo", "ObjectID", IDS["clip_logging_info_object_id"]),
    ("AudioMediaSource", "ObjectID", IDS["audio_media_source_object_id"]),
    ("AudioSequenceSource", "ObjectID", IDS["audio_sequence_source_object_id"]),
    ("VideoSequenceSource", "ObjectID", IDS["video_sequence_source_object_id"]),
    ("Media", "ObjectUID", IDS["media_uid"]),
    ("Sequence", "ObjectUID", IDS["sequence_uuid"]),
    ("AudioStream", "ObjectID", IDS["audio_stream_object_id"]),
    ("AudioClipTrackItem", "ObjectID", IDS["audio_clip_track_item_object_id"]),
    ("AudioComponentChain", "ObjectID", IDS["timeline_component_chain_object_id"]),
    ("SubClip", "ObjectID", IDS["subclip_object_id"]),
    ("AudioClip", "ObjectID", IDS["timeline_audio_clip_object_id"]),
)


class GuardError(RuntimeError):
    """Falha de uma trava de seguranca ou integridade."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def require_exact_path(path: Path, expected: Path, label: str) -> None:
    require(path.resolve() == expected.resolve(), f"{label} fora do alvo permitido: {path}")


def require_direct_child(path: Path, parent: Path, label: str) -> None:
    require(path.resolve().parent == parent.resolve(), f"{label} deve ficar diretamente em {parent}")


def read_wav_extensible_pcm(path: Path) -> Dict[str, int | str]:
    """Le cabecalho RIFF/WAVE, inclusive WAVE_FORMAT_EXTENSIBLE PCM."""
    with path.open("rb") as handle:
        header = handle.read(12)
        require(len(header) == 12, "WAV truncado antes do cabecalho RIFF")
        riff, _riff_size, wave = struct.unpack("<4sI4s", header)
        require(riff == b"RIFF" and wave == b"WAVE", "midia nao e RIFF/WAVE little-endian")

        fmt_data = None
        data_size = None
        while True:
            chunk_header = handle.read(8)
            if not chunk_header:
                break
            require(len(chunk_header) == 8, "WAV truncado no cabecalho de chunk")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            chunk_data = handle.read(chunk_size)
            require(len(chunk_data) == chunk_size, f"WAV truncado no chunk {chunk_id!r}")
            if chunk_size & 1:
                require(len(handle.read(1)) == 1, "WAV truncado no padding de chunk")
            if chunk_id == b"fmt ":
                fmt_data = chunk_data
            elif chunk_id == b"data":
                data_size = chunk_size

    require(fmt_data is not None and len(fmt_data) >= 16, "chunk fmt ausente ou invalido")
    require(data_size is not None, "chunk data ausente")
    format_tag, channels, sample_rate, byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", fmt_data[:16]
    )
    effective_format = format_tag
    format_name = "PCM"
    if format_tag == 0xFFFE:
        require(len(fmt_data) >= 40, "WAVE_FORMAT_EXTENSIBLE incompleto")
        cb_size, valid_bits, _channel_mask = struct.unpack("<HHI", fmt_data[16:24])
        require(cb_size >= 22, "extensao WAVE_FORMAT_EXTENSIBLE curta")
        effective_format = struct.unpack("<I", fmt_data[24:28])[0]
        require(valid_bits == bits, "valid bits difere de bits por amostra")
        format_name = "WAVE_FORMAT_EXTENSIBLE_PCM"
    require(effective_format == 1, f"formato WAV nao PCM: {effective_format}")
    require(block_align > 0 and data_size % block_align == 0, "data nao e multiplo de block_align")
    require(byte_rate == sample_rate * block_align, "byte_rate inconsistente")
    return {
        "format": format_name,
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits,
        "block_align": block_align,
        "frames": data_size // block_align,
        "data_bytes": data_size,
    }


def replace_exact(text: str, old: str, new: str, expected: int, label: str) -> str:
    count = text.count(old)
    require(count == expected, f"{label}: esperado {expected}, encontrado {count}")
    return text.replace(old, new)


def replace_regex(
    text: str,
    pattern: str,
    replacement: str | Callable[[re.Match[str]], str],
    expected: int,
    label: str,
    flags: int = 0,
) -> str:
    changed, count = re.subn(pattern, replacement, text, flags=flags)
    require(count == expected, f"{label}: esperado {expected}, encontrado {count}")
    return changed


def block_pattern(tag: str, attr: str, value: str) -> re.Pattern[str]:
    return re.compile(
        rf'(?ms)^\t<{re.escape(tag)}\b(?=[^\n]*\b{re.escape(attr)}="{re.escape(value)}")[^\n]*\n'
        rf'.*?^\t</{re.escape(tag)}>(?:\n|$)'
    )


def mutate_block(
    xml: str,
    tag: str,
    attr: str,
    value: str,
    mutator: Callable[[str], str],
) -> Tuple[str, str, str]:
    pattern = block_pattern(tag, attr, value)
    matches = list(pattern.finditer(xml))
    require(len(matches) == 1, f"bloco {tag} {attr}={value}: esperado 1, encontrado {len(matches)}")
    match = matches[0]
    before = match.group(0)
    after = mutator(before)
    require(after != before, f"bloco {tag} {attr}={value} nao foi alterado")
    return xml[: match.start()] + after + xml[match.end() :], before, after


def set_element_text(block: str, tag: str, old: str, new: str, label: str) -> str:
    return replace_exact(block, f"<{tag}>{old}</{tag}>", f"<{tag}>{new}</{tag}>", 1, label)


def mutate_media_project_item(block: str) -> str:
    return set_element_text(block, "Name", "76_RAW_CAL.wav", TEST_NAME, "nome do item de midia")


def mutate_sequence_project_item(block: str) -> str:
    return set_element_text(block, "Name", "76_RAW_CAL", TEST_NAME, "nome do item de sequencia")


def mutate_media_master_clip(block: str) -> str:
    block = replace_exact(
        block,
        '\t\t\t<Clip Index="1" ObjectRef="53"/>\n',
        "",
        1,
        "desanexar transcript antigo do master clip",
    )
    return set_element_text(block, "Name", "76_RAW_CAL.wav", TEST_NAME, "nome do master clip de midia")


def mutate_sequence_master_clip(block: str) -> str:
    return set_element_text(block, "Name", "76_RAW_CAL", TEST_NAME, "nome do master clip de sequencia")


def mutate_clip_logging_info(block: str, duration: str) -> str:
    block = set_element_text(block, "ClipName", "76_RAW_CAL.wav", TEST_NAME, "ClipLoggingInfo.ClipName")
    return set_element_text(
        block, "MediaOutPoint", OLD_DURATION_TICKS, duration, "ClipLoggingInfo.MediaOutPoint"
    )


def mutate_duration_only(block: str, duration: str, label: str) -> str:
    return set_element_text(block, "OriginalDuration", OLD_DURATION_TICKS, duration, label)


def mutate_media(block: str, media_path: Path) -> str:
    absolute = str(media_path.resolve())
    block = set_element_text(
        block,
        "RelativePath",
        "../exports_validacao/76_RAW_CAL.wav",
        MEDIA_RELATIVE_PATH,
        "Media.RelativePath principal",
    )
    block = set_element_text(
        block,
        "RelativePath",
        "../../Users/lucas/Documents/Codex/voz-neural/premiere_audition_native_preset/exports_validacao/76_RAW_CAL.wav",
        MEDIA_RELATIVE_PATH,
        "Media.RelativePath legado",
    )
    block = set_element_text(
        block,
        "FilePath",
        "/Users/lucas/Documents/Codex/voz-neural/premiere_audition_native_preset/exports_validacao/76_RAW_CAL.wav",
        absolute,
        "Media.FilePath",
    )
    block = set_element_text(block, "Title", "76_RAW_CAL.wav", MEDIA_FILENAME, "Media.Title")
    return set_element_text(
        block,
        "ActualMediaFilePath",
        "/Users/lucas/Documents/Codex/voz-neural/premiere_audition_native_preset/exports_validacao/76_RAW_CAL.wav",
        absolute,
        "Media.ActualMediaFilePath",
    )


def mutate_sequence(block: str, duration: str) -> str:
    block = set_element_text(
        block, "Monitor.ProgramZoomOut", OLD_DURATION_TICKS, duration, "Sequence.ProgramZoomOut"
    )
    block = set_element_text(block, "MZ.WorkOutPoint", OLD_DURATION_TICKS, duration, "Sequence.WorkOutPoint")
    block = replace_regex(
        block,
        r'(?m)^\t\t\t\t<(ExportSettings\.Info\.[^>]+)>.*?</\1>\n',
        "",
        8,
        "remover ExportSettings.Info obsoleto",
    )
    return set_element_text(block, "Name", "76_RAW_CAL", TEST_NAME, "Sequence.Name")


def mutate_audio_stream(block: str, duration: str) -> str:
    old_peak = "/Users/lucas/Library/Application Support/Adobe/Common/Peak Files/2026-08-05/76_RAW_CAL.wav 48000.pek"
    new_peak = "/Users/lucas/Library/Application Support/Adobe/Common/Peak Files/2026-08-05/94_RAW_DJI_NEW.wav 48000.pek"
    block = set_element_text(block, "ConformedAudioPath", old_peak, new_peak, "novo conform path")
    block = set_element_text(block, "PeakFilePath", old_peak, new_peak, "novo peak path")
    return set_element_text(block, "Duration", OLD_DURATION_TICKS, duration, "AudioStream.Duration")


def mutate_track_item(block: str, duration: str) -> str:
    block = replace_regex(
        block,
        r'(?ms)(^\t{5}<Properties Version="1">\n).*?(^\t{5}</Properties>\n)',
        lambda match: match.group(1) + match.group(2),
        1,
        "limpar propriedades Essential Sound/Enhance do TrackItem",
    )
    block = set_element_text(block, "End", OLD_DURATION_TICKS, duration, "TrackItem.End")
    for tag, value in (
        ("PreRenderComponentChainHashVersion", "1"),
        ("PreRenderComponentChain", '<PreRenderComponentChain ObjectRef="486"/>'),
        ("PreRenderMix", '<PreRenderMix ObjectRef="487"/>'),
        ("PreRenderSourceItem", '<PreRenderSourceItem ObjectRef="488"/>'),
    ):
        old_line = f"\t\t<{tag}>{value}</{tag}>\n" if tag.endswith("Version") else f"\t\t{value}\n"
        block = replace_exact(block, old_line, "", 1, f"remover {tag}")
    return block


def mutate_component_chain(block: str) -> str:
    replacement = (
        '\t\t\t<Components Version="1">\n'
        '\t\t\t\t<Component Index="0" ObjectRef="665"/>\n'
        '\t\t\t\t<Component Index="1" ObjectRef="666"/>\n'
        "\t\t\t</Components>\n"
    )
    return replace_regex(
        block,
        r'(?ms)^\t\t\t<Components Version="1">\n.*?^\t\t\t</Components>\n',
        replacement,
        1,
        "reduzir cadeia aos dois componentes intrinsecos",
    )


def mutate_subclip(block: str) -> str:
    return set_element_text(block, "Name", "76_RAW_CAL.wav", TEST_NAME, "SubClip.Name")


def mutate_timeline_audio_clip(block: str, duration: str) -> str:
    block = replace_regex(
        block,
        r'(?m)^\t{5}<(ESP\.LoudnessSection\.Analysis\.[^>]+)>.*?</\1>\n',
        "",
        4,
        "remover analise antiga de loudness",
    )
    block = set_element_text(block, "OutPoint", OLD_DURATION_TICKS, duration, "AudioClip.OutPoint")
    return set_element_text(block, "Gain", "1.2507641315460205", "1.0", "resetar ganho Auto-Match")


def top_level_key(index: int, node: ET.Element) -> str:
    for attr in ("ObjectID", "ObjectUID", "ObjectRef"):
        if attr in node.attrib:
            return f"{node.tag}:{attr}={node.attrib[attr]}"
    return f"{node.tag}:ordinal={index}"


def top_level_hashes(root: ET.Element) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for index, node in enumerate(list(root)):
        key = top_level_key(index, node)
        require(key not in result, f"chave top-level duplicada: {key}")
        result[key] = sha256_bytes(ET.tostring(node, encoding="utf-8"))
    return result


def find_top(root: ET.Element, tag: str, attr: str, value: str) -> ET.Element:
    found = [node for node in root.findall(tag) if node.get(attr) == value]
    require(len(found) == 1, f"XML final: {tag} {attr}={value}: esperado 1, encontrado {len(found)}")
    return found[0]


def child_text(node: ET.Element, path: str) -> str:
    child = node.find(path)
    require(child is not None and child.text is not None, f"elemento ausente: {node.tag}/{path}")
    return child.text


def validate_semantics(root: ET.Element, duration: str, media_path: Path) -> Dict[str, object]:
    sequence = find_top(root, "Sequence", "ObjectUID", IDS["sequence_uuid"])
    media_item = find_top(root, "ClipProjectItem", "ObjectUID", IDS["media_project_item_uid"])
    sequence_item = find_top(root, "ClipProjectItem", "ObjectUID", IDS["sequence_project_item_uid"])
    media_master = find_top(root, "MasterClip", "ObjectUID", IDS["media_master_clip_uid"])
    sequence_master = find_top(root, "MasterClip", "ObjectUID", IDS["sequence_master_clip_uid"])
    logging_info = find_top(root, "ClipLoggingInfo", "ObjectID", IDS["clip_logging_info_object_id"])
    media_source = find_top(root, "AudioMediaSource", "ObjectID", IDS["audio_media_source_object_id"])
    audio_sequence_source = find_top(
        root, "AudioSequenceSource", "ObjectID", IDS["audio_sequence_source_object_id"]
    )
    video_sequence_source = find_top(
        root, "VideoSequenceSource", "ObjectID", IDS["video_sequence_source_object_id"]
    )
    media = find_top(root, "Media", "ObjectUID", IDS["media_uid"])
    stream = find_top(root, "AudioStream", "ObjectID", IDS["audio_stream_object_id"])
    track_item = find_top(
        root, "AudioClipTrackItem", "ObjectID", IDS["audio_clip_track_item_object_id"]
    )
    chain = find_top(root, "AudioComponentChain", "ObjectID", IDS["timeline_component_chain_object_id"])
    subclip = find_top(root, "SubClip", "ObjectID", IDS["subclip_object_id"])
    timeline_clip = find_top(root, "AudioClip", "ObjectID", IDS["timeline_audio_clip_object_id"])

    for node, path, expected, label in (
        (sequence, "Name", TEST_NAME, "Sequence.Name"),
        (media_item, "./ProjectItem/Name", TEST_NAME, "media ProjectItem.Name"),
        (sequence_item, "./ProjectItem/Name", TEST_NAME, "sequence ProjectItem.Name"),
        (media_master, "Name", TEST_NAME, "media MasterClip.Name"),
        (sequence_master, "Name", TEST_NAME, "sequence MasterClip.Name"),
        (logging_info, "ClipName", TEST_NAME, "ClipLoggingInfo.ClipName"),
        (logging_info, "MediaOutPoint", duration, "ClipLoggingInfo.MediaOutPoint"),
        (media_source, "OriginalDuration", duration, "AudioMediaSource.OriginalDuration"),
        (audio_sequence_source, "OriginalDuration", duration, "AudioSequenceSource.OriginalDuration"),
        (video_sequence_source, "OriginalDuration", duration, "VideoSequenceSource.OriginalDuration"),
        (stream, "Duration", duration, "AudioStream.Duration"),
        (track_item, "./ClipTrackItem/TrackItem/End", duration, "TrackItem.End"),
        (subclip, "Name", TEST_NAME, "SubClip.Name"),
        (timeline_clip, "./Clip/OutPoint", duration, "AudioClip.OutPoint"),
        (timeline_clip, "Gain", "1.0", "AudioClip.Gain"),
    ):
        require(child_text(node, path) == expected, f"{label} inesperado")

    properties = sequence.find("./Node/Properties")
    require(properties is not None, "Sequence.Properties ausente")
    require(
        child_text(properties, "Monitor.ProgramZoomOut") == duration
        and child_text(properties, "MZ.WorkOutPoint") == duration,
        "duracao de trabalho/monitor da sequencia incorreta",
    )
    require(
        not any(child.tag.startswith("ExportSettings.Info.") for child in properties),
        "ExportSettings.Info obsoleto ainda alcancavel na sequencia",
    )

    absolute = str(media_path.resolve())
    relative_paths = [node.text for node in media.findall("RelativePath")]
    require(relative_paths == [MEDIA_RELATIVE_PATH, MEDIA_RELATIVE_PATH], "RelativePath final inesperado")
    require(child_text(media, "FilePath") == absolute, "Media.FilePath inesperado")
    require(child_text(media, "ActualMediaFilePath") == absolute, "Media.ActualMediaFilePath inesperado")
    require(child_text(media, "Title") == MEDIA_FILENAME, "Media.Title inesperado")

    raw_clip_refs = [node.get("ObjectRef") for node in media_master.findall("./Clips/Clip")]
    require(raw_clip_refs == [IDS["raw_master_audio_clip_object_id"]], "transcript antigo ainda anexado")
    find_top(root, "TranscriptClip", "ObjectID", IDS["stale_transcript_clip_object_id"])

    track_properties = track_item.find("./ClipTrackItem/TrackItem/Node/Properties")
    require(track_properties is not None and len(list(track_properties)) == 0, "TrackItem.Properties nao esta limpo")
    for tag in (
        "PreRenderComponentChainHashVersion",
        "PreRenderComponentChain",
        "PreRenderMix",
        "PreRenderSourceItem",
    ):
        require(track_item.find(tag) is None, f"{tag} ainda ligado ao TrackItem")

    component_refs = [node.get("ObjectRef") for node in chain.findall("./ComponentChain/Components/Component")]
    expected_refs = [
        IDS["intrinsic_volume_component_object_id"],
        IDS["intrinsic_channel_volume_component_object_id"],
    ]
    require(component_refs == expected_refs, f"cadeia final inesperada: {component_refs}")
    filter_names: List[str] = []
    for component_id in component_refs:
        component = find_top(root, "AudioFilterComponent", "ObjectID", component_id)
        filter_names.append(child_text(component, "FilterMatchName"))
        require(child_text(component, "./AudioComponent/Component/Intrinsic") == "true", "componente nao intrinseco")
    require(
        filter_names == ["Internal Volume Stereo", "Internal Channel Volume Stereo"],
        f"filtros finais inesperados: {filter_names}",
    )

    clip_properties = timeline_clip.find("./Clip/Node/Properties")
    require(clip_properties is not None, "AudioClip.Properties ausente")
    require(
        not any(child.tag.startswith("ESP.LoudnessSection.Analysis.") for child in clip_properties),
        "analise Auto-Match antiga ainda anexada",
    )
    require(len(root.findall("Sequence")) == 5, "quantidade de sequencias mudou")

    return {
        "sequence_count": len(root.findall("Sequence")),
        "sequence_name": child_text(sequence, "Name"),
        "media_file_path": child_text(media, "FilePath"),
        "media_relative_paths": relative_paths,
        "duration_ticks": int(duration),
        "track_item_properties_children": len(list(track_properties)),
        "track_item_prerender_links": 0,
        "timeline_gain": float(child_text(timeline_clip, "Gain")),
        "reachable_component_refs": component_refs,
        "reachable_filter_names": filter_names,
        "raw_master_clip_refs": raw_clip_refs,
        "stale_transcript_detached_but_preserved": True,
    }


def parse_args(native_root: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=native_root
        / "projetos_descartaveis"
        / "Teste_Preset_Nativo_CapCut_Feminino_PERENE_94_101_WORKING.prproj",
    )
    parser.add_argument(
        "--media", type=Path, default=native_root / "comparacao_ab" / MEDIA_FILENAME
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=native_root
        / "projetos_descartaveis"
        / "Teste_Preset_Nativo_CapCut_Feminino_TEST_94_MUTADO_OFFLINE.prproj",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=native_root
        / "analises"
        / "resultados"
        / "PERENE_MUTACAO_TEST_94"
        / "MUTACAO_TEST_94_MANIFEST.json",
    )
    return parser.parse_args()


def main() -> int:
    script_path = Path(__file__).resolve()
    native_root = script_path.parents[1]
    args = parse_args(native_root)

    expected_input = (
        native_root
        / "projetos_descartaveis"
        / "Teste_Preset_Nativo_CapCut_Feminino_PERENE_94_101_WORKING.prproj"
    )
    expected_media = native_root / "comparacao_ab" / MEDIA_FILENAME
    output_dir = native_root / "projetos_descartaveis"
    report_dir = native_root / "analises" / "resultados" / "PERENE_MUTACAO_TEST_94"

    require_exact_path(args.input, expected_input, "input")
    require_exact_path(args.media, expected_media, "media")
    require_direct_child(args.output, output_dir, "output")
    require_direct_child(args.report, report_dir, "report")
    require(args.output.suffix == ".prproj", "output deve terminar em .prproj")
    require(args.report.suffix == ".json", "report deve terminar em .json")
    require(args.output.resolve() != args.input.resolve(), "output nao pode sobrescrever input")
    require(args.input.is_file(), f"baseline ausente: {args.input}")
    require(args.media.is_file(), f"midia ausente: {args.media}")
    require(not args.output.exists(), f"recusa sobrescrever output existente: {args.output}")
    require(not args.report.exists(), f"recusa sobrescrever report existente: {args.report}")

    baseline_hash_before = sha256_file(args.input)
    media_hash = sha256_file(args.media)
    require(baseline_hash_before == BASELINE_SHA256, "hash do baseline divergiu do congelado")
    require(media_hash == MEDIA_SHA256, "hash da midia 94 divergiu do congelado")

    wav_info = read_wav_extensible_pcm(args.media)
    for field, expected in EXPECTED_WAV.items():
        require(wav_info.get(field) == expected, f"WAV {field}: {wav_info.get(field)!r} != {expected!r}")
    sample_rate = int(wav_info["sample_rate"])
    frames = int(wav_info["frames"])
    require(PREMIERE_TICKS_PER_SECOND % sample_rate == 0, "timebase nao e divisivel pela amostragem")
    ticks_per_sample = PREMIERE_TICKS_PER_SECOND // sample_rate
    duration_ticks = frames * ticks_per_sample
    duration = str(duration_ticks)
    require(duration == "6121785600000", f"duracao sample-exact inesperada: {duration}")

    compressed_before = args.input.read_bytes()
    require(sha256_bytes(compressed_before) == baseline_hash_before, "baseline mudou durante a leitura")
    try:
        xml_before_bytes = gzip.decompress(compressed_before)
    except (OSError, EOFError) as exc:
        raise GuardError(f"baseline nao e gzip integro: {exc}") from exc
    xml_before = xml_before_bytes.decode("utf-8")
    root_before = ET.fromstring(xml_before_bytes)
    require(root_before.tag == "PremiereData", "raiz XML do baseline nao e PremiereData")
    require(len(root_before.findall("Sequence")) == 5, "baseline nao tem as 5 sequencias esperadas")

    xml_after = xml_before
    block_digests: MutableMapping[str, Mapping[str, str]] = {}

    mutations: Tuple[Tuple[Tuple[str, str, str], Callable[[str], str]], ...] = (
        (TARGET_BLOCKS[0], mutate_media_project_item),
        (TARGET_BLOCKS[1], mutate_sequence_project_item),
        (TARGET_BLOCKS[2], mutate_media_master_clip),
        (TARGET_BLOCKS[3], mutate_sequence_master_clip),
        (TARGET_BLOCKS[4], lambda block: mutate_clip_logging_info(block, duration)),
        (TARGET_BLOCKS[5], lambda block: mutate_duration_only(block, duration, "AudioMediaSource.Duration")),
        (TARGET_BLOCKS[6], lambda block: mutate_duration_only(block, duration, "AudioSequenceSource.Duration")),
        (TARGET_BLOCKS[7], lambda block: mutate_duration_only(block, duration, "VideoSequenceSource.Duration")),
        (TARGET_BLOCKS[8], lambda block: mutate_media(block, args.media)),
        (TARGET_BLOCKS[9], lambda block: mutate_sequence(block, duration)),
        (TARGET_BLOCKS[10], lambda block: mutate_audio_stream(block, duration)),
        (TARGET_BLOCKS[11], lambda block: mutate_track_item(block, duration)),
        (TARGET_BLOCKS[12], mutate_component_chain),
        (TARGET_BLOCKS[13], mutate_subclip),
        (TARGET_BLOCKS[14], lambda block: mutate_timeline_audio_clip(block, duration)),
    )
    for identity, mutator in mutations:
        tag, attr, value = identity
        xml_after, before_block, after_block = mutate_block(xml_after, tag, attr, value, mutator)
        key = f"{tag}:{attr}={value}"
        block_digests[key] = {
            "before_sha256": sha256_bytes(before_block.encode("utf-8")),
            "after_sha256": sha256_bytes(after_block.encode("utf-8")),
        }

    xml_after_bytes = xml_after.encode("utf-8")
    root_after = ET.fromstring(xml_after_bytes)
    require(root_after.tag == "PremiereData", "raiz XML final nao e PremiereData")

    hashes_before = top_level_hashes(root_before)
    hashes_after = top_level_hashes(root_after)
    require(hashes_before.keys() == hashes_after.keys(), "conjunto de objetos top-level mudou")
    changed_keys = sorted(key for key in hashes_before if hashes_before[key] != hashes_after[key])
    expected_changed_keys = sorted(f"{tag}:{attr}={value}" for tag, attr, value in TARGET_BLOCKS)
    require(changed_keys == expected_changed_keys, f"objetos alterados fora da whitelist: {changed_keys}")
    semantic_validation = validate_semantics(root_after, duration, args.media)

    compressed_after = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after_bytes, "round-trip gzip falhou antes da escrita")
    args.output.write_bytes(compressed_after)

    output_bytes_readback = args.output.read_bytes()
    require(gzip.decompress(output_bytes_readback) == xml_after_bytes, "round-trip gzip falhou apos escrita")
    ET.fromstring(gzip.decompress(output_bytes_readback))
    baseline_hash_after = sha256_file(args.input)
    require(baseline_hash_after == baseline_hash_before, "baseline foi alterado durante a operacao")

    report: Dict[str, object] = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "operation": "offline_guarded_media_swap_76_RAW_CAL_to_TEST_94",
        "scope": "disposable_copy_only_no_gui",
        "inputs": {
            "baseline": str(args.input.resolve()),
            "baseline_sha256_before": baseline_hash_before,
            "baseline_sha256_after": baseline_hash_after,
            "baseline_unchanged": baseline_hash_before == baseline_hash_after,
            "media": str(args.media.resolve()),
            "media_sha256": media_hash,
        },
        "wav": {
            **wav_info,
            "ticks_per_sample": ticks_per_sample,
            "duration_seconds": frames / sample_rate,
            "duration_ticks": duration_ticks,
        },
        "output": {
            "project": str(args.output.resolve()),
            "project_sha256": sha256_bytes(output_bytes_readback),
            "compressed_bytes": len(output_bytes_readback),
            "xml_sha256": sha256_bytes(xml_after_bytes),
            "xml_bytes": len(xml_after_bytes),
            "gzip_integrity": True,
            "xml_integrity": True,
        },
        "target_ids_and_refs": IDS,
        "changed_top_level_objects": changed_keys,
        "changed_top_level_object_count": len(changed_keys),
        "all_non_target_top_level_objects_unchanged": True,
        "top_level_object_count_before": len(hashes_before),
        "top_level_object_count_after": len(hashes_after),
        "target_block_digests": block_digests,
        "semantic_validation": semantic_validation,
        "cleanup": {
            "reachable_effect_chain": ["Internal Volume Stereo", "Internal Channel Volume Stereo"],
            "disconnected_effect_object_refs_preserved_as_orphans": [
                IDS["enhance_speech_component_object_id"],
                IDS["essential_sound_limiter_component_object_id"],
                IDS["parametric_eq_component_object_id"],
                IDS["final_limiter_component_object_id"],
            ],
            "auto_match_gain_reset_to": 1.0,
            "old_loudness_analysis_removed": True,
            "enhance_and_prerender_links_removed": True,
            "old_transcript_detached_but_object_preserved": True,
            "export_settings_info_removed_from_target_sequence": True,
        },
        "limitations": [
            "A integridade estrutural gzip/XML e o isolamento por IDs foram validados offline.",
            "A abertura, reconformacao e reproducao no Premiere precisam ser validadas pela GUI em etapa separada.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    require(json.loads(args.report.read_text(encoding="utf-8"))["output"]["gzip_integrity"], "report invalido")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr)
        raise SystemExit(2)
