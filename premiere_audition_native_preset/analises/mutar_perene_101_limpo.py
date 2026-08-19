#!/usr/bin/env python3
"""Cria uma copia cirurgica do PERENE com 76_RAW_CAL substituida por 101.

O baseline e a midia de entrada sao somente leitura. A ferramenta descobre o
grafo da sequencia pelo XML (nao por ObjectIDs fixos), altera apenas blocos
top-level explicitamente permitidos e recusa sobrescrever qualquer saida.
"""

from __future__ import annotations

import argparse
import collections
import copy
import gzip
import hashlib
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Counter, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple


BASELINE_SHA256 = "9985c1397318f3fb62a370ad566a04c1bc8b9849954209790eb9f625f33ae895"
MEDIA_SHA256 = "b5395f0408ea88d3bbd2f013761a86019bd1133a6627f3e4ce9d48d7aadbcfae"
OLD_SEQUENCE_NAME = "76_RAW_CAL"
NEW_SEQUENCE_NAME = "101_RAW_DJI_NEW_CLEAN"
OLD_MEDIA_NAME = "76_RAW_CAL.wav"
NEW_MEDIA_NAME = "101_RAW_DJI_NEW.wav"
OLD_DURATION_TICKS = 6_000_429_456_000
PREMIERE_TICKS_PER_SECOND = 254_016_000_000

EXPECTED_WAV: Mapping[str, int | str] = {
    "format": "PCM",
    "channels": 2,
    "sample_rate": 48_000,
    "bits_per_sample": 32,
    "block_align": 8,
    "frames": 1_289_600,
}

CLEAN_DIALOG_PROPERTIES: Mapping[str, str] = {
    "MZ.AudioCategorization.AudioCategorizationAdjustmentMode": "dialog",
    "ESP.AdjustmentsModeID": "dialog",
    "ESP.ClipMixTypeID": "1",
    "ESP.Tag": "dialog",
    "ESP.PresetGuid": "bb717b4a-c0b1-4def-9040-3fb8e83b8a91",
    "MZ.EssentialSound.AdjustmentModeWorkflowType": "2",
    "ESP.RestorationSection.9": "60",
    "ESP.RestorationSection.4": "7.14300060272",
    "ESP.ClaritySection.2": "3",
    "ESP.SoundEffectsSection.3": "2",
}


