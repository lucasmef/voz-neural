#!/usr/bin/env python3
"""Cria copia RN20+C1 com Hard Limiter Input Boost +9.20 dB.

Somente 20 AudioFilterComponent do limiter (OpaqueData) e seus 20 Params
Input Boost podem mudar. Nao abre, decodifica ou hasheia arquivos de audio.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import math
import re
import struct
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import auditar_roundtrip_rn20_c1_eq_limiter as audit
import criar_copia_clean_sem_enhance_20 as safe
import mutar_rn20_eq_limiter_20 as mutation
from criar_copia_107_c1_dynamics import fmt, set_param_scalar


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "premiere_audition_native_preset"
PROJECTS = (NATIVE / "projetos_descartaveis").resolve()
TRIAL = NATIVE / "comparacao_ab/SEM_ENHANCE_ESSENTIAL_20_20260814"
INPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER92_WORKING.prproj"
MANIFEST = TRIAL / "analises/MUTACAO_RN20_C1_LIMITER92_MANIFEST.json"
PREFLIGHT = NATIVE / "comparacao_ab/FLUXO_APROVADO_20_20260814/manifest_preflight.json"

EXPECTED_INPUT_SHA256 = "af3f5c28507c8fbb336208d3bd3bcbdd6ef258a0fea31809052c76ebd1fffc1c"
EXPECTED_PREFLIGHT_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"
OLD_BOOST_DB = 8.10
NEW_BOOST_DB = 9.20
OLD_BOOST_NATIVE = 0.72066664695739746
NEW_BOOST_NATIVE = 0.7279999852180481
DEFAULT_BOOST_NATIVE = 0.66666668653488159
LEGACY_78_NATIVE = 0.718666672706604
LIMITER_MATCH = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
EXPECTED_LIMITER_VALUES = [0.99699997901916504, OLD_BOOST_NATIVE, 0.0, 1.0, 1.0, 1.0, 0.0, 2.0]


class GuardError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def boost_native(db: float) -> float:
    return f32((2.0 / 3.0) + db / 150.0)


def prove_scale(root: ET.Element) -> dict[str, object]:
    require(boost_native(0.0) == DEFAULT_BOOST_NATIVE, "escala nao reproduz default 0 dB")
    require(boost_native(7.8) == LEGACY_78_NATIVE, "escala nao reproduz referencia +7.8 dB")
    require(boost_native(OLD_BOOST_DB) == OLD_BOOST_NATIVE, "escala nao reproduz limiter v1 +8.10 dB")
    require(boost_native(NEW_BOOST_DB) == NEW_BOOST_NATIVE, "escala nao materializa +9.20 dB esperado")

    values = [
        float(node.findtext("CurrentValue"))
        for node in root.findall("AudioComponentParam")
        if node.findtext("Name") == "Input Boost" and node.findtext("CurrentValue") is not None
    ]
    require(any(value == DEFAULT_BOOST_NATIVE for value in values), "referencia local default 0 dB ausente")
    require(any(value == OLD_BOOST_NATIVE for value in values), "referencia local v1 +8.10 dB ausente")
    return {
        "formula": "float32(2/3 + dB/150)",
        "reference_pairs": [
            {"db": 0.0, "native": DEFAULT_BOOST_NATIVE, "present_in_input": True},
            {"db": 7.8, "native": LEGACY_78_NATIVE, "historical_float32_reference": True},
            {"db": OLD_BOOST_DB, "native": OLD_BOOST_NATIVE, "present_in_active_limiter_v1": True},
        ],
        "target": {"db": NEW_BOOST_DB, "native": NEW_BOOST_NATIVE},
    }


def replace_float(decoded: str, index: int, value: float) -> str:
    values = re.findall(r"<float>([^<]+)</float>", decoded)
    require(len(values) == 8, f"ParamList limiter possui {len(values)} floats")
    require(index == 1, "somente indice 1/Input Boost e autorizado")
    updated = list(values)
    updated[index] = fmt(value)
    iterator = iter(updated)
    result, count = re.subn(
        r"<float>[^<]+</float>",
        lambda _: f"<float>{next(iterator)}</float>",
        decoded,
    )
    require(count == 8, "reescrita do ParamList limiter incompleta")
    before_numeric = [float(item) for item in values]
    after_numeric = [float(item) for item in updated]
    require(
        all(
            math.isclose(before, after, rel_tol=0.0, abs_tol=0.0)
            for position, (before, after) in enumerate(zip(before_numeric, after_numeric))
            if position != 1
        ),
        "outro valor Opaque seria alterado",
    )
    require(math.isclose(after_numeric[1], NEW_BOOST_NATIVE, rel_tol=0.0, abs_tol=5e-13), "Input Boost Opaque alvo divergiu")
    return result


def component_refs(chain: ET.Element) -> list[str]:
    return [node.get("ObjectRef") or "" for node in safe.indexed_components(chain)]


def load() -> tuple[bytes, str, ET.Element, dict[str, ET.Element], dict[str, object], list[dict[str, object]]]:
    require(INPUT.resolve().parent == OUTPUT.resolve().parent == PROJECTS, "entrada/saida fora de projetos_descartaveis")
    require(INPUT != OUTPUT and not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(MANIFEST.resolve().parent == (TRIAL / "analises").resolve() and not MANIFEST.exists(), f"manifesto invalido/ocupado: {MANIFEST}")
    require(safe.sha256_file(INPUT) == EXPECTED_INPUT_SHA256, "SHA do projeto pos-save divergiu")
    require(safe.sha256_file(PREFLIGHT) == EXPECTED_PREFLIGHT_SHA256, "SHA do preflight divergiu")
    raw = INPUT.read_bytes()
    require(raw.startswith(b"\x1f\x8b"), "assinatura gzip ausente")
    xml = gzip.decompress(raw).decode("utf-8")
    root = ET.fromstring(xml)
    require(root.tag == "PremiereData", "raiz XML inesperada")
    objects = safe.object_map(root)
    preflight = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    require(preflight.get("preflight_status") == "PASS_20_OF_20", "preflight nao esta PASS")
    _, rows = audit.discover_targets(root, objects, preflight)
    require(len(rows) == 20, f"targets={len(rows)}")
    return raw, xml, root, objects, preflight, rows


def main() -> int:
    input_raw, xml_before, root_before, objects_before, preflight, rows_before = load()
    scale_proof = prove_scale(root_before)
    payloads_before = audit.opaque_payloads(root_before)
    top_before = safe.top_hashes(root_before)
    refs_before = safe.ref_multiset(root_before)
    graph_before = safe.numeric_graph(root_before)

    target_specs: list[dict[str, object]] = []
    old_decoded: str | None = None
    limiter_nodes: list[ET.Element] = []
    boost_nodes: list[ET.Element] = []
    chain_signatures: dict[int, list[str]] = {}
    identity_before: list[tuple[object, ...]] = []

    for row in rows_before:
        clip_id = int(row["id"])
        chain = row["chain"]
        refs = component_refs(chain)
        require(len(refs) == 3, f"{clip_id}: chain nao possui 3 componentes")
        matches = [safe.component_match(objects_before[value]) for value in refs]
        require(matches == [mutation.RN_MATCH, mutation.EQ_MATCH, LIMITER_MATCH], f"{clip_id}: ordem da chain divergiu")
        limiter = objects_before[refs[2]]
        params = audit.component_parameters(limiter, objects_before)
        require([node.findtext("Name") for node in params] == audit.LIMITER_PARAM_NAMES, f"{clip_id}: Params limiter divergiram")
        boost = params[2]
        current = audit.parameter_value(boost)
        require(current == OLD_BOOST_NATIVE, f"{clip_id}: Input Boost nao e +8.10 dB")
        _binary_hash, values, _sha = audit.opaque_values(limiter, payloads_before, 8)
        require(all(math.isclose(a, b, rel_tol=0.0, abs_tol=5e-9) for a, b in zip(values, EXPECTED_LIMITER_VALUES)), f"{clip_id}: limiter v1 divergiu")
        opaque = limiter.find("OpaqueData")
        require(opaque is not None and opaque.get("Encoding") == "base64", f"{clip_id}: OpaqueData invalido")
        decoded = payloads_before[str(opaque.get("BinaryHash"))]
        if old_decoded is None:
            old_decoded = decoded
        else:
            require(decoded == old_decoded, "payload limiter v1 nao uniforme")
        limiter_nodes.append(limiter)
        boost_nodes.append(boost)
        chain_signatures[clip_id] = refs
        identity_before.append((clip_id, row["start"], row["end"], row["frames"], row["media_path"], row["subclip"].findtext("Name")))
        target_specs.append({
            "id": clip_id,
            "chain_object_id": chain.get("ObjectID"),
            "limiter_object_id": limiter.get("ObjectID"),
            "input_boost_param_object_id": boost.get("ObjectID"),
        })

    require(old_decoded is not None and len({node.get("ObjectID") for node in limiter_nodes}) == 20, "20 limiters independentes ausentes")
    require(len({node.get("ObjectID") for node in boost_nodes}) == 20, "20 Params Input Boost independentes ausentes")
    decoded_after = replace_float(old_decoded, 1, NEW_BOOST_NATIVE)
    new_decoded_sha = safe.sha256_bytes(decoded_after.encode("utf-8"))
    new_binary_hash = str(uuid.uuid5(uuid.NAMESPACE_URL, new_decoded_sha))
    require(not any(node.get("BinaryHash") == new_binary_hash for node in root_before.iter("OpaqueData")), "novo BinaryHash colide com payload existente")

    root_work = copy.deepcopy(root_before)
    objects_work = safe.object_map(root_work)
    changed_nodes: list[ET.Element] = []
    for position, spec in enumerate(target_specs):
        limiter = objects_work[str(spec["limiter_object_id"])]
        boost = objects_work[str(spec["input_boost_param_object_id"])]
        opaque = limiter.find("OpaqueData")
        require(opaque is not None, f"{spec['id']}: Opaque ausente")
        opaque.set("BinaryHash", new_binary_hash)
        opaque.text = base64.b64encode(decoded_after.encode("utf-8")).decode("ascii") if position == 0 else None
        set_param_scalar(boost, NEW_BOOST_NATIVE)
        changed_nodes.extend([limiter, boost])

    require(len(changed_nodes) == 40 and len({safe.label(node) for node in changed_nodes}) == 40, "whitelist de 40 objetos invalida")
    xml_after, block_digests = safe.surgical_replace(xml_before, changed_nodes)
    xml_after_bytes = xml_after.encode("utf-8")
    root_after = ET.fromstring(xml_after_bytes)
    objects_after = safe.object_map(root_after)
    _, rows_after = audit.discover_targets(root_after, objects_after, preflight)
    identity_after = [
        (int(row["id"]), row["start"], row["end"], row["frames"], row["media_path"], row["subclip"].findtext("Name"))
        for row in rows_after
    ]
    require(identity_after == identity_before, "fontes/timing/subclipes mudaram")

    payloads_after = audit.opaque_payloads(root_after)
    require(new_binary_hash in payloads_after and payloads_after[new_binary_hash] == decoded_after, "novo payload nao resolvivel")
    materialized_active = 0
    for row in rows_after:
        clip_id = int(row["id"])
        refs = component_refs(row["chain"])
        require(refs == chain_signatures[clip_id], f"{clip_id}: refs/ordem da chain mudaram")
        limiter = objects_after[refs[2]]
        params = audit.component_parameters(limiter, objects_after)
        boost = params[2]
        require(math.isclose(float(audit.parameter_value(boost)), NEW_BOOST_NATIVE, rel_tol=0.0, abs_tol=5e-13), f"{clip_id}: Param Input Boost nao e +9.20")
        binary_hash, values, decoded_sha = audit.opaque_values(limiter, payloads_after, 8)
        require(binary_hash == new_binary_hash and decoded_sha == new_decoded_sha, f"{clip_id}: payload/hash novo divergiu")
        require(math.isclose(values[1], NEW_BOOST_NATIVE, rel_tol=0.0, abs_tol=5e-9), f"{clip_id}: Input Boost Opaque divergiu")
        require(all(math.isclose(values[i], EXPECTED_LIMITER_VALUES[i], rel_tol=0.0, abs_tol=1e-9) for i in range(8) if i != 1), f"{clip_id}: outro parametro Opaque mudou")
        materialized_active += int(bool((limiter.findtext("OpaqueData") or "").strip()))
    require(materialized_active == 1, f"payloads materializados ativos={materialized_active}; esperado 1")

    top_after = safe.top_hashes(root_after)
    require(top_before.keys() == top_after.keys(), "conjunto top-level mudou")
    actual_changed = sorted(key for key in top_before if top_before[key] != top_after[key])
    expected_changed = sorted(safe.label(node) for node in changed_nodes)
    require(actual_changed == expected_changed, f"objetos fora da whitelist mudaram: {actual_changed}")
    require(safe.ref_multiset(root_after) == refs_before, "grafo de referencias mudou")
    graph_after = safe.numeric_graph(root_after)
    require(graph_after == graph_before, "contagem/duplicatas/refs nao resolvidas mudaram")

    compressed = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed) == xml_after_bytes, "round-trip gzip falhou")
    require(safe.sha256_file(INPUT) == EXPECTED_INPUT_SHA256, "input mudou durante a mutacao")
    output_sha = safe.sha256_bytes(compressed)
    report = {
        "schema": "codex.premiere.safe-prproj-mutation.rn20-c1-limiter92.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "LIMITER92_OFFLINE_PASS_REQUIRES_PREMIERE_ROUNDTRIP_AND_REAL_EXPORT",
        "input": {"path": str(INPUT), "sha256_before": EXPECTED_INPUT_SHA256, "sha256_after": safe.sha256_file(INPUT), "modified": False},
        "output": {"path": str(OUTPUT), "sha256": output_sha, "bytes": len(compressed)},
        "preflight": {"path": str(PREFLIGHT), "sha256": EXPECTED_PREFLIGHT_SHA256},
        "scale_proof": scale_proof,
        "mutation": {
            "only_semantic_change": "Hard Limiter Input Boost +8.10 dB -> +9.20 dB",
            "old_native": OLD_BOOST_NATIVE,
            "new_native": NEW_BOOST_NATIVE,
            "maximum_amplitude_dbfs": -0.30,
            "lookahead_ms": 5.0,
            "release_ms": 200.0,
            "link_channels": True,
            "decay_to_ceiling": True,
            "limit_true_peak": False,
            "new_binary_hash": new_binary_hash,
            "old_opaque_decoded_sha256": safe.sha256_bytes(old_decoded.encode("utf-8")),
            "new_opaque_decoded_sha256": new_decoded_sha,
            "opaque_and_component_params_synchronized": True,
        },
        "scope": {
            "clips": 20,
            "ids": list(mutation.EXPECTED_IDS),
            "total_frames": mutation.EXPECTED_FRAMES,
            "audio_files_opened_decoded_or_hashed": False,
            "changed_top_level_objects": actual_changed,
            "changed_top_level_object_count": len(actual_changed),
            "added_top_level_objects": [],
            "removed_top_level_objects": [],
            "all_other_top_level_objects_unchanged": True,
            "sources_timing_subclips_unchanged": True,
            "rn_eq_automatch_unchanged": True,
            "chain_refs_and_order_unchanged": True,
            "per_clip": target_specs,
        },
        "integrity": {"gzip": True, "xml": True, "reference_multiset_unchanged": True, "graph_before": graph_before, "graph_after": graph_after, "block_digests": block_digests},
        "next_step": "Abrir/resalvar no Premiere, auditar o round-trip e exportar WAV real antes de aprovar o ganho +9.20 dB.",
    }
    safe.atomic_create(OUTPUT, compressed)
    require(safe.sha256_file(OUTPUT) == output_sha and gzip.decompress(OUTPUT.read_bytes()) == xml_after_bytes, "readback do output falhou")
    require(safe.sha256_file(INPUT) == EXPECTED_INPUT_SHA256, "input mudou apos escrita")
    safe.atomic_create(MANIFEST, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    require(json.loads(MANIFEST.read_text(encoding="utf-8"))["output"]["sha256"] == output_sha, "manifesto gravado invalido")
    print(json.dumps({
        "status": report["status"],
        "output": report["output"],
        "manifest": str(MANIFEST),
        "changed_objects": len(actual_changed),
        "clips": 20,
        "old_boost_db": OLD_BOOST_DB,
        "new_boost_db": NEW_BOOST_DB,
        "new_boost_native": NEW_BOOST_NATIVE,
        "audio_files_opened": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, audit.AuditError, safe.GuardError, OSError, ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as error:
        print(f"ERRO DE TRAVA: {error}", file=sys.stderr)
        raise SystemExit(2)
