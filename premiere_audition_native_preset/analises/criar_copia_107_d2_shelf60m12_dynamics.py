#!/usr/bin/env python3
"""Cria a ablação 107 D2: shelf mínimo -> Dynamics D2 -> limiter v1.

O mutador lê apenas projetos descartáveis do workspace, recusa sobrescrever
qualquer saída e valida uma whitelist completa de objetos top-level. O EQ e o
Dynamics são clonados de instâncias nativas reais do Premiere; OpaqueData e os
AudioComponentParam correspondentes são atualizados em conjunto.

Esta cópia é somente uma candidata para regravação e exportação real no
Premiere. Ela não cria nem aprova um preset definitivo.
"""

from __future__ import annotations

import base64
import copy
import gzip
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from criar_copia_107_c1_dynamics import (
    EXPECTED_DYNAMICS_PARAM_NAMES,
    GuardError,
    chain_refs,
    component_refs,
    db_to_native,
    fmt,
    numeric_graph,
    require,
    set_param_scalar,
    sha256,
    top,
    top_hashes,
)


ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT / "premiere_audition_native_preset/projetos_descartaveis"
RESULTS = ROOT / "premiere_audition_native_preset/analises/resultados/107_D2_SHELF60M12_DYNAMICS_REAL"

INPUT = PROJECTS / "Teste_107_ADAPTIVE_NR_LIGHT_WORKING.prproj"
EQ_DONOR = PROJECTS / "Teste_107_PREMIERE_DEFINITIVO_BASELINE_20260813.prproj"
DYNAMICS_DONOR = PROJECTS / "Teste_95_NORMALIZACAO_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_107_D2_SHELF60M12_DYNAMICS_REAL_WORKING.prproj"
MANIFEST = RESULTS / "MANIFESTO_MUTACAO_D2_SHELF60M12.json"

EXPECTED_INPUT_SHA256 = "f77337dee90059541d73fb7387bd96996479465bda1697f06e35da8192579d89"
EXPECTED_EQ_DONOR_SHA256 = "b95805827134046929a3dac6154eba5e9908fcf3b2a1d0870a7680dd775bbac2"
EXPECTED_DYNAMICS_DONOR_SHA256 = "0ddb684ed1eac47496da781150dad3b97a111657b069973f5222ac6f828ec540"
EXPECTED_INPUT_MAX_OBJECT_ID = 3399

TARGET_TRACK_ITEM_ID = "433"
TARGET_CHAIN_ID = "511"
TARGET_SUBCLIP_ID = "512"
TARGET_AUDIO_CLIP_ID = "706"
LIMITER_ID = "705"
LIMITER_PARAM_REFS = [str(i) for i in range(1016, 1024)]

EQ_DONOR_ID = "667"
EQ_DONOR_PARAM_REFS = [str(i) for i in range(1027, 1073)]
DYNAMICS_DONOR_ID = "945"
DYNAMICS_DONOR_PARAM_REFS = [str(i) for i in range(1329, 1362)]

NEW_EQ_ID = "3400"
NEW_EQ_PARAM_REFS = [str(i) for i in range(3401, 3447)]
NEW_DYNAMICS_ID = "3447"
NEW_DYNAMICS_PARAM_REFS = [str(i) for i in range(3448, 3481)]

EQ_MATCH_NAME = "8edcd326-33de-4a4d-985b-f1bf48ed1427"
DYNAMICS_MATCH_NAME = "f76dad9c-77da-477a-9939-eefefc5c23bd"
LIMITER_MATCH_NAME = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
LIMITER_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1"
EQ_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_D2_EQ_SHELF60_M12_REAL"
DYNAMICS_INSTANCE_NAME = "DJI_FEMININA_CAPCUT_D2_DYNAMICS_REAL"

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

