#!/usr/bin/env python3
"""Cria a candidata nativa RN20 -> EQ C1 -> limiter nos 20 clipes.

O script opera somente sobre uma copia descartavel auditada, recusa
sobrescrita e usa instancias que o Premiere ja abriu e salvou como doadoras.
Ele nao aprova o preset: a saida ainda precisa de round-trip e export real.
"""

from __future__ import annotations

import copy
import gzip
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from auditar_sem_enhance_essential_20 import (
    DEFAULT_DENOISE_AMOUNT,
    DENOISE_MATCH,
    EXPECTED_IDS,
    HARD_LIMITER_MATCH,
    MANIFEST_SHA256,
    PARAMETRIC_EQ_MATCH,
    SEQUENCE_NAME,
    component_inner_id,
    component_match,
    indexed_refs,
    load_manifest,
    load_project,
    validate_sources_and_timeline,
)
from criar_copia_107_c1_dynamics import (
    chain_refs,
    component_refs,
    fmt,
    numeric_graph,
    require,
    set_param_scalar,
    sha256,
    top,
    top_hashes,
)
from criar_copia_107_d2_shelf60m12_dynamics import (
    EXPECTED_EQ_PARAM_NAMES,
    clone_effect,
    decode_opaque,
)


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "premiere_audition_native_preset"
PROJECTS = NATIVE / "projetos_descartaveis"
SUITE = NATIVE / "comparacao_ab" / "SEM_ENHANCE_ESSENTIAL_20_20260814"

INPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_EQ_WORKING.prproj"
DONOR = PROJECTS / "Teste_107_SHELF60M12_ONLY_REAL_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER_WORKING.prproj"
MANIFEST = SUITE / "analises" / "MANIFESTO_C1_EQ_LIMITER_MUTACAO.json"

EXPECTED_INPUT_SHA256 = "2d9b97f423cdec834dc2423840164d6d31c847490a3518e12afd2fbe07b31e25"
EXPECTED_DONOR_SHA256 = "86f07a141b73d2140ecac0a4837363fd2461de72b32e781d0f9a321a68c2b994"
EXPECTED_INPUT_MAX_OBJECT_ID = 4840

DONOR_EQ_ID = "705"
DONOR_EQ_PARAM_REFS = [str(value) for value in range(1017, 1063)]
DONOR_LIMITER_ID = "706"
DONOR_LIMITER_PARAM_REFS = [str(value) for value in range(1063, 1071)]

EQ_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_SEM_ENHANCE__EQ_C1_ROBUSTO_v1"
LIMITER_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1"

# Curva congelada para o primeiro render real.
CURVE = {
    "high_pass": {"enabled": True, "frequency_hz": 100.0, "slope_db_oct": 24.0},
    "low_shelf": {"enabled": True, "frequency_hz": 315.0, "gain_db": 2.1, "filter_order": 1.0},
    "bands": [
        {"frequency_hz": 1000.0, "gain_db": -0.7, "q": 1.0},
        {"frequency_hz": 1400.0, "gain_db": -1.6, "q": 1.0},
        {"frequency_hz": 4000.0, "gain_db": -2.2, "q": 0.65},
        {"frequency_hz": 6800.0, "gain_db": -4.0, "q": 0.75},
    ],
    "band_5_enabled": False,
    "high_shelf_enabled": False,
    "low_pass_enabled": False,
    "output_gain_db": 0.0,
    "constant_width": True,
}


def frequency_native(hz: float) -> float:
    require(20.0 <= hz <= 24000.0, f"frequencia fora do controle: {hz}")
    return (hz - 20.0) / 23980.0


def eq_gain_native(db: float) -> float:
    require(-24.0 <= db <= 24.0, f"ganho EQ fora do controle: {db}")
    return 0.5 + db / 96.0


def width_native(width_hz: float) -> float:
    require(0.0 < width_hz <= 10000.0, f"Width fora do controle: {width_hz}")
    return width_hz / 10000.0


