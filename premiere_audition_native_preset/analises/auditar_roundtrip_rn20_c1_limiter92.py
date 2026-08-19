#!/usr/bin/env python3
"""Auditoria read-only do round-trip RN20 + EQ C1 + limiter +9.20 dB.

Le somente XML gzip e manifestos. Arquivos de audio nao sao abertos,
decodificados ou hasheados. O SHA do projeto e conferido antes e depois.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import auditar_roundtrip_rn20_c1_eq_limiter as base_audit
import auditar_sem_enhance_essential_20 as essential
import criar_copia_clean_sem_enhance_20 as safe
import mutar_rn20_c1_limiter92 as limiter92
import mutar_rn20_eq_limiter_20 as mutation


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "premiere_audition_native_preset"
PROJECT = NATIVE / "projetos_descartaveis/Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER92_WORKING.prproj"
BASE = NATIVE / "projetos_descartaveis/Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER_WORKING.prproj"
PREFLIGHT = NATIVE / "comparacao_ab/FLUXO_APROVADO_20_20260814/manifest_preflight.json"
EXPECTED_PROJECT_SHA256 = "55dd0f1e855f2344888311b39e87feefb0d81116620bc8ec410df491354a6de6"
EXPECTED_BASE_SHA256 = "af3f5c28507c8fbb336208d3bd3bcbdd6ef258a0fea31809052c76ebd1fffc1c"
EXPECTED_PREFLIGHT_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"


class AuditError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def load(path: Path) -> tuple[ET.Element, dict[str, ET.Element]]:
    payload = path.read_bytes()
    require(payload.startswith(b"\x1f\x8b"), f"{path.name}: gzip ausente")
    root = ET.fromstring(gzip.decompress(payload))
    require(root.tag == "PremiereData", f"{path.name}: XML invalido")
    return root, safe.object_map(root)


def chain_refs(chain: ET.Element) -> list[str]:
    return [node.get("ObjectRef") or "" for node in safe.indexed_components(chain)]


def param_values(component: ET.Element, objects: dict[str, ET.Element]) -> tuple[list[str], list[float | None]]:
    params = base_audit.component_parameters(component, objects)
    return [node.findtext("Name") or "" for node in params], [base_audit.parameter_value(node) for node in params]


def same_optional(actual: float | None, expected: float | None, tolerance: float = 8e-7) -> bool:
    return (actual is None and expected is None) or (
        actual is not None and expected is not None and math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance)
    )


def main() -> int:
    before_sha = safe.sha256_file(PROJECT)
    require(before_sha == EXPECTED_PROJECT_SHA256, f"SHA pos-save divergiu: {before_sha}")
    require(safe.sha256_file(BASE) == EXPECTED_BASE_SHA256, "SHA da base C1 divergiu")
    require(safe.sha256_file(PREFLIGHT) == EXPECTED_PREFLIGHT_SHA256, "SHA do preflight divergiu")

    preflight = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    root, objects = load(PROJECT)
    base_root, base_objects = load(BASE)
    _, rows = base_audit.discover_targets(root, objects, preflight)
    _, base_rows = base_audit.discover_targets(base_root, base_objects, preflight)
    require(len(rows) == len(base_rows) == 20, "targets != 20")

    payloads = base_audit.opaque_payloads(root)
    base_payloads = base_audit.opaque_payloads(base_root)
    content_states: set[str] = set()
    eq_signatures: set[tuple[str, str]] = set()
    limiter_signatures: set[tuple[str, str]] = set()
    matched_lufs: list[float] = []
    result_rows: list[dict[str, object]] = []

    for row, baseline in zip(rows, base_rows):
        clip_id = int(row["id"])
        require(clip_id == int(baseline["id"]), f"{clip_id}: ID/base divergiu")
        identity = (row["start"], row["end"], row["frames"], row["media_path"], row["subclip"].findtext("Name"))
        base_identity = (baseline["start"], baseline["end"], baseline["frames"], baseline["media_path"], baseline["subclip"].findtext("Name"))
        require(identity == base_identity, f"{clip_id}: fonte/timing/subclip divergiu")

        item = row["item"]
        props = essential.properties(item.find("./ClipTrackItem/TrackItem/Node/Properties"), f"{clip_id} Properties")
        require(props.get("ESP.AdjustmentsModeID") == "dialog" and props.get("ESP.Tag") == "dialog" and props.get("ESP.ClipMixTypeID") == "1", f"{clip_id}: Dialogue metadata divergiu")
        essential.validate_inactive_metadata(props, str(clip_id))
        require(not any(item.find(tag) is not None for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem")), f"{clip_id}: pre-render presente")

        refs = chain_refs(row["chain"])
        base_refs = chain_refs(baseline["chain"])
        require(refs == base_refs, f"{clip_id}: refs/ordem da chain mudaram")
        components = [objects[value] for value in refs]
        base_components = [base_objects[value] for value in base_refs]
        matches = [safe.component_match(node) for node in components]
        require(matches == [essential.DENOISE_MATCH, essential.PARAMETRIC_EQ_MATCH, essential.HARD_LIMITER_MATCH], f"{clip_id}: chain extra/fora de ordem: {matches}")
        denoise, equalizer, limiter = components
        _base_denoise, base_equalizer, base_limiter = base_components

        denoise_values = essential.audit_denoise(denoise, objects, essential.DEFAULT_DENOISE_AMOUNT, essential.DEFAULT_AMOUNT_TOLERANCE, f"{clip_id} RN20")
        require(props.get("ESP.RestorationSection.ReduceNoise.ComponentID") == essential.component_inner_id(denoise, f"{clip_id} RN20"), f"{clip_id}: linkage RN divergiu")
        require(essential.property_is_enabled(props, "ESP.RestorationSection.1"), f"{clip_id}: RN desligado")
        require("ESP.RestorationSection.ReduceSibilance.ComponentID" not in props and not essential.property_is_enabled(props, "ESP.RestorationSection.5"), f"{clip_id}: DeEss presente")
        essential.audit_master_clip(row["subclip"], objects, str(clip_id))
        target = essential.Target(clip_id, item, row["subclip"], row["clip"], row["chain"], int(row["start"]), int(row["end"]), int(row["frames"]), Path(str(row["media_path"])), {})
        automatch = essential.audit_automatch(target, props, "required", content_states)
        matched_lufs.append(float(automatch["matched_lufs"]))

        essential.audit_stereo_component(equalizer, f"{clip_id} EQ")
        require(equalizer.findtext("./AudioComponent/Component/InstanceName") == base_audit.EQ_INSTANCE_NAME, f"{clip_id}: EQ InstanceName")
        eq_names, eq_params = param_values(equalizer, objects)
        base_eq_names, base_eq_params = param_values(base_equalizer, base_objects)
        require(eq_names == base_eq_names == mutation.EQ_PARAM_NAMES, f"{clip_id}: Params EQ divergiram")
        require(all(same_optional(actual, expected) for actual, expected in zip(eq_params, base_eq_params)), f"{clip_id}: valor EQ mudou")
        eq_hash, eq_opaque, eq_sha = base_audit.opaque_values(equalizer, payloads, 46)
        _base_eq_hash, base_eq_opaque, base_eq_sha = base_audit.opaque_values(base_equalizer, base_payloads, 46)
        require(all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9) for a, b in zip(eq_opaque, base_eq_opaque)), f"{clip_id}: Opaque EQ mudou")
        require(eq_sha == base_eq_sha, f"{clip_id}: SHA payload EQ mudou")
        eq_signatures.add((eq_hash, eq_sha))

        essential.audit_stereo_component(limiter, f"{clip_id} limiter")
        require(limiter.findtext("./AudioComponent/Component/InstanceName") == base_audit.LIMITER_INSTANCE_NAME, f"{clip_id}: limiter InstanceName")
        limiter_names, limiter_params = param_values(limiter, objects)
        base_limiter_names, base_limiter_params = param_values(base_limiter, base_objects)
        require(limiter_names == base_limiter_names == base_audit.LIMITER_PARAM_NAMES, f"{clip_id}: Params limiter divergiram")
        require(math.isclose(float(limiter_params[2]), limiter92.NEW_BOOST_NATIVE, rel_tol=0.0, abs_tol=5e-10), f"{clip_id}: Input Boost Param nao e +9.20")
        require(all(same_optional(limiter_params[index], base_limiter_params[index], 5e-10) for index in range(8) if index != 2), f"{clip_id}: outro Param limiter mudou")
        limiter_hash, limiter_opaque, limiter_sha = base_audit.opaque_values(limiter, payloads, 8)
        _base_lim_hash, base_lim_opaque, _base_lim_sha = base_audit.opaque_values(base_limiter, base_payloads, 8)
        require(math.isclose(limiter_opaque[1], limiter92.NEW_BOOST_NATIVE, rel_tol=0.0, abs_tol=5e-9), f"{clip_id}: Input Boost Opaque nao e +9.20")
        require(all(math.isclose(limiter_opaque[index], base_lim_opaque[index], rel_tol=0.0, abs_tol=5e-9) for index in range(8) if index != 1), f"{clip_id}: teto/timing/outro Opaque limiter mudou")
        limiter_signatures.add((limiter_hash, limiter_sha))
        result_rows.append({"id": clip_id, "rn_amount": denoise_values["Amount"], "automatch_lufs": automatch["matched_lufs"], "eq_payload_sha256": eq_sha, "limiter_payload_sha256": limiter_sha})

    require(len(content_states) == 20, f"ContentStates individuais={len(content_states)}")
    require(len(eq_signatures) == 1, f"EQ signatures={len(eq_signatures)}")
    require(len(limiter_signatures) == 1, f"limiter signatures={len(limiter_signatures)}")
    graph = safe.numeric_graph(root)
    base_graph = safe.numeric_graph(base_root)
    require(graph == base_graph, f"grafo divergiu: {graph} != {base_graph}")
    require(safe.sha256_file(PROJECT) == before_sha, "projeto mudou durante auditoria")

    report = {
        "schema": "codex.premiere.readonly-audit.rn20-c1-limiter92-roundtrip.v1",
        "status": "PASS",
        "project": {"path": str(PROJECT), "sha256": before_sha, "unchanged_during_audit": True},
        "clips": 20,
        "ids": list(mutation.EXPECTED_IDS),
        "total_frames": mutation.EXPECTED_FRAMES,
        "chain": ["Essential Reduce Noise RN20", base_audit.EQ_INSTANCE_NAME, "Hard Limiter Input Boost +9.20 dB"],
        "input_boost": {"db": 9.20, "native": limiter92.NEW_BOOST_NATIVE, "params_and_opaque_synchronized": True},
        "preserved": {"rn20": True, "eq_c1": True, "automatch_individual_20": True, "sources": True, "timing": True, "subclips": True, "track_master_no_extra_effects": True, "chain_no_extra_components": True, "limiter_all_other_parameters": True},
        "uniform": {"eq_signature_count": len(eq_signatures), "limiter_signature_count": len(limiter_signatures)},
        "automatch_result_lufs": {"min": min(matched_lufs), "max": max(matched_lufs)},
        "graph": graph,
        "audio_files_opened_decoded_or_hashed": False,
        "items": result_rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AuditError, base_audit.AuditError, essential.GuardError, safe.GuardError, OSError, ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error), "audio_files_opened_decoded_or_hashed": False}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)
