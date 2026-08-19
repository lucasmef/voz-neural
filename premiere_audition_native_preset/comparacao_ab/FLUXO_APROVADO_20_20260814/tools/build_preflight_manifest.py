#!/usr/bin/env python3
"""Gera o manifesto fail-closed dos 20 pares autorizados.

Le somente os arquivos explicitamente listados. Nao percorre nem abre outros
IDs da pasta pessoal. Os WAVs ordenados precisam existir previamente.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import struct
import subprocess
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
COMPARE = BASE.parent
EDIT = Path("/Users/lucas/Movies/Vídeos/Edit")
INSPECTOR = BASE / "tools" / "inspect_audio"
MANIFEST_JSON = BASE / "manifest_preflight.json"
MANIFEST_MD = BASE / "manifest_preflight.md"

ORDER = ["64", "65", "66", "67", "68", "69", "71", "72", "73", "74", "75", "76", "81", "82", "84", "92", "94", "95", "101", "107"]
EXCLUDED = ["70", "77", "83", "21", "38", "39", "51"]

SOURCE_NAMES = {
    "64": "64.mov",
    "65": "65.mov",
    "66": "66.mov",
    "67": "67.mov",
    "68": "68-montagem.mov",
    "69": "69-montagem.mov",
    "71": "71.mov",
    "72": "72.mov",
    "73": "73.mov",
    "74": "74-montagem.mov",
    "75": "75-montagem.mov",
    "76": "76.wav",
    "81": "81.mov",
    "82": "82.mov",
    "84": "84.mov",
    "92": "92.mov",
    "94": "94-a.mov",
    "95": "95.mov",
    "101": "101.mov",
    "107": "107.mov",
}

RAW_PROVENANCE = {
    "64": COMPARE / "64_CRU_DJI_BLIND.wav",
    "65": COMPARE / "65_CRU.wav",
    "66": COMPARE / "66_CRU_DJI.wav",
    "67": COMPARE / "67_CRU_DJI.wav",
    "68": COMPARE / "68_CRU_DJI.wav",
    "69": COMPARE / "69_CRU_DJI_BLIND.wav",
    "71": COMPARE / "71_CRU_DJI.wav",
    "72": COMPARE / "72_CRU_DJI.wav",
    "73": COMPARE / "73_CRU_DJI_BLIND.wav",
    "74": COMPARE / "74_CRU_DJI.wav",
    "75": COMPARE / "75_CRU_DJI_BLIND.wav",
    "76": COMPARE / "76_CRU.wav",
    "81": COMPARE / "81_CRU_DJI.wav",
    "82": COMPARE / "82_CRU_DJI_BLIND.wav",
    "84": COMPARE / "84_CRU_DJI.wav",
    "92": BASE / "raws" / "92_RAW.wav",
    "94": COMPARE / "94_RAW_DJI_NEW.wav",
    "95": BASE / "raws" / "95_RAW.wav",
    "101": COMPARE / "101_RAW_DJI_NEW.wav",
    "107": BASE / "raws" / "107_RAW.wav",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_wav(path: Path) -> dict:
    size = path.stat().st_size
    payload_digest = hashlib.sha256()
    data_bytes = 0
    fmt = None
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError(f"WAV RIFF invalido: {path}")
        pos = 12
        while pos + 8 <= size:
            handle.seek(pos)
            chunk_id, chunk_size = struct.unpack("<4sI", handle.read(8))
            data_start = pos + 8
            if data_start + chunk_size > size:
                raise ValueError(f"chunk truncado em {path}: {chunk_id!r}")
            if chunk_id == b"fmt ":
                handle.seek(data_start)
                fmt = handle.read(chunk_size)
            elif chunk_id == b"data":
                handle.seek(data_start)
                remaining = chunk_size
                while remaining:
                    piece = handle.read(min(4 * 1024 * 1024, remaining))
                    if not piece:
                        raise ValueError(f"payload truncado: {path}")
                    payload_digest.update(piece)
                    data_bytes += len(piece)
                    remaining -= len(piece)
            pos = data_start + chunk_size + (chunk_size & 1)
    if fmt is None or len(fmt) < 16 or data_bytes == 0:
        raise ValueError(f"fmt/data ausente: {path}")
    audio_format, channels, sample_rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    effective_format = audio_format
    if audio_format == 0xFFFE and len(fmt) >= 40:
        effective_format = struct.unpack_from("<H", fmt, 24)[0]
    if effective_format == 1:
        encoding = "PCM_signed_integer"
    elif effective_format == 3:
        encoding = "PCM_IEEE_float"
    else:
        encoding = f"WAVE_format_{effective_format}"
    if channels <= 0 or sample_rate <= 0 or block_align <= 0 or data_bytes % block_align:
        raise ValueError(f"formato/frame invalido: {path}")
    frames = data_bytes // block_align
    return {
        "path": str(path.relative_to(BASE)),
        "size_bytes": size,
        "encoding": encoding,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": block_align // channels,
        "bit_depth": bits,
        "frames": frames,
        "duration_seconds": frames / sample_rate,
        "file_sha256": sha256_file(path),
        "pcm_payload_sha256": payload_digest.hexdigest(),
    }


def inspect_source(path: Path) -> dict:
    completed = subprocess.run(
        [str(INSPECTOR), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(completed.stdout)
    info["size_bytes"] = path.stat().st_size
    return info


def expected_names(kind: str) -> list[str]:
    if kind == "raw":
        return [f"{index:02d}_{item}_RAW_STEREO48.wav" for index, item in enumerate(ORDER, 1)]
    return [f"{index:02d}_{item}_TARGET_CAPCUT.wav" for index, item in enumerate(ORDER, 1)]


def assert_exact_directory(directory: Path, expected: list[str]) -> None:
    actual = sorted(path.name for path in directory.glob("*.wav"))
    if actual != sorted(expected):
        raise ValueError(f"conteudo inesperado em {directory}: {actual}")


def main() -> None:
    if MANIFEST_JSON.exists() or MANIFEST_MD.exists():
        raise FileExistsError("manifesto existente; nao sera sobrescrito")
    if ORDER != list(dict.fromkeys(ORDER)) or set(ORDER) & set(EXCLUDED):
        raise ValueError("escopo de IDs invalido")
    raw_dir = BASE / "raws_ordered"
    target_dir = BASE / "targets_ordered"
    raw_names = expected_names("raw")
    target_names = expected_names("target")
    assert_exact_directory(raw_dir, raw_names)
    assert_exact_directory(target_dir, target_names)

    raws: dict[str, dict] = {}
    targets: dict[str, dict] = {}
    pairs = []
    for order_index, item in enumerate(ORDER, 1):
        raw_path = raw_dir / raw_names[order_index - 1]
        target_path = target_dir / target_names[order_index - 1]
        target_source = EDIT / f"{item}-capcut.WAV"
        media_source = EDIT / SOURCE_NAMES[item]
        provenance = RAW_PROVENANCE[item]
        for required in (raw_path, target_path, target_source, media_source, provenance):
            if not required.is_file():
                raise FileNotFoundError(required)

        raw = inspect_wav(raw_path)
        provenance_sha = sha256_file(provenance)
        raw.update({
            "order": order_index,
            "prepared_from_path": str(provenance),
            "prepared_from_file_sha256": provenance_sha,
            "byte_identical_to_prepared_from": raw["file_sha256"] == provenance_sha,
            "source_media_path": str(media_source),
            "source_media_audio": inspect_source(media_source),
        })
        if not raw["byte_identical_to_prepared_from"]:
            raise ValueError(f"raw {item} nao coincide com a proveniencia")
        if raw["sample_rate"] != 48_000 or raw["channels"] != 2:
            raise ValueError(f"raw {item} nao e stereo 48 kHz")

        target = inspect_wav(target_path)
        target_source_sha = sha256_file(target_source)
        target.update({
            "order": order_index,
            "source_path": str(target_source),
            "source_file_sha256": target_source_sha,
            "byte_identical_to_source": target["file_sha256"] == target_source_sha,
        })
        if not target["byte_identical_to_source"]:
            raise ValueError(f"target {item} nao e copia byte a byte")

        raws[item] = raw
        targets[item] = target
        pairs.append({
            "id": item,
            "order": order_index,
            "raw_path": raw["path"],
            "target_path": target["path"],
            "raw_frames": raw["frames"],
            "target_frames": target["frames"],
        })

    manifest = {
        "schema_version": 1,
        "suite": "fluxo_aprovado_20_20260814",
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec="seconds"),
        "fixed_order": ORDER,
        "scope": {
            "authorized_ids_only": ORDER,
            "explicitly_excluded_ids": EXCLUDED,
            "ids_below_64_read": False,
            "source_directory_read_only": str(EDIT),
            "preset_modified": False,
        },
        "counts": {
            "pairs": len(ORDER),
            "raws": len(raws),
            "targets": len(targets),
        },
        "raws_total_frames": sum(item["frames"] for item in raws.values()),
        "raws_total_duration_seconds": sum(item["duration_seconds"] for item in raws.values()),
        "raws": raws,
        "targets": targets,
        "pairs": pairs,
        "preflight_status": "PASS_20_OF_20",
    }
    MANIFEST_JSON.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Preflight — fluxo aprovado, 20 pares",
        "",
        "Status: **PASS — 20/20 pares presentes e auditados.**",
        "",
        f"Ordem fixa: `{', '.join(ORDER)}`.",
        "",
        "Escopo excluído e não lido: `70, 77, 83, 21, 38, 39, 51` e qualquer ID fora da ordem acima.",
        "",
        "Os raws são estéreo/48 kHz. Os targets são cópias byte a byte dos `N-capcut.WAV` pessoais, mantendo o formato original.",
        "",
        "| Ordem | ID | Fonte | Raw | Frames raw | Duração raw | Target | Frames target | Cópias verificadas |",
        "|---:|---:|---|---|---:|---:|---|---:|---|",
    ]
    for item in ORDER:
        raw, target = raws[item], targets[item]
        raw_format = f"{raw['sample_rate']} Hz/{raw['channels']} ch/{raw['bit_depth']} bit"
        target_format = f"{target['sample_rate']} Hz/{target['channels']} ch/{target['bit_depth']} bit"
        lines.append(
            f"| {raw['order']:02d} | {item} | `{Path(SOURCE_NAMES[item]).name}` | {raw_format} | {raw['frames']} | {raw['duration_seconds']:.6f}s | {target_format} | {target['frames']} | raw ✓ · target ✓ |"
        )
    lines.extend([
        "",
        f"Total de frames raw: **{manifest['raws_total_frames']}** ({manifest['raws_total_duration_seconds']:.6f} s a 48 kHz).",
        "",
        "O JSON contém SHA-256 do arquivo completo e do payload PCM de cada WAV.",
    ])
    MANIFEST_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
