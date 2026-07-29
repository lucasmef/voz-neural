var betaOutput = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/b.txt"
);

if (betaOutput.open("w")) {
  betaOutput.writeln("started=true");

  try {
    betaOutput.writeln(
      "typeof.dvaappsupport=" + eval("typeof dvaappsupport")
    );
  } catch (betaErrorA) {
    betaOutput.writeln(
      "typeof.dvaappsupport.error=" + betaErrorA
    );
  }

  try {
    betaOutput.writeln(
      "typeof.BetaFeature=" + eval("typeof BetaFeature")
    );
  } catch (betaErrorB) {
    betaOutput.writeln(
      "typeof.BetaFeature.error=" + betaErrorB
    );
  }

  try {
    betaOutput.writeln(
      "typeof.app.dvaappsupport=" +
        eval("typeof app.dvaappsupport")
    );
  } catch (betaErrorC) {
    betaOutput.writeln(
      "typeof.app.dvaappsupport.error=" + betaErrorC
    );
  }

  betaOutput.close();
}
