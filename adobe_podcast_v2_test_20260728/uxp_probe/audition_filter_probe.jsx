var ROOT =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111";
var inputPath =
  ROOT +
  "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
  "S01_female_clean_ptbr.wav";
var reportPath =
  ROOT +
  "/adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_filter_probe.txt";
var output = new File(reportPath);

function stringify(value) {
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  if (value instanceof Array) return "[" + value.join(", ") + "]";
  return String(value);
}

function line(name, value) {
  output.writeln(name + "=" + stringify(value));
}

if (output.open("w")) {
  try {
    var input = new WaveFile(inputPath);
    line("input.filePath", input.filePath);
    line("input.sampleCount", input.sampleCount);
    line("input.sampleRate", input.sampleRate);
    line("input.bitDepth", input.bitDepth);
    line(
      "enhance.presets",
      AudioFilterProcessor.getPresetsForAudioFilter(
        "Enhance Speech"
      )
    );
    line(
      "enhance.supported",
      AudioFilterProcessor
        .isAudioChannelLayoutSupportedByAudioFilter(
          input.channelLayout,
          "Enhance Speech"
        )
    );
    line(
      "soundlift.presets",
      AudioFilterProcessor.getPresetsForAudioFilter(
        "Sound Lift"
      )
    );
    line(
      "soundlift.supported",
      AudioFilterProcessor
        .isAudioChannelLayoutSupportedByAudioFilter(
          input.channelLayout,
          "Sound Lift"
        )
    );
    line(
      "beta.enhance",
      BetaFeature.isFeatureEnabled(
        "Audition.EnhanceSpeech"
      )
    );
    line(
      "beta.web",
      BetaFeature.isFeatureEnabled(
        "Audition.EnhanceSpeechWebService"
      )
    );
    line(
      "beta.internalAllowed",
      BetaFeature.isEntitlementSetToInternalFeaturesAllowed
    );
  } catch (error) {
    line("error", error);
    if (error && error.stack) line("stack", error.stack);
  }
  output.close();
}
