var betaProbeOutput = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_beta_feature_stage3.txt"
);

if (betaProbeOutput.open("w")) {
  betaProbeOutput.writeln("started=true");

  try {
    betaProbeOutput.writeln(
      "typeof.dvaappsupport=" + eval("typeof dvaappsupport")
    );
  } catch (probeErrorA) {
    betaProbeOutput.writeln(
      "typeof.dvaappsupport.error=" + probeErrorA
    );
  }

  try {
    betaProbeOutput.writeln(
      "typeof.BetaFeature=" + eval("typeof BetaFeature")
    );
  } catch (probeErrorB) {
    betaProbeOutput.writeln(
      "typeof.BetaFeature.error=" + probeErrorB
    );
  }

  try {
    betaProbeOutput.writeln(
      "typeof.app.dvaappsupport=" +
        eval("typeof app.dvaappsupport")
    );
  } catch (probeErrorC) {
    betaProbeOutput.writeln(
      "typeof.app.dvaappsupport.error=" + probeErrorC
    );
  }

  betaProbeOutput.close();
}
