var output = new File(
  "/Users/lucas/.codex/.chatgpt-projects/" +
  "g-p-6a67e7a43cb8819180b9733ac6523111/" +
  "adobe_podcast_v2_test_20260728/uxp_probe/" +
  "audition_api_probe.txt"
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

if (output.open("w")) {
  output.writeln("[APP_PROPERTIES]");
  output.writeln(describe(app.reflect.properties));
  output.writeln("[APP_METHODS]");
  output.writeln(describe(app.reflect.methods));
  output.close();
}
