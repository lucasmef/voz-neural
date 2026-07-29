var input =
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "capcut_blackbox_audio_pack_female_v3/10_speech_quality/" +
  "S01_female_clean_ptbr.wav";
var output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_document_probe.txt"
);

function describe(items) {
  var rows = [];
  for (var index = 0; index < items.length; index += 1) {
    var item = items[index];
    rows.push(
      item.name +
      " | type=" +
      item.type +
      " | dataType=" +
      item.dataType +
      " | description=" +
      item.description
    );
  }
  return rows.join("\n");
}

var document = app.openDocument(new DocumentOpenParameter(input));
if (output.open("w")) {
  output.writeln("[DOCUMENT_PROPERTIES]");
  output.writeln(describe(document.reflect.properties));
  output.writeln("[DOCUMENT_METHODS]");
  output.writeln(describe(document.reflect.methods));
  output.close();
}
