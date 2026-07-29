var output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "console_probe_result.txt"
);

$.writeln("AUDITION_CONSOLE_PROBE_STARTED");
if (output.open("w")) {
  output.writeln("Audition console bridge OK");
  output.close();
  $.writeln("AUDITION_CONSOLE_PROBE_WRITTEN");
} else {
  $.writeln("AUDITION_CONSOLE_PROBE_OPEN_FAILED");
}