class GuardError(RuntimeError):
    """Uma pre-condicao de integridade ou isolamento nao foi satisfeita."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pcm_wav(path: Path) -> Dict[str, int | str]:
    """Le RIFF/WAVE PCM comum ou extensivel sem depender de ffmpeg."""
    with path.open("rb") as handle:
        header = handle.read(12)
        require(len(header) == 12, "WAV truncado antes do cabecalho")
        riff, _size, wave = struct.unpack("<4sI4s", header)
        require(riff == b"RIFF" and wave == b"WAVE", "midia nao e RIFF/WAVE")
        fmt_data = None
        data_size = None
        while True:
            chunk_header = handle.read(8)
            if not chunk_header:
                break
            require(len(chunk_header) == 8, "WAV truncado no cabecalho de chunk")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            chunk = handle.read(chunk_size)
            require(len(chunk) == chunk_size, f"WAV truncado no chunk {chunk_id!r}")
            if chunk_size & 1:
                require(len(handle.read(1)) == 1, "WAV truncado no padding")
            if chunk_id == b"fmt ":
                fmt_data = chunk
            elif chunk_id == b"data":
                data_size = chunk_size

    require(fmt_data is not None and len(fmt_data) >= 16, "chunk fmt ausente")
    require(data_size is not None, "chunk data ausente")
    tag, channels, sample_rate, byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", fmt_data[:16]
    )
    effective_tag = tag
    format_name = "PCM"
    if tag == 0xFFFE:
        require(len(fmt_data) >= 40, "WAVE_FORMAT_EXTENSIBLE incompleto")
        cb_size, valid_bits, _mask = struct.unpack("<HHI", fmt_data[16:24])
        require(cb_size >= 22 and valid_bits <= bits, "extensao WAV invalida")
        effective_tag = struct.unpack("<I", fmt_data[24:28])[0]
        format_name = "WAVE_FORMAT_EXTENSIBLE_PCM"
    require(effective_tag == 1, f"formato WAV nao PCM: {effective_tag}")
    require(block_align > 0 and data_size % block_align == 0, "block_align invalido")
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


def object_identity(node: ET.Element) -> Tuple[str, str, str]:
    for attr in ("ObjectUID", "ObjectID"):
        value = node.get(attr)
        if value is not None:
            return node.tag, attr, value
    raise GuardError(f"objeto top-level sem identidade: {node.tag}")


def identity_label(node: ET.Element) -> str:
    tag, attr, value = object_identity(node)
    return f"{tag}:{attr}={value}"


def describe_node(node: ET.Element) -> str:
    """Rotulo tambem utilizavel para elementos XML aninhados sem ObjectID."""
    if node.get("ObjectUID") is not None or node.get("ObjectID") is not None:
        return identity_label(node)
    return node.tag


def build_object_map(root: ET.Element) -> Dict[str, ET.Element]:
    result: Dict[str, ET.Element] = {}
    for node in root:
        for attr in ("ObjectUID", "ObjectID"):
            value = node.get(attr)
            if value is None:
                continue
            require(value not in result, f"identidade top-level duplicada: {value}")
            result[value] = node
    return result


def referenced_ids(node: ET.Element, objects: Mapping[str, ET.Element]) -> Iterable[str]:
    for child in node.iter():
        for attr in ("ObjectRef", "ObjectURef"):
            value = child.get(attr)
            if value in objects:
                yield value


def closure(start: ET.Element, objects: Mapping[str, ET.Element]) -> set[str]:
    _tag, _attr, start_id = object_identity(start)
    seen: set[str] = set()
    pending = [start_id]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(referenced_ids(objects[current], objects))
    return seen


def unique(nodes: Iterable[ET.Element], label: str) -> ET.Element:
    result = list(nodes)
    require(len(result) == 1, f"{label}: esperado 1, encontrado {len(result)}")
    return result[0]


def required_child(node: ET.Element, path: str) -> ET.Element:
    child = node.find(path)
    require(child is not None, f"elemento ausente: {describe_node(node)}/{path}")
    return child


def required_text(node: ET.Element, path: str) -> str:
    child = required_child(node, path)
    require(child.text is not None, f"texto ausente: {describe_node(node)}/{path}")
    return child.text


def require_ref(node: ET.Element, path: str, attr: str, objects: Mapping[str, ET.Element]) -> ET.Element:
    child = required_child(node, path)
    value = child.get(attr)
    require(value in objects, f"referencia ausente: {identity_label(node)}/{path}@{attr}")
    return objects[value]


def discover_graph(
    root: ET.Element, sequence_name: str = OLD_SEQUENCE_NAME
) -> Dict[str, ET.Element]:
    objects = build_object_map(root)
    sequence = unique(
        (node for node in root.findall("Sequence") if node.findtext("Name") == sequence_name),
        f"Sequence Name={sequence_name}",
    )
    if sequence_name == OLD_SEQUENCE_NAME:
        require(
            not any(node.findtext("Name") == NEW_SEQUENCE_NAME for node in root.findall("Sequence")),
            f"sequencia {NEW_SEQUENCE_NAME} ja existe",
        )
    target_closure = closure(sequence, objects)
    track_item = unique(
        (objects[key] for key in target_closure if objects[key].tag == "AudioClipTrackItem"),
        "AudioClipTrackItem alcancavel pela sequencia",
    )
    chain = require_ref(
        track_item, "./ClipTrackItem/ComponentOwner/Components", "ObjectRef", objects
    )
    subclip = require_ref(track_item, "./ClipTrackItem/SubClip", "ObjectRef", objects)
    timeline_clip = require_ref(subclip, "Clip", "ObjectRef", objects)
    source_master = require_ref(subclip, "MasterClip", "ObjectURef", objects)
    logging_info = require_ref(source_master, "LoggingInfo", "ObjectRef", objects)
    media_source = require_ref(timeline_clip, "./Clip/Source", "ObjectRef", objects)
    media = require_ref(media_source, "./MediaSource/Media", "ObjectURef", objects)
    audio_stream = require_ref(media, "AudioStream", "ObjectRef", objects)

    source_project_item = unique(
        (
            node
            for node in root.findall("ClipProjectItem")
            if (ref := node.find("MasterClip")) is not None
            and ref.get("ObjectURef") == source_master.get("ObjectUID")
        ),
        "ClipProjectItem da midia",
    )
    sequence_uid = sequence.get("ObjectUID")
    audio_sequence_source = unique(
        (
            node
            for node in root.findall("AudioSequenceSource")
            if (ref := node.find("./SequenceSource/Sequence")) is not None
            and ref.get("ObjectURef") == sequence_uid
        ),
        "AudioSequenceSource da sequencia",
    )
    video_sequence_source = unique(
        (
            node
            for node in root.findall("VideoSequenceSource")
            if (ref := node.find("./SequenceSource/Sequence")) is not None
            and ref.get("ObjectURef") == sequence_uid
        ),
        "VideoSequenceSource da sequencia",
    )
    sequence_audio_clip = unique(
        (
            node
            for node in root.findall("AudioClip")
            if (ref := node.find("./Clip/Source")) is not None
            and ref.get("ObjectRef") == audio_sequence_source.get("ObjectID")
        ),
        "AudioClip mestre da sequencia",
    )
    sequence_master = unique(
        (
            node
            for node in root.findall("MasterClip")
            if any(
                ref.get("ObjectRef") == sequence_audio_clip.get("ObjectID")
                for ref in node.findall("./Clips/Clip")
            )
        ),
        "MasterClip da sequencia",
    )
    sequence_project_item = unique(
        (
            node
            for node in root.findall("ClipProjectItem")
            if (ref := node.find("MasterClip")) is not None
            and ref.get("ObjectURef") == sequence_master.get("ObjectUID")
        ),
        "ClipProjectItem da sequencia",
    )

    critical_ids = {
        object_identity(node)[2]
        for node in (
            track_item,
            chain,
            subclip,
            timeline_clip,
            source_master,
            logging_info,
            media_source,
            media,
            audio_stream,
        )
    }
    for other in root.findall("Sequence"):
        if other is sequence:
            continue
        overlap = critical_ids & closure(other, objects)
        require(
            not overlap,
            f"a sequencia {other.findtext('Name')} compartilha objetos criticos: {sorted(overlap)}",
        )

    return {
        "sequence": sequence,
        "track_item": track_item,
        "chain": chain,
        "subclip": subclip,
        "timeline_clip": timeline_clip,
        "source_master": source_master,
        "logging_info": logging_info,
        "media_source": media_source,
        "media": media,
        "audio_stream": audio_stream,
        "source_project_item": source_project_item,
        "audio_sequence_source": audio_sequence_source,
        "video_sequence_source": video_sequence_source,
        "sequence_master": sequence_master,
        "sequence_project_item": sequence_project_item,
    }


def set_text(node: ET.Element, path: str, old: str, new: str, label: str) -> None:
    child = required_child(node, path)
    require(child.text == old, f"{label}: valor antigo inesperado {child.text!r}")
    child.text = new


def mutate_graph(graph: Mapping[str, ET.Element], media_path: Path, duration: int) -> Dict[str, object]:
    duration_text = str(duration)
    media_absolute = str(media_path.resolve())
    media_relative = f"../comparacao_ab/{NEW_MEDIA_NAME}"

    set_text(graph["sequence"], "Name", OLD_SEQUENCE_NAME, NEW_SEQUENCE_NAME, "Sequence.Name")
    set_text(
        graph["sequence_project_item"],
        "./ProjectItem/Name",
        OLD_SEQUENCE_NAME,
        NEW_SEQUENCE_NAME,
        "sequence ProjectItem.Name",
    )
    set_text(
        graph["sequence_master"],
        "Name",
        OLD_SEQUENCE_NAME,
        NEW_SEQUENCE_NAME,
        "sequence MasterClip.Name",
    )
    for key, path in (
        ("source_project_item", "./ProjectItem/Name"),
        ("source_master", "Name"),
        ("logging_info", "ClipName"),
        ("subclip", "Name"),
    ):
        set_text(graph[key], path, OLD_MEDIA_NAME, NEW_MEDIA_NAME, f"{key}.{path}")

    for key, path in (
        ("logging_info", "MediaOutPoint"),
        ("media_source", "OriginalDuration"),
        ("audio_stream", "Duration"),
        ("audio_sequence_source", "OriginalDuration"),
        ("video_sequence_source", "OriginalDuration"),
        ("track_item", "./ClipTrackItem/TrackItem/End"),
        ("timeline_clip", "./Clip/OutPoint"),
    ):
        set_text(graph[key], path, str(OLD_DURATION_TICKS), duration_text, f"{key}.{path}")

    media = graph["media"]
    relative_nodes = media.findall("RelativePath")
    require(len(relative_nodes) == 2, "Media deve ter dois RelativePath")
    old_relatives = [node.text for node in relative_nodes]
    require(
        old_relatives
        == [
            "../exports_validacao/76_RAW_CAL.wav",
            "../../Users/lucas/Documents/Codex/voz-neural/premiere_audition_native_preset/exports_validacao/76_RAW_CAL.wav",
        ],
        f"RelativePath antigo inesperado: {old_relatives}",
    )
    for node in relative_nodes:
        node.text = media_relative
    for path in ("FilePath", "ActualMediaFilePath"):
        child = required_child(media, path)
        require(child.text is not None and child.text.endswith("/exports_validacao/76_RAW_CAL.wav"), f"{path} antigo inesperado")
        child.text = media_absolute
    set_text(media, "Title", OLD_MEDIA_NAME, NEW_MEDIA_NAME, "Media.Title")

    stream = graph["audio_stream"]
    for path in ("ConformedAudioPath", "PeakFilePath"):
        child = required_child(stream, path)
        require(child.text is not None and child.text.endswith("/76_RAW_CAL.wav 48000.pek"), f"{path} antigo inesperado")
        child.text = str(Path(child.text).with_name(f"{NEW_MEDIA_NAME} 48000.pek"))

    sequence_properties = required_child(graph["sequence"], "./Node/Properties")
    for child in sequence_properties:
        if child.text == str(OLD_DURATION_TICKS):
            child.text = duration_text
        if not child.tag.startswith("ExportSettings.Info."):
            continue
        require(child.text is not None, f"{child.tag} vazio")
        settings = json.loads(child.text)
        for field in ("CustomOut", "OutPoint", "WorkOut"):
            if settings.get(field) == OLD_DURATION_TICKS:
                settings[field] = duration
        output_path = settings.get("OutPath")
        if child.tag == "ExportSettings.Info.SaveAsFile":
            settings["OutPath"] = str(
                media_path.parents[1] / "exports_validacao" / "101_XML_CLEAN.wav"
            )
        elif isinstance(output_path, str):
            settings["OutPath"] = output_path.replace(OLD_SEQUENCE_NAME, NEW_SEQUENCE_NAME)
        child.text = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
    require(
        required_text(sequence_properties, "Monitor.ProgramZoomOut") == duration_text
        and required_text(sequence_properties, "MZ.WorkOutPoint") == duration_text,
        "duracao da area de trabalho nao foi atualizada",
    )

    track_item = graph["track_item"]
    track_properties = required_child(track_item, "./ClipTrackItem/TrackItem/Node/Properties")
    removed_property_names: List[str] = []
    for child in list(track_properties):
        if child.tag == "EnhanceSpeechCompleted" or child.tag.startswith(
            ("ESP.EnhanceSpeechSection.", "ESP.LoudnessSection.", "ESP.VolumeSection.")
        ):
            removed_property_names.append(child.tag)
            track_properties.remove(child)
    remaining_properties = {child.tag: child.text or "" for child in track_properties}
    require(remaining_properties == CLEAN_DIALOG_PROPERTIES, f"propriedades limpas divergiram: {remaining_properties}")
    removed_prerender: List[str] = []
    for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem"):
        child = track_item.find(tag)
        require(child is not None, f"{tag} ausente antes da limpeza")
        removed_prerender.append(tag)
        track_item.remove(child)
    require(required_text(track_item, "PreRenderComponentChainHashVersion") == "1", "hash version inesperado")

    chain = graph["chain"]
    component_chain = required_child(chain, "ComponentChain")
    components = required_child(component_chain, "Components")
    removed_components = [child.get("ObjectRef") for child in components.findall("Component")]
    require(len(removed_components) == 6 and all(removed_components), f"cadeia historica inesperada: {removed_components}")
    component_chain.remove(components)
    defaults = (
        ("DefaultVol", "true"),
        ("DefaultVolumeComponentID", "1"),
        ("DefaultChannelVolumeComponentID", "2"),
    )
    for tag, text in reversed(defaults):
        require(chain.find(tag) is None, f"{tag} ja existia")
        child = ET.Element(tag)
        child.text = text
        chain.insert(0, child)

    clip = graph["timeline_clip"]
    clip_properties = required_child(clip, "./Clip/Node/Properties")
    removed_loudness = []
    for child in list(clip_properties):
        if child.tag.startswith("ESP.LoudnessSection.Analysis."):
            removed_loudness.append(child.tag)
            clip_properties.remove(child)
    require(len(removed_loudness) == 4, f"analise loudness inesperada: {removed_loudness}")
    gain = clip.find("Gain")
    require(gain is not None and gain.text == "1.2507641315460205", "ganho Auto-Match historico inesperado")
    clip.remove(gain)

    return {
        "removed_track_properties": removed_property_names,
        "remaining_track_properties": remaining_properties,
        "removed_prerender_links": removed_prerender,
        "removed_component_refs": removed_components,
        "removed_loudness_analysis": removed_loudness,
        "automatch_gain_removed": True,
        "default_volume_chain": {tag: text for tag, text in defaults},
        "media_relative_path": media_relative,
    }


def top_level_hashes(root: ET.Element) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for node in root:
        if node.get("ObjectID") is None and node.get("ObjectUID") is None:
            continue
        label = identity_label(node)
        require(label not in hashes, f"identidade duplicada: {label}")
        hashes[label] = sha256_bytes(ET.tostring(node, encoding="utf-8"))
    return hashes


def ref_multiset(root: ET.Element) -> Counter[Tuple[str, str, str]]:
    refs: Counter[Tuple[str, str, str]] = collections.Counter()
    for node in root:
        if node.get("ObjectID") is None and node.get("ObjectUID") is None:
            continue
        owner = identity_label(node)
        for child in node.iter():
            for attr in ("ObjectRef", "ObjectURef"):
                value = child.get(attr)
                if value is not None:
                    refs[(owner, attr, value)] += 1
    return refs


def block_pattern(node: ET.Element) -> re.Pattern[str]:
    tag, attr, value = object_identity(node)
    return re.compile(
        rf'(?ms)^\t<{re.escape(tag)}\b(?=[^\n]*\b{re.escape(attr)}="{re.escape(value)}")[^\n]*\n'
        rf'.*?^\t</{re.escape(tag)}>(?:\n|$)'
    )


def serialize_top_level(node: ET.Element) -> str:
    clone = copy.deepcopy(node)
    clone.tail = None
    ET.indent(clone, space="\t", level=1)
    return "\t" + ET.tostring(clone, encoding="unicode", short_empty_elements=True) + "\n"


def surgical_replace(
    xml: str, nodes: Sequence[ET.Element]
) -> Tuple[str, MutableMapping[str, Mapping[str, str]]]:
    digests: MutableMapping[str, Mapping[str, str]] = {}
    result = xml
    for node in nodes:
        pattern = block_pattern(node)
        matches = list(pattern.finditer(result))
        require(len(matches) == 1, f"bloco raw {identity_label(node)} nao e unico")
        match = matches[0]
        before = match.group(0)
        after = serialize_top_level(node)
        require(before != after, f"bloco {identity_label(node)} nao mudou")
        result = result[: match.start()] + after + result[match.end() :]
        digests[identity_label(node)] = {
            "before_sha256": sha256_bytes(before.encode("utf-8")),
            "after_sha256": sha256_bytes(after.encode("utf-8")),
        }
    return result, digests


def validate_output(
    root_before: ET.Element,
    root_after: ET.Element,
    changed_nodes: Sequence[ET.Element],
    graph_after: Mapping[str, ET.Element],
    duration: int,
) -> Dict[str, object]:
    changed_labels = sorted(identity_label(node) for node in changed_nodes)
    hashes_before = top_level_hashes(root_before)
    hashes_after = top_level_hashes(root_after)
    require(hashes_before.keys() == hashes_after.keys(), "conjunto de objetos top-level mudou")
    actual_changed = sorted(
        key for key in hashes_before if hashes_before[key] != hashes_after[key]
    )
    require(actual_changed == changed_labels, f"objetos alterados fora da whitelist: {actual_changed}")

    refs_before = ref_multiset(root_before)
    refs_after = ref_multiset(root_after)
    added_refs = refs_after - refs_before
    removed_refs = refs_before - refs_after
    require(not added_refs, f"referencias inesperadas adicionadas: {added_refs}")
    expected_removed_count = 9
    require(sum(removed_refs.values()) == expected_removed_count, f"refs removidas: {removed_refs}")
    allowed_owners = {
        identity_label(graph_after["track_item"]),
        identity_label(graph_after["chain"]),
    }
    require(
        all(owner in allowed_owners and attr == "ObjectRef" for owner, attr, _value in removed_refs),
        f"referencias removidas fora da limpeza: {removed_refs}",
    )

    sequences_before = {node.get("ObjectUID"): node for node in root_before.findall("Sequence")}
    sequences_after = {node.get("ObjectUID"): node for node in root_after.findall("Sequence")}
    require(sequences_before.keys() == sequences_after.keys() and len(sequences_after) == 5, "sequencias mudaram")
    target_uid = graph_after["sequence"].get("ObjectUID")
    for uid in sequences_before:
        if uid == target_uid:
            continue
        require(
            ET.tostring(sequences_before[uid]) == ET.tostring(sequences_after[uid]),
            f"sequencia nao alvo mudou: {sequences_before[uid].findtext('Name')}",
        )

    require(graph_after["sequence"].findtext("Name") == NEW_SEQUENCE_NAME, "nome final incorreto")
    require(
        required_text(graph_after["timeline_clip"], "./Clip/OutPoint") == str(duration)
        and required_text(graph_after["track_item"], "./ClipTrackItem/TrackItem/End") == str(duration),
        "duracao final da timeline incorreta",
    )
    require(graph_after["timeline_clip"].find("Gain") is None, "Gain ainda presente")
    require(
        graph_after["chain"].find("./ComponentChain/Components") is None,
        "Components ainda ligado a cadeia limpa",
    )
    require(
        not any(
            graph_after["track_item"].find(tag) is not None
            for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem")
        ),
        "cache/Enhance ainda ligado ao TrackItem",
    )
    return {
        "changed_top_level_objects": changed_labels,
        "changed_top_level_object_count": len(changed_labels),
        "all_other_top_level_objects_unchanged": True,
        "all_other_sequences_unchanged": True,
        "removed_reference_occurrences": sum(removed_refs.values()),
        "added_reference_occurrences": 0,
        "sequence_count": len(sequences_after),
        "target_sequence_uid": target_uid,
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
        "--media", type=Path, default=native_root / "comparacao_ab" / NEW_MEDIA_NAME
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=native_root
        / "projetos_descartaveis"
        / "Teste_Preset_Nativo_CapCut_Feminino_PERENE_101_XML_CLEAN_CANDIDATO.prproj",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=native_root
        / "analises"
        / "resultados"
        / "PERENE_MUTACAO_101"
        / "MUTACAO_101_MANIFEST.json",
    )
    return parser.parse_args()


def main() -> int:
    native_root = Path(__file__).resolve().parents[1]
    args = parse_args(native_root)
    expected_input = (
        native_root
        / "projetos_descartaveis"
        / "Teste_Preset_Nativo_CapCut_Feminino_PERENE_94_101_WORKING.prproj"
    )
    expected_media = native_root / "comparacao_ab" / NEW_MEDIA_NAME
    allowed_output_dir = native_root / "projetos_descartaveis"
    allowed_report_dir = native_root / "analises" / "resultados" / "PERENE_MUTACAO_101"

    require(args.input.resolve() == expected_input.resolve(), "input fora do baseline permitido")
    require(args.media.resolve() == expected_media.resolve(), "midia fora da copia permitida")
    require(args.output.resolve().parent == allowed_output_dir.resolve(), "output fora da pasta descartavel")
    require(args.report.resolve().parent == allowed_report_dir.resolve(), "report fora da pasta permitida")
    require(args.output.suffix == ".prproj" and args.report.suffix == ".json", "extensao de saida invalida")
    require(args.output.resolve() != args.input.resolve(), "output nao pode sobrescrever o baseline")
    require(args.input.is_file() and args.media.is_file(), "input ou midia ausente")
    require(not args.output.exists() and not args.report.exists(), "recusa sobrescrever saida existente")

    baseline_hash_before = sha256_file(args.input)
    media_hash = sha256_file(args.media)
    require(baseline_hash_before == BASELINE_SHA256, "hash do baseline divergiu do congelado")
    require(media_hash == MEDIA_SHA256, "hash do WAV 101 divergiu do congelado")
    wav = read_pcm_wav(args.media)
    for field, expected in EXPECTED_WAV.items():
        require(wav[field] == expected, f"WAV {field}: {wav[field]!r} != {expected!r}")
    sample_rate = int(wav["sample_rate"])
    frames = int(wav["frames"])
    require(PREMIERE_TICKS_PER_SECOND % sample_rate == 0, "timebase nao divisivel por sample rate")
    ticks_per_sample = PREMIERE_TICKS_PER_SECOND // sample_rate
    duration_ticks = frames * ticks_per_sample
    require(duration_ticks == 6_824_563_200_000, "duracao sample-exact inesperada")

    compressed_before = args.input.read_bytes()
    require(sha256_bytes(compressed_before) == baseline_hash_before, "baseline mudou durante leitura")
    try:
        xml_before_bytes = gzip.decompress(compressed_before)
    except (OSError, EOFError) as exc:
        raise GuardError(f"gzip do baseline invalido: {exc}") from exc
    root_before = ET.fromstring(xml_before_bytes)
    require(root_before.tag == "PremiereData", "raiz XML inesperada")
    graph_before = discover_graph(root_before)
    root_work = copy.deepcopy(root_before)
    graph_work = discover_graph(root_work)
    mutation = mutate_graph(graph_work, args.media, duration_ticks)

    changed_nodes = [
        graph_work[key]
        for key in (
            "source_project_item",
            "sequence_project_item",
            "source_master",
            "sequence_master",
            "logging_info",
            "media_source",
            "audio_sequence_source",
            "video_sequence_source",
            "media",
            "sequence",
            "audio_stream",
            "track_item",
            "chain",
            "subclip",
            "timeline_clip",
        )
    ]
    require(len({identity_label(node) for node in changed_nodes}) == len(changed_nodes), "whitelist duplicada")
    xml_before = xml_before_bytes.decode("utf-8")
    xml_after, block_digests = surgical_replace(xml_before, changed_nodes)
    xml_after_bytes = xml_after.encode("utf-8")
    root_after = ET.fromstring(xml_after_bytes)
    graph_after = discover_graph(root_after, NEW_SEQUENCE_NAME)
    validation = validate_output(root_before, root_after, changed_nodes, graph_after, duration_ticks)

    compressed_after = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after_bytes, "round-trip gzip falhou")
    require(sha256_file(args.input) == baseline_hash_before, "baseline mudou antes da escrita")
    args.output.write_bytes(compressed_after)
    output_bytes = args.output.read_bytes()
    require(gzip.decompress(output_bytes) == xml_after_bytes, "readback gzip falhou")
    ET.fromstring(gzip.decompress(output_bytes))
    baseline_hash_after = sha256_file(args.input)
    require(baseline_hash_after == baseline_hash_before, "baseline foi alterado")

    report: Dict[str, object] = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "operation": "offline_guarded_swap_76_RAW_CAL_to_101_RAW_DJI_NEW_clean",
        "scope": "new_disposable_copy_only_no_gui_no_personal_writes",
        "inputs": {
            "baseline": str(args.input.resolve()),
            "baseline_sha256_before": baseline_hash_before,
            "baseline_sha256_after": baseline_hash_after,
            "baseline_unchanged": True,
            "media": str(args.media.resolve()),
            "media_sha256": media_hash,
        },
        "wav": {
            **wav,
            "ticks_per_sample": ticks_per_sample,
            "duration_seconds": frames / sample_rate,
            "duration_ticks": duration_ticks,
        },
        "output": {
            "project": str(args.output.resolve()),
            "project_sha256": sha256_bytes(output_bytes),
            "compressed_bytes": len(output_bytes),
            "xml_sha256": sha256_bytes(xml_after_bytes),
            "xml_bytes": len(xml_after_bytes),
            "gzip_integrity": True,
            "xml_integrity": True,
        },
        "resolved_graph": {key: identity_label(node) for key, node in graph_before.items()},
        "mutation": mutation,
        "validation": validation,
        "target_block_digests": block_digests,
        "limitations": [
            "Validacao offline cobre hash, WAV, gzip, XML, referencias e isolamento das outras sequencias.",
            "O Premiere ainda precisa validar abertura, reconformacao, reproducao e export real desta copia.",
            "ModificationState, FileKey e transcript foram preservados; o novo caminho de peak foi invalidado pelo nome para forcar reconformacao visual.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=False)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    require(json.loads(args.report.read_text(encoding="utf-8"))["output"]["gzip_integrity"], "report invalido")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr)
        raise SystemExit(2)
