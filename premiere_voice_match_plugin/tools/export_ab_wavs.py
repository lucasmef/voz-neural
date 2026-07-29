#!/usr/bin/env python3
"""Export representative raw/plugin/CapCut comparisons as mono PCM24 WAV."""

import argparse
import json
import wave
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "premiere_voice_match_plugin"
PAIRS = ROOT / "analysis_capcut_pairs"
DEFAULT_OUTPUT = PLUGIN / "comparison_ab_dynamic_v02"
DEFAULT_PLUGIN_OUTPUTS = PLUGIN / "analysis_cli"
DEFAULT_PAIR_IDS = ("64", "69", "82")


def load_f32(path):
    return np.fromfile(path, dtype="<f4")


def write_pcm24(path, samples, sample_rate):
    samples = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    values = np.rint(samples * 8_388_607.0).astype("<i4")
    packed = values.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        output.writeframes(packed)


def resample_linear(samples, source_rate, target_rate):
    length = int(round(len(samples) * target_rate / source_rate))
    positions = np.arange(length, dtype=np.float64) * source_rate / target_rate
    return np.interp(positions, np.arange(len(samples)), samples)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--plugin-dir", type=Path, default=DEFAULT_PLUGIN_OUTPUTS)
    parser.add_argument("--latency", type=int, default=1200)
    parser.add_argument("--pairs", nargs="+", default=DEFAULT_PAIR_IDS)
    parser.add_argument("--plugin-label", default="PLUGIN_DINAMICO")
    args = parser.parse_args()
    output_directory = args.output_dir.resolve()
    plugin_directory = args.plugin_dir.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)

    analysis = json.loads((PAIRS / "ANALYSIS_RESULT.json").read_text())
    by_id = {row["pair_id"]: row for row in analysis["pairs"]}
    manifest = []

    for pair_id in args.pairs:
        row = by_id[pair_id]
        raw = load_f32(ROOT / row["raw_source"])
        plugin_full = load_f32(plugin_directory / f"{pair_id}.f32")
        plugin = plugin_full[args.latency : args.latency + len(raw)]
        capcut_44100 = load_f32(PAIRS / "decoded" / "treated" / f"{pair_id}.f32")
        capcut_48000 = resample_linear(capcut_44100, 44_100, 48_000)

        raw_path = output_directory / f"{pair_id}_A_BRUTO.wav"
        plugin_path = output_directory / f"{pair_id}_B_{args.plugin_label}.wav"
        capcut_path = output_directory / f"{pair_id}_C_CAPCUT.wav"
        write_pcm24(raw_path, raw, 48_000)
        write_pcm24(plugin_path, plugin, 48_000)
        write_pcm24(capcut_path, capcut_48000, 48_000)

        manifest.append(
            {
                "pair_id": pair_id,
                "raw_source": row["raw_source"],
                "raw_duration_s": round(len(raw) / 48_000, 3),
                "plugin_duration_s": round(len(plugin) / 48_000, 3),
                "capcut_duration_s": round(len(capcut_48000) / 48_000, 3),
                "files": [raw_path.name, plugin_path.name, capcut_path.name],
            }
        )

    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
