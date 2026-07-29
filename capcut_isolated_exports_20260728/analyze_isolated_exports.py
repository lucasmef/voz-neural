#!/usr/bin/env python3
"""Analyze the isolated CapCut audio exports without decoding dependencies."""

from __future__ import annotations

import csv
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


SR = 44_100
CHANNELS = 2
CHUNK_FRAMES = 750_000


@dataclass
class WavMap:
    path: Path
    sample_rate: int
    channels: int
    frames: int
    samples: np.memmap


def open_pcm16_wav(path: Path) -> WavMap:
    with path.open("rb") as stream:
        if stream.read(4) != b"RIFF":
            raise ValueError(f"{path}: not RIFF")
        stream.seek(8)
        if stream.read(4) != b"WAVE":
            raise ValueError(f"{path}: not WAVE")

        fmt = None
        data_offset = None
        data_size = None
        while True:
            chunk_id = stream.read(4)
            if len(chunk_id) != 4:
                break
            chunk_size_raw = stream.read(4)
            if len(chunk_size_raw) != 4:
                break
            chunk_size = struct.unpack("<I", chunk_size_raw)[0]
            payload_offset = stream.tell()
            if chunk_id == b"fmt ":
                payload = stream.read(chunk_size)
                audio_format, channels, sample_rate, _, block_align, bits = struct.unpack(
                    "<HHIIHH", payload[:16]
                )
                fmt = (audio_format, channels, sample_rate, block_align, bits)
            elif chunk_id == b"data":
                data_offset = payload_offset
                data_size = chunk_size
                break
            else:
                stream.seek(chunk_size, 1)
            if chunk_size & 1:
                stream.seek(1, 1)

    if fmt is None or data_offset is None or data_size is None:
        raise ValueError(f"{path}: missing fmt/data chunk")
    audio_format, channels, sample_rate, block_align, bits = fmt
    if audio_format != 1 or bits != 16:
        raise ValueError(f"{path}: expected PCM16, got format={audio_format}, bits={bits}")
    frames = data_size // block_align
    samples = np.memmap(
        path,
        mode="r",
        dtype="<i2",
        offset=data_offset,
        shape=(frames, channels),
    )
    return WavMap(path, sample_rate, channels, frames, samples)


def db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-30))


def rms_peak(wav: WavMap, start: int = 0, stop: int | None = None) -> tuple[float, float]:
    stop = wav.frames if stop is None else min(stop, wav.frames)
    sum_sq = 0.0
    peak = 0.0
    count = 0
    for pos in range(start, stop, CHUNK_FRAMES):
        part = np.asarray(wav.samples[pos : min(pos + CHUNK_FRAMES, stop)], dtype=np.float64)
        sum_sq += float(np.sum(part * part))
        peak = max(peak, float(np.max(np.abs(part), initial=0.0)))
        count += part.size
    return math.sqrt(sum_sq / max(count, 1)), peak


def relation(
    source: WavMap,
    target: WavMap,
    start: int = 0,
    stop: int | None = None,
) -> dict[str, float]:
    """Fit target ~= gain * source and report cosine correlation and residual."""
    stop = min(source.frames, target.frames) if stop is None else min(
        stop, source.frames, target.frames
    )
    ss = tt = st = 0.0
    count = 0
    for pos in range(start, stop, CHUNK_FRAMES):
        end = min(pos + CHUNK_FRAMES, stop)
        x = np.asarray(source.samples[pos:end], dtype=np.float64)
        y = np.asarray(target.samples[pos:end], dtype=np.float64)
        ss += float(np.sum(x * x))
        tt += float(np.sum(y * y))
        st += float(np.sum(x * y))
        count += x.size
    gain = st / max(ss, 1e-30)
    residual_sq = max(tt - 2.0 * gain * st + gain * gain * ss, 0.0)
    target_rms = math.sqrt(tt / max(count, 1))
    residual_rms = math.sqrt(residual_sq / max(count, 1))
    return {
        "gain_linear": gain,
        "gain_db": db(abs(gain)),
        "cosine_correlation": st / math.sqrt(max(ss * tt, 1e-30)),
        "target_rms_dbfs": db(target_rms / 32768.0),
        "residual_after_gain_dbfs": db(residual_rms / 32768.0),
        "residual_relative_to_target_db": db(residual_rms / max(target_rms, 1e-30)),
    }


