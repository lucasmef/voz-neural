"use strict";

function createUi(documentObject) {
  const elements = {
    button: documentObject.getElementById("process-button"),
    buttonLabel: documentObject.getElementById("button-label"),
    disableOriginal: documentObject.getElementById("disable-original"),
    progressArea: documentObject.getElementById("progress-area"),
    progressLabel: documentObject.getElementById("progress-label"),
    progressCount: documentObject.getElementById("progress-count"),
    progressTrack: documentObject.querySelector(".progress-track"),
    progressBar: documentObject.getElementById("progress-bar"),
    status: documentObject.getElementById("status"),
    statusIcon: documentObject.querySelector(".status-icon"),
    statusText: documentObject.querySelector("#status p"),
    jobList: documentObject.getElementById("job-list")
  };

  function setBusy(busy) {
    elements.button.disabled = busy;
    elements.disableOriginal.disabled = busy;
    elements.buttonLabel.textContent = busy
      ? "Tratando os áudios…"
      : "Criar voz tratada";
    elements.progressArea.hidden = !busy;
    if (!busy) setProgress(0, 0, "");
  }

  function setProgress(current, total, label) {
    const percent = total > 0 ? Math.round((current / total) * 100) : 0;
    elements.progressLabel.textContent = label || "Preparando…";
    elements.progressCount.textContent = total > 0 ? `${current}/${total}` : "";
    elements.progressBar.style.width = `${percent}%`;
    elements.progressTrack.setAttribute("aria-valuenow", String(percent));
  }

  function setStatus(message, type = "info") {
    elements.status.className = `status ${type}`;
    elements.statusIcon.textContent =
      type === "success" ? "✓" : type === "error" ? "!" : "i";
    elements.statusText.textContent = message;
  }

  function clearJobs() {
    while (elements.jobList.firstChild) {
      elements.jobList.removeChild(elements.jobList.firstChild);
    }
  }

  function addJobs(clips) {
    clearJobs();
    for (const clip of clips) {
      const item = documentObject.createElement("li");
      const state = documentObject.createElement("span");
      const name = documentObject.createElement("span");
      state.className = "job-state";
      state.textContent = "·";
      name.className = "job-name";
      name.textContent = clip.clipName;
      item.appendChild(state);
      item.appendChild(name);
      elements.jobList.appendChild(item);
    }
  }

  function setJobState(index, state) {
    const item = elements.jobList.children[index];
    if (!item) return;
    const marker = item.querySelector(".job-state");
    marker.textContent =
      state === "done" ? "✓" : state === "error" ? "!" : "●";
    if (state === "error") marker.style.color = "var(--danger)";
  }

  return {
    elements,
    setBusy,
    setProgress,
    setStatus,
    clearJobs,
    addJobs,
    setJobState
  };
}

module.exports = { createUi };
