function run() {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  const command =
    "/usr/bin/env DVA_USE_STDOUT=1 " +
    "'/Applications/Adobe Audition 2026/Adobe Audition 2026.app/Contents/" +
    "MacOS/Adobe Audition 2026' " +
    "'-DebugFlag' 'dvaaudiodevice.EnableVirtualDevice:true' " +
    "'-DebugFlag' 'dvaaudiodevice.ForceSelectVirtualDevice:true' " +
    "'-console' " +
    "'uxphost.load extension=com.adobe.dva.test.hostplugin type=command " +
    "command=execute hostview=no_ui " +
    "data={%22testScriptFilepath%22:%22/Users/lucas/.codex/.chatgpt-projects/" +
    "g-p-6a67e7a43cb8819180b9733ac6523111/" +
    "adobe_podcast_v2_test_20260728/uxp_probe/" +
    "probe_sound_lift.test.ts%22,%22configFilepath%22:%22/Users/lucas/.codex/" +
    ".chatgpt-projects/g-p-6a67e7a43cb8819180b9733ac6523111/" +
    "adobe_podcast_v2_test_20260728/uxp_probe/config.yaml%22," +
    "%22quitAfterExecute%22:true,%22updateBaselines%22:false," +
    "%22uploadTestAssets%22:false}' " +
    "> '/Users/lucas/.codex/.chatgpt-projects/" +
    "g-p-6a67e7a43cb8819180b9733ac6523111/" +
    "adobe_podcast_v2_test_20260728/uxp_probe/audition_host.log' 2>&1";
  return app.doShellScript(command);
}
