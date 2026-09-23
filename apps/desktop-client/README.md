# Coding Tools MCP Desktop

The desktop client is a Tauri 2 application with a React/Vite webview and a
small Rust control plane. The Python MCP runtime remains independent and is
started as a child process; the webview never receives a general-purpose shell
API.

## Features

- Compact menu-bar panel with no macOS Dock icon
- Multiple workspace profiles with one automatically assigned local port per workspace
- Mode-first profile creation: both Standard and Full Access ask for a workspace
  folder so project context is always explicit. Full Access does not synthesize a
  folder allowlist: host-mode file tools and commands can use the host filesystem.
- One-click OAuth runtime startup through Cloudflare Quick Tunnels
- A simplified per-workspace access menu: **Standard** maps to the workspace-confined
  trusted runtime, while **Full Access** maps to host mode for agent workflows that
  intentionally need the wider Mac environment. Desktop profiles use only these two modes.
- **Allowed folders** apply to Standard access: ordinary file tools and `apply_patch`
  can use the workspace plus explicitly selected folders. In Full Access, file tools
  and host commands can use the host filesystem; relative paths still resolve from
  the workspace. Git evidence, LSP, check discovery, project instructions, and project
  context stay anchored to the selected workspace rather than following arbitrary host paths.
- Workspace settings expose a separate default search folder for `list_files` and
  `search_text` when no path is supplied. Full Access defaults to the user's home
  directory, including existing profiles that never chose a search folder; Standard
  access defaults to the project. It can be changed to the project, an allowed folder,
  or (in Full Access) another chosen folder; it never changes the
  underlying MCP access permissions. Whole-Mac searches require an explicit path.
- **Full Access** launches host mode with SSH/Git credentials and compatibility
  annotations enabled for clients that otherwise refuse command tools. On macOS,
  the launcher also recovers `SSH_AUTH_SOCK` from the GUI environment, login shell,
  or launchd so desktop-started runtimes match terminal SSH behavior more closely.
- Stable per-workspace accent backgrounds and in-place second-click confirmation for
  Stop and Quit, so multiple workspaces are easier to distinguish and accidental
  shutdowns are less likely.
- Live runtime/tunnel status with copyable Server URL and OAuth authorization passcode
- Secrets stored in the operating-system keychain
- English and Simplified Chinese UI, including the fixed menu-bar panel with a persisted language selector
- Native process-group cleanup when the app exits
- Desktop-launched runtimes expose the same small direct primitive catalog as
  other clients, plus gateway-scoped `project_context`. Planning, review, task,
  checkpoint, and Git-write workflows stay with the calling model and native CLIs.
- The shared Gateway automatically discovers installed Skills in known Codex and
  other agent directories. Additional directories can be added in settings, and
  automatic discovery can be switched off. ChatGPT can search their metadata and read `SKILL.md` files; local plugin
  actions and hooks do not become callable without separate MCP tools.
- The compact panel surfaces pending approval decisions only when user action is
  required. Runtime and installation logs remain directly accessible in the same panel.

The first launch imports the previous PySide client's `profiles.json`. Legacy
`secrets.json` values are moved into the system keychain and the plaintext file
is removed after a successful migration.

## Install with Homebrew

Apple Silicon releases are published as signed and notarized DMGs in the
`iihciyekub/coding-tools-mcp` fork. Install the current desktop release with:

```bash
brew install --cask iihciyekub/tap/coding-tools-mcp
```

Desktop releases use `desktop-v<version>` tags so they remain independent from
the Python package's `v<version>` release series. The Homebrew Cask is maintained
in `iihciyekub/homebrew-tap`. Maintainers changing release naming, signing,
notarization, GitHub Release assets, or Homebrew publishing must follow the
[desktop release maintenance contract](../../docs/desktop-release-maintenance.md).

When Windows is included in a version's release platforms, its x64 desktop build is attached to the same GitHub Release as
`Coding-Tools-MCP-<version>-win-x64-portable.zip`. The ZIP is installation-free:
extract it, then run `Coding Tools MCP.exe`. The executable and bundled runtime
source payload are portable, while profiles, OS credentials, downloaded tools,
and managed Python environments continue to use normal per-user application-data
locations.

