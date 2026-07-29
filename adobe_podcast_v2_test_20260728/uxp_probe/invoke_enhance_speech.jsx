var output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "invoke_enhance_speech.txt"
);

function writeStatus(text, append) {
  if (output.open(append ? "a" : "w")) {
    output.writeln(text);
    output.close();
  }
}

if (app.activeDocument) {
  app.activeDocument.activate();
  app.invokeCommand("View.WaveformEditor");
  app.invokeCommand("Window.Editor");
  app.invokeCommand("Edit.SelectAll");
  writeStatus(
    "enabled=" +
      app.isCommandEnabled("Effects.EnhanceSpeechProcess"),
    false
  );
  var result = app.invokeCommand("Effects.EnhanceSpeechProcess");
  writeStatus("result=" + result, true);
} else {
  writeStatus("error=no-active-document", false);
}
