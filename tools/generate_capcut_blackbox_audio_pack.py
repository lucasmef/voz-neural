#!/usr/bin/env python3
"""Generate deterministic audio fixtures for CapCut black-box testing.

Only Python's standard library and macOS built-in speech/audio tools are used.
"""

import csv
import hashlib
import json
import math
import random
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path


FS = 48_000
BIT_DEPTH = 24
SEED = 27_072_026
RNG = random.Random(SEED)
ROOT = Path(__file__).resolve().parents[1] / "capcut_blackbox_audio_pack_female_v3"
TTS_SOURCE_ROOT = Path("/private/tmp/capcut_blackbox_tts_sources")


def db_to_amp(db):
    return 10.0 ** (db / 20.0)


def clamp(value, low=-1.0, high=1.0):
    return min(high, max(low, value))


def peak(samples):
    return max((abs(x) for x in samples), default=0.0)


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def dbfs(value):
    return 20.0 * math.log10(max(value, 1e-12))


def scale_to_peak(samples, target_dbfs):
    current = peak(samples)
    if current <= 0:
        return list(samples)
    factor = db_to_amp(target_dbfs) / current
    return [x * factor for x in samples]


def scale_to_rms(samples, target_dbfs):
    current = rms(samples)
    if current <= 0:
        return list(samples)
    factor = db_to_amp(target_dbfs) / current
    return [x * factor for x in samples]


def pad(samples, before_s=1.0, after_s=1.0):
    return [0.0] * int(before_s * FS) + list(samples) + [0.0] * int(after_s * FS)


def repeat_to_length(samples, length, offset=0):
    if not samples:
        return [0.0] * length
    return [samples[(i + offset) % len(samples)] for i in range(length)]


def fit_length(samples, length):
    if len(samples) >= length:
        return list(samples[:length])
    return list(samples) + [0.0] * (length - len(samples))


def delay(samples, delay_s):
    delay_n = int(delay_s * FS)
    return [0.0] * delay_n + list(samples[:-delay_n] if delay_n else samples)


def normalize_safely(channels, target_peak_dbfs=-2.0):
    current = max((peak(ch) for ch in channels), default=0.0)
    target = db_to_amp(target_peak_dbfs)
    if current <= target or current == 0:
        return [list(ch) for ch in channels]
    factor = target / current
    return [[x * factor for x in ch] for ch in channels]


def mix_at_snr(signal, noise, snr_db):
    noise = repeat_to_length(noise, len(signal))
    signal_rms = rms(signal)
    noise_rms = rms(noise)
    if signal_rms == 0 or noise_rms == 0:
        return list(signal)
    wanted_noise_rms = signal_rms / db_to_amp(snr_db)
    factor = wanted_noise_rms / noise_rms
    mixed = [s + n * factor for s, n in zip(signal, noise)]
    return normalize_safely([mixed])[0]


def write_wav(path, channels):
    path.parent.mkdir(parents=True, exist_ok=True)
    channels = [list(ch) for ch in channels]
    frame_count = min(len(ch) for ch in channels)
    payload = bytearray(frame_count * len(channels) * 3)
    cursor = 0
    max_int = (1 << 23) - 1
    min_int = -(1 << 23)
    for i in range(frame_count):
        for ch in channels:
            value = int(round(clamp(ch[i]) * max_int))
            value = min(max_int, max(min_int, value))
            if value < 0:
                value += 1 << 24
            payload[cursor] = value & 0xFF
            payload[cursor + 1] = (value >> 8) & 0xFF
            payload[cursor + 2] = (value >> 16) & 0xFF
            cursor += 3
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(len(channels))
        wav.setsampwidth(3)
        wav.setframerate(FS)
        wav.writeframes(payload)


