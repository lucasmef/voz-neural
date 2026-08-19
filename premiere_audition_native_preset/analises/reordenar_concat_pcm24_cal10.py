#!/usr/bin/env python3
"""Reordene, sem DSP, o primeiro concat EQV2 de calibracao DJI.

Este helper e deliberadamente fechado ao caso comprovado no projeto Premiere:

* ordem de origem: 84,81,76,74,72,71,68,67,65,66;
* ordem de destino: 65,66,67,68,71,72,74,76,81,84.

Os limites usam as contagens congeladas de quadros dos crus. Antes de montar a
saida, cada trecho precisa reconhecer seu proprio cru por fingerprint temporal.
Os payloads PCM24 sao copiados em blocos, sem decodificacao, ganho, DSP ou
reamostragem. Depois, o programa relê a saida e exige igualdade SHA-256 de cada
payload antes/depois. O arquivo de entrada nunca e alterado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Mapping

from validar_concats_finais import (
    CALIBRATION10,
    EXPECTED_SAMPLE_RATE,
    READ_CHUNK_FRAMES,
    SuiteSpec,
    file_sha256,
    inspect_wave,
    payload_sha256_and_frames,
    prepare_segments,
    prove_order,
    require_candidate_format,
    validate_raw,
    write_json_exclusive,
)


SOURCE_ORDER = ("84", "81", "76", "74", "72", "71", "68", "67", "65", "66")
TARGET_ORDER = CALIBRATION10.order


def clip_map() -> dict[str, Any]:
    return {clip.clip_id: clip for clip in CALIBRATION10.clips}


def source_suite() -> SuiteSpec:
    clips = clip_map()
    return SuiteSpec(
        name="calibracao10_ordem_export_original",
        clips=tuple(clips[clip_id] for clip_id in SOURCE_ORDER),
        blind=False,
    )


def payload_hash_from_stream(
    source: wave.Wave_read,
    frame_bytes: int,
    frames: int,
    role: str,
) -> str:
    remaining = frames
    digest = hashlib.sha256()
    while remaining:
        requested = min(remaining, READ_CHUNK_FRAMES)
        payload = source.readframes(requested)
        if len(payload) % frame_bytes:
            raise ValueError(f"{role}: frame PCM parcial")
        received = len(payload) // frame_bytes
        if received != requested:
            raise ValueError(
                f"{role}: esperados {requested} frames, recebidos {received}"
            )
        digest.update(payload)
        remaining -= received
    return digest.hexdigest()


def copy_segment_payload(
    segment: Path,
    sink: wave.Wave_write,
    expected_frames: int,
) -> str:
    info = inspect_wave(segment, "segmento temporario")
    require_candidate_format(info, "segmento temporario")
    if info.frames != expected_frames:
        raise ValueError(
            f"segmento temporario deveria ter {expected_frames} frames; "
            f"recebidos {info.frames}"
        )
    digest = hashlib.sha256()
    actual_frames = 0
    with wave.open(str(segment), "rb") as source:
        while True:
            payload = source.readframes(READ_CHUNK_FRAMES)
            if not payload:
                break
            if len(payload) % info.frame_bytes:
                raise ValueError("segmento temporario contem frame parcial")
            actual_frames += len(payload) // info.frame_bytes
            digest.update(payload)
            sink.writeframesraw(payload)
    if actual_frames != expected_frames:
        raise ValueError("contagem mudou durante a copia lossless")
    return digest.hexdigest()


def write_reordered_output(
    segment_paths: Mapping[str, Path],
    destination: Path,
) -> dict[str, str]:
    clips = clip_map()
    hashes: dict[str, str] = {}
    with wave.open(str(destination), "wb") as sink:
        sink.setnchannels(2)
        sink.setsampwidth(3)
        sink.setframerate(EXPECTED_SAMPLE_RATE)
        sink.setcomptype("NONE", "not compressed")
        for clip_id in TARGET_ORDER:
            hashes[clip_id] = copy_segment_payload(
                segment_paths[clip_id], sink, clips[clip_id].expected_frames
            )
    return hashes


def verify_reordered_output(
    path: Path,
    before_hashes: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    clips = clip_map()
    info = inspect_wave(path, "concat reordenado")
    require_candidate_format(info, "concat reordenado")
    expected_total = sum(clips[clip_id].expected_frames for clip_id in TARGET_ORDER)
    if info.frames != expected_total:
        raise ValueError(
            f"saida tem {info.frames} frames; esperados {expected_total}"
        )

    after_hashes: dict[str, str] = {}
    with wave.open(str(path), "rb") as source:
        for clip_id in TARGET_ORDER:
            after_hashes[clip_id] = payload_hash_from_stream(
                source,
                info.frame_bytes,
                clips[clip_id].expected_frames,
                f"saida segmento {clip_id}",
            )
        if source.readframes(1):
            raise ValueError("saida contem frames extras")

    for clip_id in TARGET_ORDER:
        if after_hashes[clip_id] != before_hashes[clip_id]:
            raise ValueError(
                f"payload do ID {clip_id} mudou durante a reordenacao"
            )
    output_payload_sha, _ = payload_sha256_and_frames(info, "concat reordenado")
    audit = {
        "path": str(path.resolve()),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "sample_width_bytes": info.sample_width_bytes,
        "frames": info.frames,
        "duration_seconds": info.duration_seconds,
        "file_sha256": file_sha256(path),
        "pcm_payload_sha256": output_payload_sha,
    }
    return after_hashes, audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--source-project-proof",
        type=Path,
        required=True,
        help="projeto descartavel cuja ordem de origem foi auditada",
    )
    parser.add_argument("--min-order-correlation", type=float, default=0.70)
    parser.add_argument("--min-order-margin", type=float, default=0.05)
    return parser


def execute(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 12):
        raise ValueError("use o Python 3.12 do runtime do projeto")
    if not (0.0 <= args.min_order_correlation <= 1.0):
        raise ValueError("--min-order-correlation deve estar entre 0 e 1")
    if not (0.0 <= args.min_order_margin <= 1.0):
        raise ValueError("--min-order-margin deve estar entre 0 e 1")

    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    project_path = args.source_project_proof.expanduser().resolve()
    if not project_path.is_file():
        raise ValueError(f"prova de projeto inexistente: {project_path}")
    if input_path == output_path:
        raise ValueError("entrada e saida devem ser arquivos distintos")
    if output_path.exists() or manifest_path.exists():
        raise ValueError("saida ou manifesto ja existe; nada sera sobrescrito")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # Valida os dez crus e seus hashes congelados. Nenhum target e consultado.
    raw_audits: dict[str, Any] = {}
    for clip in CALIBRATION10.clips:
        info, whole_sha, payload_sha = validate_raw(clip)
        raw_audits[clip.clip_id] = {
            "path": info.path,
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "sample_width_bytes": info.sample_width_bytes,
            "frames": info.frames,
            "file_sha256": whole_sha,
            "pcm_payload_sha256": payload_sha,
        }

    input_info = inspect_wave(input_path, "concat original")
    require_candidate_format(input_info, "concat original")
    expected_total = sum(clip.expected_frames for clip in CALIBRATION10.clips)
    if input_info.frames != expected_total:
        difference = input_info.frames - expected_total
        raise ValueError(
            f"concat original tem {input_info.frames} frames; esperados "
            f"{expected_total} (diferenca {difference:+d})"
        )
    input_payload_sha, _ = payload_sha256_and_frames(input_info, "concat original")
    input_audit = {
        "path": str(input_path),
        "sample_rate": input_info.sample_rate,
        "channels": input_info.channels,
        "sample_width_bytes": input_info.sample_width_bytes,
        "frames": input_info.frames,
        "duration_seconds": input_info.duration_seconds,
        "file_sha256": file_sha256(input_path),
        "pcm_payload_sha256": input_payload_sha,
    }

    free_required = input_info.frames * input_info.frame_bytes * 2 + 32 * 1024 * 1024
    if shutil.disk_usage(output_path.parent).free < free_required:
        raise ValueError("espaco livre insuficiente para reordenacao auditada")

    staging_dir = Path(
        tempfile.mkdtemp(prefix=".cal10_reorder_segments.", dir=output_path.parent)
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".partial", dir=output_path.parent
    )
    os.close(descriptor)
    temporary_output = Path(temporary_name)
    created_output = False
    try:
        segment_paths, source_records = prepare_segments(
            source_suite(), input_info, staging_dir
        )
        order_proof = prove_order(
            CALIBRATION10,
            segment_paths,
            args.min_order_correlation,
            args.min_order_margin,
        )
        before_hashes = write_reordered_output(segment_paths, temporary_output)
        after_hashes, temporary_output_audit = verify_reordered_output(
            temporary_output, before_hashes
        )

        os.link(temporary_output, output_path)
        created_output = True
        committed_after_hashes, output_audit = verify_reordered_output(
            output_path, before_hashes
        )
        if committed_after_hashes != after_hashes:
            raise ValueError("hashes mudaram no commit atomico da saida")

        source_offsets = {
            record["clip_id"]: {
                "start_frame": record["start_frame_in_concat"],
                "stop_frame_exclusive": record["stop_frame_in_concat_exclusive"],
                "frames": record["frames"],
                "pcm_payload_sha256": record["pcm_payload_sha256"],
            }
            for record in source_records
        }
        target_offset = 0
        target_offsets: dict[str, Any] = {}
        clips = clip_map()
        for clip_id in TARGET_ORDER:
            frames = clips[clip_id].expected_frames
            target_offsets[clip_id] = {
                "start_frame": target_offset,
                "stop_frame_exclusive": target_offset + frames,
                "frames": frames,
                "pcm_payload_sha256": after_hashes[clip_id],
            }
            target_offset += frames

        manifest = {
            "schema_version": 1,
            "operation": "lossless_pcm24_segment_reorder",
            "dsp_performed": False,
            "targets_read": False,
            "ids_below_64_read": False,
            "source_order_from_project": list(SOURCE_ORDER),
            "target_order": list(TARGET_ORDER),
            "source_project_proof": {
                "path": str(project_path),
                "file_sha256": file_sha256(project_path),
            },
            "helper": {
                "path": str(Path(__file__).resolve()),
                "file_sha256": file_sha256(Path(__file__).resolve()),
            },
            "input": input_audit,
            "output": output_audit,
            "temporary_output_audit_before_commit": temporary_output_audit,
            "raws": raw_audits,
            "source_segments": source_offsets,
            "target_segments": target_offsets,
            "order_proof": order_proof,
            "per_segment_lossless_proof": {
                clip_id: {
                    "before_pcm_payload_sha256": before_hashes[clip_id],
                    "after_pcm_payload_sha256": after_hashes[clip_id],
                    "byte_identical": before_hashes[clip_id] == after_hashes[clip_id],
                }
                for clip_id in TARGET_ORDER
            },
            "all_segment_payloads_byte_identical": all(
                before_hashes[clip_id] == after_hashes[clip_id]
                for clip_id in TARGET_ORDER
            ),
        }
        write_json_exclusive(manifest_path, manifest)
    except Exception:
        if created_output:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            temporary_output.unlink()
        except FileNotFoundError:
            pass

    print(
        json.dumps(
            {
                "input": str(input_path),
                "input_order": list(SOURCE_ORDER),
                "output": str(output_path),
                "output_order": list(TARGET_ORDER),
                "manifest": str(manifest_path),
                "all_segment_payloads_byte_identical": True,
                "targets_read": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return execute(args)
    except (OSError, ValueError, RuntimeError, wave.Error) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
