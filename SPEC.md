# Coding Tools MCP Spec

This repository implements the current macOS-first runtime contract defined in
[docs/runtime-contract-v0.4.md](docs/runtime-contract-v0.4.md).

## Product boundary

The server exposes coding primitives over MCP: inspect a project, apply
structured patches, run and interact with commands, inspect Git, navigate code,
and bind a persistent gateway session to an explicit project. It is not an
agent wrapper and does not persist plans, tasks, reviews, checkpoints, Skills,
agent memory, or orchestration state.

## Fixed tool model

The implementation contains at most 32 registered MCP tools and currently has
29. A normal project runtime exposes its enabled primitives directly in one
stable `tools/list`; there is no secondary discovery gateway, dynamic
`tools/list_changed`, workflow profile, `edit_file`, or required `open_workspace`
call.

The surface is file read/search/patch, bounded command lifecycle, Git evidence,
code navigation/diagnostics, permission requests, image inspection, project
orientation/instructions, check discovery, and runtime diagnostics. The
persistent HTTP Project Gateway additionally exposes `project_context`.

`apply_patch` is the only direct text/source file-editing tool. Git writes,
branches, worktrees, tests/builds, Xcode, SwiftPM, Homebrew, codesign,
notarytool, and similar workflows use their native commands through
`exec_command`. `safe`, `trusted`, `dangerous`, and `host` are execution
permission policies; they do not create alternative workflow APIs.

Network policy is selected at startup as `deny`, `allowlist`, or
`unrestricted`. `allowlist` accepts exact domains and `*.example.com` patterns;
network-intent commands with a statically detected target outside that set, or
with a target that cannot be resolved from the command line, require explicit
permission. This layer is command-policy enforcement rather than an OS-level
egress firewall.

## Protocol

- Two eras are served at once: `2026-07-28`, which carries its version, client
  capabilities, and identity in each request's `params._meta`, and the
  handshake era `2025-11-25` with `2025-06-18` explicitly supported. A request
  belongs to the modern era if and only if its `_meta` names that version.
- Streamable HTTP uses `/mcp`; stdio uses newline-delimited JSON-RPC.
- A normal single-project Runtime has no transport session. The optional
  persistent Project Gateway uses `Mcp-Session-Id` only as an isolated
  `session -> project` routing key; it never stores prompt or workflow state.
- JSON-RPC batches are rejected, unimplemented logging is not advertised, and
  `notifications/cancelled` is accepted without terminating the command the
  cancelled request started — a command is stopped with `kill_command`.
- `content` is agent-readable text normally sized by each tool's per-call
  limits, with a documented emergency safety ceiling for pathological entries.
  `structuredContent` is the complete stable machine result. `_meta` is
  optional UI space only.
- Root project instructions are loaded automatically and returned in the
  `instructions` of `initialize` and of `server/discover`.

## Correctness guarantees

Patch operations are staged before writing, use same-directory fsynced temporary
files and atomic replacement, preserve mode/BOM/newlines, detect stale
baselines, and roll back multi-file failures. Filesystem rollback failure is
reported explicitly rather than hidden.

Commands use a 10-second default yield, real POSIX PTYs, bounded active and
retained-command stores, per-command and runtime output budgets, TTL cleanup,
and explicit `next_action` objects for polling or truncated output. Command
handles are `command_id` values, owned by the workspace rather than by a
client: any authenticated client of the workspace can continue, read, or kill
a command with one, and no transport event ends it. `exec_command.operation_id`
provides optional retry deduplication, while `get_command` and `list_commands`
provide read-only reconnect recovery without consuming output cursors.

## Security boundary

Direct tools reject absolute paths, traversal, NULs, and symlink escapes.
`exec_command` also applies permission policy and Linux Landlock when available,
but remains a coding runtime rather than a complete container sandbox. Remote
deployment must use bearer or OAuth authentication. OAuth supports protected
resource metadata, PKCE S256, exact redirect binding, and RFC 7591 dynamic client
registration. Authentication admits a client to a workspace and does not
partition it: one workspace is one trust domain, shared by every client of it.

## Design rule

New MCP tools are added only when the model cannot reasonably obtain the same
capability by composing existing primitives, or when a dedicated tool provides
a material safety or structured-data advantage. Mechanical batching is useful;
agent reasoning and workflow orchestration are not runtime responsibilities.
