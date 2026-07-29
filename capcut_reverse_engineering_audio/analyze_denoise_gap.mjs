#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const workspace = path.dirname(here);
const audioDir = path.join(here, "build", "blackbox_denoise");
const capcutCsv = path.join(
  workspace,
  "analysis_capcut_blackbox_combined",
  "CLIP_METRICS.csv",
);
const outputCsv = path.join(here, "DENOISE_V2_VS_CAPCUT.csv");
const outputJson = path.join(here, "DENOISE_V2_VS_CAPCUT_SUMMARY.json");
const epsilon = 1e-12;

function readFloat32(file) {
  const buffer = fs.readFileSync(file);
  return new Float32Array(
    buffer.buffer,
    buffer.byteOffset,
    Math.floor(buffer.byteLength / 4),
  );
}

function db(value) {
  return 20 * Math.log10(Math.max(value, epsilon));
}

function percentile(values, fraction) {
  if (values.length === 0) return Number.NaN;
  const sorted = [...values].sort((a, b) => a - b);
  const position = (sorted.length - 1) * fraction;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  const weight = position - lower;
  return sorted[lower] * (1 - weight) + sorted[upper] * weight;
}

function median(values) {
  return percentile(values, 0.5);
}

function frameLevels(samples, frameSize) {
  const count = Math.floor(samples.length / frameSize);
  const result = new Array(count);
  for (let frame = 0; frame < count; frame += 1) {
    let energy = 0;
    const start = frame * frameSize;
    for (let index = 0; index < frameSize; index += 1) {
      const sample = samples[start + index];
      energy += sample * sample;
    }
    result[frame] = db(Math.sqrt(energy / frameSize + epsilon));
  }
  return result;
}

function rmsDb(samples) {
  let energy = 0;
  for (const sample of samples) energy += sample * sample;
  return db(Math.sqrt(energy / samples.length + epsilon));
}

function metrics(raw, processed, rawRate, processedRate) {
  const rawFrames = frameLevels(raw, Math.round(rawRate * 0.02));
  const outputFrames = frameLevels(processed, Math.round(processedRate * 0.02));
  const frameCount = Math.min(rawFrames.length, outputFrames.length);
  const alignedRaw = rawFrames.slice(0, frameCount);
  const alignedOutput = outputFrames.slice(0, frameCount);
  const activeThreshold = percentile(alignedRaw, 0.6);
  const quietThreshold = percentile(alignedRaw, 0.2);
  const activeGains = [];
  const quietGains = [];
  const rawActive = [];
  const rawQuiet = [];
  const outputActive = [];
  const outputQuiet = [];
  for (let index = 0; index < frameCount; index += 1) {
    const gain = alignedOutput[index] - alignedRaw[index];
    if (alignedRaw[index] >= activeThreshold) {
      activeGains.push(gain);
      rawActive.push(alignedRaw[index]);
      outputActive.push(alignedOutput[index]);
    }
    if (alignedRaw[index] <= quietThreshold) {
      quietGains.push(gain);
      rawQuiet.push(alignedRaw[index]);
      outputQuiet.push(alignedOutput[index]);
    }
  }
  const activeGain = median(activeGains);
  const quietGain = median(quietGains);
  const rawContrast = median(rawActive) - median(rawQuiet);
  const outputContrast = median(outputActive) - median(outputQuiet);
  return {
    raw_rms_dbfs: rmsDb(raw),
    output_rms_dbfs: rmsDb(processed),
    global_gain_db: rmsDb(processed) - rmsDb(raw),
    active_gain_db: activeGain,
    quiet_gain_db: quietGain,
    quiet_relative_to_active_db: quietGain - activeGain,
    raw_active_quiet_contrast_db: rawContrast,
    output_active_quiet_contrast_db: outputContrast,
    contrast_improvement_db: outputContrast - rawContrast,
  };
}

function parseSimpleCsv(file) {
  const lines = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "").trim().split(/\r?\n/);
  const headers = lines.shift().split(",");
  return lines.map((line) => {
    const values = line.split(",");
    return Object.fromEntries(headers.map((header, index) => [header, values[index]]));
  });
}