## Automated desktop releases

`.github/workflows/desktop-release.yml` is the canonical desktop release
pipeline. For a new version, push a version-matched tag such as:

```bash
git tag desktop-v0.5.1
git push origin desktop-v0.5.1
```

Pushing the tag automatically starts the complete release workflow. To release
or retry an **existing** tag, use the standard maintainer/agent command:

```bash
gh workflow run desktop-release.yml \
  -R iihciyekub/coding-tools-mcp \
  --ref iiaide \
  -f tag=desktop-v0.5.1 \
  -f mode=release
```

Watch the run until it finishes; a Desktop release is complete only after the
signed/notarized DMG is published to GitHub Release and the Homebrew Cask update
and install verification succeed. The detailed operator/agent procedure is in
the [desktop release maintenance contract](../../docs/desktop-release-maintenance.md).

The workflow validates the tag against the desktop metadata, builds the Apple
Silicon app on an arm64 macOS runner, imports the Developer ID certificate,
signs and notarizes both the app and final DMG, builds the Windows x64 portable
ZIP when selected by `releasePlatforms` in `package.json`, publishes immutable GitHub Release assets and checksums, then updates the
Homebrew Tap.

Version 0.5.1 selects macOS only. Existing Windows releases remain available.

The macOS release job uses the protected `production-release` GitHub
Environment, matching the production signing model used by WOS Aide. Configure
these environment secrets:

```text
MAC_CSC_P12_BASE64
MAC_CSC_KEY_PASSWORD
APPLE_API_KEY_P8_BASE64
APPLE_API_KEY_ID
APPLE_API_ISSUER
```

`MAC_CSC_P12_BASE64` is the base64 representation of the exported Developer ID
Application `.p12` including its private key. The three `APPLE_API_*` values
come from an App Store Connect Team API Key suitable for `notarytool`. The
environment variable `MAC_CSC_NAME` identifies the expected Developer ID
identity and defaults to `Yongjian Li (2NLAH5MYH8)`.

The repository-level `HOMEBREW_TAP_DEPLOY_KEY` secret remains separate and is
used only to update `iihciyekub/homebrew-tap` after the immutable GitHub Release
has been published.

Configure the Apple environment without committing credentials:

```bash
scripts/setup-desktop-release-secrets.sh \
  /secure/coding-tools-developer-id.p12 \
  /secure/AuthKey_XXXXXXXXXX.p8 \
  APPLE_KEY_ID \
  APPLE_ISSUER_ID
```

The helper securely prompts for the `.p12` password unless
`MAC_CSC_KEY_PASSWORD` is already set. A production release now fails explicitly
when any signing/notarization secret is missing; it never reports success after
silently skipping the DMG build.

## Development

Requirements:

- Node.js 24+
- Rust 1.84+
- `uv` for building the small Python source wheel and exporting locked dependencies
- `cloudflared` for Cloudflare profiles

```bash
cd apps/desktop-client
npm install
npm run tauri dev
```

Frontend and Rust checks:

```bash
make desktop-check
```

Build the native application bundle/installer:

```bash
make desktop-build
```

For an isolated macOS development app that keeps separate settings and keychain
items from the stable installation, run `npm run build:dev-app`. It produces
`coding-tools-mcp-dev.app` with a separate bundle identifier. Sign and install
the local app using a valid developer certificate; this is not a public desktop
release or a replacement for the signed/notarized release workflow.

Release bundles contain only this project's pure-Python wheel and a production
requirements file exported from `uv.lock`, including dependency hashes. They do
not contain Python or Node runtimes. Old generated PyInstaller resources are
excluded by the explicit resource map.

At startup, the client checks an explicit `CODING_TOOLS_MCP_DESKTOP_RUNTIME`
override, a PATH `coding-tools-mcp` with the same core version, then a matching
Python installation with the required imports. Otherwise it prepares an isolated
environment in the app's local data directory under `runtimes/<version-digest>`.
Each workspace uses the same prepared environment; system packages are not changed.

