// Native computer protocol v1. No shell, AppleScript, global input, or activation.
import AppKit
import ApplicationServices
import Foundation
import ScreenCaptureKit

// ScreenCaptureKit requires an initialized AppKit/WindowServer connection even
// when this helper has no windows and is started through private stdio.
let helperApplication = NSApplication.shared
helperApplication.setActivationPolicy(.prohibited)

let protocolVersion = 1
let helperInstance = UUID().uuidString
let supportedOperations = ["status", "list_apps", "resolve_app", "windows", "snapshot", "action", "inspect"]

struct Failure: Error {
    let code: String
    let message: String
    var category = "runtime"
}

func fail(_ code: String, _ message: String, _ category: String = "runtime") -> Failure {
    Failure(code: code, message: message, category: category)
}

func required(_ args: [String: Any], _ key: String) throws -> String {
    guard let value = args[key] as? String, !value.isEmpty else {
        throw fail("INVALID_ARGUMENT", "Missing \(key).", "validation")
    }
    return value
}

func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}

func bounds(_ element: AXUIElement) -> CGRect? {
    guard let position = attribute(element, kAXPositionAttribute), CFGetTypeID(position) == AXValueGetTypeID(),
          let size = attribute(element, kAXSizeAttribute), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(position, to: AXValue.self), .cgPoint, &point),
          AXValueGetValue(unsafeBitCast(size, to: AXValue.self), .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}

func geometry(_ rect: CGRect) -> [String: Double] {
    ["x": rect.origin.x, "y": rect.origin.y, "width": rect.width, "height": rect.height]
}

func summary(_ element: AXUIElement) -> [String: Any] {
    var result: [String: Any] = [:]
    for (key, name) in [("role", kAXRoleAttribute), ("subrole", kAXSubroleAttribute),
                        ("title", kAXTitleAttribute), ("identifier", kAXIdentifierAttribute),
                        ("description", kAXDescriptionAttribute)] {
        if let value = attribute(element, name) as? String { result[key] = String(value.prefix(1000)) }
    }
    let secure = (result["subrole"] as? String) == kAXSecureTextFieldSubrole
    result["secure"] = secure
    if !secure, let value = attribute(element, kAXValueAttribute) {
        if let string = value as? String {
            result["value"] = String(string.prefix(10000))
            result["value_truncated"] = string.count > 10000
        }
        else if let number = value as? NSNumber { result["value"] = number }
    }
    result["enabled"] = (attribute(element, kAXEnabledAttribute) as? Bool) ?? false
    if let rect = bounds(element) { result["bounds"] = geometry(rect) }
    var actions: CFArray?
    _ = AXUIElementCopyActionNames(element, &actions)
    var supported: [String] = []
    if (actions as? [String] ?? []).contains(kAXPressAction) { supported.append("press") }
    var settable: DarwinBoolean = false
    if !secure, AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable) == .success, settable.boolValue {
        supported.append("set_value")
    }
    result["actions"] = supported
    return result
}

func identity(_ app: NSRunningApplication) -> [String: Any] {
    ["app_id": "pid:\(app.processIdentifier)", "pid": Int(app.processIdentifier),
     "name": app.localizedName ?? "Application", "bundle_id": app.bundleIdentifier ?? "",
     "path": app.bundleURL?.path ?? "", "instance_id": "\(app.processIdentifier):\(app.launchDate?.timeIntervalSince1970 ?? 0)"]
}

func resolve(_ appID: String) throws -> NSRunningApplication {
    guard appID.hasPrefix("pid:"), let pid = Int32(appID.dropFirst(4)),
          let app = NSRunningApplication(processIdentifier: pid), !app.isTerminated else {
        throw fail("COMPUTER_APP_CHANGED", "Application is no longer running. List apps again.")
    }
    return app
}

func authorizedApp(_ args: [String: Any]) throws -> (NSRunningApplication, AXUIElement) {
    guard args["helper_id"] as? String == helperInstance else {
        throw fail("COMPUTER_SESSION_INACTIVE", "Native helper restarted. Request a new session.", "permission")
    }
    guard let expected = args["app"] as? [String: Any], let appID = expected["app_id"] as? String else {
        throw fail("INVALID_ARGUMENT", "App identity is required.", "validation")
    }
    let app = try resolve(appID)
    guard NSDictionary(dictionary: identity(app)).isEqual(to: expected) else {
        throw fail("COMPUTER_APP_CHANGED", "Application instance changed. Request a new session.")
    }
    guard AXIsProcessTrusted() else {
        throw fail("ACCESSIBILITY_PERMISSION_REQUIRED", "Allow Coding Tools MCP App Helper in System Settings > Privacy & Security > Accessibility.", "permission")
    }
    let root = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(root, 0.3)
    return (app, root)
}

