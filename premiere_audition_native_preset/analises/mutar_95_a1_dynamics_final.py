#!/usr/bin/env python3
"""Muta exclusivamente o Dynamics do 95 A1 em uma copia descartavel."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "premiere_audition_native_preset/projetos_descartaveis/Teste_95_NORMALIZACAO_WORKING.prproj"
OUTPUT = ROOT / "premiere_audition_native_preset/projetos_descartaveis/Teste_95_PERENE_A1_FINAL_CANDIDATO.prproj"
REPORT = ROOT / "premiere_audition_native_preset/analises/MANIFESTO_MUTACAO_95_PERENE_A1_FINAL_CANDIDATO.json"
BASELINE_SHA256 = "0ddb684ed1eac47496da781150dad3b97a111657b069973f5222ac6f828ec540"
CHAIN_ID = "978"
DYNAMICS_ID = "1515"
OLD_ORDER = ["1512", "1513", "1514", "1515"]
NEW_ORDER = ["1512", "1513", "1515", "1514"]
EXPECTED_PARAM_REFS = [str(i) for i in range(3957, 3990)]
EXPECTED_FILTER = "f76dad9c-77da-477a-9939-eefefc5c23bd"
FINAL_POINTS = [(-100.0, -100.0), (-87.0, -100.0), (-36.0, -49.0), (-30.0, -30.0), (-20.0, -20.0), (0.0, 0.0)]


class GuardError(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise GuardError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def top(root: ET.Element, tag: str, attr: str, value: str) -> ET.Element:
    hits = [e for e in root if e.tag == tag and e.get(attr) == value]
    require(len(hits) == 1, f"{tag} {attr}={value}: esperado 1, obtido {len(hits)}")
    return hits[0]


def top_hashes(root: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for pos, e in enumerate(root):
        identity = next(((k, e.get(k)) for k in ("ObjectID", "ObjectUID") if e.get(k)), None)
        key = f"{e.tag}:{identity[0]}={identity[1]}" if identity else f"{e.tag}:position={pos}"
        require(key not in out, f"identidade top-level duplicada: {key}")
        out[key] = sha(ET.tostring(e, encoding="utf-8"))
    return out


def normalized(db: float) -> float:
    return (db + 100.0) / 100.0


def fmt(value: float) -> str:
    if value == 0:
        return "0"
    if value == 1:
        return "1"
    return format(value, ".9g")


def main() -> int:
    require(INPUT.parent == OUTPUT.parent, "entrada e saida devem ficar em projetos_descartaveis")
    require(not OUTPUT.exists(), f"saida ja existe; recusando sobrescrever: {OUTPUT}")
    require(not REPORT.exists(), f"manifesto ja existe; recusando sobrescrever: {REPORT}")

    compressed_before = INPUT.read_bytes()
    require(sha(compressed_before) == BASELINE_SHA256, "SHA do baseline divergiu")
    xml_before = gzip.decompress(compressed_before)
    root_before = ET.fromstring(xml_before)
    require(root_before.tag == "PremiereData", "raiz XML inesperada")

    chain = top(root_before, "AudioComponentChain", "ObjectID", CHAIN_ID)
    dynamics = top(root_before, "AudioFilterComponent", "ObjectID", DYNAMICS_ID)
    require(dynamics.findtext("FilterMatchName") == EXPECTED_FILTER, "FilterMatchName do Dynamics divergiu")
    refs = [e.get("ObjectRef") for e in dynamics.findall("./AudioComponent/Component/Params/Param")]
    require(refs == EXPECTED_PARAM_REFS, "os 33 refs de parametros do Dynamics divergiram")
    order = [e.get("ObjectRef") for e in chain.findall("./ComponentChain/Components/Component")]
    require(order == OLD_ORDER, f"ordem antiga divergiu: {order}")

    opaque = dynamics.find("OpaqueData")
    require(opaque is not None and opaque.text, "OpaqueData materializado do Dynamics ausente")
    decoded_before = base64.b64decode(opaque.text).decode("utf-8")
    old_values = re.findall(r"<float>([^<]+)</float>", decoded_before)
    require(len(old_values) == 33, f"ParamList deveria ter 33 floats, tem {len(old_values)}")
    new_values = list(old_values)
    new_values[11] = fmt(len(FINAL_POINTS) / 7.0)
    point_values: list[str] = []
    for x, y in FINAL_POINTS:
        point_values.extend((fmt(normalized(x)), fmt(normalized(y))))
    point_values.extend(("0.5", "0.5"))  # slot 7 inativo; preserva o par default.
    new_values[18:32] = point_values
    require(new_values[:11] == old_values[:11], "parametros 0..10 seriam alterados")
    require(new_values[12:18] == old_values[12:18], "parametros 12..17 seriam alterados")
    require(new_values[32] == old_values[32], "make-up gain seria alterado")

    it = iter(new_values)
    decoded_after, count = re.subn(r"<float>[^<]+</float>", lambda _: f"<float>{next(it)}</float>", decoded_before)
    require(count == 33, "substituicao do ParamList nao cobriu 33 floats")
    opaque.text = base64.b64encode(decoded_after.encode("utf-8")).decode("ascii")
    opaque.set("BinaryHash", "codex-95-a1-dynamics-final-00000406")

    components = chain.find("./ComponentChain/Components")
    require(components is not None and len(components) == 4, "Components da cadeia inesperado")
    by_ref = {e.get("ObjectRef"): e for e in list(components)}
    require(set(by_ref) == set(OLD_ORDER), "refs da cadeia inesperados")
    for e in list(components):
        components.remove(e)
    for index, ref in enumerate(NEW_ORDER):
        e = by_ref[ref]
        e.set("Index", str(index))
        components.append(e)

    xml_after = ET.tostring(root_before, encoding="utf-8", xml_declaration=True)
    root_after = ET.fromstring(xml_after)
    before_hashes = top_hashes(ET.fromstring(xml_before))
    after_hashes = top_hashes(root_after)
    require(before_hashes.keys() == after_hashes.keys(), "skeleton top-level mudou")
    changed = sorted(k for k in before_hashes if before_hashes[k] != after_hashes[k])
    expected_changed = sorted([
        f"AudioComponentChain:ObjectID={CHAIN_ID}",
        f"AudioFilterComponent:ObjectID={DYNAMICS_ID}",
    ])
    require(changed == expected_changed, f"objetos fora da whitelist foram alterados: {changed}")

    chain_after = top(root_after, "AudioComponentChain", "ObjectID", CHAIN_ID)
    dyn_after = top(root_after, "AudioFilterComponent", "ObjectID", DYNAMICS_ID)
    actual_order = [e.get("ObjectRef") for e in chain_after.findall("./ComponentChain/Components/Component")]
    actual_refs = [e.get("ObjectRef") for e in dyn_after.findall("./AudioComponent/Component/Params/Param")]
    require(actual_order == NEW_ORDER, "ordem final nao persistiu")
    require(actual_refs == EXPECTED_PARAM_REFS, "refs de parametros mudaram")
    actual_vals = re.findall(r"<float>([^<]+)</float>", base64.b64decode(dyn_after.findtext("OpaqueData")).decode())
    require(actual_vals == new_values, "payload final divergiu")

    compressed_after = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after, "round-trip gzip falhou")
    OUTPUT.write_bytes(compressed_after)
    readback = OUTPUT.read_bytes()
    require(gzip.decompress(readback) == xml_after, "readback gzip divergiu")
    ET.fromstring(gzip.decompress(readback))
    require(sha(INPUT.read_bytes()) == BASELINE_SHA256, "baseline mudou durante a operacao")

    report = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "operation": "guarded_95_A1_Dynamics_curve_and_order_only",
        "scope": "copy_only_no_gui",
        "input": {"path": str(INPUT), "sha256_before": BASELINE_SHA256, "sha256_after": sha(INPUT.read_bytes()), "unchanged": True},
        "output": {"path": str(OUTPUT), "sha256": sha(readback), "bytes": len(readback), "xml_sha256": sha(xml_after), "gzip_integrity": True, "xml_integrity": True},
        "target": {"sequence": "95_A1_AUDITION_NR_ONLY", "component_chain_object_id": CHAIN_ID, "dynamics_object_id": DYNAMICS_ID, "filter_match_name": EXPECTED_FILTER},
        "chain": {"before": OLD_ORDER, "after": actual_order, "semantic_after": ["EQ v2", "DeEsser M38", "Dynamics Processing", "Hard Limiter v1"]},
        "dynamics": {"points_db": FINAL_POINTS, "number_of_pairs": len(FINAL_POINTS), "old_paramlist_sha256": sha(decoded_before.encode()), "new_paramlist_sha256": sha(decoded_after.encode()), "preserved_parameter_indices": list(range(0, 11)) + list(range(12, 18)) + [32], "parameter_refs_preserved_exactly": True},
        "isolation": {"changed_top_level_objects": changed, "changed_top_level_object_count": len(changed), "all_non_whitelisted_top_level_objects_unchanged": True, "top_level_object_count_before": len(before_hashes), "top_level_object_count_after": len(after_hashes)},
        "limitations": ["Validacao estrutural offline; abertura e render no Premiere ainda exigem validacao separada."]
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    require(json.loads(REPORT.read_text(encoding="utf-8"))["output"]["gzip_integrity"], "manifesto invalido")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GuardError as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr)
        raise SystemExit(2)
