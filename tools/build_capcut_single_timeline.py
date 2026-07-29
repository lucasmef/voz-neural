#!/usr/bin/env python3
"""Build a single, fixed-timecode CapCut black-box audio timeline."""

import csv
import hashlib
import wave
from pathlib import Path


FS = 48_000
SAMPLE_WIDTH = 3
SECTION_SECONDS = 14
GAP_SECONDS = 1
BASE = Path(__file__).resolve().parents[1]
SOURCE = BASE / "capcut_blackbox_audio_pack_female_v3"
OUTPUT = BASE / "capcut_blackbox_single_female_v1"
WAV_NAME = "CAPCUT_TESTE_UNICO_FEMININO.wav"

SECTIONS = [
    (
        "00:00",
        "Voz feminina limpa",
        "10_speech_quality/S01_female_clean_ptbr.wav",
        "Referência de timbre e dano introduzido pelos efeitos.",
    ),
    (
        "00:15",
        "Voz em três níveis",
        "10_speech_quality/S03_female_level_steps.wav",
        "Normalização, ganho automático, compressão e gate.",
    ),
    (
        "00:30",
        "Voz limitada como telefone",
        "10_speech_quality/S04_female_bandlimited_telephone.wav",
        "Reconstrução de agudos e alteração de EQ.",
    ),
    (
        "00:45",
        "Voz feminina com reverberação",
        "10_speech_quality/S06_female_alt_room_reverb.wav",
        "Redução de sala e preservação da naturalidade.",
    ),
    (
        "01:00",
        "Ruído branco sem voz",
        "20_noise_only/N01_white_noise.wav",
        "Supressão estacionária e falsos positivos de voz.",
    ),
    (
        "01:15",
        "Ventilador sem voz",
        "20_noise_only/N04_fan_ac_noise.wav",
        "Supressão de ruído realista e modulado.",
    ),
    (
        "01:30",
        "Voz com ruído branco",
        "30_speech_noise_mixes/M02_female_white_snr_0.wav",
        "Separação de voz e ruído em condição difícil.",
    ),
    (
        "01:45",
        "Voz com ventilador",
        "30_speech_noise_mixes/M04_female_alt_fan_snr_0.wav",
        "Redução de ventilador preservando a fala.",
    ),
    (
        "02:00",
        "Voz sob música alta",
        "40_voice_focus/F02_female_music_snr_m5.wav",
        "Foco na voz com música mais alta que a fala.",
    ),
    (
        "02:15",
        "Duas vozes femininas",
        "40_voice_focus/F01_two_female_speakers_overlap.wav",
        "Comportamento quando existem duas locutoras.",
    ),
    (
        "02:30",
        "Voz central e música estéreo",
        "40_voice_focus/F03_center_voice_wide_music_stereo.wav",
        "Uso de informação espacial pelo foco na voz.",
    ),
    (
        "02:45",
        "Música estéreo sem voz",
        "40_voice_focus/F05_music_only_stereo.wav",
        "Supressão e falsos positivos quando não há fala.",
    ),
]


def to_stereo_pcm24(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != FS or wav.getsampwidth() != SAMPLE_WIDTH:
            raise RuntimeError(f"Unexpected WAV format: {path}")
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())

    if channels == 2:
        return frames
    if channels != 1:
        raise RuntimeError(f"Unsupported channel count: {channels} in {path}")

    stereo = bytearray(len(frames) * 2)
    write_at = 0
    for read_at in range(0, len(frames), SAMPLE_WIDTH):
        sample = frames[read_at : read_at + SAMPLE_WIDTH]
        stereo[write_at : write_at + SAMPLE_WIDTH] = sample
        stereo[write_at + SAMPLE_WIDTH : write_at + 2 * SAMPLE_WIDTH] = sample
        write_at += 2 * SAMPLE_WIDTH
    return bytes(stereo)


def main():
    if OUTPUT.exists():
        raise SystemExit(f"Output already exists; refusing to overwrite: {OUTPUT}")
    OUTPUT.mkdir(parents=True)

    bytes_per_frame = 2 * SAMPLE_WIDTH
    section_size = SECTION_SECONDS * FS * bytes_per_frame
    gap = b"\x00" * (GAP_SECONDS * FS * bytes_per_frame)
    timeline = bytearray()

    rows = []
    for index, (timecode, label, relative_path, purpose) in enumerate(SECTIONS, 1):
        source_path = SOURCE / relative_path
        section = to_stereo_pcm24(source_path)
        section = section[:section_size]
        if len(section) < section_size:
            section += b"\x00" * (section_size - len(section))
        timeline.extend(section)
        timeline.extend(gap)
        rows.append(
            {
                "bloco": f"{index:02d}",
                "inicio": timecode,
                "fim_amostra": f"{(index - 1) * 15 // 60:02d}:{((index - 1) * 15 + 14) % 60:02d}",
                "proximo_bloco": f"{index * 15 // 60:02d}:{(index * 15) % 60:02d}",
                "conteudo": label,
                "objetivo": purpose,
            }
        )

    wav_path = OUTPUT / WAV_NAME
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(FS)
        wav.writeframes(timeline)

    with (OUTPUT / "MAPA_DE_TIMECODE.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    digest = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    (OUTPUT / "SHA256.txt").write_text(
        f"{digest}  {WAV_NAME}\n",
        encoding="utf-8",
    )
    print(f"{wav_path}\nsections={len(SECTIONS)}\nduration=180.000\nsha256={digest}")


if __name__ == "__main__":
    main()
