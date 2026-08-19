#!/usr/bin/env python3
"""Propaga somente o estado semantico do Reduce Noise do clipe 64.

O mutador e fail-closed e opera offline sobre uma copia descartavel do
Premiere. Ele nao substitui componentes, IDs, OpaqueData ou presets. Nos 19
destinos, altera apenas o Amount do efeito e seu espelho na interface do
Essential Sound. Os demais valores pedidos (Make-Up, Auto Gain, Focus/Shelve,
Difference, toggle e EffectOrder) ja sao identicos ao 64 e sao validados sem
reescrita.
"""

from __future__ import annotations

import collections
import copy
import gzip
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import criar_copia_clean_sem_enhance_20 as safe


WORKSPACE = Path(__file__).resolve().parents[2]
NATIVE = WORKSPACE / "premiere_audition_native_preset"
SUITE = NATIVE / "comparacao_ab" / "FLUXO_APROVADO_20_20260814"
TRIAL = NATIVE / "comparacao_ab" / "SEM_ENHANCE_ESSENTIAL_20_20260814"
PROJECTS = NATIVE / "projetos_descartaveis"

INPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_DE0_20_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_DE0_20_CLONADO_OFFLINE.prproj"
REPORT = TRIAL / "analises" / "MUTACAO_RN20_DE0_CLONADO_OFFLINE_MANIFEST.json"
SOURCE_MANIFEST = SUITE / "manifest_preflight.json"

EXPECTED_INPUT_SHA256 = "6ed9b98336ed34bf54461de96df16289560c1458aef5de60ca7529482f4b5e8a"
EXPECTED_MANIFEST_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"
SEQUENCE_NAME = "FLUXO_APROVADO_20"
EXPECTED_IDS = [64, 65, 66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 81, 82, 84, 92, 94, 95, 101, 107]
DESTINATION_IDS = EXPECTED_IDS[1:]
EXPECTED_TOTAL_FRAMES = 25_181_516

RN_MATCH = "c6762838-e451-4d22-9c90-b0eaa734605a"
SEMANTIC_PARAMS = (
    "Amount",
    "Make-Up Gain",
    "Auto Gain",
    "Shelve Type",  # nome nativo do controle Focus
    "Output Difference Signal",
)
UI_AMOUNT = "ESP.RestorationSection.2"
UI_TOGGLE = "ESP.RestorationSection.1"
UI_COMPONENT_ID = "ESP.RestorationSection.ReduceNoise.ComponentID"
EFFECT_ORDER = "EssentialSound.EffectOrder"

SOURCE_EXPECTED = {
    "ui_amount": "1.96371304989",
    "ui_toggle": "1",
    "effect_order": "20",
    "Amount": {
        "StartKeyframe": "-91445760000000000,0.196371302009,0,0,0,0,0,0",
        "CurrentValue": "0.19637130200862885",
    },
}

# Whitelist material fixada ao snapshot de entrada acima. Somente estes 38
# objetos top-level podem mudar: 19 TrackItems e 19 parametros Amount.
EXPECTED_DEST_OBJECTS: Mapping[int, Mapping[str, str]] = {
    65: {"item": "2188", "amount": "4690"},
    66: {"item": "2189", "amount": "4698"},
    67: {"item": "2190", "amount": "4706"},
    68: {"item": "2191", "amount": "4714"},
    69: {"item": "2192", "amount": "4722"},
    71: {"item": "2193", "amount": "4730"},
    72: {"item": "2194", "amount": "4738"},
    73: {"item": "2195", "amount": "4746"},
    74: {"item": "2196", "amount": "4754"},
    75: {"item": "2197", "amount": "4762"},
    76: {"item": "2198", "amount": "4770"},
    81: {"item": "2199", "amount": "4778"},
    82: {"item": "2200", "amount": "4786"},
    84: {"item": "2201", "amount": "4794"},
    92: {"item": "2202", "amount": "4802"},
    94: {"item": "2203", "amount": "4810"},
    95: {"item": "2204", "amount": "4818"},
    101: {"item": "2205", "amount": "4826"},
    107: {"item": "2206", "amount": "4834"},
}


