import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

enum HelperFailure: Error {
    case tool(String, String, String, Bool, [String: Any])
}

func failure(_ code: String, _ message: String, category: String = "runtime", retryable: Bool = false, details: [String: Any] = [:]) -> HelperFailure {
    .tool(code, message, category, retryable, details)
}

func stringArg(_ args: [String: Any], _ key: String, required: Bool = false) throws -> String? {
    if let value = args[key] as? String { return value }
    if required { throw failure("INVALID_ARGUMENT", "Missing required argument \(key).", category: "validation") }
    return nil
}

func intArg(_ args: [String: Any], _ key: String, default fallback: Int) -> Int {
    if let value = args[key] as? Int { return value }
    if let value = args[key] as? NSNumber { return value.intValue }
    return fallback
}

func boolArg(_ args: [String: Any], _ key: String, default fallback: Bool) -> Bool {
    if let value = args[key] as? Bool { return value }
    if let value = args[key] as? NSNumber { return value.boolValue }
    return fallback
}

func runningApps() -> [[String: Any]] {
    NSWorkspace.shared.runningApplications.map { app in
        [
            "name": app.localizedName ?? "",
            "bundle_id": app.bundleIdentifier ?? "",
            "pid": Int(app.processIdentifier),
            "background_only": app.activationPolicy == .prohibited,
            "frontmost": app.isActive,
        ]
    }
}

func resolveApp(_ reference: String) throws -> [String: Any] {
    let needle = reference.lowercased()
    if let app = runningApps().first(where: {
        (($0["name"] as? String)?.lowercased() == needle) || (($0["bundle_id"] as? String)?.lowercased() == needle)
    }) {
        return app
    }
    throw failure("NOT_FOUND", "Application \(reference.debugDescription) is not running.", category: "not_found")
}

func requireAccessibility() throws {
    guard AXIsProcessTrusted() else {
        throw failure(
            "ACCESSIBILITY_PERMISSION_REQUIRED",
            "Coding Tools MCP App Helper does not have macOS Accessibility permission.",
            category: "permission",
            retryable: true,
            details: [
                "settings": "System Settings > Privacy & Security > Accessibility",
                "hint": "Enable Coding Tools MCP App Helper, then retry."
            ]
        )
    }
}

func screenRecordingTrusted(request: Bool = false) -> Bool {
    if #available(macOS 10.15, *) {
        return request ? CGRequestScreenCaptureAccess() : CGPreflightScreenCaptureAccess()
    }
    return false
}

func axValue(_ element: AXUIElement, _ attribute: String) -> Any? {
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &raw) == .success, let value = raw else {
        return nil
    }
    if CFGetTypeID(value) == CFStringGetTypeID() { return value as? String }
    if CFGetTypeID(value) == CFBooleanGetTypeID() { return (value as? NSNumber)?.boolValue }
    if CFGetTypeID(value) == CFNumberGetTypeID() { return value as? NSNumber }
    if CFGetTypeID(value) == AXValueGetTypeID() {
        let ax: AXValue = unsafeBitCast(value, to: AXValue.self)
        switch AXValueGetType(ax) {
        case .cgPoint:
            var point = CGPoint.zero
            if AXValueGetValue(ax, .cgPoint, &point) { return ["x": point.x, "y": point.y] }
        case .cgSize:
            var size = CGSize.zero
            if AXValueGetValue(ax, .cgSize, &size) { return ["width": size.width, "height": size.height] }
        case .cgRect:
            var rect = CGRect.zero
            if AXValueGetValue(ax, .cgRect, &rect) {
                return ["x": rect.origin.x, "y": rect.origin.y, "width": rect.size.width, "height": rect.size.height]
            }
        default: break
        }
    }
    return nil
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &raw) == .success,
          let values = raw as? [AXUIElement] else { return [] }
    return values
}

