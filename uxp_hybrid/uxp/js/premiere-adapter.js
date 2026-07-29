"use strict";

const { VoiceNeuralError } = require("./errors");

const SPEED_TOLERANCE = 0.0001;
const DURATION_TOLERANCE_SECONDS = 0.05;
const ENCODE_TIMEOUT_MS = 30 * 60 * 1000;

function secondsOf(tickTime, label) {
  const value = tickTime && Number(tickTime.seconds);
  if (!Number.isFinite(value)) {
    throw new VoiceNeuralError(
      `O Premiere não informou ${label} do clipe.`,
      "INVALID_CLIP_TIME",
      { label, tickTime }
    );
  }
  return value;
}

function normalizePath(value) {
  return String(value || "")
    .replace(/\\/g, "/")
    .replace(/\/+/g, "/")
    .replace(/\/$/, "")
    .toLocaleLowerCase();
}

class PremiereAdapter {
  constructor(ppro = null) {
    try {
      this.ppro = ppro || require("premierepro");
    } catch (error) {
      throw new VoiceNeuralError(
        "O painel precisa ser aberto dentro do Premiere Pro 26.3 ou mais novo.",
        "PREMIERE_API_UNAVAILABLE",
        error
      );
    }
  }

  async getSelectedAudioClips() {
    const project = await this.ppro.Project.getActiveProject();
    if (!project) {
      throw new VoiceNeuralError(
        "Abra um projeto no Premiere antes de continuar.",
        "NO_ACTIVE_PROJECT"
      );
    }

    const sequence = await project.getActiveSequence();
    if (!sequence) {
      throw new VoiceNeuralError(
        "Abra uma sequência e selecione os clipes de áudio.",
        "NO_ACTIVE_SEQUENCE"
      );
    }

    const selection = await sequence.getSelection();
    const selectedItems = selection
      ? await selection.getTrackItems()
      : [];

    if (!Array.isArray(selectedItems) || selectedItems.length === 0) {
      throw new VoiceNeuralError(
        "Selecione pelo menos um clipe de áudio na linha do tempo.",
        "EMPTY_SELECTION"
      );
    }

    const audioItems = [];
    let ignoredCount = 0;
    for (const item of selectedItems) {
      if (this._isAudioClipTrackItem(item)) {
        audioItems.push(item);
      } else {
        ignoredCount += 1;
      }
    }

    if (audioItems.length === 0) {
      throw new VoiceNeuralError(
        "A seleção não contém clipes de áudio. Selecione o áudio, não o vídeo.",
        "NO_AUDIO_CLIPS",
        { ignoredCount }
      );
    }

    const clips = [];
    for (let index = 0; index < audioItems.length; index += 1) {
      clips.push(await this._describeAudioClip(audioItems[index], index));
    }

    clips.sort(
      (left, right) =>
        left.timelineStartSeconds - right.timelineStartSeconds ||
        left.timelineEndSeconds - right.timelineEndSeconds ||
        left.selectionIndex - right.selectionIndex
    );

    return { project, sequence, clips, ignoredCount };
  }

  _isAudioClipTrackItem(item) {
    if (!item || typeof item !== "object") return false;

    const AudioCtor = this.ppro.AudioClipTrackItem;
    if (typeof AudioCtor === "function") {
      try {
        if (item instanceof AudioCtor) return true;
      } catch (_) {
        // Some host objects do not expose a usable instanceof bridge.
      }
    }

    const constructorName =
      item.constructor && typeof item.constructor.name === "string"
        ? item.constructor.name
        : "";
    if (constructorName === "AudioClipTrackItem") return true;
    if (constructorName === "VideoClipTrackItem") return false;

    // Refuse ambiguous host objects instead of accidentally processing video.
    return false;
  }

