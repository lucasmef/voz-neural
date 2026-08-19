#!/usr/bin/env python3
"""Extrai de modo lossless o trecho valido de um export DN35/DN50/EXP do take 95.

O helper e fechado a tres pares de nomes permitidos, exige PCM24 estereo/48 kHz e as
contagens conhecidas de 2.044.800 -> 1.899.200 frames. O audio comeca depois
de um gap inicial de 145.600 frames, portanto o intervalo permitido e somente
``[145600, 2044800)``. Ele nunca substitui um arquivo existente: cria um
temporario, valida-o e publica o resultado por hard link com semantica O_EXCL.
Em nova execucao, valida o resultado existente e encerra sem regrava-lo.

Somente o payload PCM e fatiado. Nenhum DSP, ganho, reamostragem, downmix ou
recodificacao e aplicado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator


NATIVE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE = "DN35"
ALLOWED_JOBS = {
    "DN35": {
        "source_name": "95_N1_DN35_PREMIERE_REAL_FULL.wav",
        "output_name": "95_N1_DN35_PREMIERE_REAL.wav",
        "manifest_name": "95_N1_DN35_PREMIERE_REAL_TRIM_MANIFEST.json",
    },
    "DN50": {
        "source_name": "95_N2_DN50_PREMIERE_REAL_FULL.wav",
        "output_name": "95_N2_DN50_PREMIERE_REAL.wav",
        "manifest_name": "95_N2_DN50_PREMIERE_REAL_TRIM_MANIFEST.json",
    },
    "EXP": {
        "source_name": "95_N3_EXP24_PREMIERE_REAL_FULL.wav",
        "output_name": "95_N3_EXP24_PREMIERE_REAL.wav",
        "manifest_name": "95_N3_EXP24_PREMIERE_REAL_TRIM_MANIFEST.json",
    },
}
EXPECTED_SOURCE_FRAMES = 2_044_800
EXPECTED_START_FRAME = 145_600
EXPECTED_OUTPUT_FRAMES = 1_899_200
EXPECTED_STOP_FRAME = EXPECTED_START_FRAME + EXPECTED_OUTPUT_FRAMES
EXPECTED_CHANNELS = 2
EXPECTED_SAMPLE_WIDTH = 3
EXPECTED_SAMPLE_RATE = 48_000
FRAME_BYTES = EXPECTED_CHANNELS * EXPECTED_SAMPLE_WIDTH
CHUNK_FRAMES = 65_536


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "sha256": sha256_file(path),
    }


def validate_paths(
    candidate: str,
    source: Path,
    output: Path,
    manifest: Path,
) -> None:
    job = ALLOWED_JOBS[candidate]
    if "blind" in str(source).lower() or "blind" in str(output).lower():
        raise ValueError("caminhos cegos sao proibidos")
    if source.name != job["source_name"]:
        raise ValueError(
            f"fonte fora da allow-list: {source.name}; esperado {job['source_name']}"
        )
    if output.name != job["output_name"]:
        raise ValueError(
            f"saida fora da allow-list: {output.name}; esperado {job['output_name']}"
        )
    if source == output:
        raise ValueError("fonte e saida nao podem coincidir")
    if manifest.suffix.lower() != ".json":
        raise ValueError("manifesto precisa terminar em .json")
    if manifest.name != job["manifest_name"]:
        raise ValueError(
            f"manifesto fora da allow-list: {manifest.name}; esperado {job['manifest_name']}"
        )


def wav_format(path: Path, expected_frames: int) -> dict[str, object]:
    with wave.open(str(path), "rb") as wav:
        result = {
            "channels": wav.getnchannels(),
            "sample_width_bytes": wav.getsampwidth(),
            "bits_per_sample": wav.getsampwidth() * 8,
            "sample_rate": wav.getframerate(),
            "frames": wav.getnframes(),
            "duration_seconds": wav.getnframes() / wav.getframerate(),
            "compression": wav.getcomptype(),
        }
    expected = {
        "channels": EXPECTED_CHANNELS,
        "sample_width_bytes": EXPECTED_SAMPLE_WIDTH,
        "sample_rate": EXPECTED_SAMPLE_RATE,
        "frames": expected_frames,
        "compression": "NONE",
    }
    mismatches = {
        key: {"actual": result[key], "expected": value}
        for key, value in expected.items()
        if result[key] != value
    }
    if mismatches:
        raise ValueError(f"formato WAV inesperado em {path}: {mismatches}")
    return result


def iter_payload(wav: wave.Wave_read, frames: int) -> Iterator[bytes]:
    remaining = frames
    while remaining:
        count = min(CHUNK_FRAMES, remaining)
        block = wav.readframes(count)
        expected_bytes = count * FRAME_BYTES
        if len(block) != expected_bytes:
            raise ValueError(
                f"payload truncado: {len(block)} bytes; esperados {expected_bytes}"
            )
        yield block
        remaining -= count


def payload_hashes(
    path: Path,
    total_frames: int,
    slice_start_frame: int = EXPECTED_START_FRAME,
    slice_stop_frame: int = EXPECTED_STOP_FRAME,
) -> dict[str, object]:
    if not 0 <= slice_start_frame <= slice_stop_frame <= total_frames:
        raise ValueError(
            f"slice invalido [{slice_start_frame}, {slice_stop_frame}) "
            f"para {total_frames} frames"
        )
    full = hashlib.sha256()
    selected = hashlib.sha256()
    full_bytes = 0
    selected_bytes = 0
    with wave.open(str(path), "rb") as wav:
        frame_cursor = 0
        for block in iter_payload(wav, total_frames):
            full.update(block)
            full_bytes += len(block)
            block_frames = len(block) // FRAME_BYTES
            block_stop = frame_cursor + block_frames
            overlap_start = max(frame_cursor, slice_start_frame)
            overlap_stop = min(block_stop, slice_stop_frame)
            if overlap_start < overlap_stop:
                local_start = (overlap_start - frame_cursor) * FRAME_BYTES
                local_stop = (overlap_stop - frame_cursor) * FRAME_BYTES
                selected.update(block[local_start:local_stop])
                selected_bytes += local_stop - local_start
            frame_cursor = block_stop
        if wav.readframes(1):
            raise ValueError("WAV contem payload alem da contagem declarada")
    expected_full_bytes = total_frames * FRAME_BYTES
    expected_selected_bytes = (slice_stop_frame - slice_start_frame) * FRAME_BYTES
    if full_bytes != expected_full_bytes or selected_bytes != expected_selected_bytes:
        raise ValueError(
            "contagem de payload inesperada: "
            f"full={full_bytes}/{expected_full_bytes}, "
            f"selected={selected_bytes}/{expected_selected_bytes}"
        )
    return {
        "full_payload_bytes": full_bytes,
        "full_payload_sha256": full.hexdigest(),
        "expected_slice_start_frame": slice_start_frame,
        "expected_slice_stop_frame_exclusive": slice_stop_frame,
        "expected_slice_payload_bytes": selected_bytes,
        "expected_slice_payload_sha256": selected.hexdigest(),
    }


def write_slice(source: Path, temporary_output: Path) -> None:
    with wave.open(str(source), "rb") as source_wav, wave.open(
        str(temporary_output), "wb"
    ) as output_wav:
        output_wav.setnchannels(EXPECTED_CHANNELS)
        output_wav.setsampwidth(EXPECTED_SAMPLE_WIDTH)
        output_wav.setframerate(EXPECTED_SAMPLE_RATE)
        output_wav.setcomptype("NONE", "not compressed")
        output_wav.setnframes(EXPECTED_OUTPUT_FRAMES)
        source_wav.setpos(EXPECTED_START_FRAME)
        for block in iter_payload(source_wav, EXPECTED_OUTPUT_FRAMES):
            output_wav.writeframesraw(block)
    with temporary_output.open("rb") as handle:
        os.fsync(handle.fileno())


def payload_equal_slice(source: Path, output: Path) -> bool:
    with wave.open(str(source), "rb") as source_wav, wave.open(
        str(output), "rb"
    ) as output_wav:
        source_wav.setpos(EXPECTED_START_FRAME)
        remaining = EXPECTED_OUTPUT_FRAMES
        while remaining:
            count = min(CHUNK_FRAMES, remaining)
            if source_wav.readframes(count) != output_wav.readframes(count):
                return False
            remaining -= count
        return output_wav.readframes(1) == b""


def validate_output(
    source: Path,
    output: Path,
    source_payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    output_format = wav_format(output, EXPECTED_OUTPUT_FRAMES)
    output_payload = payload_hashes(
        output,
        EXPECTED_OUTPUT_FRAMES,
        slice_start_frame=0,
        slice_stop_frame=EXPECTED_OUTPUT_FRAMES,
    )
    expected_hash = source_payload["expected_slice_payload_sha256"]
    actual_hash = output_payload["full_payload_sha256"]
    if actual_hash != expected_hash:
        raise ValueError(
            f"payload da saida diverge do slice da fonte: {actual_hash} != {expected_hash}"
        )
    if not payload_equal_slice(source, output):
        raise ValueError("comparacao byte a byte do payload falhou")
    return output_format, output_payload


def publish_without_overwrite(source: Path, output: Path) -> bool:
    """Crie output se ausente; retorne True se criado, False se ja existia."""

    if output.exists():
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        write_slice(source, temporary)
        wav_format(temporary, EXPECTED_OUTPUT_FRAMES)
        try:
            os.link(temporary, output)
            created = True
        except FileExistsError:
            created = False
        return created
    finally:
        temporary.unlink(missing_ok=True)


def atomic_create_text(path: Path, content: str) -> bool:
    """Crie texto sem substituir; valide conteudo se ja existir."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(f"arquivo existente diverge e nao sera sobrescrito: {path}")
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def stable_manifest_fields(manifest: dict[str, object]) -> dict[str, object]:
    result = json.loads(json.dumps(manifest))
    result.pop("created_at_utc", None)
    result.pop("run", None)
    # O hash preservado no manifesto identifica a versao que criou o artefato.
    # Atualizar o helper depois disso nao invalida um payload ja provado.
    provenance = result.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("script", None)
    return result