func summary(_ element: AXUIElement) -> [String: Any] {
    let attributes = [
        "role": kAXRoleAttribute,
        "subrole": kAXSubroleAttribute,
        "title": kAXTitleAttribute,
        "identifier": kAXIdentifierAttribute,
        "description": kAXDescriptionAttribute,
        "value": kAXValueAttribute,
        "enabled": kAXEnabledAttribute,
        "focused": kAXFocusedAttribute,
        "position": kAXPositionAttribute,
        "size": kAXSizeAttribute,
    ]
    var result: [String: Any] = [:]
    for (key, attribute) in attributes {
        if let value = axValue(element, attribute) { result[key] = value }
    }
    return result
}

func walk(_ root: AXUIElement, maxDepth: Int, maxElements: Int? = nil) -> [(AXUIElement, Int)] {
    var stack: [(AXUIElement, Int)] = [(root, 0)]
    var result: [(AXUIElement, Int)] = []
    while let (element, depth) = stack.popLast() {
        result.append((element, depth))
        if let limit = maxElements, result.count >= limit { break }
        if depth < maxDepth {
            for child in children(element).reversed() { stack.append((child, depth + 1)) }
        }
    }
    return result
}

func matches(_ item: [String: Any], role: String?, title: String?, identifier: String?) -> Bool {
    if let role, ((item["role"] as? String) ?? "").lowercased() != role.lowercased() { return false }
    if let identifier, ((item["identifier"] as? String) ?? "").lowercased() != identifier.lowercased() { return false }
    if let title {
        let value = (item["title"] as? String) ?? (item["description"] as? String) ?? String(describing: item["value"] ?? "")
        if value.lowercased() != title.lowercased() { return false }
    }
    return true
}

func findElement(_ root: AXUIElement, args: [String: Any], maxDepth: Int = 12) throws -> (AXUIElement, [String: Any]) {
    let role = try stringArg(args, "role")
    let title = try stringArg(args, "title")
    let identifier = try stringArg(args, "identifier")
    let targetIndex = intArg(args, "index", default: 0)
    var matched = 0
    for (element, _) in walk(root, maxDepth: maxDepth) {
        let item = summary(element)
        if matches(item, role: role, title: title, identifier: identifier) {
            if matched == targetIndex { return (element, item) }
            matched += 1
        }
    }
    throw failure(
        "NOT_FOUND",
        "No accessibility element matched the requested selector.",
        category: "not_found",
        details: ["role": role ?? NSNull(), "title": title ?? NSNull(), "identifier": identifier ?? NSNull(), "index": targetIndex]
    )
}

func appRoot(_ reference: String) throws -> ([String: Any], AXUIElement) {
    try requireAccessibility()
    let app = try resolveApp(reference)
    guard let pid = app["pid"] as? Int else { throw failure("APP_CONTROL_ERROR", "Resolved application has no pid.") }
    return (app, AXUIElementCreateApplication(pid_t(pid)))
}

func performPress(_ element: AXUIElement) -> Bool {
    AXUIElementPerformAction(element, kAXPressAction as CFString) == .success
}

func center(_ item: [String: Any]) -> CGPoint? {
    guard let p = item["position"] as? [String: Any], let s = item["size"] as? [String: Any],
          let x = (p["x"] as? NSNumber)?.doubleValue, let y = (p["y"] as? NSNumber)?.doubleValue,
          let w = (s["width"] as? NSNumber)?.doubleValue, let h = (s["height"] as? NSNumber)?.doubleValue else { return nil }
    return CGPoint(x: x + w / 2, y: y + h / 2)
}

func mouseClick(_ point: CGPoint) throws {
    guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) else {
        throw failure("APP_CONTROL_ERROR", "Could not create mouse events.")
    }
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
}

let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "enter": 36, "return": 36,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45,
    "m": 46, ".": 47, "tab": 48, "space": 49, "backspace": 51, "delete": 51, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
]

