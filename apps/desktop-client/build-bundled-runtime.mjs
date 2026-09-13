// Keep the build:runtime entry point, but ship only our Python code.
// Interpreters and third-party dependencies live on the user's machine.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const output = join(root, "apps/desktop-client/src-tauri/resources/runtime");
// setuptools may otherwise reuse stale build/lib or egg-info entries from an
// earlier source tree and silently repackage files that have since been deleted.
rmSync(join(root, "build"), { recursive: true, force: true });
rmSync(join(root, "coding_tools_mcp.egg-info"), { recursive: true, force: true });
rmSync(output, { recursive: true, force: true });
mkdirSync(output, { recursive: true });
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
console.log(`Runtime source payload: ${bytes} bytes; no Python or Node binaries.`);
