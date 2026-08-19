#!/usr/bin/env python3
"""Cria uma copia CLEAN fail-closed dos 20 clipes, sem abrir o Premiere.

O checkpoint e somente leitura. A ferramenta altera exclusivamente os 20
TrackItems, suas 20 cadeias e seus 20 AudioClips da sequencia auditada. Ela
desanexa Enhance/pre-render, Auto-Match, shelf e limiter, mantendo apenas os
dois componentes intrinsecos neutros e a categorizacao Dialogue generica.
Objetos historicos ficam orfaos de proposito: remove-los fisicamente seria uma
mutacao mais ampla e desnecessaria; o Premiere pode coleta-los no round-trip.
"""

from __future__ import annotations

import collections
import copy
import gzip
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Counter, Iterable, Mapping, MutableMapping, Sequence


WORKSPACE = Path(__file__).resolve().parents[2]
NATIVE = WORKSPACE / "premiere_audition_native_preset"
SUITE = NATIVE / "comparacao_ab" / "FLUXO_APROVADO_20_20260814"
TRIAL = NATIVE / "comparacao_ab" / "SEM_ENHANCE_ESSENTIAL_20_20260814"
PROJECTS = NATIVE / "projetos_descartaveis"

INPUT = TRIAL / "checkpoints" / "Teste_SEM_ENHANCE_ESSENTIAL_20_BASELINE_FROM_E100.prproj"
OUTPUT = PROJECTS / "Teste_SEM_ENHANCE_ESSENTIAL_20_CLEAN_CANDIDATO.prproj"
REPORT = TRIAL / "analises" / "MUTACAO_CLEAN_20_MANIFEST.json"
SOURCE_MANIFEST = SUITE / "manifest_preflight.json"
SOURCE_AUDIT = SUITE / "auditoria_final.json"
GUI_REFERENCE = PROJECTS / "Teste_107_ADAPTIVE_NR_LIGHT_WORKING.prproj"

EXPECTED_INPUT_SHA256 = "04bae08966ffb04a1513e2baff6308efc0b43d4f22735e5acb071a90e965826f"
EXPECTED_MANIFEST_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"
EXPECTED_GUI_REFERENCE_SHA256 = "f77337dee90059541d73fb7387bd96996479465bda1697f06e35da8192579d89"
SEQUENCE_NAME = "FLUXO_APROVADO_20"
EXPECTED_IDS = [64, 65, 66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 81, 82, 84, 92, 94, 95, 101, 107]
EXCLUDED_IDS = {21, 38, 39, 51, 70, 77, 83}
EXPECTED_TOTAL_FRAMES = 25_181_516
PREMIERE_TICKS_PER_SECOND = 254_016_000_000
SAMPLE_RATE = 48_000
TICKS_PER_SAMPLE = PREMIERE_TICKS_PER_SECOND // SAMPLE_RATE

ENHANCE_MATCH = "7f446972-835b-422e-a8a3-d64028839ba2"
VOLUME_MATCH = "Internal Volume Stereo"
CHANNEL_VOLUME_MATCH = "Internal Channel Volume Stereo"
SHELF_MATCH = "8edcd326-33de-4a4d-985b-f1bf48ed1427"
LIMITER_MATCH = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"

CLEAN_DIALOG_PROPERTIES: Mapping[str, str] = {
    "MZ.AudioCategorization.AudioCategorizationAdjustmentMode": "dialog",
    "ESP.AdjustmentsModeID": "dialog",
    "ESP.ClipMixTypeID": "1",
    "ESP.Tag": "dialog",
    "ESP.PresetGuid": "bb717b4a-c0b1-4def-9040-3fb8e83b8a91",
    "MZ.EssentialSound.AdjustmentModeWorkflowType": "2",
    "ESP.SoundEffectsSection.3": "2",
    "ESP.ClaritySection.2": "3",
    "ESP.RestorationSection.4": "7.14300060272",
    "ESP.RestorationSection.9": "60",
}

REMOVED_TRACK_PROPERTY_NAMES = {
    "EnhanceSpeechCompleted",
    "ESP.EnhanceSpeechSection.1",
    "ESP.EnhanceSpeechSection.2",
    "ESP.EnhanceSpeechSection.3",
    "ESP.EnhanceSpeechSection.EnhanceSpeechEnabled.ComponentID",
    "ESP.LoudnessSection.1",
}

