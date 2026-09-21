# Coding Tools MCP Runtime Contract v0.4

Status: implemented contract for `coding-tools-mcp` 0.4.x. The frozen contract
for 0.3.x is [runtime-contract-v0.3.md](runtime-contract-v0.3.md); what changed
between them, and what a client has to do about it, is
[migration-0.4.md](migration-0.4.md).

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
      "version": "0.4.2"
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
- Project-scoped path inputs remain workspace-relative. In safe/trusted/dangerous
  modes, ordinary file tools and `apply_patch` additionally accept absolute paths
  only when they resolve inside an explicitly configured file-access root. In host
  mode, those file tools may resolve absolute and home-relative paths across the host
  filesystem; relative paths still resolve from the configured workspace. Git, LSP,
  checks, reviews, workflow state, and project instructions remain workspace-scoped.
  NUL bytes and `..` traversal in relative paths remain rejected.
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
["ABSOLUTE_PATH_DENIED", "APPROVAL_EXPIRED", "APPROVAL_NOT_FOUND", "APPROVAL_NOT_USABLE", "APPROVAL_SCOPE_MISMATCH", "BINARY_FILE", "CHECKPOINT_CONFLICT", "CHECKPOINT_NOT_FOUND", "CHECKPOINT_SCOPE_INVALID", "CHECKPOINT_TOO_LARGE", "CHECK_NOT_FOUND", "CHECK_RUN_NOT_FOUND", "COMMAND_CLOSED", "COMMAND_LIMIT_REACHED", "COMMAND_NOT_FOUND", "CONTEXT_CHECKPOINT_INVALID", "CONTEXT_CHECKPOINT_NOT_FOUND", "CONTEXT_CHECKPOINT_TOO_LARGE", "GIT_COMMIT_SCOPE_MISMATCH", "GIT_ERROR", "GIT_NOT_REPOSITORY", "GIT_PATH_SCOPE_REQUIRED", "GIT_REPOSITORY_MISMATCH", "GIT_REPOSITORY_OUTSIDE_WORKSPACE", "GIT_STATE_CONFLICT", "GIT_WORKTREE_DIRTY", "GIT_WORKTREE_EXISTS", "GIT_WORKTREE_NOT_FOUND", "HOOK_BLOCKED", "INTERNAL_ERROR", "INVALID_ARGUMENT", "INVALID_GIT_BRANCH", "INVALID_HOOK_CONFIG", "INVALID_TASK_TRANSITION", "IS_DIRECTORY", "LSP_EDIT_TOO_LARGE", "LSP_EDIT_UNSUPPORTED", "LSP_ERROR", "LSP_EXITED", "LSP_LANGUAGE_UNSUPPORTED", "LSP_PATH_OUTSIDE_WORKSPACE", "LSP_TIMEOUT", "LSP_UNAVAILABLE", "NOT_A_DIRECTORY", "NOT_FOUND", "OPERATION_CONFLICT", "OPERATION_NOT_FOUND", "OPERATION_PENDING", "OUTPUT_TOO_LARGE", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_OUTSIDE_FILE_SCOPE", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "PROTOCOL_TASK_NOT_FOUND", "REVIEW_CONFLICT", "REVIEW_NOT_FOUND", "REVIEW_TOO_LARGE", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_UNAVAILABLE", "SYMLINK_ESCAPE", "TASK_CONFLICT", "TASK_NOT_FOUND", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING", "WORKFLOW_STORE_ERROR"]
```

Error categories are `validation`, `security`, `permission`, `runtime`,
`not_found`, `conflict`, and `internal`.

Multi-project routing additionally defines `"GIT_REPOSITORY_MISMATCH"` and
`"GIT_REPOSITORY_OUTSIDE_WORKSPACE"`; both reject the operation before Git writes.

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
tool. `--enable-workflow-tools` adds the 41 tools specified in the opt-in
workflow section for 69 directly exposed tools. Adding
`--defer-workflow-tools` instead hides those 41 workflow tools from the direct
catalog, exposes `tool_invoke`, and leaves them searchable through
`tool_search`: 29 tools are direct and 41 are deferred, while all 70 capabilities
available in that startup configuration remain reachable. These selections are fixed at startup;
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
requests and does not mutate the workspace. On macOS it also reports bounded
Apple toolchain metadata including architecture, macOS version, selected
developer directory, Xcode/Command Line Tools, Swift, SourceKit-LSP, codesign,
notarytool, xcresulttool, Homebrew, and Git presence without reading Keychain or
credential contents.

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
without advancing any output cursor. Running commands also report
`last_output_at`, `idle_seconds`, `runtime_seconds`, `activity_state`, and
`needs_attention`. `activity_state="long_silent"` means the process is still
running but has produced no output for at least 60 seconds; it is an attention
signal, not an automatic hang verdict, and the runtime does not kill it. An
operation still between acceptance and process registration reports
`status: "accepting"` and is safe to poll again.

### list_commands

Inputs: `"operation_id"`, `"max_results"`, `"workdir"`.

Annotations: `{"title":"List commands","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Lists recent active/retained commands plus any accepting operation record,
newest first. Registered commands include the same activity/idle fields as
`get_command`, allowing clients to surface long-silent work without consuming
output. `"operation_id"` optionally filters the list after a lost HTTP response
or reconnect.

Registered commands carry their canonical absolute `workdir` in execution,
status, polling and recovery results. Optional `workdir` filters this list by
exact canonical directory; relative inputs remain workspace-relative. Pending
acceptance records without a registered command are omitted from a directory-filtered list.

### write_stdin

Inputs: `"command_id"`, `"chars"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Annotations: `{"title":"Write stdin","readOnlyHint":false,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

Poll or interact with a command. Pass empty `chars` to wait for output.
Polling and initial `exec_command` results include the same activity health fields
as `get_command`, so a client can detect long-silent work without issuing an
extra status call.

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

Inputs: `"path"`, `"include_untracked"`, `"max_entries"`, `"repo_path"`.

Annotations: `{"title":"Git status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

All Git tools support optional `repo_path` to select one worktree inside the
configured workspace. Other `path` / `paths` arguments remain workspace-relative,
including when `repo_path` is present. Without `repo_path`, explicit file/directory
paths infer one repository; with no paths, the workspace itself is used. No global
current-project state is kept. Mixed repositories or conflicting explicit selection
return `"GIT_REPOSITORY_MISMATCH"`; an actual worktree root outside the workspace
returns `"GIT_REPOSITORY_OUTSIDE_WORKSPACE"`. Explicit non-Git selection returns
`GIT_NOT_REPOSITORY`, never a fallback.

Results identify absolute `repo_root` and `path_base`. Status/diff/Git-write paths
are repository-relative; input paths are still workspace-relative. `git_blame`
keeps its selected source `path` workspace-relative. Worktree listing/create/remove
paths are absolute. Status uses NUL-separated porcelain to preserve special names.
The opaque `index_fingerprint` binds both index content and worktree identity;
refresh old tokens through `git_status` after upgrading. Git location overrides
such as `GIT_DIR` and `GIT_INDEX_FILE` do not override an explicit target.

### git_diff

Inputs: `"path"`, `"paths"`, `"staged"`, `"unstaged"`, `"context_lines"`, `"max_bytes"`, `"repo_path"`.

Annotations: `{"title":"Git diff","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Real Git differences return `is_repo=true, diff_source="git"`. The legacy
non-Git patch-baseline comparison remains available only without explicit repository
selection and identifies `is_repo=false, diff_source="patch_baseline"`. It is not
an authoritative view of arbitrary filesystem changes. File filters are literal
paths, including names containing wildcard characters.

### git_log

Inputs: `"path"`, `"ref"`, `"max_count"`, `"skip"`, `"repo_path"`.

Annotations: `{"title":"Git log","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_show

Inputs: `"rev"`, `"path"`, `"paths"`, `"include_diff"`, `"context_lines"`, `"max_bytes"`, `"repo_path"`.

Annotations: `{"title":"Git show","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_blame

Inputs: `"path"`, `"rev"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"repo_path"`.

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

Inputs: `"symbol"`, `"path"`, `"line"`, `"column"`, `"prefer_lsp"`,
`"max_results"`, `"max_files"`.

Annotations: `{"title":"Code definition","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Finds exact symbol or qualified-name definitions using the same language-aware
indexing rules as `code_symbols`. Multiple definitions are returned for
overloads or duplicate names rather than guessing one winner.
When `prefer_lsp=true` and `path` is a source file with one-based `line` and
`column`, the tool first requests an LSP semantic definition. Runtime LSP
unavailability, timeout, exit, unsupported-language, or server error falls back
to the existing bounded symbol scan. Path/security/argument failures do not fall
back. Results identify `backend`, `fallback_used`, and coverage.

### code_references

Inputs: `"symbol"`, `"path"`, `"line"`, `"column"`, `"prefer_lsp"`,
`"include_declaration"`, `"case_sensitive"`, `"max_results"`, `"max_files"`.

Annotations: `{"title":"Code references","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Without a semantic position, finds exact identifier-token occurrences across
supported source files and returns bounded workspace-relative path, line,
column, and preview metadata. With `prefer_lsp=true` plus file/line/column it
first requests semantic references, falling back only for the same LSP runtime
failure classes as `code_definition`. Results identify the backend and whether
fallback occurred.

## Opt-in workflow toolset

The tools in this section are exposed only when the server starts with
`--enable-workflow-tools` (or `CODING_TOOLS_MCP_ENABLE_WORKFLOW_TOOLS=1`). The
selection remains fixed for the lifetime of the runtime and `listChanged` stays
`false`. Persistent state is stored outside the workspace in the platform
application-state directory, or under `--state-root`; it is partitioned by a
hash of the canonical workspace path. A task id does not create a new transport
session or client trust boundary.

### workspace_overview

Inputs: `"max_files"`, `"path"`.

Returns bounded manifest, language, entry-point, top-level area, and project
instruction metadata. `scan_complete` and `truncated` disclose coverage.
The optional `path` defaults to `.` and limits enumeration before applying the
file budget. File paths remain workspace-relative. Current applicable rules are
returned separately from startup discovery metadata. Apple workspaces also
return an `apple` block identifying `.xcodeproj`, `.xcworkspace`, `Package.swift`,
Swift source presence, and bounded local Xcode/Swift/SDK toolchain metadata.
Discovery is read-only and does not start Xcode.

### repo_map

Inputs: `"path"`, `"query"`, `"impact"`, `"changed_paths"`, `"max_files"`,
`"max_symbols"`, `"max_impact_files"`, `"max_impact_symbols"`.

Returns bounded code symbols grouped by file. `coverage` identifies the current
Python-AST and language-pattern implementation; this tool does not claim LSP
or compiler completeness. With `impact=true`, the tool also returns a bounded
deterministic change-impact estimate. Explicit `changed_paths` are workspace-
relative and must stay under the selected `path`; when they are omitted, the
runtime seeds impact analysis from current Git status. Impact analysis extracts
symbols from changed files, scans exact identifier references once across the
selected tree, ranks candidate impacted files, and surfaces likely test files
using reference and filename heuristics. The result includes `coverage`,
`limitations`, `scan_complete`, and `truncated`; it is intentionally described
as a heuristic rather than a compiler call graph.

### project_instructions

Inputs: `"path"`.

Returns root and nested `AGENTS.md`/`CLAUDE.md` files whose directory scope
contains the selected path, ordered from broad to narrow scope.
Rules are read along the target's ancestor chain on every call, independent of
startup scan limits. This observes newly added or changed rules. Reads are bounded
to 16 KiB per file and 64 KiB in total, deduplicate filesystem identities, reject
outside-workspace symlinks and disclose truncation or unreadable rules.

### skills_list

Inputs: `"max_results"`.

Lists metadata and content hashes for UTF-8 workspace
`.agents/skills/**/SKILL.md` entries. Listing or reading a Skill never executes
its scripts and cannot raise runtime permissions.

### skills_read

Inputs: `"path"`.

Reads one selected workspace Skill, with a 128 KiB limit and workspace/symlink
confinement.

### agent_environment

Inputs: `"provider"`, `"query"`, `"kind"`, `"max_items"`.

This tool is exposed only in `permission_mode=host`. It discovers metadata for
installed local agent environments (`codex`, `claude`, `gemini`, `cursor`, and
`opencode`) including CLI/home paths, Skill names, Codex enabled Plugin ids,
Plugin Skill names, worktrees, rule filenames, and selected local capability
presence. It reports only booleans for auth/browser-session/OAuth-like resources
and never returns their contents. Optional `query` plus `kind` performs a bounded
cross-environment metadata search over `skill`, `plugin_skill`, `plugin`, `rule`,
`worktree`, or `capability` entries. Search mode returns compact provider summaries
plus flattened matches instead of repeating full discovered metadata. It does not
execute an agent CLI or Skill.

### checks_discover

Inputs: `"path"`, `"recommend"`, `"changed_paths"`.

Discovers bounded test, lint, typecheck, aggregate, and build commands from
recognized project files without executing them. `Package.swift` contributes
`swift build` and `swift test`. On macOS, when exactly one top-level
`.xcodeproj` or `.xcworkspace` exists and `xcodebuild` is available, discovery
adds a read-only `xcodebuild ... -list -json` metadata check; it does not guess
schemes, destinations, simulators, or devices.

By default, `checks_discover` also reads bounded current Git changes for the
selected target and ranks only the already-discovered checks. Callers may pass
explicit `changed_paths` or disable recommendation with `recommend=false`.
Recommendation is deterministic changed-path metadata only: Python changes
prefer Python checks, JavaScript/TypeScript prefer npm, Rust prefers Cargo, Go
prefers Go, Swift prefers SwiftPM, Xcode project metadata prefers the Xcode
metadata check, and Make checks act as a cross-language fallback. Each check
reports `recommended`, `priority`, `recommendation_score`, and bounded
`recommendation_reasons`; the response also includes ordered
`recommended_check_ids`. Recommendation does not create a new command, execute
anything, or change `checks_run` semantics.

### checks_run

Inputs: `"check_id"`, `"path"`, `"task_id"`, `"operation_id"`, `"timeout_ms"`,
`"yield_time_ms"`, `"max_output_bytes"`, `"max_diagnostics"`, `"approval_ids"`.

Re-discovers the requested check and runs its exact command through the existing
`exec_command` policy and command manager. Unknown or removed checks return
`CHECK_NOT_FOUND`; command handles and retry deduplication keep their existing
semantics. Bounded structured diagnostics are parsed from retained output for
common pytest/mypy/ruff/rustc/TypeScript/Go/ESLint shapes plus high-signal
Swift/Clang locations, XCTest failures, Xcode build/test failure markers,
codesign errors, and notarytool errors. `diagnostics`, `failing_tests`, parser
metadata, and truncation flags are advisory; raw command output remains
authoritative and is still available through the normal output references.

Completed and running checks receive a persistent `check_run_id`. When
`task_id` is supplied, the run is also appended to that task's event history.
The evidence records bounded output and code fingerprints before and after the
command.

### checks_result

Inputs: `"check_run_id"`, `"max_diagnostics"`.

Returns persisted check evidence. A retained running command is refreshed from
the command manager; after a runtime restart an unresolvable running command is
reported as `unknown`/interrupted without erasing any previously retained warning.
Structured diagnostics are preserved in check evidence and can be rebuilt from
retained command output when a long-running check completes. This parsing is
heuristic and bounded; the raw output remains the source of truth.
`stale` is true when the current code fingerprint differs
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

Returns the task record together with recent events and linked check, file
checkpoint, context checkpoint, and review evidence so a client can resume after
reconnecting or restarting the runtime.

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

### context_checkpoint

Inputs: `"action"`, `"context_checkpoint_id"`, `"label"`, `"summary"`,
`"decisions"`, `"unresolved"`, `"next_steps"`, `"task_id"`, `"max_results"`.

This is a model-free cross-session context metadata store. `action="create"`
requires a semantic `summary` supplied by the current caller and locally records
the current workspace, Git state, running-command set, runtime capability state,
and SHA-256 fingerprints of those deterministic sections. `action="get"`
recomputes the deterministic state and reports `stale` plus bounded reasons such
as `git_state_changed`, `running_commands_changed`, or `capabilities_changed`.
It also returns structured `drift` explaining changed HEAD/branch/paths, commands
that started or finished, and capability differences, plus a compact `resume`
packet containing the saved summary/decisions/unresolved/next steps, freshness,
recommended reads, and recommended actions. `task_context` automatically embeds
the latest linked checkpoint's compact `resume` packet when one exists.
`action="list"` returns bounded checkpoint metadata. The combined deterministic
and semantic payload is limited to 128 KiB. Supplying `task_id` links the context
checkpoint into `task_context`. The tool does not call Codex compact or any
other model service.

### git_branch_list

Inputs: `"max_results"`, `"repo_path"`.

Lists local branches and returns the current `head` and `index_fingerprint`.
Those values are concurrency tokens for every Git write tool.

### git_branch_create

Inputs: `"name"`, `"start_point"`, `"checkout"`, `"expected_head"`,
`"expected_index_fingerprint"`, `"repo_path"`.

Validates the branch name with Git and creates a local branch only while HEAD
and index match the reviewed state. Checkout is explicit and defaults to false.

### git_conflicts

Inputs: `"repo_path"` (optional).

Lists unmerged paths and all index stages. It also returns current HEAD and
index fingerprints and never resolves conflicts automatically.

### git_stage

Inputs: `"paths"`, `"expected_head"`, `"expected_index_fingerprint"`, `"repo_path"`.

Stages 1-200 unique explicit workspace paths. The workspace root is rejected,
and stale HEAD/index values return retryable `GIT_STATE_CONFLICT`.

### git_unstage

Inputs: `"paths"`, `"expected_head"`, `"expected_index_fingerprint"`, `"repo_path"`.

Removes only the explicit paths from the index through `git restore --staged`.
Working-tree content is preserved.

### git_commit

Inputs: `"paths"`, `"message"`, `"expected_head"`,
`"expected_index_fingerprint"`, `"repo_path"`.

Commits only when the complete staged path set exactly equals `paths`; unrelated
staged content returns `GIT_COMMIT_SCOPE_MISMATCH`. Hooks and configured signing
may run and their failures are returned as `GIT_ERROR`. This tool does not push.

### git_worktree_list

Inputs: `"repo_path"` (optional).

Annotations: `{"title":"List Git worktrees","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Lists all repository worktrees and marks entries stored under this workspace's
private workflow state as managed.

### git_worktree_create

Inputs: `"worktree_id"`, `"branch"`, `"create_branch"`, `"start_point"`,
`"expected_head"`, `"expected_index_fingerprint"`, `"repo_path"`.

Annotations: `{"title":"Create Git worktree","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Creates an isolated checkout under private workflow state after HEAD and index
concurrency checks. It can create a new branch or attach an existing branch and
returns the absolute path so the Desktop app can register it as a workspace.
Nested repositories use separate managed-worktree namespaces; the same
worktree_id in another repository does not alias this directory. An external
managed checkout is not automatically added to the current workspace's file scope.

### git_worktree_remove

Inputs: `"worktree_id"`, `"repo_path"`.

Annotations: `{"title":"Remove Git worktree","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

Removes only a runtime-managed worktree. Dirty or untracked content returns
`GIT_WORKTREE_DIRTY`; the branch is always preserved.
Removal verifies the checkout's actual Git common directory against the selected
repository. MCP write calls coordinate their check-and-write sections per Git
common directory within the process; this does not lock out arbitrary external
writers or implement multi-round agent scheduling.

### lsp_status

Inputs: none.

Reports available/running Python, TypeScript/JavaScript, Rust, and Swift
language-server backends, their commands and supported extensions. Rust uses `rust-analyzer` and
is rooted at the nearest ancestor `Cargo.toml`; a rustup proxy without the actual
component installed is reported unavailable. Backends start only when a semantic
operation first needs them.
Swift uses `sourcekit-lsp`, first from PATH and then from `xcrun --find
sourcekit-lsp`, and roots at the nearest `Package.swift`, `.xcodeproj`, or
`.xcworkspace` boundary.
Python and TypeScript also use nearest language/project configuration markers,
stopping at the nearest Git worktree boundary instead of borrowing parent/neighbor
configuration. Backends are reused by language and project root. This does not
automatically install servers, activate virtual environments or execute shell profiles.

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
Results include `project_root`, `document_version`, `diagnostics_version` and
`freshness` (`fresh`, `unversioned`, `pending`, `stale`). Only an explicitly matching
document version is fresh; versionless publications remain unconfirmed. Waiting
is URI/version-aware, late older-version notifications cannot overwrite current
results, and a file changed during the query is stale. No latest diagnostics is
not proof that a document has no errors.

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
`"max_bytes"`, `"repo_path"`.

Persists a bounded review snapshot containing Git status and diff, applicable
project instructions, optional task evidence, and a code fingerprint. Preparing
materials does not claim that an AI or human has reviewed them.
The selected directory scopes diff paths, applicable rules and fingerprint input;
explicit file filters outside it are rejected. With repo_path and no path, the
selected repository directory is used. Fingerprints enumerate the target before
applying budgets, so a sibling project does not consume its scan budget.

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
