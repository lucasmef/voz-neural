const uxp = require("uxp");
const fs = require("fs");

const ROOT =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111";
const OUTPUT_DIR =
  ROOT + "/adobe_podcast_v2_test_20260728/soundlift_uxp_output";
const REPORT_PATH = OUTPUT_DIR + "/soundlift_report.json";
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

let running = false;
let report = {
  startedAt: null,
  completedAt: null,
  filter: null,
  preset: null,
  outputBuses: [],
  results: [],
  error: null,
};

function setStatus(message) {
  const status = document.getElementById("status");
  if (status) status.textContent = message;
}

function setButtonsDisabled(disabled) {
  document.getElementById("runOne").disabled = disabled;
  document.getElementById("runBatch").disabled = disabled;
}

function fileUrl(nativePath) {
  return "file://" + nativePath;
}

async function ensureOutputDirectory() {
  const url = fileUrl(OUTPUT_DIR);
  try {
    fs.readdirSync(url);
    return;
  } catch (_error) {
    // The output leaf does not exist yet.
  }
  await fs.mkdir(url);
}

function writeReport() {
  try {
    fs.writeFileSync(fileUrl(REPORT_PATH), JSON.stringify(report, null, 2));
  } catch (error) {
    console.log("Não foi possível gravar o relatório:", error);
  }
}

function normalizeBusName(name) {
  return String(name || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "");
}

function canonicalBusName(name, index, busCount) {
  const normalized = normalizeBusName(name);
  const aliases = {
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
  if (busCount === EXPECTED_BUSES.length && EXPECTED_BUSES[index]) {
    return EXPECTED_BUSES[index];
  }
  const safe = String(name || "Bus" + (index + 1)).replace(
    /[^A-Za-z0-9_.-]+/g,
    "_"
  );
  return safe || "Bus" + (index + 1);
}

async function resolveFilter(audio, input) {
  for (const filter of FILTER_CANDIDATES) {
    try {
      const presets =
        await audio.AudioFilterProcessor.getPresetsForAudioFilter(filter);
      const supported =
        await audio.AudioFilterProcessor
          .isAudioChannelLayoutSupportedByAudioFilter(
            input.channelLayout,
            filter
          );
      if (supported && Array.isArray(presets) && presets.length) {
        const runtimeBuses =
          await audio.AudioFilterProcessor.getOutputBusNames(
            filter,
            input.channelLayout
          );
        if (!Array.isArray(runtimeBuses) || runtimeBuses.length === 0) {
          throw new Error("O filtro não declarou barramentos de saída.");
        }
        const buses = runtimeBuses.map((runtimeName, index) => ({
          runtimeName,
          canonicalName: canonicalBusName(
            runtimeName,
            index,
            runtimeBuses.length
          ),
        }));
        const required = ["Speech", "Noise", "Reverb", "Background"];
        const available = new Set(buses.map((bus) => bus.canonicalName));
        const missing = required.filter((name) => !available.has(name));
        if (missing.length) {
          throw new Error(
            "Barramentos obrigatórios ausentes: " + missing.join(", ")
          );
        }
        const preset =
          presets.find((name) => name === "Version 1") || presets[0];
        return { filter, preset, buses };
      }
    } catch (error) {
      console.log("Filtro não disponível:", filter, error);
    }
  }
  throw new Error("O Sound Lift não foi encontrado nesta sessão do Audition.");
}

async function processTests(tests) {
  if (running) return;
  running = true;
  setButtonsDisabled(true);
  report = {
    startedAt: new Date().toISOString(),
    completedAt: null,
    filter: null,
    preset: null,
    outputBuses: [],
    results: [],
    error: null,
  };

  try {
    const audio = uxp.dvaaudiofilters;
    if (!audio || !audio.AudioFilterProcessor || !audio.WaveFile) {
      throw new Error("A API interna dvaaudiofilters não está disponível.");
    }

    await ensureOutputDirectory();
    const firstInput = audio.WaveFile(tests[0].input);
    const selection = await resolveFilter(audio, firstInput);
    report.filter = selection.filter;
    report.preset = selection.preset;
    report.outputBuses = selection.buses;
    writeReport();

    for (let index = 0; index < tests.length; index += 1) {
      const test = tests[index];
      setStatus(
        "Processando " + test.id + " (" + (index + 1) + "/" + tests.length + ")…"
      );
      const input = audio.WaveFile(test.input);
      const outputDescriptors = selection.buses.map((bus) => ({
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
      const outputs = outputDescriptors.map((output) =>
        audio.WaveFile(output.path)
      );
      const processResult = await audio.AudioFilterProcessor.process(
        selection.filter,
        selection.preset,
        input,
        outputs
      );
      for (const output of outputDescriptors) {
        const stats = await fs.lstat(fileUrl(output.path));
        if (!stats || !stats.size || stats.size <= 44) {
          throw new Error(
            "Saída ausente ou vazia para o barramento " +
            output.canonicalName +
            ": " +
            output.path
          );
        }
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
    setStatus(
      "Concluído. " +
      report.results.length +
      " clipe(s), " +
      selection.buses.length +
      " camadas por clipe.\n" +
      OUTPUT_DIR
    );
  } catch (error) {
    report.error = String(error && error.stack ? error.stack : error);
    writeReport();
    setStatus("Erro:\n" + report.error);
  } finally {
    running = false;
    setButtonsDisabled(false);
  }
}

function bindUi() {
  const one = document.getElementById("runOne");
  const batch = document.getElementById("runBatch");
  if (one && !one.dataset.bound) {
    one.dataset.bound = "true";
    one.addEventListener("click", () => processTests(TESTS.slice(0, 1)));
  }
  if (batch && !batch.dataset.bound) {
    batch.dataset.bound = "true";
    batch.addEventListener("click", () => processTests(TESTS));
  }
}

uxp.entrypoints.setup({
  panels: {
    soundLiftProbe: {
      show() {
        bindUi();
      },
    },
  },
});

document.addEventListener("DOMContentLoaded", bindUi);
