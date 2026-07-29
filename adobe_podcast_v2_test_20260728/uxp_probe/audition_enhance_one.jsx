var ROOT =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111";
var outputDirPath =
  ROOT + "/adobe_podcast_v2_test_20260728/audition_neural_output";
var inputPath =
  ROOT +
  "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
  "S01_female_clean_ptbr.wav";
var enhancedPath = outputDirPath + "/S01_EnhanceSpeech_100.wav";
var reportPath = outputDirPath + "/S01_EnhanceSpeech_report.txt";
var outputDir = new Folder(outputDirPath);
if (!outputDir.exists) outputDir.create();
var report = new File(reportPath);

function log(text) {
  if (report.open("a")) {
    report.writeln(text);
    report.close();
  }
}

if (report.open("w")) {
  report.writeln("started=" + String(new Date()));
  report.close();
}

try {
  var input = new WaveFile(inputPath);
  var enhanced = new WaveFile(enhancedPath);
  log("input.sampleRate=" + input.sampleRate);
  log("input.sampleCount=" + input.sampleCount);
  log(
    "presets=" +
      AudioFilterProcessor
        .getPresetsForAudioFilter("Enhance Speech")
        .join(",")
  );
  var result = AudioFilterProcessor.process(
    "Enhance Speech",
    "(Default)",
    input,
    [enhanced]
  );
  log("result=" + result);
  log("completed=" + String(new Date()));
} catch (error) {
  log("error=" + error);
  if (error && error.stack) log("stack=" + error.stack);
}