  async _describeAudioClip(trackItem, selectionIndex) {
    const [
      name,
      projectItem,
      inPoint,
      outPoint,
      timelineStart,
      timelineEnd,
      speed,
      reversed,
      disabled,
      sourceTrackIndex
    ] = await Promise.all([
      trackItem.getName(),
      trackItem.getProjectItem(),
      trackItem.getInPoint(),
      trackItem.getOutPoint(),
      trackItem.getStartTime(),
      trackItem.getEndTime(),
      trackItem.getSpeed(),
      trackItem.isSpeedReversed(),
      trackItem.isDisabled(),
      trackItem.getTrackIndex()
    ]);

    const clipName = String(name || `Áudio ${selectionIndex + 1}`);
    const numericSpeed = Number(speed);
    if (
      Boolean(reversed) ||
      !Number.isFinite(numericSpeed) ||
      Math.abs(numericSpeed - 1) > SPEED_TOLERANCE
    ) {
      throw new VoiceNeuralError(
        `“${clipName}” usa velocidade alterada ou reversa. ` +
          "Renderize esse clipe antes de aplicar o Voz Neural.",
        "UNSUPPORTED_SPEED",
        { clipName, speed, reversed }
      );
    }

    let clipProjectItem;
    try {
      clipProjectItem = this.ppro.ClipProjectItem.cast(projectItem);
    } catch (error) {
      throw new VoiceNeuralError(
        `“${clipName}” não aponta para uma mídia de áudio compatível.`,
        "PROJECT_ITEM_CAST_FAILED",
        error
      );
    }

    if (!clipProjectItem) {
      throw new VoiceNeuralError(
        `“${clipName}” não aponta para uma mídia de áudio compatível.`,
        "PROJECT_ITEM_NOT_CLIP"
      );
    }

    const [offline, merged, multicam, nested, inputPath] = await Promise.all([
      this._optionalBoolean(clipProjectItem, "isOffline"),
      this._optionalBoolean(clipProjectItem, "isMergedClip"),
      this._optionalBoolean(clipProjectItem, "isMulticamClip"),
      this._optionalBoolean(clipProjectItem, "isSequence"),
      clipProjectItem.getMediaFilePath()
    ]);

    if (offline) {
      throw new VoiceNeuralError(
        `A mídia de “${clipName}” está offline. Vincule-a antes de continuar.`,
        "MEDIA_OFFLINE"
      );
    }
    if (merged || multicam || nested) {
      throw new VoiceNeuralError(
        `“${clipName}” é um clipe mesclado, multicâmera ou sequência. ` +
          "Renderize-o antes de aplicar o Voz Neural.",
        "UNSUPPORTED_PROJECT_ITEM",
        { merged, multicam, nested }
      );
    }
    if (!inputPath || typeof inputPath !== "string") {
      throw new VoiceNeuralError(
        `O Premiere não informou o arquivo de origem de “${clipName}”.`,
        "MEDIA_PATH_MISSING"
      );
    }

    const sourceInSeconds = secondsOf(inPoint, "o início na mídia");
    const sourceOutSeconds = secondsOf(outPoint, "o fim na mídia");
    const timelineStartSeconds = secondsOf(
      timelineStart,
      "o início na linha do tempo"
    );
    const timelineEndSeconds = secondsOf(
      timelineEnd,
      "o fim na linha do tempo"
    );
    const sourceDuration = sourceOutSeconds - sourceInSeconds;
    const timelineDuration = timelineEndSeconds - timelineStartSeconds;

    if (sourceDuration <= 0 || timelineDuration <= 0) {
      throw new VoiceNeuralError(
        `“${clipName}” não possui duração de áudio válida.`,
        "INVALID_CLIP_DURATION"
      );
    }
    if (
      Math.abs(sourceDuration - timelineDuration) >
      DURATION_TOLERANCE_SECONDS
    ) {
      throw new VoiceNeuralError(
        `“${clipName}” tem duração de origem diferente da linha do tempo. ` +
          "Renderize esse clipe antes de aplicar o Voz Neural.",
        "DURATION_MISMATCH",
        { sourceDuration, timelineDuration }
      );
    }

    return {
      trackItem,
      clipProjectItem,
      clipName,
      inputPath,
      sourceInSeconds,
      sourceOutSeconds,
      timelineStartSeconds,
      timelineEndSeconds,
      sourceTrackIndex: Number(sourceTrackIndex),
      originallyDisabled: Boolean(disabled),
      selectionIndex
    };
  }

  async _optionalBoolean(target, methodName) {
    if (!target || typeof target[methodName] !== "function") return false;
    return Boolean(await target[methodName]());
  }