D2_CURVE_POINTS_DB = [
    (-100.0, -100.0),
    (-70.0, -70.0),
    (-48.0, -48.0),
    (-42.0, -46.0),
    (-38.0, -46.0),
    (-34.0, -34.0),
    (0.0, 0.0),
]

DYNAMICS_TIMING_NATIVE = {
    "level_detector_attack": 0.0,
    "level_detector_release": 0.0,
    "gain_processor_attack": 0.010,
    "gain_processor_release": 0.015,
    "lookahead": 0.0,
}

# O doador nativo usa 20 Hz como mínimo e 24 kHz como limite do controle.
# Seu float32 materializado corresponde a 60 Hz na interface.
LOW_SHELF_60HZ_NATIVE = 0.0016680567059665918
LOW_SHELF_MINUS12DB_NATIVE = 0.25


def decode_opaque(effect: ET.Element) -> tuple[ET.Element, str, list[str]]:
    """Decodifica ParamList sem presumir a quantidade de parâmetros."""
    opaque = effect.find("OpaqueData")
    require(opaque is not None and opaque.text and opaque.text.strip(), "OpaqueData materializado ausente")
    decoded = base64.b64decode(opaque.text).decode("utf-8")
    values = re.findall(r"<float>([^<]+)</float>", decoded)
    require(values, "ParamList do OpaqueData nao contem floats")
    return opaque, decoded, values


def rewrite_opaque(effect: ET.Element, param_values: list[float], expected_count: int) -> dict[str, object]:
    """Sincroniza Params[1:] com Opaque[:-1], preservando a versão interna."""
    opaque, decoded_before, old_values = decode_opaque(effect)
    require(len(old_values) == expected_count, f"Opaque deveria ter {expected_count} floats")
    require(len(param_values) == expected_count, f"lista deveria ter {expected_count} Params")
    require(abs(float(old_values[-1]) - 1.0) < 1e-8, "versao interna do efeito divergiu")
    new_values = [fmt(value) for value in param_values[1:]] + [fmt(float(old_values[-1]))]
    require(len(new_values) == expected_count, "payload Opaque perdeu elementos")
    iterator = iter(new_values)
    decoded_after, substitutions = re.subn(
        r"<float>[^<]+</float>",
        lambda _: f"<float>{next(iterator)}</float>",
        decoded_before,
    )
    require(substitutions == expected_count, "reescrita do Opaque nao cobriu todos os floats")
    opaque.text = base64.b64encode(decoded_after.encode("utf-8")).decode("ascii")
    opaque.set("BinaryHash", str(uuid.uuid5(uuid.NAMESPACE_URL, sha256(decoded_after.encode("utf-8")))))
    return {
        "before": decoded_before,
        "after": decoded_after,
        "old_values": old_values,
        "new_values": new_values,
    }


def clone_effect(
    donor_effect: ET.Element,
    donor_params: list[ET.Element],
    new_effect_id: str,
    new_param_refs: list[str],
    instance_name: str,
    component_id: str,
    param_values: list[float],
) -> tuple[ET.Element, list[ET.Element], dict[str, object]]:
    require(len(donor_params) == len(new_param_refs) == len(param_values), "clone com contagens divergentes")
    effect = copy.deepcopy(donor_effect)
    effect.set("ObjectID", new_effect_id)
    component = effect.find("./AudioComponent/Component")
    require(component is not None, "Component do efeito ausente")
    internal_id = component.find("ID")
    require(internal_id is not None, "ID interno do efeito ausente")
    internal_id.text = component_id
    instance = component.find("InstanceName")
    if instance is None:
        instance = ET.SubElement(component, "InstanceName")
    instance.text = instance_name
    for ref_node, new_ref in zip(effect.findall("./AudioComponent/Component/Params/Param"), new_param_refs):
        ref_node.set("ObjectRef", new_ref)
    require(component_refs(effect) == new_param_refs, "refs clonadas do efeito divergiram")

    opaque_audit = rewrite_opaque(effect, param_values, len(param_values))
    new_params: list[ET.Element] = []
    for source, new_ref, value in zip(donor_params, new_param_refs, param_values):
        clone = copy.deepcopy(source)
        clone.set("ObjectID", new_ref)
        set_param_scalar(clone, value)
        new_params.append(clone)
    return effect, new_params, opaque_audit


