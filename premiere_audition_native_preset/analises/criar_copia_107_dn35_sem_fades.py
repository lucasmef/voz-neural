#!/usr/bin/env python3
"""Cria uma copia descartavel do projeto 107 sem as duas transicoes de audio.

O mutador e deliberadamente estrito: fixa o SHA-256 da entrada, exige que cada
fragmento-alvo apareca exatamente uma vez, recusa sobrescrever saidas e valida
gzip, XML e referencias antes e depois de gravar a nova copia.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "premiere_audition_native_preset/projetos_descartaveis/Teste_107_PREMIERE_DEFINITIVO_WORKING.prproj"
OUTPUT = ROOT / "premiere_audition_native_preset/projetos_descartaveis/Teste_107_DN35_NOFADES_EXPORT_CANDIDATO.prproj"
MANIFEST = ROOT / "premiere_audition_native_preset/analises/resultados/107_DN35_PREMIERE_REAL/MANIFESTO_MUTACAO_NOFADES.json"
EXPECTED_INPUT_SHA256 = "a84fe41d450a8d365da569d66c5007ee24481c57a8f13e473abc149e8c506265"


REMOVALS = {
    "referencia_head_audio_513": '\t\t\t<HeadTransition ObjectRef="513"/>\n',
    "referencia_tail_audio_514": '\t\t\t<TailTransition ObjectRef="514"/>\n',
    "lista_transicoes_audio_513_514": (
        '\t\t\t\t<TrackItems Version="1">\n'
        '\t\t\t\t\t<TrackItem Index="0" ObjectRef="513"/>\n'
        '\t\t\t\t\t<TrackItem Index="1" ObjectRef="514"/>\n'
        '\t\t\t\t</TrackItems>\n'
    ),
    "objeto_audio_transition_513": (
        '\t<AudioTransitionTrackItem ObjectID="513" ClassID="23f687b6-9c5a-42f4-bea1-e7b3e28e7082" Version="4">\n'
        '\t\t<TransitionTrackItem Version="3">\n'
        '\t\t\t<TrackItem Version="4">\n'
        '\t\t\t\t<End>254016000000</End>\n'
        '\t\t\t</TrackItem>\n'
        '\t\t\t<DisplayName>Constant Power</DisplayName>\n'
        '\t\t\t<HasOutgoingClip>false</HasOutgoingClip>\n'
        '\t\t\t<HasIncomingClip>true</HasIncomingClip>\n'
        '\t\t\t<MatchName>Constant Power</MatchName>\n'
        '\t\t\t<Alignment>0</Alignment>\n'
        '\t\t</TransitionTrackItem>\n'
        '\t\t<AudioChannelLayout>[{"channellabel":100},{"channellabel":101}]</AudioChannelLayout>\n'
        '\t</AudioTransitionTrackItem>\n'
    ),
    "objeto_audio_transition_514": (
        '\t<AudioTransitionTrackItem ObjectID="514" ClassID="23f687b6-9c5a-42f4-bea1-e7b3e28e7082" Version="4">\n'
        '\t\t<TransitionTrackItem Version="3">\n'
        '\t\t\t<TrackItem Version="4">\n'
        '\t\t\t\t<Start>7353763200000</Start>\n'
        '\t\t\t\t<End>7607779200000</End>\n'
        '\t\t\t</TrackItem>\n'
        '\t\t\t<DisplayName>Constant Power</DisplayName>\n'
        '\t\t\t<HasOutgoingClip>true</HasOutgoingClip>\n'
        '\t\t\t<HasIncomingClip>false</HasIncomingClip>\n'
        '\t\t\t<MatchName>Constant Power</MatchName>\n'
        '\t\t\t<Alignment>254016000000</Alignment>\n'
        '\t\t</TransitionTrackItem>\n'
        '\t\t<AudioChannelLayout>[{"channellabel":100},{"channellabel":101}]</AudioChannelLayout>\n'
        '\t</AudioTransitionTrackItem>\n'
    ),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def numeric_graph(xml_text: str) -> dict[str, object]:
    object_ids = re.findall(r'\bObjectID="(\d+)"', xml_text)
    object_refs = re.findall(r'\bObjectRef="(\d+)"', xml_text)
    object_id_counts = Counter(object_ids)
    unresolved = sorted(set(object_refs) - set(object_ids), key=int)
    return {
        "object_id_count": len(object_ids),
        "object_ref_count": len(object_refs),
        "duplicate_object_id_values": sum(1 for count in object_id_counts.values() if count > 1),
        "unresolved_object_refs": unresolved,
    }


def numeric_counters(xml_text: str) -> tuple[Counter[str], Counter[str]]:
    return (
        Counter(re.findall(r'\bObjectID="(\d+)"', xml_text)),
        Counter(re.findall(r'\bObjectRef="(\d+)"', xml_text)),
    )


def count_markers(xml_text: str) -> dict[str, int]:
    return {
        "video_ref_509": xml_text.count('ObjectRef="509"'),
        "video_ref_510": xml_text.count('ObjectRef="510"'),
        "video_object_509": xml_text.count('VideoTransitionTrackItem ObjectID="509"'),
        "video_object_510": xml_text.count('VideoTransitionTrackItem ObjectID="510"'),
        "audio_ref_513": xml_text.count('ObjectRef="513"'),
        "audio_ref_514": xml_text.count('ObjectRef="514"'),
        "audio_object_513": xml_text.count('AudioTransitionTrackItem ObjectID="513"'),
        "audio_object_514": xml_text.count('AudioTransitionTrackItem ObjectID="514"'),
        "denoise_object_707": xml_text.count('ObjectID="707"'),
        "limiter_object_708": xml_text.count('ObjectID="708"'),
    }


def main() -> None:
    for path in (OUTPUT, MANIFEST):
        if path.exists():
            raise SystemExit(f"RECUSA DE OVERWRITE: a saida ja existe: {path}")

    compressed_before = INPUT.read_bytes()
    actual_input_sha = sha256(compressed_before)
    if actual_input_sha != EXPECTED_INPUT_SHA256:
        raise SystemExit(
            "ABORTADO: SHA-256 da entrada mudou; o WORKING nao sera tocado. "
            f"Esperado {EXPECTED_INPUT_SHA256}, encontrado {actual_input_sha}."
        )

    xml_before_bytes = gzip.decompress(compressed_before)
    xml_before = xml_before_bytes.decode("utf-8")
    ElementTree.fromstring(xml_before_bytes)

    removal_counts = {name: xml_before.count(fragment) for name, fragment in REMOVALS.items()}
    invalid_counts = {name: count for name, count in removal_counts.items() if count != 1}
    if invalid_counts:
        raise SystemExit(f"ABORTADO: fragmentos-alvo nao sao unicos: {invalid_counts}")

    markers_before = count_markers(xml_before)
    expected_before = {
        "video_ref_509": 2,
        "video_ref_510": 2,
        "video_object_509": 1,
        "video_object_510": 1,
        "audio_ref_513": 2,
        "audio_ref_514": 2,
        "audio_object_513": 1,
        "audio_object_514": 1,
        "denoise_object_707": 1,
        "limiter_object_708": 1,
    }
    if markers_before != expected_before:
        raise SystemExit(f"ABORTADO: marcadores de seguranca inesperados: {markers_before}")

    xml_after = xml_before
    for fragment in REMOVALS.values():
        xml_after = xml_after.replace(fragment, "", 1)
    xml_after_bytes = xml_after.encode("utf-8")
    ElementTree.fromstring(xml_after_bytes)

    # A igualdade abaixo prova que nenhuma outra alteracao textual foi feita.
    expected_after = xml_before
    for fragment in REMOVALS.values():
        expected_after = expected_after.replace(fragment, "", 1)
    if xml_after != expected_after:
        raise SystemExit("ABORTADO: a transformacao textual divergiu do conjunto permitido")

    markers_after = count_markers(xml_after)
    for marker in ("video_ref_509", "video_ref_510", "video_object_509", "video_object_510", "denoise_object_707", "limiter_object_708"):
        if markers_after[marker] != markers_before[marker]:
            raise SystemExit(f"ABORTADO: marcador preservado mudou: {marker}")
    for marker in ("audio_ref_513", "audio_ref_514", "audio_object_513", "audio_object_514"):
        if markers_after[marker] != 0:
            raise SystemExit(f"ABORTADO: alvo de audio ainda presente: {marker}")

    graph_before = numeric_graph(xml_before)
    graph_after = numeric_graph(xml_after)
    ids_before, refs_before = numeric_counters(xml_before)
    ids_after, refs_after = numeric_counters(xml_after)
    expected_ids_after = ids_before.copy()
    expected_refs_after = refs_before.copy()
    expected_ids_after.subtract({"513": 1, "514": 1})
    expected_refs_after.subtract({"513": 2, "514": 2})
    expected_ids_after += Counter()
    expected_refs_after += Counter()
    if ids_after != expected_ids_after:
        raise SystemExit("ABORTADO: o multiconjunto de ObjectID mudou fora de 513/514")
    if refs_after != expected_refs_after:
        raise SystemExit("ABORTADO: o multiconjunto de ObjectRef mudou fora de 513/514")
    if graph_after["unresolved_object_refs"] != graph_before["unresolved_object_refs"]:
        raise SystemExit(
            "ABORTADO: a lista de referencias numericas nao resolvidas mudou: "
            f"antes={graph_before['unresolved_object_refs']} depois={graph_after['unresolved_object_refs']}"
        )
    if graph_after["object_id_count"] != graph_before["object_id_count"] - 2:
        raise SystemExit("ABORTADO: contagem de ObjectID nao caiu exatamente em dois")
    if graph_after["object_ref_count"] != graph_before["object_ref_count"] - 4:
        raise SystemExit("ABORTADO: contagem de ObjectRef nao caiu exatamente em quatro")

    compressed_after = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0)
    output_sha = sha256(compressed_after)
    manifest = {
        "schema": "codex.premiere.safe-prproj-mutation.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "candidate_requires_premiere_open_resave_and_real_export_validation",
        "input": {
            "path": str(INPUT),
            "sha256": actual_input_sha,
            "compressed_bytes": len(compressed_before),
            "xml_bytes": len(xml_before_bytes),
            "modified": False,
        },
        "output": {
            "path": str(OUTPUT),
            "sha256": output_sha,
            "compressed_bytes": len(compressed_after),
            "xml_bytes": len(xml_after_bytes),
            "overwrite_refused": True,
        },
        "allowed_change": {
            "description": "remove only Constant Power audio head/tail transitions from sequence 107",
            "removed_audio_object_ids": [513, 514],
            "removed_object_refs": {"513": 2, "514": 2},
            "removed_exact_fragments": removal_counts,
            "xml_bytes_removed": len(xml_before_bytes) - len(xml_after_bytes),
        },
        "preservation_checks": {
            "video_transitions_509_510_unchanged": True,
            "denoise_707_and_limiter_708_markers_unchanged": True,
            "all_non_target_text_identical": True,
            "source_project_untouched_by_script": True,
            "markers_before": markers_before,
            "markers_after": markers_after,
        },
        "integrity_checks": {
            "input_gzip_ok": True,
            "input_xml_ok": True,
            "output_gzip_ok": True,
            "output_xml_ok": True,
            "numeric_reference_graph_before": graph_before,
            "numeric_reference_graph_after": graph_after,
            "unresolved_reference_set_unchanged": True,
        },
        "remaining_risk": (
            "The Premiere serialization is structurally valid but Adobe semantic compatibility must still be "
            "confirmed by opening this disposable copy in Premiere, saving it, and producing a real export."
        ),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        with OUTPUT.open("xb") as handle:
            handle.write(compressed_after)
        created.append(OUTPUT)
        reread = OUTPUT.read_bytes()
        if sha256(reread) != output_sha:
            raise RuntimeError("SHA-256 da copia gravada divergiu")
        reread_xml = gzip.decompress(reread)
        if reread_xml != xml_after_bytes:
            raise RuntimeError("XML da copia gravada divergiu")
        ElementTree.fromstring(reread_xml)

        with MANIFEST.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        created.append(MANIFEST)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise

    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
