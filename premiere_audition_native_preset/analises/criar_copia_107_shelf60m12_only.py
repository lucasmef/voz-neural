#!/usr/bin/env python3
"""Cria a ablação 107 EQ shelf mínimo -> limiter, sem outros efeitos.

O EQ é clonado da instância que o Premiere 2026 já abriu e salvou no projeto
D2. Isso preserva a representação nativa validada pela própria interface,
inclusive o valor 0.375 que o round-trip confirmou como -12 dB nesse controle.

O mutador opera somente em projetos descartáveis do workspace, fixa hashes,
recusa sobrescrita e valida uma whitelist completa de objetos top-level.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from criar_copia_107_c1_dynamics import (
    GuardError,
    chain_refs,
    component_refs,
    numeric_graph,
    require,
    sha256,
    top,
    top_hashes,
)


ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT / "premiere_audition_native_preset/projetos_descartaveis"
RESULTS = ROOT / "premiere_audition_native_preset/analises/resultados/107_SHELF60M12_ONLY_REAL"

INPUT = PROJECTS / "Teste_107_ADAPTIVE_NR_LIGHT_WORKING.prproj"
EQ_DONOR = PROJECTS / "Teste_107_D2_SHELF60M12_DYNAMICS_REAL_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_107_SHELF60M12_ONLY_REAL_WORKING.prproj"
MANIFEST = RESULTS / "MANIFESTO_MUTACAO_SHELF60M12_ONLY.json"

EXPECTED_INPUT_SHA256 = "f77337dee90059541d73fb7387bd96996479465bda1697f06e35da8192579d89"
EXPECTED_EQ_DONOR_SHA256 = "50338a6e21d638148cd3bd804ef439a73530fd400dd9e701060238c725e2a00a"
EXPECTED_INPUT_MAX_OBJECT_ID = 3399

TARGET_TRACK_ITEM_ID = "433"
TARGET_CHAIN_ID = "511"
TARGET_SUBCLIP_ID = "512"
TARGET_AUDIO_CLIP_ID = "706"
LIMITER_ID = "705"
LIMITER_PARAM_REFS = [str(i) for i in range(1016, 1024)]

# IDs materializados pelo Premiere após o round-trip do projeto D2.
EQ_DONOR_ID = "705"
EQ_DONOR_PARAM_REFS = [str(i) for i in range(1018, 1064)]

NEW_EQ_ID = "3400"
NEW_EQ_PARAM_REFS = [str(i) for i in range(3401, 3447)]

EQ_MATCH_NAME = "8edcd326-33de-4a4d-985b-f1bf48ed1427"
LIMITER_MATCH_NAME = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
LIMITER_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1"
EQ_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_EQ_SHELF60_M12_ONLY_REAL"

EXPECTED_EQ_PARAM_NAMES = [
    "Bypass",
    "Low Shelf Frequency",
    "Low Shelf Gain",
    "High Shelf Frequency",
    "High Shelf Gain",
    "EQ Band 1 Center Frequency",
    "EQ Band 1 Q",
    "EQ Band 1 Gain",
    "EQ Band 1 Enable",
    "EQ Band 2 Center Frequency",
    "EQ Band 2 Q",
    "EQ Band 2 Gain",
    "EQ Band 2 Enable",
    "EQ Band 3 Center Frequency",
    "EQ Band 3 Q",
    "EQ Band 3 Gain",
    "EQ Band 3 Enable",
    "EQ Band 4 Center Frequency",
    "EQ Band 4 Q",
    "EQ Band 4 Gain",
    "EQ Band 4 Enable",
    "EQ Band 5 Center Frequency",
    "EQ Band 5 Q",
    "EQ Band 5 Gain",
    "EQ Band 5 Enable",
    "Gain",
    "ConstantQ",
    "Low Shelf Filter Order",
    "High Shelf Filter Order",
    "Ultra-Quiet Mode",
    "Low Shelf Enable",
    "High Shelf Enable",
    "Original Sample Rate",
    "EQ Band 1 Bandwidth",
    "EQ Band 2 Bandwidth",
    "EQ Band 3 Bandwidth",
    "EQ Band 4 Bandwidth",
    "EQ Band 5 Bandwidth",
    "High-Pass Filter Enable",
    "High-Pass Cutoff Frequency",
    "High-Pass Slope",
    "Low-Pass Filter Enable",
    "Low-Pass Cutoff Frequency",
    "Low-Pass Slope",
    "Bypass",
    "LowGraphRange",
]


def decode_opaque_values(effect: ET.Element) -> tuple[str, list[float]]:
    opaque = effect.find("OpaqueData")
    require(opaque is not None and opaque.text and opaque.text.strip(), "OpaqueData EQ ausente")
    decoded = base64.b64decode(opaque.text).decode("utf-8")
    raw_values = re.findall(r"<float>([^<]+)</float>", decoded)
    require(len(raw_values) == 46, f"ParamList EQ deveria ter 46 floats, tem {len(raw_values)}")
    return decoded, [float(value) for value in raw_values]


def scalar(param: ET.Element) -> float | None:
    value = param.findtext("CurrentValue")
    return None if value is None else float(value)


def main() -> int:
    require(INPUT != OUTPUT, "entrada e saida nao podem coincidir")
    require(INPUT.parent == EQ_DONOR.parent == OUTPUT.parent == PROJECTS, "projeto fora de projetos_descartaveis")
    require(not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(not MANIFEST.exists(), f"RECUSA DE OVERWRITE: {MANIFEST}")

    compressed_before = INPUT.read_bytes()
    donor_compressed_before = EQ_DONOR.read_bytes()
    require(sha256(compressed_before) == EXPECTED_INPUT_SHA256, "SHA da entrada limiter-only divergiu")
    require(sha256(donor_compressed_before) == EXPECTED_EQ_DONOR_SHA256, "SHA do doador EQ round-trip divergiu")

    xml_before = gzip.decompress(compressed_before)
    donor_xml = gzip.decompress(donor_compressed_before)
    root = ET.fromstring(xml_before)
    donor_root = ET.fromstring(donor_xml)
    require(root.tag == donor_root.tag == "PremiereData", "raiz XML inesperada")

    graph_before = numeric_graph(xml_before)
    require(graph_before["max_object_id"] == EXPECTED_INPUT_MAX_OBJECT_ID, "max ObjectID da entrada divergiu")
    existing_ids = set(re.findall(r'\bObjectID="(\d+)"', xml_before.decode("utf-8")))
    reserved_ids = {NEW_EQ_ID, *NEW_EQ_PARAM_REFS}
    require(reserved_ids.isdisjoint(existing_ids), "IDs reservados para shelf-only ja existem")

    track_item = top(root, "AudioClipTrackItem", TARGET_TRACK_ITEM_ID)
    owner = track_item.find("./ClipTrackItem/ComponentOwner/Components")
    require(owner is not None and owner.get("ObjectRef") == TARGET_CHAIN_ID, "track item 107 nao aponta para chain 511")
    require(track_item.find("./ClipTrackItem/HeadTransition") is None, "fade de cabeca reapareceu")
    require(track_item.find("./ClipTrackItem/TailTransition") is None, "fade de cauda reapareceu")

    chain = top(root, "AudioComponentChain", TARGET_CHAIN_ID)
    require(chain_refs(chain) == [LIMITER_ID], "entrada shelf-only nao e limiter-only")
    components = chain.find("./ComponentChain/Components")
    require(components is not None and len(components) == 1, "estrutura da chain 511 divergiu")

    subclip = top(root, "SubClip", TARGET_SUBCLIP_ID)
    require(subclip.findtext("Name") == "107_REF_E100_AUTOMATCH.mov", "fonte do subclip 107 divergiu")
    clip_ref = subclip.find("Clip")
    require(clip_ref is not None and clip_ref.get("ObjectRef") == TARGET_AUDIO_CLIP_ID, "AudioClip do 107 divergiu")

    limiter = top(root, "AudioFilterComponent", LIMITER_ID)
    require(limiter.findtext("FilterMatchName") == LIMITER_MATCH_NAME, "efeito 705 da entrada nao e limiter")
    require(component_refs(limiter) == LIMITER_PARAM_REFS, "refs do limiter divergiram")
    require(limiter.findtext("./AudioComponent/Component/InstanceName") == LIMITER_INSTANCE_NAME, "instancia do limiter divergiu")
    limiter_hash_before = sha256(ET.tostring(limiter, encoding="utf-8"))

    donor_chain = top(donor_root, "AudioComponentChain", TARGET_CHAIN_ID)
    donor_chain_refs = chain_refs(donor_chain)
    require(len(donor_chain_refs) == 3, "chain round-trip D2 deveria ter tres efeitos")
    donor_effects = [top(donor_root, "AudioFilterComponent", ref) for ref in donor_chain_refs]
    require([effect.findtext("FilterMatchName") for effect in donor_effects] == [
        EQ_MATCH_NAME,
        "f76dad9c-77da-477a-9939-eefefc5c23bd",
        LIMITER_MATCH_NAME,
    ], "ordem do doador round-trip nao e EQ -> Dynamics -> limiter")

    donor_eq = top(donor_root, "AudioFilterComponent", EQ_DONOR_ID)
    require(donor_eq.findtext("FilterMatchName") == EQ_MATCH_NAME, "efeito 705 do doador nao e Parametric Equalizer")
    require(component_refs(donor_eq) == EQ_DONOR_PARAM_REFS, "refs do EQ doador divergiram")
    donor_params = [top(donor_root, "AudioComponentParam", ref) for ref in EQ_DONOR_PARAM_REFS]
    require([item.findtext("Name") for item in donor_params] == EXPECTED_EQ_PARAM_NAMES, "nomes/ordem dos 46 Params EQ divergiram")

    decoded_opaque, opaque_values = decode_opaque_values(donor_eq)
    # ParamList[0..44] corresponde a Params[1..45]; ParamList[45] e a versao.
    require(abs(opaque_values[0] - 0.0016680567) < 1e-10, "frequencia nativa do shelf nao e a round-trip de 60 Hz")
    require(abs(opaque_values[1] - 0.375) < 1e-12, "ganho nativo do shelf nao e o round-trip de -12 dB")
    require(all(abs(opaque_values[index]) < 1e-12 for index in (7, 11, 15, 19, 23)), "alguma banda bell esta ligada")
    require(abs(opaque_values[24] - 0.66666698) < 2e-8, "ganho de saida do EQ nao e 0 dB")
    require(abs(opaque_values[25]) < 1e-12, "EQ nao esta em Constant Width")
    require(abs(opaque_values[29] - 1.0) < 1e-12, "low shelf nao esta ligado")
    require(abs(opaque_values[30]) < 1e-12, "high shelf esta ligado")
    require(abs(opaque_values[37]) < 1e-12, "high-pass esta ligado")
    require(abs(opaque_values[40]) < 1e-12, "low-pass esta ligado")
    require(abs(opaque_values[43]) < 1e-12, "bypass interno esta ligado")
    require(abs(opaque_values[45] - 1.0) < 1e-12, "versao interna do EQ divergiu")

    # Confirma também os escalares que o Premiere materializa fora do Opaque.
    require(abs((scalar(donor_params[1]) or -1.0) - 0.0016680567059665918) < 1e-15, "CurrentValue de 60 Hz divergiu")
    require(abs((scalar(donor_params[2]) or -1.0) - 0.375) < 1e-12, "CurrentValue de -12 dB divergiu")
    require(all(abs(scalar(donor_params[index]) or 0.0) < 1e-12 for index in (8, 12, 16, 20, 24)), "CurrentValue de alguma banda bell esta ligado")
    require(abs((scalar(donor_params[25]) or -1.0) - 0.66666698455810547) < 1e-15, "CurrentValue de output 0 dB divergiu")
    require(abs(scalar(donor_params[26]) or 0.0) < 1e-12, "CurrentValue de ConstantQ nao e zero")
    require(all(abs(scalar(donor_params[index]) or 0.0) < 1e-12 for index in (31, 38, 41, 44)), "CurrentValue de filtro desligado divergiu")
    require(scalar(donor_params[0]) is None, "Premiere deveria omitir o escalar do bypass externo desligado")

    eq = copy.deepcopy(donor_eq)
    eq.set("ObjectID", NEW_EQ_ID)
    component = eq.find("./AudioComponent/Component")
    require(component is not None, "Component do EQ ausente")
    internal_id = component.find("ID")
    require(internal_id is not None and internal_id.text == "4", "ID interno do EQ round-trip divergiu")
    instance = component.find("InstanceName")
    if instance is None:
        instance = ET.SubElement(component, "InstanceName")
    instance.text = EQ_INSTANCE_NAME
    for ref_node, new_ref in zip(eq.findall("./AudioComponent/Component/Params/Param"), NEW_EQ_PARAM_REFS):
        ref_node.set("ObjectRef", new_ref)
    require(component_refs(eq) == NEW_EQ_PARAM_REFS, "refs clonadas do EQ divergiram")

    new_params: list[ET.Element] = []
    for source, new_ref in zip(donor_params, NEW_EQ_PARAM_REFS):
        clone = copy.deepcopy(source)
        clone.set("ObjectID", new_ref)
        new_params.append(clone)

    before_hashes = top_hashes(root)
    limiter_ref = list(components)[0]
    limiter_ref.set("Index", "1")
    components.insert(0, ET.Element("Component", {"Index": "0", "ObjectRef": NEW_EQ_ID}))
    root.append(eq)
    for param in new_params:
        root.append(param)

    require(chain_refs(top(root, "AudioComponentChain", TARGET_CHAIN_ID)) == [NEW_EQ_ID, LIMITER_ID], "ordem final nao e EQ -> limiter")
    final_eq = top(root, "AudioFilterComponent", NEW_EQ_ID)
    require(final_eq.findtext("FilterMatchName") == EQ_MATCH_NAME, "EQ final nao persistiu")
    require(final_eq.findtext("./AudioComponent/Component/InstanceName") == EQ_INSTANCE_NAME, "nome da instancia EQ nao persistiu")
    require(sha256(ET.tostring(top(root, "AudioFilterComponent", LIMITER_ID), encoding="utf-8")) == limiter_hash_before, "limiter foi alterado")
    require(not any(
        element.findtext("FilterMatchName") in {
            "f76dad9c-77da-477a-9939-eefefc5c23bd",  # Dynamics Processing
        }
        for element in root.findall("AudioFilterComponent")
        if element.get("ObjectID") in chain_refs(top(root, "AudioComponentChain", TARGET_CHAIN_ID))
    ), "Dynamics apareceu na chain final")

    final_opaque, final_values = decode_opaque_values(final_eq)
    require(final_opaque == decoded_opaque, "OpaqueData do EQ salvo mudou durante a clonagem")
    require(final_values == opaque_values, "valores OpaqueData do EQ salvo mudaram")
    for source, clone in zip(donor_params, new_params):
        source_copy = copy.deepcopy(source)
        source_copy.set("ObjectID", clone.get("ObjectID"))
        require(ET.tostring(source_copy, encoding="utf-8") == ET.tostring(clone, encoding="utf-8"), f"Param EQ clonado divergiu: {clone.findtext('Name')}")

    after_hashes = top_hashes(root)
    removed_keys = sorted(set(before_hashes) - set(after_hashes))
    added_keys = sorted(set(after_hashes) - set(before_hashes))
    changed_keys = sorted(key for key in set(before_hashes) & set(after_hashes) if before_hashes[key] != after_hashes[key])
    expected_added = sorted(
        [f"AudioFilterComponent:ObjectID={NEW_EQ_ID}"]
        + [f"AudioComponentParam:ObjectID={ref}" for ref in NEW_EQ_PARAM_REFS]
    )
    require(not removed_keys, f"objeto top-level removido: {removed_keys}")
    require(added_keys == expected_added, f"objetos adicionados fora da whitelist: {added_keys}")
    require(changed_keys == [f"AudioComponentChain:ObjectID={TARGET_CHAIN_ID}"], f"objetos alterados fora da whitelist: {changed_keys}")

    xml_after = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml_after)
    graph_after = numeric_graph(xml_after)
    require(graph_after["duplicate_object_ids"] == graph_before["duplicate_object_ids"], "duplicatas de ObjectID mudaram")
    require(graph_after["unresolved_object_refs"] == graph_before["unresolved_object_refs"], "refs nao resolvidas mudaram")
    require(graph_after["object_id_count"] == graph_before["object_id_count"] + 47, "ObjectID nao aumentou em 47")
    require(graph_after["object_ref_count"] == graph_before["object_ref_count"] + 47, "ObjectRef nao aumentou em 47")

    compressed_after = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after, "round-trip gzip falhou")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "entrada mudou durante a operacao")
    require(sha256(EQ_DONOR.read_bytes()) == EXPECTED_EQ_DONOR_SHA256, "doador EQ mudou durante a operacao")

    report = {
        "schema": "codex.premiere.safe-prproj-mutation.v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "SHELF_ONLY_ABLATION_REQUIRES_PREMIERE_RESAVE_UI_CHECK_AND_REAL_EXPORT_NOT_APPROVED_PRESET",
        "operation": "clone_GUI_roundtripped_native_ParametricEQ_shelf60m12_before_existing_limiter_without_Dynamics",
        "input": {"path": str(INPUT), "sha256": EXPECTED_INPUT_SHA256, "modified": False},
        "donor": {
            "path": str(EQ_DONOR),
            "sha256": EXPECTED_EQ_DONOR_SHA256,
            "effect_object_id": EQ_DONOR_ID,
            "roundtrip_saved_by": "Adobe Premiere Pro 2026",
            "modified": False,
        },
        "output": {"path": str(OUTPUT), "sha256": sha256(compressed_after), "bytes": len(compressed_after)},
        "target": {
            "sequence": "107_REF_E100_AUTOMATCH",
            "track_item_object_id": TARGET_TRACK_ITEM_ID,
            "component_chain_object_id": TARGET_CHAIN_ID,
            "source_subclip_object_id": TARGET_SUBCLIP_ID,
            "source_audio_clip_object_id": TARGET_AUDIO_CLIP_ID,
            "chain_before": ["Hard Limiter v1"],
            "chain_after": ["Parametric Equalizer shelf 60 Hz -12 dB", "Hard Limiter v1"],
            "eq_object_id": NEW_EQ_ID,
            "eq_param_object_ids": NEW_EQ_PARAM_REFS,
            "limiter_object_id": LIMITER_ID,
        },
        "parametric_equalizer": {
            "bypass": False,
            "bypass_representation": "external Bypass default/off scalar omitted by Premiere round-trip; internal Bypass=0",
            "low_shelf": {
                "enabled": True,
                "frequency_hz_ui_verified": 60.0,
                "gain_db_ui_verified": -12.0,
                "frequency_native_roundtrip": opaque_values[0],
                "gain_native_roundtrip": opaque_values[1],
            },
            "high_shelf_enabled": False,
            "bell_bands_enabled": [False, False, False, False, False],
            "high_pass_enabled": False,
            "low_pass_enabled": False,
            "output_gain_db": 0.0,
            "output_gain_native_roundtrip": opaque_values[24],
            "mode": "Constant Width",
            "constant_q_native_roundtrip": opaque_values[25],
            "opaque_and_component_params_cloned_byte_equivalent_from_roundtrip_donor": True,
            "opaque_sha256": sha256(decoded_opaque.encode("utf-8")),
        },
        "excluded_from_target_chain": [
            "Dynamics Processing",
            "DeNoise",
            "DeEsser",
            "Reduce Reverb",
        ],
        "isolation": {
            "added_top_level_objects": added_keys,
            "changed_top_level_objects": changed_keys,
            "removed_top_level_objects": removed_keys,
            "all_other_top_level_objects_unchanged": True,
            "existing_limiter_unchanged": True,
            "source_and_timing_unchanged": True,
            "audio_head_tail_transitions_absent": True,
        },
        "integrity": {"gzip": True, "xml": True, "graph_before": graph_before, "graph_after": graph_after},
        "mandatory_next_steps": [
            "open and resave only this disposable project in Premiere 2026",
            "visually verify low shelf 60 Hz -12 dB, all bells/high shelf/HP/LP off, Constant Width and output 0 dB",
            "verify chain order EQ -> Hard Limiter with no Dynamics/DeNoise/DeEsser",
            "export a real 48 kHz stereo PCM24 WAV over Entire Source",
            "compare with CapCut and limiter-only baseline using objective gates",
        ],
        "warning": "Shelf-only is one disposable real-render ablation, not an approved or definitive preset.",
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        with OUTPUT.open("xb") as handle:
            handle.write(compressed_after)
        created.append(OUTPUT)
        reread = OUTPUT.read_bytes()
        require(sha256(reread) == report["output"]["sha256"], "SHA da copia gravada divergiu")
        require(gzip.decompress(reread) == xml_after, "XML da copia gravada divergiu")
        ET.fromstring(gzip.decompress(reread))

        with MANIFEST.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        created.append(MANIFEST)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr)
        raise SystemExit(2)