def validate_d2_curve() -> None:
    require(len(D2_CURVE_POINTS_DB) == 7, "D2 deve ter sete pares")
    require(D2_CURVE_POINTS_DB[0] == (-100.0, -100.0), "D2 deve iniciar em (-100,-100)")
    require(D2_CURVE_POINTS_DB[-1] == (0.0, 0.0), "D2 deve terminar em (0,0)")
    xs = [x for x, _ in D2_CURVE_POINTS_DB]
    ys = [y for _, y in D2_CURVE_POINTS_DB]
    require(all(right > left for left, right in zip(xs, xs[1:])), "X da D2 nao cresce estritamente")
    require(all(right >= left for left, right in zip(ys, ys[1:])), "Y da D2 nao e monotono")
    require(all(y <= x + 1e-9 for x, y in D2_CURVE_POINTS_DB), "D2 contem ganho ascendente")
    require(max(x - y for x, y in D2_CURVE_POINTS_DB) == 8.0, "atenuacao maxima D2 divergiu")


def main() -> int:
    validate_d2_curve()
    require(INPUT != OUTPUT, "entrada e saida nao podem coincidir")
    require(INPUT.parent == OUTPUT.parent == PROJECTS, "entrada/saida fora de projetos_descartaveis")
    require(not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(not MANIFEST.exists(), f"RECUSA DE OVERWRITE: {MANIFEST}")

    compressed_before = INPUT.read_bytes()
    eq_donor_compressed = EQ_DONOR.read_bytes()
    dynamics_donor_compressed = DYNAMICS_DONOR.read_bytes()
    require(sha256(compressed_before) == EXPECTED_INPUT_SHA256, "SHA da entrada limiter-only divergiu")
    require(sha256(eq_donor_compressed) == EXPECTED_EQ_DONOR_SHA256, "SHA do doador EQ divergiu")
    require(sha256(dynamics_donor_compressed) == EXPECTED_DYNAMICS_DONOR_SHA256, "SHA do doador Dynamics divergiu")

    xml_before = gzip.decompress(compressed_before)
    root = ET.fromstring(xml_before)
    eq_donor_root = ET.fromstring(gzip.decompress(eq_donor_compressed))
    dynamics_donor_root = ET.fromstring(gzip.decompress(dynamics_donor_compressed))
    require(root.tag == eq_donor_root.tag == dynamics_donor_root.tag == "PremiereData", "raiz XML inesperada")

    graph_before = numeric_graph(xml_before)
    require(graph_before["max_object_id"] == EXPECTED_INPUT_MAX_OBJECT_ID, "max ObjectID da entrada divergiu")
    existing_ids = set(re.findall(r'\bObjectID="(\d+)"', xml_before.decode("utf-8")))
    reserved_ids = {NEW_EQ_ID, NEW_DYNAMICS_ID, *NEW_EQ_PARAM_REFS, *NEW_DYNAMICS_PARAM_REFS}
    require(reserved_ids.isdisjoint(existing_ids), "IDs reservados para D2 ja existem")

    track_item = top(root, "AudioClipTrackItem", TARGET_TRACK_ITEM_ID)
    owner = track_item.find("./ClipTrackItem/ComponentOwner/Components")
    require(owner is not None and owner.get("ObjectRef") == TARGET_CHAIN_ID, "track item 107 nao aponta para chain 511")
    require(track_item.find("./ClipTrackItem/HeadTransition") is None, "fade de cabeca reapareceu")
    require(track_item.find("./ClipTrackItem/TailTransition") is None, "fade de cauda reapareceu")

    chain = top(root, "AudioComponentChain", TARGET_CHAIN_ID)
    require(chain_refs(chain) == [LIMITER_ID], "entrada D2 nao e limiter-only")
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

    eq_donor = top(eq_donor_root, "AudioFilterComponent", EQ_DONOR_ID)
    require(eq_donor.findtext("FilterMatchName") == EQ_MATCH_NAME, "doador 667 nao e Parametric Equalizer")
    require(component_refs(eq_donor) == EQ_DONOR_PARAM_REFS, "refs do doador EQ divergiram")
    eq_donor_params = [top(eq_donor_root, "AudioComponentParam", ref) for ref in EQ_DONOR_PARAM_REFS]
    require([item.findtext("Name") for item in eq_donor_params] == EXPECTED_EQ_PARAM_NAMES, "nomes/ordem dos 46 Params EQ divergiram")
    eq_opaque_node, _, eq_donor_opaque_values = decode_opaque(eq_donor)
    require(eq_opaque_node.text is not None and len(eq_donor_opaque_values) == 46, "Opaque EQ do doador nao esta materializado")

    # Params[1..45] vêm do Opaque[0..44]. Opaque[45] é a versão interna.
    eq_values = [0.0] + [float(value) for value in eq_donor_opaque_values[:-1]]
    require(len(eq_values) == 46, "lista inicial do EQ nao tem 46 valores")
    eq_values[0] = 0.0  # bypass externo off
    eq_values[1] = LOW_SHELF_60HZ_NATIVE
    eq_values[2] = LOW_SHELF_MINUS12DB_NATIVE
    eq_values[4] = 0.5  # ganho high shelf neutro, embora desligado
    for gain_index in (7, 11, 15, 19, 23):
        eq_values[gain_index] = 0.5
    for enable_index in (8, 12, 16, 20, 24):
        eq_values[enable_index] = 0.0
    require(abs(eq_values[25] - 0.66666698455810547) < 1e-8, "ganho de saida 0 dB do doador divergiu")
    eq_values[26] = 0.0  # ConstantQ false = modo Constant Width
    eq_values[30] = 1.0  # low shelf on
    eq_values[31] = 0.0  # high shelf off
    eq_values[38] = 0.0  # HP off
    eq_values[41] = 0.0  # LP off
    eq_values[44] = 0.0  # bypass interno off
    require(eq_values[27] == 1.0, "ordem do low shelf do doador divergiu")
    require(eq_values[29] == 0.0, "Ultra-Quiet do doador divergiu")
    require(abs((20.0 + 23980.0 * eq_values[1]) - 60.0) < 1e-3, "frequencia nativa nao corresponde a 60 Hz")
    require(abs((-24.0 + 48.0 * eq_values[2]) - (-12.0)) < 1e-9, "ganho nativo nao corresponde a -12 dB")

    dynamics_donor = top(dynamics_donor_root, "AudioFilterComponent", DYNAMICS_DONOR_ID)
    require(dynamics_donor.findtext("FilterMatchName") == DYNAMICS_MATCH_NAME, "doador 945 nao e Dynamics Processing")
    require(component_refs(dynamics_donor) == DYNAMICS_DONOR_PARAM_REFS, "refs do doador Dynamics divergiram")
    dynamics_donor_params = [top(dynamics_donor_root, "AudioComponentParam", ref) for ref in DYNAMICS_DONOR_PARAM_REFS]
    require([item.findtext("Name") for item in dynamics_donor_params] == EXPECTED_DYNAMICS_PARAM_NAMES, "nomes/ordem dos 33 Params Dynamics divergiram")
    dynamics_map = dynamics_donor.find("ChannelMapData")
    require(dynamics_map is not None and dynamics_map.text and dynamics_map.text.strip(), "ChannelMapData do Dynamics ausente")

    points_native = [(db_to_native(x), db_to_native(y)) for x, y in D2_CURVE_POINTS_DB]
    point_scalars = [value for pair in points_native for value in pair]
    dynamics_values = [
        0.0,
        DYNAMICS_TIMING_NATIVE["level_detector_attack"],
        DYNAMICS_TIMING_NATIVE["level_detector_release"],
        DYNAMICS_TIMING_NATIVE["gain_processor_attack"],
        DYNAMICS_TIMING_NATIVE["gain_processor_release"],
        DYNAMICS_TIMING_NATIVE["lookahead"],
        0.5,
        0.5,
        0.0,
        1.0,
        0.0,
        1.0,
        1.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        *point_scalars,
        0.0,
    ]
    require(len(dynamics_values) == 33, "Dynamics D2 deveria ter 33 Params")

    eq, eq_params, eq_opaque_audit = clone_effect(
        eq_donor,
        eq_donor_params,
        NEW_EQ_ID,
        NEW_EQ_PARAM_REFS,
        EQ_INSTANCE_NAME,
        "4",
        eq_values,
    )
    dynamics, dynamics_params, dynamics_opaque_audit = clone_effect(
        dynamics_donor,
        dynamics_donor_params,
        NEW_DYNAMICS_ID,
        NEW_DYNAMICS_PARAM_REFS,
        DYNAMICS_INSTANCE_NAME,
        "3",
        dynamics_values,
    )

    before_hashes = top_hashes(root)
    limiter_ref = list(components)[0]
    limiter_ref.set("Index", "2")
    components.insert(0, ET.Element("Component", {"Index": "0", "ObjectRef": NEW_EQ_ID}))
    components.insert(1, ET.Element("Component", {"Index": "1", "ObjectRef": NEW_DYNAMICS_ID}))
    root.append(eq)
    for param in eq_params:
        root.append(param)
    root.append(dynamics)
    for param in dynamics_params:
        root.append(param)

    require(chain_refs(top(root, "AudioComponentChain", TARGET_CHAIN_ID)) == [NEW_EQ_ID, NEW_DYNAMICS_ID, LIMITER_ID], "ordem final nao e EQ -> Dynamics -> limiter")
    require(top(root, "AudioFilterComponent", NEW_EQ_ID).findtext("FilterMatchName") == EQ_MATCH_NAME, "EQ final nao persistiu")
    require(top(root, "AudioFilterComponent", NEW_DYNAMICS_ID).findtext("FilterMatchName") == DYNAMICS_MATCH_NAME, "Dynamics final nao persistiu")
    require(sha256(ET.tostring(top(root, "AudioFilterComponent", LIMITER_ID), encoding="utf-8")) == limiter_hash_before, "limiter foi alterado")

    _, _, eq_opaque_final = decode_opaque(top(root, "AudioFilterComponent", NEW_EQ_ID))
    _, _, dynamics_opaque_final = decode_opaque(top(root, "AudioFilterComponent", NEW_DYNAMICS_ID))
    expected_eq_opaque = [fmt(value) for value in eq_values[1:]] + ["1"]
    expected_dynamics_opaque = [fmt(value) for value in dynamics_values[1:]] + ["1"]
    require([fmt(float(value)) for value in eq_opaque_final] == expected_eq_opaque, "Opaque EQ final diverge dos Params")
    require([fmt(float(value)) for value in dynamics_opaque_final] == expected_dynamics_opaque, "Opaque Dynamics final diverge dos Params")
    for param, expected in zip(eq_params, eq_values):
        require(param.findtext("CurrentValue") == fmt(expected), f"EQ CurrentValue nao sincronizado: {param.findtext('Name')}")
    for param, expected in zip(dynamics_params, dynamics_values):
        require(param.findtext("CurrentValue") == fmt(expected), f"Dynamics CurrentValue nao sincronizado: {param.findtext('Name')}")

    after_hashes = top_hashes(root)
    removed_keys = sorted(set(before_hashes) - set(after_hashes))
    added_keys = sorted(set(after_hashes) - set(before_hashes))
    changed_keys = sorted(key for key in set(before_hashes) & set(after_hashes) if before_hashes[key] != after_hashes[key])
    expected_added = sorted(
        [f"AudioFilterComponent:ObjectID={NEW_EQ_ID}", f"AudioFilterComponent:ObjectID={NEW_DYNAMICS_ID}"]
        + [f"AudioComponentParam:ObjectID={ref}" for ref in NEW_EQ_PARAM_REFS]
        + [f"AudioComponentParam:ObjectID={ref}" for ref in NEW_DYNAMICS_PARAM_REFS]
    )
    require(not removed_keys, f"objeto top-level removido: {removed_keys}")
    require(added_keys == expected_added, f"objetos adicionados fora da whitelist: {added_keys}")
    require(changed_keys == [f"AudioComponentChain:ObjectID={TARGET_CHAIN_ID}"], f"objetos alterados fora da whitelist: {changed_keys}")

    xml_after = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml_after)
    graph_after = numeric_graph(xml_after)
    require(graph_after["duplicate_object_ids"] == graph_before["duplicate_object_ids"], "duplicatas de ObjectID mudaram")
    require(graph_after["unresolved_object_refs"] == graph_before["unresolved_object_refs"], "refs nao resolvidas mudaram")
    require(graph_after["object_id_count"] == graph_before["object_id_count"] + 81, "ObjectID nao aumentou em 81")
    require(graph_after["object_ref_count"] == graph_before["object_ref_count"] + 81, "ObjectRef nao aumentou em 81")

    compressed_after = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after, "round-trip gzip falhou")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "entrada mudou durante a operacao")
    require(sha256(EQ_DONOR.read_bytes()) == EXPECTED_EQ_DONOR_SHA256, "doador EQ mudou durante a operacao")
    require(sha256(DYNAMICS_DONOR.read_bytes()) == EXPECTED_DYNAMICS_DONOR_SHA256, "doador Dynamics mudou durante a operacao")

    report = {
        "schema": "codex.premiere.safe-prproj-mutation.v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "D2_SHELF_ABLATION_REQUIRES_PREMIERE_RESAVE_UI_CHECK_AND_REAL_EXPORT_NOT_APPROVED_PRESET",
        "operation": "add_synchronized_native_ParametricEQ_shelf60m12_and_Dynamics_D2_before_existing_limiter",
        "input": {"path": str(INPUT), "sha256": EXPECTED_INPUT_SHA256, "modified": False},
        "donors": [
            {"path": str(EQ_DONOR), "sha256": EXPECTED_EQ_DONOR_SHA256, "effect_object_id": EQ_DONOR_ID, "modified": False},
            {"path": str(DYNAMICS_DONOR), "sha256": EXPECTED_DYNAMICS_DONOR_SHA256, "effect_object_id": DYNAMICS_DONOR_ID, "modified": False},
        ],
        "output": {"path": str(OUTPUT), "sha256": sha256(compressed_after), "bytes": len(compressed_after)},
        "target": {
            "sequence": "107_REF_E100_AUTOMATCH",
            "track_item_object_id": TARGET_TRACK_ITEM_ID,
            "component_chain_object_id": TARGET_CHAIN_ID,
            "source_subclip_object_id": TARGET_SUBCLIP_ID,
            "source_audio_clip_object_id": TARGET_AUDIO_CLIP_ID,
            "chain_before": ["Hard Limiter v1"],
            "chain_after": ["Parametric Equalizer shelf 60 Hz -12 dB", "Dynamics Processing D2", "Hard Limiter v1"],
            "eq_object_id": NEW_EQ_ID,
            "eq_param_object_ids": NEW_EQ_PARAM_REFS,
            "dynamics_object_id": NEW_DYNAMICS_ID,
            "dynamics_param_object_ids": NEW_DYNAMICS_PARAM_REFS,
            "limiter_object_id": LIMITER_ID,
        },
        "parametric_equalizer": {
            "bypass": False,
            "low_shelf": {"enabled": True, "frequency_hz": 60.0, "gain_db": -12.0, "frequency_native": eq_values[1], "gain_native": eq_values[2], "filter_order_native_preserved_from_donor": eq_values[27]},
            "high_shelf_enabled": False,
            "bell_bands_enabled": [False, False, False, False, False],
            "high_pass_enabled": False,
            "low_pass_enabled": False,
            "output_gain_db": 0.0,
            "output_gain_native_preserved_from_zero_db_donor": eq_values[25],
            "mode": "Constant Width",
            "constant_q_native": eq_values[26],
            "opaque_offset_map": {"opaque_0_to_44": "AudioComponentParam indices 1 to 45", "internal_version": 45},
            "opaque_sha256_before": sha256(str(eq_opaque_audit["before"]).encode("utf-8")),
            "opaque_sha256_after": sha256(str(eq_opaque_audit["after"]).encode("utf-8")),
            "opaque_and_component_params_synchronized": True,
        },
        "dynamics": {
            "curve_points_db_pre_limiter": D2_CURVE_POINTS_DB,
            "curve_points_native": points_native,
            "maximum_downward_attenuation_db": 8.0,
            "bypass": False,
            "timing_native_serialized": DYNAMICS_TIMING_NATIVE,
            "timing_native_scale_ms_per_unit": {"gain_processor_attack": 500.0, "gain_processor_release": 2000.0},
            "timing_ms_ui_expected": {"level_detector_attack": 0.0, "level_detector_release": 0.0, "gain_processor_attack": 5.0, "gain_processor_release": 30.0, "lookahead": 0.0},
            "input_gain_db": 0.0,
            "output_gain_db": 0.0,
            "makeup_gain_db": 0.0,
            "detector": "RMS",
            "link_channels": True,
            "full_band": True,
            "spline": False,
            "noise_gate": False,
            "opaque_offset_map": {"opaque_0_to_31": "AudioComponentParam indices 1 to 32", "number_of_pairs": 10, "curve_points": [17, 30], "makeup_gain": 31, "internal_version": 32},
            "opaque_sha256_before": sha256(str(dynamics_opaque_audit["before"]).encode("utf-8")),
            "opaque_sha256_after": sha256(str(dynamics_opaque_audit["after"]).encode("utf-8")),
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
            "target_chain_contains_no_DeNoise_DeReverb_DeEsser_or_Enhance": True,
        },
        "offline_proof_limits": [
            "The XML proves ConstantQ=0, but the exact Premiere UI label 'Constant Width' still requires visual confirmation after resave.",
            "Low Shelf Filter Order=1 is preserved from a real GUI-saved donor; its UI label and audible slope are not independently provable offline.",
            "The numeric mappings for 60 Hz, -12 dB, zero EQ output, and D2 timings must be confirmed in Premiere before export.",
            "No offline XML check can prove the rendered DSP result; a real 48 kHz PCM24 export is mandatory.",
        ],
        "integrity": {"gzip": True, "xml": True, "graph_before": graph_before, "graph_after": graph_after},
        "mandatory_next_steps": [
            "open and resave only this disposable project in Premiere 2026",
            "visually verify shelf 60 Hz -12 dB, all other EQ bands off, Constant Width and output 0 dB",
            "visually verify the seven D2 curve points, timing, RMS/link/full-band, spline/gate/makeup off",
            "export a real 48 kHz stereo PCM24 WAV over Entire Source",
            "compare with CapCut and limiter-only baseline using objective gates",
            "reject D2 on any phrase-level, loudness, peak, ambience, tone or sibilance regression",
        ],
        "warning": "D2 shelf is one disposable real-render ablation, not an approved or definitive preset.",
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
