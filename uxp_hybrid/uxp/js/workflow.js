"use strict";

const { VoiceNeuralError, asVoiceNeuralError } = require("./errors");

const FIXED_PRESET = "capcut75-premiere-stereo-v1";
const MONO_COMPENSATION_MODE = "premiere_mono_center_v1";
const NO_COMPENSATION_MODE = "none";
const STEREO_PRESET_FILENAME = "Wave48stereo24.epr";

function safeFileStem(value) {
  const stem = String(value || "audio")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\.[a-z0-9]{1,8}$/i, "")
    .replace(/[^a-z0-9_-]+/gi, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 52);
  return stem || "audio";
}

function nativeSeparator(path) {
  return String(path).includes("\\") ? "\\" : "/";
}

function createRequestId(index) {
  return [
    "voz",
    Date.now().toString(36),
    index.toString(36),
    Math.random().toString(36).slice(2, 9)
  ].join("-");
}

function yieldForPaint() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function assertStereoExtractionPreset(presetPath) {
  if (!presetPath || typeof presetPath !== "string") {
    throw new VoiceNeuralError(
      "O componente local não informou o preset estéreo de extração.",
      "EXTRACTION_PRESET_MISSING"
    );
  }
  const normalized = presetPath.replace(/\\/g, "/");
  if (!normalized.toLocaleLowerCase().endsWith(
    `/${STEREO_PRESET_FILENAME.toLocaleLowerCase()}`
  )) {
    throw new VoiceNeuralError(
      "O preset de extração instalado não é o estéreo validado. " +
        "Reinstale o Voz Neural antes de continuar.",
      "STEREO_EXTRACTION_PRESET_REQUIRED",
      { presetPath }
    );
  }
  return presetPath;
}

function compensationModeForTopology(topology, clipName = "o clipe") {
  const audioTrackCount = Number(topology && topology.audioTrackCount);
  const channels = Number(topology && topology.channels);

  if (!Number.isInteger(audioTrackCount) || audioTrackCount !== 1) {
    throw new VoiceNeuralError(
      `“${clipName}” precisa ter exatamente uma trilha de áudio na mídia ` +
        "original. Renderize o clipe antes de continuar.",
      "UNSUPPORTED_AUDIO_TRACK_COUNT",
      { audioTrackCount, channels }
    );
  }
  if (channels === 1) return MONO_COMPENSATION_MODE;
  if (channels === 2) return NO_COMPENSATION_MODE;

  throw new VoiceNeuralError(
    `Não foi possível tratar “${clipName}”: a mídia original precisa ser ` +
      "mono ou estéreo. Renderize o clipe com 1 ou 2 canais.",
    "UNSUPPORTED_AUDIO_CHANNEL_COUNT",
    { audioTrackCount, channels }
  );
}

async function getOrCreateFolder(parent, name) {
  try {
    const existing = await parent.getEntry(name);
    if (existing && existing.isFolder) return existing;
  } catch (_) {
    // The first run is expected to reach createFolder.
  }
  return parent.createFolder(name);
}

class VoiceNeuralWorkflow {
  constructor({ premiere, addon, localFileSystem }) {
    this.premiere = premiere;
    this.addon = addon;
    this.localFileSystem = localFileSystem;
    this.running = false;
    this.activeRequestId = null;
  }

