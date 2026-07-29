var flagOutput = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/d.txt"
);

function readFlag(label, source) {
  try {
    flagOutput.writeln(label + "=" + eval(source));
  } catch (flagError) {
    flagOutput.writeln(label + ".error=" + flagError);
  }
}

if (flagOutput.open("w")) {
  flagOutput.writeln("started=true");
  readFlag(
    "EnhanceSpeech.enabled",
    "BetaFeature.isFeatureEnabled('Audition.EnhanceSpeech')"
  );
  readFlag(
    "EnhanceSpeechWebService.enabled",
    "BetaFeature.isFeatureEnabled(" +
      "'Audition.EnhanceSpeechWebService')"
  );
  readFlag(
    "EnhanceSpeech.floodgate",
    "BetaFeature.isFeatureEnabledFloodgate(" +
      "'Audition.EnhanceSpeech')"
  );
  readFlag(
    "EnhanceSpeechWebService.floodgate",
    "BetaFeature.isFeatureEnabledFloodgate(" +
      "'Audition.EnhanceSpeechWebService')"
  );
  readFlag(
    "internalEntitlement",
    "BetaFeature.isEntitlementSetToInternalFeaturesAllowed"
  );
  readFlag(
    "override.off",
    "BetaFeature.FEATURE_FLAG_OVERRIDE_OFF"
  );
  readFlag(
    "override.on",
    "BetaFeature.FEATURE_FLAG_OVERRIDE_ON"
  );
  readFlag(
    "override.default",
    "BetaFeature.FEATURE_FLAG_OVERRIDE_DEFAULT"
  );
  readFlag(
    "reflect.properties",
    "BetaFeature.reflect.properties"
  );
  readFlag(
    "reflect.methods",
    "BetaFeature.reflect.methods"
  );
  flagOutput.close();
}