def read_pcm16_wav(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != FS:
            raise RuntimeError(f"Unexpected intermediate WAV format: {path}")
        frames = wav.readframes(wav.getnframes())
    values = struct.unpack("<{}h".format(len(frames) // 2), frames)
    return [value / 32768.0 for value in values]


def synthesize_tts(temp_dir, voice, rate, stem, text):
    aiff = TTS_SOURCE_ROOT / f"{stem}.aiff"
    wav = temp_dir / f"{stem}.wav"
    if not aiff.exists() or aiff.stat().st_size < 4097:
        raise RuntimeError(f"Missing or empty pre-generated TTS source: {aiff}")
    subprocess.run(
        [
            "/usr/bin/afconvert",
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{FS}",
            str(aiff),
            str(wav),
        ],
        check=True,
    )
    return read_pcm16_wav(wav)


def make_speech(temp_dir, voice, stem, rate=175):
    first = (
        "Este arquivo foi criado para medir o processamento de voz. "
        "Hoje o céu está limpo, mas o ventilador permanece ligado. "
        "Pedro trouxe seis pratos, três copos e uma caixa azul."
    )
    second = (
        "Depois desta pausa, a frase continua com sons fortes, suaves e sibilantes. "
        "Chaves, chuva, folhas e passos ajudam a revelar mudanças no áudio."
    )
    part_a = synthesize_tts(temp_dir, voice, rate, f"{stem}_a", first)
    part_b = synthesize_tts(temp_dir, voice, rate, f"{stem}_b", second)
    speech = (
        [0.0] * FS
        + part_a
        + [0.0] * int(0.9 * FS)
        + part_b
        + [0.0] * FS
    )
    return scale_to_peak(speech, -6.0)


def lowpass(samples, cutoff_hz, passes=1):
    result = list(samples)
    alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / FS)
    for _ in range(passes):
        output = []
        previous = 0.0
        for value in result:
            previous += alpha * (value - previous)
            output.append(previous)
        result = output
    return result


def highpass(samples, cutoff_hz, passes=1):
    result = list(samples)
    alpha = math.exp(-2.0 * math.pi * cutoff_hz / FS)
    for _ in range(passes):
        output = []
        previous_y = 0.0
        previous_x = 0.0
        for value in result:
            current_y = alpha * (previous_y + value - previous_x)
            output.append(current_y)
            previous_y = current_y
            previous_x = value
        result = output
    return result


def room_reverb(samples):
    taps = [
        (0.031, 0.35),
        (0.047, 0.29),
        (0.071, 0.24),
        (0.103, 0.20),
        (0.149, 0.17),
        (0.211, 0.14),
        (0.307, 0.11),
        (0.419, 0.09),
        (0.557, 0.075),
        (0.733, 0.060),
        (0.941, 0.048),
        (1.173, 0.038),
        (1.427, 0.030),
    ]
    output = list(samples)
    for delay_s, gain in taps:
        offset = int(delay_s * FS)
        for i in range(offset, len(output)):
            output[i] += samples[i - offset] * gain
    return scale_to_peak(output, -3.0)


def make_white_noise(duration_s):
    count = int(duration_s * FS)
    values = [RNG.uniform(-1.0, 1.0) for _ in range(count)]
    return scale_to_rms(values, -24.0)


def make_pink_noise(duration_s):
    count = int(duration_s * FS)
    b0 = b1 = b2 = b3 = b4 = b5 = b6 = 0.0
    values = []
    for _ in range(count):
        white = RNG.uniform(-1.0, 1.0)
        b0 = 0.99886 * b0 + white * 0.0555179
        b1 = 0.99332 * b1 + white * 0.0750759
        b2 = 0.96900 * b2 + white * 0.1538520
        b3 = 0.86650 * b3 + white * 0.3104856
        b4 = 0.55000 * b4 + white * 0.5329522
        b5 = -0.7616 * b5 - white * 0.0168980
        pink = b0 + b1 + b2 + b3 + b4 + b5 + b6 + white * 0.5362
        b6 = white * 0.115926
        values.append(pink)
    return scale_to_rms(values, -24.0)


def make_hum(duration_s):
    values = []
    for i in range(int(duration_s * FS)):
        t = i / FS
        value = (
            math.sin(2 * math.pi * 60 * t)
            + 0.45 * math.sin(2 * math.pi * 120 * t + 0.2)
            + 0.22 * math.sin(2 * math.pi * 180 * t + 0.5)
            + 0.10 * math.sin(2 * math.pi * 240 * t + 0.8)
        )
        values.append(value)
    return scale_to_rms(values, -24.0)


def make_fan_noise(duration_s):
    raw = [RNG.uniform(-1.0, 1.0) for _ in range(int(duration_s * FS))]
    shaped = lowpass(raw, 1400.0, passes=2)
    values = []
    for i, value in enumerate(shaped):
        t = i / FS
        modulation = 0.78 + 0.16 * math.sin(2 * math.pi * 0.37 * t)
        motor = 0.12 * math.sin(2 * math.pi * 60 * t)
        values.append(value * modulation + motor)
    return scale_to_rms(values, -24.0)


def make_transients(duration_s):
    count = int(duration_s * FS)
    values = [RNG.uniform(-0.001, 0.001) for _ in range(count)]
    event = int(0.55 * FS)
    while event < count:
        length = int(RNG.uniform(0.018, 0.055) * FS)
        frequency = RNG.uniform(900, 4200)
        amplitude = RNG.uniform(0.25, 0.65)
        for j in range(min(length, count - event)):
            decay = math.exp(-8.0 * j / max(1, length))
            values[event + j] += amplitude * decay * math.sin(
                2 * math.pi * frequency * j / FS
            )
        event += int(RNG.uniform(0.32, 0.92) * FS)
    return scale_to_peak(values, -6.0)


def make_music(duration_s):
    count = int(duration_s * FS)
    chord_roots = [130.81, 98.00, 110.00, 87.31]
    values = []
    for i in range(count):
        t = i / FS
        beat = int(t / 0.5)
        root = chord_roots[(beat // 4) % len(chord_roots)]
        local = t % 2.0
        envelope = 0.50 + 0.50 * math.exp(-3.2 * (t % 0.5))
        chord = (
            math.sin(2 * math.pi * root * t)
            + 0.62 * math.sin(2 * math.pi * root * 1.25 * t + 0.3)
            + 0.48 * math.sin(2 * math.pi * root * 1.50 * t + 0.6)
            + 0.18 * math.sin(2 * math.pi * root * 2.00 * t)
        )
        kick_phase = t % 0.5
        kick = math.exp(-18 * kick_phase) * math.sin(
            2 * math.pi * (72 - 38 * kick_phase) * kick_phase
        )
        hat_phase = (t + 0.25) % 0.5
        hat = (
            RNG.uniform(-1.0, 1.0)
            * math.exp(-55 * hat_phase)
            * (1.0 if hat_phase < 0.08 else 0.0)
        )
        bass = math.sin(2 * math.pi * root / 2.0 * t) * math.exp(-1.1 * local)
        values.append(0.22 * chord * envelope + 0.35 * kick + 0.08 * hat + 0.18 * bass)
    return scale_to_rms(values, -24.0)


def make_sweep(duration_s):
    count = int(duration_s * FS)
    f0 = 20.0
    f1 = 20_000.0
    log_ratio = math.log(f1 / f0)
    values = []
    for i in range(count):
        t = i / FS
        phase = 2 * math.pi * f0 * duration_s / log_ratio * (
            math.exp(t * log_ratio / duration_s) - 1
        )
        fade = min(1.0, t / 0.12, (duration_s - t) / 0.12)
        values.append(0.22 * max(0.0, fade) * math.sin(phase))
    return values


def make_impulses(duration_s):
    values = [0.0] * int(duration_s * FS)
    for index, time_s in enumerate([1.0, 2.5, 4.0, 6.0, 8.0, 9.5]):
        values[int(time_s * FS)] = db_to_amp(-6.0) * (1.0 if index % 2 == 0 else -1.0)
    return values


def make_multitone(duration_s):
    frequencies = [60, 120, 250, 500, 1000, 2000, 4000, 8000, 12000, 16000]
    values = []
    for i in range(int(duration_s * FS)):
        t = i / FS
        value = sum(
            math.sin(2 * math.pi * frequency * t + index * 0.37)
            for index, frequency in enumerate(frequencies)
        )
        values.append(value)
    return scale_to_peak(values, -12.0)


def make_level_steps():
    values = [0.0] * FS
    for level_db in [-36.0, -24.0, -12.0, -6.0]:
        amplitude = db_to_amp(level_db)
        values.extend(
            amplitude * math.sin(2 * math.pi * 1000 * i / FS)
            for i in range(2 * FS)
        )
        values.extend([0.0] * int(0.5 * FS))
    values.extend([0.0] * FS)
    return values


def stepped_speech(speech):
    active = [x for x in speech if abs(x) > 1e-5]
    segment = active[: 3 * FS] if active else [0.0] * (3 * FS)
    values = [0.0] * FS
    for attenuation in [0.0, -12.0, -24.0]:
        values.extend(x * db_to_amp(attenuation) for x in segment)
        values.extend([0.0] * FS)
    return values


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    if ROOT.exists():
        raise SystemExit(f"Output already exists; refusing to overwrite: {ROOT}")

    records = []

    def add(category, filename, channels, description, targets, condition):
        path = ROOT / category / filename
        channels = normalize_safely(channels)
        write_wav(path, channels)
        duration = min(len(ch) for ch in channels) / FS
        records.append(
            {
                "id": Path(filename).stem.split("_", 1)[0],
                "category": category,
                "filename": str(path.relative_to(ROOT)),
                "channels": len(channels),
                "sample_rate_hz": FS,
                "bit_depth": BIT_DEPTH,
                "duration_s": f"{duration:.3f}",
                "peak_dbfs": f"{dbfs(max(peak(ch) for ch in channels)):.2f}",
                "rms_dbfs": f"{dbfs(max(rms(ch) for ch in channels)):.2f}",
                "description": description,
                "recommended_targets": targets,
                "known_condition": condition,
            }
        )

    with tempfile.TemporaryDirectory(prefix="capcut_audio_pack_") as temp:
        temp_dir = Path(temp)
        female = make_speech(temp_dir, "Luciana", "female", rate=175)
        female_alt = make_speech(
            temp_dir,
            "Sandy (Português (Brasil))",
            "female_alt",
            rate=170,
        )

    common_duration = max(len(female), len(female_alt)) / FS
    noise_duration = max(12.0, common_duration)
    female = fit_length(female, int(noise_duration * FS))
    female_alt = fit_length(female_alt, len(female))

    white = make_white_noise(noise_duration)
    pink = make_pink_noise(noise_duration)
    hum = make_hum(noise_duration)
    fan = make_fan_noise(noise_duration)
    transients = make_transients(noise_duration)
    music = make_music(noise_duration)

    add(
        "00_calibration",
        "C01_digital_silence.wav",
        [[0.0] * (10 * FS)],
        "Digital silence; reveals added noise, gating, padding, or artifacts.",
        "CTRL,NR,ENH,FOCUS",
        "10 s, exact digital zero",
    )
    add(
        "00_calibration",
        "C02_impulse_train.wav",
        [make_impulses(10.5)],
        "Alternating impulses; measures latency, pre-ringing, smearing, and alignment.",
        "CTRL,NR,ENH,FOCUS",
        "Impulses at known timestamps, +/-6 dBFS",
    )
    add(
        "00_calibration",
        "C03_log_sweep_20hz_20khz.wav",
        [make_sweep(12.0)],
        "Logarithmic sine sweep; estimates frequency response and nonlinear artifacts.",
        "CTRL,ENH",
        "20 Hz to 20 kHz over 12 s",
    )
    add(
        "00_calibration",
        "C04_multitone.wav",
        [make_multitone(10.0)],
        "Ten simultaneous tones; shows spectral shaping and distortion.",
        "CTRL,ENH",
        "60 Hz to 16 kHz, peak -12 dBFS",
    )
    add(
        "00_calibration",
        "C05_level_steps_1khz.wav",
        [make_level_steps()],
        "Level steps; measures compression, expansion, gating, and auto gain.",
        "CTRL,ENH",
        "1 kHz at -36/-24/-12/-6 dBFS",
    )

    add(
        "10_speech_quality",
        "S01_female_clean_ptbr.wav",
        [female],
        "Clean Brazilian Portuguese synthetic female speech with silence gaps.",
        "CTRL,NR,ENH,FOCUS",
        "TTS Luciana, peak -6 dBFS",
    )
    add(
        "10_speech_quality",
        "S02_female_alt_clean_ptbr.wav",
        [female_alt],
        "Clean Brazilian Portuguese synthetic alternate female speech with silence gaps.",
        "CTRL,NR,ENH,FOCUS",
        "TTS Sandy pt-BR, peak -6 dBFS",
    )
    add(
        "10_speech_quality",
        "S03_female_level_steps.wav",
        [stepped_speech(female)],
        "Same speech excerpt repeated at three levels; probes auto leveling.",
        "CTRL,ENH",
        "Relative levels 0/-12/-24 dB",
    )
    telephone = lowpass(highpass(female, 300.0, passes=2), 3400.0, passes=2)
    add(
        "10_speech_quality",
        "S04_female_bandlimited_telephone.wav",
        [scale_to_peak(telephone, -6.0)],
        "Band-limited speech; reveals spectral restoration or synthetic brightness.",
        "CTRL,ENH",
        "Approx. 300 Hz to 3.4 kHz",
    )
    clipped = [clamp(x * 4.0, -0.34, 0.34) for x in female]
    add(
        "10_speech_quality",
        "S05_female_hard_clipped.wav",
        [scale_to_peak(clipped, -3.0)],
        "Hard-clipped speech; tests whether enhancement attempts declipping.",
        "CTRL,ENH",
        "Intentional symmetrical clipping",
    )
    add(
        "10_speech_quality",
        "S06_female_alt_room_reverb.wav",
        [room_reverb(female_alt)],
        "Alternate female speech with deterministic synthetic room reflections.",
        "CTRL,ENH,FOCUS",
        "Sparse reflections through 1.427 s",
    )

    add(
        "20_noise_only",
        "N01_white_noise.wav",
        [white],
        "Stationary white noise only.",
        "CTRL,NR,ENH,FOCUS",
        "RMS -24 dBFS",
    )
    add(
        "20_noise_only",
        "N02_pink_noise.wav",
        [pink],
        "Stationary pink noise only.",
        "CTRL,NR,ENH,FOCUS",
        "RMS -24 dBFS",
    )
    add(
        "20_noise_only",
        "N03_hum_60hz_harmonics.wav",
        [hum],
        "60 Hz mains hum with harmonics.",
        "CTRL,NR,ENH",
        "60/120/180/240 Hz, RMS -24 dBFS",
    )
    add(
        "20_noise_only",
        "N04_fan_ac_noise.wav",
        [fan],
        "Synthetic fan/air-conditioner noise with slow modulation.",
        "CTRL,NR,ENH",
        "Colored broadband plus motor tone",
    )
    add(
        "20_noise_only",
        "N05_keyboard_clicks.wav",
        [transients],
        "Irregular short transient clicks over a very low noise floor.",
        "CTRL,NR,ENH",
        "Deterministic transient sequence",
    )
    add(
        "20_noise_only",
        "N06_synthetic_music.wav",
        [music],
        "Original synthetic chord/drum bed with no speech.",
        "CTRL,FOCUS",
        "Deterministic, copyright-free synthetic music",
    )

    add(
        "30_speech_noise_mixes",
        "M01_female_white_snr_p10.wav",
        [mix_at_snr(female, white, 10.0)],
        "Female speech plus white noise at +10 dB SNR.",
        "CTRL,NR,ENH",
        "Global RMS SNR +10 dB",
    )
    add(
        "30_speech_noise_mixes",
        "M02_female_white_snr_0.wav",
        [mix_at_snr(female, white, 0.0)],
        "Female speech plus white noise at 0 dB SNR.",
        "CTRL,NR,ENH",
        "Global RMS SNR 0 dB",
    )
    add(
        "30_speech_noise_mixes",
        "M03_female_alt_pink_snr_p10.wav",
        [mix_at_snr(female_alt, pink, 10.0)],
        "Alternate female speech plus pink noise at +10 dB SNR.",
        "CTRL,NR,ENH",
        "Global RMS SNR +10 dB",
    )
    add(
        "30_speech_noise_mixes",
        "M04_female_alt_fan_snr_0.wav",
        [mix_at_snr(female_alt, fan, 0.0)],
        "Alternate female speech plus fan noise at 0 dB SNR.",
        "CTRL,NR,ENH,FOCUS",
        "Global RMS SNR 0 dB",
    )
    add(
        "30_speech_noise_mixes",
        "M05_female_hum_snr_p5.wav",
        [mix_at_snr(female, hum, 5.0)],
        "Female speech plus electrical hum at +5 dB SNR.",
        "CTRL,NR,ENH",
        "Global RMS SNR +5 dB",
    )
    add(
        "30_speech_noise_mixes",
        "M06_female_clicks_snr_p5.wav",
        [mix_at_snr(female, transients, 5.0)],
        "Female speech plus transient clicks at +5 dB SNR.",
        "CTRL,NR,ENH",
        "Global RMS SNR +5 dB",
    )
    add(
        "30_speech_noise_mixes",
        "M07_female_alt_music_snr_0.wav",
        [mix_at_snr(female_alt, music, 0.0)],
        "Alternate female speech plus synthetic music at 0 dB SNR.",
        "CTRL,ENH,FOCUS",
        "Global RMS SNR 0 dB",
    )
    female_reverb = room_reverb(female)
    add(
        "30_speech_noise_mixes",
        "M08_female_reverb_fan_snr_p5.wav",
        [mix_at_snr(female_reverb, fan, 5.0)],
        "Reverberant female speech plus fan noise.",
        "CTRL,NR,ENH,FOCUS",
        "Global RMS SNR +5 dB after reverb",
    )

    offset_female = delay(female, 0.42)
    competing = mix_at_snr(female_alt, offset_female, 0.0)
    add(
        "40_voice_focus",
        "F01_two_female_speakers_overlap.wav",
        [competing],
        "Two Portuguese female voices overlap at approximately equal RMS.",
        "CTRL,FOCUS",
        "Luciana begins 420 ms after Sandy",
    )
    add(
        "40_voice_focus",
        "F02_female_music_snr_m5.wav",
        [mix_at_snr(female, music, -5.0)],
        "Female speech under louder synthetic music.",
        "CTRL,FOCUS,ENH",
        "Global RMS speech-to-music SNR -5 dB",
    )

    voice_rms = rms(female)
    music_rms = rms(music)
    wide_factor = voice_rms / (music_rms * db_to_amp(-3.0))
    music_right = delay(music, 0.013)
    stereo = normalize_safely(
        [
            [s + n * wide_factor for s, n in zip(female, music)],
            [s + n * wide_factor for s, n in zip(female, music_right)],
        ]
    )
    add(
        "40_voice_focus",
        "F03_center_voice_wide_music_stereo.wav",
        stereo,
        "Identical center voice with decorrelated stereo music bed.",
        "CTRL,FOCUS",
        "Voice centered; music right channel delayed 13 ms",
    )

    stereo_separated = normalize_safely(
        [
            [x * 0.8 for x in female],
            repeat_to_length(fan, len(female)),
        ]
    )
    add(
        "40_voice_focus",
        "F04_voice_left_noise_right_stereo.wav",
        stereo_separated,
        "Voice isolated in left channel and fan noise in right channel.",
        "CTRL,FOCUS,NR",
        "Deliberately channel-separated sources",
    )
    add(
        "40_voice_focus",
        "F05_music_only_stereo.wav",
        [music, delay(music, 0.017)],
        "Stereo synthetic music without any voice; probes false voice detection.",
        "CTRL,FOCUS",
        "Right channel delayed 17 ms",
    )

    ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / "MANIFESTO_AUDIO.csv"
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)

    metadata = {
        "package": ROOT.name,
        "version": 3,
        "sample_rate_hz": FS,
        "bit_depth": BIT_DEPTH,
        "seed": SEED,
        "file_count": len(records),
        "speech_generation": {
            "female_voice": "Luciana (pt-BR), macOS local TTS",
            "alternate_female_voice": "Sandy (Português (Brasil)), macOS local TTS",
        },
    }
    (ROOT / "GERACAO.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    wav_paths = sorted(ROOT.rglob("*.wav"))
    checksum_path = ROOT / "SHA256SUMS.txt"
    with checksum_path.open("w", encoding="utf-8") as stream:
        for path in wav_paths:
            stream.write(f"{hash_file(path)}  {path.relative_to(ROOT)}\n")

    print(json.dumps(metadata, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
