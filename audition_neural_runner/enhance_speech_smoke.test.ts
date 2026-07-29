setExpectedRuntime(10, "minutes");

const ROOT =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111";
const INPUT =
  ROOT +
  "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
  "S01_female_clean_ptbr.wav";
const OUTPUT_DIR = ROOT + "/audition_neural_runner/output";
const OUTPUT = OUTPUT_DIR + "/S01_EnhanceSpeech_100.wav";
const REPORT = OUTPUT_DIR + "/S01_EnhanceSpeech_report.json";
const FILE_PREFIX = "file://";

describe("Audition Enhance Speech smoke test", () => {
  it("processes one female speech clip and writes a non-empty WAV", async () => {
    const uxp = require("uxp");
    const fs = require("fs");
    const audio = uxp.dvaaudiofilters;
    const report: any = {
      startedAt: new Date().toISOString(),
      input: INPUT,
      output: OUTPUT,
      filter: "Enhance Speech",
      presets: [],
      outputBuses: [],
      processResult: null,
      byteSize: 0,
      completedAt: null,
      error: null,
    };

    function writeReport() {
      fs.writeFileSync(
        FILE_PREFIX + REPORT,
        JSON.stringify(report, null, 2)
      );
    }

    try {
      expect(audio).toBeDefined();
      expect(audio.AudioFilterProcessor).toBeDefined();
      expect(audio.WaveFile).toBeDefined();

      const input = audio.WaveFile(INPUT);
      report.presets =
        await audio.AudioFilterProcessor.getPresetsForAudioFilter(
          report.filter
        );
      expect(Array.isArray(report.presets)).toBeTrue();
      expect(report.presets.length).toBeGreaterThan(0);

      report.outputBuses =
        await audio.AudioFilterProcessor.getOutputBusNames(
          report.filter,
          input.channelLayout
        );
      const supported =
        await audio.AudioFilterProcessor
          .isAudioChannelLayoutSupportedByAudioFilter(
            input.channelLayout,
            report.filter
          );
      expect(supported).toBeTrue();
      writeReport();

      const output = audio.WaveFile(OUTPUT);
      report.processResult =
        await audio.AudioFilterProcessor.process(
          report.filter,
          report.presets[0],
          input,
          [output]
        );

      const stats = await fs.lstat(FILE_PREFIX + OUTPUT);
      report.byteSize = stats.size;
      report.completedAt = new Date().toISOString();
      writeReport();
      expect(report.byteSize).toBeGreaterThan(44);
    } catch (error) {
      report.error = String(error && error.stack ? error.stack : error);
      try {
        writeReport();
      } catch (_writeError) {
        // The test runner will still record the original failure.
      }
      throw error;
    }
  });
});