struct WindowRef {
    let pid: pid_t
    let element: AXUIElement
}
struct ElementRef {
    let element: AXUIElement
    let observed: [String: Any]
}
struct SnapshotRef {
    let windowID: String
    let created: Date
    let windowBounds: CGRect?
    let elements: [String: ElementRef]
    var acted = false
}
var windowRefs: [String: WindowRef] = [:]
var snapshots: [String: SnapshotRef] = [:]

func window(_ id: String, _ app: NSRunningApplication, _ root: AXUIElement) throws -> AXUIElement {
    guard let entry = windowRefs[id], entry.pid == app.processIdentifier,
          let current = attribute(root, kAXWindowsAttribute) as? [AXUIElement],
          current.contains(where: { CFEqual($0, entry.element) }) else {
        throw fail("COMPUTER_WINDOW_STALE", "Window reference is stale. List windows again.")
    }
    return entry.element
}

func awaitCallback<T>(_ start: (@escaping (Result<T, Error>) -> Void) -> Void) throws -> T {
    let gate = NSLock()
    var result: Result<T, Error>?
    start { value in gate.lock(); result = value; gate.unlock() }
    let deadline = Date().addingTimeInterval(5)
    while Date() < deadline {
        gate.lock(); let current = result; gate.unlock()
        if let current { return try current.get() }
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    throw fail("COMPUTER_HELPER_FAILED", "Window capture timed out.")
}

func screenshot(_ element: AXUIElement, _ app: NSRunningApplication, _ maxDimension: Int) throws -> [String: Any] {
    guard CGPreflightScreenCaptureAccess() else {
        throw fail("SCREEN_RECORDING_PERMISSION_REQUIRED", "Allow Coding Tools MCP App Helper in System Settings > Privacy & Security > Screen Recording.", "permission")
    }
    guard #available(macOS 14.0, *) else {
        throw fail("UNSUPPORTED_PLATFORM", "Window screenshots require macOS 14 or later; accessibility-only snapshots remain available.")
    }
    guard let rect = bounds(element), rect.width > 0, rect.height > 0 else {
        throw fail("COMPUTER_WINDOW_STALE", "Window has no capturable bounds.")
    }
    if (attribute(element, kAXMinimizedAttribute) as? Bool) == true {
        throw fail("COMPUTER_WINDOW_UNAVAILABLE", "Minimized windows are not supported. Restore the window before capturing it.")
    }
    let content: SCShareableContent = try awaitCallback { done in
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false) { content, error in
            if let content { done(.success(content)) }
            else { done(.failure(error ?? fail("COMPUTER_HELPER_FAILED", "Could not enumerate capturable windows."))) }
        }
    }
    let title = attribute(element, kAXTitleAttribute) as? String ?? ""
    let matches = content.windows.filter { item in
        item.owningApplication?.processID == app.processIdentifier
        && abs(item.frame.minX - rect.minX) < 2 && abs(item.frame.minY - rect.minY) < 2
        && abs(item.frame.width - rect.width) < 2 && abs(item.frame.height - rect.height) < 2
        && (title.isEmpty || item.title == title)
    }
    guard matches.count == 1 else {
        throw fail("COMPUTER_WINDOW_UNAVAILABLE", "Could not uniquely match this accessibility window to a capture window.")
    }
    let config = SCStreamConfiguration()
    let scale = min(2.0, Double(maxDimension) / max(rect.width, rect.height))
    config.width = max(1, Int(rect.width * scale))
    config.height = max(1, Int(rect.height * scale))
    config.showsCursor = false
    let filter = SCContentFilter(desktopIndependentWindow: matches[0])
    let image: CGImage = try awaitCallback { done in
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { image, error in
            if let image { done(.success(image)) }
            else { done(.failure(error ?? fail("COMPUTER_HELPER_FAILED", "Could not capture window."))) }
        }
    }
    guard bounds(element) == rect else {
        throw fail("COMPUTER_SNAPSHOT_STALE", "Window moved while capturing. Observe it again.")
    }
    guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]), png.count <= 5 * 1024 * 1024 else {
        throw fail("OUTPUT_TOO_LARGE", "Screenshot exceeds the image budget. Request a smaller max_dimension.")
    }
    return ["_mcp_image_data": png.base64EncodedString(), "mime_type": "image/png", "bytes": png.count,
            "width": image.width, "height": image.height, "window_bounds": geometry(rect),
            "pixels_per_point_x": Double(image.width) / rect.width, "pixels_per_point_y": Double(image.height) / rect.height]
}