def mix_test(
    y0: WavMap,
    y100: WavMap,
    yp: WavMap,
    slider: int,
    start: int = 0,
    stop: int | None = None,
) -> dict[str, float]:
    stop = min(y0.frames, y100.frames, yp.frames) if stop is None else min(
        stop, y0.frames, y100.frames, yp.frames
    )
    expected = 1.0 - slider / 100.0
    bb = bd = dd = pp = yy = py = err2 = 0.0
    count = within_half = within_one = within_two = 0
    for pos in range(start, stop, CHUNK_FRAMES):
        end = min(pos + CHUNK_FRAMES, stop)
        a = np.asarray(y0.samples[pos:end], dtype=np.float64)
        v = np.asarray(y100.samples[pos:end], dtype=np.float64)
        y = np.asarray(yp.samples[pos:end], dtype=np.float64)
        background = a - v
        delta = y - v
        pred = v + expected * background
        err = y - pred
        bb += float(np.sum(background * background))
        bd += float(np.sum(background * delta))
        dd += float(np.sum(delta * delta))
        pp += float(np.sum(pred * pred))
        yy += float(np.sum(y * y))
        py += float(np.sum(pred * y))
        err2 += float(np.sum(err * err))
        absolute = np.abs(err)
        within_half += int(np.count_nonzero(absolute <= 0.5))
        within_one += int(np.count_nonzero(absolute <= 1.0))
        within_two += int(np.count_nonzero(absolute <= 2.0))
        count += err.size

    fitted = bd / max(bb, 1e-30)
    fitted_residual_sq = max(dd - 2.0 * fitted * bd + fitted * fitted * bb, 0.0)
    error_rms = math.sqrt(err2 / max(count, 1))
    output_rms = math.sqrt(yy / max(count, 1))
    return {
        "slider_percent": slider,
        "expected_background_factor": expected,
        "fitted_background_factor": fitted,
        "factor_error": fitted - expected,
        "prediction_error_rms_lsb": error_rms,
        "prediction_error_dbfs": db(error_rms / 32768.0),
        "prediction_error_relative_to_output_db": db(
            error_rms / max(output_rms, 1e-30)
        ),
        "prediction_cosine_correlation": py / math.sqrt(max(pp * yy, 1e-30)),
        "samples_within_half_lsb_fraction": within_half / max(count, 1),
        "samples_within_one_lsb_fraction": within_one / max(count, 1),
        "samples_within_two_lsb_fraction": within_two / max(count, 1),
        "fitted_residual_rms_lsb": math.sqrt(fitted_residual_sq / max(count, 1)),
    }


def mix_test_below_peak(
    y0: WavMap,
    y100: WavMap,
    yp: WavMap,
    slider: int,
    peak_fraction: float,
    start: int = 0,
    stop: int | None = None,
) -> dict[str, float]:
    """Test the same mix law away from samples affected by output limiting."""
    stop = min(y0.frames, y100.frames, yp.frames) if stop is None else min(
        stop, y0.frames, y100.frames, yp.frames
    )
    expected = 1.0 - slider / 100.0
    threshold = peak_fraction * 32768.0
    bb = bd = dd = pp = yy = py = err2 = 0.0
    count = total = 0
    for pos in range(start, stop, CHUNK_FRAMES):
        end = min(pos + CHUNK_FRAMES, stop)
        a = np.asarray(y0.samples[pos:end], dtype=np.float64)
        v = np.asarray(y100.samples[pos:end], dtype=np.float64)
        y = np.asarray(yp.samples[pos:end], dtype=np.float64)
        background = a - v
        pred = v + expected * background
        mask = (
            (np.abs(a) < threshold)
            & (np.abs(v) < threshold)
            & (np.abs(y) < threshold)
            & (np.abs(pred) < threshold)
            & (np.abs(background) > 2.0)
        )
        total += mask.size
        if not np.any(mask):
            continue
        background = background[mask]
        delta = (y - v)[mask]
        pred = pred[mask]
        y = y[mask]
        err = y - pred
        bb += float(np.sum(background * background))
        bd += float(np.sum(background * delta))
        dd += float(np.sum(delta * delta))
        pp += float(np.sum(pred * pred))
        yy += float(np.sum(y * y))
        py += float(np.sum(pred * y))
        err2 += float(np.sum(err * err))
        count += err.size

    fitted = bd / max(bb, 1e-30)
    fitted_residual_sq = max(dd - 2.0 * fitted * bd + fitted * fitted * bb, 0.0)
    error_rms = math.sqrt(err2 / max(count, 1))
    output_rms = math.sqrt(yy / max(count, 1))
    return {
        "slider_percent": slider,
        "expected_background_factor": expected,
        "fitted_background_factor": fitted,
        "factor_error": fitted - expected,
        "prediction_error_dbfs": db(error_rms / 32768.0),
        "prediction_error_relative_to_output_db": db(
            error_rms / max(output_rms, 1e-30)
        ),
        "prediction_cosine_correlation": py / math.sqrt(max(pp * yy, 1e-30)),
        "fitted_residual_dbfs": db(
            math.sqrt(fitted_residual_sq / max(count, 1)) / 32768.0
        ),
        "selected_sample_fraction": count / max(total, 1),
        "peak_fraction": peak_fraction,
    }


