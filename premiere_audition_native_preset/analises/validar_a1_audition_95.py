#!/usr/bin/env python3
"""Valide o WAV A1 do take 95 antes e depois da terminacao no Premiere.

O helper nao processa audio. Ele exige o enquadramento canonico do take 95,
executa a grade fechada de ``analisar_grade_final_95.py`` em uma pasta de
resultado separada e transforma os dados do candidato A1 em um preflight
objetivo. No estagio ``final``, tambem retorna erro se qualquer um dos 13
gates contra o CapCut falhar.

Use ``--stage audition`` no WAV que acabou de sair da reducao de ruido do
Audition. Depois de Auto-Match, EQ v2, DeEsser M38 e limiter v1 no Premiere,
use ``--stage final`` no WAV realmente renderizado pelo Premiere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any


ANALYSIS_DIR = Path(__file__).resolve().parent
NATIVE_ROOT = ANALYSIS_DIR.parent
GRADE_SCRIPT = ANALYSIS_DIR / "analisar_grade_final_95.py"
BASE_MANIFEST = ANALYSIS_DIR / "GRADE_FINAL_95_MANIFEST.json"
EXPECTED_RATE = 48_000
EXPECTED_CHANNELS = 2
EXPECTED_SAMPLE_WIDTH = 3
EXPECTED_FRAMES = 1_899_200
MAX_RELATIVE_LAG_MS = 1.0
MIN_COMMON_DURATION_SECONDS = 39.54
DEFAULT_NAMES = {
    "audition": "95_A1_AUDITION_NR_ONLY.wav",
    "final": "95_A1_AUDITION_PREMIERE_FINAL.wav",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def validate_candidate_path(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"WAV A1 ausente: {path}")
    if path.suffix.lower() != ".wav":
        raise ValueError(f"A1 precisa ser WAV PCM: {path.name}")
    if "blind" in str(path).lower():
        raise ValueError(f"caminho cego proibido: {path}")
    match = re.match(r"^0*(\d{2,3})(?:[-_.]|$)", path.name)
    if not match or int(match.group(1)) != 95:
        raise ValueError(f"o nome do A1 precisa comecar pelo ID 95: {path.name}")


def wav_format(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as source:
        result = {
            "sample_rate": source.getframerate(),
            "channels": source.getnchannels(),
            "sample_width_bytes": source.getsampwidth(),
            "bits_per_sample": source.getsampwidth() * 8,
            "frames": source.getnframes(),
            "duration_seconds": source.getnframes() / source.getframerate(),
            "compression": source.getcomptype(),
        }
    return result


def expected_format_gates(info: dict[str, Any]) -> dict[str, bool]:
    return {
        "pcm_uncompressed": info["compression"] == "NONE",
        "sample_rate_48000": info["sample_rate"] == EXPECTED_RATE,
        "stereo": info["channels"] == EXPECTED_CHANNELS,
        "pcm24": info["sample_width_bytes"] == EXPECTED_SAMPLE_WIDTH,
        "frames_exact_1899200": info["frames"] == EXPECTED_FRAMES,
    }


def write_stage_manifest(stage: str, candidate: Path, output_dir: Path) -> Path:
    manifest = json.loads(BASE_MANIFEST.read_text(encoding="utf-8"))
    labels = {
        "audition": "A1 Audition — reducao de ruido, antes da terminacao",
        "final": "A1 Audition + terminacao Premiere, sem Enhance",
    }
    chains = {
        "audition": "Noise Reduction (process) do Audition; sem terminacao de entrega",
        "final": (
            "Noise Reduction (process) Audition -> Dialogue/Enhance OFF -> "
            "Auto-Match -23 recalculado -> EQv2 -> DeEsser M38 -> limiter final"
        ),
    }
    slot = next(item for item in manifest["candidates"] if item["id"] == "N0")
    slot["label"] = labels[stage]
    slot["expected_chain"] = chains[stage]
    slot["paths"] = [str(candidate)]
    path = output_dir / "95_A1_GRADE_MANIFEST.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def run_grade(candidate: Path, output_dir: Path, manifest: Path) -> Path:
    command = [
        sys.executable,
        str(GRADE_SCRIPT),
        "--manifest",
        str(manifest),
        "--candidate",
        f"N0={candidate}",
        "--output-dir",
        str(output_dir),
    ]
    completed = subprocess.run(command, check=False, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"analisar_grade_final_95.py terminou com codigo {completed.returncode}"
        )
    result_path = output_dir / "95_GRADE_FINAL_RANKING.json"
    if not result_path.is_file():
        raise RuntimeError(f"grade nao criou o JSON esperado: {result_path}")
    return result_path


def candidate_a1(result: dict[str, Any], expected_path: Path) -> dict[str, Any]:
    matches = [item for item in result["candidates"] if item.get("id") == "N0"]
    if len(matches) != 1 or matches[0].get("status") != "analyzed":
        raise RuntimeError("a grade nao analisou o A1 no slot N0")
    item = matches[0]
    if Path(item["source_path"]).resolve() != expected_path:
        raise RuntimeError("a grade analisou um arquivo diferente do A1 solicitado")
    return item


def markdown_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"# Validacao A1 Audition/Premiere — take 95 ({summary['stage']})",
        "",
        f"Arquivo: `{summary['candidate']['path']}`",
        "",
        f"SHA-256: `{summary['candidate']['sha256']}`",
        "",
        "## Enquadramento",
        "",
        "| Gate | Resultado |",
        "|---|:---:|",
    ]
    for name, passed in summary["framing_gates"].items():
        lines.append(f"| {name} | {'PASSA' if passed else 'FALHA'} |")
    lines.extend(
        [
            "",
            "## Alinhamento",
            "",
            f"- lag relativo ao bruto: `{summary['alignment']['candidate_minus_raw_lag_ms']:+.6f} ms`;",
            f"- duracao comum analisada: `{summary['alignment']['analyzed_duration_seconds']:.6f} s`;",
            f"- correlacao candidato/CapCut usada no alinhamento: `{summary['alignment']['candidate_to_target_correlation']:.6f}`.",
            "",
            "## Gates contra o CapCut",
            "",
            f"Resultado: `{summary['capcut_gates']['passed']}/{summary['capcut_gates']['total']}`; "
            + ("elegivel." if summary["capcut_gates"]["eligible"] else "nao elegivel."),
            "",
            "Falhas: "
            + (", ".join(summary["capcut_gates"]["failed"]) or "nenhuma")
            + ".",
            "",
        ]
    )
    if summary["stage"] == "audition":
        lines.extend(
            [
                "Neste estagio os gates de entrega sao apenas diagnosticos: ainda faltam",
                "Auto-Match, EQ v2, DeEsser M38 e limiter v1 no Premiere.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "No estagio final, elegibilidade exige enquadramento, alinhamento e os",
                "13 gates automaticos. A escuta A/B perceptual continua obrigatoria.",
                "",
            ]
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("audition", "final"), required=True)
    parser.add_argument(
        "--candidate",
        type=Path,
        help="WAV A1; por padrao usa exports_validacao/95_A1_...wav",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError as error:
        raise SystemExit(
            "NumPy ausente. Use o Python 3.12 do runtime do projeto."
        ) from error

    default_candidate = NATIVE_ROOT / "exports_validacao" / DEFAULT_NAMES[args.stage]
    candidate = (args.candidate or default_candidate).expanduser().resolve()
    output_dir = (
        args.output_dir
        or NATIVE_ROOT / "analises/resultados" / f"95_A1_{args.stage.upper()}"
    ).expanduser().resolve()
    try:
        validate_candidate_path(candidate)
        info = wav_format(candidate)
        format_gates = expected_format_gates(info)
        output_dir.mkdir(parents=True, exist_ok=True)
        stage_manifest = write_stage_manifest(args.stage, candidate, output_dir)
        grade_path = run_grade(candidate, output_dir, stage_manifest)
        grade = json.loads(grade_path.read_text(encoding="utf-8"))
        a1 = candidate_a1(grade, candidate)
    except (ValueError, RuntimeError, wave.Error) as error:
        print(f"FALHA: {error}", file=sys.stderr)
        return 2

    alignment = a1["alignment"]
    relative_lag_ms = float(alignment["candidate_to_target_lag_ms"]) - float(
        alignment["raw_to_target_lag_ms"]
    )
    timing_gates = {
        "relative_lag_abs_le_1ms": abs(relative_lag_ms) <= MAX_RELATIVE_LAG_MS,
        "common_duration_ge_39p54s": float(alignment["analyzed_duration_seconds"])
        >= MIN_COMMON_DURATION_SECONDS,
        "alignment_values_finite": all(
            math.isfinite(float(alignment[name]))
            for name in (
                "raw_to_target_lag_ms",
                "raw_to_target_correlation",
                "candidate_to_target_lag_ms",
                "candidate_to_target_correlation",
                "analyzed_duration_seconds",
            )
        ),
    }
    framing_gates = {**format_gates, **timing_gates}
    capcut = a1["gate_summary"]
    framing_eligible = all(framing_gates.values())
    final_eligible = framing_eligible and bool(capcut["eligible"])
    summary = {
        "schema_version": 1,
        "stage": args.stage,
        "candidate": {
            "path": str(candidate),
            "bytes": candidate.stat().st_size,
            "sha256": sha256_file(candidate),
            "format": info,
        },
        "framing_gates": framing_gates,
        "framing_eligible": framing_eligible,
        "alignment": {
            **alignment,
            "candidate_minus_raw_lag_ms": relative_lag_ms,
        },
        "capcut_gates": capcut,
        "final_eligible": final_eligible,
        "grade_json": str(grade_path),
        "grade_markdown": str(output_dir / "95_GRADE_FINAL_RANKING.md"),
        "grade_manifest": str(stage_manifest),
        "notes": {
            "audition_stage_capcut_gates_are_diagnostic": args.stage == "audition",
            "perceptual_ab_still_required": True,
            "audio_modified_by_this_helper": False,
        },
    }
    summary_path = output_dir / "95_A1_VALIDATION_SUMMARY.json"
    markdown_path = output_dir / "95_A1_VALIDATION_SUMMARY.md"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(markdown_summary(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "summary_json": str(summary_path),
                "summary_markdown": str(markdown_path),
                "framing_eligible": framing_eligible,
                "capcut_gates": capcut,
                "final_eligible": final_eligible,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not framing_eligible:
        return 2
    if args.stage == "final" and not final_eligible:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
