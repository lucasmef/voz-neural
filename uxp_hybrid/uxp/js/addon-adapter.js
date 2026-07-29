"use strict";

const { VoiceNeuralError } = require("./errors");

function parseAddonResponse(raw, operation) {
  let response = raw;
  if (typeof raw === "string") {
    try {
      response = JSON.parse(raw);
    } catch (error) {
      throw new VoiceNeuralError(
        `O componente local respondeu de forma inválida durante ${operation}.`,
        "ADDON_INVALID_JSON",
        { operation, raw }
      );
    }
  }

  if (!response || typeof response !== "object" || Array.isArray(response)) {
    throw new VoiceNeuralError(
      `O componente local não confirmou ${operation}.`,
      "ADDON_INVALID_RESPONSE",
      { operation, response }
    );
  }

  if (response.ok === false || response.success === false || response.error) {
    const message =
      (response.error && (response.error.message || response.error)) ||
      response.message ||
      "O processamento local não pôde ser concluído.";
    throw new VoiceNeuralError(
      String(message),
      (response.error && response.error.code) ||
        response.code ||
        "ADDON_OPERATION_FAILED",
      response
    );
  }

  return response;
}

class AddonAdapter {
  constructor(loader = () => require("voz-neural.uxpaddon")) {
    this.loader = loader;
    this.addon = null;
    this.loadPromise = null;
  }

  async load() {
    if (this.addon) return this.addon;
    if (this.loadPromise) return this.loadPromise;

    this.loadPromise = Promise.resolve()
      .then(() => this.loader())
      .then(addon => {
        for (const method of [
          "getRuntimeInfo",
          "probeAudioTopology",
          "processFile",
          "cancel"
        ]) {
          if (!addon || typeof addon[method] !== "function") {
            throw new VoiceNeuralError(
              `O componente instalado está incompleto (${method}).`,
              "ADDON_METHOD_MISSING",
              { method }
            );
          }
        }

        this.addon = addon;
        return addon;
      })
      .catch(error => {
        this.loadPromise = null;
        if (error instanceof VoiceNeuralError) throw error;
        throw new VoiceNeuralError(
          "O componente de áudio não foi carregado. Reinstale o Voz Neural " +
            "para este modelo de Mac.",
          "ADDON_LOAD_FAILED",
          error
        );
      });

    return this.loadPromise;
  }

  async getRuntimeInfo() {
    const addon = await this.load();
    let raw;
    try {
      raw = await Promise.resolve(addon.getRuntimeInfo());
    } catch (error) {
      throw new VoiceNeuralError(
        "Não foi possível verificar o mecanismo de áudio local.",
        "ADDON_RUNTIME_INFO_FAILED",
        error
      );
    }

    const info = parseAddonResponse(raw, "a verificação");
    if (info.ready === false) {
      throw new VoiceNeuralError(
        info.message || "O mecanismo de áudio ainda não está pronto.",
        info.code || "RUNTIME_NOT_READY",
        info
      );
    }
    return info;
  }

  async probeAudioTopology(inputPath) {
    if (!inputPath || typeof inputPath !== "string") {
      throw new VoiceNeuralError(
        "O Premiere não informou o arquivo que será analisado.",
        "AUDIO_TOPOLOGY_PATH_MISSING"
      );
    }

    const addon = await this.load();
    let raw;
    try {
      raw = await Promise.resolve(addon.probeAudioTopology(inputPath));
    } catch (error) {
      throw new VoiceNeuralError(
        "Não foi possível identificar os canais do áudio original.",
        "AUDIO_TOPOLOGY_PROBE_THROW",
        error
      );
    }

    const topology = parseAddonResponse(raw, "a análise dos canais");
    return {
      ...topology,
      channels: Number(topology.channels),
      sampleRate: Number(topology.sampleRate),
      audioTrackCount: Number(topology.audioTrackCount)
    };
  }

  async processFile(job) {
    const addon = await this.load();
    let raw;
    try {
      raw = await Promise.resolve(addon.processFile(JSON.stringify(job)));
    } catch (error) {
      throw new VoiceNeuralError(
        `Não foi possível tratar “${job.clipName || "o clipe"}”.`,
        "ADDON_PROCESS_THROW",
        error
      );
    }

    const response = parseAddonResponse(raw, "o processamento");
    const outputPath = response.outputPath || response.output || job.outputPath;
    if (!outputPath || typeof outputPath !== "string") {
      throw new VoiceNeuralError(
        "O componente terminou sem informar o arquivo tratado.",
        "ADDON_OUTPUT_MISSING",
        response
      );
    }

    return { ...response, outputPath };
  }

  async cancel(requestId) {
    if (!this.addon && !this.loadPromise) {
      return { ok: true, cancelled: false };
    }
    const addon = await this.load();
    let raw;
    try {
      raw = await Promise.resolve(
        addon.cancel(JSON.stringify({ requestId }))
      );
    } catch (firstError) {
      try {
        raw = await Promise.resolve(addon.cancel(requestId));
      } catch (secondError) {
        throw new VoiceNeuralError(
          "Não foi possível cancelar o processamento atual.",
          "ADDON_CANCEL_FAILED",
          secondError
        );
      }
    }
    return parseAddonResponse(raw, "o cancelamento");
  }
}

module.exports = {
  AddonAdapter,
  parseAddonResponse
};
