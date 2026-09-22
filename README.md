# Coding Tools MCP

**English** | [简体中文](README.zh-CN.md)

> Give any AI chat or agent a safe pair of hands on your codebase.

[![PyPI](https://img.shields.io/pypi/v/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![npm](https://img.shields.io/npm/v/coding-tools-mcp)](https://www.npmjs.com/package/coding-tools-mcp)
[![Python](https://img.shields.io/pypi/pyversions/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![compliance](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml)
[![release](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Coding Tools MCP is a **macOS-first, model-neutral coding runtime** served over the
[Model Context Protocol](https://modelcontextprotocol.io): file reading and
search, structured multi-file patches, command execution, interactive
sessions, and git — one server that any MCP client can drive. Claude Desktop,
Claude Code, Codex, Cursor, Cline, VS Code, Windsurf, Gemini CLI, or an agent
you build yourself all get the same stable default tool catalog, workspace-confined
by default and gated by explicit permission modes.

[![Watch the demo](https://img.youtube.com/vi/N9lQaXt1eqQ/maxresdefault.jpg)](https://youtu.be/N9lQaXt1eqQ?si=LyEwvzzQF6QjUxR0)

## Why people use it

- **It turns a chat app into a coding agent.** Claude Desktop — or any MCP
  chat client — gets real repo access with the subscription you already have.
  No extra product required.
- **Safety is the product, not an afterthought.** One workspace root per
  server. Absolute paths, `..` traversal, and symlink escapes are rejected.
  Permission modes gate network access, shell expansion, inline scripts, and
  destructive commands. On Linux, [Landlock](docs/security-boundary.md) adds
  kernel-level filesystem confinement. Explicit `host` mode opts command
  execution out of that boundary for full local development.
- **It stays below the agent layer.** The runtime exposes coding primitives;
  planning, review state, task tracking, Skills, and agent orchestration stay
  with the calling model/client.
- **It is engineered for context windows.** Results are summarized, paginated,
  and capped by design; serialized tool-result bytes dropped 37%
  release-over-release on the deterministic dogfood workload with unchanged
  task completion.

## Quickstart

Run it with whichever toolchain you already have (the server is Python ≥ 3.11
from PyPI; the npm package is a thin launcher that starts it via `uv` or
`pipx`):

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo   # Python toolchain
npx coding-tools-mcp --stdio --workspace /path/to/repo   # Node toolchain
```

Wire it into Claude Desktop, Claude Code, Codex, Cursor, VS Code, Windsurf,
Gemini CLI, or Cline — the JSON is the same everywhere (swap `uvx` for `npx`
if you prefer Node):

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

Then ask your client: *"run the test suite and fix the first failure."*

The complete tool set is deliberately small and exposed directly. Helpers such as
`runtime_doctor`, project overview/instructions, check discovery, code diagnostics,
and Git history need no secondary discovery call. There is no separate workflow
engine.

Prefer HTTP? Drop `--stdio` and the server speaks Streamable HTTP on
`http://127.0.0.1:8765/mcp`. Both protocol eras are served on either
transport: MCP `2026-07-28` plus the handshake era `2025-11-25` with
`2025-06-18` compatibility. The persistent Project Gateway uses MCP sessions
only to bind a connection to an explicit project; ordinary single-workspace
runtimes remain stateless at the transport layer. A one-line installer, per-client
walkthroughs, and troubleshooting live in
[docs/quickstart.md](docs/quickstart.md) and
[docs/mcp-client-config.md](docs/mcp-client-config.md).

## Seven things to try

**1. Make Claude Desktop your coding agent.** The config above is all it
takes — the chat window you already pay for can now read, patch, test, and
commit-review a real repository.

**2. Code on your own machine from anywhere.**

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer ./integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

Loopback bind + authenticated HTTPS tunnel (`cloudflared`, `ngrok`, or
Microsoft Dev Tunnel). Point claude.ai on your phone at
`https://<tunnel-host>/mcp` and drive your home workstation from anywhere.
ChatGPT and Grok connect through their connector settings the same way.
Bearer tokens and OAuth 2.1 + PKCE (with RFC 7591 dynamic registration) are
built in. → [docs/remote-mcp.md](docs/remote-mcp.md)

**3. Let an agent loose on untrusted code — inside a disposable sandbox.**

```bash
docker build -t coding-tools-mcp-sandbox:local .
docker run --rm --init -it -p 8765:8765 -v "$PWD:/workspace" coding-tools-mcp-sandbox:local
```

A containerized server with toolchains and caches preconfigured, safe to point
at a sketchy PR and destroy afterwards. → [docs/docker.md](docs/docker.md)

**4. Drive it from a GUI.**

```bash
cd apps/desktop-client
npm install
npm run tauri dev
```

The Tauri desktop app provides per-workspace profiles, server and tunnel
start/stop, OS-keychain credential storage, clipboard helpers, and live health
checks. The project is macOS-first and macOS is the supported/tested desktop
platform. English and 简体中文. →
[Desktop client](apps/desktop-client/README.md)

**6. Keep an interactive command alive.** `exec_command` starts a REPL or
debugger under a real PTY; `write_stdin` feeds it across turns; `read_output`
pages long output; `kill_command` cleans up. Long-running processes are
first-class, with deadline watchdogs and bounded buffers.

**7. Give your own agent production-grade hands.** Building an agent loop with
the Anthropic SDK or anything else? Don't hand-roll file and exec tools —
speak MCP to this server and inherit the whole safety boundary. →
[docs/embedding.md](docs/embedding.md)

## The tool catalog

One deliberately small, truthfully annotated coding surface. `apply_patch` is
the sole direct text/source-editing primitive: staged, baseline-checked, atomic
across files, with rollback. Git writes, branches, worktrees, build systems,
Xcode, SwiftPM, Homebrew, and other developer workflows use their native CLIs
through `exec_command` instead of dedicated wrapper tools.

Every enabled capability appears directly in `tools/list`; the complete inventory
and selection hints are maintained in [Tools and schemas](docs/tools-and-schemas.md).
There is no MCP-internal tool-discovery workflow.

`runtime_doctor` gives an agent a non-destructive macOS-oriented preflight with
actionable information about Xcode, Swift, SourceKit-LSP, codesign,
notarytool, Homebrew, Git, workspace access, LSP, sandbox state, and network
policy.

Network command policy can be selected independently with
`--network-policy deny|allowlist|unrestricted`. In allowlist mode, repeat
`--network-allow-domain github.com` (or use `*.example.com` for subdomains).
Targets outside the list, and network-intent commands whose destination cannot
be resolved statically, require explicit permission. This is a command-policy
gate rather than an OS-level egress firewall. The legacy `--allow-network`
switch remains an alias for `--network-policy unrestricted`.

Root `AGENTS.md`/`CLAUDE.md` files load automatically and come back in the
`instructions` of `initialize`, or of `server/discover` for a client that
never handshakes. Tool `content` is concise agent-facing text;
`structuredContent` carries the complete machine result. Schemas and result
envelopes: [docs/tools-and-schemas.md](docs/tools-and-schemas.md) ·
[docs/runtime-contract-v0.4.md](docs/runtime-contract-v0.4.md).

## Safety Boundary

| Mode | Meant for | What it allows |
| --- | --- | --- |
| `safe` (default) | day-to-day agent work | file tools and vetted commands; network-looking commands, shell expansion, inline scripts, and destructive commands all require explicit permission |
| `trusted` | local development | opens network, shell expansion, and inline scripts; keeps secret filtering and destructive-command checks |
| `dangerous` | isolated containers/VMs only | disables ordinary `exec_command` permission gates; workspace path boundaries still apply, and an explicit `deny`/`allowlist` network policy still applies |
| `host` | explicit full-host development | disables ordinary command gates and Landlock, inherits the server process's real home, temporary directories, SSH agent, Git credentials, and complete environment; an explicit `deny`/`allowlist` network policy still applies |

Recursive listing and search exclude `.git`, `node_modules`, build outputs,
virtualenvs, and caches. Commands run with workspace-bound cwd, timeouts, and
output caps; the environment is scrubbed except in explicit `host` mode. Linux hosts with Landlock get
kernel-enforced filesystem confinement; other platforms get an explicit
warning — this is still not a complete OS sandbox, so use the Docker image or
a VM for genuinely untrusted work. Details:
[SECURITY.md](SECURITY.md) · [docs/security-boundary.md](docs/security-boundary.md) ·
[docs/permission-modes.md](docs/permission-modes.md)

## Telemetry

The server sends anonymous usage telemetry (per-tool success/latency counters
and version/platform dimensions — never paths, arguments, commands, or file
contents) to help prioritize fixes. Disable it with
`CODING_TOOLS_MCP_TELEMETRY=off` or `DO_NOT_TRACK=1`; it is automatically off
in CI. `CODING_TOOLS_MCP_TELEMETRY=debug` prints every event to stderr instead
of sending. The full event list and guarantees are in
[docs/telemetry.md](docs/telemetry.md).

## Evidence, Dogfood and SWE-bench

Every release ships through a tag-triggered pipeline in which the compliance
suite, real-workload benchmark, and SWE-bench harness run from the same commit
that publishes to PyPI and npm — both via trusted publishing, npm with
provenance. Dogfood efficiency metrics are reproducible (`make dogfood-smoke`)
and checked in under `reports/`. This repository does not claim a
model-generated SWE-bench leaderboard result — see
[docs/swe-bench.md](docs/swe-bench.md) for exactly what is and is not
measured. More: [COMPLIANCE.md](COMPLIANCE.md) · [BENCHMARK.md](BENCHMARK.md) ·
[docs/dogfood.md](docs/dogfood.md)

## Documentation

| | |
| --- | --- |
| Documentation map | [Browse docs by topic](docs/README.md) |
| Getting started | [Quickstart](docs/quickstart.md) · [Client configuration](docs/mcp-client-config.md) · [Troubleshooting](docs/troubleshooting.md) |
| Remote & sandboxed | [Remote MCP](docs/remote-mcp.md) · [Docker sandbox](docs/docker.md) |
| Tools & contract | [Tools and schemas](docs/tools-and-schemas.md) · [Runtime contract](docs/runtime-contract-v0.4.md) · [Permission modes](docs/permission-modes.md) |
| Execution | [Exec recipes](docs/exec-command-recipes.md) · [Exec troubleshooting](docs/troubleshooting-exec.md) |
| Integration | [Embedding](docs/embedding.md) · [npm launcher](packages/npm-launcher/README.md) |
| Security & quality | [Security policy](SECURITY.md) · [Security boundary](docs/security-boundary.md) · [CI and tests](docs/ci-and-tests.md) · [Limitations](docs/limitations.md) · [Competitive analysis](docs/competitive-analysis.md) |

## Development

```bash
python -m pip install -e ".[dev]"
make ci        # lint, typecheck, tests, protocol/integration suites, gates
```

The full gate matrix is in [docs/ci-and-tests.md](docs/ci-and-tests.md).

## License

This project is licensed under the [Apache License 2.0](LICENSE).

If you use code, documentation, substantial implementation details, or
derivative work from this project, preserve the copyright notice, license
notice, and [NOTICE](NOTICE) file, and clearly attribute the original project.

Project: Coding Tools MCP  
Author: Coding Tools MCP Contributors  
Source: https://github.com/xyTom/coding-tools-mcp

Citation metadata is available in [CITATION.cff](CITATION.cff).
