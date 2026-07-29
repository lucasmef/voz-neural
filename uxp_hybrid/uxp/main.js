"use strict";

const { storage } = require("uxp");
const { AddonAdapter } = require("./js/addon-adapter");
const { PremiereAdapter } = require("./js/premiere-adapter");
const { VoiceNeuralWorkflow } = require("./js/workflow");
const { createUi } = require("./js/ui");

function startPanel() {
  const ui = createUi(document);
  let workflow;

  try {
    workflow = new VoiceNeuralWorkflow({
      premiere: new PremiereAdapter(),
      addon: new AddonAdapter(),
      localFileSystem: storage.localFileSystem
    });
  } catch (error) {
    ui.elements.button.disabled = true;
    ui.setStatus(error.message || String(error), "error");
    return;
  }

  ui.elements.button.addEventListener("click", async () => {
    ui.setBusy(true);
    ui.clearJobs();
    ui.setStatus("Conferindo os clipes selecionados…", "info");

    const observer = {
      onSelection(clips, ignoredCount) {
        ui.addJobs(clips);
        const ignoredMessage =
          ignoredCount > 0
            ? ` ${ignoredCount} item(ns) que não eram áudio foram ignorados.`
            : "";
        ui.setStatus(
          `${clips.length} clipe(s) de áudio pronto(s) para tratar.` +
            ignoredMessage,
          ignoredCount > 0 ? "warning" : "info"
        );
      },
      onJobStart(clip, index, total) {
        ui.setJobState(index, "running");
        ui.setProgress(index, total, `Preparando “${clip.clipName}”…`);
      },
      onJobStage(clip, index, total, stage) {
        const verb =
          stage === "probe"
            ? "Conferindo canais de"
            : stage === "extract"
            ? "Extraindo"
            : "Tratando";
        ui.setProgress(index, total, `${verb} “${clip.clipName}”…`);
      },
      onJobComplete(clip, index, total) {
        ui.setJobState(index, "done");
        ui.setProgress(index + 1, total, `“${clip.clipName}” ficou pronto`);
      },
      onJobError(clip, index) {
        ui.setJobState(index, "error");
        ui.setStatus(`Falha ao tratar “${clip.clipName}”.`, "error");
      },
      onStage(stage) {
        if (stage === "import") {
          ui.setStatus("Importando os áudios tratados no projeto…", "info");
        } else if (stage === "insert") {
          ui.setStatus("Colocando os áudios em uma faixa nova…", "info");
        }
      }
    };

    try {
      const summary = await workflow.run(
        {
          disableOriginals: ui.elements.disableOriginal.checked
        },
        observer
      );
      const trackText =
        summary.laneCount === 1
          ? "uma faixa nova"
          : `${summary.laneCount} faixas novas`;
      const originalText = summary.originalsDisabled
        ? " Os originais foram desativados e podem ser restaurados com Desfazer."
        : " Os originais continuam ativos.";
      ui.setStatus(
        `${summary.clipCount} clipe(s) tratado(s) em ${trackText}.` +
          originalText,
        "success"
      );
    } catch (error) {
      ui.setStatus(error.message || String(error), "error");
    } finally {
      ui.setBusy(false);
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", startPanel, { once: true });
} else {
  startPanel();
}