func referencedElement(_ args: [String: Any], _ targetWindow: AXUIElement, forAction: Bool) throws -> (String, ElementRef) {
    let snapshotID = try required(args, "snapshot_id")
    let windowID = try required(args, "window_id")
    let elementID = try required(args, "element_id")
    guard let snapshot = snapshots[snapshotID], snapshot.windowID == windowID,
          let element = snapshot.elements[elementID] else {
        throw fail("COMPUTER_SNAPSHOT_STALE", "Unknown snapshot or element. Observe the window again.")
    }
    guard attribute(element.element, kAXRoleAttribute) != nil else {
        throw fail("COMPUTER_SNAPSHOT_STALE", "Element no longer exists. Observe the window again.")
    }
    if forAction {
        guard !snapshot.acted, Date().timeIntervalSince(snapshot.created) <= 60,
              bounds(targetWindow) == snapshot.windowBounds else {
            throw fail("COMPUTER_SNAPSHOT_STALE", "Snapshot changed, expired, or was already used. Observe the window again.")
        }
        let current = summary(element.element)
        for key in ["role", "subrole", "identifier", "title", "value", "enabled", "bounds", "secure"] {
            let before = element.observed[key] as? NSObject
            let after = current[key] as? NSObject
            if before != after { throw fail("COMPUTER_SNAPSHOT_STALE", "Element changed since observation. Observe the window again.") }
        }
        guard current["enabled"] as? Bool == true, current["secure"] as? Bool != true, current["value_truncated"] as? Bool != true else {
            throw fail("COMPUTER_ACTION_UNSUPPORTED", "Element is disabled or is a secure text control.")
        }
    }
    return (snapshotID, element)
}

