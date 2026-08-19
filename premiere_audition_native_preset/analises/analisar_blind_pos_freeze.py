#!/usr/bin/env python3
"""Compare blind5 somente depois do congelamento formal da cadeia.

Este programa nao conhece nem procura caminhos de alvos. Eles precisam ser
fornecidos, depois do freeze, em um manifesto explicito com os cinco IDs e
hashes SHA-256. Antes de abrir esse manifesto, o programa exige confirmacao
literal e valida que os candidatos ainda sao exatamente os registrados pelo
divisor cego.

Formato do manifesto de alvos (a ser criado somente pos-freeze)::

    {
      "schema_version": 1,
      "suite": "blind5",
      "frozen_split_manifest_sha256": "<sha256>",
      "targets": {
        "64": {"path": "/caminho/64-capcut.wav", "file_sha256": "..."},
        "69": {"path": "/caminho/69-capcut.wav", "file_sha256": "..."},
        "73": {"path": "/caminho/73-capcut.wav", "file_sha256": "..."},
        "75": {"path": "/caminho/75-capcut.wav", "file_sha256": "..."},
        "82": {"path": "/caminho/82-capcut.wav", "file_sha256": "..."}
      }
    }
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Mapping

from validar_concats_finais import (
    ANALYZER,
    BLIND5,
    DEFAULT_RESULTS_DIR,
    TAG_PATTERN,
    aggregate_markdown,
    aggregate_summaries,
    file_sha256,
    inspect_wave,
    payload_sha256_and_frames,
    require_candidate_format,
    validate_raw,
    write_json_exclusive,
)


CONFIRMATION = "CADEIA_E_PRESETS_CONGELADOS"


def read_json(path: Path, role: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"{role} inexistente: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{role} nao contem JSON valido: {resolved}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{role} deve ser um objeto JSON")
    return payload


def validate_split_manifest(path: Path) -> tuple[dict[str, Any], str]:
    resolved = path.expanduser().resolve()
    payload = read_json(resolved, "manifesto de split cego")
    if payload.get("schema_version") != 2:
        raise ValueError("manifesto de split cego deve usar schema_version 2")
    if payload.get("suite") != "blind5" or payload.get("blind") is not True:
        raise ValueError("manifesto fornecido nao e do conjunto blind5")
    if payload.get("blind_targets_read") is not False:
        raise ValueError("manifesto nao prova que os alvos permaneceram fechados")
    if payload.get("comparison_performed") is not False:
        raise ValueError("manifesto cego ja declara comparacao; fluxo invalido")
    if payload.get("fixed_order") != list(BLIND5.order):
        raise ValueError("ordem do manifesto cego diverge da ordem congelada")
    if payload.get("order_proof", {}).get("all_passed") is not True:
        raise ValueError("manifesto cego nao contem prova de ordem aprovada")

    candidates = payload.get("candidates")
    if not isinstance(candidates, dict) or set(candidates) != set(BLIND5.order):
        raise ValueError("manifesto cego nao contem exatamente os cinco candidatos")
    for clip in BLIND5.clips:
        record = candidates[clip.clip_id]
        if not isinstance(record, dict):
            raise ValueError(f"registro de candidato invalido no ID {clip.clip_id}")
        candidate_path = Path(str(record.get("path", ""))).expanduser().resolve()
        info = inspect_wave(candidate_path, f"candidato cego {clip.clip_id}")
        require_candidate_format(info, f"candidato cego {clip.clip_id}")
        if info.frames != clip.expected_frames:
            raise ValueError(f"quadros do candidato cego {clip.clip_id} mudaram")
        whole_sha = file_sha256(candidate_path)
        if whole_sha != record.get("file_sha256"):
            raise ValueError(f"arquivo candidato cego {clip.clip_id} mudou apos o freeze")
        payload_sha, _ = payload_sha256_and_frames(
            info, f"candidato cego {clip.clip_id}"
        )
        if payload_sha != record.get("pcm_payload_sha256"):
            raise ValueError(f"payload candidato cego {clip.clip_id} mudou apos o freeze")
    return payload, file_sha256(resolved)


def validate_targets_manifest(
    path: Path, expected_split_sha: str
) -> tuple[dict[str, Path], dict[str, Any], str]:
    resolved = path.expanduser().resolve()
    payload = read_json(resolved, "manifesto de alvos pos-freeze")
    if payload.get("schema_version") != 1 or payload.get("suite") != "blind5":
        raise ValueError("manifesto de alvos nao e schema 1 / blind5")
    if payload.get("frozen_split_manifest_sha256") != expected_split_sha:
        raise ValueError(
            "manifesto de alvos nao esta vinculado ao snapshot cego congelado"
        )
    targets = payload.get("targets")
    if not isinstance(targets, dict) or set(targets) != set(BLIND5.order):
        raise ValueError("manifesto de alvos deve conter exatamente 64,69,73,75,82")

    paths: dict[str, Path] = {}
    audits: dict[str, Any] = {}
    for clip in BLIND5.clips:
        record = targets[clip.clip_id]
        if not isinstance(record, dict):
            raise ValueError(f"registro de alvo invalido no ID {clip.clip_id}")
        expected_sha = record.get("file_sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise ValueError(f"SHA-256 de alvo invalido no ID {clip.clip_id}")
        target_path = Path(str(record.get("path", ""))).expanduser().resolve()
        info = inspect_wave(target_path, f"alvo cego revelado {clip.clip_id}")
        if info.sample_rate not in (44_100, 48_000):
            raise ValueError(f"taxa inesperada no alvo cego {clip.clip_id}")
        if info.channels not in (1, 2) or info.sample_width_bytes not in (2, 3):
            raise ValueError(f"formato inesperado no alvo cego {clip.clip_id}")
        whole_sha = file_sha256(target_path)
        if whole_sha != expected_sha:
            raise ValueError(f"hash do alvo cego {clip.clip_id} nao confere")
        payload_sha, _ = payload_sha256_and_frames(
            info, f"alvo cego revelado {clip.clip_id}"
        )
        paths[clip.clip_id] = target_path
        audits[clip.clip_id] = {
            "path": str(target_path),
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "sample_width_bytes": info.sample_width_bytes,
            "frames": info.frames,
            "duration_seconds": info.duration_seconds,
            "file_sha256": whole_sha,
            "pcm_payload_sha256": payload_sha,
        }
    return paths, audits, file_sha256(resolved)


def run_analyzers(
    python_executable: Path,
    split_manifest: Mapping[str, Any],
    targets: Mapping[str, Path],
    analysis_tag: str,
    staging_results: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    summaries: dict[str, Any] = {}
    temporary_dirs: dict[str, Path] = {}
    for clip in BLIND5.clips:
        clip_id = clip.clip_id
        candidate = Path(split_manifest["candidates"][clip_id]["path"])
        output_dir = staging_results / clip_id
        temporary_dirs[clip_id] = output_dir
        output_prefix = f"{clip_id}_{analysis_tag}"
        command = [
            str(python_executable),
            str(ANALYZER),
            "--raw",
            str(clip.raw_path()),
            "--candidate",
            str(candidate),
            "--target",
            str(targets[clip_id]),
            "--output-dir",
            str(output_dir),
            "--output-prefix",
            output_prefix,
            "--label",
            f"{clip_id} {analysis_tag} — blind5 pos-freeze",
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"analisador falhou no ID {clip_id} "
                f"(codigo {completed.returncode}): {details}"
            )
        try:
            summaries[clip_id] = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"analisador retornou JSON invalido no ID {clip_id}") from error
    return summaries, temporary_dirs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--targets-manifest", type=Path, required=True)
    parser.add_argument("--analysis-tag", required=True)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--confirm-frozen",
        required=True,
        help=f"confirmacao literal obrigatoria: {CONFIRMATION}",
    )
    return parser


def execute(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 12):
        raise ValueError("use o Python 3.12 do runtime do projeto")
    if args.confirm_frozen != CONFIRMATION:
        raise ValueError(
            "confirmacao recusada; nenhum manifesto de alvo foi aberto. "
            f"Use --confirm-frozen {CONFIRMATION} somente apos o freeze."
        )
    if not TAG_PATTERN.fullmatch(args.analysis_tag):
        raise ValueError("--analysis-tag invalido")
    python_executable = args.python.expanduser().resolve()
    if not python_executable.is_file() or not ANALYZER.is_file():
        raise ValueError("Python ou analisador inexistente")

    # Toda a validacao do snapshot ocorre antes de abrir o manifesto de alvos.
    split_manifest, split_sha = validate_split_manifest(args.split_manifest)
    for clip in BLIND5.clips:
        validate_raw(clip)

    # Primeira e unica porta que pode abrir alvos cegos.
    target_paths, target_audits, targets_manifest_sha = validate_targets_manifest(
        args.targets_manifest, split_sha
    )

    results_dir = args.results_dir.expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    analysis_dirs = {
        clip.clip_id: results_dir / f"{clip.clip_id}_{args.analysis_tag}"
        for clip in BLIND5.clips
    }
    aggregate_json = results_dir / f"{args.analysis_tag}_RELATORIO_CEGO_AGREGADO.json"
    aggregate_md = results_dir / f"{args.analysis_tag}_RELATORIO_CEGO_AGREGADO.md"
    collisions = [
        *[path for path in analysis_dirs.values() if path.exists()],
        *[path for path in (aggregate_json, aggregate_md) if path.exists()],
    ]
    if collisions:
        raise ValueError("saidas ja existem; escolha outro --analysis-tag")

    staging_results = Path(
        tempfile.mkdtemp(prefix=f".{args.analysis_tag}.blind.", dir=results_dir)
    )
    created_dirs: list[Path] = []
    created_reports: list[Path] = []
    try:
        summaries, temporary_dirs = run_analyzers(
            python_executable,
            split_manifest,
            target_paths,
            args.analysis_tag,
            staging_results,
        )
        for clip in BLIND5.clips:
            destination = analysis_dirs[clip.clip_id]
            temporary_dirs[clip.clip_id].rename(destination)
            created_dirs.append(destination)
            prefix = f"{clip.clip_id}_{args.analysis_tag}_COMPARACAO"
            summaries[clip.clip_id]["json"] = str(destination / f"{prefix}.json")
            summaries[clip.clip_id]["markdown"] = str(
                destination / f"{prefix}.md"
            )

        technical = {
            "format_passed": True,
            "frame_sum_passed": True,
            "hashes_passed": True,
            "order_passed": True,
            "total_frames": split_manifest["expected_total_frames_from_frozen_raws"],
            "duration_seconds": (
                split_manifest["expected_total_frames_from_frozen_raws"] / 48_000
            ),
            "order_proof": split_manifest["order_proof"],
            "frozen_split_manifest_sha256": split_sha,
            "targets_manifest_sha256": targets_manifest_sha,
            "targets_opened_only_after_confirmation": True,
            "target_audits": target_audits,
        }
        aggregate = aggregate_summaries(
            BLIND5, args.analysis_tag, summaries, technical
        )
        # Neste script, blindness_preserved significa que houve cegueira ate o
        # freeze; depois da confirmacao, a comparacao foi deliberadamente aberta.
        aggregate["blindness_preserved_until_freeze"] = True
        aggregate["blindness_preserved"] = False
        write_json_exclusive(aggregate_json, aggregate)
        created_reports.append(aggregate_json)
        with aggregate_md.open("x", encoding="utf-8") as destination:
            destination.write(aggregate_markdown(aggregate))
            destination.write(
                "\n- Cegueira preservada ate o freeze: **sim**; alvos abertos "
                "somente nesta etapa pos-freeze.\n"
            )
        created_reports.append(aggregate_md)
    except Exception:
        for path in reversed(created_reports):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for path in reversed(created_dirs):
            shutil.rmtree(path, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging_results, ignore_errors=True)

    print(
        json.dumps(
            {
                "analysis_tag": args.analysis_tag,
                "fixed_order": list(BLIND5.order),
                "aggregate_json": str(aggregate_json),
                "aggregate_markdown": str(aggregate_md),
                "blindness_preserved_until_freeze": True,
                "comparison_completed": True,
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
