setExpectedRuntime(15, "minutes");

const ROOT =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111";
const OUTPUT_DIR =
  ROOT + "/adobe_podcast_v2_test_20260728/soundlift_uxp_output";
const REPORT_PATH = OUTPUT_DIR + "/soundlift_report.json";
const FILE_PREFIX = "file://";
const EXPECTED_BUSES = [
  "Speech",
  "Music",
  "Sfx",
  "Noise",
  "Reverb",
  "Background",
];
const FILTER_CANDIDATES = [
  "Elevação de som",
  "Sound Lift",
  "SoundLift",
  "Sound Lift...",
  "$$$/dvaaudiofilters/SoundLiftTitle",
];
const TESTS = [
  {
    id: "S01",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
      "S01_female_clean_ptbr.wav",
  },
  {
    id: "S03",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
      "S03_female_level_steps.wav",
  },
  {
    id: "S06",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
      "S06_female_alt_room_reverb.wav",
  },
  {
    id: "M04",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/30_speech_noise_mixes/" +
      "M04_female_alt_fan_snr_0.wav",
  },
  {
    id: "F02",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/40_voice_focus/" +
      "F02_female_music_snr_m5.wav",
  },
];

function canonicalBusName(name: string, index: number, count: number): string {
  const normalized = String(name || "")
    .toLowerCase()
    .replace(/[áàâãä]/g, "a")
    .replace(/[éèêë]/g, "e")
    .replace(/[íìîï]/g, "i")
    .replace(/[óòôõö]/g, "o")
    .replace(/[úùûü]/g, "u")
    .replace(/ç/g, "c")
    .replace(/[^a-z0-9]+/g, "");
  const aliases: Record<string, string> = {
    speech: "Speech",
    fala: "Speech",
    voice: "Speech",
    voz: "Speech",
    music: "Music",
    musica: "Music",
    sfx: "Sfx",
    soundeffects: "Sfx",
    efeitossonoros: "Sfx",
    noise: "Noise",
    ruido: "Noise",
    reverb: "Reverb",
    reverberation: "Reverb",
    reverberacao: "Reverb",
    background: "Background",
    ambience: "Background",
    ambient: "Background",
    fundo: "Background",
    ambiente: "Background",
  };
  if (aliases[normalized]) return aliases[normalized];
  if (count === EXPECTED_BUSES.length) return EXPECTED_BUSES[index];
  return "Bus" + (index + 1);
}

describe("Sound Lift batch stems", () => {
  it("renders the five female calibration clips", async () => {
    const uxp = require("uxp");
    const fs = require("fs");
    const audio = uxp.dvaaudiofilters;
    const report: any = {
      startedAt: new Date().toISOString(),
      completedAt: null,
      filter: null,
      preset: null,
      outputBuses: [],
      results: [],
      error: null,
    };

    function writeReport() {
      fs.writeFileSync(
        FILE_PREFIX + REPORT_PATH,
        JSON.stringify(report, null, 2)
      );
    }

    try {
      try {
        fs.readdirSync(FILE_PREFIX + OUTPUT_DIR);
      } catch (_error) {
        await fs.mkdir(FILE_PREFIX + OUTPUT_DIR);
      }

      expect(audio).toBeDefined();
      expect(audio.AudioFilterProcessor).toBeDefined();
      expect(audio.WaveFile).toBeDefined();

      const probeInput = audio.WaveFile(TESTS[0].input);
      let selection: any = null;
      for (const filter of FILTER_CANDIDATES) {
        try {
          const presets =
            await audio.AudioFilterProcessor.getPresetsForAudioFilter(filter);
          const supported =
            await audio.AudioFilterProcessor
              .isAudioChannelLayoutSupportedByAudioFilter(
                probeInput.channelLayout,
                filter
              );
          if (!supported || !Array.isArray(presets) || !presets.length) {
            continue;
          }
          const runtimeBuses =
            await audio.AudioFilterProcessor.getOutputBusNames(
              filter,
              probeInput.channelLayout
            );
          if (!Array.isArray(runtimeBuses) || !runtimeBuses.length) {
            continue;
          }
          selection = {
            filter,
            preset:
              presets.find(
                (name: string) =>
                  name === "Version 1" || name === "Versão 1"
              ) || presets[0],
            buses: runtimeBuses.map((runtimeName: string, index: number) => ({
              runtimeName,
              canonicalName: canonicalBusName(
                runtimeName,
                index,
                runtimeBuses.length
              ),
            })),
          };
          break;
        } catch (_error) {
          // Try the next localized/internal filter identifier.
        }
      }

      expect(selection).not.toBeNull();
      const required = ["Speech", "Noise", "Reverb", "Background"];
      const available = selection.buses.map(
        (bus: any) => bus.canonicalName
      );
      for (const name of required) {
        expect(available).toContain(name);
      }

      report.filter = selection.filter;
      report.preset = selection.preset;
      report.outputBuses = selection.buses;
      writeReport();

      for (const test of TESTS) {
        const input = audio.WaveFile(test.input);
        const outputDescriptors = selection.buses.map((bus: any) => ({
          runtimeName: bus.runtimeName,
          canonicalName: bus.canonicalName,
          path:
            OUTPUT_DIR +
            "/" +
            test.id +
            "_" +
            bus.canonicalName +
            ".wav",
        }));
        const outputs = outputDescriptors.map((output: any) =>
          audio.WaveFile(output.path)
        );
        const processResult = await audio.AudioFilterProcessor.process(
          selection.filter,
          selection.preset,
          input,
          outputs
        );

        for (const output of outputDescriptors) {
          const stats = await fs.lstat(FILE_PREFIX + output.path);
          expect(stats.size).toBeGreaterThan(44);
          output.byteSize = stats.size;
        }

        report.results.push({
          id: test.id,
          input: test.input,
          outputs: outputDescriptors,
          processResult,
          sampleRate: input.sampleRate,
          sampleCount: input.sampleCount,
          bitDepth: input.bitDepth,
        });
        writeReport();
      }

      report.completedAt = new Date().toISOString();
      writeReport();
      expect(report.results.length).toBe(TESTS.length);
    } catch (error) {
      report.error = String(error && error.stack ? error.stack : error);
      try {
        writeReport();
      } catch (_writeError) {
        // The test assertion below will still expose the original error.
      }
      throw error;
    }
  });
});