On macOS, no prerequisite package manager is required. If neither a compatible
Python nor `uv` is available, the app downloads the pinned `uv` release into its
private application-data directory after verifying its SHA-256 digest. If a
compatible Python 3.11+ with venv/pip already exists, the app can use it instead.

The first preparation may take several minutes and requires internet access for
uncached dependencies. Dependency downloads use locked versions and hash checks.
Preparation has a separate five-minute deadline and writes `setup.log` alongside
the workspace logs. Failures can be retried; `.ready` is written only after a
successful health check. An OS file lock prevents concurrent installers. Status
queries remain responsive during preparation; **Cancel startup** stops the
preparation process and prevents a delayed server launch. A
prepared environment is reused without uv or network access; a removed base
Python may require repair. The server's normal 20-second startup deadline begins
after preparation. Old versioned environments are retained for rollback/reuse;
they are not included in app updates.

For Cloudflare Quick Tunnel profiles, a missing `cloudflared` is handled the same
way on macOS: Desktop 0.3.25 downloads the pinned, SHA-256-verified release into
the app-owned tools directory. The menu-bar **Environment & setup** page focuses on
the coding runtime itself: MCP Runtime/core version, uv, and cloudflared readiness,
plus prepare/repair actions. Homebrew, `/usr/local`, and system Python are not
modified. Other desktop platforms currently use an existing platform-installed
Python/uv and cloudflared.

Developer ID signing and notarization for stable releases are handled by the
canonical `desktop-release.yml` workflow. Local signing remains a maintainer
fallback only; do not publish an unsigned or unnotarized DMG as a stable release.

The explicit runtime override remains user-managed and bypasses version checks.
A PATH installation of a different core version is skipped instead of silently
running older code. Development uses the source wheel too: run `npm run
build:runtime` after changing the Python core. Python CLI users can continue using
the source checkout directly.

On Apple Silicon, install native tools through `/opt/homebrew`; the runtime
resolver deliberately prefers `/opt/homebrew/bin/cloudflared` over an older
Rosetta `/usr/local/bin/cloudflared`.

## ChatGPT on the web

ChatGPT cannot reach the local `127.0.0.1` URL on your Mac. Click the menu-bar
icon, add a workspace folder, and start it. The app creates a Cloudflare Quick
Tunnel automatically. When the status is healthy, **Server URL** copies the
temporary public HTTPS endpoint ending in `/mcp`, and **Authorization passcode**
copies the OAuth authorization passcode. Starting a workspace also copies its
Server URL to the clipboard automatically.

The temporary hostname changes whenever the workspace tunnel restarts. Update
the custom MCP app address in ChatGPT after starting a new tunnel.

Installed Skills in known Codex, Claude Code, Cursor, Gemini CLI, Agents, and
OpenCode folders are found automatically; the app checks these locations without
scanning the entire home directory. Workspace settings lists the detected roots,
lets you add other specific folders, and lets you disable automatic discovery.
Shared settings can be saved while the Gateway is running and take effect when it
next starts. Then refresh the MCP connection metadata in ChatGPT developer mode
and start a new conversation. The optional catalog tools let ChatGPT search and
read those files; they do not activate Codex-only plugin scripts or hooks.

## Security model

- The webview can call only the commands declared in `src-tauri/src/lib.rs`.
- There is no arbitrary command field or shell command bridge.
- The MCP server always binds to `127.0.0.1`.
- Public access is handled by an authenticated tunnel.
- NoAuth remains available in the core CLI for loopback-only development, but
  is intentionally excluded from this public-tunnel desktop client.
- Full host access is opt-in per stopped workspace. It keeps OAuth enabled but
  allows authenticated MCP commands to act with the desktop user's authority.
- Runtime and tunnel processes are placed in dedicated process groups and are
  stopped together on app exit.
- Configuration and environment-variable names are public metadata in
  `~/.coding-tools-mcp-desktop/profiles.json`; credential and environment-variable
  values are stored in Keychain/Credential Manager/Secret Service.
