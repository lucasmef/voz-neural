ObjC.import("CoreGraphics");

const systemEvents = Application("System Events");
const audition = systemEvents.applicationProcesses()
  .find((process) => {
    try {
      return process.bundleIdentifier() === "com.adobe.Audition";
    } catch (_error) {
      return false;
    }
  });
const pid = audition ? Number(audition.unixId()) : 0;
if (!Number.isInteger(pid) || pid <= 0) {
  throw new Error("O processo do Audition não foi encontrado.");
}

const command =
  "uxphost.load extension=com.adobe.dva.test.hostplugin " +
  "type=testPlugin panelId=mainPanel hostview=panel";

const keyCodes = {
  a: 0,
  s: 1,
  d: 2,
  f: 3,
  h: 4,
  g: 5,
  z: 6,
  x: 7,
  c: 8,
  v: 9,
  b: 11,
  q: 12,
  w: 13,
  e: 14,
  r: 15,
  y: 16,
  t: 17,
  "1": 18,
  "2": 19,
  "3": 20,
  "4": 21,
  "6": 22,
  "5": 23,
  "=": 24,
  "9": 25,
  "7": 26,
  "-": 27,
  "8": 28,
  "0": 29,
  "]": 30,
  o: 31,
  u: 32,
  "[": 33,
  i: 34,
  p: 35,
  l: 37,
  j: 38,
  k: 40,
  ";": 41,
  ",": 43,
  "/": 44,
  n: 45,
  m: 46,
  ".": 47,
  " ": 49,
};

const shiftMask = 1 << 17;
const commandMask = 1 << 20;

function postKey(keyCode, flags = 0) {
  const down = $.CGEventCreateKeyboardEvent(null, keyCode, true);
  const up = $.CGEventCreateKeyboardEvent(null, keyCode, false);
  if (flags) {
    $.CGEventSetFlags(down, flags);
    $.CGEventSetFlags(up, flags);
  }
  $.CGEventPostToPid(pid, down);
  $.CGEventPostToPid(pid, up);
}

// Clear only the currently focused Audition console field.
postKey(keyCodes.a, commandMask);
postKey(51);

for (const character of command) {
  const lower = character.toLowerCase();
  const keyCode = keyCodes[lower];
  if (keyCode === undefined) {
    throw new Error(`Caractere sem mapeamento: ${character}`);
  }
  postKey(keyCode, character !== lower ? shiftMask : 0);
}

JSON.stringify({ pid, command, characters: command.length, submitted: false });
