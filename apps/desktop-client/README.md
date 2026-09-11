# Coding Tools MCP Desktop

The desktop client is a Tauri 2 application with a React/Vite webview and a
small Rust control plane. The Python MCP runtime remains independent and is
started as a child process; the webview never receives a general-purpose shell
API.

## Features

- Compact menu-bar panel with no macOS Dock icon
- Multiple workspace profiles with one automatically assigned local port per workspace
- One-click OAuth runtime startup through Cloudflare Quick Tunnels
- A simplified per-workspace access menu: **Standard** maps to the workspace-confined
  trusted runtime, while **Full Access** maps to host mode for agent workflows that
  intentionally need the wider Mac environment. Legacy safe/dangerous profiles remain
  readable until the user explicitly chooses one of the two desktop modes.
- Live runtime/tunnel status with copyable Server URL and OAuth authorization passcode
- Secrets stored in the operating-system keychain
- English and Simplified Chinese UI, including the native menu-bar menu with a persisted language selector
- Native process-group cleanup when the app exits
- Project context, checks/evidence, tasks, checkpoints, LSP, structured Git,
  reviews, approvals, managed-worktree registration, and extended browser
  actions are enabled for desktop-launched runtimes
- Native workspace menus surface pending approval decisions only when user action is
  required; routine task/check/checkpoint/review activity stays out of the tray menu.
  Runtime logs remain directly accessible without requiring a separate webview window.

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
in `iihciyekub/homebrew-tap`.

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

Release bundles contain only this project's pure-Python wheel and a production
requirements file exported from `uv.lock`, including dependency hashes. They do
not contain Python, Playwright's Node driver, Chromium, or a frozen native host.
Old generated PyInstaller resources are excluded by the explicit resource map.

At startup, the client checks an explicit `CODING_TOOLS_MCP_DESKTOP_RUNTIME`
override, a PATH `coding-tools-mcp` with the same core version, then a matching
Python installation with the required imports. Otherwise it prepares an isolated
environment in the app's local data directory under `runtimes/<version-digest>`.
Each workspace uses the same prepared environment; system packages are not changed.

On macOS, no prerequisite package manager is required. If neither a compatible
Python nor `uv` is available, the app downloads the pinned `uv` release into its
private application-data directory after verifying its SHA-256 digest. If a
compatible Python 3.11+ with venv/pip already exists, the app can use it instead.
Chrome itself must already be installed; no browser is downloaded.

The first preparation may take several minutes and requires internet access for
uncached dependencies. Dependency downloads use locked versions and hash checks.
Preparation has a separate five-minute deadline and writes `setup.log` alongside
the workspace logs. Failures can be retried; `.ready` is written only after a
successful health check. An OS file lock prevents concurrent installers. Status
queries remain responsive during preparation; **Cancel startup** stops the
preparation process and prevents a delayed server launch. A
prepared environment is reused without uv or network access; a removed base
Python may require repair. The server's normal 20-second startup deadline begins
after preparation. Old versioned environments are retained so existing Native
Messaging registrations remain valid; they are not included in app updates.

For Cloudflare Quick Tunnel profiles, a missing `cloudflared` is handled the same
way on macOS: Desktop 0.3.20 downloads the pinned, SHA-256-verified release into
the app-owned tools directory. The native menu-bar **Resources** menu presents three
user-facing health summaries—Runtime, Browser, and App Control—plus the normal repair,
Chrome integration, and macOS permission actions. Low-level Playwright, uv,
cloudflared, App Helper, CDP, and bridge details remain available under
**Diagnostics** rather than crowding the main menu. Homebrew, `/usr/local`, and system
Python are not modified. Other desktop platforms currently use an existing
platform-installed Python/uv and cloudflared.

Native Messaging uses the adjacent Python console entry point, or creates a small
launcher pinned to the current interpreter/import root for module-only installs.
After switching Python environments, run `chrome_extension_install` again to
refresh Chrome's registration.

Desktop 0.3.20 keeps detailed dependency readiness under **Resources → Diagnostics**:
MCP Runtime/core version, Playwright/version, uv, cloudflared, App Helper, installed
Chrome, Chrome CDP availability, Native Messaging manifest/bridge connection,
Accessibility permission and Screen Recording permission. **Prepare Chrome
integration** installs the unpacked extension files and Native Messaging manifest
using the prepared private runtime and opens `chrome://extensions`. Stable Chrome
still requires the user to enable Developer mode and choose **Load unpacked** once;
the app cannot bypass that browser policy. The compact **macOS Permissions** submenu
opens/requests Accessibility and Screen Recording access for the bundled App Helper.

macOS application automation is routed through the bundled **Coding Tools MCP App
Helper** when the runtime is launched by the desktop app. The helper is a small
native executable signed with the release and keeps Accessibility/CGEvent
permission attached to a stable code identity rather than a versioned Python
environment. `app_accessibility(open_settings=true)` requests/opens Accessibility
settings for the helper. Window screenshots can separately require Screen
Recording permission.

For a Developer ID release, set `CODING_TOOLS_MCP_SIGNING_IDENTITY` to the same
Developer ID Application identity passed to Tauri. `build:runtime` signs the
native App Helper with Hardened Runtime before Tauri seals it into the signed app
bundle, keeping the helper's TCC identity stable across runtime updates.

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
- Configuration is public metadata in `~/.coding-tools-mcp-desktop/profiles.json`;
  credentials are stored in Keychain/Credential Manager/Secret Service.