def clone_limiter(
    donor_effect: ET.Element,
    donor_params: list[ET.Element],
    effect_id: str,
    param_ids: list[str],
    component_id: str,
    *,
    keep_opaque_text: bool,
) -> tuple[ET.Element, list[ET.Element]]:
    require(len(donor_params) == len(param_ids) == 8, "limiter deveria ter 8 Params")
    effect = copy.deepcopy(donor_effect)
    effect.set("ObjectID", effect_id)
    component = effect.find("./AudioComponent/Component")
    require(component is not None, "Component do limiter ausente")
    inner = component.find("ID")
    require(inner is not None, "ID interno do limiter ausente")
    inner.text = component_id
    instance = component.find("InstanceName")
    require(instance is not None, "InstanceName do limiter ausente")
    instance.text = LIMITER_INSTANCE_NAME
    for node, new_id in zip(component.findall("./Params/Param"), param_ids):
        node.set("ObjectRef", new_id)
    opaque = effect.find("OpaqueData")
    require(opaque is not None and opaque.text and opaque.text.strip(), "OpaqueData limiter doador ausente")
    if not keep_opaque_text:
        opaque.text = None
    params: list[ET.Element] = []
    for donor, new_id in zip(donor_params, param_ids):
        clone = copy.deepcopy(donor)
        clone.set("ObjectID", new_id)
        params.append(clone)
    require(component_refs(effect) == param_ids, "refs clonadas do limiter divergiram")
    return effect, params


def build_eq_values(donor_eq: ET.Element) -> list[float]:
    _, _, opaque_values = decode_opaque(donor_eq)
    require(len(opaque_values) == 46 and abs(float(opaque_values[-1]) - 1.0) < 1e-8, "Opaque EQ doador divergente")
    # Params[0] e bypass externo; Params[1:46] correspondem a Opaque[0:45].
    values = [0.0] + [float(value) for value in opaque_values[:-1]]
    require(len(values) == 46, "vetor EQ deveria ter 46 Params")

    values[1] = frequency_native(CURVE["low_shelf"]["frequency_hz"])
    values[2] = eq_gain_native(CURVE["low_shelf"]["gain_db"])
    values[4] = 0.5  # high shelf neutro/desligado

    for index, band in enumerate(CURVE["bands"]):
        base = 5 + index * 4
        values[base] = frequency_native(band["frequency_hz"])
        # O campo Q permanece serializado, mas Constant Width usa Bandwidth.
        values[base + 2] = eq_gain_native(band["gain_db"])
        values[base + 3] = 1.0
        values[33 + index] = width_native(band["frequency_hz"] / band["q"])

    values[23] = 0.5
    values[24] = 0.0  # band 5 off
    values[25] = 0.66666698455810547  # output 0 dB validado no round-trip
    values[26] = 0.0  # ConstantQ false = Constant Width
    values[27] = CURVE["low_shelf"]["filter_order"]
    values[28] = 0.0
    values[29] = 0.0
    values[30] = 1.0
    values[31] = 0.0
    values[38] = 1.0
    values[39] = frequency_native(CURVE["high_pass"]["frequency_hz"])
    values[40] = CURVE["high_pass"]["slope_db_oct"] / 60.0
    values[41] = 0.0
    values[44] = 0.0
    values[45] = 0.0
    return values


