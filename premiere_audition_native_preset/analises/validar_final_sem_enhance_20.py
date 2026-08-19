#!/usr/bin/env python3
"""Revele e valide uma unica cadeia sem Enhance ja congelada nos 20 pares.

Este runner e a etapa final, separada do ajuste. Ele fica deliberadamente
inutilizavel sem as tres declaracoes explicitas abaixo:

* ``--frozen-candidate``: concat final exportado pelo Premiere;
* ``--frozen-candidate-sha256``: SHA-256 exato congelado antes da revelacao;
* ``--confirm-holdout-reveal FROZEN_CANDIDATE_HASH_VERIFIED``.

Na execucao real, o programa exige o conjunto autorizado, na ordem fixa
64,65,66,67,68,69,71,72,73,74,75,76,81,82,84,92,94,95,101,107; rejeita
qualquer divergencia do preflight; exige PCM24 estereo 48 kHz e exatamente
25.181.516 frames; prova a ordem dos segmentos; executa os dois analisadores
canonicos e publica tabela CSV/JSON/Markdown, agregados e gates por um unico
rename atomico. Nenhum preset ou projeto Adobe e aberto ou modificado.

``--preflight-only`` valida apenas o concat/hash e os metadados JSON do
preflight: nao abre os payloads dos raws/alvos. ``--self-test`` usa somente
linhas sinteticas e tambem nao abre nenhum arquivo de audio.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
import tempfile
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

import avaliar_grade_sem_enhance_20 as grade
import validar_concats_finais as common
import validar_fluxo_aprovado_20 as frozen


ROOT = Path(__file__).resolve().parents[2]
PRESET_ROOT = ROOT / "premiere_audition_native_preset"
BASE = PRESET_ROOT / "comparacao_ab" / "FLUXO_APROVADO_20_20260814"
OUTPUT_ROOT = BASE / "resultados_sem_enhance_final20"

EXPECTED_IDS = (
    "64",
    "65",
    "66",
    "67",
    "68",
    "69",
    "71",
    "72",
    "73",
    "74",
    "75",
    "76",
    "81",
    "82",
    "84",
    "92",
    "94",
    "95",
    "101",
    "107",
)
EXPECTED_TOTAL_FRAMES = 25_181_516
CONFIRMATION = "FROZEN_CANDIDATE_HASH_VERIFIED"
CANDIDATE_LABEL = "C1_FINAL_FROZEN"


def require_runtime() -> None:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("use Python 3.12 do runtime canonico do workspace")
    if np.__version__ != "2.3.5":
        raise ValueError(f"use NumPy 2.3.5; recebido {np.__version__}")


def require_contract_constants() -> None:
    if tuple(frozen.CLIP_IDS) != EXPECTED_IDS:
        raise ValueError("IDs do pipeline canonico divergiram do contrato final")
    if len(EXPECTED_IDS) != 20 or len(set(EXPECTED_IDS)) != 20:
        raise ValueError("contrato final nao contem exatamente 20 IDs unicos")
    if set(EXPECTED_IDS) & set(frozen.EXPLICITLY_EXCLUDED_IDS):
        raise ValueError("contrato final contem ID explicitamente excluido")
    if grade.EXPECTED_TOTAL_FRAMES != EXPECTED_TOTAL_FRAMES:
        raise ValueError("contagem de frames divergiu do runner de calibracao")
    if grade.EXPECTED_SAMPLE_RATE != 48_000:
        raise ValueError("sample rate canonico divergiu")
    if grade.EXPECTED_CHANNELS != 2 or grade.EXPECTED_SAMPLE_WIDTH_BYTES != 3:
        raise ValueError("formato canonico divergiu de PCM24 estereo")


def preflight_manifest_metadata_only() -> dict[str, Any]:
    """Valide o contrato JSON sem abrir qualquer WAV raw/target."""

    path = frozen.PREFLIGHT_MANIFEST
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"manifesto de preflight ausente/inseguro: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"preflight nao e JSON valido: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("schema do preflight deve ser 1")
    if payload.get("suite") != frozen.SUITE_NAME:
        raise ValueError("suite do preflight divergiu")
    if payload.get("fixed_order") != list(EXPECTED_IDS):
        raise ValueError("preflight nao contem exatamente os 20 IDs na ordem fixa")
    if payload.get("preflight_status") != "PASS_20_OF_20":
        raise ValueError("preflight nao esta fechado como PASS_20_OF_20")
    if payload.get("counts") != {"pairs": 20, "raws": 20, "targets": 20}:
        raise ValueError("contagens do preflight divergiram de 20/20/20")
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("scope ausente no preflight")
    if scope.get("authorized_ids_only") != list(EXPECTED_IDS):
        raise ValueError("scope do preflight divergiu dos 20 IDs autorizados")
    excluded = scope.get("explicitly_excluded_ids")
    if not isinstance(excluded, list) or set(excluded) != set(
        frozen.EXPLICITLY_EXCLUDED_IDS
    ):
        raise ValueError("IDs excluidos do preflight divergiram")
    if scope.get("ids_below_64_read") is not False:
        raise ValueError("preflight nao comprova bloqueio de IDs abaixo de 64")
    raws = payload.get("raws")
    targets = payload.get("targets")
    pairs = payload.get("pairs")
    if not isinstance(raws, Mapping) or not isinstance(targets, Mapping):
        raise ValueError("mapas raws/targets ausentes no preflight")
    frozen.require_exact_keys(raws, "raws do preflight")
    frozen.require_exact_keys(targets, "targets do preflight")
    if not isinstance(pairs, list) or len(pairs) != 20:
        raise ValueError("preflight nao contem exatamente 20 pares")
    pair_ids = tuple(
        str(record.get("id")) if isinstance(record, Mapping) else ""
        for record in pairs
    )
    if pair_ids != EXPECTED_IDS:
        raise ValueError("pares do preflight divergiram da ordem fixa")
    return {
        "path": str(path.resolve()),
        "file_sha256": common.file_sha256(path),
        "suite": payload["suite"],
        "fixed_order": payload["fixed_order"],
        "counts": payload["counts"],
        "payload_audio_read": False,
    }


def inspect_frozen_candidate(
    path_value: Path, expected_sha256: str
) -> tuple[Path, dict[str, Any]]:
    if not frozen.SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("--frozen-candidate-sha256 deve ser SHA-256 minusculo")
    path = frozen.require_within(path_value, PRESET_ROOT, "concat final congelado")
    records = grade.inspect_candidates({CANDIDATE_LABEL: path})
    record = records[CANDIDATE_LABEL]
    if record["frames"] != EXPECTED_TOTAL_FRAMES:
        raise ValueError("concat congelado nao tem exatamente 25.181.516 frames")
    if record["file_sha256"] != expected_sha256:
        raise ValueError(
            "hash do concat nao corresponde ao hash congelado esperado: "
            f"recebido {record['file_sha256']}"
        )
    return path, record


def final_grade_row(clip_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    comparison = payload.get("comparison")
    sibilance = payload.get("sibilance")
    if not isinstance(comparison, Mapping) or not isinstance(sibilance, Mapping):
        raise ValueError(f"payload incompleto no ID {clip_id}")
    if comparison.get("schema_version") != 1 or sibilance.get("schema_version") != 1:
        raise ValueError(f"schema de analisador invalido no ID {clip_id}")
    primary = grade.nested(comparison, "comparisons", "candidate_vs_target")
    bands = grade.nested(
        primary,
        "spectral_active_after_integrated_loudness_match",
        "band_delta_db",
    )
    s_metrics = sibilance.get("metrics")
    candidate_audit = grade.nested(comparison, "files", "candidate")
    if not isinstance(primary, Mapping) or not isinstance(bands, Mapping):
        raise ValueError(f"espectro ativo ausente no ID {clip_id}")
    if not isinstance(s_metrics, Mapping) or not isinstance(candidate_audit, Mapping):
        raise ValueError(f"auditoria/sibilancia ausente no ID {clip_id}")
    core = [
        grade.finite(bands.get(name), f"{clip_id}/{name}")
        for name in grade.CORE_BANDS
    ]
    return {
        "clip_id": clip_id,
        "core_voice_mae_db": float(np.mean(np.abs(core))),
        "quiet_spectral_mae_db": grade.finite(
            grade.nested(
                primary,
                "spectral_quiet_after_voice_match",
                "mean_absolute_error_db",
            ),
            f"quiet MAE {clip_id}",
        ),
        "background_after_voice_match_db": grade.finite(
            grade.nested(primary, "background_after_voice_match", "quiet_rms_delta_db"),
            f"fundo {clip_id}",
        ),
        "loudness_delta_lu": grade.finite(
            grade.nested(
                primary,
                "native_delivery_file_delta",
                "integrated_loudness_delta_lu",
            ),
            f"loudness {clip_id}",
        ),
        "active_voice_delta_db": grade.finite(
            grade.nested(primary, "active", "candidate_minus_reference_rms_db"),
            f"voz {clip_id}",
        ),
        "sample_peak_dbfs": grade.finite(
            grade.nested(candidate_audit, "sample_peak", "mono_dbfs"),
            f"sample peak {clip_id}",
        ),
        "true_peak_dbtp": grade.finite(
            grade.nested(candidate_audit, "true_peak_approx", "mono_dbtp"),
            f"true peak {clip_id}",
        ),
        "sibilance_event_median_delta_db": grade.finite(
            s_metrics.get("target_sibilant_ratio_delta_median_db"),
            f"S mediano {clip_id}",
        ),
        "sibilance_event_p90_abs_db": grade.finite(
            s_metrics.get("target_sibilant_ratio_delta_p90_absolute_db"),
            f"S P90 {clip_id}",
        ),
        "band_delta_db": {
            name: grade.finite(bands.get(name), f"{clip_id}/{name}")
            for name in grade.CORE_BANDS
        },
    }


def final_grade_aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(rows) != 20 or tuple(row.get("clip_id") for row in rows) != EXPECTED_IDS:
        raise ValueError("agregado final exige exatamente os 20 IDs na ordem fixa")

    def values(key: str) -> list[float]:
        return [grade.finite(row.get(key), f"final/{key}") for row in rows]

    core = values("core_voice_mae_db")
    background = values("background_after_voice_match_db")
    loudness = values("loudness_delta_lu")
    voice = values("active_voice_delta_db")
    s_median = values("sibilance_event_median_delta_db")
    s_p90 = values("sibilance_event_p90_abs_db")
    quiet = values("quiet_spectral_mae_db")
    sample_peaks = values("sample_peak_dbfs")
    true_peaks = values("true_peak_dbtp")
    band_medians = {
        name: grade.median(
            [
                grade.finite(row["band_delta_db"].get(name), f"final/{name}")
                for row in rows
            ],
            f"mediana final/{name}",
        )
        for name in grade.CORE_BANDS
    }
    summary: dict[str, Any] = {
        "schema_version": 1,
        "scope": "all_20_post_freeze",
        "clips": 20,
        "fixed_order": list(EXPECTED_IDS),
        "core_voice_mae_median_db": grade.median(core, "MAE core final"),
        "core_voice_mae_p90_db": grade.percentile(core, 0.90, "P90 core final"),
        "quiet_spectral_mae_median_db": grade.median(quiet, "quiet MAE final"),
        "background_abs_median_db": grade.median(
            map(abs, background), "fundo final"
        ),
        "background_signed_median_db": grade.median(
            background, "fundo assinado final"
        ),
        "loudness_abs_median_lu": grade.median(
            map(abs, loudness), "loudness final"
        ),
        "loudness_abs_max_lu": max(map(abs, loudness)),
        "active_voice_abs_median_db": grade.median(map(abs, voice), "voz final"),
        "active_voice_abs_max_db": max(map(abs, voice)),
        "sibilance_event_median_abs_median_db": grade.median(
            map(abs, s_median), "S mediano final"
        ),
        "sibilance_event_p90_median_db": grade.median(s_p90, "S P90 final"),
        "sample_peak_max_dbfs": max(sample_peaks),
        "true_peak_max_dbtp": max(true_peaks),
        "core_band_median_delta_db": band_medians,
    }
    gates = {
        "core_mae_median_le_1_25db": summary["core_voice_mae_median_db"]
        <= grade.FINAL_CORE_MAE_MEDIAN_DB,
        "core_mae_p90_le_1_75db": summary["core_voice_mae_p90_db"]
        <= grade.FINAL_CORE_MAE_P90_DB,
        "all_core_band_medians_abs_le_1_50db": all(
            abs(value) <= grade.FINAL_BAND_MEDIAN_ABS_DB
            for value in band_medians.values()
        ),
        "loudness_median_abs_le_0_50lu": summary["loudness_abs_median_lu"]
        <= grade.FINAL_LOUDNESS_MEDIAN_ABS_LU,
        "loudness_each_abs_le_1lu": summary["loudness_abs_max_lu"]
        <= grade.FINAL_LOUDNESS_MAX_ABS_LU,
        "voice_median_abs_le_0_50db": summary["active_voice_abs_median_db"]
        <= grade.FINAL_VOICE_MEDIAN_ABS_DB,
        "voice_each_abs_le_1db": summary["active_voice_abs_max_db"]
        <= grade.FINAL_VOICE_MAX_ABS_DB,
        "background_median_abs_le_3db": summary["background_abs_median_db"]
        <= grade.FINAL_BACKGROUND_MEDIAN_ABS_DB,
        "sibilance_event_median_abs_le_1db": summary[
            "sibilance_event_median_abs_median_db"
        ]
        <= grade.FINAL_SIBILANCE_MEDIAN_ABS_DB,
        "sample_peak_le_minus0_29dbfs": summary["sample_peak_max_dbfs"]
        <= grade.FINAL_SAMPLE_PEAK_MAX_DBFS,
        "true_peak_below_0dbtp": summary["true_peak_max_dbtp"]
        < grade.FINAL_TRUE_PEAK_MAX_DBTP,
    }
    summary["gates"] = gates
    summary["hard_gate_passed"] = all(gates.values())
    summary["failed_gates"] = [name for name, passed in gates.items() if not passed]
    summary["gate_contract_source"] = str(Path(grade.__file__).resolve())
    return summary


def render_final_grade_markdown(
    tag: str,
    candidate_sha256: str,
    aggregate: Mapping[str, Any],
) -> str:
    lines = [
        f"# Agregados finais sem Enhance — 20 pares — {tag}",
        "",
        f"- Candidato congelado: `{CANDIDATE_LABEL}`",
        f"- SHA-256 exigido e confirmado: `{candidate_sha256}`",
        f"- IDs revelados uma unica vez: `{','.join(EXPECTED_IDS)}`",
        "- Formato exigido: `PCM24, estereo, 48 kHz, 25.181.516 frames`",
        "",
        "## Metricas agregadas",
        "",
        f"- MAE de timbre core: mediana {aggregate['core_voice_mae_median_db']:.3f} dB; P90 {aggregate['core_voice_mae_p90_db']:.3f} dB.",
        f"- Loudness: mediana absoluta {aggregate['loudness_abs_median_lu']:.3f} LU; maximo absoluto {aggregate['loudness_abs_max_lu']:.3f} LU.",
        f"- Voz ativa: mediana absoluta {aggregate['active_voice_abs_median_db']:.3f} dB; maximo absoluto {aggregate['active_voice_abs_max_db']:.3f} dB.",
        f"- Fundo apos casar voz: mediana absoluta {aggregate['background_abs_median_db']:.3f} dB.",
        f"- Sibilancia por evento: mediana absoluta {aggregate['sibilance_event_median_abs_median_db']:.3f} dB.",
        f"- Peaks: sample {aggregate['sample_peak_max_dbfs']:.3f} dBFS; true peak aprox. {aggregate['true_peak_max_dbtp']:.3f} dBTP.",
        "",
        "## Gates finais",
        "",
    ]
    for name, passed in aggregate["gates"].items():
        lines.append(f"- `{name}`: **{'PASS' if passed else 'REJECT'}**")
    lines.extend(
        [
            "",
            f"Veredito agregado: **{'PASS' if aggregate['hard_gate_passed'] else 'REJECT'}**.",
            "",
            "O codigo de saida 0 significa somente que a medicao foi concluida e publicada; consulte os gates para a qualidade sonora.",
            "",
        ]
    )
    return "\n".join(lines)


def write_final_reports(
    results_staging: Path,
    tag: str,
    candidate_sha256: str,
    pipeline_aggregate: Mapping[str, Any],
    final_rows: Sequence[Mapping[str, Any]],
    final_aggregate: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    frozen.write_reports(results_staging, pipeline_aggregate, manifest, tag)
    (results_staging / "AGREGADOS_GATES_FINAL.json").write_text(
        json.dumps(
            {"aggregate": final_aggregate, "per_clip": final_rows},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (results_staging / "AGREGADOS_GATES_FINAL.md").write_text(
        render_final_grade_markdown(tag, candidate_sha256, final_aggregate),
        encoding="utf-8",
    )


def synthetic_pipeline_row(clip_id: str) -> dict[str, Any]:
    gates = {
        "loudness_abs_le_1lu": True,
        "active_rms_abs_le_1db": True,
        "active_spectral_mae_le_1_5db": True,
        "envelope_corr_ge_0_98": True,
        "sample_peak_abs_delta_le_0_5db": True,
        "true_peak_abs_delta_le_0_5db": True,
        "background_rms_abs_delta_le_3db": True,
        "active_4k_8k_abs_le_1_5db": True,
        "sibilant_event_median_abs_le_1db": True,
        "sibilant_event_p90_abs_le_2db": True,
    }
    row = {
        "clip_id": clip_id,
        "integrated_loudness_delta_lu": 0.0,
        "active_rms_delta_db": 0.0,
        "background_after_voice_match_db": 0.0,
        "quiet_median_after_voice_match_db": 0.0,
        "envelope_correlation": 1.0,
        "active_envelope_correlation": 1.0,
        "active_spectral_mae_db": 0.0,
        "quiet_spectral_mae_db": 0.0,
        "active_4k_8k_delta_db": 0.0,
        "sibilant_event_median_delta_db": 0.0,
        "sibilant_event_p90_abs_db": 0.0,
        "sample_peak_delta_db": 0.0,
        "true_peak_delta_db": 0.0,
        "gates_passed": 10,
        "gates_failed": 0,
        "gates_unavailable": 0,
        "gates_total": 10,
        "verdict": "APROVADO",
        "failed_gate_names": [],
        "unavailable_gate_names": [],
        "gates": gates,
    }
    return row


def synthetic_final_row(clip_id: str) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "core_voice_mae_db": 0.0,
        "quiet_spectral_mae_db": 0.0,
        "background_after_voice_match_db": 0.0,
        "loudness_delta_lu": 0.0,
        "active_voice_delta_db": 0.0,
        "sample_peak_dbfs": -0.30,
        "true_peak_dbtp": -0.01,
        "sibilance_event_median_delta_db": 0.0,
        "sibilance_event_p90_abs_db": 0.0,
        "band_delta_db": {name: 0.0 for name in grade.CORE_BANDS},
    }


def self_test() -> int:
    require_contract_constants()
    pipeline_rows = [synthetic_pipeline_row(clip_id) for clip_id in EXPECTED_IDS]
    technical = {
        "total_frames": EXPECTED_TOTAL_FRAMES,
        "duration_seconds": EXPECTED_TOTAL_FRAMES / 48_000,
    }
    pipeline_aggregate = frozen.aggregate_rows(pipeline_rows, technical)
    final_rows = [synthetic_final_row(clip_id) for clip_id in EXPECTED_IDS]
    final_aggregate = final_grade_aggregate(final_rows)
    if pipeline_aggregate["overall_quality_verdict"] != "ALL_20_APPROVED":
        raise ValueError("self-test do agregado do pipeline falhou")
    if not final_aggregate["hard_gate_passed"]:
        raise ValueError("self-test dos gates finais falhou")
    with tempfile.TemporaryDirectory(prefix="final20-self-test-") as directory:
        root = Path(directory)
        write_final_reports(
            root,
            "SELF_TEST",
            "0" * 64,
            pipeline_aggregate,
            final_rows,
            final_aggregate,
            {"schema_version": 1, "self_test": True},
        )
        expected = {
            "TABELA_COMPARACAO.csv",
            "TABELA_COMPARACAO.json",
            "TABELA_COMPARACAO.md",
            "MANIFESTO_EXECUCAO.json",
            "AGREGADOS_GATES_FINAL.json",
            "AGREGADOS_GATES_FINAL.md",
        }
        if {path.name for path in root.iterdir()} != expected:
            raise ValueError("self-test nao publicou o conjunto exato de relatorios")
        with (root / "TABELA_COMPARACAO.csv").open(
            "r", encoding="utf-8", newline=""
        ) as source:
            if len(list(csv.DictReader(source))) != 20:
                raise ValueError("self-test da tabela nao produziu 20 linhas")
    print(
        json.dumps(
            {
                "status": "SELF_TEST_PASS",
                "authorized_ids": list(EXPECTED_IDS),
                "audio_payloads_read": False,
                "holdout_payloads_read": False,
                "real_output_created": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="rotulo unico da execucao final")
    parser.add_argument(
        "--frozen-candidate",
        type=Path,
        help="concat C1 final congelado dentro da arvore do preset",
    )
    parser.add_argument(
        "--frozen-candidate-sha256",
        help="SHA-256 exato registrado antes de revelar o holdout",
    )
    parser.add_argument(
        "--confirm-holdout-reveal",
        help=f"deve ser exatamente {CONFIRMATION}",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="valida candidato/hash e metadados; nao abre raws/alvos",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="smoke sintetico; nao le nenhum arquivo de audio",
    )
    return parser


def execute(args: argparse.Namespace) -> int:
    require_runtime()
    require_contract_constants()
    if args.self_test:
        if any(
            value is not None
            for value in (
                args.tag,
                args.frozen_candidate,
                args.frozen_candidate_sha256,
                args.confirm_holdout_reveal,
            )
        ) or args.preflight_only:
            raise ValueError("--self-test deve ser usado sozinho")
        return self_test()

    if not args.tag or not frozen.TAG_PATTERN.fullmatch(args.tag):
        raise ValueError("--tag seguro e unico e obrigatorio")
    if args.frozen_candidate is None:
        raise ValueError("--frozen-candidate e obrigatorio")
    if args.frozen_candidate_sha256 is None:
        raise ValueError("--frozen-candidate-sha256 e obrigatorio")
    if args.confirm_holdout_reveal != CONFIRMATION:
        raise ValueError(
            "revelacao bloqueada: informe --confirm-holdout-reveal " + CONFIRMATION
        )
    for analyzer in (frozen.ANALYZER, frozen.SIBILANCE_ANALYZER):
        if not analyzer.is_file():
            raise ValueError(f"analisador inexistente: {analyzer}")

    preflight_metadata = preflight_manifest_metadata_only()
    candidate_path, candidate_record = inspect_frozen_candidate(
        args.frozen_candidate, args.frozen_candidate_sha256
    )
    if args.preflight_only:
        print(
            json.dumps(
                {
                    "status": "FINAL20_PREFLIGHT_PASS",
                    "candidate": candidate_record,
                    "preflight_manifest": preflight_metadata,
                    "authorized_ids": list(EXPECTED_IDS),
                    "raw_target_audio_payloads_read": False,
                    "holdout_revealed": False,
                    "real_output_created": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    preflight_hash = preflight_metadata["file_sha256"]
    suite, raw_infos, target_infos, frozen_hashes = frozen.load_frozen_suite()
    if suite.order != EXPECTED_IDS:
        raise ValueError("suite carregada divergiu da ordem exata dos 20 IDs")
    if set(raw_infos) != set(EXPECTED_IDS) or set(target_infos) != set(EXPECTED_IDS):
        raise ValueError("suite carregada nao contem exatamente 20 raws e 20 alvos")
    expected_frames = sum(raw_infos[clip_id].frames for clip_id in EXPECTED_IDS)
    if expected_frames != EXPECTED_TOTAL_FRAMES:
        raise ValueError("soma dos raws divergiu de 25.181.516 frames")

    if OUTPUT_ROOT.is_symlink():
        raise ValueError("pasta de resultados nao pode ser link simbolico")
    run_root = OUTPUT_ROOT / args.tag
    frozen.reject_collisions([run_root])
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if OUTPUT_ROOT.resolve().parent != BASE.resolve():
        raise ValueError("pasta de resultados escapou da base congelada")
    required_bytes = candidate_path.stat().st_size + sum(
        Path(info.path).stat().st_size
        for info in (*raw_infos.values(), *target_infos.values())
    )
    if shutil.disk_usage(OUTPUT_ROOT).free < required_bytes + 128 * 1024 * 1024:
        raise ValueError("espaco livre insuficiente para a validacao final")

    temporary_root = Path(tempfile.mkdtemp(prefix=f".{args.tag}.", dir=OUTPUT_ROOT))
    candidates_staging = temporary_root / "candidatos_split"
    results_staging = temporary_root / "analises"
    candidates_staging.mkdir()
    results_staging.mkdir()
    committed = False
    try:
        frozen_suite, frozen_input_records = frozen.freeze_suite_inputs(
            suite, frozen_hashes, temporary_root
        )
        candidate_info = common.inspect_wave(candidate_path, "concat final congelado")
        segment_paths, segment_records = common.prepare_segments(
            frozen_suite, candidate_info, candidates_staging
        )
        if tuple(segment_paths) != EXPECTED_IDS:
            raise ValueError("split nao devolveu exatamente os 20 IDs na ordem fixa")
        order_proof = frozen.prove_order(
            frozen_suite,
            segment_paths,
            frozen.ORDER_MIN_CORRELATION,
            frozen.ORDER_MIN_MARGIN,
        )
        summaries, payloads = frozen.run_analyzers(
            frozen_suite, segment_paths, results_staging, args.tag
        )
        frozen.require_exact_keys(summaries, "resumos finais")
        frozen.require_exact_keys(payloads, "payloads finais")
        pipeline_rows = [
            frozen.make_row(clip_id, summaries[clip_id], payloads[clip_id])
            for clip_id in EXPECTED_IDS
        ]
        technical = {
            "format_passed": True,
            "frame_sum_passed": True,
            "frozen_candidate_sha256_passed": True,
            "hashes_passed": True,
            "split_sample_exact_against_concat_passed": True,
            "order_passed": True,
            "total_frames": EXPECTED_TOTAL_FRAMES,
            "duration_seconds": EXPECTED_TOTAL_FRAMES / 48_000,
            "order_proof": order_proof,
        }
        pipeline_aggregate = frozen.aggregate_rows(pipeline_rows, technical)
        final_rows = [
            final_grade_row(clip_id, payloads[clip_id]) for clip_id in EXPECTED_IDS
        ]
        final_aggregate = final_grade_aggregate(final_rows)

        candidate_records: dict[str, Any] = {}
        by_record = {record["clip_id"]: record for record in segment_records}
        frozen.require_exact_keys(by_record, "registros do split")
        for clip_id in EXPECTED_IDS:
            path = segment_paths[clip_id]
            info = common.inspect_wave(path, f"candidato split {clip_id}")
            payload_sha, frames = common.payload_sha256_and_frames(
                info, f"candidato split {clip_id}"
            )
            record = by_record[clip_id]
            if frames != record["frames"] or payload_sha != record["pcm_payload_sha256"]:
                raise ValueError(f"payload do split mudou no ID {clip_id}")
            candidate_records[clip_id] = {
                **asdict(info),
                "duration_seconds": info.duration_seconds,
                "file_sha256": common.file_sha256(path),
                "pcm_payload_sha256": payload_sha,
                "start_frame_in_concat": record["start_frame_in_concat"],
                "stop_frame_in_concat_exclusive": record[
                    "stop_frame_in_concat_exclusive"
                ],
            }

        manifest = {
            "schema_version": 1,
            "execution_status": "COMPLETE_MEASUREMENT_POST_FREEZE",
            "exit_code_semantics": (
                "0 = medicao integra e publicada; consulte hard_gate_passed e "
                "overall_quality_verdict"
            ),
            "tag": args.tag,
            "suite": frozen.SUITE_NAME,
            "fixed_order": list(EXPECTED_IDS),
            "authorized_ids_count": 20,
            "explicitly_excluded_ids_not_read": sorted(
                frozen.EXPLICITLY_EXCLUDED_IDS, key=int
            ),
            "holdout_reveal": {
                "performed": True,
                "post_freeze_only": True,
                "confirmation_token": CONFIRMATION,
                "candidate_label": CANDIDATE_LABEL,
                "expected_file_sha256": args.frozen_candidate_sha256,
                "received_file_sha256": candidate_record["file_sha256"],
                "hash_passed": True,
            },
            "preflight_manifest": {
                "path": str(frozen.PREFLIGHT_MANIFEST.resolve()),
                "file_sha256": preflight_hash,
            },
            "concat": candidate_record,
            "candidate_segments": candidate_records,
            "frozen_inputs_actually_read_by_analyzers": frozen_input_records,
            "technical_validation": technical,
            "pipeline_overall_quality_verdict": pipeline_aggregate[
                "overall_quality_verdict"
            ],
            "final_chain_hard_gate_passed": final_aggregate["hard_gate_passed"],
            "analysis_scripts": {
                "comparison": str(frozen.ANALYZER),
                "sibilance": str(frozen.SIBILANCE_ANALYZER),
                "calibration_grade_contract": str(Path(grade.__file__).resolve()),
                "all20_pipeline": str(Path(frozen.__file__).resolve()),
                "runner": str(Path(__file__).resolve()),
            },
            "preset_or_project_modified": False,
        }
        write_final_reports(
            results_staging,
            args.tag,
            args.frozen_candidate_sha256,
            pipeline_aggregate,
            final_rows,
            final_aggregate,
            manifest,
        )

        if common.file_sha256(frozen.PREFLIGHT_MANIFEST) != preflight_hash:
            raise ValueError("preflight mudou durante a execucao")
        if common.file_sha256(candidate_path) != args.frozen_candidate_sha256:
            raise ValueError("candidato congelado mudou durante a execucao")
        for clip_id in EXPECTED_IDS:
            if common.file_sha256(Path(raw_infos[clip_id].path)) != frozen_hashes[
                clip_id
            ]["raw_file_sha256"]:
                raise ValueError(f"raw {clip_id} mudou durante a execucao")
            if common.file_sha256(Path(target_infos[clip_id].path)) != frozen_hashes[
                clip_id
            ]["target_file_sha256"]:
                raise ValueError(f"alvo {clip_id} mudou durante a execucao")

        frozen.rebase_generated_paths(temporary_root, temporary_root, run_root)
        if run_root.exists():
            raise ValueError("tag apareceu durante a execucao; commit cancelado")
        temporary_root.rename(run_root)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(temporary_root, ignore_errors=True)

    reports = run_root / "analises"
    print(
        json.dumps(
            {
                "status": "COMPLETE_MEASUREMENT_POST_FREEZE",
                "run_root": str(run_root),
                "table_csv": str(reports / "TABELA_COMPARACAO.csv"),
                "table_json": str(reports / "TABELA_COMPARACAO.json"),
                "table_markdown": str(reports / "TABELA_COMPARACAO.md"),
                "aggregate_gates_json": str(
                    reports / "AGREGADOS_GATES_FINAL.json"
                ),
                "aggregate_gates_markdown": str(
                    reports / "AGREGADOS_GATES_FINAL.md"
                ),
                "pipeline_overall_quality_verdict": pipeline_aggregate[
                    "overall_quality_verdict"
                ],
                "final_chain_hard_gate_passed": final_aggregate[
                    "hard_gate_passed"
                ],
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
