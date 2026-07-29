"use strict";

const assert = require("assert");

const { AddonAdapter } = require("../js/addon-adapter");
const { PremiereAdapter } = require("../js/premiere-adapter");
const {
  FIXED_PRESET,
  MONO_COMPENSATION_MODE,
  NO_COMPENSATION_MODE,
  VoiceNeuralWorkflow,
  assertStereoExtractionPreset,
  compensationModeForTopology
} = require("../js/workflow");

async function checkAddonPromiseCompatibility() {
  let loadCount = 0;
  const adapter = new AddonAdapter(async () => {
    loadCount += 1;
    return {
    async getRuntimeInfo() {
      return JSON.stringify({
        ok: true,
        runtimeReady: true,
        extractionPresetPath: "/runtime/Wave48stereo24.epr"
      });
    },
    async probeAudioTopology(inputPath) {
      assert.strictEqual(inputPath, "/tmp/in.wav");
      return JSON.stringify({
        ok: true,
        channels: 1,
        sampleRate: 48000,
        audioTrackCount: 1
      });
    },
    async processFile(request) {
      const parsed = JSON.parse(request);
      return JSON.stringify({ ok: true, outputPath: parsed.outputPath });
    },
    async cancel() {
      return JSON.stringify({ ok: true, cancelled: true });
    }
    };
  });

  const [info, secondInfo] = await Promise.all([
    adapter.getRuntimeInfo(),
    adapter.getRuntimeInfo()
  ]);
  assert.strictEqual(
    info.extractionPresetPath,
    "/runtime/Wave48stereo24.epr"
  );
  assert.strictEqual(secondInfo.extractionPresetPath, info.extractionPresetPath);
  assert.strictEqual(loadCount, 1);
  const processed = await adapter.processFile({
    inputPath: "/tmp/in.wav",
    outputPath: "/tmp/out.wav"
  });
  assert.strictEqual(processed.outputPath, "/tmp/out.wav");
  const topology = await adapter.probeAudioTopology("/tmp/in.wav");
  assert.strictEqual(topology.channels, 1);
  assert.strictEqual(topology.audioTrackCount, 1);
}

async function checkIndependentExtractionWorkflow() {
  const calls = [];
  const clip = {
    clipName: "Voz ótima.WAV",
    inputPath: "/media/camera.mov",
    sourceInSeconds: 2,
    sourceOutSeconds: 5,
    timelineStartSeconds: 10,
    timelineEndSeconds: 13
  };
  const premiere = {
    async getSelectedAudioClips() {
      return { clips: [clip], ignoredCount: 0 };
    },
    async prepareInsertionContext(context) {
      return context;
    },
    async extractClipToWav(selected, outputPath, presetPath) {
      calls.push(["extract", selected, outputPath, presetPath]);
      return { ok: true, outputPath };
    },
    async importProcessedFiles(context, processed) {
      calls.push(["import", context, processed]);
      return processed.map(item => ({ ...item, projectItem: {} }));
    },
    async insertOnNewTracks(context, imported, disableOriginals) {
      calls.push(["insert", context, imported, disableOriginals]);
      return { laneCount: 1 };
    }
  };
  const addonJobs = [];
  const addon = {
    async getRuntimeInfo() {
      return {
        ok: true,
        extractionPresetPath: "/runtime/Wave48stereo24.epr"
      };
    },
    async probeAudioTopology(inputPath) {
      calls.push(["probe", inputPath]);
      return {
        ok: true,
        channels: 1,
        sampleRate: 48000,
        audioTrackCount: 1
      };
    },
    async processFile(job) {
      addonJobs.push(job);
      return { ok: true, outputPath: job.outputPath };
    },
    async cancel() {
      return { ok: true, cancelled: true };
    }
  };
  const dataFolder = {
    nativePath: "/plugin-data",
    async getEntry() {
      throw new Error("not created yet");
    },
    async createFolder(name) {
      assert.strictEqual(name, "extracted");
      return { isFolder: true, nativePath: "/plugin-data/extracted" };
    }
  };
  const workflow = new VoiceNeuralWorkflow({
    premiere,
    addon,
    localFileSystem: {
      async getDataFolder() {
        return dataFolder;
      }
    }
  });

  const result = await workflow.run({ disableOriginals: true });
  assert.strictEqual(result.clipCount, 1);
  assert.strictEqual(result.originalsDisabled, true);
  assert.strictEqual(calls[0][0], "probe");
  assert.strictEqual(calls[0][1], clip.inputPath);
  assert.strictEqual(calls[1][0], "extract");
  assert.strictEqual(calls[1][1], clip);
  assert.strictEqual(calls[1][3], "/runtime/Wave48stereo24.epr");
  assert.match(calls[1][2], /^\/plugin-data\/extracted\/.+\.wav$/);
  assert.strictEqual(addonJobs.length, 1);
  assert.strictEqual(addonJobs[0].preset, FIXED_PRESET);
  assert.strictEqual(addonJobs[0].inputPath, calls[1][2]);
  assert.strictEqual(
    addonJobs[0].inputCompensationMode,
    MONO_COMPENSATION_MODE
  );
  assert(!Object.hasOwn(addonJobs[0], "sourceInSeconds"));
  assert(!Object.hasOwn(addonJobs[0], "sourceOutSeconds"));
}