def main() -> int:
    require(INPUT.parent == DONOR.parent == OUTPUT.parent == PROJECTS, "projeto fora de projetos_descartaveis")
    require(INPUT != OUTPUT and DONOR != OUTPUT, "entrada/doador/saida colidem")
    require(not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(not MANIFEST.exists(), f"RECUSA DE OVERWRITE: {MANIFEST}")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "SHA da entrada RN20 divergiu")
    require(sha256(DONOR.read_bytes()) == EXPECTED_DONOR_SHA256, "SHA do doador round-trip divergiu")

    root, objects, before_snapshot, resolved_input = load_project(INPUT)
    manifest_input = load_manifest()
    _, targets = validate_sources_and_timeline(root, objects, manifest_input, SEQUENCE_NAME)
    require([target.clip_id for target in targets] == list(EXPECTED_IDS), "ordem alvo divergiu")

    xml_before = gzip.decompress(INPUT.read_bytes())
    graph_before = numeric_graph(xml_before)
    require(graph_before["max_object_id"] == EXPECTED_INPUT_MAX_OBJECT_ID, "max ObjectID da entrada divergiu")
    before_hashes = top_hashes(root)

    donor_root = ET.fromstring(gzip.decompress(DONOR.read_bytes()))
    donor_eq = top(donor_root, "AudioFilterComponent", DONOR_EQ_ID)
    donor_limiter = top(donor_root, "AudioFilterComponent", DONOR_LIMITER_ID)
    require(component_match(donor_eq) == PARAMETRIC_EQ_MATCH, "doador EQ nao e Parametric Equalizer")
    require(component_match(donor_limiter) == HARD_LIMITER_MATCH, "doador limiter nao e Hard Limiter")
    require(component_refs(donor_eq) == DONOR_EQ_PARAM_REFS, "refs do EQ doador divergiram")
    require(component_refs(donor_limiter) == DONOR_LIMITER_PARAM_REFS, "refs do limiter doador divergiram")
    donor_eq_params = [top(donor_root, "AudioComponentParam", value) for value in DONOR_EQ_PARAM_REFS]
    donor_limiter_params = [top(donor_root, "AudioComponentParam", value) for value in DONOR_LIMITER_PARAM_REFS]
    require([node.findtext("Name") for node in donor_eq_params] == EXPECTED_EQ_PARAM_NAMES, "nomes dos Params EQ divergiram")
    limiter_names = [node.findtext("Name") for node in donor_limiter_params]
    require(limiter_names == ["Bypass", "Maximum Amplitude", "Input Boost", "Look-Ahead Time", "Release Time", "Link Channels", "Decay to Ceiling", "Limit True Peak"], "Params limiter divergiram")
    eq_values = build_eq_values(donor_eq)

    next_id = EXPECTED_INPUT_MAX_OBJECT_ID + 1
    added_object_keys: list[str] = []
    changed_chain_keys: list[str] = []
    per_clip: list[dict[str, object]] = []
    common_eq_hash: str | None = None
    common_limiter_hash: str | None = None

    for position, target in enumerate(targets):
        chain = target.chain
        refs_before = chain_refs(chain)
        require(len(refs_before) == 1, f"{target.clip_id}: chain nao tem somente RN")
        rn = objects[refs_before[0]]
        require(component_match(rn) == DENOISE_MATCH, f"{target.clip_id}: efeito unico nao e RN")
        max_inner = max(int(component_inner_id(objects[value], str(target.clip_id))) for value in refs_before)

        eq_id = str(next_id)
        eq_param_ids = [str(value) for value in range(next_id + 1, next_id + 47)]
        limiter_id = str(next_id + 47)
        limiter_param_ids = [str(value) for value in range(next_id + 48, next_id + 56)]
        next_id += 56

        eq, eq_params, _ = clone_effect(
            donor_eq,
            donor_eq_params,
            eq_id,
            eq_param_ids,
            EQ_INSTANCE_NAME,
            str(max_inner + 1),
            eq_values,
        )
        limiter, limiter_params = clone_limiter(
            donor_limiter,
            donor_limiter_params,
            limiter_id,
            limiter_param_ids,
            str(max_inner + 2),
            keep_opaque_text=position == 0,
        )

        eq_opaque = eq.find("OpaqueData")
        limiter_opaque = limiter.find("OpaqueData")
        require(eq_opaque is not None and limiter_opaque is not None, "OpaqueData clonado ausente")
        if common_eq_hash is None:
            common_eq_hash = eq_opaque.get("BinaryHash")
        else:
            require(eq_opaque.get("BinaryHash") == common_eq_hash, "hash EQ deixou de ser comum")
            eq_opaque.text = None
        if common_limiter_hash is None:
            common_limiter_hash = limiter_opaque.get("BinaryHash")
        else:
            require(limiter_opaque.get("BinaryHash") == common_limiter_hash, "hash limiter deixou de ser comum")

        components = chain.find("./ComponentChain/Components")
        require(components is not None and len(components) == 1, f"{target.clip_id}: Components divergiu")
        existing = list(components)[0]
        existing.set("Index", "0")
        components.append(ET.Element("Component", {"Index": "1", "ObjectRef": eq_id}))
        components.append(ET.Element("Component", {"Index": "2", "ObjectRef": limiter_id}))

        for node in [eq, *eq_params, limiter, *limiter_params]:
            root.append(node)
            identity = node.get("ObjectID")
            require(identity is not None, "objeto adicionado sem ObjectID")
            added_object_keys.append(f"{node.tag}:ObjectID={identity}")
        changed_chain_keys.append(f"AudioComponentChain:ObjectID={chain.get('ObjectID')}")
        per_clip.append({
            "id": target.clip_id,
            "chain_id": chain.get("ObjectID"),
            "rn_id": refs_before[0],
            "eq_id": eq_id,
            "limiter_id": limiter_id,
            "component_inner_ids": [component_inner_id(rn, str(target.clip_id)), str(max_inner + 1), str(max_inner + 2)],
        })

    require(next_id - 1 == EXPECTED_INPUT_MAX_OBJECT_ID + 20 * 56, "alocacao final de IDs divergiu")
    objects_after = {node.get("ObjectID"): node for node in root if node.get("ObjectID")}
    for target, row in zip(targets, per_clip):
        refs = chain_refs(target.chain)
        require(refs == [str(row["rn_id"]), str(row["eq_id"]), str(row["limiter_id"])], f"{target.clip_id}: ordem final divergiu")
        require([component_match(objects_after[value]) for value in refs] == [DENOISE_MATCH, PARAMETRIC_EQ_MATCH, HARD_LIMITER_MATCH], f"{target.clip_id}: tipos finais divergem")

    after_hashes = top_hashes(root)
    removed = sorted(set(before_hashes) - set(after_hashes))
    added = sorted(set(after_hashes) - set(before_hashes))
    changed = sorted(key for key in set(before_hashes) & set(after_hashes) if before_hashes[key] != after_hashes[key])
    require(not removed, f"objetos removidos: {removed}")
    require(added == sorted(added_object_keys), "objetos adicionados fora da whitelist")
    require(changed == sorted(changed_chain_keys), f"objetos alterados fora das 20 chains: {changed}")

    xml_after = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml_after)
    graph_after = numeric_graph(xml_after)
    require(graph_after["duplicate_object_ids"] == graph_before["duplicate_object_ids"], "duplicatas ObjectID mudaram")
    require(graph_after["unresolved_object_refs"] == graph_before["unresolved_object_refs"], "refs nao resolvidas mudaram")
    require(graph_after["object_id_count"] == graph_before["object_id_count"] + 1120, "contagem ObjectID divergiu")
    require(graph_after["object_ref_count"] == graph_before["object_ref_count"] + 1120, "contagem ObjectRef divergiu")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "entrada mudou durante a mutacao")
    require(sha256(DONOR.read_bytes()) == EXPECTED_DONOR_SHA256, "doador mudou durante a mutacao")

    compressed = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed) == xml_after, "round-trip gzip falhou")
    report = {
        "schema": "codex.premiere.safe-prproj-mutation.v3",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "C1_REQUIRES_PREMIERE_ROUNDTRIP_AND_REAL_EXPORT",
        "input": {"path": str(resolved_input), "sha256": EXPECTED_INPUT_SHA256, "modified": False},
        "donor": {"path": str(DONOR), "sha256": EXPECTED_DONOR_SHA256, "modified": False},
        "output": {"path": str(OUTPUT), "sha256": sha256(compressed), "bytes": len(compressed)},
        "preflight_manifest_sha256": MANIFEST_SHA256,
        "sequence": SEQUENCE_NAME,
        "ids": list(EXPECTED_IDS),
        "curve": CURVE,
        "eq_instance_name": EQ_INSTANCE_NAME,
        "limiter_instance_name": LIMITER_INSTANCE_NAME,
        "effect_order": ["Reduce Noise", "Parametric Equalizer C1", "Hard Limiter v1"],
        "per_clip": per_clip,
        "graph": {"before": graph_before, "after": graph_after},
        "whitelist": {"changed": changed, "added_count": len(added), "removed": removed},
    }
    manifest_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=OUTPUT.parent, prefix=".c1-eq-lim-", suffix=".tmp", delete=False) as handle:
        tmp_project = Path(handle.name)
        handle.write(compressed)
    with tempfile.NamedTemporaryFile(dir=MANIFEST.parent, prefix=".c1-eq-lim-manifest-", suffix=".tmp", delete=False) as handle:
        tmp_manifest = Path(handle.name)
        handle.write(manifest_bytes)
    try:
        require(sha256(tmp_project.read_bytes()) == report["output"]["sha256"], "SHA temporario do projeto divergiu")
        json.loads(tmp_manifest.read_text(encoding="utf-8"))
        os.link(tmp_project, OUTPUT)
        os.link(tmp_manifest, MANIFEST)
    finally:
        tmp_project.unlink(missing_ok=True)
        tmp_manifest.unlink(missing_ok=True)

    require(sha256(OUTPUT.read_bytes()) == report["output"]["sha256"], "SHA final do projeto divergiu")
    print(json.dumps({
        "status": report["status"],
        "output": str(OUTPUT),
        "output_sha256": report["output"]["sha256"],
        "manifest": str(MANIFEST),
        "manifest_sha256": sha256(MANIFEST.read_bytes()),
        "changed_chains": len(changed),
        "added_objects": len(added),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise
