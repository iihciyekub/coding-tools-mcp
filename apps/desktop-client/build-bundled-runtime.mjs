// Keep the build:runtime entry point, but ship only our Python code.
// Interpreters and third-party dependencies live on the user's machine.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const output = join(root, "apps/desktop-client/src-tauri/resources/runtime");
const helpers = join(root, "apps/desktop-client/src-tauri/resources/helpers");
rmSync(output, { recursive: true, force: true });
rmSync(helpers, { recursive: true, force: true });
mkdirSync(output, { recursive: true });
mkdirSync(helpers, { recursive: true });
function uv(args) {
  const result = spawnSync("uv", args, {
    cwd: root, stdio: "inherit",
    env: { ...process.env, SOURCE_DATE_EPOCH: process.env.SOURCE_DATE_EPOCH || "315532800" },
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
uv(["build", "--wheel", "--out-dir", output]);
uv(["export", "--frozen", "--no-dev", "--no-emit-project", "--no-editable",
  "--no-header", "--no-annotate", "--output-file", join(output, "requirements.txt")]);
const wheels = readdirSync(output).filter((name) => name.endsWith(".whl"));
if (wheels.length !== 1 || !/^coding_tools_mcp-.+-py3-none-any\.whl$/.test(wheels[0])) {
  throw new Error("Expected exactly one pure-Python Coding Tools MCP wheel.");
}
const wheel = wheels[0];
const version = wheel.split("-")[1];
const digest = createHash("sha256")
  .update(readFileSync(join(output, wheel)))
  .update(readFileSync(join(output, "requirements.txt")))
  .digest("hex");
writeFileSync(join(output, "manifest.json"), JSON.stringify({
  version, wheel, environment_key: `${version}-${digest.slice(0, 20)}`,
}, null, 2) + "\n");
const bytes = readdirSync(output).reduce((sum, name) => sum + statSync(join(output, name)).size, 0);
if (bytes > 2 * 1024 * 1024) throw new Error(`Runtime source payload exceeded 2 MiB: ${bytes}`);
if (process.platform === "darwin") {
  const helperSource = join(root, "apps/desktop-client/macos-app-helper.swift");
  const helperOutput = join(helpers, "coding-tools-mcp-app-helper");
  const result = spawnSync("xcrun", [
    "swiftc", "-O", helperSource,
    "-framework", "AppKit",
    "-framework", "ApplicationServices",
    "-framework", "CoreGraphics",
    "-o", helperOutput,
  ], { cwd: root, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
  const signingIdentity = process.env.CODING_TOOLS_MCP_SIGNING_IDENTITY?.trim();
  if (signingIdentity) {
    const signed = spawnSync("/usr/bin/codesign", [
      "--force",
      "--sign", signingIdentity,
      "--options", "runtime",
      "--timestamp",
      helperOutput,
    ], { cwd: root, stdio: "inherit" });
    if (signed.error) throw signed.error;
    if (signed.status !== 0) process.exit(signed.status ?? 1);
  }
  const helperBytes = statSync(helperOutput).size;
  if (helperBytes > 2 * 1024 * 1024) throw new Error(`macOS app helper exceeded 2 MiB: ${helperBytes}`);
  console.log(`macOS app helper: ${helperBytes} bytes.`);
}
console.log(`Runtime source payload: ${bytes} bytes; no Python, Node or browser binaries.`);