async function checkTopologyRoutingAndRejection() {
  assert.strictEqual(
    compensationModeForTopology(
      { channels: 1, audioTrackCount: 1 },
      "Mono"
    ),
    MONO_COMPENSATION_MODE
  );
  assert.strictEqual(
    compensationModeForTopology(
      { channels: 2, audioTrackCount: 1 },
      "Estéreo"
    ),
    NO_COMPENSATION_MODE
  );
  assert.throws(
    () =>
      compensationModeForTopology(
        { channels: 6, audioTrackCount: 1 },
        "Surround"
      ),
    error => error.code === "UNSUPPORTED_AUDIO_CHANNEL_COUNT"
  );
  assert.throws(
    () =>
      compensationModeForTopology(
        { channels: NaN, audioTrackCount: 1 },
        "Incerto"
      ),
    error => error.code === "UNSUPPORTED_AUDIO_CHANNEL_COUNT"
  );
  assert.throws(
    () =>
      compensationModeForTopology(
        { channels: 2, audioTrackCount: 2 },
        "Duas trilhas"
      ),
    error => error.code === "UNSUPPORTED_AUDIO_TRACK_COUNT"
  );
  assert.strictEqual(
    assertStereoExtractionPreset("/runtime/Wave48stereo24.epr"),
    "/runtime/Wave48stereo24.epr"
  );
  assert.throws(
    () => assertStereoExtractionPreset("/runtime/Wave48mono24.epr"),
    error => error.code === "STEREO_EXTRACTION_PRESET_REQUIRED"
  );
}

async function checkEncoderAndLockedTransaction() {
  const listeners = new Map();
  const encoderCalls = [];
  const manager = {
    isAMEInstalled: true,
    async launchEncoder() {
      return true;
    },
    async encodeFile(...argumentsList) {
      encoderCalls.push(argumentsList);
      const outputPath = argumentsList[1];
      setTimeout(() => {
        listeners.get("queue")?.({ jobID: "job-1" });
        listeners.get("complete")?.({
          jobID: "job-1",
          outputFiles: [outputPath]
        });
      }, 0);
      return true;
    }
  };
  let locked = false;
  const insertedProjectItem = { type: "project-item" };
  const fakePpro = {
    ProjectItem: {
      cast(item) {
        assert(locked, "o cast precisa ocorrer dentro de lockedAccess");
        assert.deepStrictEqual(item, { type: "clip-project-item" });
        return insertedProjectItem;
      }
    },
    EncoderManager: {
      EVENT_RENDER_QUEUE: "queue",
      EVENT_RENDER_COMPLETE: "complete",
      EVENT_RENDER_ERROR: "error",
      EVENT_RENDER_CANCEL: "cancel",
      getManager() {
        return manager;
      }
    },
    EventManager: {
      addEventListener(target, name, handler) {
        assert.strictEqual(target, manager);
        listeners.set(name, handler);
      },
      removeEventListener(target, name, handler) {
        assert.strictEqual(target, manager);
        if (listeners.get(name) === handler) listeners.delete(name);
      }
    },
    TickTime: {
      createWithSeconds(seconds) {
        return { seconds };
      }
    },
    SequenceEditor: {
      getEditor() {
        return {
          createInsertProjectItemAction(projectItem) {
            assert(locked, "a ação precisa nascer dentro de lockedAccess");
            assert.strictEqual(projectItem, insertedProjectItem);
            return { type: "insert" };
          }
        };
      }
    }
  };
  const adapter = new PremiereAdapter(fakePpro);
  const extracted = await adapter.extractClipToWav(
    {
      clipName: "Voz",
      inputPath: "/media/camera.mov",
      sourceInSeconds: 1.25,
      sourceOutSeconds: 4.5
    },
    "/tmp/extracted.wav",
    "/runtime/preset.epr"
  );
  assert.strictEqual(extracted.outputPath, "/tmp/extracted.wav");
  assert.strictEqual(encoderCalls.length, 1);
  assert.strictEqual(encoderCalls[0].length, 8);
  assert.deepStrictEqual(encoderCalls[0][3], { seconds: 1.25 });
  assert.deepStrictEqual(encoderCalls[0][4], { seconds: 4.5 });
  assert.strictEqual(encoderCalls[0][7], true);

  const project = {
    lockedAccess(callback) {
      locked = true;
      try {
        callback();
      } finally {
        locked = false;
      }
    },
    executeTransaction(callback) {
      assert(locked);
      callback({
        addAction(action) {
          return Boolean(action);
        }
      });
      return true;
    }
  };
  const insertion = await adapter.insertOnNewTracks(
    {
      project,
      sequence: {},
      baseAudioTrackCount: 2
    },
    [
      {
        clip: {
          clipName: "Voz",
          timelineStartSeconds: 0,
          timelineEndSeconds: 1,
          timelineStartTime: { seconds: 0 },
          originallyDisabled: true
        },
        projectItem: { type: "clip-project-item" }
      }
    ],
    false
  );
  assert.strictEqual(insertion.laneCount, 1);
}

(async () => {
  await checkAddonPromiseCompatibility();
  await checkIndependentExtractionWorkflow();
  await checkTopologyRoutingAndRejection();
  await checkEncoderAndLockedTransaction();
  console.log("UXP workflow checks: OK");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
