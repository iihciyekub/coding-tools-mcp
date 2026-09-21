# Coding Tools MCP Spec

This repository implements the `coding-tools-mcp-v0.3` runtime contract defined
in [docs/runtime-contract-v0.3.md](docs/runtime-contract-v0.3.md).

## Product boundary

The server exposes coding primitives over MCP: inspect a workspace, apply
structured patches, run and interact with commands, inspect Git, and optionally
persist workspace-local task/checkpoint records. It is not an agent wrapper and
does not expose accounts, personal memory, cloud tasks, web search, model
routing, plugin installation, image generation, or subagent orchestration.

## Fixed tool model

There is one stable default catalog and one opt-in workflow extension selected
at process startup. Workflow tools may also be placed behind a static deferred
gateway selected at startup. The runtime has no dynamic `tools/list_changed`, no
`edit_file`, and no required `open_workspace` call.
`apply_patch` is the only direct text/source file-editing tool. Workflow Git
operations, checkpoint restore, and workflow-state updates have separately
specified guarded mutation semantics. `safe`, `trusted`, `dangerous`, and
`host` are command permission policies and never alter `tools/list`.

The default catalog contains 28 tools when `view_image` is enabled:

- runtime/context: `server_info`, `check_exec_environment`, `runtime_doctor`, `hooks_status`,
  `shell_snapshot`
- workspace inspection: `read_file`, `read_files`, `list_dir`, `list_files`,
  `search_text`, `tool_search`
- mutation: `apply_patch`
- processes: `exec_command`, `get_command`, `list_commands`, `write_stdin`,
  `read_output`, `kill_command`
- Git: `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame`
- policy/image: `request_permissions`, `view_image`
- code intelligence: `code_symbols`, `code_definition`, `code_references`

`view_image` can be disabled as an installation capability. All other tools are
fixed by the selected startup configuration. `--enable-workflow-tools` adds 41
project insight, Skills, checks, task, and checkpoint tools; see
[tools and schemas](docs/tools-and-schemas.md) for their authoritative inventory.
With `--defer-workflow-tools`, those 41 workflow tools are omitted from the
direct catalog and are discovered through `tool_search` and invoked through the
additional `tool_invoke` gateway. This yields 29 directly exposed tools while
retaining all 70 runtime capabilities available in that startup configuration.

`--enable-hooks` loads workspace-confined hook rules from `.agents/hooks.json`
by default. Hooks run under the same command policy/sandbox as normal command
execution and may observe `before_tool`, `after_tool`, and `tool_error` events.
`shell_snapshot` explicitly freezes the filtered command environment and PATH
tool resolution for subsequent commands until refreshed.

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
- There are no sessions in either era. One `Runtime` owns the workspace and
  serves every client of it; HTTP issues no `Mcp-Session-Id` and `DELETE /mcp`
  returns `405`.
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

## Compatibility

Version 0.3 adds `2026-07-28` and removes every session. The handshake era is
unchanged on the wire; the cwd tools, the HTTP session, and several
`server_info` fields are not. See
[docs/migration-0.3.md](docs/migration-0.3.md).

Version 0.2 changes model-facing result text from a JSON mirror to summaries.
Clients that parsed `content[0].text` as JSON must read `structuredContent`.
Image base64 now appears once, in the MCP image block. Tool profiles and the
`view_image.output` selector are removed.