LOUDNESS_ANALYSIS_NAMES = {
    "ESP.LoudnessSection.Analysis.TimeOut",
    "ESP.LoudnessSection.Analysis.TimeIn",
    "ESP.LoudnessSection.Analysis.ContentState",
    "ESP.LoudnessSection.Analysis.ITU1770LoudnessValue",
}


class GuardError(RuntimeError):
    """Uma pre-condicao de isolamento/integridade falhou."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(node: ET.Element) -> tuple[str, str, str]:
    for attr in ("ObjectUID", "ObjectID"):
        value = node.get(attr)
        if value is not None:
            return node.tag, attr, value
    raise GuardError(f"objeto top-level sem identidade: {node.tag}")


def label(node: ET.Element) -> str:
    tag, attr, value = identity(node)
    return f"{tag}:{attr}={value}"


def object_map(root: ET.Element) -> dict[str, ET.Element]:
    result: dict[str, ET.Element] = {}
    for node in root:
        for attr in ("ObjectUID", "ObjectID"):
            value = node.get(attr)
            if value is None:
                continue
            require(value not in result or result[value] is node, f"identidade duplicada: {value}")
            result[value] = node
    return result


def resolve(objects: Mapping[str, ET.Element], value: str | None, context: str) -> ET.Element:
    require(value is not None and value in objects, f"{context}: referencia ausente {value!r}")
    return objects[value]


def refs_in(node: ET.Element, objects: Mapping[str, ET.Element]) -> Iterable[str]:
    for child in node.iter():
        for attr in ("ObjectRef", "ObjectURef"):
            value = child.get(attr)
            if value in objects:
                yield value


def closure(node: ET.Element, objects: Mapping[str, ET.Element]) -> set[str]:
    starts = [value for attr in ("ObjectUID", "ObjectID") if (value := node.get(attr))]
    seen: set[str] = set()
    pending = list(starts)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(refs_in(objects[current], objects))
    return seen


def top_hashes(root: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in root:
        if node.get("ObjectID") is None and node.get("ObjectUID") is None:
            continue
        key = label(node)
        require(key not in result, f"rotulo top-level duplicado: {key}")
        result[key] = sha256_bytes(ET.tostring(node, encoding="utf-8"))
    return result


def ref_multiset(root: ET.Element) -> Counter[tuple[str, str, str]]:
    result: Counter[tuple[str, str, str]] = collections.Counter()
    for node in root:
        if node.get("ObjectID") is None and node.get("ObjectUID") is None:
            continue
        owner = label(node)
        for child in node.iter():
            for attr in ("ObjectRef", "ObjectURef"):
                value = child.get(attr)
                if value is not None:
                    result[(owner, attr, value)] += 1
    return result


def numeric_graph(root: ET.Element) -> dict[str, object]:
    ids = [node.get("ObjectID") for node in root if node.get("ObjectID")]
    id_counts = collections.Counter(ids)
    refs = [child.get("ObjectRef") for node in root for child in node.iter() if child.get("ObjectRef")]
    return {
        "object_id_count": len(ids),
        "duplicate_object_ids": sorted(value for value, count in id_counts.items() if count > 1),
        "object_ref_count": len(refs),
        "unresolved_object_refs": sorted(value for value in refs if value not in id_counts),
    }


def children_map(node: ET.Element) -> dict[str, str]:
    return {child.tag: child.text or "" for child in list(node)}


def param_value(param: ET.Element) -> float | None:
    current = param.findtext("CurrentValue")
    if current is not None:
        return float(current)
    start = param.findtext("StartKeyframe")
    if start:
        fields = start.split(",")
        if len(fields) >= 2:
            return float(fields[1])
    return None


def component_params(component: ET.Element, objects: Mapping[str, ET.Element]) -> list[ET.Element]:
    result: list[ET.Element] = []
    for ref_node in component.findall("./AudioComponent/Component/Params/Param"):
        param = resolve(objects, ref_node.get("ObjectRef"), "parametro de componente")
        require(param.tag == "AudioComponentParam", f"parametro aponta para {param.tag}")
        require(param.findtext("IsTimeVarying") == "false", f"parametro variavel: {param.findtext('Name')}")
        require(param.find("Keyframes") is None, f"parametro com keyframes: {param.findtext('Name')}")
        result.append(param)
    return result


def audit_intrinsic(component: ET.Element, objects: Mapping[str, ET.Element], kind: str) -> None:
    require(component.tag == "AudioFilterComponent", f"intrinseco {kind} nao e AudioFilterComponent")
    require(component.findtext("./AudioComponent/Component/Intrinsic") == "true", f"{kind} nao e intrinseco")
    params = component_params(component, objects)
    by_name: MutableMapping[str, list[float | None]] = collections.defaultdict(list)
    for param in params:
        by_name[param.findtext("Name") or ""].append(param_value(param))
    neutral = 0.17782793939113617
    names = ["Level"] if kind == "volume" else ["Left", "Right"]
    for name in names:
        require(len(by_name[name]) == 1, f"{kind}: parametro {name} ausente/duplicado")
        require(by_name[name][0] is not None and math.isclose(by_name[name][0], neutral, abs_tol=1e-7), f"{kind}: {name} nao neutro")
    for name in ("Mute", "Bypass"):
        require(all(value in {None, 0.0} for value in by_name.get(name, [])), f"{kind}: {name} ativo")


def indexed_components(chain: ET.Element) -> list[ET.Element]:
    refs = chain.findall("./ComponentChain/Components/Component")
    indices = [int(node.get("Index", "-1")) for node in refs]
    require(indices == list(range(len(indices))), f"indices de cadeia inesperados: {indices}")
    return refs


def component_match(component: ET.Element) -> str:
    return component.findtext("FilterMatchName") or ""


def block_pattern(node: ET.Element) -> re.Pattern[str]:
    tag, attr, value = identity(node)
    return re.compile(
        rf'(?ms)^\t<{re.escape(tag)}\b(?=[^\n]*\b{re.escape(attr)}="{re.escape(value)}")[^\n]*\n'
        rf'.*?^\t</{re.escape(tag)}>(?:\n|$)'
    )


def serialize_top(node: ET.Element) -> str:
    clone = copy.deepcopy(node)
    clone.tail = None
    ET.indent(clone, space="\t", level=1)
    return "\t" + ET.tostring(clone, encoding="unicode", short_empty_elements=True) + "\n"


def surgical_replace(xml: str, changed_nodes: Sequence[ET.Element]) -> tuple[str, dict[str, dict[str, str]]]:
    result = xml
    digests: dict[str, dict[str, str]] = {}
    for node in changed_nodes:
        pattern = block_pattern(node)
        matches = list(pattern.finditer(result))
        require(len(matches) == 1, f"bloco raw nao unico: {label(node)}")
        match = matches[0]
        before = match.group(0)
        after = serialize_top(node)
        require(before != after, f"bloco alvo nao mudou: {label(node)}")
        result = result[: match.start()] + after + result[match.end() :]
        digests[label(node)] = {
            "before_sha256": sha256_bytes(before.encode("utf-8")),
            "after_sha256": sha256_bytes(after.encode("utf-8")),
        }
    return result, digests


def unique_sequence(root: ET.Element, name: str) -> ET.Element:
    found = [node for node in root.findall("Sequence") if node.findtext("Name") == name]
    require(len(found) == 1, f"sequencia {name!r}: esperado 1, encontrado {len(found)}")
    return found[0]


def discover_items(root: ET.Element, objects: Mapping[str, ET.Element]) -> tuple[ET.Element, list[ET.Element]]:
    sequence = unique_sequence(root, SEQUENCE_NAME)
    reachable = closure(sequence, objects)
    items = [objects[value] for value in reachable if objects[value].tag == "AudioClipTrackItem"]
    require(len(items) == 20, f"sequencia alvo alcanca {len(items)} AudioClipTrackItems")
    items.sort(key=lambda node: int(node.findtext("./ClipTrackItem/TrackItem/Start") or "0"))
    require((items[0].findtext("./ClipTrackItem/TrackItem/Start") or "0") == "0", "timeline nao comeca em zero")
    for previous, current in zip(items, items[1:]):
        require(
            previous.findtext("./ClipTrackItem/TrackItem/End") == (current.findtext("./ClipTrackItem/TrackItem/Start") or "0"),
            "gap/overlap na timeline",
        )
    return sequence, items


def identify_item(item: ET.Element, objects: Mapping[str, ET.Element]) -> tuple[int, ET.Element, ET.Element, ET.Element]:
    sub_ref = item.find("./ClipTrackItem/SubClip")
    subclip = resolve(objects, sub_ref.get("ObjectRef") if sub_ref is not None else None, "SubClip")
    require(subclip.tag == "SubClip", f"SubClip aponta para {subclip.tag}")
    name = subclip.findtext("Name") or ""
    tokens = {int(value) for value in re.findall(r"(?<!\d)(\d{2,3})(?!\d)", name)}
    authorized = tokens & set(EXPECTED_IDS)
    require(len(authorized) == 1, f"ID ambiguo no SubClip {name!r}: {sorted(authorized)}")
    clip_id = next(iter(authorized))
    require(not (tokens & EXCLUDED_IDS), f"ID excluido no SubClip {name!r}")
    clip_ref = subclip.find("Clip")
    clip = resolve(objects, clip_ref.get("ObjectRef") if clip_ref is not None else None, f"AudioClip {clip_id}")
    require(clip.tag == "AudioClip", f"{clip_id}: Clip aponta para {clip.tag}")
    chain_ref = item.find("./ClipTrackItem/ComponentOwner/Components")
    chain = resolve(objects, chain_ref.get("ObjectRef") if chain_ref is not None else None, f"chain {clip_id}")
    require(chain.tag == "AudioComponentChain", f"{clip_id}: chain aponta para {chain.tag}")
    return clip_id, subclip, clip, chain


def validate_gui_dialog_reference() -> dict[str, object]:
    require(GUI_REFERENCE.is_file(), f"referencia GUI ausente: {GUI_REFERENCE}")
    actual_sha = sha256_file(GUI_REFERENCE)
    require(actual_sha == EXPECTED_GUI_REFERENCE_SHA256, "SHA da referencia GUI clean divergiu")
    root = ET.fromstring(gzip.decompress(GUI_REFERENCE.read_bytes()))
    objects = object_map(root)
    item = resolve(objects, "433", "TrackItem 107 da referencia GUI")
    props = item.find("./ClipTrackItem/TrackItem/Node/Properties")
    require(props is not None, "Properties da referencia GUI ausente")
    require(children_map(props) == CLEAN_DIALOG_PROPERTIES, f"estado Dialogue GUI divergiu: {children_map(props)}")
    require(not any(item.find(tag) is not None for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem")), "referencia GUI ainda tem pre-render")
    return {"path": str(GUI_REFERENCE), "sha256": actual_sha, "track_item_object_id": "433"}


def validate_sources_and_timeline(
    root: ET.Element,
    objects: Mapping[str, ET.Element],
    manifest: Mapping[str, object],
) -> list[dict[str, object]]:
    _sequence, items = discover_items(root, objects)
    rows: list[dict[str, object]] = []
    seen_audio_clips: set[str] = set()
    total_frames = 0
    for position, (expected_id, item) in enumerate(zip(EXPECTED_IDS, items), 1):
        clip_id, _subclip, clip, chain = identify_item(item, objects)
        require(clip_id == expected_id, f"ordem {position}: ID={clip_id}, esperado {expected_id}")
        require(clip.get("ObjectID") not in seen_audio_clips, f"AudioClip repetido: {clip.get('ObjectID')}")
        seen_audio_clips.add(clip.get("ObjectID") or "")
        start = int(item.findtext("./ClipTrackItem/TrackItem/Start") or "0")
        end = int(item.findtext("./ClipTrackItem/TrackItem/End", "-1"))
        duration = end - start
        require(duration > 0 and duration % TICKS_PER_SAMPLE == 0, f"{clip_id}: duracao invalida")
        frames = duration // TICKS_PER_SAMPLE
        raw = manifest["raws"][str(clip_id)]
        require(frames == int(raw["frames"]), f"{clip_id}: frames={frames}, manifesto={raw['frames']}")
        source_ref = clip.find("./Clip/Source")
        source = resolve(objects, source_ref.get("ObjectRef") if source_ref is not None else None, f"fonte {clip_id}")
        media_ref = source.find("./MediaSource/Media")
        media = resolve(objects, media_ref.get("ObjectURef") if media_ref is not None else None, f"media {clip_id}")
        media_path = Path(media.findtext("ActualMediaFilePath") or media.findtext("FilePath") or "").resolve()
        expected_path = (SUITE / str(raw["path"])).resolve()
        require(media_path == expected_path, f"{clip_id}: media {media_path}, esperado {expected_path}")
        require(media_path.is_file(), f"{clip_id}: media ausente {media_path}")
        require(sha256_file(media_path) == raw["file_sha256"], f"{clip_id}: SHA da media divergiu")
        total_frames += frames
        rows.append({
            "order": position,
            "id": clip_id,
            "track_item_object_id": item.get("ObjectID"),
            "audio_clip_object_id": clip.get("ObjectID"),
            "chain_object_id": chain.get("ObjectID"),
            "start_ticks": start,
            "end_ticks": end,
            "frames": frames,
            "media_path": str(media_path),
            "media_sha256": raw["file_sha256"],
        })
    require(total_frames == EXPECTED_TOTAL_FRAMES, f"total de frames={total_frames}")
    return rows


def ensure_target_not_shared(root: ET.Element, objects: Mapping[str, ET.Element], sequence: ET.Element, critical: set[str]) -> None:
    for other in root.findall("Sequence"):
        if other is sequence:
            continue
        overlap = critical & closure(other, objects)
        require(not overlap, f"sequencia {other.findtext('Name')} compartilha objetos alvo: {sorted(overlap)}")


def mutate(root: ET.Element, audit_rows: Mapping[int, Mapping[str, object]]) -> tuple[list[ET.Element], list[dict[str, object]], Counter[tuple[str, str, str]]]:
    objects = object_map(root)
    sequence, items = discover_items(root, objects)
    changed: list[ET.Element] = []
    summaries: list[dict[str, object]] = []
    expected_removed_refs: Counter[tuple[str, str, str]] = collections.Counter()
    critical: set[str] = set()

    for expected_id, item in zip(EXPECTED_IDS, items):
        clip_id, subclip, clip, chain = identify_item(item, objects)
        require(clip_id == expected_id, f"ordem divergente: {clip_id} != {expected_id}")
        audit = audit_rows[clip_id]
        require(item.get("ObjectID") == str(audit["item_object_id"]), f"{clip_id}: TrackItem divergiu da auditoria")
        gain = clip.find("Gain")
        require(gain is not None and gain.text is not None, f"{clip_id}: Gain Auto-Match ausente")
        require(math.isclose(float(gain.text), float(audit["auto_match_gain_linear"]), abs_tol=1e-12), f"{clip_id}: Gain Auto-Match divergiu")

        props = item.find("./ClipTrackItem/TrackItem/Node/Properties")
        require(props is not None, f"{clip_id}: TrackItem.Properties ausente")
        before_props = children_map(props)
        require(set(before_props) == set(CLEAN_DIALOG_PROPERTIES) | REMOVED_TRACK_PROPERTY_NAMES, f"{clip_id}: propriedades inesperadas {before_props}")
        require(all(before_props[key] == value for key, value in CLEAN_DIALOG_PROPERTIES.items()), f"{clip_id}: base Dialogue divergiu")
        for child in list(props):
            if child.tag in REMOVED_TRACK_PROPERTY_NAMES:
                props.remove(child)
        require(children_map(props) == CLEAN_DIALOG_PROPERTIES, f"{clip_id}: Dialogue clean divergente")

        require(item.findtext("PreRenderComponentChainHashVersion") == "1", f"{clip_id}: hash version de pre-render")
        prerender_removed: dict[str, str] = {}
        for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem"):
            child = item.find(tag)
            require(child is not None and child.get("ObjectRef") in objects, f"{clip_id}: {tag} ausente/invalido")
            value = child.get("ObjectRef") or ""
            prerender_removed[tag] = value
            expected_removed_refs[(label(item), "ObjectRef", value)] += 1
            item.remove(child)

        component_nodes = indexed_components(chain)
        component_refs = [node.get("ObjectRef") or "" for node in component_nodes]
        components = [resolve(objects, value, f"componente {clip_id}") for value in component_refs]
        matches = [component_match(component) for component in components]
        require(matches == [ENHANCE_MATCH, VOLUME_MATCH, CHANNEL_VOLUME_MATCH, SHELF_MATCH, LIMITER_MATCH], f"{clip_id}: cadeia inesperada {matches}")
        require(components[3].findtext("./AudioComponent/Component/InstanceName") == "DJI_FEMININA_CAPCUT_DEFINITIVO__SHELF60_M12_v1", f"{clip_id}: shelf inesperado")
        require(components[4].findtext("./AudioComponent/Component/InstanceName") == "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1", f"{clip_id}: limiter inesperado")
        require(prerender_removed["PreRenderMix"] == component_refs[0], f"{clip_id}: PreRenderMix nao aponta ao Enhance")
        pre_chain = resolve(objects, prerender_removed["PreRenderComponentChain"], f"pre-chain {clip_id}")
        require(pre_chain.tag == "AudioComponentChain", f"{clip_id}: pre-chain e {pre_chain.tag}")
        pre_chain_refs = [node.get("ObjectRef") for node in indexed_components(pre_chain)]
        require(len(pre_chain_refs) == 1, f"{clip_id}: pre-chain deveria ter um componente")
        pre_chain_component = resolve(objects, pre_chain_refs[0], f"componente de pre-chain {clip_id}")
        require(
            component_match(pre_chain_component) == "9d9d83e3-dd1a-4553-ab2c-67a4f3e75882",
            f"{clip_id}: componente de pre-chain inesperado {component_match(pre_chain_component)}",
        )
        preview = resolve(objects, prerender_removed["PreRenderSourceItem"], f"preview {clip_id}")
        require(preview.tag == "AudioPreviewItem", f"{clip_id}: preview e {preview.tag}")

        audit_intrinsic(components[1], objects, "volume")
        audit_intrinsic(components[2], objects, "channel_volume")
        container = chain.find("./ComponentChain/Components")
        require(container is not None and len(list(container)) == 5, f"{clip_id}: container de cadeia invalido")
        for node in list(container):
            container.remove(node)
        for index, ref_value in enumerate(component_refs[1:3]):
            container.append(ET.Element("Component", {"Index": str(index), "ObjectRef": ref_value}))
        for removed in (component_refs[0], component_refs[3], component_refs[4]):
            expected_removed_refs[(label(chain), "ObjectRef", removed)] += 1

        clip_props = clip.find("./Clip/Node/Properties")
        require(clip_props is not None, f"{clip_id}: Clip.Properties ausente")
        loudness = [child for child in list(clip_props) if child.tag.startswith("ESP.LoudnessSection.Analysis.")]
        require({child.tag for child in loudness} == LOUDNESS_ANALYSIS_NAMES and len(loudness) == 4, f"{clip_id}: analise loudness inesperada")
        require(math.isclose(float(next(child.text for child in loudness if child.tag.endswith("ITU1770LoudnessValue"))), float(audit["loudness_analyzed_lufs"]), abs_tol=1e-8), f"{clip_id}: loudness divergiu da auditoria")
        for child in loudness:
            clip_props.remove(child)
        clip.remove(gain)

        critical.update(filter(None, [item.get("ObjectID"), chain.get("ObjectID"), subclip.get("ObjectID"), clip.get("ObjectID"), *component_refs]))
        changed.extend([item, chain, clip])
        summaries.append({
            "id": clip_id,
            "track_item_object_id": item.get("ObjectID"),
            "chain_object_id": chain.get("ObjectID"),
            "audio_clip_object_id": clip.get("ObjectID"),
            "chain_before": matches,
            "chain_after": [VOLUME_MATCH, CHANNEL_VOLUME_MATCH],
            "removed_track_properties": sorted(REMOVED_TRACK_PROPERTY_NAMES),
            "remaining_track_properties": dict(CLEAN_DIALOG_PROPERTIES),
            "removed_prerender_refs": prerender_removed,
            "removed_automatch_gain": gain.text,
            "removed_loudness_analysis": sorted(LOUDNESS_ANALYSIS_NAMES),
        })

    require(len({label(node) for node in changed}) == 60, "whitelist de 60 objetos nao e unica")
    ensure_target_not_shared(root, objects, sequence, critical)
    require(sum(expected_removed_refs.values()) == 120, f"refs previstas removidas={sum(expected_removed_refs.values())}")
    return changed, summaries, expected_removed_refs


def validate_clean(root: ET.Element, audit_rows: Mapping[int, Mapping[str, object]]) -> None:
    objects = object_map(root)
    _sequence, items = discover_items(root, objects)
    for expected_id, item in zip(EXPECTED_IDS, items):
        clip_id, _subclip, clip, chain = identify_item(item, objects)
        require(clip_id == expected_id, f"ordem clean divergente: {clip_id}")
        require(item.get("ObjectID") == str(audit_rows[clip_id]["item_object_id"]), f"{clip_id}: TrackItem clean mudou")
        props = item.find("./ClipTrackItem/TrackItem/Node/Properties")
        require(props is not None and children_map(props) == CLEAN_DIALOG_PROPERTIES, f"{clip_id}: Dialogue clean invalido")
        require(not any(item.find(tag) is not None for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem")), f"{clip_id}: pre-render ainda ligado")
        require(item.findtext("PreRenderComponentChainHashVersion") == "1", f"{clip_id}: hash version ausente")
        component_nodes = indexed_components(chain)
        component_refs = [node.get("ObjectRef") or "" for node in component_nodes]
        components = [resolve(objects, value, f"componente clean {clip_id}") for value in component_refs]
        require([component_match(component) for component in components] == [VOLUME_MATCH, CHANNEL_VOLUME_MATCH], f"{clip_id}: cadeia clean invalida")
        audit_intrinsic(components[0], objects, "volume")
        audit_intrinsic(components[1], objects, "channel_volume")
        require(clip.find("Gain") is None, f"{clip_id}: Gain ainda presente")
        clip_props = clip.find("./Clip/Node/Properties")
        require(clip_props is not None and not any(child.tag.startswith("ESP.LoudnessSection.Analysis.") for child in clip_props), f"{clip_id}: analise loudness ainda presente")


def atomic_create(path: Path, payload: bytes) -> None:
    require(not path.exists(), f"recusa sobrescrever: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        require(temp.read_bytes() == payload, f"readback temporario falhou: {temp}")
        os.link(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    require(INPUT != OUTPUT, "entrada e saida coincidem")
    require(INPUT.resolve().parent == (TRIAL / "checkpoints").resolve(), "input fora do checkpoint")
    require(OUTPUT.resolve().parent == PROJECTS.resolve(), "output fora de projetos_descartaveis")
    require(REPORT.resolve().parent == (TRIAL / "analises").resolve(), "report fora da area do ensaio")
    require(INPUT.is_file(), f"checkpoint ausente: {INPUT}")
    require(not OUTPUT.exists() and not REPORT.exists(), "recusa sobrescrever candidato/report")

    input_bytes = INPUT.read_bytes()
    input_sha = sha256_bytes(input_bytes)
    require(input_sha == EXPECTED_INPUT_SHA256, f"SHA do checkpoint divergiu: {input_sha}")
    require(sha256_file(SOURCE_MANIFEST) == EXPECTED_MANIFEST_SHA256, "SHA do manifesto de fontes divergiu")
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    require([int(value) for value in manifest["fixed_order"]] == EXPECTED_IDS, "ordem do manifesto divergiu")
    require(manifest["preflight_status"] == "PASS_20_OF_20", "preflight de fontes nao esta PASS")
    audit = json.loads(SOURCE_AUDIT.read_text(encoding="utf-8"))
    require(audit["status"] == "PASS" and audit["project"]["sha256"] == EXPECTED_INPUT_SHA256, "auditoria-base divergiu")
    audit_rows = {int(row["id"]): row for row in audit["timeline"]["items"]}
    require(list(audit_rows) == EXPECTED_IDS, "ordem da auditoria-base divergiu")
    gui_reference = validate_gui_dialog_reference()

    try:
        xml_before_bytes = gzip.decompress(input_bytes)
    except (OSError, EOFError) as exc:
        raise GuardError(f"gzip de entrada invalido: {exc}") from exc
    root_before = ET.fromstring(xml_before_bytes)
    require(root_before.tag == "PremiereData", f"raiz XML inesperada: {root_before.tag}")
    objects_before = object_map(root_before)
    timeline_before = validate_sources_and_timeline(root_before, objects_before, manifest)
    top_before = top_hashes(root_before)
    refs_before = ref_multiset(root_before)
    graph_before = numeric_graph(root_before)

    root_work = copy.deepcopy(root_before)
    changed_nodes, mutation_rows, expected_removed_refs = mutate(root_work, audit_rows)
    xml_before = xml_before_bytes.decode("utf-8")
    xml_after, block_digests = surgical_replace(xml_before, changed_nodes)
    xml_after_bytes = xml_after.encode("utf-8")
    root_after = ET.fromstring(xml_after_bytes)
    objects_after = object_map(root_after)
    validate_clean(root_after, audit_rows)
    timeline_after = validate_sources_and_timeline(root_after, objects_after, manifest)
    require(timeline_after == timeline_before, "fontes/timing mudaram durante a limpeza")

    top_after = top_hashes(root_after)
    require(top_before.keys() == top_after.keys(), "conjunto de objetos top-level mudou")
    actual_changed = sorted(key for key in top_before if top_before[key] != top_after[key])
    expected_changed = sorted(label(node) for node in changed_nodes)
    require(actual_changed == expected_changed, f"objetos alterados fora da whitelist: {actual_changed}")
    refs_after = ref_multiset(root_after)
    added_refs = refs_after - refs_before
    removed_refs = refs_before - refs_after
    require(not added_refs, f"referencias adicionadas: {added_refs}")
    require(removed_refs == expected_removed_refs, f"referencias removidas divergiram: {removed_refs}")
    graph_after = numeric_graph(root_after)
    require(graph_after["object_id_count"] == graph_before["object_id_count"], "ObjectID count mudou")
    require(graph_after["duplicate_object_ids"] == graph_before["duplicate_object_ids"], "duplicatas de ObjectID mudaram")
    require(graph_after["unresolved_object_refs"] == graph_before["unresolved_object_refs"], "refs nao resolvidas mudaram")
    require(graph_after["object_ref_count"] == int(graph_before["object_ref_count"]) - 120, "contagem ObjectRef nao caiu em 120")

    compressed_after = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after_bytes, "round-trip gzip em memoria falhou")
    ET.fromstring(gzip.decompress(compressed_after))
    require(sha256_file(INPUT) == input_sha, "checkpoint mudou durante a operacao")

    output_sha = sha256_bytes(compressed_after)
    report = {
        "schema": "codex.premiere.safe-prproj-mutation.clean20.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "CLEAN_OFFLINE_CANDIDATE_REQUIRES_PREMIERE_OPEN_RESAVE_VALIDATION",
        "operation": "detach_enhance_prerender_automatch_shelf_limiter_keep_neutral_intrinsics_and_generic_dialogue",
        "input": {"path": str(INPUT), "sha256_before": input_sha, "sha256_after": sha256_file(INPUT), "modified": False},
        "output": {"path": str(OUTPUT), "sha256": output_sha, "bytes": len(compressed_after)},
        "source_manifest": {"path": str(SOURCE_MANIFEST), "sha256": EXPECTED_MANIFEST_SHA256},
        "gui_roundtrip_reference_for_generic_dialogue": gui_reference,
        "scope": {
            "sequence": SEQUENCE_NAME,
            "ids": EXPECTED_IDS,
            "changed_top_level_objects": actual_changed,
            "changed_top_level_object_count": len(actual_changed),
            "all_other_top_level_objects_byte_semantics_unchanged": True,
            "target_objects_not_shared_with_other_sequences": True,
            "personal_files_written": False,
            "premiere_opened": False,
        },
        "timeline": {"before": timeline_before, "after_identical": True, "total_frames": EXPECTED_TOTAL_FRAMES},
        "clean_state": {
            "chain_per_clip": [VOLUME_MATCH, CHANNEL_VOLUME_MATCH],
            "intrinsics_neutral": True,
            "generic_dialogue_properties": dict(CLEAN_DIALOG_PROPERTIES),
            "enhance_active": False,
            "prerender_links_active": False,
            "prerender_hash_schema_marker_retained": "1",
            "automatch_gain_present": False,
            "automatch_analysis_present": False,
            "shelf_active": False,
            "limiter_active": False,
            "historical_orphan_objects_retained_for_minimal_mutation": True,
        },
        "mutation_per_clip": mutation_rows,
        "isolation": {
            "removed_reference_occurrences": sum(removed_refs.values()),
            "added_reference_occurrences": 0,
            "expected_removed_reference_occurrences": 120,
            "top_level_object_set_unchanged": True,
            "object_id_count_unchanged": True,
            "unresolved_object_refs_unchanged": True,
            "block_digests": block_digests,
        },
        "integrity": {"gzip": True, "xml": True, "graph_before": graph_before, "graph_after": graph_after},
        "mandatory_next_step": "Open only this disposable candidate in Premiere, verify the 20 clips show Dialogue with no Enhance/Repair/Loudness and only neutral intrinsic controls, then Save and audit the round-trip before adding Reduce Noise/DeEss.",
    }

    atomic_create(OUTPUT, compressed_after)
    require(sha256_file(OUTPUT) == output_sha, "SHA do output gravado divergiu")
    readback = OUTPUT.read_bytes()
    require(gzip.decompress(readback) == xml_after_bytes, "readback gzip do output falhou")
    validate_clean(ET.fromstring(gzip.decompress(readback)), audit_rows)
    require(sha256_file(INPUT) == input_sha, "checkpoint mudou apos escrita")
    report_payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_create(REPORT, report_payload)
    require(json.loads(REPORT.read_text(encoding="utf-8"))["output"]["sha256"] == output_sha, "report gravado invalido")
    print(json.dumps({
        "status": report["status"],
        "output": report["output"],
        "changed_top_level_object_count": len(actual_changed),
        "removed_reference_occurrences": sum(removed_refs.values()),
        "timeline_total_frames": EXPECTED_TOTAL_FRAMES,
        "input_unchanged": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr)
        raise SystemExit(2)