  async extractClipToWav(clip, outputPath, presetPath) {
    const manager = this.ppro.EncoderManager.getManager();
    if (!manager || !manager.isAMEInstalled) {
      throw new VoiceNeuralError(
        "O Adobe Media Encoder 2026 precisa estar instalado para extrair cada clipe.",
        "AME_UNAVAILABLE"
      );
    }

    const inPoint = this.ppro.TickTime.createWithSeconds(
      clip.sourceInSeconds
    );
    const outPoint = this.ppro.TickTime.createWithSeconds(
      clip.sourceOutSeconds
    );

    return new Promise((resolve, reject) => {
      const ownedJobIds = new Set();
      let finished = false;

      const eventManager = this.ppro.EventManager;
      const encoderClass = this.ppro.EncoderManager;
      const listeners = [
        [
          encoderClass.EVENT_RENDER_QUEUE,
          event => {
            if (event && event.jobID != null) {
              ownedJobIds.add(String(event.jobID));
            }
          }
        ],
        [
          encoderClass.EVENT_RENDER_COMPLETE,
          event => {
            const outputs =
              event && Array.isArray(event.outputFiles)
                ? event.outputFiles
                : [];
            if (
              outputs.some(path => normalizePath(path) === normalizePath(outputPath))
            ) {
              settle(null, {
                ok: true,
                outputPath,
                jobID: event && event.jobID
              });
            }
          }
        ],
        [
          encoderClass.EVENT_RENDER_ERROR,
          event => {
            if (
              event &&
              event.jobID != null &&
              ownedJobIds.has(String(event.jobID))
            ) {
              settle(
                new VoiceNeuralError(
                  `O Media Encoder falhou ao extrair “${clip.clipName}”.`,
                  "AME_RENDER_ERROR",
                  event
                )
              );
            }
          }
        ],
        [
          encoderClass.EVENT_RENDER_CANCEL,
          event => {
            if (
              event &&
              event.jobID != null &&
              ownedJobIds.has(String(event.jobID))
            ) {
              settle(
                new VoiceNeuralError(
                  `A extração de “${clip.clipName}” foi cancelada.`,
                  "AME_RENDER_CANCELLED",
                  event
                )
              );
            }
          }
        ]
      ];

      const timeout = setTimeout(() => {
        settle(
          new VoiceNeuralError(
            `O Media Encoder excedeu o tempo de extração de “${clip.clipName}”.`,
            "AME_RENDER_TIMEOUT"
          )
        );
      }, ENCODE_TIMEOUT_MS);

      const removeListeners = () => {
        for (const [eventName, handler] of listeners) {
          try {
            eventManager.removeEventListener(manager, eventName, handler);
          } catch (_) {
            // Cleanup errors must not hide the render result.
          }
        }
      };

      const settle = (error, value) => {
        if (finished) return;
        finished = true;
        clearTimeout(timeout);
        removeListeners();
        if (error) reject(error);
        else resolve(value);
      };

      for (const [eventName, handler] of listeners) {
        eventManager.addEventListener(manager, eventName, handler);
      }

      Promise.resolve()
        .then(async () => {
          if (typeof manager.launchEncoder === "function") {
            await manager.launchEncoder();
          }
          const queued = await manager.encodeFile(
            clip.inputPath,
            outputPath,
            presetPath,
            inPoint,
            outPoint,
            0,
            true,
            true
          );
          if (!queued) {
            throw new VoiceNeuralError(
              `O Media Encoder não aceitou “${clip.clipName}” para extração.`,
              "AME_QUEUE_FAILED"
            );
          }
        })
        .catch(error => settle(error));
    });
  }

  async importProcessedFiles(context, processed) {
    const outputPaths = processed.map(item => item.outputPath);
    const root = await context.project.getRootItem();
    const imported = await context.project.importFiles(
      outputPaths,
      true,
      root,
      false
    );
    if (!imported) {
      throw new VoiceNeuralError(
        "Os áudios ficaram prontos, mas o Premiere não conseguiu importá-los.",
        "IMPORT_FAILED"
      );
    }

    const allProjectItems = await this._walkProjectItems(root);
    const byPath = new Map();
    for (const item of allProjectItems) {
      const clip = this._tryCastClipProjectItem(item);
      if (!clip || typeof clip.getMediaFilePath !== "function") continue;
      try {
        const path = await clip.getMediaFilePath();
        if (path) byPath.set(normalizePath(path), clip);
      } catch (_) {
        // Bins, sequences and unsupported project items are intentionally skipped.
      }
    }

    return processed.map(result => {
      const projectItem = byPath.get(normalizePath(result.outputPath));
      if (!projectItem) {
        throw new VoiceNeuralError(
          `O Premiere importou “${result.clip.clipName}”, mas não devolveu ` +
            "a referência necessária para colocá-lo na linha do tempo.",
          "IMPORTED_ITEM_NOT_FOUND",
          { outputPath: result.outputPath }
        );
      }
      return { ...result, projectItem };
    });
  }