def existing_manifest_matches(path: Path, expected: dict[str, object]) -> bool:
    if not path.exists():
        return False
    current = json.loads(path.read_text(encoding="utf-8"))
    if stable_manifest_fields(current) != stable_manifest_fields(expected):
        raise ValueError(f"manifesto existente diverge e nao sera sobrescrito: {path}")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate", choices=tuple(ALLOWED_JOBS), default=DEFAULT_CANDIDATE
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    job = ALLOWED_JOBS[args.candidate]
    default_source = NATIVE_ROOT / "exports_validacao" / job["source_name"]
    default_output = NATIVE_ROOT / "exports_validacao" / job["output_name"]
    default_manifest = (
        NATIVE_ROOT / "analises/resultados/95_GRADE_FINAL" / job["manifest_name"]
    )
    source = (args.source or default_source).expanduser().resolve()
    output = (args.output or default_output).expanduser().resolve()
    manifest_path = (args.manifest or default_manifest).expanduser().resolve()
    validate_paths(args.candidate, source, output, manifest_path)
    if not source.is_file():
        raise SystemExit(f"fonte ausente: {source}")

    source_before = file_identity(source)
    source_format = wav_format(source, EXPECTED_SOURCE_FRAMES)
    source_payload = payload_hashes(source, EXPECTED_SOURCE_FRAMES)
    output_created = publish_without_overwrite(source, output)
    output_format, output_payload = validate_output(
        source, output, source_payload
    )
    source_after = file_identity(source)
    if source_after != source_before:
        raise RuntimeError("a fonte mudou durante a operacao")
    output_identity = file_identity(output)
    script_identity = file_identity(Path(__file__).resolve())

    manifest: dict[str, object] = {
        "schema_version": 1,
        "operation": "lossless_pcm24_exact_slice",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "clip_id": 95,
            "candidate": args.candidate,
            "blind_targets_read": False,
            "gui_used": False,
        },
        "provenance": {
            "script": script_identity,
            "source": source_before,
            "output": output_identity,
            "method": (
                "copy interleaved PCM frames [145600, 2044800) exactly; "
                "new RIFF/WAVE header only"
            ),
            "publish_semantics": (
                "temporary validated file + atomic hard link; never overwrite"
            ),
        },
        "source_audio": {
            "format": source_format,
            "payload": source_payload,
        },
        "output_audio": {
            "format": output_format,
            "payload": output_payload,
        },
        "trim": {
            "kept_start_frame_inclusive": EXPECTED_START_FRAME,
            "kept_stop_frame_exclusive": EXPECTED_STOP_FRAME,
            "kept_frames": EXPECTED_OUTPUT_FRAMES,
            "discarded_initial_gap_frames": EXPECTED_START_FRAME,
            "discarded_tail_frames": EXPECTED_SOURCE_FRAMES - EXPECTED_STOP_FRAME,
            "kept_duration_seconds": EXPECTED_OUTPUT_FRAMES / EXPECTED_SAMPLE_RATE,
            "discarded_initial_gap_duration_seconds": EXPECTED_START_FRAME
            / EXPECTED_SAMPLE_RATE,
            "discarded_tail_duration_seconds": (
                EXPECTED_SOURCE_FRAMES - EXPECTED_STOP_FRAME
            )
            / EXPECTED_SAMPLE_RATE,
        },
        "validation": {
            "source_format_exact": True,
            "output_format_exact": True,
            "source_unchanged_sha256": source_before["sha256"]
            == source_after["sha256"],
            "output_payload_sha256_equals_source_slice": output_payload[
                "full_payload_sha256"
            ]
            == source_payload["expected_slice_payload_sha256"],
            "output_payload_byte_for_byte_equals_source_slice": True,
            "payload_bytes_expected": EXPECTED_OUTPUT_FRAMES * FRAME_BYTES,
            "payload_bytes_actual": output_payload["full_payload_bytes"],
            "dsp_applied": False,
            "gain_applied": False,
            "resampling_applied": False,
            "channel_conversion_applied": False,
        },
        "run": {
            "output_created_this_run": output_created,
            "existing_output_was_never_overwritten": not output_created,
        },
    }

    if existing_manifest_matches(manifest_path, manifest):
        manifest_created = False
    else:
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        manifest_created = atomic_create_text(manifest_path, manifest_text)
    manifest_sha256 = sha256_file(manifest_path)
    checksum_path = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    checksum_text = f"{manifest_sha256}  {manifest_path.name}\n"
    checksum_created = atomic_create_text(checksum_path, checksum_text)

    summary = {
        "source": source_before,
        "output": output_identity,
        "source_payload_sha256": source_payload["full_payload_sha256"],
        "source_slice_payload_sha256": source_payload[
            "expected_slice_payload_sha256"
        ],
        "output_payload_sha256": output_payload["full_payload_sha256"],
        "manifest": {
            "path": str(manifest_path),
            "sha256": manifest_sha256,
            "checksum_path": str(checksum_path),
        },
        "frames": {
            "source": EXPECTED_SOURCE_FRAMES,
            "output": EXPECTED_OUTPUT_FRAMES,
            "slice_start": EXPECTED_START_FRAME,
            "slice_stop_exclusive": EXPECTED_STOP_FRAME,
            "discarded_initial_gap": EXPECTED_START_FRAME,
            "discarded_tail": EXPECTED_SOURCE_FRAMES - EXPECTED_STOP_FRAME,
        },
        "created_this_run": {
            "output": output_created,
            "manifest": manifest_created,
            "checksum": checksum_created,
        },
        "validation_passed": True,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
