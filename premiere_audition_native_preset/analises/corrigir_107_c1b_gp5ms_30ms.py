#!/usr/bin/env python3
"""Cria C1B corrigindo somente Gain Processor para 5/30 ms.

Entrada: C1 ressavado e validado visualmente pelo Premiere. O programa mostrou
que os valores nativos 0.005/0.030 correspondem a 2/60 ms. Portanto:

* Gain Processor Attack: escala observada 400 ms por unidade nativa;
  5 ms -> 0.0125.
* Gain Processor Release: escala observada 2000 ms por unidade nativa;
  30 ms -> 0.015.

O patch atualiza simultaneamente OpaqueData[2:4] e os Params 3/4. Todo o resto
deve permanecer byte-equivalente no nível dos objetos XML do Premiere.
"""

from __future__ import annotations

import base64
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
RESULTS = ROOT / "premiere_audition_native_preset/analises/resultados/107_C1B_DYNAMICS_GP5MS_30MS_REAL"

INPUT = PROJECTS / "Teste_107_C1_DYNAMICS_REAL_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_107_C1B_DYNAMICS_GP5MS_30MS_REAL_WORKING.prproj"
MANIFEST = RESULTS / "MANIFESTO_PATCH_C1B_GP5MS_30MS.json"

EXPECTED_INPUT_SHA256 = "8c92fd726c6bf6903426c0dbea142f3188d032b916611151643421338994c17a"
TARGET_TRACK_ITEM_ID = "433"
TARGET_CHAIN_ID = "511"
DYNAMICS_ID = "705"
LIMITER_ID = "706"
DYNAMICS_PARAM_REFS = [str(i) for i in range(1017, 1050)]
GP_ATTACK_PARAM_ID = "1020"  # Component Param index 3 -> Opaque index 2
GP_RELEASE_PARAM_ID = "1021"  # Component Param index 4 -> Opaque index 3

DYNAMICS_MATCH_NAME = "f76dad9c-77da-477a-9939-eefefc5c23bd"
LIMITER_MATCH_NAME = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"

OLD_GP_ATTACK_NATIVE = 0.005
OLD_GP_RELEASE_NATIVE = 0.030
NEW_GP_ATTACK_NATIVE = 0.0125
NEW_GP_RELEASE_NATIVE = 0.015