func eventFlags(_ modifiers: [String]) throws -> CGEventFlags {
    var flags: CGEventFlags = []
    for value in modifiers {
        switch value.lowercased() {
        case "cmd", "command", "meta": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "ctrl", "control": flags.insert(.maskControl)
        case "alt", "option": flags.insert(.maskAlternate)
        case "fn", "function": flags.insert(.maskSecondaryFn)
        default: throw failure("INVALID_ARGUMENT", "Unknown modifier \(value.debugDescription).", category: "validation")
        }
    }
    return flags
}

func keyPress(code: CGKeyCode, modifiers: [String] = []) throws {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        throw failure("APP_CONTROL_ERROR", "Could not create keyboard events.")
    }
    let flags = try eventFlags(modifiers)
    down.flags = flags; up.flags = flags
    down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
}

func typeUnicode(_ text: String) throws {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false) else {
        throw failure("APP_CONTROL_ERROR", "Could not create text keyboard events.")
    }
    var units = Array(text.utf16)
    let unitCount = units.count
    units.withUnsafeMutableBufferPointer { pointer in
        down.keyboardSetUnicodeString(stringLength: unitCount, unicodeString: pointer.baseAddress!)
        up.keyboardSetUnicodeString(stringLength: unitCount, unicodeString: pointer.baseAddress!)
    }
    down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
}

