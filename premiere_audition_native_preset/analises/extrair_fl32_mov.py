#!/usr/bin/env python3
"""Extrai uma faixa QuickTime fl32 para WAV PCM24 sem ffmpeg.

O script e estritamente de analise: le o MOV, valida a tabela de amostras e
grava uma copia WAV no caminho indicado. A fonte nunca e alterada.
"""

from __future__ import annotations

import argparse
import json
import struct
import wave
from pathlib import Path

import numpy as np


CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf"}


def atoms(handle, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        handle.seek(pos)
        header = handle.read(8)
        if len(header) != 8:
            break
        size32, atom_type = struct.unpack(">I4s", header)
        header_size = 8
        if size32 == 1:
            size = struct.unpack(">Q", handle.read(8))[0]
            header_size = 16
        elif size32 == 0:
            size = end - pos
        else:
            size = size32
        if size < header_size or pos + size > end:
            raise ValueError(f"atomo invalido {atom_type!r} em {pos}: {size}")
        yield atom_type, pos, size, header_size
        pos += size


def children(handle, atom):
    atom_type, pos, size, header_size = atom
    if atom_type not in CONTAINERS:
        raise ValueError(f"{atom_type!r} nao e container")
    return list(atoms(handle, pos + header_size, pos + size))


def child(handle, parent, kind: bytes):
    matches = [item for item in children(handle, parent) if item[0] == kind]
    if len(matches) != 1:
        raise ValueError(f"esperado um {kind!r}; encontrados {len(matches)}")
    return matches[0]


def payload(handle, atom):
    _kind, pos, size, header_size = atom
    handle.seek(pos + header_size)
    return handle.read(size - header_size)


def handler_type(handle, trak):
    mdia = child(handle, trak, b"mdia")
    hdlr = child(handle, mdia, b"hdlr")
    data = payload(handle, hdlr)
    if len(data) < 12:
        raise ValueError("hdlr truncado")
    return data[8:12]


def parse_stsd(handle, stsd):
    data = payload(handle, stsd)
    if len(data) < 16:
        raise ValueError("stsd truncado")
    entry_count = struct.unpack_from(">I", data, 4)[0]
    if entry_count != 1:
        raise ValueError(f"stsd com {entry_count} entradas")
    entry_size, codec = struct.unpack_from(">I4s", data, 8)
    entry = data[8 : 8 + entry_size]
    if len(entry) != entry_size or entry_size < 36:
        raise ValueError("sample entry truncada")
    channels = struct.unpack_from(">H", entry, 24)[0]
    bits = struct.unpack_from(">H", entry, 26)[0]
    sample_rate = struct.unpack_from(">I", entry, 32)[0] >> 16
    endian = "big"
    marker = entry.find(b"enda")
    if marker >= 4 and marker + 6 <= len(entry):
        endian = "little" if struct.unpack_from(">H", entry, marker + 4)[0] == 1 else "big"
    return {
        "codec": codec.decode("ascii", "replace"),
        "channels": channels,
        "bits": bits,
        "sample_rate": sample_rate,
        "endian": endian,
    }


def parse_stsz(handle, atom):
    data = payload(handle, atom)
    default_size, count = struct.unpack_from(">II", data, 4)
    if default_size:
        return default_size, count, None
    sizes = np.frombuffer(data, dtype=">u4", offset=12, count=count).astype(np.int64)
    return default_size, count, sizes


def parse_stco(handle, atom):
    data = payload(handle, atom)
    count = struct.unpack_from(">I", data, 4)[0]
    return np.frombuffer(data, dtype=">u4", offset=8, count=count).astype(np.int64)


def parse_co64(handle, atom):
    data = payload(handle, atom)
    count = struct.unpack_from(">I", data, 4)[0]
    return np.frombuffer(data, dtype=">u8", offset=8, count=count).astype(np.int64)


def parse_stsc(handle, atom):
    data = payload(handle, atom)
    count = struct.unpack_from(">I", data, 4)[0]
    return [struct.unpack_from(">III", data, 8 + 12 * i) for i in range(count)]


def samples_per_chunk(stsc, chunk_count: int):
    result = []
    for chunk_number in range(1, chunk_count + 1):
        active = stsc[0]
        for entry in stsc:
            if entry[0] <= chunk_number:
                active = entry
            else:
                break
        result.append(active[1])
    return result


def pcm24_bytes(samples: np.ndarray):
    clipped = np.clip(samples, -1.0, np.nextafter(np.float32(1.0), np.float32(0.0)))
    integers = np.rint(clipped * 8388608.0).astype(np.int32)
    unsigned = integers.astype(np.uint32)
    packed = np.empty((unsigned.size, 3), dtype=np.uint8)
    packed[:, 0] = unsigned & 0xFF
    packed[:, 1] = (unsigned >> 8) & 0xFF
    packed[:, 2] = (unsigned >> 16) & 0xFF
    return packed.tobytes()


def extract(source: Path, output: Path):
    if output.exists():
        raise FileExistsError(f"saida ja existe: {output}")
    with source.open("rb") as handle:
        file_size = source.stat().st_size
        top = list(atoms(handle, 0, file_size))
        moov = [item for item in top if item[0] == b"moov"]
        if len(moov) != 1:
            raise ValueError("moov ausente ou duplicado")
        tracks = [item for item in children(handle, moov[0]) if item[0] == b"trak"]
        audio_tracks = [item for item in tracks if handler_type(handle, item) == b"soun"]
        if len(audio_tracks) != 1:
            raise ValueError(f"esperada uma faixa de audio; encontradas {len(audio_tracks)}")
        mdia = child(handle, audio_tracks[0], b"mdia")
        stbl = child(handle, child(handle, mdia, b"minf"), b"stbl")
        table = {item[0]: item for item in children(handle, stbl)}
        info = parse_stsd(handle, table[b"stsd"])
        if info["codec"] != "fl32" or info["bits"] != 32:
            raise ValueError(f"codec nao suportado: {info}")
        default_size, frame_count, sizes = parse_stsz(handle, table[b"stsz"])
        offsets = parse_stco(handle, table[b"stco"]) if b"stco" in table else parse_co64(handle, table[b"co64"])
        chunk_frames = samples_per_chunk(parse_stsc(handle, table[b"stsc"]), len(offsets))
        expected_frame_bytes = info["channels"] * 4
        if default_size not in (0, expected_frame_bytes):
            raise ValueError(f"stsz inesperado: {default_size}, esperado {expected_frame_bytes}")
        pieces = []
        sample_index = 0
        dtype = "<f4" if info["endian"] == "little" else ">f4"
        for offset, frames in zip(offsets, chunk_frames):
            byte_count = frames * expected_frame_bytes if default_size else int(sizes[sample_index : sample_index + frames].sum())
            handle.seek(int(offset))
            raw = handle.read(byte_count)
            if len(raw) != byte_count:
                raise ValueError("chunk de audio truncado")
            chunk = np.frombuffer(raw, dtype=dtype)
            if chunk.size != frames * info["channels"]:
                raise ValueError("chunk nao coincide com o numero de frames")
            pieces.append(chunk.astype(np.float32, copy=False))
            sample_index += frames
        samples = np.concatenate(pieces)
        if sample_index != frame_count or samples.size != frame_count * info["channels"]:
            raise ValueError(f"contagem inconsistente: {sample_index} vs {frame_count}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(info["channels"])
        wav.setsampwidth(3)
        wav.setframerate(info["sample_rate"])
        wav.writeframes(pcm24_bytes(samples))
    return {
        "source": str(source.resolve()),
        "output": str(output.resolve()),
        **info,
        "frames": frame_count,
        "duration_seconds": frame_count / info["sample_rate"],
        "chunks": len(offsets),
        "output_bytes": output.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extract(args.input, args.output), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