  async _walkProjectItems(root) {
    const result = [];
    const pending = [root];
    while (pending.length > 0) {
      const item = pending.pop();
      result.push(item);
      const folder = this._tryCastFolderItem(item);
      if (!folder || typeof folder.getItems !== "function") continue;
      try {
        const children = await folder.getItems();
        if (Array.isArray(children)) pending.push(...children);
      } catch (_) {
        // A failed cast is harmless; the item remains available as a leaf.
      }
    }
    return result;
  }

  _tryCastFolderItem(item) {
    try {
      return this.ppro.FolderItem.cast(item);
    } catch (_) {
      return null;
    }
  }

  _tryCastClipProjectItem(item) {
    try {
      return this.ppro.ClipProjectItem.cast(item);
    } catch (_) {
      return null;
    }
  }

  async insertOnNewTracks(context, processed, disableOriginals) {
    const editor = this.ppro.SequenceEditor.getEditor(context.sequence);
    if (!editor) {
      throw new VoiceNeuralError(
        "O Premiere não abriu o editor da sequência ativa.",
        "SEQUENCE_EDITOR_UNAVAILABLE"
      );
    }

    const laneCount = assignDestinationLanes(processed);
    let transactionResult = false;
    context.project.lockedAccess(() => {
      transactionResult = context.project.executeTransaction(
        compoundAction => {
          for (const result of processed) {
            const audioTrackIndex = context.baseAudioTrackCount + result.lane;
            let insertProjectItem;
            try {
              insertProjectItem = this.ppro.ProjectItem.cast(
                result.projectItem
              );
            } catch (error) {
              throw new VoiceNeuralError(
                `O Premiere não reconheceu “${result.clip.clipName}” para ` +
                  "inserção na linha do tempo.",
                "INSERT_PROJECT_ITEM_CAST_FAILED",
                error
              );
            }
            if (!insertProjectItem) {
              throw new VoiceNeuralError(
                `O Premiere não reconheceu “${result.clip.clipName}” para ` +
                  "inserção na linha do tempo.",
                "INSERT_PROJECT_ITEM_CAST_FAILED"
              );
            }
            const action = editor.createInsertProjectItemAction(
              insertProjectItem,
              result.clip.timelineStartTime,
              0,
              audioTrackIndex,
              true
            );
            if (!action || compoundAction.addAction(action) === false) {
              throw new VoiceNeuralError(
                `O Premiere não preparou a inserção de “${result.clip.clipName}”.`,
                "INSERT_ACTION_FAILED"
              );
            }
          }

          if (disableOriginals) {
            for (const result of processed) {
              if (result.clip.originallyDisabled) continue;
              const disableAction =
                result.clip.trackItem.createSetDisabledAction(true);
              if (
                !disableAction ||
                compoundAction.addAction(disableAction) === false
              ) {
                throw new VoiceNeuralError(
                  `O Premiere não preparou a desativação de ` +
                    `“${result.clip.clipName}”.`,
                  "DISABLE_ACTION_FAILED"
                );
              }
            }
          }
        },
        disableOriginals
          ? "Criar voz tratada e desativar originais"
          : "Criar voz tratada"
      );
    });

    if (transactionResult === false) {
      throw new VoiceNeuralError(
        "O Premiere não concluiu a inserção dos áudios tratados.",
        "TRANSACTION_FAILED"
      );
    }

    return { laneCount };
  }

  prepareInsertionContext(selectionContext) {
    return Promise.all([
      selectionContext.sequence.getAudioTrackCount(),
      ...selectionContext.clips.map(clip =>
        Promise.resolve(
          this.ppro.TickTime.createWithSeconds(clip.timelineStartSeconds)
        )
      )
    ]).then(values => {
      const [baseAudioTrackCount, ...timelineStartTimes] = values;
      const clips = selectionContext.clips.map((clip, index) => ({
        ...clip,
        timelineStartTime: timelineStartTimes[index]
      }));
      return {
        ...selectionContext,
        clips,
        baseAudioTrackCount: Number(baseAudioTrackCount)
      };
    });
  }
}

function assignDestinationLanes(processed) {
  const laneEnds = [];
  for (const result of processed) {
    const start = result.clip.timelineStartSeconds;
    const end = result.clip.timelineEndSeconds;
    let lane = laneEnds.findIndex(laneEnd => laneEnd <= start + 0.000001);
    if (lane < 0) {
      lane = laneEnds.length;
      laneEnds.push(end);
    } else {
      laneEnds[lane] = end;
    }
    result.lane = lane;
  }
  return laneEnds.length;
}

module.exports = {
  PremiereAdapter,
  ENCODE_TIMEOUT_MS,
  assignDestinationLanes,
  normalizePath,
  secondsOf
};