EXPECTED_CURVE_NATIVE = [
    0.0,
    0.0,
    0.062,
    0.0,
    0.32,
    0.258,
    0.56,
    0.498,
    0.62,
    0.52,
    0.66,
    0.66,
    1.0,
    1.0,
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
    return [node.get("ObjectRef") for node in effect.findall("./AudioComponent/Component/Params/Param")]


def chain_refs(chain: ET.Element) -> list[str | None]:
    return [node.get("ObjectRef") for node in chain.findall("./ComponentChain/Components/Component")]


def set_param_scalar(param: ET.Element, value: float) -> None:
    start = param.find("StartKeyframe")
    current = param.find("CurrentValue")
    require(start is not None and current is not None, f"parametro sem valores materializados: {param.findtext('Name')}")
    start.text = f"-91445760000000000,{fmt(value)},0,0,0,0,0,0"
    current.text = fmt(value)


def decode_opaque(effect: ET.Element) -> tuple[ET.Element, str, list[str]]:
    opaque = effect.find("OpaqueData")
    require(opaque is not None and opaque.text and opaque.text.strip(), "OpaqueData materializado ausente")
    decoded = base64.b64decode(opaque.text).decode("utf-8")
    values = re.findall(r"<float>([^<]+)</float>", decoded)
    require(len(values) == 33, f"ParamList deveria ter 33 floats, tem {len(values)}")
    return opaque, decoded, values


def close(actual: str, expected: float, tolerance: float = 1e-7) -> bool:
    return abs(float(actual) - expected) <= tolerance


def main() -> int:
    require(INPUT != OUTPUT, "entrada e saida nao podem coincidir")
    require(not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(not MANIFEST.exists(), f"RECUSA DE OVERWRITE: {MANIFEST}")

    compressed_before = INPUT.read_bytes()
    require(sha256(compressed_before) == EXPECTED_INPUT_SHA256, "SHA do C1 ressavado divergiu")
    xml_before = gzip.decompress(compressed_before)
    root = ET.fromstring(xml_before)
    require(root.tag == "PremiereData", "raiz XML inesperada")

    track_item = top(root, "AudioClipTrackItem", TARGET_TRACK_ITEM_ID)
    owner = track_item.find("./ClipTrackItem/ComponentOwner/Components")
    require(owner is not None and owner.get("ObjectRef") == TARGET_CHAIN_ID, "track item 107 nao aponta para chain 511")
    require(track_item.find("./ClipTrackItem/HeadTransition") is None, "fade de cabeca reapareceu")
    require(track_item.find("./ClipTrackItem/TailTransition") is None, "fade de cauda reapareceu")

    chain = top(root, "AudioComponentChain", TARGET_CHAIN_ID)
    require(chain_refs(chain) == [DYNAMICS_ID, LIMITER_ID], "cadeia deixou de ser Dynamics -> limiter")
    dynamics = top(root, "AudioFilterComponent", DYNAMICS_ID)
    limiter = top(root, "AudioFilterComponent", LIMITER_ID)
    require(dynamics.findtext("FilterMatchName") == DYNAMICS_MATCH_NAME, "efeito 705 nao e Dynamics")
    require(limiter.findtext("FilterMatchName") == LIMITER_MATCH_NAME, "efeito 706 nao e limiter")
    require(component_refs(dynamics) == DYNAMICS_PARAM_REFS, "refs dos 33 parametros do Dynamics divergiram")

    attack = top(root, "AudioComponentParam", GP_ATTACK_PARAM_ID)
    release = top(root, "AudioComponentParam", GP_RELEASE_PARAM_ID)
    require(attack.findtext("Name") == "Gain Processor - Attack Time", "Param 1020 nao e GP Attack")
    require(release.findtext("Name") == "Gain Processor - Release Time", "Param 1021 nao e GP Release")
    require(close(attack.findtext("CurrentValue", "nan"), OLD_GP_ATTACK_NATIVE), "GP Attack de entrada nao e 0.005")
    require(close(release.findtext("CurrentValue", "nan"), OLD_GP_RELEASE_NATIVE), "GP Release de entrada nao e 0.030")

    opaque, decoded_before, opaque_values = decode_opaque(dynamics)
    require(close(opaque_values[2], OLD_GP_ATTACK_NATIVE), "Opaque[2] nao confirma GP Attack antigo")
    require(close(opaque_values[3], OLD_GP_RELEASE_NATIVE), "Opaque[3] nao confirma GP Release antigo")
    require(close(opaque_values[0], 0.0) and close(opaque_values[1], 0.0), "detector 0/0 mudou")
    require(close(opaque_values[4], 0.0), "lookahead zero mudou")
    require(all(close(value, expected) for value, expected in zip(opaque_values[17:31], EXPECTED_CURVE_NATIVE)), "curva C1 da entrada divergiu")
    require(close(opaque_values[11], 1.0) and close(opaque_values[12], 1.0), "link/RMS divergiram")
    require(close(opaque_values[14], 0.0) and close(opaque_values[15], 0.0), "spline/gate divergiram")
    require(close(opaque_values[31], 0.0) and close(opaque_values[32], 1.0), "makeup/version divergiram")

    before_hashes = top_hashes(root)
    graph_before = numeric_graph(xml_before)
    old_values = list(opaque_values)
    new_values = list(old_values)
    new_values[2] = fmt(NEW_GP_ATTACK_NATIVE)
    new_values[3] = fmt(NEW_GP_RELEASE_NATIVE)
    require(new_values[:2] == old_values[:2] and new_values[4:] == old_values[4:], "Opaque mudaria fora de GP Attack/Release")
    iterator = iter(new_values)
    decoded_after, substitutions = re.subn(
        r"<float>[^<]+</float>",
        lambda _: f"<float>{next(iterator)}</float>",
        decoded_before,
    )
    require(substitutions == 33, "reescrita do OpaqueData nao cobriu 33 floats")
    opaque.text = base64.b64encode(decoded_after.encode("utf-8")).decode("ascii")
    opaque.set("BinaryHash", str(uuid.uuid5(uuid.NAMESPACE_URL, sha256(decoded_after.encode("utf-8")))))
    set_param_scalar(attack, NEW_GP_ATTACK_NATIVE)
    set_param_scalar(release, NEW_GP_RELEASE_NATIVE)

    opaque_final, decoded_final, final_values = decode_opaque(dynamics)
    require(opaque_final is opaque, "OpaqueData mudou de objeto inesperadamente")
    require(close(final_values[2], NEW_GP_ATTACK_NATIVE) and close(final_values[3], NEW_GP_RELEASE_NATIVE), "Opaque final nao contem 5/30 ms nativos")
    require(close(attack.findtext("CurrentValue", "nan"), NEW_GP_ATTACK_NATIVE), "Param GP Attack final divergiu")
    require(close(release.findtext("CurrentValue", "nan"), NEW_GP_RELEASE_NATIVE), "Param GP Release final divergiu")

    after_hashes = top_hashes(root)
    require(set(before_hashes) == set(after_hashes), "skeleton top-level mudou")
    changed = sorted(key for key in before_hashes if before_hashes[key] != after_hashes[key])
    expected_changed = sorted([
        f"AudioFilterComponent:ObjectID={DYNAMICS_ID}",
        f"AudioComponentParam:ObjectID={GP_ATTACK_PARAM_ID}",
        f"AudioComponentParam:ObjectID={GP_RELEASE_PARAM_ID}",
    ])
    require(changed == expected_changed, f"objetos fora da whitelist foram alterados: {changed}")

    xml_after = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml_after)
    graph_after = numeric_graph(xml_after)
    require(graph_after == graph_before, "grafo numerico mudou no patch de valores")
    compressed_after = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after, "round-trip gzip falhou")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "C1 ressavado mudou durante a operacao")

    report = {
        "schema": "codex.premiere.safe-prproj-value-patch.v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "C1B_REQUIRES_PREMIERE_VISUAL_CONFIRMATION_AND_REAL_EXPORT_NOT_APPROVED_PRESET",
        "input": {"path": str(INPUT), "sha256": EXPECTED_INPUT_SHA256, "modified": False},
        "output": {"path": str(OUTPUT), "sha256": sha256(compressed_after), "bytes": len(compressed_after)},
        "observed_mapping": {
            "premiere_visual_before": {"gain_processor_attack_ms": 2.0, "gain_processor_release_ms": 60.0},
            "native_before": {"gain_processor_attack": OLD_GP_ATTACK_NATIVE, "gain_processor_release": OLD_GP_RELEASE_NATIVE},
            "derived_scale_ms_per_native_unit": {"gain_processor_attack": 400.0, "gain_processor_release": 2000.0},
            "target_visual_ms": {"gain_processor_attack": 5.0, "gain_processor_release": 30.0},
            "native_after": {"gain_processor_attack": NEW_GP_ATTACK_NATIVE, "gain_processor_release": NEW_GP_RELEASE_NATIVE},
        },
        "patch": {
            "dynamics_object_id": DYNAMICS_ID,
            "component_params": {"attack_object_id": GP_ATTACK_PARAM_ID, "release_object_id": GP_RELEASE_PARAM_ID},
            "opaque_indices": {"gain_processor_attack": 2, "gain_processor_release": 3},
            "opaque_sha256_before": sha256(decoded_before.encode("utf-8")),
            "opaque_sha256_after": sha256(decoded_final.encode("utf-8")),
            "opaque_and_component_params_synchronized": True,
        },
        "preserved": {
            "curve": True,
            "detector_0_0_ms": True,
            "lookahead_0_ms": True,
            "RMS_and_link": True,
            "spline_and_noise_gate_off": True,
            "input_output_makeup_0_db": True,
            "limiter": True,
            "source_timing_and_all_other_objects": True,
            "changed_top_level_objects": changed,
        },
        "integrity": {"gzip": True, "xml": True, "numeric_graph_unchanged": True},
        "mandatory_next_steps": [
            "open and resave only C1B in Premiere 2026",
            "visually confirm Gain Processor Attack 5 ms and Release 30 ms",
            "confirm every other Dynamics value remained unchanged",
            "export a real 48 kHz stereo PCM24 WAV over Entire Source",
            "run the objective gates before any preset decision",
        ],
        "warning": "C1B remains a single ablation and is not an approved preset.",
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