func runProcess(_ executable: String, _ arguments: [String], timeout: TimeInterval = 15) throws -> (Int32, String, String) {
    let process = Process()
    let stdout = Pipe(), stderr = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.standardOutput = stdout; process.standardError = stderr
    try process.run()
    let deadline = Date().addingTimeInterval(timeout)
    while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
    if process.isRunning { process.terminate(); throw failure("APP_CONTROL_ERROR", "Process timed out: \(executable)") }
    let out = String(data: stdout.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    let err = String(data: stderr.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    return (process.terminationStatus, out, err)
}

func handle(_ action: String, _ args: [String: Any]) throws -> [String: Any] {
    switch action {
    case "accessibility":
        let shouldPrompt = boolArg(args, "open_settings", default: false)
        let trusted: Bool
        if shouldPrompt {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            trusted = AXIsProcessTrustedWithOptions(options)
        } else {
            trusted = AXIsProcessTrusted()
        }
        if shouldPrompt && !trusted,
           let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
        return ["ok": true, "trusted": trusted, "settings": "System Settings > Privacy & Security > Accessibility", "helper": true]
    case "screen_recording":
        let shouldPrompt = boolArg(args, "open_settings", default: false)
        let trusted = screenRecordingTrusted(request: shouldPrompt)
        if shouldPrompt && !trusted,
           let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
        return ["ok": true, "trusted": trusted, "settings": "System Settings > Privacy & Security > Screen Recording", "helper": true]
    case "permissions":
        return [
            "ok": true,
            "accessibility_trusted": AXIsProcessTrusted(),
            "screen_recording_trusted": screenRecordingTrusted(),
            "helper": true,
        ]
    case "list_apps":
        var apps = runningApps()
        if !boolArg(args, "include_background", default: false) { apps.removeAll { ($0["background_only"] as? Bool) == true } }
        if let query = try stringArg(args, "query"), !query.isEmpty {
            let needle = query.lowercased()
            apps.removeAll { !((($0["name"] as? String) ?? "").lowercased().contains(needle) || (($0["bundle_id"] as? String) ?? "").lowercased().contains(needle)) }
        }
        let limit = max(1, intArg(args, "max_results", default: 200))
        return ["ok": true, "accessibility_trusted": AXIsProcessTrusted(), "apps": Array(apps.prefix(limit)), "count": min(apps.count, limit), "truncated": apps.count > limit, "helper": true]
    case "launch":
        let ref = try stringArg(args, "app", required: true)!
        var launchArgs: [String] = []
        if boolArg(args, "new_instance", default: false) { launchArgs.append("-n") }
        launchArgs.append((ref.contains(".") && !ref.contains(" ")) ? "-b" : "-a")
        launchArgs.append(ref)
        let (status, _, err) = try runProcess("/usr/bin/open", launchArgs)
        if status != 0 { throw failure("APP_CONTROL_ERROR", err.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Could not launch \(ref)." : err) }
        return ["ok": true, "app": ref, "helper": true]
    case "activate":
        let ref = try stringArg(args, "app", required: true)!
        _ = try handle("launch", ["app": ref, "new_instance": false])
        Thread.sleep(forTimeInterval: Double(min(intArg(args, "wait_ms", default: 150), 2000)) / 1000.0)
        let resolved = try resolveApp(ref)
        if let pid = resolved["pid"] as? Int, let app = NSRunningApplication(processIdentifier: pid_t(pid)) {
            _ = app.activate(options: [])
        }
        return ["ok": true, "app": ref, "resolved": resolved, "helper": true]
    case "windows":
        let ref = try stringArg(args, "app", required: true)!
        let (app, root) = try appRoot(ref)
        var items: [[String: Any]] = []
        var raw: CFTypeRef?
        if AXUIElementCopyAttributeValue(root, kAXWindowsAttribute as CFString, &raw) == .success, let windows = raw as? [AXUIElement] {
            for (index, window) in windows.enumerated() { var item = summary(window); item["index"] = index; items.append(item) }
        }
        return ["ok": true, "app": app, "windows": items, "count": items.count, "helper": true]
    case "snapshot":
        let ref = try stringArg(args, "app", required: true)!
        let (app, root) = try appRoot(ref)
        let maxDepth = max(0, intArg(args, "max_depth", default: 6)), maxElements = max(1, intArg(args, "max_elements", default: 500))
        var items: [[String: Any]] = []
        for (element, depth) in walk(root, maxDepth: maxDepth, maxElements: maxElements) { var item = summary(element); item["depth"] = depth; items.append(item) }
        return ["ok": true, "app": app, "elements": items, "count": items.count, "truncated": items.count >= maxElements, "helper": true]
    case "click":
        guard args["role"] != nil || args["title"] != nil || args["identifier"] != nil else { throw failure("INVALID_ARGUMENT", "app_click requires at least one of role, title, or identifier.", category: "validation") }
        let ref = try stringArg(args, "app", required: true)!, (app, root) = try appRoot(ref)
        let (element, item) = try findElement(root, args: args)
        if performPress(element) { return ["ok": true, "app": app, "element": item, "method": "AXPress", "helper": true] }
        guard let point = center(item) else { throw failure("APP_CONTROL_ERROR", "Element cannot be pressed and has no clickable bounds.") }
        try mouseClick(point)
        return ["ok": true, "app": app, "element": item, "method": "mouse", "helper": true]
    case "type_text":
        let ref = try stringArg(args, "app", required: true)!, text = try stringArg(args, "text", required: true)!, (app, root) = try appRoot(ref)
        if args["role"] != nil || args["title"] != nil || args["identifier"] != nil {
            let (element, item) = try findElement(root, args: args)
            if boolArg(args, "clear", default: true), AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, text as CFTypeRef) == .success {
                return ["ok": true, "app": app, "element": item, "characters": text.count, "method": "AXValue", "helper": true]
            }
            if !performPress(element), let point = center(item) { try mouseClick(point) }
        }
        if boolArg(args, "clear", default: true) { try keyPress(code: 0, modifiers: ["command"]); try keyPress(code: 51) }
        try typeUnicode(text)
        return ["ok": true, "app": app, "characters": text.count, "method": "keyboard", "helper": true]
    case "press":
        let ref = try stringArg(args, "app", required: true)!, key = try stringArg(args, "key", required: true)!.lowercased()
        _ = try handle("activate", ["app": ref, "wait_ms": intArg(args, "wait_ms", default: 100)])
        guard let code = keyCodes[key] else { throw failure("INVALID_ARGUMENT", "Unsupported key \(key.debugDescription).", category: "validation") }
        let modifiers = (args["modifiers"] as? [String]) ?? []
        try keyPress(code: code, modifiers: modifiers)
        return ["ok": true, "app": try resolveApp(ref), "key": key, "modifiers": modifiers, "helper": true]
    case "menu":
        let ref = try stringArg(args, "app", required: true)!, path = (args["path"] as? [String]) ?? []
        guard !path.isEmpty else { throw failure("INVALID_ARGUMENT", "Menu path cannot be empty.", category: "validation") }
        let (app, root) = try appRoot(ref)
        for (depth, title) in path.enumerated() {
            let selector: [String: Any] = ["role": depth == 0 ? "AXMenuBarItem" : "AXMenuItem", "title": title, "index": 0]
            let (element, _) = try findElement(root, args: selector, maxDepth: 6)
            guard performPress(element) else { throw failure("APP_CONTROL_ERROR", "Could not choose menu item \(title.debugDescription).") }
            if depth < path.count - 1 { Thread.sleep(forTimeInterval: 0.08) }
        }
        return ["ok": true, "app": app, "path": path, "helper": true]
    case "screenshot":
        let ref = try stringArg(args, "app", required: true)!, windowIndex = intArg(args, "window_index", default: 0), (app, root) = try appRoot(ref)
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(root, kAXWindowsAttribute as CFString, &raw) == .success, let windows = raw as? [AXUIElement], windowIndex >= 0, windowIndex < windows.count else {
            throw failure("INVALID_ARGUMENT", "window_index \(windowIndex) is out of range.", category: "validation")
        }
        let item = summary(windows[windowIndex])
        guard let p = item["position"] as? [String: Any], let s = item["size"] as? [String: Any],
              let x = (p["x"] as? NSNumber)?.intValue, let y = (p["y"] as? NSNumber)?.intValue,
              let width = (s["width"] as? NSNumber)?.intValue, let height = (s["height"] as? NSNumber)?.intValue, width > 0, height > 0 else {
            throw failure("APP_CONTROL_ERROR", "Window has no capturable bounds.")
        }
        let output = FileManager.default.temporaryDirectory.appendingPathComponent("coding-tools-mcp-\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: output) }
        let (status, _, err) = try runProcess("/usr/sbin/screencapture", ["-x", "-R", "\(x),\(y),\(width),\(height)", "-t", "png", output.path])
        guard status == 0, let data = try? Data(contentsOf: output) else {
            throw failure("SCREEN_RECORDING_PERMISSION_REQUIRED", err.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Window screenshot failed. Check Screen Recording permission." : err, category: "permission", details: ["settings": "System Settings > Privacy & Security > Screen Recording"])
        }
        return ["ok": true, "app": app, "window_index": windowIndex, "bounds": ["x": x, "y": y, "width": width, "height": height], "mime_type": "image/png", "bytes": data.count, "_mcp_image_data": data.base64EncodedString(), "helper": true]
    default:
        throw failure("INVALID_ARGUMENT", "Unknown helper action \(action.debugDescription).", category: "validation")
    }
}

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: []), let line = String(data: data, encoding: .utf8) {
        FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
    }
}

do {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let request = try JSONSerialization.jsonObject(with: input) as? [String: Any], let action = request["action"] as? String else {
        throw failure("INVALID_ARGUMENT", "Helper request must contain an action.", category: "validation")
    }
    let args = request["args"] as? [String: Any] ?? [:]
    emit(try handle(action, args))
} catch HelperFailure.tool(let code, let message, let category, let retryable, let details) {
    emit(["ok": false, "error": ["code": code, "message": message, "category": category, "retryable": retryable, "details": details]])
    exit(2)
} catch {
    emit(["ok": false, "error": ["code": "APP_CONTROL_ERROR", "message": String(describing: error), "category": "runtime", "retryable": false, "details": [:]]])
    exit(2)
}
