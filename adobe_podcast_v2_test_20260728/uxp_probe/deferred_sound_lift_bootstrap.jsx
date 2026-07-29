var command =
  "uxphost.load extension=com.adobe.dva.test.hostplugin " +
  "type=command command=execute hostview=no_ui " +
  "data={%22testScriptFilepath%22:%22/Users/lucas/.codex/" +
  ".chatgpt-projects/g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "render_sound_lift_stems.test.ts%22," +
  "%22configFilepath%22:%22/Users/lucas/.codex/" +
  ".chatgpt-projects/g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/config.yaml%22," +
  "%22quitAfterExecute%22:false,%22updateBaselines%22:false," +
  "%22uploadTestAssets%22:false}";

app.scheduleTask(
  "app.executeConsoleCommand(" + command.toSource() + ")",
  5000,
  false
);