func handle(_ operation: String, _ args: [String: Any]) throws -> [String: Any] {
    switch operation {
    case "status":
        return ["platform": "macos", "accessibility": AXIsProcessTrusted(), "screen_recording": CGPreflightScreenCaptureAccess(),
                "operations": supportedOperations, "actions": ["press", "set_value"], "background": "accessibility_only", "global_input": false]
    case "list_apps":
        let query = (args["query"] as? String ?? "").lowercased()
        let limit = min(100, max(1, args["max_results"] as? Int ?? 30))
        let apps = NSWorkspace.shared.runningApplications.filter { !$0.isTerminated && $0.activationPolicy != .prohibited }
            .map(identity).filter { query.isEmpty || "\($0["name"] ?? "") \($0["bundle_id"] ?? "")".lowercased().contains(query) }
            .sorted { ($0["pid"] as? Int ?? 0) < ($1["pid"] as? Int ?? 0) }
        return ["apps": Array(apps.prefix(limit)), "count": min(limit, apps.count), "truncated": apps.count > limit]
    case "resolve_app":
        return ["app": identity(try resolve(required(args, "app_id")))]
    case "windows", "snapshot", "action", "inspect":
        let (app, root) = try authorizedApp(args)
        if operation == "windows" {
            let windows = attribute(root, kAXWindowsAttribute) as? [AXUIElement] ?? []
            if windowRefs.count > 100 { windowRefs.removeAll(); snapshots.removeAll() }
            var items: [[String: Any]] = []
            for element in windows.prefix(30) {
                let id = windowRefs.first(where: { $0.value.pid == app.processIdentifier && CFEqual($0.value.element, element) })?.key ?? UUID().uuidString
                windowRefs[id] = WindowRef(pid: app.processIdentifier, element: element)
                var item = summary(element); item["window_id"] = id; items.append(item)
            }
            return ["windows": items, "count": items.count, "truncated": windows.count > 30]
        }
        let windowID = try required(args, "window_id")
        let targetWindow = try window(windowID, app, root)
        if operation == "snapshot" {
            let limit = min(500, max(1, args["max_elements"] as? Int ?? 200))
            let depthLimit = min(20, max(1, args["max_depth"] as? Int ?? 8))
            let initialBounds = bounds(targetWindow)
            let deadline = Date().addingTimeInterval(4)
            var pending: [(AXUIElement, Int)] = [(targetWindow, 0)]
            var items: [[String: Any]] = []
            var references: [String: ElementRef] = [:]
            var textBytes = 0
            var depthTruncated = false
            while let (element, depth) = pending.popLast() {
                if items.count >= limit || Date() >= deadline { pending.append((element, depth)); break }
                var item = summary(element)
                textBytes += (try? JSONSerialization.data(withJSONObject: item).count) ?? 0
                if textBytes > 256 * 1024 { pending.append((element, depth)); break }
                let id = UUID().uuidString
                references[id] = ElementRef(element: element, observed: item)
                item["element_id"] = id; item["depth"] = depth; items.append(item)
                let descendants = children(element)
                if depth < depthLimit { pending.append(contentsOf: descendants.reversed().map { ($0, depth + 1) }) }
                else if !descendants.isEmpty { depthTruncated = true }
            }
            var result: [String: Any] = ["window_id": windowID, "elements": items, "truncated": !pending.isEmpty || depthTruncated]
            if args["include_image"] as? Bool ?? true {
                let dimension = min(2400, max(320, args["max_dimension"] as? Int ?? 1600))
                result.merge(try screenshot(targetWindow, app, dimension)) { _, new in new }
            }
            guard bounds(targetWindow) == initialBounds else { throw fail("COMPUTER_SNAPSHOT_STALE", "Window moved while observing. Observe again.") }
            let id = UUID().uuidString
            if snapshots.count >= 8, let oldest = snapshots.min(by: { $0.value.created < $1.value.created })?.key { snapshots.removeValue(forKey: oldest) }
            snapshots[id] = SnapshotRef(windowID: windowID, created: Date(), windowBounds: initialBounds, elements: references)
            result["snapshot_id"] = id; result["observed_at"] = Date().timeIntervalSince1970
            return result
        }
        let (snapshotID, reference) = try referencedElement(args, targetWindow, forAction: operation == "action")
        if operation == "inspect" { return ["element": summary(reference.element)] }
        let action = try required(args, "action")
        let available = summary(reference.element)["actions"] as? [String] ?? []
        guard available.contains(action) else { throw fail("COMPUTER_ACTION_UNSUPPORTED", "Element does not support this action. No foreground fallback was attempted.") }
        // Invalidate before submitting: ambiguous native errors must not allow replay.
        snapshots[snapshotID]?.acted = true
        let status: AXError
        if action == "press" { status = AXUIElementPerformAction(reference.element, kAXPressAction as CFString) }
        else if action == "set_value", let value = args["value"] as? String, value.count <= 10000 {
            status = AXUIElementSetAttributeValue(reference.element, kAXValueAttribute as CFString, value as CFString)
        } else { throw fail("INVALID_ARGUMENT", "Invalid element action.", "validation") }
        guard status == .success else { throw fail("COMPUTER_ACTION_FAILED", "App did not confirm the action. Inspect its state before another action.") }
        return ["status": "submitted", "method": action == "press" ? "AXPress" : "AXValue", "window_id": windowID]
    default:
        throw fail("INVALID_ARGUMENT", "Unknown native operation.", "validation")
    }
}

// Only the human-facing desktop command invokes this branch. MCP never forwards it.
if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--request-permission" {
    switch CommandLine.arguments[2] {
    case "accessibility":
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(options)
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") { NSWorkspace.shared.open(url) }
    case "screen_recording":
        _ = CGRequestScreenCaptureAccess()
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") { NSWorkspace.shared.open(url) }
    default: exit(2)
    }
    exit(0)
}

while let line = readLine() {
    var requestID = ""
    var result: [String: Any]
    do {
        guard line.utf8.count <= 128 * 1024, let data = line.data(using: .utf8),
              let request = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              request["version"] as? Int == protocolVersion,
              let id = request["id"] as? String, let operation = request["operation"] as? String,
              let args = request["arguments"] as? [String: Any] else {
            throw fail("INVALID_ARGUMENT", "Invalid native protocol request.", "validation")
        }
        requestID = id
        // NSWorkspace's running-app list is maintained by main-run-loop events.
        // stdin is blocking between requests, so drain those events before observing.
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
        result = try handle(operation, args)
        result["ok"] = true
    } catch let error as Failure {
        result = ["ok": false, "error": ["code": error.code, "message": error.message, "category": error.category, "retryable": false]]
    } catch {
        result = ["ok": false, "error": ["code": "COMPUTER_HELPER_FAILED", "message": "Native operation failed.", "category": "runtime", "retryable": false]]
    }
    result["id"] = requestID; result["version"] = protocolVersion; result["helper_instance_id"] = helperInstance
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10]))
    }
}
