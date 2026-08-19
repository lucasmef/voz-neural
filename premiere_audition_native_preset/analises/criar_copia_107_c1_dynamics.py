#!/usr/bin/env python3
"""Cria a ablação C1 do 107: Dynamics Processing -> Hard Limiter.

O mutador opera somente sobre uma cópia descartável, fixa os hashes da entrada
e do projeto doador, recusa sobrescrever saídas e valida a whitelist completa.
Dynamics e seus 33 parâmetros são clonados de uma instância real do Premiere.

Importante: o ParamList de OpaqueData omite Bypass. Logo Opaque[0..31]
corresponde a Params[1..32], e Opaque[32] é a versão interna. A curva e os
AudioComponentParam são atualizados em conjunto para evitar o deslocamento que
invalidou a antiga experiência XML do 95.
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT / "premiere_audition_native_preset/projetos_descartaveis"
RESULTS = ROOT / "premiere_audition_native_preset/analises/resultados/107_C1_DYNAMICS_REAL"

INPUT = PROJECTS / "Teste_107_ADAPTIVE_NR_LIGHT_WORKING.prproj"
DONOR = PROJECTS / "Teste_95_NORMALIZACAO_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_107_C1_DYNAMICS_REAL_WORKING.prproj"
MANIFEST = RESULTS / "MANIFESTO_MUTACAO_C1.json"

EXPECTED_INPUT_SHA256 = "f77337dee90059541d73fb7387bd96996479465bda1697f06e35da8192579d89"
EXPECTED_DONOR_SHA256 = "0ddb684ed1eac47496da781150dad3b97a111657b069973f5222ac6f828ec540"
EXPECTED_INPUT_MAX_OBJECT_ID = 3399

TARGET_TRACK_ITEM_ID = "433"
TARGET_CHAIN_ID = "511"
TARGET_SUBCLIP_ID = "512"
TARGET_AUDIO_CLIP_ID = "706"
LIMITER_ID = "705"
LIMITER_PARAM_REFS = [str(i) for i in range(1016, 1024)]

DONOR_DYNAMICS_ID = "945"
DONOR_DYNAMICS_PARAM_REFS = [str(i) for i in range(1329, 1362)]
NEW_DYNAMICS_ID = "3400"
NEW_DYNAMICS_PARAM_REFS = [str(i) for i in range(3401, 3434)]

DYNAMICS_MATCH_NAME = "f76dad9c-77da-477a-9939-eefefc5c23bd"
LIMITER_MATCH_NAME = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
LIMITER_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1"
DYNAMICS_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_C1_DYNAMICS_REAL"

CURVE_POINTS_DB = [
    (-100.0, -100.0),
    (-93.8, -100.0),
    (-68.0, -74.2),
    (-44.0, -50.2),
    (-38.0, -48.0),
    (-34.0, -34.0),
    (0.0, 0.0),
]

# A validação visual do primeiro C1 provou que os dois controles do Gain
# Processor não usam segundos diretos: 0.005 apareceu como 2 ms e 0.030 como
# 60 ms. A segunda leitura visual mostrou 0.0125 como 6 ms; a escala efetiva
# do Attack e 500 ms/unidade (com arredondamento da interface), enquanto o
# Release permanece em 2000 ms/unidade.
# Detector e Lookahead permanecem nos valores nativos zero.
TIMING_NATIVE = {
    "level_detector_attack": 0.0,
    "level_detector_release": 0.0,
    "gain_processor_attack": 0.010,  # 5 ms / 500
    "gain_processor_release": 0.015,  # 30 ms / 2000
    "lookahead": 0.0,
}

EXPECTED_DYNAMICS_PARAM_NAMES = [
    "Bypass",
    "Level Detector - Attack Time",
    "Level Detector - Release Time",
    "Gain Processor - Attack Time",
    "Gain Processor - Release Time",
    "Lookahead Time",
    "Input Gain",
    "Output Gain",
    "Low Cutoff",
    "High Cutoff",
    "Lock Filter",
    "Number of X/Y Pairs",
    "Link Channels",
    "Peak/RMS",
    "Envelope Only",
    "Spline Curves",
    "Noise Gating (if expanding more than 50:1)",
    "Current Tab",
    "Location1",
    "Value1",
    "Location2",
    "Value2",
    "Location3",
    "Value3",
    "Location4",
    "Value4",
    "Location5",
    "Value5",
    "Location6",
    "Value6",
    "Location7",
    "Value7",
    "Make-Up Gain",
]


class GuardError(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise GuardError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fmt(value: float) -> str:
    if abs(value) < 5e-13:
        return "0"
    if abs(value - 1.0) < 5e-13:
        return "1"
    return format(value, ".12g")


def db_to_native(value_db: float) -> float:
    return (value_db + 100.0) / 100.0


def top(root: ET.Element, tag: str, object_id: str) -> ET.Element:
    hits = [element for element in root if element.tag == tag and element.get("ObjectID") == object_id]
    require(len(hits) == 1, f"{tag} ObjectID={object_id}: esperado 1, obtido {len(hits)}")
    return hits[0]


def top_identity(element: ET.Element) -> str:
    for attr in ("ObjectID", "ObjectUID", "ObjectRef"):
        value = element.get(attr)
        if value:
            return f"{element.tag}:{attr}={value}"
    raise GuardError(f"elemento top-level sem identidade segura: {element.tag}")


def top_hashes(root: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for element in root:
        identity = top_identity(element)
        require(identity not in result, f"identidade top-level duplicada: {identity}")
        result[identity] = sha256(ET.tostring(element, encoding="utf-8"))
    return result


def numeric_graph(xml_bytes: bytes) -> dict[str, object]:
    text = xml_bytes.decode("utf-8")
    ids = re.findall(r'\bObjectID="(\d+)"', text)
    refs = re.findall(r'\bObjectRef="(\d+)"', text)
    counts = Counter(ids)
    return {
        "object_id_count": len(ids),
        "object_ref_count": len(refs),
        "duplicate_object_ids": sorted(key for key, count in counts.items() if count > 1),
        "unresolved_object_refs": sorted(set(refs) - set(ids), key=int),
        "max_object_id": max(map(int, ids)),
    }


def component_refs(effect: ET.Element) -> list[str | None]:
    return [item.get("ObjectRef") for item in effect.findall("./AudioComponent/Component/Params/Param")]


def chain_refs(chain: ET.Element) -> list[str | None]:
    return [item.get("ObjectRef") for item in chain.findall("./ComponentChain/Components/Component")]


def validate_curve() -> None:
    require(len(CURVE_POINTS_DB) == 7, "C1 deve usar exatamente sete pares")
    require(CURVE_POINTS_DB[0] == (-100.0, -100.0), "curva C1 deve iniciar em (-100,-100)")
    require(CURVE_POINTS_DB[-1] == (0.0, 0.0), "curva C1 deve terminar em (0,0)")
    xs = [x for x, _ in CURVE_POINTS_DB]
    ys = [y for _, y in CURVE_POINTS_DB]
    require(all(-100.0 <= value <= 0.0 for value in xs + ys), "ponto C1 fora de -100..0 dB")
    require(all(right > left for left, right in zip(xs, xs[1:])), "X da curva C1 nao cresce estritamente")
    require(all(right >= left for left, right in zip(ys, ys[1:])), "Y da curva C1 nao e monotono")
    require(all(y <= x + 1e-9 for x, y in CURVE_POINTS_DB), "curva C1 contem ganho ascendente")
    require(max(x - y for x, y in CURVE_POINTS_DB) == 10.0, "atenuacao maxima C1 divergiu de 10 dB")


def set_param_scalar(param: ET.Element, value: float) -> None:
    for tag in ("StartKeyframe", "CurrentValue"):
        previous = param.find(tag)
        if previous is not None:
            param.remove(previous)
    insertion = next((i for i, child in enumerate(param) if child.tag == "IsTimeVarying"), len(param))
    start = ET.Element("StartKeyframe")
    start.text = f"-91445760000000000,{fmt(value)},0,0,0,0,0,0"
    current = ET.Element("CurrentValue")
    current.text = fmt(value)
    param.insert(insertion, start)
    param.insert(insertion + 1, current)


def decode_opaque(effect: ET.Element) -> tuple[ET.Element, str, list[str]]:
    opaque = effect.find("OpaqueData")
    require(opaque is not None and opaque.text and opaque.text.strip(), "OpaqueData materializado ausente")
    decoded = base64.b64decode(opaque.text).decode("utf-8")
    values = re.findall(r"<float>([^<]+)</float>", decoded)
    require(len(values) == 33, f"ParamList deveria ter 33 floats, tem {len(values)}")
    return opaque, decoded, values


def main() -> int:
    validate_curve()
    require(INPUT != OUTPUT, "entrada e saida nao podem coincidir")
    require(INPUT.parent == OUTPUT.parent == PROJECTS, "entrada/saida fora de projetos_descartaveis")
    require(not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(not MANIFEST.exists(), f"RECUSA DE OVERWRITE: {MANIFEST}")

    compressed_before = INPUT.read_bytes()
    donor_compressed_before = DONOR.read_bytes()
    require(sha256(compressed_before) == EXPECTED_INPUT_SHA256, "SHA da cadeia limiter-only divergiu")
    require(sha256(donor_compressed_before) == EXPECTED_DONOR_SHA256, "SHA do donor Dynamics divergiu")
    xml_before = gzip.decompress(compressed_before)
    donor_xml = gzip.decompress(donor_compressed_before)
    root = ET.fromstring(xml_before)
    donor_root = ET.fromstring(donor_xml)
    require(root.tag == donor_root.tag == "PremiereData", "raiz XML inesperada")

    graph_before = numeric_graph(xml_before)
    require(graph_before["max_object_id"] == EXPECTED_INPUT_MAX_OBJECT_ID, "max ObjectID da entrada divergiu")
    # O formato Premiere admite ObjectID locais aninhados (por exemplo,
    # ProjectViewState ObjectID=1 dentro de Project ObjectID=1). A lista pode
    # existir legitimamente, mas deve permanecer exatamente igual após C1.
    existing_ids = set(re.findall(r'\bObjectID="(\d+)"', xml_before.decode("utf-8")))
    require({NEW_DYNAMICS_ID, *NEW_DYNAMICS_PARAM_REFS}.isdisjoint(existing_ids), "IDs reservados para C1 ja existem")

    track_item = top(root, "AudioClipTrackItem", TARGET_TRACK_ITEM_ID)
    owner = track_item.find("./ClipTrackItem/ComponentOwner/Components")
    require(owner is not None and owner.get("ObjectRef") == TARGET_CHAIN_ID, "track item 107 nao aponta para chain 511")
    require(track_item.find("./ClipTrackItem/HeadTransition") is None, "fade de cabeca reapareceu no audio 107")
    require(track_item.find("./ClipTrackItem/TailTransition") is None, "fade de cauda reapareceu no audio 107")

    chain = top(root, "AudioComponentChain", TARGET_CHAIN_ID)
    require(chain_refs(chain) == [LIMITER_ID], "baseline C1 nao e limiter-only")
    components = chain.find("./ComponentChain/Components")
    require(components is not None and len(components) == 1, "estrutura da chain 511 divergiu")

    subclip = top(root, "SubClip", TARGET_SUBCLIP_ID)
    require(subclip.findtext("Name") == "107_REF_E100_AUTOMATCH.mov", "fonte do subclip 107 divergiu")
    clip_ref = subclip.find("Clip")
    require(clip_ref is not None and clip_ref.get("ObjectRef") == TARGET_AUDIO_CLIP_ID, "AudioClip do 107 divergiu")

    limiter = top(root, "AudioFilterComponent", LIMITER_ID)
    require(limiter.findtext("FilterMatchName") == LIMITER_MATCH_NAME, "efeito 705 nao e limiter")
    require(component_refs(limiter) == LIMITER_PARAM_REFS, "refs do limiter divergiram")
    require(limiter.findtext("./AudioComponent/Component/InstanceName") == LIMITER_INSTANCE_NAME, "instancia do limiter divergiu")
    limiter_hash_before = sha256(ET.tostring(limiter, encoding="utf-8"))

    donor_effect = top(donor_root, "AudioFilterComponent", DONOR_DYNAMICS_ID)
    require(donor_effect.findtext("FilterMatchName") == DYNAMICS_MATCH_NAME, "donor 945 nao e Dynamics Processing")
    require(component_refs(donor_effect) == DONOR_DYNAMICS_PARAM_REFS, "refs do donor Dynamics divergiram")
    donor_params = [top(donor_root, "AudioComponentParam", ref) for ref in DONOR_DYNAMICS_PARAM_REFS]
    require([item.findtext("Name") for item in donor_params] == EXPECTED_DYNAMICS_PARAM_NAMES, "nomes/ordem dos 33 Params divergiram")
    donor_map = donor_effect.find("ChannelMapData")
    require(donor_map is not None and donor_map.text and donor_map.text.strip(), "ChannelMapData materializado do donor ausente")

    dynamics = copy.deepcopy(donor_effect)
    dynamics.set("ObjectID", NEW_DYNAMICS_ID)
    component = dynamics.find("./AudioComponent/Component")
    require(component is not None, "Component do Dynamics ausente")
    component_id = component.find("ID")
    require(component_id is not None, "ID interno do Dynamics ausente")
    component_id.text = "3"
    for tag, value in (("DisplayName", "Dynamics Processing"), ("InstanceName", DYNAMICS_INSTANCE_NAME)):
        node = component.find(tag)
        if node is None:
            node = ET.SubElement(component, tag)
        node.text = value
    for ref_node, new_ref in zip(dynamics.findall("./AudioComponent/Component/Params/Param"), NEW_DYNAMICS_PARAM_REFS):
        ref_node.set("ObjectRef", new_ref)
    require(component_refs(dynamics) == NEW_DYNAMICS_PARAM_REFS, "refs clonados do Dynamics divergiram")

    points_native = [(db_to_native(x), db_to_native(y)) for x, y in CURVE_POINTS_DB]
    point_scalars = [value for pair in points_native for value in pair]
    require(len(point_scalars) == 14, "curva C1 nativa deveria ter 14 escalares")

    # Param 0 e Bypass. Params 1..32 mapeiam Opaque[0..31].
    param_values = [
        0.0,
        TIMING_NATIVE["level_detector_attack"],
        TIMING_NATIVE["level_detector_release"],
        TIMING_NATIVE["gain_processor_attack"],
        TIMING_NATIVE["gain_processor_release"],
        TIMING_NATIVE["lookahead"],
        0.5,  # Input Gain = 0 dB
        0.5,  # Output Gain = 0 dB
        0.0,  # Low cutoff: banda completa
        1.0,  # High cutoff: banda completa
        0.0,  # Lock Filter off
        1.0,  # 7 de 7 pares
        1.0,  # Link Channels on
        1.0,  # RMS (0=Peak, 1=RMS na instancia nativa)
        0.0,  # Envelope Only off
        0.0,  # Spline Curves off
        0.0,  # Noise Gating off
        1.0,  # aba Dynamics ativa; nao afeta o DSP
        *point_scalars,
        0.0,  # Make-Up Gain = 0 dB
    ]
    require(len(param_values) == 33, f"esperados 33 valores de Params, obtidos {len(param_values)}")

    opaque, decoded_before, old_opaque_values = decode_opaque(dynamics)
    require(abs(float(old_opaque_values[32]) - 1.0) < 1e-8, "versao interna do donor divergiu")
    new_opaque_values = [fmt(value) for value in param_values[1:]] + [old_opaque_values[32]]
    require(len(new_opaque_values) == 33, "payload C1 deveria manter 33 floats")
    iterator = iter(new_opaque_values)
    decoded_after, substitutions = re.subn(
        r"<float>[^<]+</float>",
        lambda _: f"<float>{next(iterator)}</float>",
        decoded_before,
    )
    require(substitutions == 33, "reescrita do OpaqueData nao cobriu 33 floats")
    opaque.text = base64.b64encode(decoded_after.encode("utf-8")).decode("ascii")
    opaque.set("BinaryHash", str(uuid.uuid5(uuid.NAMESPACE_URL, sha256(decoded_after.encode("utf-8")))))

    new_params: list[ET.Element] = []
    for source, new_ref, value in zip(donor_params, NEW_DYNAMICS_PARAM_REFS, param_values):
        clone = copy.deepcopy(source)
        clone.set("ObjectID", new_ref)
        set_param_scalar(clone, value)
        new_params.append(clone)

    before_hashes = top_hashes(root)
    old_limiter_ref = list(components)[0]
    old_limiter_ref.set("Index", "1")
    dynamics_ref = ET.Element("Component", {"Index": "0", "ObjectRef": NEW_DYNAMICS_ID})
    components.insert(0, dynamics_ref)
    root.append(dynamics)
    for param in new_params:
        root.append(param)

    require(chain_refs(top(root, "AudioComponentChain", TARGET_CHAIN_ID)) == [NEW_DYNAMICS_ID, LIMITER_ID], "ordem final nao e Dynamics -> limiter")
    dynamics_after = top(root, "AudioFilterComponent", NEW_DYNAMICS_ID)
    limiter_after = top(root, "AudioFilterComponent", LIMITER_ID)
    require(dynamics_after.findtext("FilterMatchName") == DYNAMICS_MATCH_NAME, "Dynamics final nao persistiu")
    require(component_refs(dynamics_after) == NEW_DYNAMICS_PARAM_REFS, "refs finais do Dynamics divergiram")
    require(sha256(ET.tostring(limiter_after, encoding="utf-8")) == limiter_hash_before, "limiter foi alterado")

    # Prova de sincronismo: Params[1..32] devem ser idênticos a Opaque[0..31].
    _, decoded_final, opaque_values_final = decode_opaque(dynamics_after)
    expected_opaque = [fmt(value) for value in param_values[1:]] + ["1"]
    require([fmt(float(value)) for value in opaque_values_final] == expected_opaque, "OpaqueData final diverge dos Params")
    for param, expected in zip(new_params, param_values):
        require(param.findtext("CurrentValue") == fmt(expected), f"CurrentValue nao sincronizado: {param.findtext('Name')}")

    after_hashes = top_hashes(root)
    removed_keys = sorted(set(before_hashes) - set(after_hashes))
    added_keys = sorted(set(after_hashes) - set(before_hashes))
    changed_keys = sorted(key for key in set(before_hashes) & set(after_hashes) if before_hashes[key] != after_hashes[key])
    expected_added = sorted(
        [f"AudioFilterComponent:ObjectID={NEW_DYNAMICS_ID}"]
        + [f"AudioComponentParam:ObjectID={ref}" for ref in NEW_DYNAMICS_PARAM_REFS]
    )
    require(not removed_keys, f"objeto top-level foi removido: {removed_keys}")
    require(added_keys == expected_added, f"objetos adicionados fora da whitelist: {added_keys}")
    require(changed_keys == [f"AudioComponentChain:ObjectID={TARGET_CHAIN_ID}"], f"objetos alterados fora da whitelist: {changed_keys}")

    xml_after = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml_after)
    graph_after = numeric_graph(xml_after)
    require(
        graph_after["duplicate_object_ids"] == graph_before["duplicate_object_ids"],
        "conjunto de ObjectID locais duplicados mudou apos C1",
    )
    require(graph_after["unresolved_object_refs"] == graph_before["unresolved_object_refs"], "grafo de referencias nao resolvidas mudou")
    require(graph_after["object_id_count"] == graph_before["object_id_count"] + 34, "contagem de ObjectID nao aumentou em 34")
    require(graph_after["object_ref_count"] == graph_before["object_ref_count"] + 34, "contagem de ObjectRef nao aumentou em 34")

    compressed_after = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after, "round-trip gzip falhou")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "baseline mudou durante a operacao")
    require(sha256(DONOR.read_bytes()) == EXPECTED_DONOR_SHA256, "donor mudou durante a operacao")

    report = {
        "schema": "codex.premiere.safe-prproj-mutation.v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "C1_ABLATION_REQUIRES_PREMIERE_RESAVE_AND_REAL_EXPORT_NOT_APPROVED_PRESET",
        "operation": "add_synchronized_native_Dynamics_before_existing_limiter",
        "input": {"path": str(INPUT), "sha256": EXPECTED_INPUT_SHA256, "modified": False},
        "donor": {"path": str(DONOR), "sha256": EXPECTED_DONOR_SHA256, "effect_object_id": DONOR_DYNAMICS_ID, "modified": False},
        "output": {"path": str(OUTPUT), "sha256": sha256(compressed_after), "bytes": len(compressed_after)},
        "target": {
            "sequence": "107_REF_E100_AUTOMATCH",
            "track_item_object_id": TARGET_TRACK_ITEM_ID,
            "component_chain_object_id": TARGET_CHAIN_ID,
            "source_subclip_object_id": TARGET_SUBCLIP_ID,
            "source_audio_clip_object_id": TARGET_AUDIO_CLIP_ID,
            "chain_before": ["Hard Limiter v1"],
            "chain_after": ["Dynamics Processing C1", "Hard Limiter v1"],
            "dynamics_object_id": NEW_DYNAMICS_ID,
            "dynamics_param_object_ids": NEW_DYNAMICS_PARAM_REFS,
            "limiter_object_id": LIMITER_ID,
        },
        "dynamics": {
            "curve_points_db_pre_limiter": CURVE_POINTS_DB,
            "curve_points_native": points_native,
            "maximum_downward_attenuation_db": 10.0,
            "bypass": False,
            "timing_native_serialized": TIMING_NATIVE,
            "timing_native_scale_ms_per_unit": {
                "gain_processor_attack": 500.0,
                "gain_processor_release": 2000.0,
            },
            "timing_ms_ui": {
                "level_detector_attack": 0.0,
                "level_detector_release": 0.0,
                "gain_processor_attack": 5.0,
                "gain_processor_release": 30.0,
                "lookahead": 0.0,
            },
            "input_gain_db": 0.0,
            "output_gain_db": 0.0,
            "makeup_gain_db": 0.0,
            "detector": "RMS",
            "link_channels": True,
            "full_band": True,
            "spline": False,
            "noise_gate": False,
            "opaque_offset_map": {
                "opaque_0_to_31": "AudioComponentParam indices 1 to 32",
                "number_of_pairs": 10,
                "curve_points": [17, 30],
                "makeup_gain": 31,
                "internal_version": 32,
            },
            "opaque_sha256_before": sha256(decoded_before.encode("utf-8")),
            "opaque_sha256_after": sha256(decoded_final.encode("utf-8")),
            "opaque_and_component_params_synchronized": True,
        },
        "isolation": {
            "added_top_level_objects": added_keys,
            "changed_top_level_objects": changed_keys,
            "removed_top_level_objects": removed_keys,
            "all_other_top_level_objects_unchanged": True,
            "existing_limiter_unchanged": True,
            "source_and_timing_unchanged": True,
            "audio_head_tail_transitions_absent": True,
            "target_chain_contains_no_DeNoise_EQ_DeEsser_or_Enhance": True,
        },
        "integrity": {"gzip": True, "xml": True, "graph_before": graph_before, "graph_after": graph_after},
        "mandatory_next_steps": [
            "open and resave only this disposable project in Premiere 2026",
            "verify the native UI displays the seven curve points and exact timing values",
            "export a real 48 kHz stereo PCM24 WAV over Entire Source",
            "compare with CapCut and limiter-only baseline using the objective gates",
            "reject C1 on any phrase-level, loudness, peak, ambience, tone or sibilance regression",
        ],
        "warning": "C1 is a single real-render ablation. It is not an approved preset and must not be promoted before passing the real export gates.",
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
