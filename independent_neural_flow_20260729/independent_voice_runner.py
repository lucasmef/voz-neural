#!/usr/bin/env python3
"""Independent two-pass neural voice processor.

Runtime signal path:

    input
      -> RNNoise VAD (analysis only; no audio is taken from RNNoise)
      -> DeepFilterNet pass 1
      -> 95% pass 1 + 5% input
      -> DeepFilterNet pass 2
      -> 75% pass 2 + 25% first mix
      -> independently reconstructed Voice Focus EQ
      -> input-only active-voice leveling
      -> VAD/DFN-confidence residual reconstruction in non-speech only
      -> -0.30 dBFS limiter

No CapCut executable, runtime, model, cache, or generated stem is used.
DeepFilterNet is an independently redistributable MIT/Apache-2.0 project.
RNNoise is an independently redistributable BSD-3-Clause project.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

from runtime_dsp import (
    EPS,
    apply_limiter,
    apply_voice_focus_eq,
    best_placement_lag,
    db,
    frame_rms,
    mono,
    place_on_reference,
    read_pcm_wav,
    rms,
    sinc_resample,
    write_pcm24_wav,
)


@dataclass(frozen=True)
class FlowSettings:
    first_wet: float = 0.95
    second_wet: float = 0.75
    focus_pregain: float = 3.0
    frame_ms: float = 20.0
    edge_exclusion_ms: float = 300.0
    quiet_quantile: float = 0.25
    active_quantile: float = 0.65
    vad_residual_threshold: float = 0.10
    vad_high_speech_threshold: float = 0.70
    vad_hangover_ms: float = 50.0
    vad_hangover_voice_floor: float = 0.15
    vad_mix_at_015: float = 0.10
    vad_mix_at_040: float = 0.45
    vad_mix_at_070: float = 0.80
    vad_mix_at_090: float = 1.00
    residual_minimum_frames: int = 16
    residual_suppression_min_db: float = 2.5
    residual_suppression_max_db: float = 6.0
    residual_stationarity_intercept_db: float = 11.0
    residual_stationarity_slope: float = 1.2
    residual_dfn_confidence_scale: float = 0.45
    residual_neural_weight: float = 0.65
    residual_transition_ms: float = 20.0
    low_suppression_voice_floor: float = 0.15
    target_active_median_dbfs: float = -15.50
    final_ceiling_dbfs: float = -0.30
    limiter_release_ms: float = 500.0
    max_final_peak_boost_db: float = 0.0


DEFAULT_VAD_BINARY = HERE / "bin/rnnoise_vad_arm64"
DEFAULT_DEEP_FILTER_BINARY = (
    HERE / "bin/deep-filter-0.5.6-aarch64-apple-darwin"
)


def render_output_channels(
    final: np.ndarray,
    force_mono: bool = False,
) -> np.ndarray:
    """Return the delivery layout without changing the processed samples.

    Premiere's default stereo pan law can attenuate a centered mono clip by
    about 3.01 dB.  The normal delivery is therefore stereo dual-mono: the
    same processed sample is written independently to left and right.
    ``--mono`` remains available for workflows that explicitly need one
    channel.
    """

    processed = np.asarray(final, dtype=np.float32)
    if processed.ndim != 1:
        raise ValueError("render_output_channels expects one mono channel")
    channels = 1 if force_mono else 2
    return np.repeat(processed[:, None], channels, axis=1)


def write_pcm16_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    samples = np.asarray(audio, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    clipped = np.clip(samples, -1.0, np.nextafter(1.0, 0.0))
    integer = np.clip(
        np.rint(clipped * 32768.0), -32768, 32767
    ).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(samples.shape[1])
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(integer.tobytes())


def smooth_control(control: np.ndarray, window_samples: int) -> np.ndarray:
    values = np.asarray(control, dtype=np.float64)
    if window_samples <= 1 or values.size < 2:
        return values.copy()
    window_samples = min(window_samples, values.size)
    left = window_samples // 2
    right = window_samples - 1 - left
    padded = np.pad(values, (left, right), mode="edge")
    cumulative = np.concatenate(([0.0], np.cumsum(padded, dtype=np.float64)))
    return (cumulative[window_samples:] - cumulative[:-window_samples]) / (
        window_samples
    )


def frame_control_to_samples(
    frame_values: np.ndarray,
    frame_size: int,
    sample_count: int,
    smoothing_samples: int,
) -> np.ndarray:
    control = np.repeat(np.asarray(frame_values, dtype=np.float64), frame_size)
    if control.size < sample_count:
        control = np.pad(control, (0, sample_count - control.size), mode="edge")
    control = control[:sample_count]
    return smooth_control(control, smoothing_samples)


def frame_masks(
    raw: np.ndarray,
    sample_rate: int,
    settings: FlowSettings,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    frame_size = max(1, int(round(settings.frame_ms * sample_rate / 1000.0)))
    envelope = frame_rms(raw, frame_size)
    eligible = np.ones(envelope.size, dtype=bool)
    edge_frames = int(
        round(settings.edge_exclusion_ms / max(settings.frame_ms, EPS))
    )
    if envelope.size > 2 * edge_frames:
        eligible[:edge_frames] = False
        eligible[-edge_frames:] = False
    if not np.any(eligible):
        eligible[:] = True
    values = envelope[eligible]
    quiet_threshold = float(np.quantile(values, settings.quiet_quantile))
    active_threshold = float(np.quantile(values, settings.active_quantile))
    quiet = eligible & (envelope <= quiet_threshold)
    active = eligible & (envelope >= active_threshold)
    diagnostics = {
        "frame_size": frame_size,
        "frames": int(envelope.size),
        "eligible_frames": int(np.count_nonzero(eligible)),
        "quiet_frames": int(np.count_nonzero(quiet)),
        "active_frames": int(np.count_nonzero(active)),
        "quiet_threshold_dbfs": db(quiet_threshold),
        "active_threshold_dbfs": db(active_threshold),
        "raw_quiet_median_dbfs": db(float(np.median(envelope[quiet]))),
        "raw_active_median_dbfs": db(float(np.median(envelope[active]))),
    }
    return envelope, quiet, active, diagnostics


def align_neural_output(
    reference: np.ndarray,
    neural: np.ndarray,
    sample_rate: int,
) -> tuple[np.ndarray, int, float]:
    lag, correlation = best_placement_lag(
        reference,
        neural,
        max_lag=int(round(0.100 * sample_rate)),
    )
    aligned = place_on_reference(
        reference,
        neural,
        lag,
        edge_fallback=True,
    )
    return aligned, lag, correlation


def run_deep_filter(
    audio: np.ndarray,
    sample_rate: int,
    deep_filter_binary: Path,
    temporary: Path,
    label: str,
) -> tuple[np.ndarray, dict[str, float | int | str]]:
    if sample_rate != 48000:
        raise ValueError("DeepFilterNet stage expects a 48 kHz working signal")
    source_path = temporary / f"{label}.wav"
    output_directory = temporary / f"{label}_output"
    output_directory.mkdir(parents=True, exist_ok=True)
    write_pcm16_wav(source_path, audio, sample_rate)
    completed = subprocess.run(
        [
            str(deep_filter_binary),
            "-D",
            "-o",
            str(output_directory),
            str(source_path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"DeepFilterNet failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    enhanced_path = output_directory / source_path.name
    enhanced_wav = read_pcm_wav(enhanced_path)
    enhanced = mono(enhanced_wav.samples)
    aligned, lag, correlation = align_neural_output(audio, enhanced, sample_rate)
    return aligned, {
        "source_frames": int(audio.size),
        "neural_frames": int(enhanced.size),
        "lag_samples": lag,
        "lag_ms": 1000.0 * lag / sample_rate,
        "alignment_correlation": correlation,
        "binary": str(deep_filter_binary),
    }


def run_rnnoise_vad(
    raw: np.ndarray,
    sample_rate: int,
    vad_binary: Path,
    temporary: Path,
) -> tuple[np.ndarray, dict[str, float | int | str]]:
    """Return one raw-derived speech probability per 10 ms.

    RNNoise receives the raw signal only to estimate probability.  Its
    denoised audio output is deliberately discarded.
    """

    if sample_rate != 48000:
        raise ValueError("RNNoise VAD stage expects a 48 kHz working signal")
    if not vad_binary.exists():
        raise FileNotFoundError(vad_binary)
    source = temporary / "rnnoise_vad_input.f32"
    output = temporary / "rnnoise_vad_probabilities.f32"
    np.asarray(raw, dtype="<f4").tofile(source)
    completed = subprocess.run(
        [str(vad_binary), str(source), str(output)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            f"RNNoise VAD failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    probabilities = np.fromfile(output, dtype="<f4").astype(np.float64)
    expected = int(math.ceil(raw.size / 480.0))
    if probabilities.size != expected:
        raise RuntimeError(
            "RNNoise VAD returned "
            f"{probabilities.size} frames; expected {expected}"
        )
    return probabilities, {
        "binary": str(vad_binary),
        "frame_size": 480,
        "frames": int(probabilities.size),
        "minimum_probability": float(np.min(probabilities)),
        "median_probability": float(np.median(probabilities)),
        "maximum_probability": float(np.max(probabilities)),
    }


def vad_voice_mix(
    probabilities: np.ndarray,
    settings: FlowSettings,
) -> tuple[np.ndarray, np.ndarray, dict[str, float | int]]:
    """Build a continuous residual-to-voice mix from raw-only VAD.

    A binary gate over-boosts breaths and onsets.  These piecewise points keep
    VAD transition frames between the residual and full voice paths.
    """

    probabilities = np.asarray(probabilities, dtype=np.float64)
    voice_mix = np.interp(
        probabilities,
        np.array([0.0, 0.10, 0.15, 0.40, 0.70, 0.90, 1.0]),
        np.array(
            [
                0.0,
                0.0,
                settings.vad_mix_at_015,
                settings.vad_mix_at_040,
                settings.vad_mix_at_070,
                settings.vad_mix_at_090,
                1.0,
            ]
        ),
    )
    high_speech = probabilities >= settings.vad_high_speech_threshold
    hangover_frames = int(round(settings.vad_hangover_ms / 10.0))
    near_high_speech = high_speech.copy()
    if hangover_frames > 0 and high_speech.size:
        kernel = np.ones(2 * hangover_frames + 1, dtype=np.int32)
        near_high_speech = (
            np.convolve(
                high_speech.astype(np.int32), kernel, mode="same"
            )
            > 0
        )
        voice_mix[near_high_speech] = np.maximum(
            voice_mix[near_high_speech],
            settings.vad_hangover_voice_floor,
        )
    residual_analysis = (
        (probabilities < settings.vad_residual_threshold)
        & ~near_high_speech
    )
    return voice_mix, residual_analysis, {
        "residual_threshold": settings.vad_residual_threshold,
        "high_speech_threshold": settings.vad_high_speech_threshold,
        "hangover_ms_each_side": settings.vad_hangover_ms,
        "hangover_voice_floor": settings.vad_hangover_voice_floor,
        "high_speech_frames": int(np.count_nonzero(high_speech)),
        "residual_analysis_frames": int(
            np.count_nonzero(residual_analysis)
        ),
        "voice_mix_mean": float(np.mean(voice_mix)),
        "voice_mix_p10": float(np.quantile(voice_mix, 0.10)),
        "voice_mix_median": float(np.median(voice_mix)),
        "voice_mix_p90": float(np.quantile(voice_mix, 0.90)),
    }


def adaptive_voice_level(
    processed: np.ndarray,
    raw_active: np.ndarray,
    frame_size: int,
    settings: FlowSettings,
) -> tuple[np.ndarray, dict[str, float]]:
    envelope = frame_rms(processed, frame_size)
    count = min(envelope.size, raw_active.size)
    active = raw_active[:count]
    if not np.any(active):
        active = np.ones(count, dtype=bool)
    active_median = float(np.median(envelope[:count][active]))
    target = 10.0 ** (settings.target_active_median_dbfs / 20.0)
    active_gain = target / max(active_median, EPS)
    levelled = processed * active_gain
    peak = float(np.max(np.abs(levelled)))
    ceiling = 10.0 ** (settings.final_ceiling_dbfs / 20.0)
    peak_boost = min(
        max(ceiling / max(peak, EPS), 1.0),
        10.0 ** (settings.max_final_peak_boost_db / 20.0),
    )
    levelled *= peak_boost
    levelled_envelope = frame_rms(levelled, frame_size)[:count]
    return levelled.astype(np.float64), {
        "active_median_before_dbfs": db(active_median),
        "requested_active_target_dbfs": settings.target_active_median_dbfs,
        "active_gain_db": db(active_gain),
        "peak_completion_gain_db": db(peak_boost),
        "total_static_gain_db": db(active_gain * peak_boost),
        "active_median_after_static_gain_dbfs": db(
            float(np.median(levelled_envelope[active]))
        ),
        "levelled_rms_dbfs": db(rms(levelled)),
        "levelled_peak_dbfs": db(float(np.max(np.abs(levelled)))),
    }


def adaptive_residual_control(
    raw: np.ndarray,
    dfn_first: np.ndarray,
    voice_levelled: np.ndarray,
    voice_mix: np.ndarray,
    residual_analysis: np.ndarray,
    sample_rate: int,
    settings: FlowSettings,
) -> tuple[np.ndarray, dict[str, float | int | str]]:
    """Reconstruct only confidently non-speech residual.

    Speech and weak-speech transition frames are untouched.  In sufficiently
    long non-speech regions, the attenuation is inferred from two input-only
    observations: temporal stationarity of the raw bed and how confidently
    the first DeepFilterNet pass separates that bed.
    """

    frame_size = int(round(0.010 * sample_rate))
    raw_envelope = frame_rms(raw, frame_size)
    dfn_envelope = frame_rms(dfn_first, frame_size)
    processed_envelope = frame_rms(voice_levelled, frame_size)
    count = min(
        raw_envelope.size,
        dfn_envelope.size,
        processed_envelope.size,
        residual_analysis.size,
    )
    selected = residual_analysis[:count]
    selected_count = int(np.count_nonzero(selected))
    diagnostics: dict[str, float | int | str] = {
        "mode": "vad_soft_mix_default_residual",
        "frame_size": frame_size,
        "non_speech_frames": selected_count,
        "controlled_frames": 0,
    }
    suppression_db = settings.residual_suppression_min_db
    raw_p10_dbfs = db(float(np.quantile(raw_envelope, 0.10)))
    raw_p90_dbfs = db(float(np.quantile(raw_envelope, 0.90)))
    stationarity_spread_db = raw_p90_dbfs - raw_p10_dbfs
    stationarity_suppression_db = suppression_db
    dfn_suppression_db = suppression_db
    raw_median = float(np.median(raw_envelope))
    dfn_median = float(np.median(dfn_envelope))
    dfn_attenuation_db = max(db(raw_median) - db(dfn_median), 0.0)
    if selected_count >= settings.residual_minimum_frames:
        raw_selected = raw_envelope[:count][selected]
        dfn_selected = dfn_envelope[:count][selected]
        raw_db = 20.0 * np.log10(np.maximum(raw_selected, EPS))
        finite_floor = raw_db[raw_db > -110.0]
        distribution = finite_floor if finite_floor.size >= 8 else raw_db
        raw_p10_dbfs = float(np.quantile(distribution, 0.10))
        raw_p90_dbfs = float(np.quantile(distribution, 0.90))
        stationarity_spread_db = raw_p90_dbfs - raw_p10_dbfs
        stationarity_suppression_db = float(
            np.clip(
                settings.residual_stationarity_intercept_db
                - settings.residual_stationarity_slope
                * stationarity_spread_db,
                settings.residual_suppression_min_db,
                settings.residual_suppression_max_db,
            )
        )
        raw_median = float(np.median(raw_selected))
        dfn_median = float(np.median(dfn_selected))
        dfn_attenuation_db = max(
            db(raw_median) - db(dfn_median), 0.0
        )
        dfn_suppression_db = float(
            np.clip(
                settings.residual_dfn_confidence_scale
                * dfn_attenuation_db,
                settings.residual_suppression_min_db,
                settings.residual_suppression_max_db,
            )
        )
        suppression_db = max(
            stationarity_suppression_db,
            dfn_suppression_db,
        )
    suppression = 10.0 ** (-suppression_db / 20.0)
    scene_voice_floor = settings.low_suppression_voice_floor * float(
        np.clip((4.0 - suppression_db) / 1.5, 0.0, 1.0)
    )
    if scene_voice_floor > 0.0:
        voice_mix = np.maximum(voice_mix, scene_voice_floor)

    base = (
        settings.residual_neural_weight * dfn_first
        + (1.0 - settings.residual_neural_weight) * raw
    )
    base = apply_voice_focus_eq(base, sample_rate)
    base_envelope = frame_rms(base, frame_size)
    desired_envelope = suppression * raw_envelope
    residual_gain = desired_envelope / np.maximum(base_envelope, EPS)
    # The dry component keeps the denominator stable; the bound is a final
    # guard against amplifying numerical tails.
    residual_gain = np.clip(residual_gain, 0.0, 4.0)
    gain_samples = frame_control_to_samples(
        residual_gain,
        frame_size,
        raw.size,
        1,
    )
    residual = base * gain_samples
    smoothing = int(
        round(settings.residual_transition_ms * sample_rate / 1000.0)
    )
    voice_mix_samples = frame_control_to_samples(
        voice_mix,
        frame_size,
        raw.size,
        smoothing,
    )
    output = (
        (1.0 - voice_mix_samples) * residual
        + voice_mix_samples * voice_levelled
    )
    output_envelope = frame_rms(output, frame_size)
    diagnostics.update(
        {
            "mode": "vad_continuous_raw_dfn_residual_to_voice",
            "controlled_frames": selected_count,
            "raw_non_speech_p10_dbfs": raw_p10_dbfs,
            "raw_non_speech_p90_dbfs": raw_p90_dbfs,
            "raw_non_speech_stationarity_spread_db": (
                stationarity_spread_db
            ),
            "raw_non_speech_median_dbfs": db(raw_median),
            "dfn1_non_speech_median_dbfs": db(dfn_median),
            "dfn1_attenuation_db": dfn_attenuation_db,
            "stationarity_suppression_db": stationarity_suppression_db,
            "dfn_confidence_suppression_db": dfn_suppression_db,
            "selected_suppression_db": suppression_db,
            "scene_voice_floor": scene_voice_floor,
            "voice_mix_mean": float(np.mean(voice_mix)),
            "processed_non_speech_median_before_dbfs": (
                db(float(np.median(processed_envelope[:count][selected])))
                if selected_count
                else -120.0
            ),
            "residual_non_speech_median_after_dbfs": (
                db(float(np.median(output_envelope[:count][selected])))
                if selected_count
                else -120.0
            ),
        }
    )
    return output, diagnostics


def process_array(
    raw: np.ndarray,
    sample_rate: int,
    deep_filter_binary: Path,
    temporary: Path,
    settings: FlowSettings | None = None,
    vad_binary: Path = DEFAULT_VAD_BINARY,
) -> tuple[np.ndarray, dict[str, object]]:
    settings = settings or FlowSettings()
    source = np.asarray(raw, dtype=np.float32)
    if source.ndim != 1:
        raise ValueError("process_array expects one mono channel")
    if sample_rate != 48000:
        working = sinc_resample(source, sample_rate, 48000)
    else:
        working = source.copy()
    working_rate = 48000
    _, quiet, active, selection = frame_masks(
        working, working_rate, settings
    )
    frame_size = int(selection["frame_size"])
    vad_probabilities, vad_diagnostics = run_rnnoise_vad(
        working,
        working_rate,
        vad_binary,
        temporary,
    )
    voice_mix, residual_analysis, vad_mask_diagnostics = vad_voice_mix(
        vad_probabilities,
        settings,
    )
    vad_diagnostics.update(vad_mask_diagnostics)

    first, first_diagnostics = run_deep_filter(
        working,
        working_rate,
        deep_filter_binary,
        temporary,
        "dfn_pass1",
    )
    first_mix = settings.first_wet * first + (1.0 - settings.first_wet) * working
    second, second_diagnostics = run_deep_filter(
        first_mix,
        working_rate,
        deep_filter_binary,
        temporary,
        "dfn_pass2",
    )
    second_mix = (
        settings.second_wet * second
        + (1.0 - settings.second_wet) * first_mix
    )

    focused = apply_voice_focus_eq(second_mix, working_rate)
    focused_pre = settings.focus_pregain * focused
    voice_levelled, level_diagnostics = adaptive_voice_level(
        focused_pre,
        active,
        frame_size,
        settings,
    )
    residual_controlled, residual_diagnostics = adaptive_residual_control(
        working,
        first,
        voice_levelled,
        voice_mix,
        residual_analysis,
        working_rate,
        settings,
    )
    final_working = apply_limiter(
        residual_controlled,
        working_rate,
        ceiling_db=settings.final_ceiling_dbfs,
        release_ms=settings.limiter_release_ms,
    )
    final_working = final_working.astype(np.float32)
    final_envelope = frame_rms(final_working, frame_size)
    level_diagnostics.update(
        {
            "active_median_after_final_limiter_dbfs": db(
                float(np.median(final_envelope[: active.size][active]))
            ),
            "final_rms_dbfs": db(rms(final_working)),
            "final_peak_dbfs": db(
                float(np.max(np.abs(final_working)))
            ),
        }
    )
    if sample_rate != working_rate:
        final = sinc_resample(
            final_working,
            working_rate,
            sample_rate,
            output_frames=source.size,
        )
    else:
        final = final_working
    diagnostics: dict[str, object] = {
        "independent_runtime": True,
        "uses_capcut_runtime_or_model": False,
        "settings": asdict(settings),
        "input": {
            "sample_rate": sample_rate,
            "frames": int(source.size),
            "rms_dbfs": db(rms(source)),
            "peak_dbfs": db(float(np.max(np.abs(source)))),
        },
        "selection": selection,
        "rnnoise_vad": vad_diagnostics,
        "dfn_pass1": first_diagnostics,
        "dfn_pass2": second_diagnostics,
        "residual_control": residual_diagnostics,
        "adaptive_level": level_diagnostics,
        "output": {
            "sample_rate": sample_rate,
            "frames": int(final.size),
            "rms_dbfs": db(rms(final)),
            "peak_dbfs": db(float(np.max(np.abs(final)))),
        },
    }
    return final.astype(np.float32), diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--deep-filter-bin",
        type=Path,
        default=DEFAULT_DEEP_FILTER_BINARY,
    )
    parser.add_argument(
        "--rnnoise-vad-bin",
        type=Path,
        default=DEFAULT_VAD_BINARY,
    )
    parser.add_argument(
        "--mono",
        action="store_true",
        help=(
            "Write one-channel mono instead of the default Premiere-safe "
            "stereo dual-mono delivery."
        ),
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="Defaults to OUTPUT.json.",
    )
    arguments = parser.parse_args()
    input_path = arguments.input.resolve()
    output_path = arguments.output.resolve()
    deep_filter_binary = arguments.deep_filter_bin.resolve()
    vad_binary = arguments.rnnoise_vad_bin.resolve()
    if not deep_filter_binary.exists():
        raise FileNotFoundError(deep_filter_binary)
    if not vad_binary.exists():
        raise FileNotFoundError(vad_binary)
    source_wav = read_pcm_wav(input_path)
    source = mono(source_wav.samples)
    with tempfile.TemporaryDirectory(prefix="independent_voice_") as temporary:
        final, diagnostics = process_array(
            source,
            source_wav.sample_rate,
            deep_filter_binary,
            Path(temporary),
            vad_binary=vad_binary,
        )
    rendered = render_output_channels(final, force_mono=arguments.mono)
    output_channels = int(rendered.shape[1])
    output_layout = "mono" if arguments.mono else "dual_mono_stereo"
    write_pcm24_wav(output_path, rendered, source_wav.sample_rate)
    diagnostics["input"]["file_channels"] = source_wav.channels
    diagnostics["output"].update(
        {
            "file_channels": output_channels,
            "channel_layout": output_layout,
            "channels_are_sample_identical": output_channels == 2,
        }
    )
    diagnostics["files"] = {
        "input": str(input_path),
        "output": str(output_path),
    }
    diagnostics_path = (
        arguments.diagnostics.resolve()
        if arguments.diagnostics
        else output_path.with_suffix(".json")
    )
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "diagnostics": str(diagnostics_path),
                "input_rms_dbfs": diagnostics["input"]["rms_dbfs"],
                "output_rms_dbfs": diagnostics["output"]["rms_dbfs"],
                "output_peak_dbfs": diagnostics["output"]["peak_dbfs"],
                "output_channels": output_channels,
                "output_channel_layout": output_layout,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