def residual_stats(
    y0: WavMap,
    y100: WavMap,
    raw: WavMap,
    start: int = 0,
    stop: int | None = None,
) -> dict[str, float]:
    stop = min(y0.frames, y100.frames, raw.frames) if stop is None else min(
        stop, y0.frames, y100.frames, raw.frames
    )
    bb = vv = rr = br = vr = bv = 0.0
    count = 0
    peak_b = 0.0
    for pos in range(start, stop, CHUNK_FRAMES):
        end = min(pos + CHUNK_FRAMES, stop)
        a = np.asarray(y0.samples[pos:end], dtype=np.float64)
        v = np.asarray(y100.samples[pos:end], dtype=np.float64)
        r = np.asarray(raw.samples[pos:end], dtype=np.float64)
        b = a - v
        bb += float(np.sum(b * b))
        vv += float(np.sum(v * v))
        rr += float(np.sum(r * r))
        br += float(np.sum(b * r))
        vr += float(np.sum(v * r))
        bv += float(np.sum(b * v))
        peak_b = max(peak_b, float(np.max(np.abs(b), initial=0.0)))
        count += b.size
    rms_b = math.sqrt(bb / max(count, 1))
    rms_v = math.sqrt(vv / max(count, 1))
    rms_r = math.sqrt(rr / max(count, 1))
    return {
        "background_rms_dbfs": db(rms_b / 32768.0),
        "voice100_rms_dbfs": db(rms_v / 32768.0),
        "raw_rms_dbfs": db(rms_r / 32768.0),
        "background_relative_to_voice_db": db(rms_b / max(rms_v, 1e-30)),
        "background_relative_to_raw_db": db(rms_b / max(rms_r, 1e-30)),
        "background_peak_dbfs": db(peak_b / 32768.0),
        "background_raw_cosine_correlation": br / math.sqrt(max(bb * rr, 1e-30)),
        "voice100_raw_cosine_correlation": vr / math.sqrt(max(vv * rr, 1e-30)),
        "background_voice_cosine_correlation": bv / math.sqrt(max(bb * vv, 1e-30)),
    }


