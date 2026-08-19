#!/usr/bin/env python3
"""Cria C1C alterando exclusivamente GP Attack de 0.0125 para 0.010.

Validação visual nativa:
  0.005  -> 2 ms
  0.0125 -> 6 ms

Isso estabelece 500 ms por unidade nativa, com arredondamento de exibição.
Portanto 5 ms = 0.010. GP Release permanece 0.015 = 30 ms.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from corrigir_107_c1b_gp5ms_30ms import (
    DYNAMICS_MATCH_NAME,
    EXPECTED_CURVE_NATIVE,
    GuardError,
    LIMITER_MATCH_NAME,
    chain_refs,
    close,
    component_refs,
    decode_opaque,
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
RESULTS = ROOT / "premiere_audition_native_preset/analises/resultados/107_C1C_DYNAMICS_GP5MS_30MS_REAL"

INPUT = PROJECTS / "Teste_107_C1B_DYNAMICS_GP5MS_30MS_REAL_WORKING.prproj"
OUTPUT = PROJECTS / "Teste_107_C1C_DYNAMICS_GP5MS_30MS_REAL_WORKING.prproj"
MANIFEST = RESULTS / "MANIFESTO_PATCH_C1C_GP_ATTACK5MS.json"
BASELINE = PROJECTS / "Teste_107_ADAPTIVE_NR_LIGHT_WORKING.prproj"
C1_RESAVED = PROJECTS / "Teste_107_C1_DYNAMICS_REAL_WORKING.prproj"

EXPECTED_INPUT_SHA256 = "95a6d111183229ef9ff08ae940ba6b7404f6027940e12f5aed62fb4db76c87a0"
EXPECTED_BASELINE_SHA256 = "f77337dee90059541d73fb7387bd96996479465bda1697f06e35da8192579d89"
EXPECTED_C1_RESAVED_SHA256 = "8c92fd726c6bf6903426c0dbea142f3188d032b916611151643421338994c17a"

TRACK_ITEM_ID = "433"
CHAIN_ID = "511"
DYNAMICS_ID = "705"
LIMITER_ID = "706"
DYNAMICS_PARAM_REFS = [str(i) for i in range(1017, 1050)]
GP_ATTACK_PARAM_ID = "1020"
GP_RELEASE_PARAM_ID = "1021"

OLD_ATTACK_NATIVE = 0.0125
NEW_ATTACK_NATIVE = 0.010
PRESERVED_RELEASE_NATIVE = 0.015


def main() -> int:
    require(INPUT != OUTPUT, "entrada e saida nao podem coincidir")
    require(not OUTPUT.exists(), f"RECUSA DE OVERWRITE: {OUTPUT}")
    require(not MANIFEST.exists(), f"RECUSA DE OVERWRITE: {MANIFEST}")

    compressed_before = INPUT.read_bytes()
    require(sha256(compressed_before) == EXPECTED_INPUT_SHA256, "SHA do C1B divergiu")
    require(sha256(BASELINE.read_bytes()) == EXPECTED_BASELINE_SHA256, "baseline divergiu antes do patch")
    require(sha256(C1_RESAVED.read_bytes()) == EXPECTED_C1_RESAVED_SHA256, "C1 ressavado divergiu antes do patch")
    xml_before = gzip.decompress(compressed_before)
    root = ET.fromstring(xml_before)
    require(root.tag == "PremiereData", "raiz XML inesperada")

    track = top(root, "AudioClipTrackItem", TRACK_ITEM_ID)
    owner = track.find("./ClipTrackItem/ComponentOwner/Components")
    require(owner is not None and owner.get("ObjectRef") == CHAIN_ID, "track 107 nao aponta para chain 511")
    require(track.find("./ClipTrackItem/HeadTransition") is None, "fade de cabeca reapareceu")
    require(track.find("./ClipTrackItem/TailTransition") is None, "fade de cauda reapareceu")

    chain = top(root, "AudioComponentChain", CHAIN_ID)
    require(chain_refs(chain) == [DYNAMICS_ID, LIMITER_ID], "cadeia nao e Dynamics -> limiter")
    dynamics = top(root, "AudioFilterComponent", DYNAMICS_ID)
    limiter = top(root, "AudioFilterComponent", LIMITER_ID)
    require(dynamics.findtext("FilterMatchName") == DYNAMICS_MATCH_NAME, "efeito 705 nao e Dynamics")
    require(limiter.findtext("FilterMatchName") == LIMITER_MATCH_NAME, "efeito 706 nao e limiter")
    require(component_refs(dynamics) == DYNAMICS_PARAM_REFS, "refs do Dynamics divergiram")

    attack = top(root, "AudioComponentParam", GP_ATTACK_PARAM_ID)
    release = top(root, "AudioComponentParam", GP_RELEASE_PARAM_ID)
    require(attack.findtext("Name") == "Gain Processor - Attack Time", "Param 1020 nao e GP Attack")
    require(release.findtext("Name") == "Gain Processor - Release Time", "Param 1021 nao e GP Release")
    require(close(attack.findtext("CurrentValue", "nan"), OLD_ATTACK_NATIVE), "Attack C1B nao e 0.0125")
    require(close(release.findtext("CurrentValue", "nan"), PRESERVED_RELEASE_NATIVE), "Release C1B nao e 0.015")

    opaque, decoded_before, old_values = decode_opaque(dynamics)
    require(close(old_values[2], OLD_ATTACK_NATIVE), "Opaque[2] C1B nao e 0.0125")
    require(close(old_values[3], PRESERVED_RELEASE_NATIVE), "Opaque[3] C1B nao preserva Release 0.015")
    require(close(old_values[0], 0.0) and close(old_values[1], 0.0), "detector 0/0 mudou")
    require(close(old_values[4], 0.0), "lookahead zero mudou")
    require(all(close(value, expected) for value, expected in zip(old_values[17:31], EXPECTED_CURVE_NATIVE)), "curva C1B divergiu")
    require(close(old_values[11], 1.0) and close(old_values[12], 1.0), "link/RMS divergiram")
    require(close(old_values[14], 0.0) and close(old_values[15], 0.0), "spline/gate divergiram")
    require(close(old_values[31], 0.0) and close(old_values[32], 1.0), "makeup/version divergiram")

    before_hashes = top_hashes(root)
    graph_before = numeric_graph(xml_before)
    new_values = list(old_values)
    new_values[2] = fmt(NEW_ATTACK_NATIVE)
    require(new_values[:2] == old_values[:2] and new_values[3:] == old_values[3:], "Opaque mudaria fora de GP Attack")
    iterator = iter(new_values)
    decoded_after, substitutions = re.subn(
        r"<float>[^<]+</float>",
        lambda _: f"<float>{next(iterator)}</float>",
        decoded_before,
    )
    require(substitutions == 33, "reescrita do Opaque nao cobriu 33 floats")
    opaque.text = base64.b64encode(decoded_after.encode("utf-8")).decode("ascii")
    opaque.set("BinaryHash", str(uuid.uuid5(uuid.NAMESPACE_URL, sha256(decoded_after.encode("utf-8")))))
    set_param_scalar(attack, NEW_ATTACK_NATIVE)

    _, decoded_final, final_values = decode_opaque(dynamics)
    require(close(final_values[2], NEW_ATTACK_NATIVE), "Opaque final nao contem Attack 0.010")
    require(close(final_values[3], PRESERVED_RELEASE_NATIVE), "Opaque final alterou Release")
    require(close(attack.findtext("CurrentValue", "nan"), NEW_ATTACK_NATIVE), "Param Attack final divergiu")
    require(close(release.findtext("CurrentValue", "nan"), PRESERVED_RELEASE_NATIVE), "Param Release final mudou")

    after_hashes = top_hashes(root)
    require(set(before_hashes) == set(after_hashes), "skeleton top-level mudou")
    changed = sorted(key for key in before_hashes if before_hashes[key] != after_hashes[key])
    expected_changed = sorted([
        f"AudioFilterComponent:ObjectID={DYNAMICS_ID}",
        f"AudioComponentParam:ObjectID={GP_ATTACK_PARAM_ID}",
    ])
    require(changed == expected_changed, f"objetos fora da whitelist mudaram: {changed}")

    xml_after = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml_after)
    require(numeric_graph(xml_after) == graph_before, "grafo numerico mudou")
    compressed_after = gzip.compress(xml_after, compresslevel=9, mtime=0)
    require(gzip.decompress(compressed_after) == xml_after, "round-trip gzip falhou")
    require(sha256(INPUT.read_bytes()) == EXPECTED_INPUT_SHA256, "C1B mudou durante o patch")
    require(sha256(BASELINE.read_bytes()) == EXPECTED_BASELINE_SHA256, "baseline mudou durante o patch")
    require(sha256(C1_RESAVED.read_bytes()) == EXPECTED_C1_RESAVED_SHA256, "C1 ressavado mudou durante o patch")

    report = {
        "schema": "codex.premiere.safe-prproj-value-patch.v2",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "C1C_REQUIRES_PREMIERE_VISUAL_CONFIRMATION_AND_REAL_EXPORT_NOT_APPROVED_PRESET",
        "input": {"path": str(INPUT), "sha256": EXPECTED_INPUT_SHA256, "modified": False},
        "output": {"path": str(OUTPUT), "sha256": sha256(compressed_after), "bytes": len(compressed_after)},
        "visual_mapping": {
            "observations": [
                {"native": 0.005, "display_ms": 2.0},
                {"native": 0.0125, "display_ms": 6.0},
            ],
            "attack_scale_ms_per_native_unit": 500.0,
            "attack_target_ms": 5.0,
            "attack_native_before": OLD_ATTACK_NATIVE,
            "attack_native_after": NEW_ATTACK_NATIVE,
            "release_target_ms": 30.0,
            "release_native_preserved": PRESERVED_RELEASE_NATIVE,
        },
        "patch": {
            "dynamics_object_id": DYNAMICS_ID,
            "attack_param_object_id": GP_ATTACK_PARAM_ID,
            "opaque_attack_index": 2,
            "opaque_sha256_before": sha256(decoded_before.encode("utf-8")),
            "opaque_sha256_after": sha256(decoded_final.encode("utf-8")),
            "opaque_and_component_param_synchronized": True,
        },
        "preserved": {
            "release_0_015_target_30ms": True,
            "curve": True,
            "detector_0_0_ms": True,
            "lookahead_0_ms": True,
            "RMS_and_link": True,
            "spline_and_noise_gate_off": True,
            "input_output_makeup_0_db": True,
            "limiter_source_timing_and_all_other_objects": True,
            "changed_top_level_objects": changed,
            "baseline_sha256": EXPECTED_BASELINE_SHA256,
            "c1_resaved_sha256": EXPECTED_C1_RESAVED_SHA256,
        },
        "integrity": {"gzip": True, "xml": True, "numeric_graph_unchanged": True},
        "mandatory_next_steps": [
            "open and resave only C1C in Premiere 2026",
            "visually confirm GP Attack 5 ms and Release 30 ms",
            "confirm all other Dynamics values remained unchanged",
            "export the real WAV and run objective gates",
        ],
        "warning": "C1C remains an ablation and is not an approved preset.",
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
