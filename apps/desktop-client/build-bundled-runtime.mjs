import { spawnSync } from "node:child_process";
import { chmodSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const resources = join(here, "src-tauri", "resources");
const buildRoot = join(root, ".build", "pyinstaller");
const executableSuffix = process.platform === "win32" ? ".exe" : "";

mkdirSync(resources, { recursive: true });
mkdirSync(buildRoot, { recursive: true });

for (const name of ["coding-tools-mcp-runtime", "coding-tools-mcp-chrome-host"]) {
  rmSync(join(resources, `${name}${executableSuffix}`), { force: true, recursive: true });
}

function pyinstaller(name, entry, mode, extra = []) {
  const result = spawnSync(
    "uv",
    [
      "run",
      "--with",
      "pyinstaller>=6,<7",
      "pyinstaller",
      "--noconfirm",
      "--clean",
      mode,
      "--name",
      name,
      "--distpath",
      resources,
      "--workpath",
      join(buildRoot, `${name}-work`),
      "--specpath",
      buildRoot,
      ...extra,
      entry,
    ],
    { cwd: root, stdio: "inherit" },
  );
  if (result.status !== 0) process.exit(result.status ?? 1);
  if (process.platform !== "win32") {
    const executable = mode === "--onedir" ? join(resources, name, name) : join(resources, name);
    chmodSync(executable, 0o755);
  }
}

pyinstaller("coding-tools-mcp-runtime", "coding_tools_mcp/frozen_main.py", "--onedir", [
  "--collect-all",
  "playwright",
  "--collect-data",
  "coding_tools_mcp",
  "--hidden-import",
  "playwright.sync_api",
]);

if (process.platform === "darwin") {
  // Native Messaging keeps stdio attached to this process. Use a tiny wrapper
  // that execs the already bundled/signed runtime instead of shipping a second
  // Python bundle. This avoids duplicate libpython resources and PyInstaller
  // onefile extraction under Hardened Runtime.
  const host = join(resources, "coding-tools-mcp-chrome-host");
  writeFileSync(
    host,
    [
      "#!/bin/sh",
      'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
      'exec "$SCRIPT_DIR/coding-tools-mcp-runtime/coding-tools-mcp-runtime" --chrome-native-host',
      "",
    ].join("\n"),
  );
  chmodSync(host, 0o755);
} else {
  pyinstaller("coding-tools-mcp-chrome-host", "coding_tools_mcp/chrome_native_host.py", "--onefile");
}
