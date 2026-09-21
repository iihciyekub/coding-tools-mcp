import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, "src-tauri/resources/computer");
mkdirSync(output, { recursive: true });
writeFileSync(join(output, "capability.json"), JSON.stringify({ protocol_version: 1, platform: process.platform, supported: process.platform === "darwin" }) + "\n");
if (process.platform === "darwin") {
  const bundle = join(output, "Coding Tools MCP App Helper.app/Contents");
  mkdirSync(join(bundle, "MacOS"), { recursive: true });
  writeFileSync(join(bundle, "Info.plist"), `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.codingtoolsmcp.desktop.app-helper</string>
<key>CFBundleName</key><string>Coding Tools MCP App Helper</string>
<key>CFBundleExecutable</key><string>coding-tools-computer-helper</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>\n`);
  const target = process.env.TAURI_ENV_TARGET_TRIPLE?.startsWith("x86_64") ? "x86_64" : process.arch === "arm64" ? "arm64" : "x86_64";
  const result = spawnSync("xcrun", ["swiftc", "-swift-version", "5", "-O", "-target", `${target}-apple-macosx12.3`,
    join(root, "src-tauri/computer-helper.swift"), "-o", join(bundle, "MacOS/coding-tools-computer-helper"),
    "-framework", "AppKit", "-framework", "ApplicationServices", "-framework", "ScreenCaptureKit"], { stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
  // Developer ID signing is performed inside-out by the release workflow.
  console.log("Built native computer helper (protocol v1).");
}
