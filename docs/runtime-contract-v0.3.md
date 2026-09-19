# Coding Tools MCP Runtime Contract v0.3

Status: implemented contract for `coding-tools-mcp` 0.3.x. The frozen contract
for 0.2.x is [runtime-contract-v0.2.md](runtime-contract-v0.2.md); what changed
between them, and what a client has to do about it, is
[migration-0.3.md](migration-0.3.md).

Protocol targets: MCP `2026-07-28`, which serves every request on its own, and
the handshake era `2025-11-25` with explicit compatibility for `2025-06-18`.

This contract describes one stable, model-neutral coding tool set. There are no
tool profiles and the server does not add or remove process tools dynamically.
`apply_patch` is the only direct text/source file-editing primitive; `edit_file`
is not provided. Workflow Git operations, checkpoint restore, and workflow-state
updates have separately specified guarded mutation semantics. Permission modes
alter command policy, not the advertised catalog.

One switch, `--dangerously-fake-readonly-annotations`, rewrites the exposure hints
in `tools/list` for clients that refuse mutating tools by annotation. It is not a
tool profile: the catalog, the schemas, and what every tool actually does are all
unchanged, and no tool is hidden. It requires `dangerous` or `host` permission mode, requires
authentication over HTTP, and is reported by `server_info.annotation_override` and
the server card, both of which continue to publish the real annotations recorded
below. Unless that switch is set, the annotations in this document are what
`tools/list` returns.

## Two protocol eras, one server

Both eras are served by the one runtime that owns the workspace, and protocol
negotiation leaves no hidden transport session behind. Workflow-enabled modern
requests may explicitly create durable protocol Task records, addressed by
unguessable `taskId`; that application state is not session state. Which era a
request belongs to is decided by the request alone: a `params._meta` carrying
`io.modelcontextprotocol/protocolVersion` is a
`2026-07-28` request, and everything else is a handshake-era one. A legacy
`_meta` such as `progressToken` does not make a request modern, and `initialize`
is always the handshake, whatever `_meta` it carries.

Stable tools with `listChanged: false` are advertised in both eras. When the
workflow toolset is enabled, modern `server/discover` additionally advertises
`extensions.io.modelcontextprotocol/tasks: {}`. The handshake era never
advertises the incompatible 2025 experimental Tasks capability. Logging,
resources, prompts, sampling, and elicitation are not advertised.

### The handshake era

- `initialize` negotiates a version and answers with it. A version this server
  does not speak — including `2026-07-28`, which is not negotiated at all — is
  answered with an `InitializeResult` naming the newest version that it does
  (`2025-11-25`), as the handshake spec requires.
- `initialize` is idempotent and is not an admission gate. Each one negotiates
  on its own, and every other implemented method is served whether or not one
  was sent first. `notifications/initialized` is accepted and answered with
  nothing.
- No session is created. HTTP responses carry no `Mcp-Session-Id`, and a header
  returned by a client that spoke to an older server is ignored rather than
  refused.
- The results of this era are exactly what they were: no field of the modern
  era is added to them, and every error is HTTP `200` with the JSON-RPC error.

### The `2026-07-28` era

A request states its own protocol version, so it needs no handshake and may
call `server/discover`, `ping`, `tools/list`, and `tools/call` immediately.
Workflow-enabled runtimes also accept `tasks/get`, `tasks/update`, and
`tasks/cancel` for requests that declare the Tasks extension in their current
client capabilities. `notifications/cancelled` is accepted here as well. Its
`params._meta` carries:

| `_meta` key | Required | Value |
| --- | --- | --- |
| `io.modelcontextprotocol/protocolVersion` | yes | `"2026-07-28"` |
| `io.modelcontextprotocol/clientCapabilities` | yes | object, may be empty |
| `io.modelcontextprotocol/clientInfo` | no | object naming the client |

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {}
    }
  }
}
```

Over Streamable HTTP such a request also mirrors its body in headers, as
SEP-2243 requires, so a gateway can route it without reading the body:

- `MCP-Protocol-Version` repeats the `_meta` protocol version.
- `Mcp-Method` repeats the JSON-RPC method, on notifications too.
- `Mcp-Name` repeats the subject of the methods that name one: `params.name`
  for `tools/call` and `prompts/get`, `params.uri` for `resources/read`, and
  `params.taskId` for `tasks/get`, `tasks/update`, and `tasks/cancel`. A
  value that cannot travel as an HTTP field is wrapped as
  `=?base64?<base64 of the UTF-8 value>?=`, whose payload may not exceed 8192
  characters. `server/discover` does not take this header.

Each of the three headers may appear exactly once. A gateway routes on them
alone, and which of two values it would read is its own business, so a request
that states its version, method, or subject twice is refused with `-32020`
rather than resolved.

Handshake-era requests are asked for none of these headers. A
`MCP-Protocol-Version: 2026-07-28` header over a body that carries no modern
`_meta` is a mirror violation like any other.

### `server/discover`

The probe a `2026-07-28` client sends in place of a handshake. It reports the
versions this server speaks per request, its capabilities, and the workspace
instructions:

```json
{
  "supportedVersions": ["2026-07-28"],
  "capabilities": {
    "tools": {"listChanged": false},
    "extensions": {"io.modelcontextprotocol/tasks": {}}
  },
  "instructions": "...",
  "resultType": "complete",
  "ttlMs": 0,
  "cacheScope": "private",
  "_meta": {
    "io.modelcontextprotocol/serverInfo": {
      "name": "coding-tools-mcp",
      "title": "Coding Tools MCP",
      "version": "0.3.10"
    }
  }
}
```

The `extensions` member above is present only when workflow tools are enabled.
`supportedVersions` lists the modern versions only. The handshake versions are
negotiated by `initialize` and are not accepted in `_meta`, so naming one here
would invite a client to retry a version that cannot work.

A `server/discover` that carries no modern `_meta` is a handshake-era request
for a method this server does not implement in that era, and is answered with
`-32601`. That is what sends a client which probes before it handshakes to
`initialize`, which works.

### Result encoding

A normal `2026-07-28` result carries `resultType: "complete"` and an
`_meta.io.modelcontextprotocol/serverInfo` naming this server. The results
whose content a client might be tempted to keep — `tools/list` and
`server/discover` — also carry `ttlMs: 0` and `cacheScope: "private"` on the
result root. Both are shaped by the workspace and the permission mode they were
served under, and the discover instructions quote the workspace's own
instruction files, so the conservative defaults are the correct ones: never
shared, never reused. A tool result that failed still reports
`resultType: "complete"` with `isError: true`; the envelope was complete, the
tool was not. A Task-augmented `tools/call` is the one supported exception:
its creation response retains `resultType: "task"` so a client can distinguish
the durable handle from a final tool result.

### `io.modelcontextprotocol/tasks`

This extension is exposed only by the workflow-enhanced `2026-07-28` runtime.
It is deliberately separate from the product-level `task_create/get/list/...`
tools: protocol Tasks represent deferred completion of one MCP request, while
workflow tasks represent a developer's persistent plan and evidence history.

The first task-augmented operation is `checks_run`. If a Tasks-capable client
calls it and the discovered check has already completed by the time the tool
returns, it receives the normal `CallToolResult`. If the check is still
`running`, the server first persists a protocol Task bound to its durable
`check_run_id`, then returns a `CreateTaskResult` with `resultType: "task"`, an
unguessable 128-bit `taskId`, status `working`, `ttlMs: null`, and a suggested
1000 ms poll interval. Approval ids are not copied into the protocol Task's
stored argument summary.

`tasks/get` refreshes a working Task from the persisted check evidence. While
the command is retained it reports `working`; when the check becomes
`passed`, `failed`, or `unknown`, the protocol Task becomes `completed` and
stores a final `CallToolResult` under `result`. A failed test/check remains a
completed protocol Task rather than a protocol failure, because check failure
is a tool-domain result. After Runtime restart, a taskId still resolves from
SQLite; if its backing command was interrupted, the existing check evidence
mechanism resolves it honestly as `unknown` rather than inventing completion.

`tasks/update` accepts an `inputResponses` object and acknowledges it. The
current task-augmented operation never enters `input_required`, so all response
keys are unknown/already-satisfied and are ignored as the extension permits.
`tasks/cancel` is cooperative: for a running backing check it invokes the
existing bounded command termination path and records `cancelled` only when the
process was actually terminated/killed. It never uses
`notifications/cancelled` as task cancellation. There is no `tasks/list`.

Every `tasks/get/update/cancel` request must declare
`io.modelcontextprotocol/tasks` in that request's client capability extensions;
otherwise it fails with the extension-defined `-32003` Missing Required Client
Capability. A missing or unknown `taskId` is `-32602`. Over Streamable HTTP,
`Mcp-Name` must exactly mirror the `taskId`.

### Errors and HTTP statuses

| Code | Meaning | HTTP status of a `2026-07-28` request |
| --- | --- | --- |
| `-32600` | invalid request envelope | `200`, in either era |
| `-32601` | unknown method | `404` |
| `-32602` | invalid params, including a missing or mistyped required `_meta` field | `400` |
| `-32003` | Tasks extension request without the required per-request client extension capability | `400` |
| `-32020` | headers do not mirror the body: missing, duplicated, or contradicting it | `400` |
| `-32022` | `_meta` names a protocol version this server does not speak; `data.supported` lists the modern versions only | `400` |
| `-32603` | unexpected server failure | `200` |
| `-32700` | parse error | `400` |

The one `-32600` that is not `200` is a transport refusal rather than a verdict
on a request that was dispatched: a **handshake-era** request whose
`MCP-Protocol-Version` header names a version from neither era is refused with
`400` before dispatch, and `data.supported` lists every version this server
speaks. A `2026-07-28` request is never refused that way, because its body
settles what the header could not: an unknown version stated by both is
`-32022`, and a header that contradicts the body is `-32020`.

That order holds generally. What the body says about itself is checked before
the headers that mirror it, so a request that is wrong in both ways is answered
with the protocol's verdict — an invalid `_meta` field is `-32602` and an
unsupported `_meta` version is `-32022` on either transport, never the `-32020`
the mirror would also have produced.

Every handshake-era error a dispatched request produced stays HTTP `200` with
the JSON-RPC error in the body, which is the only thing a client of that era
reads. What the transport rejects before dispatch, in either era, carries its
own status: `415` for a content type that is not JSON, `411` for a missing
`Content-Length`, `413` for a body over the maximum size, and `400` for a parse
error, a JSON-RPC batch, or the unknown version header above. `-32002 Server
not initialized` and `-32001 Unknown MCP session` are never returned by any
era.

A notification — a message with no `id` — is answered with nothing whatever
goes wrong inside it: over HTTP with an empty `202`, over stdio with silence.
The mirror headers are the exception, because they are a transport contract
rather than a verdict on the message: a notification whose headers do not
mirror its body is still refused with `400` and `-32020`.

### Transports

- Streamable HTTP uses `POST /mcp`. There are no sessions, so `DELETE /mcp`
  returns `405` with `Allow: POST`. Because this server does not provide an SSE
  stream, `GET /mcp` and `HEAD /mcp` return `405` as well.
- A request without an `MCP-Protocol-Version` header is treated as
  `2025-11-25`, this server's newest handshake version. The older spec suggests
  assuming `2025-03-26`, a version this server does not speak. The header value
  travels with the request as context and is available to the runtime; nothing
  echoes it, records it, or acts on it, and no method behaves differently for
  it.
- JSON-RPC batches are rejected.
- `notifications/cancelled` is accepted in both eras and answered with nothing,
  but it does not terminate the command the cancelled request started. A
  command outlives its request and is shared by every client of the workspace;
  terminate one with `kill_command`.
- stdio is newline-delimited JSON-RPC. stdout contains protocol messages only;
  diagnostics and logs go to stderr.
- The server card at `/.well-known/mcp.json` and
  `/.well-known/mcp/server-card.json` reports `supportedProtocolVersions`,
  every version this server speaks, newest first.

## Automatic project context

The server loads bounded root project instructions from `AGENTS.md`,
`AGENTS.MD`, `CLAUDE.md`, and `CLAUDE.MD` when present. The content is returned
in the `instructions` field of `initialize` and of `server/discover`, so an
agent of either era does not need an `open_workspace` call. Nested instruction
files are indexed by path but are not eagerly injected. Loading is UTF-8 safe
and bounded by file-count, scan-count, depth, per-file, and total-byte limits.

## Workspace and patch guarantees

- One server runtime owns one canonical workspace root and serves every client
  of it. Concurrent clients share the command pool, the retained output, and
  the patch baselines; this is a single trust domain by design.
- Project-scoped path inputs remain workspace-relative. Ordinary file tools and
  `apply_patch` additionally accept absolute paths only when they resolve inside
  an explicitly configured file-access root. `..` traversal, NUL bytes, and
  symlink escapes outside the selected root are rejected.
- `apply_patch` parses and validates every operation before committing, under a
  lock that spans every client, so two clients patching one file cannot lose
  an update: the later one is answered with a conflict rather than silently
  overwriting.
- Every replacement is prepared and fsynced in the target directory, then
  installed with `os.replace`.
- Existing mode bits, UTF-8 BOMs, and CRLF/LF style are preserved. Moves inherit
  the source mode.
- Baseline hashes and modes are checked before commit and again immediately
  before replacement. Conflicts are retryable and never silently overwrite a
  newly-created target.
- A failed multi-file commit restores all backups. Portable filesystems do not
  offer a true transaction across directories, so a rollback failure is
  reported explicitly as `PATCH_ROLLBACK_FAILED` with recovery details.

## Result contract

Every valid `tools/call` response contains:

```json
{
  "content": [{"type": "text", "text": "Short agent-readable result"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

`content` is concise model-facing text and is never a JSON serialization of the
whole payload. Its normal size is governed by each tool's own per-call limits
(`max_bytes`, `max_output_bytes`, `max_results`, ...), without the former
16 KiB renderer preview cap. A 2,162,688-byte emergency safety ceiling protects
clients from pathological individual entries that count-based limits cannot
bound. Command results always begin with a status line (status, exit code,
signal, timeout). Stable pageable truncation names an executable continuation
call (`read_output(output_ref=..., offset=...)`,
`read_file(path=..., start_line=...)`, ...); non-pageable results explicitly
say which limit or scope to change. `structuredContent` is the complete,
stable machine-readable interface. Large diffs and command output are not
copied into `_meta`; `_meta` is optional UI extension space only.

Tool failures keep the same envelope with `isError: true`, a readable error in
`content`, and this machine shape:

```json
{
  "ok": false,
  "error": {
    "code": "PATCH_CONTEXT_AMBIGUOUS",
    "message": "Patch context matched more than one location.",
    "category": "validation",
    "retryable": true,
    "details": {"path": "src/app.py", "hunk_index": 0, "match_count": 2}
  }
}
```

A client that forwards only `content` to the model must still be able to tell
a transient failure from a permanent one, so the error text restates the
category and retryability under the code and message, and a terminal failure
says so outright:

```text
COMMAND_NOT_FOUND: Command not found; stdin access denied.
Category: not_found. Retryable: no. Do not repeat this call unchanged.
Retry: This command_id has expired or never existed; …
```

Known tool error codes include:

```json
["ABSOLUTE_PATH_DENIED", "ACCESSIBILITY_PERMISSION_REQUIRED", "APPROVAL_EXPIRED", "APPROVAL_NOT_FOUND", "APPROVAL_NOT_USABLE", "APPROVAL_SCOPE_MISMATCH", "APP_CONTROL_ERROR", "APP_HELPER_ERROR", "BINARY_FILE", "BROWSER_DOWNLOAD_NOT_FOUND", "BROWSER_DOWNLOAD_TOO_LARGE", "BROWSER_DOWNLOAD_UNAVAILABLE", "BROWSER_ERROR", "BROWSER_TIMEOUT", "BROWSER_WATCH_NOT_FOUND", "CHECKPOINT_CONFLICT", "CHECKPOINT_NOT_FOUND", "CHECKPOINT_SCOPE_INVALID", "CHECKPOINT_TOO_LARGE", "CHECK_NOT_FOUND", "CHECK_RUN_NOT_FOUND", "CHROME_EXTENSION_ERROR", "CHROME_EXTENSION_UNAVAILABLE", "COMMAND_CLOSED", "COMMAND_LIMIT_REACHED", "COMMAND_NOT_FOUND", "ELICITATION_UNSUPPORTED", "GIT_COMMIT_SCOPE_MISMATCH", "GIT_ERROR", "GIT_NOT_REPOSITORY", "GIT_PATH_SCOPE_REQUIRED", "GIT_STATE_CONFLICT", "GIT_WORKTREE_DIRTY", "GIT_WORKTREE_EXISTS", "GIT_WORKTREE_NOT_FOUND", "HOOK_BLOCKED", "INTERNAL_ERROR", "INVALID_ARGUMENT", "INVALID_GIT_BRANCH", "INVALID_HOOK_CONFIG", "INVALID_TASK_TRANSITION", "IS_DIRECTORY", "LSP_EDIT_TOO_LARGE", "LSP_EDIT_UNSUPPORTED", "LSP_ERROR", "LSP_EXITED", "LSP_LANGUAGE_UNSUPPORTED", "LSP_PATH_OUTSIDE_WORKSPACE", "LSP_TIMEOUT", "LSP_UNAVAILABLE", "NOT_A_DIRECTORY", "NOT_FOUND", "OPERATION_CONFLICT", "OPERATION_NOT_FOUND", "OPERATION_PENDING", "OUTPUT_TOO_LARGE", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_OUTSIDE_FILE_SCOPE", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "PROTOCOL_TASK_NOT_FOUND", "REVIEW_CONFLICT", "REVIEW_NOT_FOUND", "REVIEW_TOO_LARGE", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_UNAVAILABLE", "SCREEN_RECORDING_PERMISSION_REQUIRED", "SYMLINK_ESCAPE", "TASK_CONFLICT", "TASK_NOT_FOUND", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING", "UNSUPPORTED_PLATFORM", "WORKFLOW_STORE_ERROR"]
```

Error categories are `validation`, `security`, `permission`, `runtime`,
`not_found`, `conflict`, and `internal`.

Malformed JSON-RPC uses standard protocol errors: parse `-32700`, invalid
request `-32600`, unknown method `-32601`, invalid params/tool `-32602`, and
unexpected server failure `-32603`. The base modern era adds `-32020` and
`-32022`; the opt-in Tasks extension additionally uses `-32003`, described
above. `PROTOCOL_TASK_NOT_FOUND` is an internal workflow-store code that the
protocol adapter always translates to `-32602` before it reaches a Tasks client.

## Command lifecycle

`exec_command`, `get_command`, `list_commands`, `write_stdin`, `read_output`,
and `kill_command` are always in the catalog. `exec_command` and `write_stdin`
default to a 10-second yield. A
short command normally finishes in one call. A running command returns:

```json
{
  "status": "running",
  "command_id": "...",
  "next_action": {
    "tool": "write_stdin",
    "arguments": {"command_id": "...", "chars": "", "yield_time_ms": 10000}
  }
}
```

For read-only polling, call `get_command` by `command_id` or `operation_id`;
`write_stdin` remains the interactive path when input must actually be sent.
`list_commands` enumerates recent workspace-managed commands after a reconnect.
`read_output` pages retained content without advancing a global cursor. Its
offsets are absolute and independent for stdout and stderr, and pages never
split a valid UTF-8 code point. A single truncated stream is selected by
`next_action`; when both streams are truncated, `next_actions` contains one
executable `read_output` call for each stream.

`exec_command.operation_id` is optional and workspace-scoped. Repeating the
same id with identical execution parameters returns the existing command and
does not spawn another process. Reusing it for different execution parameters
returns `OPERATION_CONFLICT`. Completed command state and operation mappings
are retained for up to 30 minutes (subject to bounded retained-command and
output budgets), so a response lost in a tunnel can normally be recovered
without repeating side effects.

A command belongs to the workspace, not to the client or the request that
started it. Any authenticated client of the same workspace can continue, read,
or terminate one with its `command_id`, and no transport event — a closed HTTP
response, a cancelled request, a reconnect — ends it. Active processes,
completed-output commands, per-command bytes, and total runtime bytes are
bounded, all of them per workspace rather than per client. Completed commands
have a TTL. POSIX `tty=true` uses a real pseudo-terminal; Windows reports
`TTY_UNSUPPORTED` in this build instead of pretending pipes are a TTY.

## HTTP authentication

Non-loopback deployment requires bearer or OAuth authentication unless the
operator explicitly selects no-auth. OAuth implements Authorization Code +
PKCE S256, protected-resource metadata, authorization-server metadata, exact
redirect URI matching, one-time five-minute codes, 24-hour access tokens, and
RFC 7591 dynamic client registration at `POST /oauth/register`. Public and
confidential clients are bound to their registered authentication method.

Authentication admits a client to the workspace; it does not partition it.
Every admitted client of one workspace shares that workspace's commands,
retained output, and patch state.

Dynamic client registrations are persisted per workspace, and when
`CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET` is omitted the generated HS256 signing
key is persisted alongside that registry with user-only file permissions.
Existing access tokens therefore survive runtime restarts. Authorization codes
remain short-lived and process-local. Forwarded headers are ignored unless
`CODING_TOOLS_MCP_TRUST_PROXY_HEADERS=1` is explicitly set.

## Stable tool inventory

The default catalog has 28 tools, including `view_image`. Setting
`CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE=0` removes that optional binary-content
tool. `--enable-workflow-tools` adds the 40 tools specified in the opt-in
workflow section for 68 directly exposed tools. Adding
`--defer-workflow-tools` instead hides those 40 workflow tools from the direct
catalog, exposes `tool_invoke`, and leaves them searchable through
`tool_search`: 29 tools are direct and 40 are deferred, while all 69 registered
runtime capabilities remain available. These selections are fixed at startup;
the runtime does not emit dynamic tool-list changes. The desktop launcher
selects deferred workflow exposure by default; the CLI defaults are unchanged.
Tool descriptions carry a category and a concise selection hint. Both
`initialize` and `server/discover` include progressive discovery guidance
alongside the existing project instructions.

Each definition below lists the live input property names and annotations. The
authoritative JSON Schemas are returned by `tools/list` and checked for drift in
CI. The annotations recorded here are the truthful ones and are what `server_info`
and the server card always report, including while
`--dangerously-fake-readonly-annotations` is rewriting the hints in `tools/list`.

### server_info

Inputs: none.

Annotations: `{"title":"Server info","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns server version, `supported_protocol_versions`, workspace, fixed tool
count, auth state, permission mode, environment scope, runtime directories,
boolean host SSH/Git integration availability, project-context
metadata, exec policy, network policy metadata, and the static retained-output
budget. The compatibility `network_allowed` boolean is true only for
`unrestricted`; allowlist details live under `network_policy`. It reports no
per-session value and no runtime counter: there is no session, and how often a
budget was hit is a property of the process rather than an answer to whichever
client asked. Those counters travel with telemetry.

### check_exec_environment

Inputs: none.

Annotations: `{"title":"Check exec environment","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns lightweight policy and Landlock status without running active probes.

### runtime_doctor

Inputs: none.

Annotations: `{"title":"Runtime doctor","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns a non-destructive health report intended for agent planning before it
starts trial-and-error execution. It resolves common command names from the
current command PATH, distinguishes `python` from `python3`, reports workspace
read/write access, shell-snapshot state, hook warnings, workflow/deferred-tool
state, optional LSP status, Landlock availability/enforcement, and the active
network policy. Actionable findings are returned as bounded `issues` entries
with a code, message, and suggested fix. The doctor does not make network
requests and does not mutate the workspace.

### hooks_status

Inputs: none.

Annotations: `{"title":"Hooks status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reports whether opt-in workspace hooks are enabled, their workspace-relative
configuration path, supported event names, loaded rule metadata, and config
warnings. It never returns hook command text. Hooks are disabled by default;
`--enable-hooks` reads `.agents/hooks.json` unless `--hooks-file` or
`CODING_TOOLS_MCP_HOOKS_FILE` selects another workspace-confined JSON file.

Supported hook events are `before_tool`, `after_tool`, and `tool_error`.
Commands receive one redacted JSON event on stdin and run under the same command
policy and filesystem sandbox as normal commands. A failing blocking
`before_tool` hook rejects the target call with `HOOK_BLOCKED`; failures in
non-blocking/post/error hooks are returned as warnings. Hook output is bounded
and timed-out hook process groups are terminated.

### shell_snapshot

Inputs: `"refresh"`, `"tools"`.

Annotations: `{"title":"Shell snapshot","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Captures the runtime's already-filtered command environment without returning
raw environment values. The result includes a stable snapshot id, environment
fingerprint, shell, PATH entries, environment count, and resolved executable
paths for a bounded requested tool-name list. Once captured, subsequent
`exec_command` environment construction starts from the frozen snapshot until
`shell_snapshot(refresh=true)` replaces it. Per-call `exec_command.env` values
are still merged afterward and remain subject to the normal secret filtering.

### read_file

Inputs: `"path"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"max_bytes"`, `"encoding"`.

Annotations: `{"title":"Read file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads UTF-8 ranges as a stream, reports full file line/byte metadata, rejects
binary content, and returns continuation metadata when bounded. The
continuation repeats the workspace-relative path it was given.

### read_files

Inputs: `"requests"`, `"max_total_bytes"`.

Annotations: `{"title":"Read files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads up to 32 bounded UTF-8 file slices in one call. Each request accepts
`path`, `start_line`, `end_line`, `max_lines`, `max_bytes`, and `encoding`.
`max_total_bytes` bounds the complete batch and the result includes a
`read_files` continuation when later requests do not fit in that budget.

### list_dir

Inputs: `"path"`, `"recursive"`, `"max_depth"`, `"max_entries"`, `"include_hidden"`, `"include_ignored"`, `"sort"`.

Annotations: `{"title":"List directory","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### list_files

Inputs: `"path"`, `"patterns"`, `"glob"`, `"exclude_patterns"`, `"include_hidden"`, `"include_ignored"`, `"max_results"`, `"sort"`.

Annotations: `{"title":"List files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Traversal is iterative and git-ignore checks are batched.

### search_text

Inputs: `"query"`, `"path"`, `"regex"`, `"case_sensitive"`, `"include_globs"`, `"glob"`, `"exclude_globs"`, `"context_lines"`, `"max_results"`, `"max_preview_bytes"`.

Annotations: `{"title":"Search text","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Ripgrep output is consumed incrementally and the process stops once the result
cap is known to be exceeded. `context_lines=0` does not reread matching files.

### tool_search

Inputs: `"query"`, `"category"`, `"limit"`, `"offset"`, `"include_schema"`.

Annotations: `{"title":"Search tools","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

All inputs are optional. With no query or category, returns a compact directory
of enabled categories, each with its selection guidance, direct/deferred counts,
and a `next_action` for browsing. This directory contains no tool schemas.
With a category and no query, returns bounded tool summaries in registry order;
`include_schema=true` adds their schemas. Empty categories are absent from the
directory, and a selected category with no enabled tools returns no matches.

A nonempty query ranks only tools available to the current Runtime, optionally
filtered by category. Exact normalized tool names return only the named tool;
other queries use English/Chinese intent aliases, names, descriptions, and
category keywords. Recognized intent phrases exclude weak keyword-only matches
so unrelated schemas do not fill the result limit. This is local lexical search,
not a semantic model. A query
containing only punctuation is invalid. Results include tool name, title,
description, category, use_when, annotations, score, and whether the match is
deferred. `include_schema=true` returns direct-tool schemas. Deferred query
matches always include their schema plus `invoke_via: "tool_invoke"`; direct
matches use their own name as `invoke_via`. Summaries without a schema provide
a `schema_action` that retrieves that tool's parameters.

Both text content and structured results include any returned parameter schemas
and invocation guidance. The default result limit is eight (maximum twenty);
`offset` defaults to zero and paginates category or search results. `next_action`
preserves the query/filter/schema options when more results remain. A miss
returns a directory action. Directory responses contain all nonempty categories
and do not paginate. Discovery never changes `tools/list` or enables a tool.
See [Progressive tool discovery](tool-discovery.md) for calling examples.

### tool_invoke

Inputs: `"name"`, `"arguments"`.

Annotations: `{"title":"Invoke deferred tool","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Exposed only when both `--enable-workflow-tools` and
`--defer-workflow-tools` are active. It accepts only names in that runtime's
40-tool deferred workflow set; direct tools and the gateway itself are rejected.
The nested tool receives its normal schema validation, permission checks, hook
events, telemetry, and structured result. `tool_invoke` therefore acts as a
static MCP-compatible deferred dispatch gateway rather than changing the live
tool catalog. Nested errors, status, diagnostics, and permission requirements
are also exposed at the gateway result's top level, preserving actionable error
text for clients that do not forward structured results.

### apply_patch

Inputs: `"patch"`, `"dry_run"`.

Annotations: `{"title":"Apply patch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Supports `*** Add File`, `*** Update File`, `*** Delete File`, and
`*** Move to` inside a `*** Begin Patch` / `*** End Patch` envelope.

```text
*** Begin Patch
*** Update File: app.py
@@
-old
+new
*** End Patch
```

### exec_command

Inputs: `"cmd"`, `"approval_ids"`, `"operation_id"`, `"workdir"`, `"cwd"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`, `"stdin"`, `"tty"`, `"env"`.

Annotations: `{"title":"Execute command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Statuses are `exited`, `running`, `timeout`, `terminated`, or `failed`.
Launch/policy failures use the error envelope with `status: "failed"`; signal
exits use `terminated`. Ordinary non-zero exit codes still use `exited`.
`"workdir"` is workspace-relative and defaults to the workspace root.
`"operation_id"` is optional. Within one workspace it makes retries of an
identical execution idempotent for the retained-command lifetime: a duplicate
returns the existing `command_id`; different execution parameters under the
same id return `OPERATION_CONFLICT`.

Network intent is evaluated independently from the other command gates. The
startup-selected network policy is `deny`, `allowlist`, or `unrestricted`.
`deny` requires explicit permission for detected network-intent commands.
`allowlist` permits a command without a network approval only when every
statically resolved target host matches an exact allowlist entry or a
`*.example.com` subdomain rule; a blocked host or unresolved destination still
requires permission. `unrestricted` skips the network gate. `--allow-network`
is retained as a compatibility alias for `--network-policy unrestricted`.
The allowlist is command-policy enforcement and does not claim kernel-level
egress isolation.

Example: `{"cmd":"pytest -q","workdir":".","yield_time_ms":30000}`.

### get_command

Inputs: `"command_id"`, `"operation_id"`.

Annotations: `{"title":"Get command","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Provide exactly one identifier. The result reports status, exit/signal/timeout,
absolute stdout/stderr byte totals, retained `output_refs`, and expiry metadata
without advancing any output cursor. An operation still between acceptance and
process registration reports `status: "accepting"` and is safe to poll again.

### list_commands

Inputs: `"operation_id"`, `"max_results"`.

Annotations: `{"title":"List commands","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Lists recent active/retained commands plus any accepting operation record,
newest first. `"operation_id"` optionally filters the list after a lost HTTP
response or reconnect.

### write_stdin

Inputs: `"command_id"`, `"chars"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Write stdin","readOnlyHint":false,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

Poll or interact with a command. Pass empty `chars` to wait for output.

Poll example: `{"command_id":"abc","chars":"","yield_time_ms":10000}`.
Input example: `{"command_id":"abc","chars":"yes\n"}`.

### kill_command

Inputs: `"command_id"`, `"signal"`, `"wait_ms"`, `"kill_wait_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Kill command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Statuses are `["terminated", "killed", "exited", "terminating", "not_found"]`.

If the process is still alive `"wait_ms"` after a non-KILL signal, the runtime
escalates to a hard kill and waits up to `"kill_wait_ms"` for the exit. This is
how a client stops a command: `notifications/cancelled` does not.

Example: `{"command_id":"abc","signal":"KILL"}`.

### read_output

Inputs: `"output_ref"`, `"stream"`, `"offset"`, `"limit"`.

Annotations: `{"title":"Read output","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Retention is head+tail per stream: the earliest bytes (head) and the most
recent bytes (rolling tail) are kept; the range between them may be evicted
once the per-stream buffer overflows. Responses report `head_retained_bytes`,
`evicted_gap_bytes`, and `omitted_bytes`; reads inside the evicted range clamp
forward to the tail. Offsets remain absolute and stable.
Returned chunks are aligned to UTF-8 code-point boundaries. If a requested
offset lands inside a retained multibyte character it advances to the next
valid boundary; a `limit` too small to return even the next complete character
returns `INVALID_ARGUMENT` instead of replacement-character corruption.

Example: `{"output_ref":"command:abc:stdout","offset":0,"limit":4096}`.

### git_status

Inputs: `"path"`, `"include_untracked"`, `"max_entries"`.

Annotations: `{"title":"Git status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_diff

Inputs: `"path"`, `"paths"`, `"staged"`, `"unstaged"`, `"context_lines"`, `"max_bytes"`.

Annotations: `{"title":"Git diff","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_log

Inputs: `"path"`, `"ref"`, `"max_count"`, `"skip"`.

Annotations: `{"title":"Git log","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_show

Inputs: `"rev"`, `"path"`, `"paths"`, `"include_diff"`, `"context_lines"`, `"max_bytes"`.

Annotations: `{"title":"Git show","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_blame

Inputs: `"path"`, `"rev"`, `"start_line"`, `"end_line"`, `"max_lines"`.

Annotations: `{"title":"Git blame","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### request_permissions

Inputs: `"tool_name"`, `"permission"`, `"reason"`, `"arguments"`, `"scope"`, `"ttl_seconds"`.

Annotations: `{"title":"Request permissions","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

When workflow tools are enabled in safe or trusted mode, this creates an
expiring `pending` request bound to the exact tool name and arguments. The local
Desktop app is the trusted decision channel. An approved request is consumed by
one matching call and cannot be replayed. Without workflow tools, this returns
`ELICITATION_UNSUPPORTED`. Dangerous and host modes continue to report the
operator's explicit startup-time auto-grant policy. The accepted durable scope
is `once`; session-wide escalation is rejected.

### view_image

Inputs: `"path"`, `"max_bytes"`, `"max_width"`, `"max_height"`, `"auto_resize"`.

Annotations: `{"title":"View image","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

The base64 data appears exactly once, in one MCP image content block. Stable
`structuredContent` contains metadata only; it has no duplicate base64 or data
URL. Pillow is optional and used only for requested auto-resize.

> Historical note: the `browser_*`, `chrome_extension_*`, and `app_*` sections
> below document GUI-automation tools removed from the live v0.3 catalog. They
> are not registered, returned by `tools/list`, or callable by current runtimes.
> They remain here only to make older v0.3 deployments and migration records
> interpretable.

### browser_status

Inputs: `"endpoint"`, `"timeout_ms"`.

Annotations: `{"title":"Browser status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Connects with Playwright to a local loopback Chrome CDP endpoint, defaulting to
`http://127.0.0.1:9222`, and reports browser version, context count, and tab count.

### browser_tabs

Inputs: `"endpoint"`, `"timeout_ms"`.

Annotations: `{"title":"Browser tabs","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Lists inspectable Chrome tabs with per-call indexes plus CDP `tab_id` values,
titles, URLs, and document visibility state. `tab_id` is stable across ordinary
tab-list reorderings and is preferred for multi-step browser workflows.

### browser_active_tab

Inputs: `"endpoint"`, `"timeout_ms"`.

Annotations: `{"title":"Browser active tab","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Returns the last tab reporting `document.visibilityState == "visible"`, with a
fallback to the last inspectable tab when Chrome cannot expose foreground state.

### browser_snapshot

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"max_chars"`, `"max_elements"`.

Annotations: `{"title":"Browser snapshot","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Returns bounded `document.body.innerText` plus a simplified list of visible
links, buttons, form controls, ARIA roles, focusable and editable elements with
unique CSS selectors suitable for subsequent browser calls. Control labels and
disabled/checked state are included; password values are redacted.
`element_count` and `elements_truncated` describe the element limit separately
from `text_truncated`.

### browser_screenshot

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"full_page"`.

Annotations: `{"title":"Browser screenshot","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Captures the selected tab as PNG. The base64 appears only in the MCP image
content block; structured content keeps metadata only.

### browser_evaluate

Inputs: `"script"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser evaluate","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Evaluates arbitrary JavaScript in the selected page. It is intentionally marked
mutating because JavaScript can change page state even when a particular script
only reads it. `timeout_ms` is an execution deadline enforced through CDP, so
an unresolved Promise or blocked JavaScript execution cannot occupy the tool
request indefinitely.

### browser_click

Inputs: `"selector"`, `"dialog_action"`, `"dialog_text"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser click","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Clicks the first element matching the supplied Playwright selector. Optional
dialog handling is registered before the click and can accept, dismiss, or
supply prompt text without leaving the page blocked.

### browser_type

Inputs: `"selector"`, `"text"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"clear"`, `"delay_ms"`.

Annotations: `{"title":"Browser type","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

By default fills the first matching element, replacing its current value. With
`clear=false`, it types sequentially and optionally applies `delay_ms` between
characters.

### browser_navigate

Inputs: `"url"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"wait_until"`.

Annotations: `{"title":"Browser navigate","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Navigates the selected tab to an HTTP(S) URL and returns the resulting tab and
response status when available.

### browser_back

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"wait_until"`.

Annotations: `{"title":"Browser back","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Moves the selected tab back once and reports whether a history entry existed.

### browser_reload

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"wait_until"`.

Annotations: `{"title":"Browser reload","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Reloads the selected tab with a bounded Playwright load-state wait.

### browser_hover

Inputs: `"selector"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser hover","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Hovers the first matching element so menus and hover-driven UI can be operated.

### browser_select

Inputs: `"selector"`, `"values"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser select","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Selects one or more option values on the first matching native select control.

### browser_press

Inputs: `"key"`, `"selector"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser press","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Sends a Playwright key or key chord to a matched element or the active page.

### browser_upload

Inputs: `"selector"`, `"paths"`, `"download_ids"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser upload","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Attaches up to 32 files to the first matching file input. Inputs may be explicit
workspace-confined paths or `download_id` values returned by `browser_download`.
Direct host paths and unmanaged runtime files are rejected. Managed downloads
expire with the runtime instance and are not silently copied into the workspace.

### browser_download

Inputs: `"selector"`, `"url"`, `"filename"`, `"max_bytes"`, `"endpoint"`,
`"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser download","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Requires exactly one of `selector` or `url`. A selector must identify an element
with an `href`; relative URLs are resolved against the selected page. The final
resource must be HTTP(S). The runtime asks Chrome to load the resource through
the selected tab's CDP network context with credentials enabled, receives a CDP
stream, and writes it incrementally into the runtime-private `browser-downloads`
area. The browser's native Downloads UI and its unreliable default-context
Playwright download events are not used.

`max_bytes` defaults to 64 MiB and is capped by schema at 256 MiB. A declared
`Content-Length` above the limit is rejected before streaming; an undeclared or
incorrect length is still enforced while reading. Partial files are removed on
failure. A successful result returns a random `download_id`, filename, byte
count, SHA-256, source URL, HTTP status, content type, and managed path. The
managed file can be reused by `browser_upload` with its `download_id`.

### browser_watch_start

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`,
`"max_entries"`, `"dialog_action"`, `"dialog_text"`.

Annotations: `{"title":"Start browser watch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Starts one background Playwright/CDP attachment owned by the current Runtime and
selected tab. The worker continuously services Playwright so console, page-error,
failed-request, dialog, and popup events remain observable across later MCP tool
calls. Events receive monotonically increasing `seq` values and Unix timestamps.
The retained deque is bounded by `max_entries` (default 1000, maximum 10000);
old entries are dropped rather than growing memory without bound.

The watch installs a dialog handler for its whole lifetime and is therefore
mutating: dialogs are dismissed by default or accepted when configured. The
watch is process-local. It is not stored in the workflow database, does not
survive Runtime restart, and never closes the user's Chrome when stopped.

### browser_watch_poll

Inputs: `"watch_id"`, `"after_seq"`, `"max_entries"`, `"wait_ms"`.

Annotations: `{"title":"Poll browser watch","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Returns retained events with `seq > after_seq`, up to `max_entries`. `wait_ms`
optionally waits until at least one new event arrives, the watch stops/fails, or
the bounded deadline expires. `next_after_seq` is the continuation cursor.
`dropped_since_cursor` and `dropped_total` disclose buffer loss; `truncated`
also becomes true when more retained events remain than the current result cap.
Unknown or previous-runtime ids return `BROWSER_WATCH_NOT_FOUND`.

### browser_watch_stop

Inputs: `"watch_id"`.

Annotations: `{"title":"Stop browser watch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":true,"openWorldHint":true}`.

Signals the watch worker to stop and waits briefly for the dedicated attachment
thread to exit. Repeating stop for the same retained watch is safe. Runtime
shutdown stops all remaining watches before command-manager cleanup.

### browser_wait

Inputs: `"selector"`, `"url"`, `"text"`, `"exact"`, `"state"`, `"wait_ms"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser wait","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Waits for any supplied selector, URL pattern, text, and bounded delay in order.
At least one condition is required; unmet conditions return `BROWSER_TIMEOUT`.

### browser_events

Inputs: `"trigger_selector"`, `"endpoint"`, `"tab_index"`, `"tab_id"`,
`"timeout_ms"`, `"wait_ms"`, `"max_entries"`, `"reload"`,
`"dialog_action"`, `"dialog_text"`.

Annotations: `{"title":"Capture browser events","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Samples bounded console, page-error, failed-request, dialog, and popup
events emitted after the Playwright attachment. When `trigger_selector` is
provided, listeners are installed before the first matching element is clicked,
so click-triggered dialogs and new windows can be observed without a cross-call
race. `dialog_action` controls any dialog seen during the sample.
This tool is a bounded one-call sample, not a persistent browser subscription;
events that happened before attachment are not recoverable. Native browser
download events are still not claimed here; use `browser_download` for bounded,
managed resource retrieval instead.

### browser_console

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"wait_ms"`, `"max_entries"`, `"reload"`.

Annotations: `{"title":"Browser console","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Captures console messages and uncaught page errors emitted after the Playwright
attachment. `reload=true` reloads the selected page first so page-load console
output can be observed; for that reason the tool is truthfully marked mutating.

### browser_network

Inputs: `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"wait_ms"`, `"max_entries"`, `"reload"`, `"include_resources"`.

Annotations: `{"title":"Browser network","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Returns current `PerformanceResourceTiming` entries when requested and captures
responses/request failures emitted after attachment. `reload=true` reloads the
page to sample page-load traffic, so the tool is marked mutating.

### browser_inspect

Inputs: `"selector"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"max_html_chars"`.

Annotations: `{"title":"Browser inspect","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Inspects the first matching element and returns its attributes, bounded
`outerHTML`, bounding rectangle, selected computed style properties, parent
chain, visibility, and element-scoped Web Animations state/keyframes.

### code_symbols

Inputs: `"path"`, `"query"`, `"kind"`, `"max_results"`, `"max_files"`.

Annotations: `{"title":"Code symbols","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Lists language-aware definitions under a workspace file or directory. Python
uses the standard-library AST; common JavaScript/TypeScript, Rust, Swift, Go,
Java/Kotlin, and C/C++ declarations use lightweight language patterns. Results
include symbol name, kind, workspace-relative path, line, column, and preview.
If `max_files` stops directory scanning before all supported source files are
visited, results report `truncated: true`, `truncated_by: "max_files"`, and
`scan_complete: false`; callers must not interpret a missing symbol as absent.

### code_definition

Inputs: `"symbol"`, `"path"`, `"max_results"`, `"max_files"`.

Annotations: `{"title":"Code definition","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Finds exact symbol or qualified-name definitions using the same language-aware
indexing rules as `code_symbols`. Multiple definitions are returned for
overloads or duplicate names rather than guessing one winner.

### code_references

Inputs: `"symbol"`, `"path"`, `"case_sensitive"`, `"max_results"`, `"max_files"`.

Annotations: `{"title":"Code references","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Finds exact identifier-token occurrences across supported source files and
returns bounded workspace-relative path, line, column, and preview metadata.
This is intentionally a lightweight textual reference scan rather than a
compiler or persistent LSP service.

### chrome_extension_install

Inputs: `"host_path"`, `"open_extensions_page"`.

Annotations: `{"title":"Install Chrome extension bridge","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

On macOS, copies the bundled Manifest V3 bridge extension into the current
user's application-support directory and writes Chrome's per-user Native
Messaging host manifest. `host_path` may override discovery of the installed
`coding-tools-mcp-chrome-host` executable. Module-only Python installations
receive a small launcher pinned to their current interpreter/import root. `open_extensions_page=true` opens
`chrome://extensions` to make the one-time approval step immediate. Chrome
still requires one explicit **Load unpacked** approval from that page.

### chrome_extension_status

Inputs: `"timeout_ms"`.

Annotations: `{"title":"Chrome extension status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Reports the stable bridge extension id, install paths, Native Messaging
manifest state, local Unix-socket state, and live bridge metadata when the
extension is connected.

### chrome_extensions

Inputs: `"query"`, `"max_results"`, `"timeout_ms"`.

Annotations: `{"title":"Chrome extensions","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Uses Chrome's `management` extension API through Native Messaging to list
installed extensions with ids, names, versions, enablement, type, and install
type. `query` filters by extension name or id.

### chrome_extension_tabs

Inputs: `"timeout_ms"`.

Annotations: `{"title":"Chrome extension tabs","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Lists tabs through Chrome's extension `tabs` API. This is independent of the
Playwright/CDP browser tools and does not require a remote-debugging port.

### chrome_extension_execute

Inputs: `"tab_id"`, `"script"`, `"timeout_ms"`.

Annotations: `{"title":"Chrome extension execute","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Temporarily attaches Chrome's debugger API to the selected tab, evaluates the
JavaScript expression, returns a by-value result, then detaches. Chrome may
surface its normal debugger-attached UI while the request is active.

### chrome_extension_send

Inputs: `"extension_id"`, `"message"`, `"timeout_ms"`.

Annotations: `{"title":"Chrome extension send","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Sends a runtime message to another installed Chrome extension. Chrome security
rules still apply: the target extension must explicitly permit external
messages from the bridge extension; this tool does not bypass extension
isolation.

### app_accessibility

Inputs: `"open_settings"`.

Annotations: `{"title":"App accessibility","readOnlyHint":false,"destructiveHint":false,"idempotentHint":false,"openWorldHint":true}`.

Reports whether macOS Accessibility trusts the current runtime. With
`open_settings=true`, opens the system Accessibility privacy pane when trust is
missing.

### app_list

Inputs: `"query"`, `"include_background"`, `"max_results"`.

Annotations: `{"title":"App list","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Lists running macOS applications with name, bundle id, pid, background-only
state, foreground state, and current Accessibility trust.

### app_launch

Inputs: `"app"`, `"new_instance"`.

Annotations: `{"title":"App launch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Launches a macOS application by display name or bundle identifier using the
system `open` service. `new_instance=true` requests a separate application
instance where macOS permits one.

### app_activate

Inputs: `"app"`, `"wait_ms"`.

Annotations: `{"title":"App activate","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Brings an application to the foreground and returns the resolved running-app
metadata after the bounded activation delay.

### app_windows

Inputs: `"app"`.

Annotations: `{"title":"App windows","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Returns Accessibility metadata for each window, including role/subrole, title,
identifier, focus, position, and size. Accessibility permission is required.

### app_snapshot

Inputs: `"app"`, `"max_depth"`, `"max_elements"`.

Annotations: `{"title":"App snapshot","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Walks a bounded portion of the macOS Accessibility hierarchy and returns roles,
titles, identifiers, descriptions, values, focus/enabled state, geometry, and
tree depth for subsequent app-control calls.

### app_click

Inputs: `"app"`, `"role"`, `"title"`, `"identifier"`, `"index"`.

Annotations: `{"title":"App click","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Finds an Accessibility element by role/title/identifier (at least one selector
is required) and invokes `AXPress`, falling back to a synthesized mouse click at
the element center when appropriate.

### app_type

Inputs: `"app"`, `"text"`, `"role"`, `"title"`, `"identifier"`, `"index"`, `"clear"`.

Annotations: `{"title":"App type","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Sets `AXValue` directly for a matched editable element when possible; otherwise
uses synthesized keyboard input. With no element selector, text is sent to the
currently focused control in the target application.

### app_press

Inputs: `"app"`, `"key"`, `"modifiers"`, `"wait_ms"`.

Annotations: `{"title":"App press","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Activates the application and posts one supported keyboard key with optional
Command, Shift, Control, Option, or Fn modifiers using CGEvent.

### app_menu

Inputs: `"app"`, `"path"`.

Annotations: `{"title":"App menu","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Selects a menu path such as `["File", "Open…"]` through Accessibility
`AXMenuBarItem`/`AXMenuItem` elements.

### app_screenshot

Inputs: `"app"`, `"window_index"`.

Annotations: `{"title":"App screenshot","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}`.

Captures the Accessibility bounds of one app window as PNG. The image is
returned once as MCP image content with metadata-only structured content.
macOS Screen Recording permission may be required in addition to Accessibility.

## Opt-in workflow toolset

The tools in this section are exposed only when the server starts with
`--enable-workflow-tools` (or `CODING_TOOLS_MCP_ENABLE_WORKFLOW_TOOLS=1`). The
selection remains fixed for the lifetime of the runtime and `listChanged` stays
`false`. Persistent state is stored outside the workspace in the platform
application-state directory, or under `--state-root`; it is partitioned by a
hash of the canonical workspace path. A task id does not create a new transport
session or client trust boundary.

### workspace_overview

Inputs: `"max_files"`.

Returns bounded manifest, language, entry-point, top-level area, and project
instruction metadata. `scan_complete` and `truncated` disclose coverage.

### repo_map

Inputs: `"path"`, `"query"`, `"max_files"`, `"max_symbols"`.

Returns bounded code symbols grouped by file. `coverage` identifies the current
Python-AST and language-pattern implementation; this tool does not claim LSP
reference or call-graph semantics.

### project_instructions

Inputs: `"path"`.

Returns root and nested `AGENTS.md`/`CLAUDE.md` files whose directory scope
contains the selected path, ordered from broad to narrow scope.

### skills_list

Inputs: `"max_results"`.

Lists metadata and content hashes for UTF-8 workspace
`.agents/skills/**/SKILL.md` entries. Listing or reading a Skill never executes
its scripts and cannot raise runtime permissions.

### skills_read

Inputs: `"path"`.

Reads one selected workspace Skill, with a 128 KiB limit and workspace/symlink
confinement.

### checks_discover

Inputs: `"path"`.

Discovers bounded test, lint, typecheck, aggregate, and build commands from
recognized project files without executing them.

### checks_run

Inputs: `"check_id"`, `"path"`, `"task_id"`, `"operation_id"`, `"timeout_ms"`,
`"yield_time_ms"`, `"max_output_bytes"`, `"approval_ids"`.

Re-discovers the requested check and runs its exact command through the existing
`exec_command` policy and command manager. Unknown or removed checks return
`CHECK_NOT_FOUND`; command handles and retry deduplication keep their existing
semantics.

Completed and running checks receive a persistent `check_run_id`. When
`task_id` is supplied, the run is also appended to that task's event history.
The evidence records bounded output and code fingerprints before and after the
command.

### checks_result

Inputs: `"check_run_id"`.

Returns persisted check evidence. A retained running command is refreshed from
the command manager; after a runtime restart an unresolvable running command is
reported as `unknown`. `stale` is true when the current code fingerprint differs
from the recorded post-check fingerprint. Unknown ids return
`CHECK_RUN_NOT_FOUND`.

### task_create

Inputs: `"title"`, `"objective"`, `"details"`.

Creates a persistent task in `pending` state at revision 1.

### task_get

Inputs: `"task_id"`.

Returns one persistent task or `TASK_NOT_FOUND`.

### task_list

Inputs: `"status"`, `"max_results"`.

Lists tasks by latest update, optionally filtered to a declared task status.

### task_update

Inputs: `"task_id"`, `"expected_revision"`, `"status"`, `"title"`,
`"objective"`, `"details"`.

Updates a task only when `expected_revision` is current. Concurrent updates
return retryable `TASK_CONFLICT`; invalid state changes return
`INVALID_TASK_TRANSITION`. Details are shallow-merged and the revision advances
once.

### task_event_add

Inputs: `"task_id"`, `"event_type"`, `"message"`, `"details"`.

Appends a progress, decision, evidence, note, blocked, or resumed event to one
task. It does not change task status or revision.

### task_events

Inputs: `"task_id"`, `"max_results"`.

Lists the most recent persistent events for a task. Task creation, updates,
linked checks, and linked checkpoints also generate events automatically.

### task_context

Inputs: `"task_id"`, `"event_limit"`.

Returns the task record together with recent events and linked check/checkpoint
evidence so a client can resume after reconnecting or restarting the runtime.

### task_plan_get

Inputs: `"task_id"`.

Annotations: `{"title":"Get task plan","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns ordered plan steps and the current task revision.

### task_plan_update

Inputs: `"task_id"`, `"expected_revision"`, `"steps"`.

Annotations: `{"title":"Update task plan","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Atomically replaces up to 100 ordered steps and advances the task revision.
Step ids must be unique and at most one step may be `in_progress`. A concurrent
task or plan update returns retryable `TASK_CONFLICT`.

### checkpoint_create

Inputs: `"paths"`, `"label"`, `"task_id"`.

Snapshots 1-64 unique explicit UTF-8 regular-file paths, including their
existence state and mode, up to 8 MiB total. It does not change HEAD, the index,
or unlisted files. Directories, symlinks and unsupported content are rejected.
When `task_id` is supplied, the checkpoint is linked through the task event log.

### checkpoint_list

Inputs: `"max_results"`.

Lists checkpoint identity, label, optional HEAD, creation time, file count and
stored byte count without returning file content.

### checkpoint_diff

Inputs: `"checkpoint_id"`.

Compares every checkpointed path to the current workspace and returns per-file
restore actions plus a `restore_token` bound to the checkpoint id and the exact
current existence/digest/mode state.

### checkpoint_restore

Inputs: `"checkpoint_id"`, `"restore_token"`.

Rechecks every path and rejects stale previews with retryable
`CHECKPOINT_CONFLICT`. A current token restores the complete explicit scope
through the existing atomic multi-file committer; files absent at checkpoint
creation are deleted. Git index and external side effects are outside its scope.

### git_branch_list

Inputs: `"max_results"`.

Lists local branches and returns the current `head` and `index_fingerprint`.
Those values are concurrency tokens for every Git write tool.

### git_branch_create

Inputs: `"name"`, `"start_point"`, `"checkout"`, `"expected_head"`,
`"expected_index_fingerprint"`.

Validates the branch name with Git and creates a local branch only while HEAD
and index match the reviewed state. Checkout is explicit and defaults to false.

### git_conflicts

Inputs: none.

Lists unmerged paths and all index stages. It also returns current HEAD and
index fingerprints and never resolves conflicts automatically.

### git_stage

Inputs: `"paths"`, `"expected_head"`, `"expected_index_fingerprint"`.

Stages 1-200 unique explicit workspace paths. The workspace root is rejected,
and stale HEAD/index values return retryable `GIT_STATE_CONFLICT`.

### git_unstage

Inputs: `"paths"`, `"expected_head"`, `"expected_index_fingerprint"`.

Removes only the explicit paths from the index through `git restore --staged`.
Working-tree content is preserved.

### git_commit

Inputs: `"paths"`, `"message"`, `"expected_head"`,
`"expected_index_fingerprint"`.

Commits only when the complete staged path set exactly equals `paths`; unrelated
staged content returns `GIT_COMMIT_SCOPE_MISMATCH`. Hooks and configured signing
may run and their failures are returned as `GIT_ERROR`. This tool does not push.

### git_worktree_list

Inputs: none.

Annotations: `{"title":"List Git worktrees","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Lists all repository worktrees and marks entries stored under this workspace's
private workflow state as managed.

### git_worktree_create

Inputs: `"worktree_id"`, `"branch"`, `"create_branch"`, `"start_point"`,
`"expected_head"`, `"expected_index_fingerprint"`.

Annotations: `{"title":"Create Git worktree","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Creates an isolated checkout under private workflow state after HEAD and index
concurrency checks. It can create a new branch or attach an existing branch and
returns the absolute path so the Desktop app can register it as a workspace.

### git_worktree_remove

Inputs: `"worktree_id"`.

Annotations: `{"title":"Remove Git worktree","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Removes only a runtime-managed worktree. Dirty or untracked content returns
`GIT_WORKTREE_DIRTY`; the branch is always preserved.

### lsp_status

Inputs: none.

Reports available/running Python, TypeScript/JavaScript, and Rust language-server
backends, their commands and supported extensions. Rust uses `rust-analyzer` and
is rooted at the nearest ancestor `Cargo.toml`; a rustup proxy without the actual
component installed is reported unavailable. Backends start only when a semantic
operation first needs them.

### lsp_definition

Inputs: `"path"`, `"line"`, `"column"`.

Returns workspace-confined semantic definitions. Public input positions are
one-based; requests use negotiated UTF-16 positions, and results identify that
encoding explicitly.

### lsp_references

Inputs: `"path"`, `"line"`, `"column"`, `"include_declaration"`,
`"max_results"`.

Returns bounded semantic references and source-file SHA-256. Results beyond the
limit set `truncated`.

### lsp_diagnostics

Inputs: `"path"`, `"wait_ms"`, `"max_results"`.

Opens or refreshes the UTF-8 document and returns bounded diagnostics published
by the language server. It does not label AST or text-search results as LSP.

### lsp_rename_preview

Inputs: `"path"`, `"line"`, `"column"`, `"new_name"`, `"max_files"`,
`"max_edits"`.

Returns text edits grouped by workspace path with each current file SHA-256.
The tool never applies edits. Resource operations and paths outside the
workspace are rejected; oversized previews return `LSP_EDIT_TOO_LARGE`.

Python backend discovery tries `basedpyright-langserver`,
`pyright-langserver`, then `pylsp`. TypeScript/JavaScript discovery tries
`typescript-language-server`. Operators may set
`CODING_TOOLS_MCP_PYTHON_LSP_COMMAND` or
`CODING_TOOLS_MCP_TYPESCRIPT_LSP_COMMAND`. Missing backends return
`LSP_UNAVAILABLE` while the existing `code_*` tools remain usable.

### review_prepare

Inputs: `"path"`, `"paths"`, `"task_id"`, `"staged"`, `"unstaged"`,
`"max_bytes"`.

Persists a bounded review snapshot containing Git status and diff, applicable
project instructions, optional task evidence, and a code fingerprint. Preparing
materials does not claim that an AI or human has reviewed them.

### review_record

Inputs: `"review_id"`, `"expected_revision"`, `"status"`, `"findings"`.

Records up to 500 workspace-confined findings with path, line/end line,
priority, title, body, and finding status. Revision mismatch returns retryable
`REVIEW_CONFLICT`; the review status is `completed`, `changes_requested`, or
`approved`.

### review_get

Inputs: `"review_id"`.

Returns the immutable preparation snapshot and current recorded findings.
`stale` becomes true when the current scoped code fingerprint differs from the
prepared fingerprint. Unknown ids return `REVIEW_NOT_FOUND`.

### approval_get

Inputs: `"approval_id"`.

Annotations: `{"title":"Get approval request","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns one approval as `pending`, `approved`, `denied`, `expired`, or
`consumed`. Reading an elapsed pending or approved request marks it expired.

### approval_list

Inputs: `"status"`, `"max_results"`.

Annotations: `{"title":"List approval requests","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Lists recent approvals with redacted display arguments. MCP clients cannot
approve or deny requests; those mutations are restricted to the local Desktop
app. Approved requests must match the exact tool arguments and every required
permission, expire after their TTL, and are consumed atomically before use.

## Forbidden product-layer tools

The runtime does not expose external-agent login/accounts, agent memory, cloud
tasks, web search/fetch, image generation, model routing, plugin installation,
subagent orchestration, or high-level prompt wrappers.

## Known limitation: cancellation responsiveness

A cancelled request is answered as this contract says it is, in both eras, but
the work it started is not stopped any sooner. On stdio the loop is serial, so
the response is already written by the time a cancellation could be read; over
HTTP the modern cancellation signal is a closed response stream, and this
server does not detect a disconnect. No client-observable rule is broken —
nothing is sent for a cancelled request that would not have been sent anyway —
but the SHOULD to stop working promptly is not met. The mitigations are the
30-second foreground window of `exec_command` and terminating a command with
`kill_command`. Tracked in issue
[#48](https://github.com/xyTom/coding-tools-mcp/issues/48).

## Compatibility note for 0.3

0.2 clients keep working: the handshake era is unchanged on the wire. What
changed for them is the tool catalog and the transport, not the envelope —
`get_default_cwd` and `set_default_cwd` are gone, relative paths resolve
against the workspace root, and HTTP no longer has sessions. Every removal, and
what to do instead, is in [migration-0.3.md](migration-0.3.md).
