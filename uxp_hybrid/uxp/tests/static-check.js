"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const manifest = JSON.parse(
  fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8")
);

assert.strictEqual(manifest.manifestVersion, 6);
assert.strictEqual(manifest.host.app, "premierepro");
assert.strictEqual(manifest.host.minVersion, "26.3.0");
assert.strictEqual(manifest.addon.name, "voz-neural.uxpaddon");
assert.strictEqual(manifest.requiredPermissions.enableAddon, true);
assert.strictEqual(
  manifest.requiredPermissions.localFileSystem,
  "fullAccess"
);

const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
for (const id of [
  "process-button",
  "disable-original",
  "progress-area",
  "progress-bar",
  "status",
  "job-list"
]) {
  assert(
    html.includes(`id="${id}"`),
    `index.html precisa conter #${id}`
  );
}

const {
  assignDestinationLanes,
  normalizePath
} = require("../js/premiere-adapter");
const {
  safeFileStem,
  FIXED_PRESET,
  MONO_COMPENSATION_MODE,
  NO_COMPENSATION_MODE,
  STEREO_PRESET_FILENAME
} = require("../js/workflow");

const laneFixture = [
  { clip: { timelineStartSeconds: 0, timelineEndSeconds: 2 } },
  { clip: { timelineStartSeconds: 1, timelineEndSeconds: 3 } },
  { clip: { timelineStartSeconds: 3, timelineEndSeconds: 4 } }
];
assert.strictEqual(assignDestinationLanes(laneFixture), 2);
assert.deepStrictEqual(
  laneFixture.map(item => item.lane),
  [0, 1, 0]
);
assert.strictEqual(normalizePath("C:\\Audio\\VOZ.wav"), "c:/audio/voz.wav");
assert.strictEqual(safeFileStem("Voz ótima 01.WAV"), "Voz_otima_01");
assert.strictEqual(FIXED_PRESET, "capcut75-premiere-stereo-v1");
assert.strictEqual(MONO_COMPENSATION_MODE, "premiere_mono_center_v1");
assert.strictEqual(NO_COMPENSATION_MODE, "none");
assert.strictEqual(STEREO_PRESET_FILENAME, "Wave48stereo24.epr");

const jsRoot = path.join(ROOT, "js");
const source = [
  fs.readFileSync(path.join(ROOT, "main.js"), "utf8"),
  ...fs
    .readdirSync(jsRoot)
    .filter(file => file.endsWith(".js"))
    .map(file => fs.readFileSync(path.join(jsRoot, file), "utf8"))
].join("\n");

for (const forbidden of [
  "changeMediaFilePath(",
  "createRemoveItemsAction(",
  "createRemoveItemAction(",
  "deleteSequence("
]) {
  assert(
    !source.includes(forbidden),
    `operação destrutiva ou relink proibido: ${forbidden}`
  );
}

console.log("UXP static checks: OK");
