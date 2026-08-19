#!/usr/bin/env python3
"""Gera dois presets Premiere portateis, estritamente Stereo-only.

O script reutiliza wrappers .prfpset previamente exportados pelo Premiere e
transplanta, na variante stereo, os Params e o OpaqueData materializados no
projeto final pos-save. As variantes mono e 5.1 permanecem nos defaults dos
wrappers. Nenhum arquivo de audio e aberto, decodificado ou hasheado.
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import auditar_roundtrip_rn20_c1_eq_limiter as project_audit
import criar_copia_clean_sem_enhance_20 as safe
import mutar_rn20_eq_limiter_20 as mutation


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "premiere_audition_native_preset"
PRESETS = NATIVE / "presets"
PROJECT = NATIVE / "projetos_descartaveis/Teste_SEM_ENHANCE_RN20_C1_EQ_LIMITER92_WORKING.prproj"
PREFLIGHT = NATIVE / "comparacao_ab/FLUXO_APROVADO_20_20260814/manifest_preflight.json"
REPORT_DIR = NATIVE / "analises/resultados/PRESETS_PORTATEIS_C1_LIMITER92"
MANIFEST = REPORT_DIR / "MANIFESTO_PRESETS_PORTATEIS.json"
REPORT = REPORT_DIR / "AUDITORIA_PRESETS_PORTATEIS.md"

PROJECT_SHA256 = "133f9d19154e209fcbc3b4c248748098c2d000acf4c7b31846b300ca43b1fa89"
PREFLIGHT_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"

EQ_MATCH = mutation.EQ_MATCH
LIMITER_MATCH = mutation.LIMITER_MATCH
EQ_WRAPPER = PRESETS / "DJI_FEMININA_CAPCUT_BASE__EQ_TONAL_v2.prfpset"
LIMITER_WRAPPER = PRESETS / "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1.prfpset"
EQ_WRAPPER_SHA256 = "31ee42a23fb641442eb1948430af6b30603798e965cbc7817787e725c5412cbc"
LIMITER_WRAPPER_SHA256 = "61554b484b172941e22fcda3f1c15eb075eadd99a6b1c40282ea4b461d8c3cfa"

EQ_NAME = "DJI_FEMININA_CAPCUT_DEFINITIVO__EQ_C1_STEREO_v1"
LIMITER_NAME = "DJI_FEMININA_CAPCUT_DEFINITIVO__LIMITER92_STEREO_v1"
EQ_OUTPUT = PRESETS / f"{EQ_NAME}.prfpset"
LIMITER_OUTPUT = PRESETS / f"{LIMITER_NAME}.prfpset"

EXPECTED_EQ_OPAQUE = [
    0.012301918119192123,
    0.5218750238418579,
    0.3744787275791168,
    0.5,
    0.040867388248443604,
    9.99899857561104e-05,
    0.49270832538604736,
    1.0,
    0.057547956705093384,
    9.998999303206801e-05,
    0.4833333194255829,
    1.0,
    0.16597163677215576,
    6.498998845927417e-05,
    0.47708332538604736,
    1.0,
    0.2827356159687042,
    7.498999912058935e-05,
    0.4583333432674408,
    1.0,
    0.4370308518409729,
    0.0001049899947247468,
    0.5,
    0.0,
    0.6666669845581055,
    0.0,
    1.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.25,
    0.10000000149011612,
    0.14000000059604645,
    0.6153846383094788,
    0.9066666960716248,
    1.0,
    1.0,
    0.0033361134119331837,
    0.4000000059604645,
    0.0,
    0.6872393488883972,
    0.4000000059604645,
    0.0,
    0.0,
    1.0,
]
EXPECTED_LIMITER_OPAQUE = [
    0.996999979019165,
    0.7279999852180481,
    0.0,
    1.0,
    1.0,
    1.0,
    0.0,
    2.0,
]


class PresetError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PresetError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def object_map(root: ET.Element) -> dict[str, ET.Element]:
    result: dict[str, ET.Element] = {}
    for node in root:
        object_id = node.get("ObjectID")
        if object_id is None:
            continue
        require(object_id not in result, f"ObjectID duplicado: {object_id}")
        result[object_id] = node
    return result


def semantic_value(node: ET.Element) -> object:
    text = (node.text or "").strip()
    return {
        "tag": node.tag,
        "attributes": sorted(node.attrib.items()),
        "text": text,
        "children": [semantic_value(child) for child in list(node)],
    }


def semantic_sha(node: ET.Element) -> str:
    payload = json.dumps(semantic_value(node), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256_bytes(payload)


def replace_children(target: ET.Element, source: ET.Element) -> None:
    for child in list(target):
        target.remove(child)
    target.text = source.text
    for child in source:
        target.append(copy.deepcopy(child))


def param_nodes(component: ET.Element, objects: dict[str, ET.Element]) -> list[ET.Element]:
    entries = component.findall("./AudioComponent/Component/Params/Param")
    require([int(entry.get("Index", "-1")) for entry in entries] == list(range(len(entries))), "indices Params nao contiguos")
    result: list[ET.Element] = []
    for entry in entries:
        ref = entry.get("ObjectRef")
        require(ref in objects, f"Param ObjectRef orfao: {ref}")
        node = objects[ref]
        require(node.tag == "AudioComponentParam", f"Param aponta para {node.tag}")
        result.append(node)
    return result


def component_variants(root: ET.Element, objects: dict[str, ET.Element]) -> dict[int, tuple[ET.Element, ET.Element]]:
    result: dict[int, tuple[ET.Element, ET.Element]] = {}
    preset_item = root.find("FilterPresetItem/FilterPresets")
    require(preset_item is not None, "FilterPresetItem ausente")
    for entry in preset_item.findall("FilterPreset"):
        preset_ref = entry.get("ObjectRef")
        require(preset_ref in objects, f"FilterPreset orfao: {preset_ref}")
        preset = objects[preset_ref]
        component_ref = preset.find("Component")
        require(component_ref is not None and component_ref.get("ObjectRef") in objects, "Component do FilterPreset orfao")
        component = objects[str(component_ref.get("ObjectRef"))]
        channel_type = int(component.findtext("./AudioComponent/ChannelType") or "-1")
        require(channel_type not in result, f"ChannelType duplicado: {channel_type}")
        result[channel_type] = (preset, component)
    require(set(result) == {0, 1, 2}, f"variantes inesperadas: {sorted(result)}")
    return result


def decoded_opaque(component: ET.Element, payloads: dict[str, str] | None = None) -> bytes:
    opaque = component.find("OpaqueData")
    require(opaque is not None and opaque.get("Encoding") == "base64", "OpaqueData base64 ausente")
    inline = "".join((opaque.text or "").split())
    if inline:
        return base64.b64decode(inline)
    binary_hash = opaque.get("BinaryHash")
    require(payloads is not None and binary_hash in payloads, f"OpaqueData compartilhado nao resolvido: {binary_hash}")
    return payloads[str(binary_hash)].encode("utf-8")


def opaque_values(payload: bytes) -> list[float]:
    return [float(value) for value in re.findall(rb"<float>([^<]+)</float>", payload)]


def close_enough(actual: list[float], expected: list[float], tolerance: float = 5e-8) -> bool:
    return len(actual) == len(expected) and all(abs(left - right) <= tolerance for left, right in zip(actual, expected))


def load_final_sources() -> tuple[ET.Element, dict[str, ET.Element], dict[str, str], ET.Element, ET.Element]:
    require(sha256_file(PROJECT) == PROJECT_SHA256, "SHA do projeto final pos-save divergiu")
    require(sha256_file(PREFLIGHT) == PREFLIGHT_SHA256, "SHA do preflight divergiu")
    raw = PROJECT.read_bytes()
    require(raw.startswith(b"\x1f\x8b"), "projeto final nao e gzip")
    root = ET.fromstring(gzip.decompress(raw))
    objects = safe.object_map(root)
    preflight = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    _, rows = project_audit.discover_targets(root, objects, preflight)
    require(len(rows) == 20, f"fluxo final possui {len(rows)} alvos")
    payloads = project_audit.opaque_payloads(root)

    first_refs = [entry.get("ObjectRef") for entry in safe.indexed_components(rows[0]["chain"])]
    require(len(first_refs) == 3, "chain final nao possui RN + EQ + limiter")
    first_components = [objects[str(ref)] for ref in first_refs]
    require([safe.component_match(node) for node in first_components] == [mutation.RN_MATCH, EQ_MATCH, LIMITER_MATCH], "ordem da chain final divergiu")
    eq_source, limiter_source = first_components[1], first_components[2]

    eq_signatures: set[tuple[str, str, tuple[float, ...]]] = set()
    limiter_signatures: set[tuple[str, str, tuple[float, ...]]] = set()
    for row in rows:
        refs = [entry.get("ObjectRef") for entry in safe.indexed_components(row["chain"])]
        components = [objects[str(ref)] for ref in refs]
        eq, limiter = components[1], components[2]
        eq_payload = decoded_opaque(eq, payloads)
        limiter_payload = decoded_opaque(limiter, payloads)
        eq_params = tuple(project_audit.parameter_value(node) or 0.0 for node in project_audit.component_parameters(eq, objects))
        limiter_params = tuple(project_audit.parameter_value(node) or 0.0 for node in project_audit.component_parameters(limiter, objects))
        eq_signatures.add((sha256_bytes(eq_payload), sha256_bytes(json.dumps(eq_params).encode()), tuple(opaque_values(eq_payload))))
        limiter_signatures.add((sha256_bytes(limiter_payload), sha256_bytes(json.dumps(limiter_params).encode()), tuple(opaque_values(limiter_payload))))
    require(len(eq_signatures) == 1, f"EQ final nao uniforme: {len(eq_signatures)} signatures")
    require(len(limiter_signatures) == 1, f"limiter final nao uniforme: {len(limiter_signatures)} signatures")
    require(close_enough(opaque_values(decoded_opaque(eq_source, payloads)), EXPECTED_EQ_OPAQUE), "Opaque EQ C1 nao bate a especificacao congelada")
    require(close_enough(opaque_values(decoded_opaque(limiter_source, payloads)), EXPECTED_LIMITER_OPAQUE), "Opaque limiter92 nao bate a especificacao congelada")
    return root, objects, payloads, eq_source, limiter_source


def preset_uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://codex.local/premiere/preset/{name}"))


def transplant(
    wrapper_path: Path,
    wrapper_sha: str,
    output_path: Path,
    name: str,
    effect_match: str,
    source_component: ET.Element,
    project_objects: dict[str, ET.Element],
    project_payloads: dict[str, str],
    stereo_description: str,
) -> dict[str, object]:
    require(sha256_file(wrapper_path) == wrapper_sha, f"wrapper mudou: {wrapper_path.name}")
    wrapper_root = ET.parse(wrapper_path).getroot()
    objects = object_map(wrapper_root)
    variants = component_variants(wrapper_root, objects)
    source_params = project_audit.component_parameters(source_component, project_objects)
    source_names = [node.findtext("Name") or "" for node in source_params]
    source_opaque = decoded_opaque(source_component, project_payloads)
    source_opaque_node = source_component.find("OpaqueData")
    require(source_opaque_node is not None, "OpaqueData fonte ausente")

    nonstereo_before = {channel_type: semantic_sha(component) for channel_type, (_preset, component) in variants.items() if channel_type != 1}
    stereo_preset, stereo_component = variants[1]
    require(stereo_component.findtext("FilterMatchName") == effect_match, "wrapper stereo e de outro efeito")
    target_params = param_nodes(stereo_component, objects)
    target_names = [node.findtext("Name") or "" for node in target_params]
    require(target_names == source_names, "nomes/ordem dos Params wrapper != projeto")

    for target, source in zip(target_params, source_params):
        require(target.get("ClassID") == source.get("ClassID") and target.get("Version") == source.get("Version"), f"schema Param divergiu: {target.findtext('Name')}")
        replace_children(target, source)

    target_opaque = stereo_component.find("OpaqueData")
    require(target_opaque is not None, "OpaqueData do wrapper ausente")
    opaque_position = list(stereo_component).index(target_opaque)
    stereo_component.remove(target_opaque)
    transplanted_opaque = copy.deepcopy(source_opaque_node)
    transplanted_opaque.text = base64.b64encode(source_opaque).decode("ascii")
    stereo_component.insert(opaque_position, transplanted_opaque)
    instance_name = stereo_component.find("./AudioComponent/Component/InstanceName")
    require(instance_name is not None, "InstanceName ausente")
    instance_name.text = name

    stereo_preset.find("Description").text = stereo_description
    for channel_type in (0, 2):
        variant_preset, _component = variants[channel_type]
        variant_preset.find("Description").text = "DEFAULT DO WRAPPER; NAO E O PRESET FINAL. Este arquivo e Stereo-only."

    tree_item = wrapper_root.find("TreeItem/TreeItemBase")
    require(tree_item is not None, "TreeItemBase ausente")
    tree_name = tree_item.find("Name")
    require(tree_name is not None, "nome do preset ausente")
    tree_name.text = name
    uid_node = tree_item.find("./Node/Properties/MZ.EffectPresets.PresetUID")
    require(uid_node is not None, "PresetUID ausente")
    uid_node.text = preset_uid(name)

    ET.indent(wrapper_root, space="  ")
    output_bytes = ET.tostring(wrapper_root, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    require(not output_path.exists(), f"RECUSA DE OVERWRITE: {output_path}")
    output_path.write_bytes(output_bytes)

    audit_root = ET.fromstring(output_path.read_bytes())
    audit_objects = object_map(audit_root)
    audit_variants = component_variants(audit_root, audit_objects)
    audit_stereo_preset, audit_stereo = audit_variants[1]
    audit_params = param_nodes(audit_stereo, audit_objects)
    audit_opaque = decoded_opaque(audit_stereo)
    require([node.findtext("Name") or "" for node in audit_params] == source_names, "Params finais fora de ordem")
    for target, source in zip(audit_params, source_params):
        require(semantic_value(target)["children"] == semantic_value(source)["children"], f"Param final divergiu: {target.findtext('Name')}")
    require(audit_opaque == source_opaque, "OpaqueData final nao e byte-exato ao projeto")
    require(audit_stereo.find("OpaqueData").attrib == source_opaque_node.attrib, "atributos OpaqueData finais divergiram")
    require(audit_stereo.findtext("./AudioComponent/Component/InstanceName") == name, "InstanceName final divergiu")
    require(audit_stereo_preset.findtext("Description") == stereo_description, "descricao stereo divergiu")
    require({channel_type: semantic_sha(component) for channel_type, (_preset, component) in audit_variants.items() if channel_type != 1} == nonstereo_before, "mono/5.1 deixaram de ser defaults identicos")

    ids = [node.get("ObjectID") for node in audit_root if node.get("ObjectID")]
    refs = [node.get("ObjectRef") for node in audit_root.iter() if node.get("ObjectRef")]
    require(len(ids) == len(set(ids)), "ObjectID duplicado no preset final")
    require(not sorted(set(refs) - set(ids)), f"ObjectRef orfao: {sorted(set(refs) - set(ids))}")
    require(len(audit_root.findall("FilterPreset")) == 3, "preset final nao possui as tres variantes de layout")

    return {
        "name": name,
        "path": str(output_path),
        "sha256": sha256_file(output_path),
        "bytes": output_path.stat().st_size,
        "preset_uid": uid_node.text,
        "effect_match_name": effect_match,
        "wrapper": {"path": str(wrapper_path), "sha256": wrapper_sha},
        "variant_policy": {
            "validated_and_modified": "stereo / ChannelType 1 / L-R layout [100,101]",
            "mono_channel_type_0": "default original do wrapper; nao usar como preset final",
            "surround_channel_type_2": "default original do wrapper; nao usar como preset final",
            "label": "Stereo-only",
        },
        "source": {
            "project": str(PROJECT),
            "project_sha256": PROJECT_SHA256,
            "component_object_id": source_component.get("ObjectID"),
            "opaque_binary_hash": source_opaque_node.get("BinaryHash"),
            "opaque_decoded_sha256": sha256_bytes(source_opaque),
            "opaque_decoded_bytes": len(source_opaque),
            "opaque_attributes_exact_from_project": True,
            "params_children_exact_from_project": True,
        },
        "xml_audit": {
            "parse": "PASS",
            "object_ids_unique": True,
            "object_refs_resolved": True,
            "filter_preset_variants": [0, 1, 2],
            "stereo_params_exact": True,
            "stereo_opaque_exact": True,
            "mono_component_semantically_identical_to_wrapper": True,
            "surround_component_semantically_identical_to_wrapper": True,
        },
        "native_param_values": [project_audit.parameter_value(node) for node in source_params],
        "native_param_names": source_names,
        "opaque_float_values": opaque_values(source_opaque),
    }


def atomic_create_text(path: Path, text: str) -> None:
    require(not path.exists(), f"RECUSA DE OVERWRITE: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    require(not EQ_OUTPUT.exists() and not LIMITER_OUTPUT.exists(), "outputs finais ja existem")
    require(not MANIFEST.exists() and not REPORT.exists(), "relatorio/manifesto ja existem")
    _project_root, project_objects, payloads, eq_source, limiter_source = load_final_sources()

    eq_result = transplant(
        EQ_WRAPPER,
        EQ_WRAPPER_SHA256,
        EQ_OUTPUT,
        EQ_NAME,
        EQ_MATCH,
        eq_source,
        project_objects,
        payloads,
        "STEREO-ONLY. EQ C1 final sem Enhance: HP 100 Hz/24 dB; LS 315 Hz +2,1 dB; 1 kHz -0,7; 1,4 kHz -1,6; 4 kHz -2,2; 6,8 kHz -4,0 dB.",
    )
    limiter_result = transplant(
        LIMITER_WRAPPER,
        LIMITER_WRAPPER_SHA256,
        LIMITER_OUTPUT,
        LIMITER_NAME,
        LIMITER_MATCH,
        limiter_source,
        project_objects,
        payloads,
        "STEREO-ONLY. Hard Limiter final: Input +9,20 dB; teto -0,30 dBFS; lookahead 5 ms; release 200 ms; Link ON; True Peak OFF; Decay ON.",
    )

    manifest = {
        "schema": "codex.premiere.portable-stereo-presets.c1-limiter92.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "scope": {
            "generated_presets": 2,
            "premiere_gui_used": False,
            "adobe_profile_read_or_modified": False,
            "old_presets_modified": False,
            "audio_files_opened_decoded_or_hashed": False,
            "preset_policy": "Stereo-only; mono e 5.1 permanecem defaults dos wrappers e nao sao aprovados",
        },
        "source_project": {"path": str(PROJECT), "sha256": PROJECT_SHA256, "unchanged": sha256_file(PROJECT) == PROJECT_SHA256},
        "preflight": {"path": str(PREFLIGHT), "sha256": PREFLIGHT_SHA256},
        "presets": [eq_result, limiter_result],
        "human_spec": {
            "eq_c1": {
                "high_pass": {"enabled": True, "frequency_hz": 100, "slope_db_oct": 24},
                "low_shelf": {"enabled": True, "frequency_hz": 315, "gain_db": 2.1, "order": 1},
                "bell_1": {"enabled": True, "frequency_hz": 1000, "gain_db": -0.7, "q": 1.0},
                "bell_2": {"enabled": True, "frequency_hz": 1400, "gain_db": -1.6, "q": 1.0},
                "bell_3": {"enabled": True, "frequency_hz": 4000, "gain_db": -2.2, "q": 0.65},
                "bell_4": {"enabled": True, "frequency_hz": 6800, "gain_db": -4.0, "q": 0.75},
                "bell_5": {"enabled": False},
                "high_shelf": {"enabled": False},
                "low_pass": {"enabled": False},
                "output_gain_db": 0.0,
                "mode": "Constant Width",
            },
            "hard_limiter92": {
                "input_boost_db": 9.2,
                "maximum_amplitude_dbfs": -0.3,
                "lookahead_ms": 5.0,
                "release_ms": 200.0,
                "link_channels": True,
                "limit_true_peak": False,
                "decay_to_ceiling": True,
            },
        },
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_create_text(MANIFEST, manifest_text)

    lines = [
        "# Auditoria dos presets portateis C1 + Limiter92",
        "",
        "**Status: PASS.** Foram gerados dois `.prfpset` independentes, marcados **Stereo-only**, a partir do projeto final pos-save. O Premiere e o perfil global da Adobe nao foram abertos nem alterados.",
        "",
        "## Arquivos",
        "",
        "| Preset | SHA-256 | Tamanho | Auditoria XML |",
        "|---|---|---:|---|",
        f"| `{EQ_NAME}` | `{eq_result['sha256']}` | {eq_result['bytes']} bytes | PASS |",
        f"| `{LIMITER_NAME}` | `{limiter_result['sha256']}` | {limiter_result['bytes']} bytes | PASS |",
        "",
        "## Garantias verificadas",
        "",
        "- O projeto fonte confere com o SHA-256 pos-save e permaneceu inalterado.",
        "- A variante stereo tem `Params` e `OpaqueData` byte/semanticamente iguais aos componentes do projeto final.",
        "- Todos os `ObjectID` sao unicos e todos os `ObjectRef` resolvem dentro de cada `.prfpset`.",
        "- Os wrappers originais e os presets antigos permaneceram inalterados.",
        "- As variantes mono e 5.1 conservam exatamente os componentes default dos wrappers; portanto, nao representam C1/Limiter92.",
        "- Nenhum arquivo de audio foi aberto, decodificado ou hasheado.",
        "",
        "## Uso correto",
        "",
        f"1. Em clipe **stereo**, aplique `{EQ_NAME}`.",
        f"2. Depois, aplique `{LIMITER_NAME}`.",
        "3. Nao use estes arquivos em clipes mono ou 5.1 sem converter/interpretar o audio como stereo.",
        "",
        "O manifesto JSON contem a lista completa dos valores nativos, hashes dos payloads e a especificacao legivel.",
        "",
    ]
    atomic_create_text(REPORT, "\n".join(lines))
    print(json.dumps({"status": "PASS", "presets": [eq_result["path"], limiter_result["path"]], "manifest": str(MANIFEST), "report": str(REPORT)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PresetError, project_audit.AuditError, safe.GuardError, OSError, ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error), "audio_files_opened_decoded_or_hashed": False}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2)
