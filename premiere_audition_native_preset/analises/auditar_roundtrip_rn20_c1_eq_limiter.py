#!/usr/bin/env python3
"""Auditoria fail-closed e somente leitura da candidata RN20 + EQ C1 + limiter.

O auditor abre apenas os projetos/manifestos XML. Nenhum arquivo de midia e
aberto, decodificado ou hasheado. A saida e JSON em stdout e o projeto alvo e
re-hasheado ao final para provar que permaneceu inalterado.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import auditar_sem_enhance_essential_20 as essential
import criar_copia_clean_sem_enhance_20 as safe
import mutar_rn20_eq_limiter_20 as mutation


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "premiere_audition_native_preset"
PROJECTS = NATIVE / "projetos_descartaveis"
TRIAL = NATIVE / "comparacao_ab" / "SEM_ENHANCE_ESSENTIAL_20_20260814"
PREFLIGHT = NATIVE / "comparacao_ab" / "FLUXO_APROVADO_20_20260814" / "manifest_preflight.json"

PROJECT = PROJECTS / "Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER_WORKING.prproj"
BASE = PROJECTS / "Teste_SEM_ENHANCE_RN20_EQ_WORKING.prproj"
DONOR = PROJECTS / "Teste_107_SHELF60M12_ONLY_REAL_WORKING.prproj"
MUTATION_MANIFEST = TRIAL / "analises" / "MUTACAO_RN20_C1_EQ_LIMITER_MANIFEST.json"

EXPECTED_PROJECT_SHA256 = "af3f5c28507c8fbb336208d3bd3bcbdd6ef258a0fea31809052c76ebd1fffc1c"
EXPECTED_BASE_SHA256 = "2d9b97f423cdec834dc2423840164d6d31c847490a3518e12afd2fbe07b31e25"
EXPECTED_DONOR_SHA256 = "86f07a141b73d2140ecac0a4837363fd2461de72b32e781d0f9a321a68c2b994"
EXPECTED_PREFLIGHT_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"

EQ_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_SEM_ENHANCE__EQ_C1_ROBUSTO_v1"
LIMITER_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1"
LIMITER_PARAM_NAMES = [
    "Bypass",
    "Maximum Amplitude",
    "Input Boost",
    "Look-Ahead Time",
    "Release Time",
    "Link Channels",
    "Decay to Ceiling",
    "Limit True Peak",
]


class AuditError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AuditError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_project(path: Path) -> tuple[ET.Element, dict[str, ET.Element]]:
    payload = path.read_bytes()
    require(payload.startswith(b"\x1f\x8b"), f"{path.name}: assinatura gzip ausente")
    root = ET.fromstring(gzip.decompress(payload))
    require(root.tag == "PremiereData", f"{path.name}: raiz XML divergente")
    return root, safe.object_map(root)


def parameter_value(parameter: ET.Element) -> float | None:
    require(parameter.findtext("IsTimeVarying") == "false", f"{parameter.findtext('Name')}: parametro variavel")
    require(parameter.find("Keyframes") is None, f"{parameter.findtext('Name')}: possui keyframes")
    current_text = parameter.findtext("CurrentValue")
    start_text = parameter.findtext("StartKeyframe")
    current = float(current_text) if current_text is not None else None
    start = float(start_text.split(",")[1]) if start_text else None
    if current is not None and start is not None:
        require(math.isclose(current, start, rel_tol=0.0, abs_tol=5.0e-10), f"{parameter.findtext('Name')}: CurrentValue != StartKeyframe")
    return current if current is not None else start


def component_parameters(component: ET.Element, objects: dict[str, ET.Element]) -> list[ET.Element]:
    entries = component.findall("./AudioComponent/Component/Params/Param")
    indices = [int(entry.get("Index", position)) for position, entry in enumerate(entries)]
    require(indices == list(range(len(entries))), f"indices de parametros divergentes: {indices}")
    result: list[ET.Element] = []
    for entry in entries:
        ref = entry.get("ObjectRef")
        require(ref in objects, f"referencia de parametro orfa: {ref}")
        parameter = objects[ref]
        require(parameter.tag == "AudioComponentParam", f"parametro aponta para {parameter.tag}")
        result.append(parameter)
    return result


def opaque_payloads(root: ET.Element) -> dict[str, str]:
    """Materializa somente payloads XML dos dois efeitos sob auditoria."""
    result: dict[str, str] = {}
    for component in root:
        if component.tag != "AudioFilterComponent":
            continue
        if safe.component_match(component) not in {essential.PARAMETRIC_EQ_MATCH, essential.HARD_LIMITER_MATCH}:
            continue
        opaque = component.find("OpaqueData")
        if opaque is None or not opaque.get("BinaryHash") or not (opaque.text or "").strip():
            continue
        decoded = base64.b64decode("".join((opaque.text or "").split())).decode("utf-8")
        binary_hash = str(opaque.get("BinaryHash"))
        require(binary_hash not in result or result[binary_hash] == decoded, f"colisao de BinaryHash: {binary_hash}")
        result[binary_hash] = decoded
    return result


def opaque_values(
    component: ET.Element,
    payloads: dict[str, str],
    expected_count: int,
) -> tuple[str, list[float], str]:
    opaque = component.find("OpaqueData")
    require(opaque is not None and opaque.get("Encoding") == "base64", "OpaqueData ausente ou sem base64")
    binary_hash = str(opaque.get("BinaryHash") or "")
    require(binary_hash in payloads, f"payload compartilhado nao resolvido: {binary_hash}")
    decoded = payloads[binary_hash]
    values = [float(value) for value in re.findall(r"<float>([^<]+)</float>", decoded)]
    require(len(values) == expected_count, f"OpaqueData possui {len(values)} floats; esperado {expected_count}")
    return binary_hash, values, hashlib.sha256(decoded.encode("utf-8")).hexdigest()


def discover_targets(
    root: ET.Element,
    objects: dict[str, ET.Element],
    preflight: dict[str, object],
) -> tuple[ET.Element, list[dict[str, object]]]:
    sequence, items = safe.discover_items(root, objects)
    rows: list[dict[str, object]] = []
    total_frames = 0
    for position, (expected_id, item) in enumerate(zip(essential.EXPECTED_IDS, items), 1):
        clip_id, subclip, clip, chain = safe.identify_item(item, objects)
        require(clip_id == expected_id, f"ordem {position}: ID {clip_id}; esperado {expected_id}")
        start = int(item.findtext("./ClipTrackItem/TrackItem/Start") or "0")
        end = int(item.findtext("./ClipTrackItem/TrackItem/End") or "-1")
        require(end > start and (end - start) % essential.TICKS_PER_SAMPLE == 0, f"{clip_id}: intervalo invalido")
        frames = (end - start) // essential.TICKS_PER_SAMPLE
        raw = preflight["raws"][str(clip_id)]
        require(frames == int(raw["frames"]), f"{clip_id}: frames divergentes")
        require(int(clip.findtext("./Clip/InPoint") or "0") == 0, f"{clip_id}: InPoint divergente")
        require(int(clip.findtext("./Clip/OutPoint") or "-1") == end - start, f"{clip_id}: OutPoint divergente")

        source_ref = clip.find("./Clip/Source")
        source = essential.resolve(objects, source_ref.get("ObjectRef") if source_ref is not None else None, f"{clip_id} fonte")
        require(source.tag == "AudioMediaSource", f"{clip_id}: fonte e {source.tag}")
        media_ref = source.find("./MediaSource/Media")
        media = essential.resolve(objects, media_ref.get("ObjectURef") if media_ref is not None else None, f"{clip_id} media")
        media_path = media.findtext("ActualMediaFilePath") or media.findtext("FilePath") or ""
        expected_path = str((essential.SUITE / str(raw["path"])).resolve())
        require(str(Path(media_path).resolve()) == expected_path, f"{clip_id}: caminho da fonte divergente")

        total_frames += frames
        rows.append(
            {
                "id": clip_id,
                "item": item,
                "subclip": subclip,
                "clip": clip,
                "chain": chain,
                "start": start,
                "end": end,
                "frames": frames,
                "media_path": media_path,
            }
        )
    require(total_frames == essential.EXPECTED_TOTAL_FRAMES, f"total de frames={total_frames}")
    essential.audit_track_and_master_state(sequence, objects, {str(row["item"].get("ObjectID") or "") for row in rows})
    return sequence, rows


def main() -> int:
    require(sha256_file(PROJECT) == EXPECTED_PROJECT_SHA256, "SHA do projeto pos-save divergiu")
    require(sha256_file(BASE) == EXPECTED_BASE_SHA256, "SHA da base confiavel divergiu")
    require(sha256_file(DONOR) == EXPECTED_DONOR_SHA256, "SHA do doador limiter v1 divergiu")
    require(sha256_file(PREFLIGHT) == EXPECTED_PREFLIGHT_SHA256, "SHA do preflight divergiu")

    preflight = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    mutation_manifest = json.loads(MUTATION_MANIFEST.read_text(encoding="utf-8"))
    expected_eq = [float(value) for value in mutation_manifest["eq_native_values"]]
    require(len(expected_eq) == 46, "vetor esperado do EQ nao possui 46 Params")

    root, objects = load_project(PROJECT)
    base_root, base_objects = load_project(BASE)
    donor_root, donor_objects = load_project(DONOR)
    _, rows = discover_targets(root, objects, preflight)
    _, base_rows = discover_targets(base_root, base_objects, preflight)

    for current, baseline in zip(rows, base_rows):
        clip_id = current["id"]
        current_identity = (current["start"], current["end"], current["frames"], current["media_path"])
        baseline_identity = (baseline["start"], baseline["end"], baseline["frames"], baseline["media_path"])
        require(current_identity == baseline_identity, f"{clip_id}: fonte/timing divergiu da base")
        require(current["subclip"].findtext("Name") == baseline["subclip"].findtext("Name"), f"{clip_id}: nome do subclipe divergiu")

    payloads = opaque_payloads(root)
    donor_payloads = opaque_payloads(donor_root)
    donor_limiter_hits = [
        node for node in donor_root
        if node.tag == "AudioFilterComponent" and node.get("ObjectID") == mutation.LIMITER_DONOR_ID
    ]
    require(len(donor_limiter_hits) == 1, "limiter doador ObjectID=706 ausente/duplicado")
    donor_limiter = donor_limiter_hits[0]
    donor_hash, donor_limiter_values, donor_limiter_sha = opaque_values(donor_limiter, donor_payloads, 8)
    donor_limiter_params = component_parameters(donor_limiter, donor_objects)
    require([node.findtext("Name") for node in donor_limiter_params] == LIMITER_PARAM_NAMES, "nomes dos Params do limiter doador divergiram")

    content_states: set[str] = set()
    audit_rows: list[dict[str, object]] = []
    eq_hashes: set[str] = set()
    eq_payload_shas: set[str] = set()
    limiter_hashes: set[str] = set()
    limiter_payload_shas: set[str] = set()
    gain_values: list[float] = []
    analyzed_lufs: list[float] = []

    # O Premiere omite CurrentValue/StartKeyframe quando o valor coincide com
    # estes defaults. O OpaqueData continua contendo os 46 valores explicitos.
    eq_defaults = {0: 0.0, 8: 1.0, 12: 1.0, 16: 1.0, 20: 1.0, 27: 1.0, 30: 1.0, 37: 1.0, 38: 1.0}

    for row in rows:
        clip_id = int(row["id"])
        item = row["item"]
        subclip = row["subclip"]
        clip = row["clip"]
        chain = row["chain"]
        properties = essential.properties(item.find("./ClipTrackItem/TrackItem/Node/Properties"), f"{clip_id} Properties")
        require(properties.get("ESP.AdjustmentsModeID") == "dialog", f"{clip_id}: AdjustmentsModeID")
        require(properties.get("ESP.Tag") == "dialog", f"{clip_id}: ESP.Tag")
        require(properties.get("ESP.ClipMixTypeID") == "1", f"{clip_id}: ClipMixTypeID")
        essential.validate_inactive_metadata(properties, str(clip_id))
        for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem"):
            require(item.find(tag) is None, f"{clip_id}: {tag} presente")

        refs = [entry.get("ObjectRef") for entry in safe.indexed_components(chain)]
        require(all(ref in objects for ref in refs), f"{clip_id}: referencia orfa na cadeia")
        components = [objects[str(ref)] for ref in refs]
        matches = [safe.component_match(component) for component in components]
        require(
            matches == [essential.DENOISE_MATCH, essential.PARAMETRIC_EQ_MATCH, essential.HARD_LIMITER_MATCH],
            f"{clip_id}: ordem/tipos da cadeia divergentes: {matches}",
        )
        denoise, equalizer, limiter = components

        denoise_values = essential.audit_denoise(
            denoise,
            objects,
            essential.DEFAULT_DENOISE_AMOUNT,
            essential.DEFAULT_AMOUNT_TOLERANCE,
            f"{clip_id} RN20",
        )
        require(
            properties.get("ESP.RestorationSection.ReduceNoise.ComponentID") == essential.component_inner_id(denoise, f"{clip_id} RN20"),
            f"{clip_id}: linkage do Reduce Noise divergente",
        )
        require(essential.property_is_enabled(properties, "ESP.RestorationSection.1"), f"{clip_id}: Reduce Noise desligado")
        ui_noise = float(properties["ESP.RestorationSection.2"])
        require(math.isclose(ui_noise, denoise_values["Amount"] * 10.0, abs_tol=1.0e-5), f"{clip_id}: UI/Amount do RN divergem")
        require("ESP.RestorationSection.ReduceSibilance.ComponentID" not in properties, f"{clip_id}: linkage DeEss presente")
        require(not essential.property_is_enabled(properties, "ESP.RestorationSection.5"), f"{clip_id}: DeEss habilitado")

        essential.audit_master_clip(subclip, objects, str(clip_id))
        target = essential.Target(
            clip_id,
            item,
            subclip,
            clip,
            chain,
            int(row["start"]),
            int(row["end"]),
            int(row["frames"]),
            Path(str(row["media_path"])),
            {},
        )
        automatch = essential.audit_automatch(target, properties, "required", content_states)
        gain_values.append(float(automatch["gain_db"]))
        analyzed_lufs.append(float(automatch["analysis_lufs"]))

        require(equalizer.tag == "AudioFilterComponent", f"{clip_id}: EQ nao e AudioFilterComponent")
        essential.audit_stereo_component(equalizer, f"{clip_id} EQ")
        require(equalizer.findtext("./AudioComponent/Component/InstanceName") == EQ_INSTANCE_NAME, f"{clip_id}: InstanceName do EQ")
        # Ao salvar um Parametric Equalizer personalizado, o Premiere
        # canonicaliza FilterPreset de 0 para 4 (Custom); o doador validado faz
        # o mesmo round-trip. Os valores efetivos seguem auditados abaixo.
        require(equalizer.findtext("FilterPreset") == "4" and equalizer.findtext("FilterIndex") == "-1", f"{clip_id}: FilterPreset/Index do EQ")
        eq_params = component_parameters(equalizer, objects)
        eq_names = [node.findtext("Name") for node in eq_params]
        require(eq_names == mutation.EQ_PARAM_NAMES, f"{clip_id}: nomes dos Params do EQ divergiram")
        for index, (parameter, expected) in enumerate(zip(eq_params, expected_eq)):
            actual = parameter_value(parameter)
            if actual is None:
                require(index in eq_defaults and math.isclose(expected, eq_defaults[index], abs_tol=1.0e-12), f"{clip_id}: {eq_names[index]} omitido fora do default")
                actual = eq_defaults[index]
            require(math.isclose(actual, expected, rel_tol=0.0, abs_tol=8.0e-7), f"{clip_id}: {eq_names[index]}={actual}; esperado {expected}")
        eq_hash, eq_opaque, eq_sha = opaque_values(equalizer, payloads, 46)
        require(all(math.isclose(actual, expected, rel_tol=0.0, abs_tol=1.0e-9) for actual, expected in zip(eq_opaque[:45], expected_eq[1:])), f"{clip_id}: payload EQ divergiu")
        require(math.isclose(eq_opaque[45], 1.0, abs_tol=1.0e-12), f"{clip_id}: sentinela do EQ divergiu")
        eq_hashes.add(eq_hash)
        eq_payload_shas.add(eq_sha)

        require(limiter.tag == "AudioFilterComponent", f"{clip_id}: limiter nao e AudioFilterComponent")
        essential.audit_stereo_component(limiter, f"{clip_id} limiter")
        require(limiter.findtext("./AudioComponent/Component/InstanceName") == LIMITER_INSTANCE_NAME, f"{clip_id}: InstanceName do limiter")
        require(limiter.findtext("FilterPreset") == donor_limiter.findtext("FilterPreset"), f"{clip_id}: FilterPreset do limiter")
        require(limiter.findtext("FilterIndex") == donor_limiter.findtext("FilterIndex"), f"{clip_id}: FilterIndex do limiter")
        limiter_params = component_parameters(limiter, objects)
        require([node.findtext("Name") for node in limiter_params] == LIMITER_PARAM_NAMES, f"{clip_id}: nomes dos Params do limiter")
        for current_parameter, donor_parameter in zip(limiter_params, donor_limiter_params):
            current_value = parameter_value(current_parameter)
            donor_value = parameter_value(donor_parameter)
            require(
                (current_value is None and donor_value is None)
                or (
                    current_value is not None
                    and donor_value is not None
                    and math.isclose(current_value, donor_value, rel_tol=0.0, abs_tol=5.0e-10)
                ),
                f"{clip_id}: parametro {current_parameter.findtext('Name')} do limiter divergiu do v1",
            )
        limiter_hash, limiter_opaque, limiter_sha = opaque_values(limiter, payloads, 8)
        require(limiter_hash == donor_hash, f"{clip_id}: BinaryHash do limiter divergiu do v1")
        require(limiter_opaque == donor_limiter_values and limiter_sha == donor_limiter_sha, f"{clip_id}: payload do limiter divergiu do v1")
        limiter_hashes.add(limiter_hash)
        limiter_payload_shas.add(limiter_sha)

        audit_rows.append(
            {
                "id": clip_id,
                "frames": row["frames"],
                "rn_amount": denoise_values["Amount"],
                "automatch_lufs": automatch["matched_lufs"],
                "gain_db": automatch["gain_db"],
                "eq_hash": eq_hash,
                "limiter_hash": limiter_hash,
            }
        )

    require(len(audit_rows) == 20, f"clipes auditados={len(audit_rows)}")
    require(len(content_states) == 20, f"ContentStates individuais={len(content_states)}")
    require(len(eq_hashes) == len(eq_payload_shas) == 1, "EQ nao uniforme")
    require(len(limiter_hashes) == len(limiter_payload_shas) == 1, "limiter nao uniforme")
    base_graph = safe.numeric_graph(base_root)
    graph = safe.numeric_graph(root)
    require(not graph["duplicate_object_ids"], f"ObjectIDs duplicados: {graph['duplicate_object_ids']}")
    require(graph["duplicate_object_ids"] == base_graph["duplicate_object_ids"], "duplicatas do grafo mudaram")
    require(graph["unresolved_object_refs"] == base_graph["unresolved_object_refs"], "referencias nao resolvidas mudaram")
    require(graph["object_id_count"] == base_graph["object_id_count"] + 1120, "delta de ObjectIDs divergiu de +1120")
    require(graph["object_ref_count"] == base_graph["object_ref_count"] + 1120, "delta de ObjectRefs divergiu de +1120")
    require(sha256_file(PROJECT) == EXPECTED_PROJECT_SHA256, "projeto mudou durante a auditoria")

    print(
        json.dumps(
            {
                "status": "PASS_20_OF_20_POST_ROUNDTRIP",
                "project": str(PROJECT),
                "project_sha256": EXPECTED_PROJECT_SHA256,
                "project_bytes": PROJECT.stat().st_size,
                "ids": [row["id"] for row in audit_rows],
                "total_frames": sum(int(row["frames"]) for row in audit_rows),
                "source_timing_equal_trusted_base": True,
                "media_files_opened_or_hashed": False,
                "rn_amount_min": min(float(row["rn_amount"]) for row in audit_rows),
                "rn_amount_max": max(float(row["rn_amount"]) for row in audit_rows),
                "automatch_count": len(audit_rows),
                "content_states_unique": len(content_states),
                "matched_lufs_min": min(float(row["automatch_lufs"]) for row in audit_rows),
                "matched_lufs_max": max(float(row["automatch_lufs"]) for row in audit_rows),
                "gain_db_min": min(gain_values),
                "gain_db_max": max(gain_values),
                "chain_order": ["Essential Reduce Noise RN20", "Parametric Equalizer C1", "Hard Limiter v1"],
                "eq_hash": next(iter(eq_hashes)),
                "eq_decoded_sha256": next(iter(eq_payload_shas)),
                "eq_native_values_exact_after_float32": True,
                "limiter_hash": next(iter(limiter_hashes)),
                "limiter_decoded_sha256": next(iter(limiter_payload_shas)),
                "limiter_exact_donor_v1": True,
                "forbidden_target_effects_absent": [
                    "Enhance Speech",
                    "DeEss",
                    "Dynamics/Clarity",
                    "Reduce Rumble",
                    "DeHum",
                    "Reduce Reverb",
                    "track/master processing",
                ],
                "dialogue_metadata_clean": True,
                "per_clip": audit_rows,
                "graph_before": base_graph,
                "graph_after": graph,
                "graph_delta": {"object_ids": 1120, "object_refs": 1120, "unresolved_refs_unchanged": True},
                "project_unchanged": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AuditError, safe.GuardError, essential.GuardError, OSError, ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(2)
