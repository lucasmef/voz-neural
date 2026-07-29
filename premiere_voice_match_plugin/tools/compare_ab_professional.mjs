#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const workspace = path.dirname(root);
const analysisDir = path.join(root, "ab_pair_65", "analysis_professional");
const sampleRate = 48000;
const frameSize = 480;
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
  if (!values.length) return Number.NaN;
  const sorted = [...values].sort((a, b) => a - b);
  const position = fraction * (sorted.length - 1);
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  return (
    sorted[lower] * (upper - position) +
    sorted[upper] * (position - lower)
  );
}

function median(values) {
  return percentile(values, 0.5);
}

function rmsDb(samples) {
  let energy = 0;
  for (const sample of samples) energy += sample * sample;
  return db(Math.sqrt(energy / samples.length + epsilon));
}

function peakDb(samples) {
  let peak = 0;
  for (const sample of samples) peak = Math.max(peak, Math.abs(sample));
  return db(peak);
}

function frameLevels(samples) {
  const count = Math.floor(samples.length / frameSize);
  const levels = new Array(count);
  for (let frame = 0; frame < count; frame += 1) {
    let energy = 0;
    const start = frame * frameSize;
    for (let index = 0; index < frameSize; index += 1) {
      const sample = samples[start + index];
      energy += sample * sample;
    }
    levels[frame] = db(Math.sqrt(energy / frameSize + epsilon));
  }
  return levels;
}

function correlation(a, b) {
  const length = Math.min(a.length, b.length);
  let sumA = 0;
  let sumB = 0;
  for (let index = 0; index < length; index += 1) {
    sumA += a[index];
    sumB += b[index];
  }
  const meanA = sumA / length;
  const meanB = sumB / length;
  let cross = 0;
  let energyA = 0;
  let energyB = 0;
  for (let index = 0; index < length; index += 1) {
    const da = a[index] - meanA;
    const dbValue = b[index] - meanB;
    cross += da * dbValue;
    energyA += da * da;
    energyB += dbValue * dbValue;
  }
  return cross / Math.sqrt(energyA * energyB + epsilon);
}

function closeShortGaps(mask, maximumGap) {
  const result = [...mask];
  let index = 0;
  while (index < result.length) {
    if (result[index]) {
      index += 1;
      continue;
    }
    const start = index;
    while (index < result.length && !result[index]) index += 1;
    if (
      start > 0 &&
      index < result.length &&
      index - start <= maximumGap
    ) {
      for (let fill = start; fill < index; fill += 1) result[fill] = true;
    }
  }
  return result;
}

function tailMetrics(rawLevels, outputLevels, activeThreshold) {
  const initialMask = rawLevels.map((level) => level >= activeThreshold);
  const speechMask = closeShortGaps(initialMask, 18);
  const events = [];
  for (let index = 10; index < speechMask.length - 85; index += 1) {
    if (!speechMask[index - 1] || speechMask[index]) continue;
    let clear = true;
    for (let lookahead = 0; lookahead < 80; lookahead += 1) {
      if (speechMask[index + lookahead]) {
        clear = false;
        break;
      }
    }
    if (clear) events.push(index);
  }

  function windowMedian(event, startFrame, endFrame) {
    return median(outputLevels.slice(event + startFrame, event + endFrame));
  }

  const early = events.map((event) => windowMedian(event, 0, 10));
  const middle = events.map((event) => windowMedian(event, 10, 30));
  const late = events.map((event) => windowMedian(event, 50, 80));
  return {
    event_count: events.length,
    tail_0_100ms_dbfs: median(early),
    tail_100_300ms_dbfs: median(middle),
    floor_500_800ms_dbfs: median(late),
    early_tail_over_late_floor_db: median(early) - median(late),
  };
}

const raw = readFloat32(
  path.join(workspace, "analysis_capcut_pairs", "decoded", "raw", "65.f32"),
);
const capcut = readFloat32(path.join(analysisDir, "capcut.f32"));
const plugin = readFloat32(path.join(analysisDir, "plugin_v03.f32"));
const rawLevels = frameLevels(raw);
const activeThreshold = Math.max(-50, percentile(rawLevels, 0.55));
const quietThreshold = Math.min(-55, percentile(rawLevels, 0.25));

function analyze(name, output) {
  const outputLevels = frameLevels(output);
  const count = Math.min(rawLevels.length, outputLevels.length);
  const active = [];
  const quiet = [];
  const rawActive = [];
  const outputActive = [];
  for (let index = 0; index < count; index += 1) {
    if (rawLevels[index] >= activeThreshold) {
      active.push(outputLevels[index]);
      rawActive.push(rawLevels[index]);
      outputActive.push(outputLevels[index]);
    }
    if (rawLevels[index] <= quietThreshold) quiet.push(outputLevels[index]);
  }
  return {
    name,
    duration_s: output.length / sampleRate,
    rms_dbfs: rmsDb(output),
    peak_dbfs: peakDb(output),
    crest_db: peakDb(output) - rmsDb(output),
    active_median_dbfs: median(active),
    active_p90_dbfs: percentile(active, 0.9),
    quiet_median_dbfs: median(quiet),
    quiet_p25_dbfs: percentile(quiet, 0.25),
    active_to_quiet_contrast_db: median(active) - median(quiet),
    envelope_correlation_to_raw: correlation(rawActive, outputActive),
    ...tailMetrics(rawLevels.slice(0, count), outputLevels.slice(0, count), activeThreshold),
  };
}

const result = {
  source: "pair_65",
  sample_rate_hz: sampleRate,
  frame_ms: 10,
  active_threshold_raw_dbfs: activeThreshold,
  quiet_threshold_raw_dbfs: quietThreshold,
  exports: [analyze("CapCut", capcut), analyze("Plugin_0.3", plugin)],
};

fs.writeFileSync(
  path.join(analysisDir, "professional_comparison.json"),
  `${JSON.stringify(result, null, 2)}\n`,
);
console.log(JSON.stringify(result, null, 2));