function finiteMedian(rows, key) {
  return median(rows.map((row) => Number(row[key])).filter(Number.isFinite));
}

const capcutRows = parseSimpleCsv(capcutCsv);
const capcutById = new Map(
  capcutRows.map((row) => [row.arquivo.split("_", 1)[0], row]),
);
const ids = fs
  .readdirSync(audioDir)
  .filter((name) => name.endsWith("_input.f32"))
  .map((name) => name.slice(0, -"_input.f32".length))
  .sort();

const rows = ids.map((id) => {
  const raw = readFloat32(path.join(audioDir, `${id}_input.f32`));
  const denoised = readFloat32(path.join(audioDir, `${id}_denoise_v2.f32`));
  const capcutFull = readFloat32(path.join(audioDir, `${id}_capcut_full.f32`));
  const denoiseMetrics = metrics(raw, denoised, 48000, 48000);
  const capcutMetrics = metrics(raw, capcutFull, 48000, 44100);
  const capcut = capcutById.get(id);
  const category =
    id.startsWith("S") ? "speech" :
    id.startsWith("N") ? "noise_only" :
    id.startsWith("M") ? "speech_noise" :
    "voice_focus";
  const capcutQuietRelative = Number(capcut.quiet_relative_to_active_db);
  return {
    id,
    category,
    denoise_global_gain_db: denoiseMetrics.global_gain_db,
    denoise_active_gain_db: denoiseMetrics.active_gain_db,
    denoise_quiet_gain_db: denoiseMetrics.quiet_gain_db,
    denoise_quiet_relative_db: denoiseMetrics.quiet_relative_to_active_db,
    denoise_contrast_improvement_db: denoiseMetrics.contrast_improvement_db,
    capcut_global_gain_db: capcutMetrics.global_gain_db,
    capcut_active_gain_db: capcutMetrics.active_gain_db,
    capcut_quiet_gain_db: capcutMetrics.quiet_gain_db,
    capcut_quiet_relative_db: capcutMetrics.quiet_relative_to_active_db,
    capcut_contrast_improvement_db: capcutMetrics.contrast_improvement_db,
    extra_separation_after_denoise_db:
      capcutMetrics.contrast_improvement_db -
      denoiseMetrics.contrast_improvement_db,
  };
});

const headers = Object.keys(rows[0]);
const csv = [
  headers.join(","),
  ...rows.map((row) =>
    headers.map((header) => {
      const value = row[header];
      return typeof value === "number" && Number.isFinite(value)
        ? value.toFixed(6)
        : String(value);
    }).join(","),
  ),
].join("\n");
fs.writeFileSync(outputCsv, `${csv}\n`);

const groups = {};
for (const category of [...new Set(rows.map((row) => row.category))]) {
  const selected = rows.filter((row) => row.category === category);
  groups[category] = {
    count: selected.length,
    median_denoise_global_gain_db: finiteMedian(selected, "denoise_global_gain_db"),
    median_capcut_global_gain_db: finiteMedian(selected, "capcut_global_gain_db"),
    median_denoise_quiet_relative_db: finiteMedian(
      selected,
      "denoise_quiet_relative_db",
    ),
    median_capcut_quiet_relative_db: finiteMedian(
      selected,
      "capcut_quiet_relative_db",
    ),
    median_extra_separation_after_denoise_db: finiteMedian(
      selected,
      "extra_separation_after_denoise_db",
    ),
    median_denoise_contrast_improvement_db: finiteMedian(
      selected,
      "denoise_contrast_improvement_db",
    ),
    median_capcut_contrast_improvement_db: finiteMedian(
      selected,
      "capcut_contrast_improvement_db",
    ),
  };
}

const summary = {
  runtime: {
    model: "unet_denoise_44k_music_model_v1.0.model",
    denoise_rate: 0.85,
    denoise_mode: 1,
    input_rate_hz: 48000,
    internal_rate_hz: 44100,
    model_block_frames: 512,
    measured_latency_frames_at_48k: 1114,
  },
  groups,
};
fs.writeFileSync(outputJson, `${JSON.stringify(summary, null, 2)}\n`);
console.log(JSON.stringify(summary, null, 2));