  async run(options = {}, observer = {}) {
    if (this.running) {
      throw new VoiceNeuralError(
        "Já existe um processamento em andamento.",
        "WORKFLOW_ALREADY_RUNNING"
      );
    }
    this.running = true;

    try {
      observer.onStage?.("selection");
      const runtimeInfo = await this.addon.getRuntimeInfo();
      const extractionPresetPath = assertStereoExtractionPreset(
        runtimeInfo.extractionPresetPath
      );
      const rawContext = await this.premiere.getSelectedAudioClips();
      const context = await this.premiere.prepareInsertionContext(rawContext);
      observer.onSelection?.(context.clips, context.ignoredCount);

      const dataFolder = await this.localFileSystem.getDataFolder();
      const dataPath = dataFolder && dataFolder.nativePath;
      if (!dataPath) {
        throw new VoiceNeuralError(
          "O painel não encontrou sua pasta local de trabalho.",
          "PLUGIN_DATA_PATH_MISSING"
        );
      }

      const separator = nativeSeparator(dataPath);
      const extractionFolder = await getOrCreateFolder(dataFolder, "extracted");
      if (!extractionFolder || !extractionFolder.nativePath) {
        throw new VoiceNeuralError(
          "O painel não conseguiu preparar a pasta de extração.",
          "EXTRACTION_DIRECTORY_MISSING"
        );
      }
      const extractionDirectory = extractionFolder.nativePath;
      const outputDirectory = `${dataPath}${separator}processed`;
      const processed = [];

      for (let index = 0; index < context.clips.length; index += 1) {
        const clip = context.clips[index];
        const requestId = createRequestId(index);
        this.activeRequestId = requestId;
        const outputName =
          `${safeFileStem(clip.clipName)}_voz_neural_` +
          `${requestId.slice(-7)}.wav`;
        const outputPath = `${outputDirectory}${separator}${outputName}`;
        const extractionName =
          `${safeFileStem(clip.clipName)}_entrada_` +
          `${requestId.slice(-7)}.wav`;
        const extractionPath =
          `${extractionDirectory}${separator}${extractionName}`;

        observer.onJobStart?.(clip, index, context.clips.length);
        await yieldForPaint();

        let response;
        try {
          observer.onJobStage?.(
            clip,
            index,
            context.clips.length,
            "probe"
          );
          const topology = await this.addon.probeAudioTopology(clip.inputPath);
          const inputCompensationMode = compensationModeForTopology(
            topology,
            clip.clipName
          );
          observer.onJobStage?.(
            clip,
            index,
            context.clips.length,
            "extract"
          );
          await this.premiere.extractClipToWav(
            clip,
            extractionPath,
            extractionPresetPath
          );
          observer.onJobStage?.(
            clip,
            index,
            context.clips.length,
            "process"
          );
          response = await this.addon.processFile({
            requestId,
            preset: FIXED_PRESET,
            inputPath: extractionPath,
            outputPath,
            inputCompensationMode,
            timelineStartSeconds: clip.timelineStartSeconds,
            timelineEndSeconds: clip.timelineEndSeconds,
            clipName: clip.clipName
          });
        } catch (error) {
          observer.onJobError?.(clip, index, error);
          throw error;
        }

        const result = { clip, ...response, outputPath: response.outputPath };
        processed.push(result);
        observer.onJobComplete?.(clip, index, context.clips.length, result);
      }

      this.activeRequestId = null;
      observer.onStage?.("import");
      const imported = await this.premiere.importProcessedFiles(
        context,
        processed
      );

      observer.onStage?.("insert");
      const insertion = await this.premiere.insertOnNewTracks(
        context,
        imported,
        Boolean(options.disableOriginals)
      );

      const summary = {
        clipCount: imported.length,
        ignoredCount: context.ignoredCount,
        laneCount: insertion.laneCount,
        originalsDisabled: Boolean(options.disableOriginals)
      };
      observer.onComplete?.(summary);
      return summary;
    } catch (error) {
      throw asVoiceNeuralError(
        error,
        "Não foi possível concluir o tratamento dos áudios."
      );
    } finally {
      this.activeRequestId = null;
      this.running = false;
    }
  }

  async cancel() {
    if (!this.running || !this.activeRequestId) {
      return { ok: true, cancelled: false };
    }
    return this.addon.cancel(this.activeRequestId);
  }
}

module.exports = {
  FIXED_PRESET,
  MONO_COMPENSATION_MODE,
  NO_COMPENSATION_MODE,
  STEREO_PRESET_FILENAME,
  VoiceNeuralWorkflow,
  assertStereoExtractionPreset,
  compensationModeForTopology,
  createRequestId,
  getOrCreateFolder,
  nativeSeparator,
  safeFileStem
};
