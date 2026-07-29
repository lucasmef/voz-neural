import AppKit
import ApplicationServices
import Foundation

guard let application = NSWorkspace.shared.runningApplications.first(
    where: { $0.bundleIdentifier == "com.adobe.Audition" }
) else {
    fputs("Audition não encontrado.\n", stderr)
    exit(1)
}

let pid = application.processIdentifier
let appElement = AXUIElementCreateApplication(pid)

func copyAttribute(
    _ element: AXUIElement,
    _ attribute: CFString
) -> (AXError, CFTypeRef?) {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    return (error, value)
}

func stringValue(_ value: CFTypeRef?) -> String? {
    guard let value else { return nil }
    if CFGetTypeID(value) == CFStringGetTypeID() {
        return value as? String
    }
    return String(describing: value)
}

let (focusedError, focusedValue) = copyAttribute(
    appElement,
    kAXFocusedUIElementAttribute as CFString
)

var output: [String: Any] = [
    "pid": Int(pid),
    "frontmost": application.isActive,
    "focusedError": focusedError.rawValue,
]

if focusedError == .success,
   let focusedValue,
   CFGetTypeID(focusedValue) == AXUIElementGetTypeID()
{
    let focused = unsafeBitCast(focusedValue, to: AXUIElement.self)
    let fields: [(String, CFString)] = [
        ("role", kAXRoleAttribute as CFString),
        ("subrole", kAXSubroleAttribute as CFString),
        ("title", kAXTitleAttribute as CFString),
        ("description", kAXDescriptionAttribute as CFString),
        ("value", kAXValueAttribute as CFString),
        ("identifier", kAXIdentifierAttribute as CFString),
        ("focused", kAXFocusedAttribute as CFString),
        ("enabled", kAXEnabledAttribute as CFString),
        ("position", kAXPositionAttribute as CFString),
        ("size", kAXSizeAttribute as CFString),
    ]

    var focusedOutput: [String: Any] = [:]
    for (name, attribute) in fields {
        let (error, value) = copyAttribute(focused, attribute)
        focusedOutput[name] = [
            "error": error.rawValue,
            "value": stringValue(value) ?? NSNull(),
        ]
    }

    var actionNames: CFArray?
    let actionsError = AXUIElementCopyActionNames(focused, &actionNames)
    focusedOutput["actionsError"] = actionsError.rawValue
    focusedOutput["actions"] = (actionNames as? [String]) ?? []
    output["focused"] = focusedOutput
}

let data = try JSONSerialization.data(
    withJSONObject: output,
    options: [.prettyPrinted, .sortedKeys]
)
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data([0x0a]))
