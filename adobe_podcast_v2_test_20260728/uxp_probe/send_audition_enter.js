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

const enterDown = $.CGEventCreateKeyboardEvent(null, 36, true);
const enterUp = $.CGEventCreateKeyboardEvent(null, 36, false);
$.CGEventPostToPid(pid, enterDown);
$.CGEventPostToPid(pid, enterUp);

JSON.stringify({ pid, submitted: true });
