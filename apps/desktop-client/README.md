# Coding Tools MCP Desktop

The desktop client is a Tauri 2 application with a React/Vite webview and a
small Rust control plane. The Python MCP runtime remains independent and is
started as a child process; the webview never receives a general-purpose shell
API.

## Features

- Multiple workspace profiles with one fixed local port per workspace
- Safe, trusted, and dangerous MCP permission modes
- OAuth and bearer-token authentication
- Cloudflare quick and named tunnels, plus externally managed FRP
- Live runtime/tunnel status and bounded logs
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
- `uvx` or `coding-tools-mcp` on the login-shell PATH
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

On Apple Silicon, install native tools through `/opt/homebrew`; the runtime
resolver deliberately prefers `/opt/homebrew/bin/cloudflared` over an older
Rosetta `/usr/local/bin/cloudflared`.

## ChatGPT on the web

ChatGPT cannot reach the local `127.0.0.1` URL on your Mac. Start the workspace
and wait for the Cloudflare tunnel to become healthy, then use the top-right
**Copy MCP** button. It copies the public HTTPS endpoint ending in `/mcp` and
stays disabled until that endpoint exists. **Copy auth code** copies the OAuth
authorization password (or the bearer token for a bearer profile).

A Quick Tunnel is suitable for personal/developer testing while this app stays
open; its hostname changes after restart. Use a named Cloudflare tunnel and a
stable HTTPS hostname for a durable ChatGPT connection or production use.

For example, to use `mcp.iiaide.com` with local port `28767`:

1. In Cloudflare, open **Networking → Tunnels**, create/select a remotely
   managed tunnel, and add a **Published application** route.
2. Set the hostname to `mcp.iiaide.com` and the service URL to
   `http://127.0.0.1:28767`.
3. Choose **Add a replica** and copy only the `eyJ…` token from the generated
   `cloudflared` installation command.
4. In this app choose **Cloudflare → Named tunnel (recommended)**, enter
   `https://mcp.iiaide.com` and that Tunnel Token, then save and start.
5. When the status is Running, **Copy MCP** returns
   `https://mcp.iiaide.com/mcp` for ChatGPT on the web.

## Security model

- The webview can call only the commands declared in `src-tauri/src/lib.rs`.
- There is no arbitrary command field or shell command bridge.
- The MCP server always binds to `127.0.0.1`.
- Public access is handled by an authenticated tunnel.
- NoAuth remains available in the core CLI for loopback-only development, but
  is intentionally excluded from this public-tunnel desktop client.
- Runtime and tunnel processes are placed in dedicated process groups and are
  stopped together on app exit.
- Configuration is public metadata in `~/.coding-tools-mcp-desktop/profiles.json`;
  credentials are stored in Keychain/Credential Manager/Secret Service.