class GuardError(RuntimeError):
    """Uma pre-condicao de seguranca/integridade falhou."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return safe.sha256_file(path)


def exact_children(node: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in list(node):
        require(child.tag not in result, f"propriedade duplicada: {child.tag}")
        result[child.tag] = child.text or ""
    return result


def indexed_component_refs(chain: ET.Element) -> list[str]:
    refs = safe.indexed_components(chain)
    values = [node.get("ObjectRef") or "" for node in refs]
    require(all(values), f"chain {chain.get('ObjectID')}: ObjectRef vazio")
    return values


def param_signature(param: ET.Element) -> dict[str, str]:
    require(param.findtext("IsTimeVarying") == "false", f"parametro variavel: {param.findtext('Name')}")
    require(param.find("Keyframes") is None, f"parametro com Keyframes: {param.findtext('Name')}")
    start = param.find("StartKeyframe")
    current = param.find("CurrentValue")
    require(start is not None and start.text is not None, f"{param.findtext('Name')}: StartKeyframe ausente")
    require(current is not None and current.text is not None, f"{param.findtext('Name')}: CurrentValue ausente")
    return {"StartKeyframe": start.text, "CurrentValue": current.text}


def set_param_signature(param: ET.Element, signature: Mapping[str, str]) -> None:
    start = param.find("StartKeyframe")
    current = param.find("CurrentValue")
    require(start is not None and current is not None, f"{param.findtext('Name')}: campos ausentes")
    start.text = signature["StartKeyframe"]
    current.text = signature["CurrentValue"]


def opaque_snapshot(component: ET.Element) -> dict[str, Any]:
    opaque = component.find("OpaqueData")
    require(opaque is not None, f"RN {component.get('ObjectID')}: OpaqueData ausente")
    raw = ET.tostring(opaque, encoding="utf-8")
    return {
        "encoding": opaque.get("Encoding"),
        "binary_hash": opaque.get("BinaryHash"),
        "text_length": len(opaque.text or ""),
        "xml_sha256": sha256_bytes(raw),
    }


def collect_states(root: ET.Element) -> tuple[ET.Element, dict[int, dict[str, Any]]]:
    objects = safe.object_map(root)
    sequence, items = safe.discover_items(root, objects)
    require(len(items) == 20, f"timeline tem {len(items)} itens")
    states: dict[int, dict[str, Any]] = {}

    for expected_id, item in zip(EXPECTED_IDS, items):
        clip_id, subclip, clip, chain = safe.identify_item(item, objects)
        require(clip_id == expected_id, f"ordem divergente: {clip_id} != {expected_id}")
        refs = indexed_component_refs(chain)
        require(len(refs) == 1, f"{clip_id}: cadeia tem {len(refs)} componentes")
        component = safe.resolve(objects, refs[0], f"Reduce Noise {clip_id}")
        require(component.tag == "AudioFilterComponent", f"{clip_id}: componente e {component.tag}")
        require(safe.component_match(component) == RN_MATCH, f"{clip_id}: efeito inesperado {safe.component_match(component)}")

        params = safe.component_params(component, objects)
        names = [param.findtext("Name") or "" for param in params]
        require(names == ["Bypass", *SEMANTIC_PARAMS], f"{clip_id}: parametros inesperados {names}")
        bypass = params[0]
        require(bypass.findtext("IsTimeVarying") == "false", f"{clip_id}: bypass variavel")
        require(bypass.find("Keyframes") is None, f"{clip_id}: bypass com keyframes")
        require(bypass.find("StartKeyframe") is None and bypass.find("CurrentValue") is None, f"{clip_id}: bypass explicito")
        by_name = {param.findtext("Name") or "": param for param in params[1:]}

        props_node = item.find("./ClipTrackItem/TrackItem/Node/Properties")
        require(props_node is not None, f"{clip_id}: TrackItem.Properties ausente")
        props = exact_children(props_node)
        component_id = component.findtext("./AudioComponent/Component/ID")
        require(component_id is not None, f"{clip_id}: Component.ID ausente")
        require(props.get(UI_COMPONENT_ID) == component_id, f"{clip_id}: ComponentID UI/efeito diverge")
        require(props.get(UI_TOGGLE) in {"0", "1"}, f"{clip_id}: toggle invalido")
        require(UI_AMOUNT in props, f"{clip_id}: Amount UI ausente")

        order_node = component.find(f"./AudioComponent/Component/Node/Properties/{EFFECT_ORDER}")
        require(order_node is not None and order_node.text is not None, f"{clip_id}: EffectOrder ausente")
        require(item.find("PreRenderComponentChain") is None, f"{clip_id}: PreRenderComponentChain ativo")
        require(item.find("PreRenderMix") is None, f"{clip_id}: PreRenderMix ativo")
        require(item.find("PreRenderSourceItem") is None, f"{clip_id}: PreRenderSourceItem ativo")
        require(clip.find("Gain") is None, f"{clip_id}: Gain/Auto-Match presente")
        clip_props = clip.find("./Clip/Node/Properties")
        require(clip_props is not None, f"{clip_id}: Clip.Properties ausente")
        require(not any(child.tag.startswith("ESP.LoudnessSection.") for child in clip_props), f"{clip_id}: analise Auto-Match presente")
        require("EnhanceSpeechCompleted" not in props, f"{clip_id}: EnhanceSpeechCompleted presente")
        require("ESP.EnhanceSpeechSection.1" not in props, f"{clip_id}: Enhance toggle presente")
        require("ESP.EnhanceSpeechSection.2" not in props, f"{clip_id}: Enhance amount presente")
        require("ESP.EnhanceSpeechSection.EnhanceSpeechEnabled.ComponentID" not in props, f"{clip_id}: Enhance ComponentID presente")

        states[clip_id] = {
            "id": clip_id,
            "item": item,
            "subclip": subclip,
            "clip": clip,
            "chain": chain,
            "component": component,
            "params": by_name,
            "properties_node": props_node,
            "properties": props,
            "component_id": component_id,
            "effect_order_node": order_node,
            "effect_order": order_node.text,
            "semantic": {name: param_signature(by_name[name]) for name in SEMANTIC_PARAMS},
            "ui_amount": props[UI_AMOUNT],
            "ui_toggle": props[UI_TOGGLE],
            "preset_guid": props.get("ESP.PresetGuid"),
            "opaque": opaque_snapshot(component),
        }

    require(list(states) == EXPECTED_IDS, f"IDs coletados divergentes: {list(states)}")
    reachable = safe.closure(sequence, objects)
    reachable_filters = [objects[value] for value in reachable if objects[value].tag == "AudioFilterComponent"]
    require(len(reachable_filters) == 20, f"filtros alcancaveis={len(reachable_filters)}, esperado 20")
    require(collections.Counter(safe.component_match(node) for node in reachable_filters) == collections.Counter({RN_MATCH: 20}), "ha efeito adicional/inesperado na sequencia")
    require({node.get("ObjectID") for node in reachable_filters} == {state["component"].get("ObjectID") for state in states.values()}, "inventario de filtros divergiu")
    return sequence, states


def validate_input_state(states: Mapping[int, Mapping[str, Any]]) -> None:
    source = states[64]
    require(source["item"].get("ObjectID") == "2187", "TrackItem do 64 mudou")
    require(source["params"]["Amount"].get("ObjectID") == "4682", "parametro Amount do 64 mudou")
    require(source["ui_amount"] == SOURCE_EXPECTED["ui_amount"], "Amount UI do 64 divergiu")
    require(source["ui_toggle"] == SOURCE_EXPECTED["ui_toggle"], "toggle do 64 divergiu")
    require(source["effect_order"] == SOURCE_EXPECTED["effect_order"], "EffectOrder do 64 divergiu")
    require(source["semantic"]["Amount"] == SOURCE_EXPECTED["Amount"], "Amount DSP do 64 divergiu")
    for name in SEMANTIC_PARAMS[1:]:
        signature = source["semantic"][name]
        require(float(signature["CurrentValue"]) == 0.0, f"64: {name} nao e zero")
        fields = signature["StartKeyframe"].split(",")
        require(len(fields) == 8 and float(fields[1]) == 0.0, f"64: StartKeyframe de {name} nao e zero")

    for clip_id in DESTINATION_IDS:
        state = states[clip_id]
        expected = EXPECTED_DEST_OBJECTS[clip_id]
        require(state["item"].get("ObjectID") == expected["item"], f"{clip_id}: TrackItem mudou")
        require(state["params"]["Amount"].get("ObjectID") == expected["amount"], f"{clip_id}: parametro Amount mudou")
        require(state["ui_toggle"] == source["ui_toggle"] == "1", f"{clip_id}: toggle nao ativo")
        require(state["effect_order"] == source["effect_order"] == "20", f"{clip_id}: EffectOrder divergiu")
        require(float(state["ui_amount"]) == 0.0, f"{clip_id}: Amount UI nao e zero")
        require(float(state["semantic"]["Amount"]["CurrentValue"]) == 0.0, f"{clip_id}: Amount DSP nao e zero")
        for name in SEMANTIC_PARAMS[1:]:
            require(state["semantic"][name] == source["semantic"][name], f"{clip_id}: {name} diverge do 64")


def validate_output_state(states: Mapping[int, Mapping[str, Any]], before: Mapping[int, Mapping[str, Any]]) -> None:
    source = states[64]
    source_before = before[64]
    require(source["semantic"] == source_before["semantic"], "fonte 64 mudou")
    require(source["ui_amount"] == source_before["ui_amount"], "UI do 64 mudou")
    require(source["opaque"] == source_before["opaque"], "OpaqueData do 64 mudou")
    for clip_id in EXPECTED_IDS:
        state = states[clip_id]
        old = before[clip_id]
        require(state["ui_toggle"] == source["ui_toggle"] == "1", f"{clip_id}: toggle final divergiu")
        require(state["ui_amount"] == source["ui_amount"], f"{clip_id}: Amount UI final divergiu")
        require(state["effect_order"] == source["effect_order"] == "20", f"{clip_id}: EffectOrder final divergiu")
        require(state["semantic"] == source["semantic"], f"{clip_id}: parametros semanticos finais divergem")
        require(state["component_id"] == old["component_id"], f"{clip_id}: Component.ID mudou")
        require(state["properties"].get(UI_COMPONENT_ID) == old["component_id"], f"{clip_id}: ComponentID UI mudou")
        require(state["preset_guid"] == old["preset_guid"], f"{clip_id}: PresetGuid mudou")
        require(state["opaque"] == old["opaque"], f"{clip_id}: OpaqueData mudou")


def immutable_snapshot(states: Mapping[int, Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for clip_id, state in states.items():
        result[clip_id] = {
            "item_object_id": state["item"].get("ObjectID"),
            "chain_object_id": state["chain"].get("ObjectID"),
            "component_object_id": state["component"].get("ObjectID"),
            "component_id": state["component_id"],
            "amount_param_object_id": state["params"]["Amount"].get("ObjectID"),
            "semantic": copy.deepcopy(state["semantic"]),
            "ui_amount": state["ui_amount"],
            "ui_toggle": state["ui_toggle"],
            "effect_order": state["effect_order"],
            "preset_guid": state["preset_guid"],
            "opaque": copy.deepcopy(state["opaque"]),
        }
    return result


def apply_mutation(states: Mapping[int, Mapping[str, Any]]) -> tuple[list[ET.Element], list[dict[str, Any]]]:
    source = states[64]
    changed: list[ET.Element] = []
    rows: list[dict[str, Any]] = []
    for clip_id in DESTINATION_IDS:
        state = states[clip_id]
        item = state["item"]
        amount_param = state["params"]["Amount"]
        item_before = ET.tostring(item, encoding="utf-8")
        param_before = ET.tostring(amount_param, encoding="utf-8")

        # Copia apenas os valores semanticos. Os demais quatro parametros,
        # toggle e EffectOrder ja foram provados identicos e ficam byte a byte.
        set_param_signature(amount_param, source["semantic"]["Amount"])
        amount_ui_node = state["properties_node"].find(UI_AMOUNT)
        require(amount_ui_node is not None, f"{clip_id}: node UI Amount ausente")
        amount_ui_node.text = source["ui_amount"]

        require(ET.tostring(item, encoding="utf-8") != item_before, f"{clip_id}: TrackItem nao mudou")
        require(ET.tostring(amount_param, encoding="utf-8") != param_before, f"{clip_id}: Amount param nao mudou")
        changed.extend([item, amount_param])
        rows.append({
            "id": clip_id,
            "track_item_object_id": item.get("ObjectID"),
            "amount_param_object_id": amount_param.get("ObjectID"),
            "ui_amount_before": state["ui_amount"],
            "ui_amount_after": source["ui_amount"],
            "dsp_amount_before": state["semantic"]["Amount"],
            "dsp_amount_after": source["semantic"]["Amount"],
            "make_up_auto_gain_focus_difference_already_equal": True,
            "toggle_already_equal": state["ui_toggle"],
            "effect_order_already_equal": state["effect_order"],
            "component_id_preserved": state["component_id"],
            "preset_guid_preserved": state["preset_guid"],
            "opaque_preserved": state["opaque"],
        })

    expected_labels = sorted(
        [f"AudioClipTrackItem:ObjectID={values['item']}" for values in EXPECTED_DEST_OBJECTS.values()]
        + [f"AudioComponentParam:ObjectID={values['amount']}" for values in EXPECTED_DEST_OBJECTS.values()]
    )
    actual_labels = sorted(safe.label(node) for node in changed)
    require(len(changed) == 38 and len(set(actual_labels)) == 38, f"whitelist nao tem 38 objetos unicos: {len(changed)}")
    require(actual_labels == expected_labels, f"whitelist divergente: {actual_labels}")
    return changed, rows


def main() -> int:
    require(INPUT != OUTPUT, "entrada e saida coincidem")
    require(INPUT.resolve().parent == PROJECTS.resolve(), "input fora de projetos_descartaveis")
    require(OUTPUT.resolve().parent == PROJECTS.resolve(), "output fora de projetos_descartaveis")
    require(REPORT.resolve().parent == (TRIAL / "analises").resolve(), "report fora da area do ensaio")
    require(INPUT.is_file(), f"input ausente: {INPUT}")
    require(not OUTPUT.exists(), f"recusa sobrescrever output: {OUTPUT}")
    require(not REPORT.exists(), f"recusa sobrescrever manifesto: {REPORT}")

    input_bytes = INPUT.read_bytes()
    input_sha = sha256_bytes(input_bytes)
    require(input_sha == EXPECTED_INPUT_SHA256, f"SHA do input divergiu: {input_sha}")
    require(sha256_file(SOURCE_MANIFEST) == EXPECTED_MANIFEST_SHA256, "SHA do manifesto de fontes divergiu")
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    require([int(value) for value in manifest["fixed_order"]] == EXPECTED_IDS, "ordem do manifesto divergiu")
    require(manifest["preflight_status"] == "PASS_20_OF_20", "manifesto de fontes nao esta PASS")
    require(int(manifest["raws_total_frames"]) == EXPECTED_TOTAL_FRAMES, "total de frames do manifesto divergiu")

    try:
        xml_before_bytes = gzip.decompress(input_bytes)
    except (OSError, EOFError) as exc:
        raise GuardError(f"gzip de entrada invalido: {exc}") from exc
    root_before = ET.fromstring(xml_before_bytes)
    require(root_before.tag == "PremiereData", f"raiz XML inesperada: {root_before.tag}")
    objects_before = safe.object_map(root_before)
    sequence_before, states_before_live = collect_states(root_before)
    validate_input_state(states_before_live)
    states_before = immutable_snapshot(states_before_live)
    timeline_before = safe.validate_sources_and_timeline(root_before, objects_before, manifest)
    require(sum(int(row["frames"]) for row in timeline_before) == EXPECTED_TOTAL_FRAMES, "total de frames da timeline divergiu")

    critical = {
        value
        for state in states_before_live.values()
        for value in (
            state["item"].get("ObjectID"),
            state["chain"].get("ObjectID"),
            state["component"].get("ObjectID"),
            *(param.get("ObjectID") for param in state["params"].values()),
        )
        if value
    }
    safe.ensure_target_not_shared(root_before, objects_before, sequence_before, critical)

    top_before = safe.top_hashes(root_before)
    refs_before = safe.ref_multiset(root_before)
    graph_before = safe.numeric_graph(root_before)

    root_work = copy.deepcopy(root_before)
    _sequence_work, states_work = collect_states(root_work)
    validate_input_state(states_work)
    changed_nodes, mutation_rows = apply_mutation(states_work)
    xml_after, block_digests = safe.surgical_replace(xml_before_bytes.decode("utf-8"), changed_nodes)
    xml_after_bytes = xml_after.encode("utf-8")
    root_after = ET.fromstring(xml_after_bytes)
    objects_after = safe.object_map(root_after)
    _sequence_after, states_after_live = collect_states(root_after)
    validate_output_state(states_after_live, states_before)
    states_after = immutable_snapshot(states_after_live)
    timeline_after = safe.validate_sources_and_timeline(root_after, objects_after, manifest)
    require(timeline_after == timeline_before, "fontes, ordem ou timing mudaram")

    top_after = safe.top_hashes(root_after)
    require(top_before.keys() == top_after.keys(), "conjunto de objetos top-level mudou")
    actual_changed = sorted(key for key in top_before if top_before[key] != top_after[key])
    expected_changed = sorted(safe.label(node) for node in changed_nodes)
    require(actual_changed == expected_changed and len(actual_changed) == 38, f"objetos fora da whitelist: {actual_changed}")

    refs_after = safe.ref_multiset(root_after)
    require(refs_after == refs_before, "grafo de referencias mudou")
    graph_after = safe.numeric_graph(root_after)
    require(graph_after == graph_before, "inventario ObjectID/ObjectRef mudou")
    require(sha256_file(INPUT) == input_sha, "input mudou durante a mutacao")

    compressed_after = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after_bytes, "round-trip gzip em memoria falhou")
    ET.fromstring(gzip.decompress(compressed_after))
    output_sha = sha256_bytes(compressed_after)

    report = {
        "schema": "codex.premiere.safe-prproj-mutation.clone-rn64-semantic-to-20.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS_OFFLINE_CANDIDATE_REQUIRES_PREMIERE_ROUNDTRIP",
        "operation": "clone_only_reduce_noise_semantic_amount_from_64_to_19_existing_instances",
        "input": {"path": str(INPUT), "sha256_before": input_sha, "sha256_after": sha256_file(INPUT), "modified": False},
        "output": {"path": str(OUTPUT), "sha256": output_sha, "bytes": len(compressed_after)},
        "source_manifest": {"path": str(SOURCE_MANIFEST), "sha256": EXPECTED_MANIFEST_SHA256},
        "scope": {
            "sequence": SEQUENCE_NAME,
            "ids": EXPECTED_IDS,
            "source_id": 64,
            "destination_ids": DESTINATION_IDS,
            "changed_top_level_objects": actual_changed,
            "changed_top_level_object_count": len(actual_changed),
            "whitelist_exact": True,
            "target_objects_not_shared_with_other_sequences": True,
            "premiere_opened": False,
        },
        "semantic_source": {
            "ui_amount": states_before[64]["ui_amount"],
            "normalized_amount": states_before[64]["semantic"]["Amount"],
            "make_up_gain": states_before[64]["semantic"]["Make-Up Gain"],
            "auto_gain": states_before[64]["semantic"]["Auto Gain"],
            "focus_native_name": "Shelve Type",
            "focus": states_before[64]["semantic"]["Shelve Type"],
            "difference": states_before[64]["semantic"]["Output Difference Signal"],
            "toggle": states_before[64]["ui_toggle"],
            "effect_order": states_before[64]["effect_order"],
        },
        "preserved_by_design": {
            "component_and_parameter_object_ids": True,
            "per_instance_component_id": True,
            "opaque_data": True,
            "preset_guid": True,
            "all_references": True,
            "all_non_whitelisted_top_level_objects": True,
            "sources_order_timing": True,
        },
        "processing_audit": {
            "reduce_noise_components": 20,
            "reduce_noise_nonzero_after": 20,
            "chain_per_clip": [RN_MATCH],
            "other_audio_filters": 0,
            "enhance": False,
            "deess": False,
            "automatch_gain_or_analysis": False,
            "eq": False,
            "limiter": False,
            "prerender_links": False,
        },
        "timeline": {"items": timeline_after, "total_frames": EXPECTED_TOTAL_FRAMES, "before_after_identical": True},
        "mutation_per_destination": mutation_rows,
        "states_before": {str(key): value for key, value in states_before.items()},
        "states_after": {str(key): value for key, value in states_after.items()},
        "integrity": {
            "gzip": True,
            "xml": True,
            "references_before_after_identical": True,
            "graph_before": graph_before,
            "graph_after": graph_after,
            "block_digests": block_digests,
            "refuse_overwrite_guard": True,
        },
        "mandatory_next_step": "Abrir somente esta copia no Premiere, salvar uma vez e fazer auditoria round-trip antes de exportar os 20 audios.",
    }

    safe.atomic_create(OUTPUT, compressed_after)
    require(sha256_file(OUTPUT) == output_sha, "SHA do output gravado divergiu")
    output_readback = OUTPUT.read_bytes()
    require(gzip.decompress(output_readback) == xml_after_bytes, "readback gzip do output falhou")
    _seq_readback, readback_states = collect_states(ET.fromstring(gzip.decompress(output_readback)))
    validate_output_state(readback_states, states_before)
    require(sha256_file(INPUT) == input_sha, "input mudou apos escrita")

    # Prova interna do caminho de reexecucao: agora que OUTPUT existe, a mesma
    # precondicao de entrada deve obrigatoriamente recusar uma nova escrita.
    overwrite_probe_refused = False
    try:
        require(not OUTPUT.exists(), f"recusa sobrescrever output: {OUTPUT}")
    except GuardError:
        overwrite_probe_refused = True
    require(overwrite_probe_refused, "probe de recusa de sobrescrita falhou")
    report["integrity"]["refuse_overwrite_probe_after_create"] = True

    report_payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    safe.atomic_create(REPORT, report_payload)
    require(json.loads(REPORT.read_text(encoding="utf-8"))["output"]["sha256"] == output_sha, "manifesto gravado invalido")
    require(sha256_file(INPUT) == input_sha, "input mudou apos manifesto")

    print(json.dumps({
        "status": report["status"],
        "input_sha256": input_sha,
        "output": report["output"],
        "manifest": str(REPORT),
        "changed_top_level_object_count": len(actual_changed),
        "reduce_noise_nonzero_after": 20,
        "timeline_total_frames": EXPECTED_TOTAL_FRAMES,
        "references_unchanged": True,
        "input_unchanged": True,
        "refuse_overwrite_probe_after_create": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, safe.GuardError) as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr)
        raise SystemExit(2)
