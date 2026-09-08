# Coding Tools MCP Desktop

The desktop client is a Tauri 2 application with a React/Vite webview and a
small Rust control plane. The Python MCP runtime remains independent and is
started as a child process; the webview never receives a general-purpose shell
API.

## Features

- Compact menu-bar panel with no macOS Dock icon
- Multiple workspace profiles with one automatically assigned local port per workspace
- One-click OAuth runtime startup through Cloudflare Quick Tunnels
- Per-workspace Safe, Trusted, Dangerous, and Host permission-mode menus
- Live runtime/tunnel status with copyable Server URL and OAuth authorization passcode
- Secrets stored in the operating-system keychain
- English and Simplified Chinese UI
- Native process-group cleanup when the app exits

The first launch imports the previous PySide client's `profiles.json`. Legacy
`secrets.json` values are moved into the system keychain and the plaintext file
is removed after a successful migration.

## Development

Requirements:

- Node.js 24+
- Rust 1.84+
- `uv` for building the bundled Python runtime
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

Release bundles include self-contained `coding-tools-mcp-runtime` and
`coding-tools-mcp-chrome-host` executables under the app resources. At runtime
the desktop client prefers that bundled MCP binary, then falls back to an
explicit override, PATH installation, source checkout, or `uvx` only when the
bundled resource is unavailable.

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
