// Disposable native integration fixture; contains no user data.
import AppKit

final class Fixture: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let status = NSTextField(labelWithString: "Not pressed")
    let focus = NSTextField(labelWithString: "Background")
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 420, height: 220), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Coding Tools MCP Test Fixture"
        let input = NSTextField(string: "before")
        input.frame = NSRect(x: 24, y: 150, width: 360, height: 28)
        input.setAccessibilityIdentifier("fixture-input")
        let button = NSButton(title: "Test press", target: self, action: #selector(press))
        button.frame = NSRect(x: 24, y: 95, width: 160, height: 32)
        button.setAccessibilityIdentifier("fixture-button")
        status.frame = NSRect(x: 24, y: 45, width: 360, height: 28)
        status.setAccessibilityIdentifier("fixture-status")
        focus.frame = NSRect(x: 24, y: 15, width: 360, height: 24)
        focus.setAccessibilityIdentifier("fixture-focus")
        for view in [input, button, status, focus] { window.contentView!.addSubview(view) }
        window.orderFrontRegardless()
    }
    @objc func press() {
        status.stringValue = "Pressed successfully"
        focus.stringValue = NSApp.isActive ? "Foreground" : "Background"
    }
}

let app = NSApplication.shared
let fixture = Fixture()
app.delegate = fixture
app.setActivationPolicy(.accessory)
app.run()
