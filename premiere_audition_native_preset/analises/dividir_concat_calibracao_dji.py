#!/usr/bin/env python3
"""Divida um render concatenado DJI e compare os oito pares de calibracao.

O programa e deliberadamente fechado aos IDs 66, 67, 68, 71, 72, 74, 81
e 84. Os limites sao derivados da contagem exata de frames dos WAVs crus,
na ordem fixa abaixo. Cada WAV candidato recebe uma copia byte a byte do
payload PCM correspondente do concat; nenhuma amostra e decodificada,
reamostrada ou recalculada.

Use Python 3.12 do runtime do projeto. O modulo ``wave`` de Pythons antigos
nao abre corretamente todos os WAVs WAVE_FORMAT_EXTENSIBLE usados aqui.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PRESET_ROOT = ROOT / "premiere_audition_native_preset"
COMPARISON_DIR = PRESET_ROOT / "comparacao_ab"
EXPORT_DIR = PRESET_ROOT / "exports_validacao"
RESULTS_ROOT = PRESET_ROOT / "analises" / "resultados"
ANALYZER = PRESET_ROOT / "analises" / "comparar_wavs_adobe.py"

CLIP_IDS = ("66", "67", "68", "71", "72", "74", "81", "84")
RAW_PATHS = {
    clip_id: COMPARISON_DIR / f"{clip_id}_CRU_DJI.wav"
    for clip_id in CLIP_IDS
}
TARGET_PATHS = {
    "66": ROOT
    / "independent_neural_flow_20260729/validation_wavs/66_A_CAPCUT_44K1.wav",
    "67": COMPARISON_DIR / "67_CAPCUT.wav",
    "68": COMPARISON_DIR / "68_CAPCUT.wav",
    "71": COMPARISON_DIR / "71_CAPCUT.wav",
    "72": COMPARISON_DIR / "72_CAPCUT.wav",
    "74": ROOT
    / "independent_neural_flow_20260729/validation_wavs/74_A_CAPCUT_44K1.wav",
    "81": COMPARISON_DIR / "81_CAPCUT.wav",
    "84": ROOT
    / "independent_neural_flow_20260729/validation_wavs/84_A_CAPCUT_44K1.wav",
}

EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_CHANNELS = 2
EXPECTED_SAMPLE_WIDTH = 3
READ_CHUNK_FRAMES = 65_536
TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


@dataclass(frozen=True)
class WaveInfo:
    path: str
    sample_rate: int
    channels: int
    sample_width_bytes: int
    frames: int
    compression: str

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sample_width_bytes

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate

    @property
    def format_key(self) -> tuple[int, int, int, str]:
        return (
            self.sample_rate,
            self.channels,
            self.sample_width_bytes,
            self.compression,
        )


def inspect_wave(path: Path, role: str) -> WaveInfo:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"{role} inexistente: {resolved}")
    try:
        with wave.open(str(resolved), "rb") as source:
            info = WaveInfo(
                path=str(resolved),
                sample_rate=source.getframerate(),
                channels=source.getnchannels(),
                sample_width_bytes=source.getsampwidth(),
                frames=source.getnframes(),
                compression=source.getcomptype(),
            )
    except (EOFError, wave.Error) as error:
        raise ValueError(f"{role} nao e WAV PCM legivel: {resolved}: {error}") from error
    if info.compression != "NONE":
        raise ValueError(f"{role} usa compressao nao aceita: {info.compression}")
    if info.sample_rate < 1 or info.channels < 1 or info.frames < 1:
        raise ValueError(f"{role} tem cabecalho WAV invalido: {resolved}")
    if info.sample_width_bytes not in (1, 2, 3, 4):
        raise ValueError(
            f"{role} tem sample width nao suportado: "
            f"{info.sample_width_bytes} bytes"
        )
    return info


def payload_sha256_and_frames(info: WaveInfo, role: str) -> tuple[str, int]:
    """Leia todo o payload e confirme alinhamento e contagem declarada."""

    digest = hashlib.sha256()
    actual_frames = 0
    with wave.open(info.path, "rb") as source:
        while True:
            payload = source.readframes(READ_CHUNK_FRAMES)
            if not payload:
                break
            if len(payload) % info.frame_bytes:
                raise ValueError(
                    f"{role} tem payload truncado/desalinhado: {info.path}"
                )
            actual_frames += len(payload) // info.frame_bytes
            digest.update(payload)
    if actual_frames != info.frames:
        raise ValueError(
            f"{role} declara {info.frames} frames, mas contem "
            f"{actual_frames}: {info.path}"
        )
    return digest.hexdigest(), actual_frames


def require_format(info: WaveInfo, role: str) -> None:
    expected = (
        EXPECTED_SAMPLE_RATE,
        EXPECTED_CHANNELS,
        EXPECTED_SAMPLE_WIDTH,
        "NONE",
    )
    if info.format_key != expected:
        raise ValueError(
            f"{role} deve ser PCM24 estereo 48 kHz; recebido "
            f"{info.sample_width_bytes * 8} bit, {info.channels} canal(is), "
            f"{info.sample_rate} Hz, compressao {info.compression}: {info.path}"
        )


def validate_target(info: WaveInfo, clip_id: str) -> None:
    if info.sample_rate != 44_100:
        raise ValueError(
            f"alvo {clip_id} deveria estar em 44,1 kHz: {info.path}"
        )
    if info.channels not in (1, 2) or info.sample_width_bytes not in (2, 3):
        raise ValueError(
            f"formato inesperado no alvo {clip_id}: "
            f"{info.sample_width_bytes * 8} bit, {info.channels} canal(is)"
        )


def make_output_paths(tag: str) -> tuple[dict[str, Path], dict[str, Path], Path]:
    candidates = {
        clip_id: EXPORT_DIR / f"{clip_id}_{tag}.wav"
        for clip_id in CLIP_IDS
    }
    result_dirs = {
        clip_id: RESULTS_ROOT / f"{clip_id}_{tag}"
        for clip_id in CLIP_IDS
    }
    manifest = RESULTS_ROOT / f"{tag}_CONCAT_SPLIT_MANIFEST.json"
    return candidates, result_dirs, manifest


def reject_collisions(
    candidates: dict[str, Path],
    result_dirs: dict[str, Path],
    manifest: Path,
    split_only: bool,
) -> None:
    collisions = [path for path in candidates.values() if path.exists()]
    if manifest.exists():
        collisions.append(manifest)
    if not split_only:
        collisions.extend(path for path in result_dirs.values() if path.exists())
    if collisions:
        listing = "\n  - ".join(str(path) for path in collisions)
        raise ValueError(
            "a execucao nunca sobrescreve saidas; escolha outro --tag. "
            f"Colisoes:\n  - {listing}"
        )


def write_segment(
    source: wave.Wave_read,
    concat_info: WaveInfo,
    expected_frames: int,
    temporary_path: Path,
) -> str:
    remaining = expected_frames
    digest = hashlib.sha256()
    with wave.open(str(temporary_path), "wb") as sink:
        sink.setnchannels(concat_info.channels)
        sink.setsampwidth(concat_info.sample_width_bytes)
        sink.setframerate(concat_info.sample_rate)
        sink.setcomptype("NONE", "not compressed")
        sink.setnframes(expected_frames)
        while remaining:
            requested = min(remaining, READ_CHUNK_FRAMES)
            payload = source.readframes(requested)
            if len(payload) % concat_info.frame_bytes:
                raise ValueError("concat terminou com frame PCM parcial")
            received = len(payload) // concat_info.frame_bytes
            if received != requested:
                raise ValueError(
                    f"concat terminou cedo: pedidos {requested} frames, "
                    f"recebidos {received}"
                )
            sink.writeframesraw(payload)
            digest.update(payload)
            remaining -= received
    return digest.hexdigest()


def split_concat(
    concat_info: WaveInfo,
    raw_infos: dict[str, WaveInfo],
    candidates: dict[str, Path],
) -> list[dict[str, Any]]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    required_bytes = concat_info.frames * concat_info.frame_bytes
    free_bytes = shutil.disk_usage(EXPORT_DIR).free
    if free_bytes < required_bytes + 16 * 1024 * 1024:
        raise ValueError(
            f"espaco livre insuficiente em {EXPORT_DIR}: "
            f"necessarios ao menos {required_bytes + 16 * 1024 * 1024} bytes"
        )

    temporary_paths: list[Path] = []
    verified: list[tuple[Path, Path]] = []
    created: list[Path] = []
    records: list[dict[str, Any]] = []
    offset = 0
    try:
        with wave.open(concat_info.path, "rb") as source:
            for clip_id in CLIP_IDS:
                raw_info = raw_infos[clip_id]
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{clip_id}_concat_",
                    suffix=".wav.partial",
                    dir=EXPORT_DIR,
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                temporary_paths.append(temporary)
                source_payload_sha = write_segment(
                    source,
                    concat_info,
                    raw_info.frames,
                    temporary,
                )

                output_info = inspect_wave(temporary, f"segmento temporario {clip_id}")
                if output_info.format_key != concat_info.format_key:
                    raise ValueError(f"formato mudou ao escrever o segmento {clip_id}")
                if output_info.frames != raw_info.frames:
                    raise ValueError(
                        f"segmento {clip_id} deveria ter {raw_info.frames} frames, "
                        f"mas tem {output_info.frames}"
                    )
                output_payload_sha, _ = payload_sha256_and_frames(
                    output_info, f"segmento temporario {clip_id}"
                )
                if output_payload_sha != source_payload_sha:
                    raise ValueError(
                        f"payload PCM mudou durante a divisao do segmento {clip_id}"
                    )

                destination = candidates[clip_id]
                verified.append((temporary, destination))
                records.append(
                    {
                        "clip_id": clip_id,
                        "start_frame_in_concat": offset,
                        "stop_frame_in_concat_exclusive": offset + raw_info.frames,
                        "frames": raw_info.frames,
                        "duration_seconds": raw_info.duration_seconds,
                        "candidate_path": str(destination.resolve()),
                        "pcm_payload_sha256": output_payload_sha,
                    }
                )
                offset += raw_info.frames

            if source.readframes(1):
                raise ValueError("concat contem frames extras depois do ultimo segmento")

        # Hard links tornam o commit atomico e falham se alguma saida surgir
        # entre a pre-validacao e este ponto; nunca ha overwrite silencioso.
        for temporary, destination in verified:
            os.link(temporary, destination)
            created.append(destination)
        return records
    except Exception:
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for path in temporary_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def run_analyzers(
    python_executable: Path,
    tag: str,
    candidates: dict[str, Path],
    result_dirs: dict[str, Path],
) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for clip_id in CLIP_IDS:
        output_prefix = f"{clip_id}_{tag}"
        command = [
            str(python_executable),
            str(ANALYZER),
            "--raw",
            str(RAW_PATHS[clip_id]),
            "--candidate",
            str(candidates[clip_id]),
            "--target",
            str(TARGET_PATHS[clip_id]),
            "--output-dir",
            str(result_dirs[clip_id]),
            "--output-prefix",
            output_prefix,
            "--label",
            f"{clip_id} {tag} — concat calibracao DJI",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"analisador falhou no ID {clip_id} "
                f"(codigo {completed.returncode}): {details}"
            )
        try:
            summaries[clip_id] = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"analisador retornou JSON invalido no ID {clip_id}"
            ) from error
        print(completed.stdout.rstrip(), flush=True)
    return summaries


def write_manifest_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2)
        destination.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--concat",
        type=Path,
        required=True,
        help="WAV PCM24 estereo 48 kHz com os oito clipes na ordem fixa",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="identificador seguro e exclusivo para candidatos e resultados",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="Python 3.12 com NumPy; por padrao usa o interpretador atual",
    )
    parser.add_argument(
        "--split-only",
        action="store_true",
        help="divide e audita os payloads, mas nao chama o analisador",
    )
    return parser


def execute(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 12):
        raise ValueError("use o Python 3.12 do runtime do projeto")
    if not TAG_PATTERN.fullmatch(args.tag):
        raise ValueError(
            "--tag deve ter 1 a 80 caracteres: letras, numeros, ponto, "
            "hifen ou sublinhado; o primeiro deve ser alfanumerico"
        )
    if not ANALYZER.is_file():
        raise ValueError(f"analisador inexistente: {ANALYZER}")
    if not args.split_only and not args.python.expanduser().is_file():
        raise ValueError(f"Python inexistente: {args.python.expanduser()}")

    candidates, result_dirs, manifest_path = make_output_paths(args.tag)
    reject_collisions(candidates, result_dirs, manifest_path, args.split_only)

    concat_info = inspect_wave(args.concat, "concat")
    require_format(concat_info, "concat")
    concat_payload_sha, _ = payload_sha256_and_frames(concat_info, "concat")

    raw_infos: dict[str, WaveInfo] = {}
    raw_payload_sha: dict[str, str] = {}
    for clip_id in CLIP_IDS:
        info = inspect_wave(RAW_PATHS[clip_id], f"cru {clip_id}")
        require_format(info, f"cru {clip_id}")
        payload_sha, _ = payload_sha256_and_frames(info, f"cru {clip_id}")
        raw_infos[clip_id] = info
        raw_payload_sha[clip_id] = payload_sha

    expected_frames = sum(info.frames for info in raw_infos.values())
    if concat_info.frames != expected_frames:
        difference = concat_info.frames - expected_frames
        raise ValueError(
            f"duracao total invalida: concat tem {concat_info.frames} frames, "
            f"mas a soma dos crus e {expected_frames} "
            f"(diferenca {difference:+d} frames; "
            f"{difference / EXPECTED_SAMPLE_RATE:+.9f} s)"
        )

    target_infos: dict[str, WaveInfo] = {}
    target_payload_sha: dict[str, str] = {}
    if not args.split_only:
        for clip_id in CLIP_IDS:
            info = inspect_wave(TARGET_PATHS[clip_id], f"alvo {clip_id}")
            validate_target(info, clip_id)
            payload_sha, _ = payload_sha256_and_frames(info, f"alvo {clip_id}")
            target_infos[clip_id] = info
            target_payload_sha[clip_id] = payload_sha

    segment_records = split_concat(concat_info, raw_infos, candidates)
    analyzer_summaries = (
        {}
        if args.split_only
        else run_analyzers(
            args.python.expanduser().resolve(),
            args.tag,
            candidates,
            result_dirs,
        )
    )

    manifest = {
        "schema_version": 1,
        "mode": "split_only" if args.split_only else "split_and_compare",
        "fixed_order": list(CLIP_IDS),
        "concat": {
            **asdict(concat_info),
            "duration_seconds": concat_info.duration_seconds,
            "pcm_payload_sha256": concat_payload_sha,
        },
        "expected_total_frames_from_raws": expected_frames,
        "raws": {
            clip_id: {
                **asdict(raw_infos[clip_id]),
                "duration_seconds": raw_infos[clip_id].duration_seconds,
                "pcm_payload_sha256": raw_payload_sha[clip_id],
            }
            for clip_id in CLIP_IDS
        },
        "targets": {
            clip_id: {
                **asdict(target_infos[clip_id]),
                "duration_seconds": target_infos[clip_id].duration_seconds,
                "pcm_payload_sha256": target_payload_sha[clip_id],
            }
            for clip_id in target_infos
        },
        "segments": segment_records,
        "analyzer_summaries": analyzer_summaries,
    }
    write_manifest_exclusive(manifest_path, manifest)
    print(
        json.dumps(
            {
                "manifest": str(manifest_path.resolve()),
                "candidates": {
                    clip_id: str(candidates[clip_id].resolve())
                    for clip_id in CLIP_IDS
                },
                "analysis_completed": not args.split_only,
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
