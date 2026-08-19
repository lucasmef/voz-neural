#!/usr/bin/env python3
"""Executa os quatro analisadores do export real 107 shelf-only.

Execute com o Python científico empacotado do workspace. O programa falha sem
criar resultados quando o export real ou qualquer controle estiver ausente.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ANALYSES = ROOT / "premiere_audition_native_preset/analises"
RESULTS = ANALYSES / "resultados/107_SHELF60M12_ONLY_PREMIERE_REAL"
OFFICIAL = RESULTS / "oficial"

RAW = Path("/private/tmp/codex-dn35-107-gates/107_RAW_DJI.wav")
BASELINE = Path("/private/tmp/codex-dn35-107-gates/107_REF_E100_AUTOMATCH_LIMITER.wav")
CANDIDATE = ROOT / "premiere_audition_native_preset/comparacao_ab/107_ensaio/exports_testes/107_SHELF12_ONLY_E100_AM_LIM_PREMIERE_REAL.wav"
TARGET = ROOT / "premiere_audition_native_preset/comparacao_ab/107_ensaio/107_TARGET_CAPCUT.wav"

GATES_JSON = RESULTS / "107_SHELF_ONLY_GATES.json"
AMBIENCE_JSON = RESULTS / "107_SHELF_ONLY_RUIDO_AMBIENCIA.json"
SIBILANCE_JSON = RESULTS / "107_SHELF_ONLY_SIBILANCIA.json"
OFFICIAL_PREFIX = "107_SHELF60M12_ONLY_PREMIERE_REAL"
OFFICIAL_JSON = OFFICIAL / f"{OFFICIAL_PREFIX}_COMPARACAO.json"
SUMMARY_JSON = RESULTS / "RESUMO_GATES.json"
SUMMARY_MD = RESULTS / "RESUMO_GATES.md"
RUN_MANIFEST = RESULTS / "MANIFESTO_EXECUCAO.json"
LABEL = "107 Shelf 60 Hz -12 dB only E100 Auto-Match limiter real"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(script: str, *args: str) -> list[str]:
    command = [sys.executable, str(ANALYSES / script), *args]
    subprocess.run(command, cwd=ROOT, check=True)
    return command


def main() -> int:
    required = [RAW, BASELINE, CANDIDATE, TARGET]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print("ANALISE NAO EXECUTADA: arquivos ausentes:", file=sys.stderr)
        for path in missing:
            print(f"- {path}", file=sys.stderr)
        return 3

    expected_outputs = [
        GATES_JSON,
        AMBIENCE_JSON,
        SIBILANCE_JSON,
        OFFICIAL_JSON,
        SUMMARY_JSON,
        SUMMARY_MD,
        RUN_MANIFEST,
    ]
    existing = [str(path) for path in expected_outputs if path.exists()]
    if existing:
        print("RECUSA DE OVERWRITE: resultados ja existem:", file=sys.stderr)
        for path in existing:
            print(f"- {path}", file=sys.stderr)
        return 4

    RESULTS.mkdir(parents=True, exist_ok=True)
    OFFICIAL.mkdir(parents=True, exist_ok=True)

    commands: list[list[str]] = []
    commands.append(
        run(
            "avaliar_dn35_107.py",
            "--raw", str(RAW),
            "--baseline", str(BASELINE),
            "--candidate", str(CANDIDATE),
            "--target", str(TARGET),
            "--output", str(GATES_JSON),
            "--label", LABEL,
        )
    )
    commands.append(
        run(
            "comparar_wavs_adobe.py",
            "--raw", str(RAW),
            "--candidate", str(CANDIDATE),
            "--target", str(TARGET),
            "--output-dir", str(OFFICIAL),
            "--output-prefix", OFFICIAL_PREFIX,
            "--label", LABEL,
        )
    )
    commands.append(
        run(
            "comparar_ruido_ambiencia.py",
            "--raw", str(RAW),
            "--candidate", str(CANDIDATE),
            "--target", str(TARGET),
            "--output", str(AMBIENCE_JSON),
        )
    )
    commands.append(
        run(
            "medir_sibilancia_4_8.py",
            "--raw", str(RAW),
            "--candidate", str(CANDIDATE),
            "--target", str(TARGET),
            "--label", LABEL,
            "--output", str(SIBILANCE_JSON),
        )
    )

    specific = json.loads(GATES_JSON.read_text(encoding="utf-8"))
    official = json.loads(OFFICIAL_JSON.read_text(encoding="utf-8"))
    primary = official["comparisons"]["candidate_vs_target"]
    phrase = specific["phrase_preservation_vs_premiere_baseline"]
    tone = specific["active_tone_vs_capcut"]
    sibilance = specific["sibilance"]

    summary = {
        "schema": "codex.premiere.107-shelf-only-real-analysis-summary.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "label": LABEL,
        "candidate_sha256": sha256(CANDIDATE),
        "specific_107_gates": specific["gates"],
        "specific_107_gate_summary": specific["gate_summary"],
        "direct_vs_e100_automatch_limiter_baseline": {
            "baseline_sha256": sha256(BASELINE),
            "candidate_to_baseline_active_voice_match_gain_db": specific["match_gains_db"]["candidate_to_baseline_active_voice"],
            "max_abs_phrase_change_db": phrase["max_abs_phrase_change_db"],
            "phrase_delta_median_db": phrase["phrase_delta_median_db"],
            "phrase_dispersion_increase_db": phrase["dispersion_increase_db"],
            "active_log_envelope_correlation": phrase["active_log_envelope_correlation"],
            "active_80hz_4khz_max_extra_error_vs_capcut_db": tone["max_extra_absolute_error_db"],
            "sibilance_broad_extra_error_vs_capcut_db": sibilance["active_4k_8k_extra_absolute_error_db"],
            "sibilance_event_median_extra_error_vs_capcut_db": sibilance["event_extra_error_median_db"],
            "sibilance_event_p90_extra_error_vs_capcut_db": sibilance["event_extra_error_p90_db"],
        },
        "candidate_vs_capcut_specific_intervals": {
            "background_rms_delta_db": specific["background_vs_capcut"]["aggregate_rms_delta_db"],
            "background_median_frame_delta_db": specific["background_vs_capcut"]["aggregate_median_frame_delta_db"],
        },
        "official_candidate_vs_capcut": {
            "integrated_loudness_delta_lu": primary["production_level"]["candidate_minus_reference_lu"],
            "active_rms_delta_db": primary["active"]["candidate_minus_reference_rms_db"],
            "background_after_voice_match_db": primary["background_after_voice_match"]["quiet_rms_delta_db"],
            "background_median_after_voice_match_db": primary["background_after_voice_match"]["quiet_median_delta_db"],
            "envelope_correlation": primary["envelope"]["all_log_rms_correlation"],
            "active_spectral_mae_db": primary["spectral_active_after_integrated_loudness_match"]["mean_absolute_error_db"],
            "sample_peak_delta_db": primary["native_delivery_file_delta"]["sample_peak_delta_db"],
            "true_peak_delta_db": primary["native_delivery_file_delta"]["true_peak_delta_db"],
            "quality_gates": official["initial_quality_gates"],
        },
        "analyzer_outputs": {
            "specific_gates": str(GATES_JSON),
            "official": str(OFFICIAL_JSON),
            "ambience": str(AMBIENCE_JSON),
            "sibilance": str(SIBILANCE_JSON),
        },
    }
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    passed = specific["gate_summary"]["passed"]
    total = specific["gate_summary"]["total"]
    failed = specific["gate_summary"]["failed"]
    official_gates = official["initial_quality_gates"]
    official_failed = [name for name, ok in official_gates.items() if not ok]
    lines = [
        "# 107 — Shelf 60 Hz -12 dB somente — export real",
        "",
        f"- Gates específicos 107: **{passed}/{total}**; falhas: {', '.join(failed) if failed else 'nenhuma'}.",
        f"- Gates oficiais: **{sum(bool(v) for v in official_gates.values())}/{len(official_gates)}**; falhas: {', '.join(official_failed) if official_failed else 'nenhuma'}.",
        f"- Fundo nos intervalos fixos vs CapCut: {summary['candidate_vs_capcut_specific_intervals']['background_rms_delta_db']:+.3f} dB RMS; mediana {summary['candidate_vs_capcut_specific_intervals']['background_median_frame_delta_db']:+.3f} dB.",
        f"- Frases vs baseline Premiere: mudança máxima {phrase['max_abs_phrase_change_db']:.3f} dB; dispersão {phrase['dispersion_increase_db']:+.3f} dB; correlação de envelope ativo {phrase['active_log_envelope_correlation']:.5f}.",
        f"- Oficial vs CapCut: loudness {summary['official_candidate_vs_capcut']['integrated_loudness_delta_lu']:+.3f} LU; fundo {summary['official_candidate_vs_capcut']['background_after_voice_match_db']:+.3f} dB; MAE espectral ativa {summary['official_candidate_vs_capcut']['active_spectral_mae_db']:.3f} dB; correlação de envelope {summary['official_candidate_vs_capcut']['envelope_correlation']:.5f}.",
        "",
        "Resultado objetivo não substitui a escuta A/B para bombeamento, ruído musical e finais de palavras.",
    ]
    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "schema": "codex.analysis-run.v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.executable,
        "files": {str(path): sha256(path) for path in required},
        "analyzers": {
            str(ANALYSES / script): sha256(ANALYSES / script)
            for script in (
                "avaliar_dn35_107.py",
                "comparar_wavs_adobe.py",
                "comparar_ruido_ambiencia.py",
                "medir_sibilancia_4_8.py",
            )
        },
        "commands": commands,
        "summary": str(SUMMARY_JSON),
    }
    RUN_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
