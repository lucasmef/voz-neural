ObjC.import("stdlib");

const systemEvents = Application("System Events");
const candidates = systemEvents.applicationProcesses().filter((candidate) => {
  const name = safe(() => candidate.name(), "");
  return /Audition/i.test(name);
});
const process =
  candidates.find(
    (candidate) =>
      safe(() => candidate.bundleIdentifier(), "") === "com.adobe.Audition"
  ) ||
  candidates[0] ||
  null;

function safe(call, fallback = null) {
  try {
    return call();
  } catch (_error) {
    return fallback;
  }
}

function summarize(element, depth, path) {
  const role = safe(() => element.role(), "");
  const description = safe(() => element.description(), "");
  const title = safe(() => element.title(), "");
  const value = safe(() => element.value(), "");
  const focused = safe(() => element.focused(), false);

  if (
    role === "AXTextField" ||
    role === "AXTextArea" ||
    role === "AXButton" ||
    /Command|Console|Comando/i.test(
      [description, title, value].filter(Boolean).join(" ")
    )
  ) {
    return {
      path,
      depth,
      role,
      description,
      title,
      value:
        typeof value === "string" && value.length > 500
          ? value.slice(0, 500)
          : value,
      focused,
      position: safe(() => element.position(), null),
      size: safe(() => element.size(), null),
      actions: safe(
        () => element.actions().map((action) => action.name()),
        []
      ),
      attributes: safe(
        () => element.attributes().map((attribute) => attribute.name()),
        []
      ),
    };
  }
  return null;
}

function walk(element, depth, path, output) {
  if (depth > 12 || output.length > 500) return;
  const row = summarize(element, depth, path);
  if (row) output.push(row);
  const children = safe(() => element.uiElements(), []);
  for (let index = 0; index < children.length; index += 1) {
    walk(children[index], depth + 1, `${path}/${index}`, output);
  }
}

const output = [];
const windows = process ? safe(() => process.windows(), []) : [];
for (let index = 0; index < windows.length; index += 1) {
  walk(windows[index], 0, `window${index}`, output);
}

JSON.stringify(
  {
    frontmost: safe(() => process.frontmost(), false),
    windows: windows.length,
    candidates: candidates.map((candidate) => ({
      name: safe(() => candidate.name(), ""),
      bundleIdentifier: safe(() => candidate.bundleIdentifier(), ""),
      unixId: safe(() => candidate.unixId(), null),
      frontmost: safe(() => candidate.frontmost(), false),
      windows: safe(() => candidate.windows().length, 0),
    })),
    elements: output,
  },
  null,
  2
);
