"use strict";

class VoiceNeuralError extends Error {
  constructor(message, code = "VOICE_NEURAL_ERROR", details = null) {
    super(message);
    this.name = "VoiceNeuralError";
    this.code = code;
    this.details = details;
  }
}

function asVoiceNeuralError(error, fallbackMessage) {
  if (error instanceof VoiceNeuralError) return error;
  const message =
    error && typeof error.message === "string" && error.message.trim()
      ? error.message.trim()
      : fallbackMessage;
  return new VoiceNeuralError(message, "UNEXPECTED_ERROR", error || null);
}

module.exports = {
  VoiceNeuralError,
  asVoiceNeuralError
};
