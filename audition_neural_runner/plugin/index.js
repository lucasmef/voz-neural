const { entrypoints } = require("uxp");

const ROOT =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111";
const OUTPUT_DIR = ROOT + "/audition_neural_runner/output";
const FILTER = "Enhance Speech";

const jobs = [
  {
    name: "S01 limpa",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
      "S01_female_clean_ptbr.wav",
    output: OUTPUT_DIR + "/S01_EnhanceSpeech_100.wav",
  },
  {
    name: "S03 níveis",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
      "S03_female_level_steps.wav",
    output: OUTPUT_DIR + "/S03_EnhanceSpeech_100.wav",
  },
  {
    name: "S06 sala",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
      "S06_female_alt_room_reverb.wav",
    output: OUTPUT_DIR + "/S06_EnhanceSpeech_100.wav",
  },
  {
    name: "M04 ventilador",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/30_speech_noise_mixes/" +
      "M04_female_alt_fan_snr_0.wav",
    output: OUTPUT_DIR + "/M04_EnhanceSpeech_100.wav",
  },
  {
    name: "F02 música",
    input:
      ROOT +
      "/capcut_blackbox_audio_pack_female_v3/40_voice_focus/" +
      "F02_female_music_snr_m5.wav",
    output: OUTPUT_DIR + "/F02_EnhanceSpeech_100.wav",
  },
];

function errorText(error) {
  if (error && error.stack) {
    return String(error.stack);
  }
  return String(error);
}

function setupPanel(rootNode) {
  const button = rootNode.querySelector("#run");
  const status = rootNode.querySelector("#status");
  if (!button || !status || button.dataset.bound === "true") {
    return;
  }

  button.dataset.bound = "true";
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "Inicializando o filtro…";

    try {
      const uxp = require("uxp");
      const audio = uxp.dvaaudiofilters;
      if (!audio || !audio.AudioFilterProcessor || !audio.WaveFile) {
        throw new Error("A API local de filtros de áudio não foi disponibilizada.");
      }

      const presets =
        await audio.AudioFilterProcessor.getPresetsForAudioFilter(FILTER);
      if (!presets || presets.length === 0) {
        throw new Error("O Enhance Speech não retornou nenhum preset.");
      }

      const preset = presets[0];
      const lines = [
        "Filtro: " + FILTER,
        "Preset: " + preset,
        "",
      ];
      status.textContent = lines.join("\n");

      for (let index = 0; index < jobs.length; index += 1) {
        const job = jobs[index];
        lines.push(
          "[" + (index + 1) + "/" + jobs.length + "] " + job.name + "…"
        );
        status.textContent = lines.join("\n");

        const input = audio.WaveFile(job.input);
        const supported =
          await audio.AudioFilterProcessor
            .isAudioChannelLayoutSupportedByAudioFilter(
              input.channelLayout,
              FILTER
            );
        if (!supported) {
          throw new Error("Layout de canais não aceito em " + job.name + ".");
        }

        const output = audio.WaveFile(job.output);
        const result = await audio.AudioFilterProcessor.process(
          FILTER,
          preset,
          input,
          [output]
        );
        lines[lines.length - 1] += " concluído (" + result + ")";
        status.textContent = lines.join("\n");
      }

      lines.push("", "Tudo concluído.");
      status.textContent = lines.join("\n");
    } catch (error) {
      status.textContent += "\n\nERRO:\n" + errorText(error);
    } finally {
      button.disabled = false;
    }
  });
}

entrypoints.setup({
  panels: {
    mainPanel: {
      show(event) {
        setupPanel(event.node || document);
      },
    },
  },
});

document.addEventListener("DOMContentLoaded", () => setupPanel(document));