def load_timecodes(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            start = round(int(row["inicio_us"]) * SR / 1_000_000)
            stop = round((int(row["inicio_us"]) + int(row["duracao_us"])) * SR / 1_000_000)
            rows.append(
                {
                    "order": int(row["ordem"]),
                    "category": row["categoria"],
                    "file": row["arquivo"],
                    "start_frame": start,
                    "stop_frame": stop,
                }
            )
    return rows


def main() -> int:
    base = Path(__file__).resolve().parent
    root = base.parent
    files = {
        "raw": base / "CAPCUT_RAW_CONTROL_ALL_OFF.WAV",
        "enh0": base / "CAPCUT_ENHANCE_ONLY_0.WAV",
        "enh25": base / "CAPCUT_ENHANCE_ONLY_25.WAV",
        "enh50": base / "CAPCUT_ENHANCE_ONLY_50.WAV",
        "enh75": base / "CAPCUT_ENHANCE_ONLY_75.WAV",
        "enh100": base / "CAPCUT_ENHANCE_ONLY_100.WAV",
        "denoise": base / "CAPCUT_DENOISE_ONLY_085.WAV",
        "focus": base / "CAPCUT_FOCUS_ONLY_100.WAV",
        "full": base / "CAPCUT_FULL_CHAIN_75_RECHECK.WAV",
    }
    wavs = {name: open_pcm16_wav(path) for name, path in files.items()}
    formats = {
        (wav.sample_rate, wav.channels, wav.frames) for wav in wavs.values()
    }
    if len(formats) != 1:
        raise ValueError(f"mismatched formats: {formats}")

    summary: dict[str, object] = {
        "format": {
            "sample_rate": wavs["raw"].sample_rate,
            "channels": wavs["raw"].channels,
            "frames": wavs["raw"].frames,
            "duration_seconds": wavs["raw"].frames / wavs["raw"].sample_rate,
        },
        "mix_linearity_global": {
            str(p): mix_test(
                wavs["enh0"], wavs["enh100"], wavs[f"enh{p}"], p
            )
            for p in (25, 50, 75)
        },
        "mix_linearity_below_minus_6_dbfs": {
            str(p): mix_test_below_peak(
                wavs["enh0"],
                wavs["enh100"],
                wavs[f"enh{p}"],
                p,
                0.5,
            )
            for p in (25, 50, 75)
        },
        "endpoint_relations_to_raw": {
            "enhance_0_vs_raw": relation(wavs["raw"], wavs["enh0"]),
            "enhance_100_vs_raw": relation(wavs["raw"], wavs["enh100"]),
        },
        "endpoint_difference_diagnostic_not_a_stem": residual_stats(
            wavs["enh0"], wavs["enh100"], wavs["raw"]
        ),
        "isolated_stages_vs_raw": {
            "denoise_only": relation(wavs["raw"], wavs["denoise"]),
            "focus_only": relation(wavs["raw"], wavs["focus"]),
            "enhance_only_75": relation(wavs["raw"], wavs["enh75"]),
            "full_chain_75": relation(wavs["raw"], wavs["full"]),
        },
        "levels": {},
        "interpretation_notes": [
            "Enhancement exports at different slider values are separate renders and may not reuse an identical neural realization.",
            "Therefore enhance_0 minus enhance_100 is a diagnostic endpoint difference, not a valid isolated background stem.",
            "The local mixer law is verified on stable non-vocal signals and independently in the native disassembly.",
        ],
    }
    for name, wav in wavs.items():
        rms, peak = rms_peak(wav)
        summary["levels"][name] = {
            "rms_dbfs": db(rms / 32768.0),
            "peak_dbfs": db(peak / 32768.0),
        }

    timecodes = load_timecodes(
        root / "capcut_project_audio_female_30_clips" / "MAPA_TIMECODES.csv"
    )
    clip_rows: list[dict[str, object]] = []
    for clip in timecodes:
        start = int(clip["start_frame"])
        stop = int(clip["stop_frame"])
        row: dict[str, object] = dict(clip)
        row["mix_25"] = mix_test(
            wavs["enh0"], wavs["enh100"], wavs["enh25"], 25, start, stop
        )
        row["mix_50"] = mix_test(
            wavs["enh0"], wavs["enh100"], wavs["enh50"], 50, start, stop
        )
        row["mix_75"] = mix_test(
            wavs["enh0"], wavs["enh100"], wavs["enh75"], 75, start, stop
        )
        row["mix_75_below_minus_6_dbfs"] = mix_test_below_peak(
            wavs["enh0"],
            wavs["enh100"],
            wavs["enh75"],
            75,
            0.5,
            start,
            stop,
        )
        row["residual"] = residual_stats(
            wavs["enh0"], wavs["enh100"], wavs["raw"], start, stop
        )
        row["enh0_vs_raw"] = relation(wavs["raw"], wavs["enh0"], start, stop)
        row["enh100_vs_raw"] = relation(wavs["raw"], wavs["enh100"], start, stop)
        row["levels"] = {}
        for name, wav in wavs.items():
            clip_rms, clip_peak = rms_peak(wav, start, stop)
            row["levels"][name] = {
                "rms_dbfs": db(clip_rms / 32768.0),
                "peak_dbfs": db(clip_peak / 32768.0),
            }
        raw_rms_dbfs = row["levels"]["raw"]["rms_dbfs"]
        row["rms_change_vs_raw_db"] = {
            name: values["rms_dbfs"] - raw_rms_dbfs
            for name, values in row["levels"].items()
            if name != "raw"
        }
        clip_rows.append(row)
    summary["per_clip"] = clip_rows
    categories = sorted({str(row["category"]) for row in clip_rows})
    summary["category_median_rms_change_vs_raw_db"] = {
        category: {
            name: float(
                np.median(
                    [
                        row["rms_change_vs_raw_db"][name]
                        for row in clip_rows
                        if row["category"] == category
                    ]
                )
            )
            for name in ("enh0", "enh25", "enh50", "enh75", "enh100", "denoise", "focus", "full")
        }
        for category in categories
    }

    output_json = base / "ISOLATED_ANALYSIS.json"
    output_json.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    output_csv = base / "ISOLATED_CLIP_METRICS.csv"
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        fields = [
            "order",
            "category",
            "file",
            "enh0_raw_corr",
            "enh100_raw_corr",
            "background_rms_dbfs",
            "background_raw_corr",
            "mix25_factor",
            "mix25_expected",
            "mix25_error_rel_db",
            "mix50_factor",
            "mix50_expected",
            "mix50_error_rel_db",
            "mix75_factor",
            "mix75_expected",
            "mix75_error_rel_db",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in clip_rows:
            writer.writerow(
                {
                    "order": row["order"],
                    "category": row["category"],
                    "file": row["file"],
                    "enh0_raw_corr": row["enh0_vs_raw"]["cosine_correlation"],
                    "enh100_raw_corr": row["enh100_vs_raw"]["cosine_correlation"],
                    "background_rms_dbfs": row["residual"]["background_rms_dbfs"],
                    "background_raw_corr": row["residual"][
                        "background_raw_cosine_correlation"
                    ],
                    "mix25_factor": row["mix_25"]["fitted_background_factor"],
                    "mix25_expected": row["mix_25"]["expected_background_factor"],
                    "mix25_error_rel_db": row["mix_25"][
                        "prediction_error_relative_to_output_db"
                    ],
                    "mix50_factor": row["mix_50"]["fitted_background_factor"],
                    "mix50_expected": row["mix_50"]["expected_background_factor"],
                    "mix50_error_rel_db": row["mix_50"][
                        "prediction_error_relative_to_output_db"
                    ],
                    "mix75_factor": row["mix_75"]["fitted_background_factor"],
                    "mix75_expected": row["mix_75"]["expected_background_factor"],
                    "mix75_error_rel_db": row["mix_75"][
                        "prediction_error_relative_to_output_db"
                    ],
                }
            )

    stage_csv = base / "ISOLATED_STAGE_METRICS.csv"
    with stage_csv.open("w", encoding="utf-8", newline="") as stream:
        fields = [
            "order",
            "category",
            "file",
            "raw_rms_dbfs",
            "enhance75_rms_dbfs",
            "enhance75_change_db",
            "denoise_rms_dbfs",
            "denoise_change_db",
            "focus_rms_dbfs",
            "focus_change_db",
            "full_rms_dbfs",
            "full_change_db",
            "full_peak_dbfs",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in clip_rows:
            writer.writerow(
                {
                    "order": row["order"],
                    "category": row["category"],
                    "file": row["file"],
                    "raw_rms_dbfs": row["levels"]["raw"]["rms_dbfs"],
                    "enhance75_rms_dbfs": row["levels"]["enh75"]["rms_dbfs"],
                    "enhance75_change_db": row["rms_change_vs_raw_db"]["enh75"],
                    "denoise_rms_dbfs": row["levels"]["denoise"]["rms_dbfs"],
                    "denoise_change_db": row["rms_change_vs_raw_db"]["denoise"],
                    "focus_rms_dbfs": row["levels"]["focus"]["rms_dbfs"],
                    "focus_change_db": row["rms_change_vs_raw_db"]["focus"],
                    "full_rms_dbfs": row["levels"]["full"]["rms_dbfs"],
                    "full_change_db": row["rms_change_vs_raw_db"]["full"],
                    "full_peak_dbfs": row["levels"]["full"]["peak_dbfs"],
                }
            )

    compact = {
        "format": summary["format"],
        "mix_linearity_global": summary["mix_linearity_global"],
        "mix_linearity_below_minus_6_dbfs": summary[
            "mix_linearity_below_minus_6_dbfs"
        ],
        "endpoint_relations_to_raw": summary["endpoint_relations_to_raw"],
        "endpoint_difference_diagnostic_not_a_stem": summary[
            "endpoint_difference_diagnostic_not_a_stem"
        ],
        "isolated_stages_vs_raw": summary["isolated_stages_vs_raw"],
        "levels": summary["levels"],
        "category_median_rms_change_vs_raw_db": summary[
            "category_median_rms_change_vs_raw_db"
        ],
    }
    print(json.dumps(compact, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
