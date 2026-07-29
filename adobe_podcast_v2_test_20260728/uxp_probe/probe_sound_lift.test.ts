setExpectedRuntime(3, "minutes");

describe("Sound Lift discovery", () => {
  it("queries the installed multi-output audio filter", async () => {
    const uxp = require("uxp");
    const fs = require("fs");
    const audio = uxp.dvaaudiofilters;
    const inputNative =
      "/Users/lucas/.codex/.chatgpt-projects/" +
      "g-p-6a67e7a43cb8819180b9733ac6523111/" +
      "capcut_isolated_exports_20260728/" +
      "CAPCUT_RAW_CONTROL_ALL_OFF.WAV";
    const reportNative =
      "/Users/lucas/.codex/.chatgpt-projects/" +
      "g-p-6a67e7a43cb8819180b9733ac6523111/" +
      "adobe_podcast_v2_test_20260728/" +
      "uxp_probe/sound_lift_probe.json";
    const report: any = {
      hasNamespace: Boolean(audio),
      candidates: [],
      input: {},
    };

    let input: any = null;
    try {
        const candidate: any = audio.WaveFile(inputNative);
        const channelLayout: any = candidate.channelLayout;
        const channelLabels: any[] = [];
        try {
          for (let i = 0; i < channelLayout.getLength(); i += 1) {
            channelLabels.push(channelLayout.getByIndex(i));
          }
        } catch (_error) {
          // Some host builds expose only a printable layout descriptor.
        }
        const metadata = {
          filePath: candidate.filePath,
          bitDepth: candidate.bitDepth,
          sampleRate: candidate.sampleRate,
          sampleCount: candidate.sampleCount,
        };
        input = candidate;
        report.input = {
          pathStyle: "native",
          ...metadata,
          channelLayout: {
            description:
              channelLayout.description || String(channelLayout),
            labels: channelLabels,
          },
        };
    } catch (error) {
      report.input.error = String(error);
    }

    if (input) {
      const names = [
        "Elevação de som",
        "Sound Lift",
        "SoundLift",
        "Sound Lift...",
        "$$$/dvaaudiofilters/SoundLiftTitle",
      ];
      for (const name of names) {
        const result: any = { name };
        try {
          result.presets =
            await audio.AudioFilterProcessor.getPresetsForAudioFilter(name);
        } catch (error) {
          result.presetsError = String(error);
        }
        try {
          result.layoutSupported =
            await audio.AudioFilterProcessor
              .isAudioChannelLayoutSupportedByAudioFilter(
                input.channelLayout,
                name
              );
        } catch (error) {
          result.layoutError = String(error);
        }
        if (
          typeof audio.AudioFilterProcessor.getOutputBusNames === "function"
        ) {
          try {
            result.outputBuses =
              await audio.AudioFilterProcessor.getOutputBusNames(
                name,
                input.channelLayout
              );
          } catch (error) {
            result.outputBusesError = String(error);
          }
        } else {
          result.outputBusesUnavailable = true;
        }
        report.candidates.push(result);
      }
    }

    report.validCandidates = report.candidates
      .filter(
        (candidate: any) =>
          candidate.layoutSupported === true &&
          Array.isArray(candidate.presets) &&
          candidate.presets.length > 0
      )
      .map((candidate: any) => candidate.name);

    fs.writeFileSync(
      "file://" + reportNative,
      JSON.stringify(report, null, 2)
    );
    expect(report.hasNamespace).toBeTrue();
    expect(report.validCandidates.length).toBeGreaterThan(0);
  });
});
