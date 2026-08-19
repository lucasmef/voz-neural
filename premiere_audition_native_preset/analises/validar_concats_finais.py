#!/usr/bin/env python3
"""Divida e audite os concats finais DJI sem ambiguidade de conjunto.

Existem somente dois conjuntos aceitos:

* ``calibracao10``: 65, 66, 67, 68, 71, 72, 74, 76, 81 e 84;
* ``blind5``: 64, 69, 73, 75 e 82.

O modo ``blind5`` nao contem caminhos de alvos e nao oferece opcao de
comparacao. Ele le somente os WAVs crus cegos e o concat Adobe. A abertura de
alvos cegos e uma etapa posterior, isolada em ``analisar_blind_pos_freeze.py``.

O programa exige Python 3.12 porque o modulo ``wave`` dessa versao entende os
WAVs WAVE_FORMAT_EXTENSIBLE PCM usados no projeto. NumPy e usado apenas para a
prova de ordem por impressao digital de envelope; e a mesma dependencia do
analisador principal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import numpy as np
except ModuleNotFoundError as error:
    raise SystemExit(
        "NumPy nao esta disponivel. Use o Python 3.12 do runtime do projeto."
    ) from error


ROOT = Path(__file__).resolve().parents[2]
PRESET_ROOT = ROOT / "premiere_audition_native_preset"
COMPARISON_DIR = PRESET_ROOT / "comparacao_ab"
DEFAULT_EXPORT_DIR = PRESET_ROOT / "exports_validacao"
DEFAULT_RESULTS_DIR = PRESET_ROOT / "analises" / "resultados"
ANALYZER = PRESET_ROOT / "analises" / "comparar_wavs_adobe.py"

EXPECTED_SAMPLE_RATE = 48_000
EXPECTED_CHANNELS = 2
EXPECTED_CANDIDATE_WIDTH = 3
READ_CHUNK_FRAMES = 65_536
FINGERPRINT_BLOCK_FRAMES = 960  # 20 ms a 48 kHz
FINGERPRINT_MAX_LAG_BLOCKS = 50  # 1 s
TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


@dataclass(frozen=True)
class ClipSpec:
    clip_id: str
    raw_relative: str
    expected_raw_file_sha256: str
    expected_frames: int
    target_relative: str | None = None
    expected_target_file_sha256: str | None = None

    def raw_path(self) -> Path:
        return ROOT / self.raw_relative

    def target_path(self) -> Path | None:
        return ROOT / self.target_relative if self.target_relative else None


@dataclass(frozen=True)
class SuiteSpec:
    name: str
    clips: tuple[ClipSpec, ...]
    blind: bool

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(clip.clip_id for clip in self.clips)


CALIBRATION10 = SuiteSpec(
    name="calibracao10",
    blind=False,
    clips=(
        ClipSpec(
            "65",
            "premiere_audition_native_preset/comparacao_ab/65_CRU.wav",
            "c59e4fb996838e05227898d4819bb98415272ba87724e0b6d3ab35e970ebd7ad",
            1_098_627,
            "premiere_audition_native_preset/comparacao_ab/65_CAPCUT.wav",
            "bf594bbc9b06173472de27e94c1f133a89a5c2b69091f07ec371ec67ee6d73aa",
        ),
        ClipSpec(
            "66",
            "premiere_audition_native_preset/comparacao_ab/66_CRU_DJI.wav",
            "5109dbc3259a7883f1c616af8061780e34a0795693d63b9e5a8f0b2ec0197f96",
            1_152_000,
            "independent_neural_flow_20260729/validation_wavs/66_A_CAPCUT_44K1.wav",
            "76fe0ddfb7b3811c27ccd306da1ab544ad7b142fa0a6a7e583d2aac400cbff34",
        ),
        ClipSpec(
            "67",
            "premiere_audition_native_preset/comparacao_ab/67_CRU_DJI.wav",
            "30a5303d7a5556e30f8cad70b8b5897f06f7bead2fd8448c42d0b57a6c776f9b",
            1_432_338,
            "premiere_audition_native_preset/comparacao_ab/67_CAPCUT.wav",
            "db0c11c53f345aa0041da77a0b9f724bdfeb3850038e226e11863ec3cf255be0",
        ),
        ClipSpec(
            "68",
            "premiere_audition_native_preset/comparacao_ab/68_CRU_DJI.wav",
            "aa6b92afb6d15f4d16d743211a8d5887f52aaadc90dba420f3ff2853acf40959",
            1_283_200,
            "premiere_audition_native_preset/comparacao_ab/68_CAPCUT.wav",
            "bf1c07ae78f74ba68d2aff53d617cf86cd87d7827d9f74890faca0f89732cb27",
        ),
        ClipSpec(
            "71",
            "premiere_audition_native_preset/comparacao_ab/71_CRU_DJI.wav",
            "3559b4387265e1b22113b9a4a5af056aee02c4e0b26aeb24a3d953950993fd0b",
            972_230,
            "premiere_audition_native_preset/comparacao_ab/71_CAPCUT.wav",
            "4e92e780bce33b6471099a7674f5c6555c42105560f2afb10655b65e97aea1eb",
        ),
        ClipSpec(
            "72",
            "premiere_audition_native_preset/comparacao_ab/72_CRU_DJI.wav",
            "ee18e212ab462ed7abab5b37335e7104238fad3e76bba596c6e61ad5b55a09f8",
            889_600,
            "premiere_audition_native_preset/comparacao_ab/72_CAPCUT.wav",
            "6311b64b99f98194db9b5f7c4cafcffae0881353994a6d595d98548b1c6d1510",
        ),
        ClipSpec(
            "74",
            "premiere_audition_native_preset/comparacao_ab/74_CRU_DJI.wav",
            "f92cdbe073562f50768ca1a8184a8eade121ad642d795b362d1fe406fc77c2d9",
            1_658_400,
            "independent_neural_flow_20260729/validation_wavs/74_A_CAPCUT_44K1.wav",
            "7d8cf7e10f1a14127880dbe7ebf7f0e86ca9019f5f2b052dff9e46546e490fe6",
        ),
        ClipSpec(
            "76",
            "premiere_audition_native_preset/comparacao_ab/76_CRU.wav",
            "daab42fa389caab55e8d6ab9ca6a75f28cd9216c4940be0e16efce30c54ff701",
            1_133_868,
            "premiere_audition_native_preset/comparacao_ab/76_CAPCUT.wav",
            "d426c972bff18486d9fc55910600b13a6bf33c00683d66c48d06d729cf3b2a37",
        ),
        ClipSpec(
            "81",
            "premiere_audition_native_preset/comparacao_ab/81_CRU_DJI.wav",
            "0403bb05e12b5569ccc57ba0ffc008bb3fc58709e8faafffb9f9a51c942e42c0",
            1_189_600,
            "premiere_audition_native_preset/comparacao_ab/81_CAPCUT.wav",
            "2b832374c0301fe5d5fc6cb7fe8bbffd210565416233e4fdc1cd563c12522e99",
        ),
        ClipSpec(
            "84",
            "premiere_audition_native_preset/comparacao_ab/84_CRU_DJI.wav",
            "8ce745dc748f9e5de03bb439d2165c1a8fae7bc832d63b8adb0cd2b296fdfc29",
            1_344_800,
            "independent_neural_flow_20260729/validation_wavs/84_A_CAPCUT_44K1.wav",
            "f3101e252680760dc04ba16d6902eaf6b4032c1ff179b5ce9185b26440767c4f",
        ),
    ),
)


BLIND5 = SuiteSpec(
    name="blind5",
    blind=True,
    clips=(
        ClipSpec(
            "64",
            "premiere_audition_native_preset/comparacao_ab/64_CRU_DJI_BLIND.wav",
            "a2f6ea33908a2c2949eee4dde7809ae4bac3837a012e1f76a43896922b14c5cf",
            812_192,
        ),
        ClipSpec(
            "69",
            "premiere_audition_native_preset/comparacao_ab/69_CRU_DJI_BLIND.wav",
            "93b07141f25cf7395e805bcae8a709de472c5f4b04e69714c78af121b1c4e092",
            1_318_400,
        ),
        ClipSpec(
            "73",
            "premiere_audition_native_preset/comparacao_ab/73_CRU_DJI_BLIND.wav",
            "14a40bf95a0b68f31f51e960e600b26a289175382c2c7a6fc6a96945de38a5db",
            1_105_861,
        ),
        ClipSpec(
            "75",
            "premiere_audition_native_preset/comparacao_ab/75_CRU_DJI_BLIND.wav",
            "95a209375dcd78494d1fa8940a6cc2edc96c9a1da51529f81b6c952c20ceeed2",
            1_508_800,
        ),
        ClipSpec(
            "82",
            "premiere_audition_native_preset/comparacao_ab/82_CRU_DJI_BLIND.wav",
            "81afb394231da1baa81af773eb6a3d0060583670dad5e4c94bda88d6d2d42b92",
            1_214_400,
        ),
    ),
)


SUITES: Mapping[str, SuiteSpec] = {
    CALIBRATION10.name: CALIBRATION10,
    BLIND5.name: BLIND5,
}


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
        raise ValueError(f"{role} tem sample width nao suportado: {resolved}")
    return info


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_sha256_and_frames(info: WaveInfo, role: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    actual_frames = 0
    with wave.open(info.path, "rb") as source:
        while True:
            payload = source.readframes(READ_CHUNK_FRAMES)
            if not payload:
                break
            if len(payload) % info.frame_bytes:
                raise ValueError(f"{role} tem payload PCM truncado/desalinhado")
            actual_frames += len(payload) // info.frame_bytes
            digest.update(payload)
    if actual_frames != info.frames:
        raise ValueError(
            f"{role} declara {info.frames} frames, mas contem {actual_frames}"
        )
    return digest.hexdigest(), actual_frames


def require_candidate_format(info: WaveInfo, role: str) -> None:
    expected = (
        EXPECTED_SAMPLE_RATE,
        EXPECTED_CHANNELS,
        EXPECTED_CANDIDATE_WIDTH,
        "NONE",
    )
    if info.format_key != expected:
        raise ValueError(
            f"{role} deve ser PCM24 estereo 48 kHz; recebido "
            f"{info.sample_width_bytes * 8} bit, {info.channels} canal(is), "
            f"{info.sample_rate} Hz, compressao {info.compression}"
        )


def validate_raw(clip: ClipSpec) -> tuple[WaveInfo, str, str]:
    path = clip.raw_path().resolve()
    info = inspect_wave(path, f"cru {clip.clip_id}")
    if info.sample_rate != EXPECTED_SAMPLE_RATE or info.channels != 2:
        raise ValueError(
            f"cru {clip.clip_id} deve ser estereo 48 kHz: {info.path}"
        )
    if info.sample_width_bytes not in (2, 3):
        raise ValueError(
            f"cru {clip.clip_id} deve ser PCM16 ou PCM24: {info.path}"
        )
    if info.frames != clip.expected_frames:
        raise ValueError(
            f"cru {clip.clip_id}: esperados {clip.expected_frames} frames, "
            f"recebidos {info.frames}"
        )
    whole_sha = file_sha256(path)
    if whole_sha != clip.expected_raw_file_sha256:
        raise ValueError(
            f"cru {clip.clip_id} nao coincide com o conjunto congelado; "
            f"SHA-256 recebido {whole_sha}"
        )
    payload_sha, _ = payload_sha256_and_frames(info, f"cru {clip.clip_id}")
    return info, whole_sha, payload_sha


def validate_calibration_target(clip: ClipSpec) -> tuple[WaveInfo, str, str]:
    path = clip.target_path()
    if path is None or clip.expected_target_file_sha256 is None:
        raise ValueError(f"alvo de calibracao ausente no ID {clip.clip_id}")
    resolved = path.resolve()
    info = inspect_wave(resolved, f"alvo de calibracao {clip.clip_id}")
    if info.sample_rate != 44_100:
        raise ValueError(f"alvo {clip.clip_id} deveria estar em 44,1 kHz")
    if info.channels not in (1, 2) or info.sample_width_bytes not in (2, 3):
        raise ValueError(f"formato inesperado no alvo {clip.clip_id}")
    whole_sha = file_sha256(resolved)
    if whole_sha != clip.expected_target_file_sha256:
        raise ValueError(
            f"alvo {clip.clip_id} nao coincide com a referencia congelada; "
            f"SHA-256 recebido {whole_sha}"
        )
    payload_sha, _ = payload_sha256_and_frames(
        info, f"alvo de calibracao {clip.clip_id}"
    )
    return info, whole_sha, payload_sha


def decode_mono(path: Path) -> np.ndarray:
    info = inspect_wave(path, "audio para impressao digital")
    with wave.open(str(path), "rb") as source:
        payload = source.readframes(info.frames)
    if info.sample_width_bytes == 2:
        flat = np.frombuffer(payload, dtype="<i2").astype(np.float32)
        flat /= 32768.0
    elif info.sample_width_bytes == 3:
        octets = np.frombuffer(payload, dtype=np.uint8)
        if octets.size % 3:
            raise ValueError(f"PCM24 malformado: {path}")
        triples = octets.reshape(-1, 3).astype(np.int32)
        integer = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        integer = np.where(integer & 0x800000, integer - 0x1000000, integer)
        flat = integer.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"impressao digital aceita somente PCM16/24: {path}")
    expected = info.frames * info.channels
    if flat.size != expected:
        raise ValueError(f"payload malformado ao decodificar {path}")
    return np.mean(flat.reshape(info.frames, info.channels), axis=1, dtype=np.float32)


def log_rms_fingerprint(path: Path) -> np.ndarray:
    mono = decode_mono(path)
    complete = (mono.size // FINGERPRINT_BLOCK_FRAMES) * FINGERPRINT_BLOCK_FRAMES
    if complete < 20 * FINGERPRINT_BLOCK_FRAMES:
        raise ValueError(f"audio curto demais para provar ordem: {path}")
    framed = mono[:complete].astype(np.float64).reshape(
        -1, FINGERPRINT_BLOCK_FRAMES
    )
    rms = np.sqrt(np.mean(framed * framed, axis=1) + 1.0e-14)
    return np.log10(rms + 1.0e-7)


def pearson(left: np.ndarray, right: np.ndarray) -> float:
    count = min(left.size, right.size)
    if count < 20:
        return 0.0
    # ``left`` e ``right`` ja costumam ser float64. Sem ``copy=True``, a
    # centralizacao in-place abaixo corromperia as impressoes digitais usadas
    # nas correlacoes seguintes e tornaria a matriz dependente da ordem dos
    # loops.
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
    suite: SuiteSpec,
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
            raw_id: value for raw_id, value in correlations.items() if raw_id != clip_id
        }
        best_other_id, best_other = max(competitors.items(), key=lambda item: item[1])
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


def write_segment(
    source: wave.Wave_read,
    concat_info: WaveInfo,
    expected_frames: int,
    destination: Path,
) -> str:
    remaining = expected_frames
    digest = hashlib.sha256()
    with wave.open(str(destination), "wb") as sink:
        sink.setnchannels(concat_info.channels)
        sink.setsampwidth(concat_info.sample_width_bytes)
        sink.setframerate(concat_info.sample_rate)
        sink.setcomptype("NONE", "not compressed")
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


def prepare_segments(
    suite: SuiteSpec,
    concat_info: WaveInfo,
    staging_dir: Path,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    segment_paths: dict[str, Path] = {}
    records: list[dict[str, Any]] = []
    offset = 0
    with wave.open(concat_info.path, "rb") as source:
        for clip in suite.clips:
            path = staging_dir / f"{clip.clip_id}.wav"
            source_payload_sha = write_segment(
                source, concat_info, clip.expected_frames, path
            )
            info = inspect_wave(path, f"segmento temporario {clip.clip_id}")
            require_candidate_format(info, f"segmento {clip.clip_id}")
            if info.frames != clip.expected_frames:
                raise ValueError(
                    f"segmento {clip.clip_id}: esperados {clip.expected_frames} "
                    f"frames, recebidos {info.frames}"
                )
            payload_sha, _ = payload_sha256_and_frames(
                info, f"segmento {clip.clip_id}"
            )
            if payload_sha != source_payload_sha:
                raise ValueError(f"payload mudou ao dividir o segmento {clip.clip_id}")
            segment_paths[clip.clip_id] = path
            records.append(
                {
                    "clip_id": clip.clip_id,
                    "start_frame_in_concat": offset,
                    "stop_frame_in_concat_exclusive": offset + clip.expected_frames,
                    "frames": clip.expected_frames,
                    "duration_seconds": clip.expected_frames / EXPECTED_SAMPLE_RATE,
                    "pcm_payload_sha256": payload_sha,
                    "temporary_file_sha256": file_sha256(path),
                }
            )
            offset += clip.expected_frames
        if source.readframes(1):
            raise ValueError("concat contem frames extras depois do ultimo segmento")
    return segment_paths, records


def destination_paths(
    suite: SuiteSpec,
    tag: str,
    export_dir: Path,
    results_dir: Path,
) -> tuple[dict[str, Path], dict[str, Path], Path, Path, Path]:
    candidates = {
        clip.clip_id: export_dir / f"{clip.clip_id}_{tag}.wav"
        for clip in suite.clips
    }
    analysis_dirs = {
        clip.clip_id: results_dir / f"{clip.clip_id}_{tag}"
        for clip in suite.clips
    }
    manifest = results_dir / f"{tag}_CONCAT_FINAL_MANIFEST.json"
    aggregate_json = results_dir / f"{tag}_RELATORIO_AGREGADO.json"
    aggregate_md = results_dir / f"{tag}_RELATORIO_AGREGADO.md"
    return candidates, analysis_dirs, manifest, aggregate_json, aggregate_md


def reject_collisions(paths: Iterable[Path]) -> None:
    collisions = [path for path in paths if path.exists()]
    if collisions:
        listing = "\n  - ".join(str(path) for path in collisions)
        raise ValueError(
            "a execucao nunca sobrescreve saidas; escolha outro --tag. "
            f"Colisoes:\n  - {listing}"
        )


def run_analyzers(
    python_executable: Path,
    suite: SuiteSpec,
    tag: str,
    candidates: Mapping[str, Path],
    staging_results: Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    if suite.blind:
        raise ValueError("falha de seguranca: o conjunto blind5 nao pode ser comparado")
    summaries: dict[str, Any] = {}
    temporary_dirs: dict[str, Path] = {}
    for clip in suite.clips:
        target = clip.target_path()
        if target is None:
            raise ValueError(f"alvo ausente no ID {clip.clip_id}")
        output_prefix = f"{clip.clip_id}_{tag}"
        output_dir = staging_results / clip.clip_id
        temporary_dirs[clip.clip_id] = output_dir
        command = [
            str(python_executable),
            str(ANALYZER),
            "--raw",
            str(clip.raw_path()),
            "--candidate",
            str(candidates[clip.clip_id]),
            "--target",
            str(target),
            "--output-dir",
            str(output_dir),
            "--output-prefix",
            output_prefix,
            "--label",
            f"{clip.clip_id} {tag} — calibracao10 DJI",
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"analisador falhou no ID {clip.clip_id} "
                f"(codigo {completed.returncode}): {details}"
            )
        try:
            summaries[clip.clip_id] = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"analisador retornou JSON invalido no ID {clip.clip_id}"
            ) from error
    return summaries, temporary_dirs


METRICS = (
    ("integrated_loudness_delta_lu", "Loudness delta (LU)", True),
    ("active_rms_delta_db", "Voz ativa delta (dB)", True),
    ("background_after_voice_match_db", "Fundo apos casar voz (dB)", True),
    ("envelope_correlation", "Correlacao de envelope", False),
    ("active_spectral_mae_db", "MAE espectral ativo (dB)", False),
    ("sample_peak_delta_db", "Sample peak delta (dB)", True),
    ("true_peak_delta_db", "True peak aprox. delta (dB)", True),
)


def numeric_values(
    summaries: Mapping[str, Any], key: str
) -> list[float]:
    values: list[float] = []
    for summary in summaries.values():
        value = summary.get("candidate_vs_target", {}).get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            values.append(float(value))
    return values


def aggregate_summaries(
    suite: SuiteSpec,
    tag: str,
    summaries: Mapping[str, Any],
    technical: Mapping[str, Any],
) -> dict[str, Any]:
    metric_aggregates: dict[str, Any] = {}
    for key, _label, signed in METRICS:
        values = numeric_values(summaries, key)
        aggregate: dict[str, Any] = {"count": len(values)}
        if values:
            aggregate.update(
                {
                    "mean": statistics.fmean(values),
                    "median": statistics.median(values),
                    "minimum": min(values),
                    "maximum": max(values),
                }
            )
            if signed:
                aggregate.update(
                    {
                        "mean_absolute": statistics.fmean(abs(value) for value in values),
                        "maximum_absolute": max(abs(value) for value in values),
                    }
                )
        metric_aggregates[key] = aggregate

    gate_names = sorted(
        {
            gate
            for summary in summaries.values()
            for gate in summary.get("quality_gates", {})
        }
    )
    gate_aggregates: dict[str, Any] = {}
    for gate in gate_names:
        values = [summary.get("quality_gates", {}).get(gate) for summary in summaries.values()]
        gate_aggregates[gate] = {
            "passed": sum(value is True for value in values),
            "failed": sum(value is False for value in values),
            "unavailable": sum(value is None for value in values),
            "total": len(values),
        }

    return {
        "schema_version": 1,
        "suite": suite.name,
        "blind": suite.blind,
        "tag": tag,
        "fixed_order": list(suite.order),
        "technical_validation": technical,
        "comparison_performed": bool(summaries),
        "blindness_preserved": suite.blind and not summaries,
        "per_clip": dict(summaries),
        "metric_aggregates": metric_aggregates,
        "quality_gate_aggregates": gate_aggregates,
    }


def fmt(value: Any, decimals: int = 4, signed: bool = False) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return "n/d"
    prefix = "+" if signed and float(value) >= 0 else ""
    return f"{prefix}{float(value):.{decimals}f}"


def aggregate_markdown(payload: Mapping[str, Any]) -> str:
    order = ", ".join(payload["fixed_order"])
    technical = payload["technical_validation"]
    lines = [
        f"# Relatorio agregado — {payload['tag']}",
        "",
        f"- Conjunto: `{payload['suite']}`",
        f"- Ordem fixa: `{order}`",
        f"- Quadros totais: `{technical['total_frames']}`",
        f"- Duracao total: `{technical['duration_seconds']:.6f} s`",
        "- Formato do concat e dos candidatos: `PCM24, estereo, 48 kHz`",
        "- Contagem de quadros, hashes de arquivo/payload e divisao: **aprovados**",
        "- Prova de ordem por impressao digital de envelope: **aprovada**",
    ]
    if payload["blindness_preserved"]:
        lines.extend(
            [
                "- Alvos cegos lidos nesta etapa: **nenhum**",
                "- Estado: candidatos cegos prontos; comparacao reservada para pos-freeze.",
                "",
            ]
        )
    elif payload["comparison_performed"]:
        lines.extend(["- Comparacao objetiva com CapCut: **concluida**", ""])
        lines.extend(
            [
                "| ID | Loudness LU | Voz dB | Fundo dB | Env. | MAE dB | Peak dB | Gates |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for clip_id in payload["fixed_order"]:
            summary = payload["per_clip"][clip_id]
            metrics = summary["candidate_vs_target"]
            gates = summary.get("quality_gates", {})
            passed = sum(value is True for value in gates.values())
            total = sum(value is not None for value in gates.values())
            lines.append(
                "| "
                + " | ".join(
                    [
                        clip_id,
                        fmt(metrics.get("integrated_loudness_delta_lu"), 2, True),
                        fmt(metrics.get("active_rms_delta_db"), 2, True),
                        fmt(metrics.get("background_after_voice_match_db"), 2, True),
                        fmt(metrics.get("envelope_correlation"), 4),
                        fmt(metrics.get("active_spectral_mae_db"), 2),
                        fmt(metrics.get("sample_peak_delta_db"), 2, True),
                        f"{passed}/{total}",
                    ]
                )
                + " |"
            )
        lines.extend(["", "## Agregados", ""])
        for key, label, signed in METRICS:
            aggregate = payload["metric_aggregates"].get(key, {})
            if aggregate.get("count", 0):
                tail = (
                    f"; media absoluta {fmt(aggregate.get('mean_absolute'), 4)}"
                    if signed
                    else ""
                )
                lines.append(
                    f"- {label}: media {fmt(aggregate.get('mean'), 4, signed)}; "
                    f"mediana {fmt(aggregate.get('median'), 4, signed)}{tail}."
                )
        lines.extend(["", "## Gates", ""])
        for gate, aggregate in payload["quality_gate_aggregates"].items():
            lines.append(
                f"- `{gate}`: {aggregate['passed']}/{aggregate['total']} aprovados; "
                f"{aggregate['failed']} reprovados; {aggregate['unavailable']} indisponiveis."
            )
        lines.append("")
    else:
        lines.extend(["- Comparacao objetiva: nao solicitada.", ""])
    return "\n".join(lines)


def write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2)
        destination.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, choices=tuple(SUITES))
    parser.add_argument("--concat", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--compare-calibration",
        action="store_true",
        help="somente calibracao10: roda comparar_wavs_adobe.py e agrega os dez pares",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--min-order-correlation", type=float, default=0.70)
    parser.add_argument("--min-order-margin", type=float, default=0.05)
    return parser


def execute(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 12):
        raise ValueError("use o Python 3.12 do runtime do projeto")
    if not TAG_PATTERN.fullmatch(args.tag):
        raise ValueError(
            "--tag deve ter 1 a 80 caracteres seguros; o primeiro deve ser alfanumerico"
        )
    suite = SUITES[args.suite]
    if suite.blind and args.compare_calibration:
        raise ValueError(
            "falha de seguranca: --compare-calibration nunca e aceito com blind5"
        )
    if not (0.0 <= args.min_order_correlation <= 1.0):
        raise ValueError("--min-order-correlation deve estar entre 0 e 1")
    if not (0.0 <= args.min_order_margin <= 1.0):
        raise ValueError("--min-order-margin deve estar entre 0 e 1")
    python_executable = args.python.expanduser().resolve()
    if args.compare_calibration:
        if not ANALYZER.is_file():
            raise ValueError(f"analisador inexistente: {ANALYZER}")
        if not python_executable.is_file():
            raise ValueError(f"Python inexistente: {python_executable}")

    export_dir = args.output_dir.expanduser().resolve()
    results_dir = args.results_dir.expanduser().resolve()
    export_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    candidates, analysis_dirs, manifest_path, aggregate_json, aggregate_md = (
        destination_paths(suite, args.tag, export_dir, results_dir)
    )
    collision_paths: list[Path] = [
        *candidates.values(),
        manifest_path,
        aggregate_json,
        aggregate_md,
    ]
    if args.compare_calibration:
        collision_paths.extend(analysis_dirs.values())
    reject_collisions(collision_paths)

    raw_infos: dict[str, WaveInfo] = {}
    raw_file_shas: dict[str, str] = {}
    raw_payload_shas: dict[str, str] = {}
    for clip in suite.clips:
        info, whole_sha, payload_sha = validate_raw(clip)
        raw_infos[clip.clip_id] = info
        raw_file_shas[clip.clip_id] = whole_sha
        raw_payload_shas[clip.clip_id] = payload_sha

    target_infos: dict[str, WaveInfo] = {}
    target_file_shas: dict[str, str] = {}
    target_payload_shas: dict[str, str] = {}
    if args.compare_calibration:
        for clip in suite.clips:
            info, whole_sha, payload_sha = validate_calibration_target(clip)
            target_infos[clip.clip_id] = info
            target_file_shas[clip.clip_id] = whole_sha
            target_payload_shas[clip.clip_id] = payload_sha

    concat_path = args.concat.expanduser().resolve()
    concat_info = inspect_wave(concat_path, "concat final")
    require_candidate_format(concat_info, "concat final")
    concat_file_sha = file_sha256(concat_path)
    concat_payload_sha, _ = payload_sha256_and_frames(concat_info, "concat final")
    expected_frames = sum(clip.expected_frames for clip in suite.clips)
    if concat_info.frames != expected_frames:
        difference = concat_info.frames - expected_frames
        raise ValueError(
            f"duracao total invalida: concat tem {concat_info.frames} frames, "
            f"mas {suite.name} exige {expected_frames} "
            f"(diferenca {difference:+d}; {difference / 48000:+.9f} s)"
        )

    required_bytes = concat_info.frames * concat_info.frame_bytes
    if shutil.disk_usage(export_dir).free < required_bytes + 32 * 1024 * 1024:
        raise ValueError(f"espaco livre insuficiente em {export_dir}")

    staging_dir = Path(tempfile.mkdtemp(prefix=f".{args.tag}.split.", dir=export_dir))
    staging_results = Path(
        tempfile.mkdtemp(prefix=f".{args.tag}.analysis.", dir=results_dir)
    )
    created_candidates: list[Path] = []
    created_analysis_dirs: list[Path] = []
    created_reports: list[Path] = []
    try:
        segment_paths, segment_records = prepare_segments(
            suite, concat_info, staging_dir
        )
        order_proof = prove_order(
            suite,
            segment_paths,
            args.min_order_correlation,
            args.min_order_margin,
        )

        # O hard link e atomico e falha se uma saida aparecer depois da checagem.
        for clip in suite.clips:
            destination = candidates[clip.clip_id]
            os.link(segment_paths[clip.clip_id], destination)
            created_candidates.append(destination)

        summaries: dict[str, Any] = {}
        temporary_analysis_dirs: dict[str, Path] = {}
        if args.compare_calibration:
            summaries, temporary_analysis_dirs = run_analyzers(
                python_executable, suite, args.tag, candidates, staging_results
            )
            for clip in suite.clips:
                source_dir = temporary_analysis_dirs[clip.clip_id]
                destination_dir = analysis_dirs[clip.clip_id]
                source_dir.rename(destination_dir)
                created_analysis_dirs.append(destination_dir)
                prefix = f"{clip.clip_id}_{args.tag}_COMPARACAO"
                summaries[clip.clip_id]["json"] = str(
                    destination_dir / f"{prefix}.json"
                )
                summaries[clip.clip_id]["markdown"] = str(
                    destination_dir / f"{prefix}.md"
                )

        candidate_records: dict[str, Any] = {}
        for record in segment_records:
            clip_id = record["clip_id"]
            path = candidates[clip_id]
            info = inspect_wave(path, f"candidato final {clip_id}")
            payload_sha, _ = payload_sha256_and_frames(
                info, f"candidato final {clip_id}"
            )
            if payload_sha != record["pcm_payload_sha256"]:
                raise ValueError(f"payload mudou no commit do candidato {clip_id}")
            candidate_records[clip_id] = {
                **asdict(info),
                "duration_seconds": info.duration_seconds,
                "file_sha256": file_sha256(path),
                "pcm_payload_sha256": payload_sha,
                "start_frame_in_concat": record["start_frame_in_concat"],
                "stop_frame_in_concat_exclusive": record[
                    "stop_frame_in_concat_exclusive"
                ],
            }

        technical = {
            "format_passed": True,
            "frame_sum_passed": True,
            "hashes_passed": True,
            "order_passed": True,
            "total_frames": expected_frames,
            "duration_seconds": expected_frames / EXPECTED_SAMPLE_RATE,
            "order_proof": order_proof,
        }
        aggregate = aggregate_summaries(
            suite, args.tag, summaries, technical
        )
        write_json_exclusive(aggregate_json, aggregate)
        created_reports.append(aggregate_json)
        with aggregate_md.open("x", encoding="utf-8") as destination:
            destination.write(aggregate_markdown(aggregate))
        created_reports.append(aggregate_md)

        manifest = {
            "schema_version": 2,
            "suite": suite.name,
            "blind": suite.blind,
            "blind_targets_read": False if suite.blind else None,
            "tag": args.tag,
            "fixed_order": list(suite.order),
            "concat": {
                **asdict(concat_info),
                "duration_seconds": concat_info.duration_seconds,
                "file_sha256": concat_file_sha,
                "pcm_payload_sha256": concat_payload_sha,
            },
            "expected_total_frames_from_frozen_raws": expected_frames,
            "raws": {
                clip.clip_id: {
                    **asdict(raw_infos[clip.clip_id]),
                    "duration_seconds": raw_infos[clip.clip_id].duration_seconds,
                    "file_sha256": raw_file_shas[clip.clip_id],
                    "pcm_payload_sha256": raw_payload_shas[clip.clip_id],
                }
                for clip in suite.clips
            },
            "targets": {
                clip.clip_id: {
                    **asdict(target_infos[clip.clip_id]),
                    "duration_seconds": target_infos[clip.clip_id].duration_seconds,
                    "file_sha256": target_file_shas[clip.clip_id],
                    "pcm_payload_sha256": target_payload_shas[clip.clip_id],
                }
                for clip in suite.clips
                if clip.clip_id in target_infos
            },
            "candidates": candidate_records,
            "order_proof": order_proof,
            "comparison_performed": bool(summaries),
            "aggregate_json": str(aggregate_json),
            "aggregate_markdown": str(aggregate_md),
        }
        write_json_exclusive(manifest_path, manifest)
        created_reports.append(manifest_path)
    except Exception:
        for path in reversed(created_reports):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for path in reversed(created_analysis_dirs):
            shutil.rmtree(path, ignore_errors=True)
        for path in reversed(created_candidates):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(staging_results, ignore_errors=True)

    print(
        json.dumps(
            {
                "suite": suite.name,
                "fixed_order": list(suite.order),
                "manifest": str(manifest_path),
                "aggregate_json": str(aggregate_json),
                "aggregate_markdown": str(aggregate_md),
                "candidates": {
                    clip_id: str(path) for clip_id, path in candidates.items()
                },
                "comparison_completed": bool(args.compare_calibration),
                "blind_targets_read": False if suite.blind else None,
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
