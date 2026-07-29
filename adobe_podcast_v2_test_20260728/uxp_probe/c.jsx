var c = new File(
  "/Users/lucas/.codex/.chatgpt-projects/g-p-6a67e7a43cb8819180b9733ac6523111/adobe_podcast_v2_test_20260728/uxp_probe/c.txt"
);

if (c.open("w")) {
  c.writeln("C_OK");
  c.close();
}
