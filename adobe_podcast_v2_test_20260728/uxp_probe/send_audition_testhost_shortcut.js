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
  throw new Error("AUDITION_PID inválido.");
}

const keyCodeF12 = 111;
const shiftMask = 1 << 17;
const commandMask = 1 << 20;
const functionMask = 1 << 23;
const flags = shiftMask | commandMask | functionMask;

const keyDown = $.CGEventCreateKeyboardEvent(null, keyCodeF12, true);
const keyUp = $.CGEventCreateKeyboardEvent(null, keyCodeF12, false);

$.CGEventSetFlags(keyDown, flags);
$.CGEventSetFlags(keyUp, flags);
$.CGEventPostToPid(pid, keyDown);
delay(0.08);
$.CGEventPostToPid(pid, keyUp);

JSON.stringify({ pid, keyCodeF12, flags });
