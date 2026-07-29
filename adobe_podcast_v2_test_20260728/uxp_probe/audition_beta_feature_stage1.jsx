var stage1Output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_beta_feature_stage1.txt"
);

if (stage1Output.open("w")) {
  stage1Output.writeln("started=true");

  try {
    stage1Output.writeln(
      "typeof.dvaappsupport=" + typeof dvaappsupport
    );
  } catch (stage1ErrorA) {
    stage1Output.writeln("dvaappsupport.error=" + stage1ErrorA);
  }

  try {
    stage1Output.writeln("typeof.BetaFeature=" + typeof BetaFeature);
  } catch (stage1ErrorB) {
    stage1Output.writeln("BetaFeature.error=" + stage1ErrorB);
  }

  try {
    stage1Output.writeln(
      "typeof.app.dvaappsupport=" + typeof app.dvaappsupport
    );
  } catch (stage1ErrorC) {
    stage1Output.writeln("app.dvaappsupport.error=" + stage1ErrorC);
  }

  stage1Output.close();
}
