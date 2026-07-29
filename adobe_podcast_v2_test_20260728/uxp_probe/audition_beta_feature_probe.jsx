var output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_beta_feature_probe.txt"
);

function writeLine(text) {
  output.writeln(text);
}

function probe(candidateName, candidate) {
  writeLine("[CANDIDATE] " + candidateName);
  if (!candidate) {
    writeLine("available=false");
    return false;
  }

  writeLine("available=true");
  try {
    writeLine(
      "Audition.EnhanceSpeech=" +
        candidate.isFeatureEnabled("Audition.EnhanceSpeech")
    );
    writeLine(
      "Audition.EnhanceSpeechWebService=" +
        candidate.isFeatureEnabled("Audition.EnhanceSpeechWebService")
    );
    writeLine(
      "Audition.EnhanceSpeech.floodgate=" +
        candidate.isFeatureEnabledFloodgate("Audition.EnhanceSpeech")
    );
    writeLine(
      "Audition.EnhanceSpeechWebService.floodgate=" +
        candidate.isFeatureEnabledFloodgate(
          "Audition.EnhanceSpeechWebService"
        )
    );
    writeLine(
      "internalEntitlement=" +
        candidate.isEntitlementSetToInternalFeaturesAllowed
    );
    writeLine(
      "override.off=" + candidate.FEATURE_FLAG_OVERRIDE_OFF
    );
    writeLine(
      "override.on=" + candidate.FEATURE_FLAG_OVERRIDE_ON
    );
    writeLine(
      "override.default=" + candidate.FEATURE_FLAG_OVERRIDE_DEFAULT
    );
    return true;
  } catch (error) {
    writeLine("error=" + error);
    return false;
  }
}

if (output.open("w")) {
  var found = false;

  try {
    if (typeof dvaappsupport !== "undefined") {
      found = probe(
        "dvaappsupport.BetaFeature",
        dvaappsupport.BetaFeature
      ) || found;
    }
  } catch (errorA) {
    writeLine("dvaappsupport.error=" + errorA);
  }

  try {
    if (typeof BetaFeature !== "undefined") {
      found = probe("BetaFeature", BetaFeature) || found;
    }
  } catch (errorB) {
    writeLine("BetaFeature.error=" + errorB);
  }

  try {
    if (
      typeof app !== "undefined" &&
      typeof app.dvaappsupport !== "undefined"
    ) {
      found = probe(
        "app.dvaappsupport.BetaFeature",
        app.dvaappsupport.BetaFeature
      ) || found;
    }
  } catch (errorC) {
    writeLine("app.dvaappsupport.error=" + errorC);
  }

  writeLine("found=" + found);
  output.close();
}
