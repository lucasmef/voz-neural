var stage2Output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_beta_feature_stage2.txt"
);

function stage2Evaluate(label, source) {
  try {
    stage2Output.writeln(label + "=" + eval(source));
  } catch (stage2Error) {
    stage2Output.writeln(label + ".error=" + stage2Error);
  }
}

if (stage2Output.open("w")) {
  stage2Output.writeln("started=true");
  stage2Evaluate(
    "typeof.dvaappsupport",
    "typeof dvaappsupport"
  );
  stage2Evaluate(
    "typeof.BetaFeature",
    "typeof BetaFeature"
  );
  stage2Evaluate(
    "typeof.app.dvaappsupport",
    "typeof app.dvaappsupport"
  );
  stage2Evaluate(
    "dvaappsupport.EnhanceSpeech",
    "dvaappsupport.BetaFeature.isFeatureEnabled(" +
      "'Audition.EnhanceSpeech')"
  );
  stage2Evaluate(
    "dvaappsupport.EnhanceSpeechWebService",
    "dvaappsupport.BetaFeature.isFeatureEnabled(" +
      "'Audition.EnhanceSpeechWebService')"
  );
  stage2Evaluate(
    "app.EnhanceSpeech",
    "app.dvaappsupport.BetaFeature.isFeatureEnabled(" +
      "'Audition.EnhanceSpeech')"
  );
  stage2Evaluate(
    "app.EnhanceSpeechWebService",
    "app.dvaappsupport.BetaFeature.isFeatureEnabled(" +
      "'Audition.EnhanceSpeechWebService')"
  );
  stage2Output.close();
}
