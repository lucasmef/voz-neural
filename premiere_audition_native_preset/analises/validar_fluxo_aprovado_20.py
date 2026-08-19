#!/usr/bin/env python3
"""Valide e compare o concat real do fluxo Premiere aprovado em 20 pares.

Este programa e deliberadamente *fail-closed*. Ele so conhece os 20 IDs
aprovados, so le os WAVs preparados na pasta congelada deste ensaio e exige:

* manifesto de preflight com hashes e metadados de todos os raws/alvos;
* concat Premiere PCM24, estereo, 48 kHz;
* contagem total de frames exatamente igual a soma dos raws, na ordem fixa;
* prova de ordem/conteudo por impressao digital de envelope;
* divisao sample-exact, sem reamostragem nem alteracao do payload PCM;
* comparacao principal por ``comparar_wavs_adobe.py`` e diagnostico de
  sibilancia por ``medir_sibilancia_4_8.py``.

Nenhum preset ou projeto Adobe e aberto ou modificado. As saidas nunca sao
sobrepostas: use um ``--tag`` novo para cada render real.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

import validar_concats_finais as common


ROOT = Path(__file__).resolve().parents[2]
PRESET_ROOT = ROOT / "premiere_audition_native_preset"
BASE = PRESET_ROOT / "comparacao_ab" / "FLUXO_APROVADO_20_20260814"
RAW_DIR = BASE / "raws_ordered"
TARGET_DIR = BASE / "targets_ordered"
PREFLIGHT_MANIFEST = BASE / "manifest_preflight.json"
OUTPUT_ROOT = BASE / "resultados"

ANALYZER = PRESET_ROOT / "analises" / "comparar_wavs_adobe.py"
SIBILANCE_ANALYZER = PRESET_ROOT / "analises" / "medir_sibilancia_4_8.py"

SUITE_NAME = "fluxo_aprovado_20_20260814"
CLIP_IDS = (
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
EXPLICITLY_EXCLUDED_IDS = frozenset({"21", "38", "39", "51", "70", "77", "83"})

EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_CHANNELS = 2
EXPECTED_SAMPLE_WIDTH_BYTES = 3
FINGERPRINT_BLOCK_FRAMES = 960
FINGERPRINT_MAX_LAG_BLOCKS = 50
ORDER_MIN_CORRELATION = 0.70
ORDER_MIN_MARGIN = 0.05
TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def require_within(path: Path, parent: Path, role: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"{role} nao pode ser link simbolico: {expanded}")
    resolved = expanded.resolve()
    parent_resolved = parent.resolve()
    try:
        resolved.relative_to(parent_resolved)
    except ValueError as error:
        raise ValueError(f"{role} deve ficar dentro de {parent_resolved}: {resolved}") from error
    return resolved


def require_exact_keys(mapping: Mapping[str, Any], role: str) -> None:
    received = set(mapping)
    expected = set(CLIP_IDS)
    if received != expected:
        missing = sorted(expected - received, key=int)
        extras = sorted(received - expected, key=lambda item: int(item) if item.isdigit() else 10**9)
        raise ValueError(
            f"{role} deve conter exatamente os 20 IDs aprovados; "
            f"ausentes={missing}, extras={extras}"
        )
    forbidden = received & EXPLICITLY_EXCLUDED_IDS
    if forbidden:
        raise ValueError(f"falha de seguranca: {role} contem IDs excluidos: {sorted(forbidden)}")


def require_int(record: Mapping[str, Any], key: str, role: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{role}: campo inteiro positivo obrigatorio `{key}`")
    return value


def require_sha(record: Mapping[str, Any], key: str, role: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{role}: SHA-256 minusculo obrigatorio em `{key}`")
    return value


def resolve_record_path(record: Mapping[str, Any], expected: Path, role: str) -> Path:
    """Aceite o campo path apenas se ele apontar para o nome fechado esperado."""

    if expected.is_symlink():
        raise ValueError(f"falha de seguranca: {role} nao pode ser link simbolico")
    value = record.get("path")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{role}: campo `path` obrigatorio")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        # O contrato permite caminhos relativos ao manifesto ou ao workspace.
        from_manifest = (PREFLIGHT_MANIFEST.parent / candidate).resolve()
        from_workspace = (ROOT / candidate).resolve()
        if from_manifest == expected.resolve():
            candidate = from_manifest
        elif from_workspace == expected.resolve():
            candidate = from_workspace
        else:
            candidate = from_manifest
    candidate = candidate.resolve()
    if candidate != expected.resolve():
        raise ValueError(
            f"falha de seguranca: {role} deve apontar exatamente para {expected.resolve()}, "
            f"recebido {candidate}"
        )
    if not candidate.is_file():
        raise ValueError(f"{role} nao e arquivo regular: {candidate}")
    return candidate


def validate_manifest_file(
    record: Mapping[str, Any], expected_path: Path, role: str
) -> tuple[common.WaveInfo, str, str]:
    path = resolve_record_path(record, expected_path, role)
    info = common.inspect_wave(path, role)
    expected_header = {
        "sample_rate": require_int(record, "sample_rate", role),
        "channels": require_int(record, "channels", role),
        "sample_width_bytes": require_int(record, "sample_width_bytes", role),
        "frames": require_int(record, "frames", role),
    }
    actual_header = {
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "sample_width_bytes": info.sample_width_bytes,
        "frames": info.frames,
    }
    if actual_header != expected_header:
        raise ValueError(
            f"{role}: cabecalho divergiu do preflight; "
            f"esperado={expected_header}, recebido={actual_header}"
        )
    size_bytes = record.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise ValueError(f"{role}: size_bytes inteiro obrigatorio")
    actual_size = path.stat().st_size
    if size_bytes != actual_size:
        raise ValueError(
            f"{role}: size_bytes divergiu; manifesto={size_bytes}, recebido={actual_size}"
        )
    if record.get("encoding") != "PCM_signed_integer":
        raise ValueError(f"{role}: encoding deve ser PCM_signed_integer")
    if record.get("bit_depth") != info.sample_width_bytes * 8:
        raise ValueError(f"{role}: bit_depth diverge do cabecalho")
    duration = finite_number(record.get("duration_seconds"))
    if duration is None or not math.isclose(
        duration, info.duration_seconds, rel_tol=0.0, abs_tol=1.0e-9
    ):
        raise ValueError(f"{role}: duration_seconds diverge do cabecalho")
    expected_file_sha = require_sha(record, "file_sha256", role)
    expected_payload_sha = require_sha(record, "pcm_payload_sha256", role)
    file_sha = common.file_sha256(path)
    if file_sha != expected_file_sha:
        raise ValueError(
            f"{role}: arquivo mudou apos o preflight; SHA-256 recebido {file_sha}"
        )
    payload_sha, _frames = common.payload_sha256_and_frames(info, role)
    if payload_sha != expected_payload_sha:
        raise ValueError(
            f"{role}: payload PCM mudou apos o preflight; SHA-256 recebido {payload_sha}"
        )
    return info, file_sha, payload_sha


def load_frozen_suite() -> tuple[
    common.SuiteSpec,
    dict[str, common.WaveInfo],
    dict[str, common.WaveInfo],
    dict[str, dict[str, str]],
]:
    if not PREFLIGHT_MANIFEST.is_file():
        raise ValueError(
            f"preflight ainda nao existe; nao execute antes de preparar os 20 pares: "
            f"{PREFLIGHT_MANIFEST}"
        )
    try:
        payload = json.loads(PREFLIGHT_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"manifesto de preflight nao e JSON valido: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("manifesto de preflight deve ser um objeto JSON")
    if payload.get("schema_version") != 1:
        raise ValueError("schema_version do preflight deve ser 1")
    if payload.get("suite") != SUITE_NAME:
        raise ValueError(
            f"suite invalida no preflight; esperado `{SUITE_NAME}`, "
            f"recebido `{payload.get('suite')}`"
        )
    if not isinstance(payload.get("generated_at"), str) or not payload["generated_at"]:
        raise ValueError("generated_at ausente no preflight")
    if payload.get("fixed_order") != list(CLIP_IDS):
        raise ValueError(
            "ordem do preflight diverge da ordem fixa aprovada: " + ",".join(CLIP_IDS)
        )
    if payload.get("preflight_status") != "PASS_20_OF_20":
        raise ValueError("preflight nao esta fechado como PASS_20_OF_20")
    counts = payload.get("counts")
    if counts != {"pairs": 20, "raws": 20, "targets": 20}:
        raise ValueError(f"contagens invalidas no preflight: {counts}")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("objeto `scope` ausente no preflight")
    if scope.get("authorized_ids_only") != list(CLIP_IDS):
        raise ValueError("scope.authorized_ids_only diverge dos 20 IDs aprovados")
    excluded_ids = scope.get("explicitly_excluded_ids")
    if not isinstance(excluded_ids, list) or set(excluded_ids) != EXPLICITLY_EXCLUDED_IDS:
        raise ValueError("scope.explicitly_excluded_ids diverge da exclusao aprovada")
    if scope.get("ids_below_64_read") is not False:
        raise ValueError("preflight nao comprova que IDs abaixo de 64 ficaram sem leitura")
    if scope.get("preset_modified") is not False:
        raise ValueError("preflight indica modificacao de preset")
    raw_records = payload.get("raws")
    target_records = payload.get("targets")
    if not isinstance(raw_records, dict) or not isinstance(target_records, dict):
        raise ValueError("preflight deve conter objetos `raws` e `targets`")
    require_exact_keys(raw_records, "raws do preflight")
    require_exact_keys(target_records, "targets do preflight")

    for directory, role in (
        (BASE, "pasta base"),
        (RAW_DIR, "pasta de raws"),
        (TARGET_DIR, "pasta de alvos"),
    ):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"{role} deve ser diretorio regular, sem symlink: {directory}")

    clips: list[common.ClipSpec] = []
    raw_infos: dict[str, common.WaveInfo] = {}
    target_infos: dict[str, common.WaveInfo] = {}
    frozen_hashes: dict[str, dict[str, str]] = {}
    for position, clip_id in enumerate(CLIP_IDS, start=1):
        raw_record = raw_records[clip_id]
        target_record = target_records[clip_id]
        if not isinstance(raw_record, dict) or not isinstance(target_record, dict):
            raise ValueError(f"registros raw/target do ID {clip_id} devem ser objetos")
        raw_path = RAW_DIR / f"{position:02d}_{clip_id}_RAW_STEREO48.wav"
        target_path = TARGET_DIR / f"{position:02d}_{clip_id}_TARGET_CAPCUT.wav"
        raw_info, raw_file_sha, raw_payload_sha = validate_manifest_file(
            raw_record, raw_path, f"raw aprovado {clip_id}"
        )
        target_info, target_file_sha, target_payload_sha = validate_manifest_file(
            target_record, target_path, f"alvo CapCut {clip_id}"
        )
        if (
            raw_info.sample_rate != EXPECTED_SAMPLE_RATE
            or raw_info.channels != EXPECTED_CHANNELS
            or raw_info.sample_width_bytes not in (2, 3, 4)
            or raw_info.compression != "NONE"
        ):
            raise ValueError(
                f"raw {clip_id} deve ser PCM16/24/32 estereo 48 kHz; recebido "
                f"{raw_info.sample_width_bytes * 8} bit, {raw_info.channels} canais, "
                f"{raw_info.sample_rate} Hz"
            )
        if target_info.format_key != (44_100, 2, 2, "NONE"):
            raise ValueError(
                f"alvo {clip_id} deve ser PCM16 estereo 44,1 kHz; recebido "
                f"{target_info.sample_width_bytes * 8} bit, {target_info.channels} canais, "
                f"{target_info.sample_rate} Hz"
            )
        if raw_record.get("order") != position or target_record.get("order") != position:
            raise ValueError(f"ordem interna incorreta nos registros do ID {clip_id}")
        raw_relative = str(raw_path.resolve().relative_to(ROOT))
        target_relative = str(target_path.resolve().relative_to(ROOT))
        clips.append(
            common.ClipSpec(
                clip_id=clip_id,
                raw_relative=raw_relative,
                expected_raw_file_sha256=raw_file_sha,
                expected_frames=raw_info.frames,
                target_relative=target_relative,
                expected_target_file_sha256=target_file_sha,
            )
        )
        raw_infos[clip_id] = raw_info
        target_infos[clip_id] = target_info
        frozen_hashes[clip_id] = {
            "raw_file_sha256": raw_file_sha,
            "raw_pcm_payload_sha256": raw_payload_sha,
            "target_file_sha256": target_file_sha,
            "target_pcm_payload_sha256": target_payload_sha,
        }
    total_frames = sum(info.frames for info in raw_infos.values())
    if payload.get("raws_total_frames") != total_frames:
        raise ValueError(
            f"raws_total_frames inconsistente: manifesto={payload.get('raws_total_frames')}, "
            f"calculado={total_frames}"
        )
    total_duration = finite_number(payload.get("raws_total_duration_seconds"))
    expected_total_duration = total_frames / EXPECTED_SAMPLE_RATE
    if total_duration is None or not math.isclose(
        total_duration, expected_total_duration, rel_tol=0.0, abs_tol=1.0e-9
    ):
        raise ValueError("raws_total_duration_seconds inconsistente")
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != len(CLIP_IDS):
        raise ValueError("lista `pairs` deve conter exatamente 20 registros")
    for position, (clip_id, pair) in enumerate(zip(CLIP_IDS, pairs, strict=True), start=1):
        if not isinstance(pair, dict):
            raise ValueError(f"pairs[{position - 1}] deve ser um objeto")
        raw_expected = f"raws_ordered/{position:02d}_{clip_id}_RAW_STEREO48.wav"
        target_expected = (
            f"targets_ordered/{position:02d}_{clip_id}_TARGET_CAPCUT.wav"
        )
        expected_pair = {
            "id": clip_id,
            "order": position,
            "raw_path": raw_expected,
            "target_path": target_expected,
            "raw_frames": raw_infos[clip_id].frames,
            "target_frames": target_infos[clip_id].frames,
        }
        if pair != expected_pair:
            raise ValueError(
                f"registro pairs do ID {clip_id} diverge do conjunto congelado"
            )
    return (
        common.SuiteSpec(name=SUITE_NAME, clips=tuple(clips), blind=False),
        raw_infos,
        target_infos,
        frozen_hashes,
    )


def reject_collisions(paths: Iterable[Path]) -> None:
    collisions = [path for path in paths if path.exists()]
    if collisions:
        listing = "\n  - ".join(str(path) for path in collisions)
        raise ValueError(
            "a execucao nunca sobrescreve resultados; use outro --tag. "
            f"Colisoes:\n  - {listing}"
        )


def decode_mono_for_fingerprint(path: Path) -> np.ndarray:
    """Decodifique PCM16/24/32; o raw 101 congelado e PCM32."""

    info = common.inspect_wave(path, "audio para prova de ordem")
    with wave.open(str(path), "rb") as source:
        payload = source.readframes(info.frames)
    if info.sample_width_bytes == 2:
        flat = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    elif info.sample_width_bytes == 3:
        octets = np.frombuffer(payload, dtype=np.uint8)
        if octets.size % 3:
            raise ValueError(f"PCM24 malformado: {path}")
        triples = octets.reshape(-1, 3).astype(np.int32)
        integer = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        integer = np.where(integer & 0x800000, integer - 0x1000000, integer)
        flat = integer.astype(np.float32) / 8388608.0
    elif info.sample_width_bytes == 4:
        flat = (
            np.frombuffer(payload, dtype="<i4").astype(np.float32)
            / 2147483648.0
        )
    else:
        raise ValueError(f"prova de ordem nao aceita PCM{info.sample_width_bytes * 8}: {path}")
    expected = info.frames * info.channels
    if flat.size != expected:
        raise ValueError(f"payload PCM malformado na prova de ordem: {path}")
    return np.mean(
        flat.reshape(info.frames, info.channels), axis=1, dtype=np.float32
    )


def log_rms_fingerprint(path: Path) -> np.ndarray:
    mono = decode_mono_for_fingerprint(path)
    complete = (mono.size // FINGERPRINT_BLOCK_FRAMES) * FINGERPRINT_BLOCK_FRAMES
    if complete < 20 * FINGERPRINT_BLOCK_FRAMES:
        raise ValueError(f"audio curto demais para provar ordem: {path}")
    framed = mono[:complete].astype(np.float64).reshape(
        -1, FINGERPRINT_BLOCK_FRAMES
    )
    values = np.sqrt(np.mean(framed * framed, axis=1) + 1.0e-14)
    return np.log10(values + 1.0e-7)


def pearson(left: np.ndarray, right: np.ndarray) -> float:
    count = min(left.size, right.size)
    if count < 20:
        return 0.0
    a = np.array(left[:count], dtype=np.float64, copy=True)
    b = np.array(right[:count], dtype=np.float64, copy=True)
    a -= float(np.mean(a))
    b -= float(np.mean(b))
    denominator = math.sqrt(float(np.dot(a, a) * np.dot(b, b)))
    return float(np.dot(a, b) / denominator) if denominator > 1.0e-12 else 0.0


def best_fingerprint_correlation(
    candidate: np.ndarray, raw: np.ndarray
) -> tuple[float, int]:
    best = (-1.0, 0)
    for lag in range(-FINGERPRINT_MAX_LAG_BLOCKS, FINGERPRINT_MAX_LAG_BLOCKS + 1):
        if lag > 0:
            left = candidate[lag:]
            right = raw[:-lag]
        elif lag < 0:
            left = candidate[:lag]
            right = raw[-lag:]
        else:
            left = candidate
            right = raw
        correlation = pearson(left, right)
        if correlation > best[0]:
            best = (correlation, lag)
    return best


def prove_order(
    suite: common.SuiteSpec,
    segment_paths: Mapping[str, Path],
    minimum_correlation: float,
    minimum_margin: float,
) -> dict[str, Any]:
    raw_fingerprints = {
        clip.clip_id: log_rms_fingerprint(clip.raw_path()) for clip in suite.clips
    }
    candidate_fingerprints = {
        clip.clip_id: log_rms_fingerprint(segment_paths[clip.clip_id])
        for clip in suite.clips
    }
    rows: dict[str, Any] = {}
    for clip in suite.clips:
        clip_id = clip.clip_id
        correlations: dict[str, float] = {}
        lags: dict[str, int] = {}
        for raw in suite.clips:
            correlation, lag = best_fingerprint_correlation(
                candidate_fingerprints[clip_id], raw_fingerprints[raw.clip_id]
            )
            correlations[raw.clip_id] = correlation
            lags[raw.clip_id] = lag
        expected = correlations[clip_id]
        competitors = {
            raw_id: value
            for raw_id, value in correlations.items()
            if raw_id != clip_id
        }
        best_other_id, best_other = max(
            competitors.items(), key=lambda item: item[1]
        )
        margin = expected - best_other
        passed = expected >= minimum_correlation and margin >= minimum_margin
        rows[clip_id] = {
            "expected_raw_id": clip_id,
            "expected_correlation": expected,
            "expected_lag_blocks_20ms": lags[clip_id],
            "best_competing_raw_id": best_other_id,
            "best_competing_correlation": best_other,
            "margin": margin,
            "minimum_correlation": minimum_correlation,
            "minimum_margin": minimum_margin,
            "passed": passed,
            "correlation_by_raw_id": correlations,
            "lag_blocks_20ms_by_raw_id": lags,
        }
        if not passed:
            raise ValueError(
                f"ordem/conteudo nao comprovado no segmento {clip_id}: "
                f"correlacao esperada {expected:.6f}, melhor concorrente "
                f"{best_other_id}={best_other:.6f}, margem {margin:.6f}"
            )
    return {
        "method": "correlacao maxima do envelope log-RMS em blocos de 20 ms",
        "maximum_lag_seconds": FINGERPRINT_MAX_LAG_BLOCKS * 0.020,
        "all_passed": True,
        "rows": rows,
    }


def freeze_suite_inputs(
    suite: common.SuiteSpec,
    frozen_hashes: Mapping[str, Mapping[str, str]],
    staging_root: Path,
) -> tuple[common.SuiteSpec, dict[str, dict[str, Any]]]:
    """Copie os 40 WAVs autorizados e analise somente as copias imutaveis."""

    frozen_root = staging_root / "inputs_frozen"
    frozen_raw_dir = frozen_root / "raws"
    frozen_target_dir = frozen_root / "targets"
    frozen_raw_dir.mkdir(parents=True)
    frozen_target_dir.mkdir(parents=True)
    frozen_clips: list[common.ClipSpec] = []
    records: dict[str, dict[str, Any]] = {}
    for clip in suite.clips:
        clip_id = clip.clip_id
        source_raw = clip.raw_path().resolve()
        source_target = clip.target_path()
        if source_target is None:
            raise ValueError(f"alvo ausente ao congelar ID {clip_id}")
        source_target = source_target.resolve()
        frozen_raw = frozen_raw_dir / source_raw.name
        frozen_target = frozen_target_dir / source_target.name
        shutil.copyfile(source_raw, frozen_raw)
        shutil.copyfile(source_target, frozen_target)

        raw_info = common.inspect_wave(frozen_raw, f"copia congelada raw {clip_id}")
        target_info = common.inspect_wave(
            frozen_target, f"copia congelada target {clip_id}"
        )
        raw_file_sha = common.file_sha256(frozen_raw)
        target_file_sha = common.file_sha256(frozen_target)
        raw_payload_sha, _ = common.payload_sha256_and_frames(
            raw_info, f"copia congelada raw {clip_id}"
        )
        target_payload_sha, _ = common.payload_sha256_and_frames(
            target_info, f"copia congelada target {clip_id}"
        )
        expected = frozen_hashes[clip_id]
        received = {
            "raw_file_sha256": raw_file_sha,
            "raw_pcm_payload_sha256": raw_payload_sha,
            "target_file_sha256": target_file_sha,
            "target_pcm_payload_sha256": target_payload_sha,
        }
        if received != dict(expected):
            raise ValueError(f"copia congelada divergiu do preflight no ID {clip_id}")
        frozen_clips.append(
            common.ClipSpec(
                clip_id=clip_id,
                raw_relative=str(frozen_raw.resolve().relative_to(ROOT)),
                expected_raw_file_sha256=raw_file_sha,
                expected_frames=raw_info.frames,
                target_relative=str(frozen_target.resolve().relative_to(ROOT)),
                expected_target_file_sha256=target_file_sha,
            )
        )
        records[clip_id] = {
            "raw": {
                **asdict(raw_info),
                "file_sha256": raw_file_sha,
                "pcm_payload_sha256": raw_payload_sha,
            },
            "target": {
                **asdict(target_info),
                "file_sha256": target_file_sha,
                "pcm_payload_sha256": target_payload_sha,
            },
        }
    return (
        common.SuiteSpec(name=suite.name, clips=tuple(frozen_clips), blind=False),
        records,
    )


def run_json_command(command: Sequence[str], role: str) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"{role} falhou (codigo {completed.returncode}): {details}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{role} retornou JSON invalido") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{role} nao retornou um objeto JSON")
    return value


def run_analyzers(
    suite: common.SuiteSpec,
    candidates: Mapping[str, Path],
    results_staging: Path,
    tag: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    summaries: dict[str, dict[str, Any]] = {}
    full_comparisons: dict[str, dict[str, Any]] = {}
    for position, clip in enumerate(suite.clips, start=1):
        clip_id = clip.clip_id
        print(
            f"[{position:02d}/{len(suite.clips):02d}] analisando ID {clip_id}...",
            file=sys.stderr,
            flush=True,
        )
        target = clip.target_path()
        if target is None:
            raise RuntimeError(f"alvo ausente no ID {clip_id}")
        output_dir = results_staging / clip_id
        prefix = f"{clip_id}_{tag}"
        summary = run_json_command(
            [
                sys.executable,
                str(ANALYZER),
                "--raw",
                str(clip.raw_path()),
                "--candidate",
                str(candidates[clip_id]),
                "--target",
                str(target),
                "--output-dir",
                str(output_dir),
                "--output-prefix",
                prefix,
                "--label",
                f"{clip_id} — fluxo Premiere aprovado x CapCut",
            ],
            f"comparar_wavs_adobe.py no ID {clip_id}",
        )
        comparison_path = output_dir / f"{prefix}_COMPARACAO.json"
        try:
            comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"comparacao completa ausente/invalida no ID {clip_id}: {comparison_path}"
            ) from error
        sibilance_path = output_dir / f"{prefix}_SIBILANCIA_4_8.json"
        sibilance = run_json_command(
            [
                sys.executable,
                str(SIBILANCE_ANALYZER),
                "--raw",
                str(clip.raw_path()),
                "--candidate",
                str(candidates[clip_id]),
                "--target",
                str(target),
                "--label",
                f"{clip_id} — fluxo Premiere aprovado x CapCut",
                "--output",
                str(sibilance_path),
            ],
            f"medir_sibilancia_4_8.py no ID {clip_id}",
        )
        try:
            sibilance_from_file = json.loads(sibilance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"analise de sibilancia ausente/invalida no ID {clip_id}: {sibilance_path}"
            ) from error
        if sibilance_from_file != sibilance:
            raise RuntimeError(
                f"stdout e arquivo da analise de sibilancia divergem no ID {clip_id}"
            )
        expected_paths = {
            "raw": clip.raw_path().resolve(),
            "candidate": candidates[clip_id].resolve(),
            "target": target.resolve(),
        }
        expected_hashes = {
            "raw": clip.expected_raw_file_sha256,
            "candidate": common.file_sha256(candidates[clip_id]),
            "target": clip.expected_target_file_sha256,
        }
        comparison_files = comparison.get("files")
        sibilance_files = sibilance.get("files")
        if not isinstance(comparison_files, Mapping) or not isinstance(
            sibilance_files, Mapping
        ):
            raise RuntimeError(f"auditoria de arquivos ausente no ID {clip_id}")
        for role, expected_path in expected_paths.items():
            comparison_record = comparison_files.get(role)
            sibilance_record = sibilance_files.get(role)
            if not isinstance(comparison_record, Mapping) or not isinstance(
                sibilance_record, Mapping
            ):
                raise RuntimeError(f"auditoria do arquivo {role} ausente no ID {clip_id}")
            if Path(str(comparison_record.get("path"))).resolve() != expected_path:
                raise RuntimeError(
                    f"comparador leu caminho {role} inesperado no ID {clip_id}"
                )
            if Path(str(sibilance_record.get("path"))).resolve() != expected_path:
                raise RuntimeError(
                    f"medidor de sibilancia leu caminho {role} inesperado no ID {clip_id}"
                )
            if sibilance_record.get("sha256") != expected_hashes[role]:
                raise RuntimeError(
                    f"hash efetivamente lido em {role} divergiu no ID {clip_id}"
                )
        summaries[clip_id] = summary
        full_comparisons[clip_id] = {
            "comparison": comparison,
            "sibilance": sibilance,
        }
        print(
            f"[{position:02d}/{len(suite.clips):02d}] ID {clip_id} concluido",
            file=sys.stderr,
            flush=True,
        )
    return summaries, full_comparisons


def nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def abs_le(value: Any, limit: float) -> bool | None:
    number = finite_number(value)
    return abs(number) <= limit if number is not None else None


def ge(value: Any, limit: float) -> bool | None:
    number = finite_number(value)
    return number >= limit if number is not None else None


def le(value: Any, limit: float) -> bool | None:
    number = finite_number(value)
    return number <= limit if number is not None else None


def require_gate(value: Any, role: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(
        f"{role} deve ser booleano ou null; recebido {type(value).__name__}"
    )


def validated_gate(external: Any, computed: bool | None, role: str) -> bool | None:
    reported = require_gate(external, role)
    if reported is not computed:
        raise ValueError(
            f"{role} diverge da metrica: reportado={reported}, recalculado={computed}"
        )
    return computed


def make_row(
    clip_id: str,
    summary: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if nested(payload, "comparison", "schema_version") != 1:
        raise ValueError(f"comparacao principal do ID {clip_id} tem schema invalido")
    if nested(payload, "sibilance", "schema_version") != 1:
        raise ValueError(f"analise de sibilancia do ID {clip_id} tem schema invalido")
    primary = nested(payload, "comparison", "comparisons", "candidate_vs_target")
    sibilance = nested(payload, "sibilance", "metrics")
    if not isinstance(primary, Mapping):
        raise ValueError(f"comparacao candidate_vs_target ausente no ID {clip_id}")
    if not isinstance(sibilance, Mapping):
        raise ValueError(f"objeto metrics de sibilancia ausente no ID {clip_id}")
    summary_metrics = summary.get("candidate_vs_target")
    if not isinstance(summary_metrics, Mapping):
        raise ValueError(f"resumo principal do ID {clip_id} nao contem metricas")
    required_summary_metric_keys = {
        "integrated_loudness_delta_lu",
        "active_rms_delta_db",
        "background_after_voice_match_db",
        "envelope_correlation",
        "active_spectral_mae_db",
        "sample_peak_delta_db",
        "true_peak_delta_db",
    }
    if set(summary_metrics) != required_summary_metric_keys:
        raise ValueError(
            f"chaves de metricas do resumo principal divergiram no ID {clip_id}"
        )
    required_primary_objects = {
        "production_level": {"candidate_minus_reference_lu"},
        "active": {"candidate_minus_reference_rms_db"},
        "background_after_voice_match": {
            "quiet_rms_delta_db",
            "quiet_median_delta_db",
        },
        "envelope": {
            "all_log_rms_correlation",
            "active_log_rms_correlation",
        },
        "spectral_active_after_integrated_loudness_match": {
            "mean_absolute_error_db"
        },
        "spectral_quiet_after_voice_match": {"mean_absolute_error_db"},
        "native_delivery_file_delta": {
            "sample_peak_delta_db",
            "true_peak_delta_db",
        },
    }
    for object_name, required_keys in required_primary_objects.items():
        value = primary.get(object_name)
        if not isinstance(value, Mapping) or not required_keys.issubset(value):
            raise ValueError(
                f"objeto `{object_name}` incompleto na comparacao do ID {clip_id}"
            )
    required_sibilance_metric_keys = {
        "active_4k_8k_delta_after_loudness_match_db",
        "target_sibilant_ratio_delta_median_db",
        "target_sibilant_ratio_delta_p90_absolute_db",
    }
    if not required_sibilance_metric_keys.issubset(sibilance):
        raise ValueError(f"metricas de sibilancia incompletas no ID {clip_id}")
    metrics = dict(summary_metrics)
    expected_summary_metrics = {
        "integrated_loudness_delta_lu": nested(
            primary, "production_level", "candidate_minus_reference_lu"
        ),
        "active_rms_delta_db": nested(
            primary, "active", "candidate_minus_reference_rms_db"
        ),
        "background_after_voice_match_db": nested(
            primary, "background_after_voice_match", "quiet_rms_delta_db"
        ),
        "envelope_correlation": nested(
            primary, "envelope", "all_log_rms_correlation"
        ),
        "active_spectral_mae_db": nested(
            primary,
            "spectral_active_after_integrated_loudness_match",
            "mean_absolute_error_db",
        ),
        "sample_peak_delta_db": nested(
            primary, "native_delivery_file_delta", "sample_peak_delta_db"
        ),
        "true_peak_delta_db": nested(
            primary, "native_delivery_file_delta", "true_peak_delta_db"
        ),
    }
    for key, expected in expected_summary_metrics.items():
        reported_number = finite_number(metrics.get(key))
        expected_number = finite_number(expected)
        if reported_number is None or expected_number is None:
            if reported_number is not expected_number:
                raise ValueError(
                    f"metrica `{key}` do ID {clip_id} diverge entre resumo e JSON completo"
                )
        elif not math.isclose(
            reported_number, expected_number, rel_tol=0.0, abs_tol=1.0e-12
        ):
            raise ValueError(
                f"metrica `{key}` do ID {clip_id} diverge entre resumo e JSON completo"
            )
    metrics.update(
        {
            "quiet_median_after_voice_match_db": nested(
                primary, "background_after_voice_match", "quiet_median_delta_db"
            ),
            "quiet_spectral_mae_db": nested(
                primary, "spectral_quiet_after_voice_match", "mean_absolute_error_db"
            ),
            "active_4k_8k_delta_db": sibilance.get(
                "active_4k_8k_delta_after_loudness_match_db"
            ),
            "sibilant_event_median_delta_db": sibilance.get(
                "target_sibilant_ratio_delta_median_db"
            ),
            "sibilant_event_p90_abs_db": sibilance.get(
                "target_sibilant_ratio_delta_p90_absolute_db"
            ),
            "active_envelope_correlation": nested(
                primary, "envelope", "active_log_rms_correlation"
            ),
        }
    )
    summary_gates = summary.get("quality_gates")
    if not isinstance(summary_gates, Mapping):
        raise ValueError(f"resumo principal do ID {clip_id} nao contem gates")
    required_primary_gate_keys = {
        "active_rms_abs_le_1db",
        "active_spectral_mae_le_1_5db",
        "envelope_corr_ge_0_98",
        "sample_peak_abs_delta_le_0_5db",
        "background_rms_abs_delta_le_3db",
    }
    if set(summary_gates) != required_primary_gate_keys:
        raise ValueError(f"chaves de gates principais divergiram no ID {clip_id}")
    full_primary_gates = nested(payload, "comparison", "initial_quality_gates")
    if not isinstance(full_primary_gates, Mapping) or dict(full_primary_gates) != dict(
        summary_gates
    ):
        raise ValueError(
            f"gates principais divergem entre resumo e JSON completo no ID {clip_id}"
        )
    reported_sibilance_gates = nested(payload, "sibilance", "gates")
    required_sibilance_gate_keys = {
        "active_4k_8k_abs_le_1_5db",
        "sibilant_event_median_abs_le_1db",
        "sibilant_event_p90_abs_le_2db",
    }
    if not isinstance(reported_sibilance_gates, Mapping) or set(
        reported_sibilance_gates
    ) != required_sibilance_gate_keys:
        raise ValueError(f"chaves de gates de sibilancia divergiram no ID {clip_id}")
    base_gates = dict(summary_gates)
    computed_primary_gates = {
        "active_rms_abs_le_1db": abs_le(metrics.get("active_rms_delta_db"), 1.0),
        "active_spectral_mae_le_1_5db": le(
            metrics.get("active_spectral_mae_db"), 1.5
        ),
        "envelope_corr_ge_0_98": ge(metrics.get("envelope_correlation"), 0.98),
        "sample_peak_abs_delta_le_0_5db": abs_le(
            metrics.get("sample_peak_delta_db"), 0.5
        ),
        "background_rms_abs_delta_le_3db": abs_le(
            metrics.get("background_after_voice_match_db"), 3.0
        ),
    }
    computed_sibilance_gates = {
        "active_4k_8k_abs_le_1_5db": abs_le(
            metrics.get("active_4k_8k_delta_db"), 1.5
        ),
        "sibilant_event_median_abs_le_1db": abs_le(
            metrics.get("sibilant_event_median_delta_db"), 1.0
        ),
        "sibilant_event_p90_abs_le_2db": le(
            metrics.get("sibilant_event_p90_abs_db"), 2.0
        ),
    }
    gates = {
        "loudness_abs_le_1lu": abs_le(metrics.get("integrated_loudness_delta_lu"), 1.0),
        "active_rms_abs_le_1db": validated_gate(
            base_gates.get("active_rms_abs_le_1db"),
            computed_primary_gates["active_rms_abs_le_1db"],
            f"gate active_rms do ID {clip_id}",
        ),
        "active_spectral_mae_le_1_5db": validated_gate(
            base_gates.get("active_spectral_mae_le_1_5db"),
            computed_primary_gates["active_spectral_mae_le_1_5db"],
            f"gate spectral do ID {clip_id}",
        ),
        "envelope_corr_ge_0_98": validated_gate(
            base_gates.get("envelope_corr_ge_0_98"),
            computed_primary_gates["envelope_corr_ge_0_98"],
            f"gate envelope do ID {clip_id}",
        ),
        "sample_peak_abs_delta_le_0_5db": validated_gate(
            base_gates.get("sample_peak_abs_delta_le_0_5db"),
            computed_primary_gates["sample_peak_abs_delta_le_0_5db"],
            f"gate sample peak do ID {clip_id}",
        ),
        "true_peak_abs_delta_le_0_5db": abs_le(metrics.get("true_peak_delta_db"), 0.5),
        "background_rms_abs_delta_le_3db": validated_gate(
            base_gates.get("background_rms_abs_delta_le_3db"),
            computed_primary_gates["background_rms_abs_delta_le_3db"],
            f"gate background do ID {clip_id}",
        ),
        "active_4k_8k_abs_le_1_5db": validated_gate(
            nested(payload, "sibilance", "gates", "active_4k_8k_abs_le_1_5db"),
            computed_sibilance_gates["active_4k_8k_abs_le_1_5db"],
            f"gate 4-8 kHz do ID {clip_id}",
        ),
        "sibilant_event_median_abs_le_1db": validated_gate(
            nested(
                payload,
                "sibilance",
                "gates",
                "sibilant_event_median_abs_le_1db",
            ),
            computed_sibilance_gates["sibilant_event_median_abs_le_1db"],
            f"gate mediana sibilante do ID {clip_id}",
        ),
        "sibilant_event_p90_abs_le_2db": validated_gate(
            nested(payload, "sibilance", "gates", "sibilant_event_p90_abs_le_2db"),
            computed_sibilance_gates["sibilant_event_p90_abs_le_2db"],
            f"gate P90 sibilante do ID {clip_id}",
        ),
    }
    passed = sum(value is True for value in gates.values())
    failed = sum(value is False for value in gates.values())
    unavailable = sum(value is None for value in gates.values())
    failed_gate_names = [name for name, value in gates.items() if value is False]
    unavailable_gate_names = [name for name, value in gates.items() if value is None]
    if failed:
        verdict = "FORA_DO_LIMITE"
    elif unavailable:
        verdict = "INCONCLUSIVO"
    else:
        verdict = "APROVADO"
    return {
        "clip_id": clip_id,
        **{key: finite_number(value) for key, value in metrics.items()},
        "gates_passed": passed,
        "gates_failed": failed,
        "gates_unavailable": unavailable,
        "gates_total": len(gates),
        "verdict": verdict,
        "failed_gate_names": failed_gate_names,
        "unavailable_gate_names": unavailable_gate_names,
        "gates": gates,
    }


METRIC_DEFINITIONS = (
    ("integrated_loudness_delta_lu", "Loudness delta (LU)", "signed"),
    ("active_rms_delta_db", "Voz ativa delta (dB)", "signed"),
    ("background_after_voice_match_db", "Fundo apos casar voz (dB)", "signed"),
    ("quiet_median_after_voice_match_db", "Mediana quieta apos casar voz (dB)", "signed"),
    ("envelope_correlation", "Correlacao de envelope", "correlation"),
    ("active_envelope_correlation", "Correlacao de envelope ativo", "correlation"),
    ("active_spectral_mae_db", "MAE espectral ativo (dB)", "error"),
    ("quiet_spectral_mae_db", "MAE espectral quieto (dB)", "error"),
    ("active_4k_8k_delta_db", "Delta ativo 4-8 kHz (dB)", "signed"),
    ("sibilant_event_median_delta_db", "Delta mediano em S (dB)", "signed"),
    ("sibilant_event_p90_abs_db", "P90 absoluto em S (dB)", "error"),
    ("sample_peak_delta_db", "Sample peak delta (dB)", "signed"),
    ("true_peak_delta_db", "True peak aprox. delta (dB)", "signed"),
)


def aggregate_metric(rows: Sequence[Mapping[str, Any]], key: str, kind: str) -> dict[str, Any]:
    available = [
        (row, number)
        for row in rows
        if (number := finite_number(row.get(key))) is not None
    ]
    numbers = [number for _row, number in available]
    result: dict[str, Any] = {"count": len(numbers)}
    if not numbers:
        return result
    result.update(
        {
            "mean": statistics.fmean(numbers),
            "median": statistics.median(numbers),
            "minimum": min(numbers),
            "maximum": max(numbers),
        }
    )
    if kind == "signed":
        result["mean_absolute"] = statistics.fmean(abs(value) for value in numbers)
        result["maximum_absolute"] = max(abs(value) for value in numbers)
        worst_row, worst_value = max(available, key=lambda item: abs(item[1]))
    elif kind == "correlation":
        worst_row, worst_value = min(available, key=lambda item: item[1])
    else:
        worst_row, worst_value = max(available, key=lambda item: item[1])
    result["worst_clip_id"] = worst_row["clip_id"]
    result["worst_value"] = worst_value
    return result


def aggregate_rows(
    rows: Sequence[Mapping[str, Any]],
    technical: Mapping[str, Any],
) -> dict[str, Any]:
    metric_aggregates = {
        key: aggregate_metric(rows, key, kind)
        for key, _label, kind in METRIC_DEFINITIONS
    }
    gate_names = tuple(rows[0]["gates"]) if rows else ()
    gate_aggregates = {}
    for name in gate_names:
        values = [row["gates"].get(name) for row in rows]
        gate_aggregates[name] = {
            "passed": sum(value is True for value in values),
            "failed": sum(value is False for value in values),
            "unavailable": sum(value is None for value in values),
            "total": len(values),
            "failed_clip_ids": [
                row["clip_id"] for row, value in zip(rows, values, strict=True) if value is False
            ],
            "unavailable_clip_ids": [
                row["clip_id"] for row, value in zip(rows, values, strict=True) if value is None
            ],
        }
    verdict_counts = {
        verdict: sum(row["verdict"] == verdict for row in rows)
        for verdict in ("APROVADO", "FORA_DO_LIMITE", "INCONCLUSIVO")
    }
    if verdict_counts["FORA_DO_LIMITE"]:
        overall_quality_verdict = "HAS_OUTLIERS"
    elif verdict_counts["INCONCLUSIVO"]:
        overall_quality_verdict = "INCONCLUSIVE"
    else:
        overall_quality_verdict = "ALL_20_APPROVED"
    ranking = sorted(
        (
            {
                "clip_id": row["clip_id"],
                "failed": row["gates_failed"],
                "passed": row["gates_passed"],
                "unavailable": row["gates_unavailable"],
            }
            for row in rows
        ),
        key=lambda item: (-item["failed"], item["unavailable"], int(item["clip_id"])),
    )
    return {
        "schema_version": 1,
        "suite": SUITE_NAME,
        "fixed_order": list(CLIP_IDS),
        "technical_validation": dict(technical),
        "per_clip": list(rows),
        "metric_aggregates": metric_aggregates,
        "gate_aggregates": gate_aggregates,
        "verdict_counts": verdict_counts,
        "measurement_status": "COMPLETE_MEASUREMENT",
        "overall_quality_verdict": overall_quality_verdict,
        "most_failed_gates_first": ranking,
        "interpretation": {
            "signed_metrics": "positivo = candidato Premiere acima do CapCut",
            "background": "positivo = mais fundo/ambiencia no Premiere apos casar a voz",
            "sibilance": "positivo = Premiere mais brilhante/sibilante; negativo = possivel excesso de reducao",
            "verdict": "APROVADO exige todos os 10 gates; um gate falso gera FORA_DO_LIMITE",
            "process_exit": (
                "codigo 0 significa medicao completa e integra, nao aprovacao sonora; "
                "consulte overall_quality_verdict"
            ),
        },
    }


def fmt(value: Any, decimals: int = 2, signed: bool = False) -> str:
    number = finite_number(value)
    if number is None:
        return "n/d"
    prefix = "+" if signed and number >= 0 else ""
    return f"{prefix}{number:.{decimals}f}"


def render_markdown(aggregate: Mapping[str, Any], tag: str) -> str:
    technical = aggregate["technical_validation"]
    lines = [
        f"# Fluxo Premiere aprovado x CapCut — 20 pares — {tag}",
        "",
        "## Integridade do ensaio",
        "",
        f"- Ordem fixa: `{','.join(CLIP_IDS)}`",
        f"- Formato do concat: `PCM24, estereo, 48 kHz`",
        f"- Frames totais: `{technical['total_frames']}`",
        f"- Duracao total: `{technical['duration_seconds']:.6f} s`",
        "- Hashes de raws/alvos, divisao sample-exact e prova de ordem: **aprovados**",
        "- Presets/projetos modificados pelo validador: **nenhum**",
        "",
        "Valores positivos indicam Premiere acima do CapCut.",
        "",
        "## Tabela por video",
        "",
        "| ID | Loud. LU | Voz dB | Fundo dB | Env. | MAE ativo dB | 4–8k dB | S med. dB | Peak dB | Gates | Resultado |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]
    for row in aggregate["per_clip"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    row["clip_id"],
                    fmt(row.get("integrated_loudness_delta_lu"), 2, True),
                    fmt(row.get("active_rms_delta_db"), 2, True),
                    fmt(row.get("background_after_voice_match_db"), 2, True),
                    fmt(row.get("envelope_correlation"), 4),
                    fmt(row.get("active_spectral_mae_db"), 2),
                    fmt(row.get("active_4k_8k_delta_db"), 2, True),
                    fmt(row.get("sibilant_event_median_delta_db"), 2, True),
                    fmt(row.get("sample_peak_delta_db"), 2, True),
                    f"{row['gates_passed']}/{row['gates_total']}",
                    row["verdict"],
                ]
            )
            + " |"
        )
    lines.extend(["", "## Agregados", ""])
    labels = {key: label for key, label, _kind in METRIC_DEFINITIONS}
    for key, _label, kind in METRIC_DEFINITIONS:
        item = aggregate["metric_aggregates"][key]
        if not item.get("count"):
            lines.append(f"- {labels[key]}: n/d.")
            continue
        tail = (
            f"; media absoluta {fmt(item.get('mean_absolute'), 3)}"
            if kind == "signed"
            else ""
        )
        lines.append(
            f"- {labels[key]}: media {fmt(item.get('mean'), 3, kind == 'signed')}; "
            f"mediana {fmt(item.get('median'), 3, kind == 'signed')}{tail}; "
            f"pior ID {item.get('worst_clip_id')} ({fmt(item.get('worst_value'), 3, kind == 'signed')})."
        )
    lines.extend(["", "## Gates", ""])
    for name, item in aggregate["gate_aggregates"].items():
        lines.append(
            f"- `{name}`: {item['passed']}/{item['total']} aprovados; "
            f"{item['failed']} reprovados; {item['unavailable']} indisponiveis."
        )
    lines.extend(["", "## Falhas por video", ""])
    for row in aggregate["per_clip"]:
        failures = row.get("failed_gate_names", [])
        unavailable = row.get("unavailable_gate_names", [])
        if failures or unavailable:
            details = []
            if failures:
                details.append("falhou " + ", ".join(f"`{name}`" for name in failures))
            if unavailable:
                details.append(
                    "indisponivel " + ", ".join(f"`{name}`" for name in unavailable)
                )
            lines.append(f"- {row['clip_id']}: " + "; ".join(details) + ".")
    if all(row["verdict"] == "APROVADO" for row in aggregate["per_clip"]):
        lines.append("- Nenhuma falha.")
    verdicts = aggregate["verdict_counts"]
    lines.extend(
        [
            "",
            "## Leitura",
            "",
            f"- Aprovados em todos os 10 gates: **{verdicts['APROVADO']}/20**.",
            f"- Fora de pelo menos um limite: **{verdicts['FORA_DO_LIMITE']}/20**.",
            f"- Inconclusivos por metrica ausente: **{verdicts['INCONCLUSIVO']}/20**.",
            f"- Veredito sonoro agregado: **{aggregate['overall_quality_verdict']}**.",
            "- O codigo de saida 0 confirma uma medicao completa e integra; nao significa que o preset passou nos gates.",
            "- `Fundo dB` ja esta casado pelo nivel da voz; portanto mede ruido/ambiencia sem confundir ganho geral.",
            "- `S med. dB` mede os eventos mais sibilantes do alvo, nao apenas brilho medio.",
            "- Os limites sao gates objetivos de triagem; a decisao final deve incluir A/B auditivo com loudness casado.",
            "",
        ]
    )
    return "\n".join(lines)


CSV_COLUMNS = (
    "clip_id",
    "integrated_loudness_delta_lu",
    "active_rms_delta_db",
    "background_after_voice_match_db",
    "quiet_median_after_voice_match_db",
    "envelope_correlation",
    "active_envelope_correlation",
    "active_spectral_mae_db",
    "quiet_spectral_mae_db",
    "active_4k_8k_delta_db",
    "sibilant_event_median_delta_db",
    "sibilant_event_p90_abs_db",
    "sample_peak_delta_db",
    "true_peak_delta_db",
    "gates_passed",
    "gates_failed",
    "gates_unavailable",
    "gates_total",
    "verdict",
    "failed_gate_names",
    "unavailable_gate_names",
)


def write_reports(
    results_staging: Path,
    aggregate: Mapping[str, Any],
    manifest: Mapping[str, Any],
    tag: str,
) -> None:
    json_path = results_staging / "TABELA_COMPARACAO.json"
    md_path = results_staging / "TABELA_COMPARACAO.md"
    csv_path = results_staging / "TABELA_COMPARACAO.csv"
    manifest_path = results_staging / "MANIFESTO_EXECUCAO.json"
    json_path.write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(aggregate, tag), encoding="utf-8")
    with csv_path.open("x", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in aggregate["per_clip"]:
            rendered = {key: row.get(key) for key in CSV_COLUMNS}
            for key in ("failed_gate_names", "unavailable_gate_names"):
                rendered[key] = ";".join(rendered[key] or [])
            writer.writerow(rendered)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def rebase_generated_paths(root: Path, staging_root: Path, final_root: Path) -> None:
    """Troque apenas metadados textuais gerados antes do commit atomico."""

    old_variants = {str(staging_root), str(staging_root.resolve())}
    new = str(final_root.resolve())
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".csv"}:
            continue
        content = path.read_text(encoding="utf-8")
        rendered = content
        for old in old_variants:
            rendered = rendered.replace(old, new)
        if rendered != content:
            path.write_text(rendered, encoding="utf-8")
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".csv"}:
            content = path.read_text(encoding="utf-8")
            if any(old in content for old in old_variants):
                raise ValueError(f"caminho temporario permaneceu no relatorio: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concat", type=Path, required=True, help="WAV concat real exportado pelo Premiere")
    parser.add_argument("--tag", required=True, help="rotulo seguro e unico desta execucao")
    return parser


def execute(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 12):
        raise ValueError("use o Python 3.12 do runtime do projeto")
    if not TAG_PATTERN.fullmatch(args.tag):
        raise ValueError("--tag deve ter 1 a 80 caracteres seguros e iniciar por alfanumerico")
    for analyzer in (ANALYZER, SIBILANCE_ANALYZER):
        if not analyzer.is_file():
            raise ValueError(f"analisador inexistente: {analyzer}")

    # Valide todos os insumos antes de criar qualquer pasta de saida.
    if not PREFLIGHT_MANIFEST.is_file():
        raise ValueError(
            f"preflight ainda nao existe; nao execute antes de preparar os 20 pares: "
            f"{PREFLIGHT_MANIFEST}"
        )
    if PREFLIGHT_MANIFEST.is_symlink():
        raise ValueError("manifesto de preflight nao pode ser link simbolico")
    if OUTPUT_ROOT.is_symlink():
        raise ValueError("pasta de resultados nao pode ser link simbolico")
    preflight_file_sha = common.file_sha256(PREFLIGHT_MANIFEST)
    suite, raw_infos, target_infos, frozen_hashes = load_frozen_suite()
    if common.file_sha256(PREFLIGHT_MANIFEST) != preflight_file_sha:
        raise ValueError("manifesto de preflight mudou durante a leitura")
    concat_path = require_within(args.concat, PRESET_ROOT, "concat real")
    concat_info = common.inspect_wave(concat_path, "concat real do Premiere")
    common.require_candidate_format(concat_info, "concat real do Premiere")
    concat_file_sha = common.file_sha256(concat_path)
    concat_payload_sha, _ = common.payload_sha256_and_frames(
        concat_info, "concat real do Premiere"
    )
    expected_frames = sum(raw_infos[clip_id].frames for clip_id in CLIP_IDS)
    if concat_info.frames != expected_frames:
        delta = concat_info.frames - expected_frames
        raise ValueError(
            f"duracao total invalida: concat tem {concat_info.frames} frames, "
            f"mas os 20 raws exigem {expected_frames} "
            f"(diferenca {delta:+d} frames; {delta / EXPECTED_SAMPLE_RATE:+.9f} s)"
        )

    run_root = OUTPUT_ROOT / args.tag
    candidate_dir = run_root / "candidatos_split"
    results_dir = run_root / "analises"
    reject_collisions([run_root])

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    if OUTPUT_ROOT.is_symlink() or OUTPUT_ROOT.resolve().parent != BASE.resolve():
        raise ValueError(f"pasta de resultados escapou da base aprovada: {OUTPUT_ROOT}")
    required_bytes = concat_info.frames * concat_info.frame_bytes
    frozen_input_bytes = sum(
        Path(info.path).stat().st_size
        for info in (*raw_infos.values(), *target_infos.values())
    )
    if shutil.disk_usage(OUTPUT_ROOT).free < (
        required_bytes + frozen_input_bytes + 64 * 1024 * 1024
    ):
        raise ValueError(f"espaco livre insuficiente em {OUTPUT_ROOT}")

    temporary_root = Path(tempfile.mkdtemp(prefix=f".{args.tag}.", dir=OUTPUT_ROOT))
    candidates_staging = temporary_root / "candidatos_split"
    results_staging = temporary_root / "analises"
    candidates_staging.mkdir()
    results_staging.mkdir()
    committed = False
    try:
        frozen_suite, frozen_input_records = freeze_suite_inputs(
            suite, frozen_hashes, temporary_root
        )
        segment_paths, segment_records = common.prepare_segments(
            frozen_suite, concat_info, candidates_staging
        )
        order_proof = prove_order(
            frozen_suite,
            segment_paths,
            ORDER_MIN_CORRELATION,
            ORDER_MIN_MARGIN,
        )

        staging_candidates = {
            clip_id: candidates_staging / f"{clip_id}.wav" for clip_id in CLIP_IDS
        }

        summaries, full_payloads = run_analyzers(
            frozen_suite, staging_candidates, results_staging, args.tag
        )
        rows = [
            make_row(clip_id, summaries[clip_id], full_payloads[clip_id])
            for clip_id in CLIP_IDS
        ]
        technical = {
            "format_passed": True,
            "frame_sum_passed": True,
            "hashes_passed": True,
            "split_sample_exact_against_concat_passed": True,
            "order_passed": True,
            "total_frames": expected_frames,
            "duration_seconds": expected_frames / EXPECTED_SAMPLE_RATE,
            "order_proof": order_proof,
        }
        aggregate = aggregate_rows(rows, technical)

        candidate_records = {}
        for record in segment_records:
            clip_id = record["clip_id"]
            path = staging_candidates[clip_id]
            info = common.inspect_wave(path, f"candidato split {clip_id}")
            payload_sha, _ = common.payload_sha256_and_frames(
                info, f"candidato split {clip_id}"
            )
            if payload_sha != record["pcm_payload_sha256"]:
                raise ValueError(f"payload mudou no commit do candidato {clip_id}")
            candidate_records[clip_id] = {
                **asdict(info),
                "path": str(candidate_dir / f"{clip_id}.wav"),
                "duration_seconds": info.duration_seconds,
                "file_sha256": common.file_sha256(path),
                "pcm_payload_sha256": payload_sha,
                "start_frame_in_concat": record["start_frame_in_concat"],
                "stop_frame_in_concat_exclusive": record[
                    "stop_frame_in_concat_exclusive"
                ],
            }

        # Revalide os hashes congelados no fim, fechando mudancas durante a analise.
        for clip_id in CLIP_IDS:
            if common.file_sha256(Path(raw_infos[clip_id].path)) != frozen_hashes[clip_id][
                "raw_file_sha256"
            ]:
                raise ValueError(f"raw {clip_id} mudou durante a execucao")
            if common.file_sha256(Path(target_infos[clip_id].path)) != frozen_hashes[clip_id][
                "target_file_sha256"
            ]:
                raise ValueError(f"alvo {clip_id} mudou durante a execucao")
        if common.file_sha256(concat_path) != concat_file_sha:
            raise ValueError("concat mudou durante a execucao")
        if common.file_sha256(PREFLIGHT_MANIFEST) != preflight_file_sha:
            raise ValueError("manifesto de preflight mudou durante a execucao")

        execution_manifest = {
            "schema_version": 1,
            "execution_status": "COMPLETE_MEASUREMENT",
            "overall_quality_verdict": aggregate["overall_quality_verdict"],
            "exit_code_semantics": (
                "0 = medicao tecnica completa e publicada; consulte "
                "overall_quality_verdict para qualidade sonora"
            ),
            "suite": SUITE_NAME,
            "tag": args.tag,
            "fixed_order": list(CLIP_IDS),
            "explicitly_excluded_ids_not_read": sorted(EXPLICITLY_EXCLUDED_IDS, key=int),
            "preflight_manifest": {
                "path": str(PREFLIGHT_MANIFEST),
                "file_sha256": preflight_file_sha,
            },
            "concat": {
                **asdict(concat_info),
                "duration_seconds": concat_info.duration_seconds,
                "file_sha256": concat_file_sha,
                "pcm_payload_sha256": concat_payload_sha,
            },
            "candidates": candidate_records,
            "frozen_inputs_actually_read_by_analyzers": frozen_input_records,
            "technical_validation": technical,
            "analysis_scripts": {
                "comparison": str(ANALYZER),
                "sibilance": str(SIBILANCE_ANALYZER),
            },
            "preflight_provenance_fields_not_revalidated": [
                "prepared_from_path",
                "prepared_from_file_sha256",
                "byte_identical_to_prepared_from",
                "source_media_path",
                "source_media_audio",
                "source_path",
                "source_file_sha256",
                "byte_identical_to_source",
                "scope.source_directory_read_only",
            ],
            "preflight_provenance_note": (
                "o validador revalida somente os 40 WAVs autorizados; nao abre "
                "midias pessoais nem arquivos preparados externos ao conjunto ordered"
            ),
            "preset_or_project_modified": False,
        }
        write_reports(results_staging, aggregate, execution_manifest, args.tag)
        rebase_generated_paths(temporary_root, temporary_root, run_root)
        # Um unico rename no mesmo volume publica candidatos e relatorios juntos.
        if run_root.exists():
            raise ValueError(f"tag apareceu durante a execucao; commit cancelado: {run_root}")
        temporary_root.rename(run_root)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(temporary_root, ignore_errors=True)

    print(
        json.dumps(
            {
                "suite": SUITE_NAME,
                "fixed_order": list(CLIP_IDS),
                "tag": args.tag,
                "candidates_dir": str(candidate_dir),
                "results_dir": str(results_dir),
                "table_csv": str(results_dir / "TABELA_COMPARACAO.csv"),
                "table_json": str(results_dir / "TABELA_COMPARACAO.json"),
                "table_markdown": str(results_dir / "TABELA_COMPARACAO.md"),
                "manifest": str(results_dir / "MANIFESTO_EXECUCAO.json"),
                "measurement_status": "COMPLETE_MEASUREMENT",
                "overall_quality_verdict": aggregate["overall_quality_verdict"],
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
