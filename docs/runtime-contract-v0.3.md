# Coding Tools MCP Runtime Contract v0.3

Status: implemented contract for `coding-tools-mcp` 0.3.x. The frozen contract
for 0.2.x is [runtime-contract-v0.2.md](runtime-contract-v0.2.md); what changed
between them, and what a client has to do about it, is
[migration-0.3.md](migration-0.3.md).

Protocol targets: MCP `2026-07-28`, which serves every request on its own, and
the handshake era `2025-11-25` with explicit compatibility for `2025-06-18`.

This contract describes one stable, model-neutral coding tool set. There are no
tool profiles and the server does not add or remove process tools dynamically.
`apply_patch` is the only direct file-mutation primitive; `edit_file` is not
provided. Permission modes alter command policy, not the advertised catalog.

One switch, `--dangerously-fake-readonly-annotations`, rewrites the exposure hints
in `tools/list` for clients that refuse mutating tools by annotation. It is not a
tool profile: the catalog, the schemas, and what every tool actually does are all
unchanged, and no tool is hidden. It requires `dangerous` or `host` permission mode, requires
authentication over HTTP, and is reported by `server_info.annotation_override` and
the server card, both of which continue to publish the real annotations recorded
below. Unless that switch is set, the annotations in this document are what
`tools/list` returns.

## Two protocol eras, one server

Both eras are served by the one runtime that owns the workspace, and neither
leaves state behind. Which era a request belongs to is decided by the request
alone: a `params._meta` carrying `io.modelcontextprotocol/protocolVersion` is a
`2026-07-28` request, and everything else is a handshake-era one. A legacy
`_meta` such as `progressToken` does not make a request modern, and `initialize`
is always the handshake, whatever `_meta` it carries.

The only advertised server capability is stable tools with `listChanged: false`,
in both eras. Logging, resources, prompts, sampling, and elicitation are not
advertised.

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
`notifications/cancelled` is accepted here as well. Its `params._meta` carries:

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
  for `tools/call` and `prompts/get`, `params.uri` for `resources/read`. A
  value that cannot travel as an HTTP field is wrapped as
  `=?base64?<base64 of the UTF-8 value>?=`, whose payload may not exceed 8192
  characters. No other method takes this header, `server/discover` included.

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
  "capabilities": {"tools": {"listChanged": false}},
  "instructions": "...",
  "resultType": "complete",
  "ttlMs": 0,
  "cacheScope": "private",
  "_meta": {
    "io.modelcontextprotocol/serverInfo": {
      "name": "coding-tools-mcp",
      "title": "Coding Tools MCP",
      "version": "0.3.6"
    }
  }
}
```

`supportedVersions` lists the modern versions only. The handshake versions are
negotiated by `initialize` and are not accepted in `_meta`, so naming one here
would invite a client to retry a version that cannot work.

A `server/discover` that carries no modern `_meta` is a handshake-era request
for a method this server does not implement in that era, and is answered with
`-32601`. That is what sends a client which probes before it handshakes to
`initialize`, which works.

### Result encoding

A `2026-07-28` result carries `resultType: "complete"` and an
`_meta.io.modelcontextprotocol/serverInfo` naming this server. The results
whose content a client might be tempted to keep — `tools/list` and
`server/discover` — also carry `ttlMs: 0` and `cacheScope: "private"` on the
result root. Both are shaped by the workspace and the permission mode they were
served under, and the discover instructions quote the workspace's own
instruction files, so the conservative defaults are the correct ones: never
shared, never reused. A tool result that failed still reports
`resultType: "complete"` with `isError: true`; the envelope was complete, the
tool was not.

### Errors and HTTP statuses

| Code | Meaning | HTTP status of a `2026-07-28` request |
| --- | --- | --- |
| `-32600` | invalid request envelope | `200`, in either era |
| `-32601` | unknown method | `404` |
| `-32602` | invalid params, including a missing or mistyped required `_meta` field | `400` |
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
- Direct path inputs are workspace-relative and always resolve against the
  workspace root. Absolute paths, `..` traversal, NUL bytes, and symlink
  escapes are rejected.
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
["ABSOLUTE_PATH_DENIED", "ACCESSIBILITY_PERMISSION_REQUIRED", "APP_CONTROL_ERROR", "APP_HELPER_ERROR", "BINARY_FILE", "BROWSER_ERROR", "BROWSER_TIMEOUT", "CHROME_EXTENSION_ERROR", "CHROME_EXTENSION_UNAVAILABLE", "COMMAND_CLOSED", "COMMAND_LIMIT_REACHED", "COMMAND_NOT_FOUND", "ELICITATION_UNSUPPORTED", "GIT_ERROR", "INTERNAL_ERROR", "INVALID_ARGUMENT", "IS_DIRECTORY", "NOT_A_DIRECTORY", "NOT_FOUND", "OPERATION_CONFLICT", "OPERATION_NOT_FOUND", "OPERATION_PENDING", "OUTPUT_TOO_LARGE", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_UNAVAILABLE", "SCREEN_RECORDING_PERMISSION_REQUIRED", "SYMLINK_ESCAPE", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING", "UNSUPPORTED_PLATFORM"]
```

Error categories are `validation`, `security`, `permission`, `runtime`,
`not_found`, `conflict`, and `internal`.

Malformed JSON-RPC uses standard protocol errors: parse `-32700`, invalid
request `-32600`, unknown method `-32601`, invalid params/tool `-32602`, and
unexpected server failure `-32603`. The two codes the modern era adds are
`-32020` and `-32022`, described above.

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

The default catalog has 51 tools, including `view_image`. Setting
`CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE=0` is the sole installation capability gate
and removes only that optional binary-content tool. It is not a tool profile.

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
metadata, exec policy, and the static retained-output budget. It reports no
per-session value and no runtime counter: there is no session, and how often a
budget was hit is a property of the process rather than an answer to whichever
client asked. Those counters travel with telemetry.

### check_exec_environment

Inputs: none.

Annotations: `{"title":"Check exec environment","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Returns lightweight policy and Landlock status without running active probes.

### read_file

Inputs: `"path"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"max_bytes"`, `"encoding"`.

Annotations: `{"title":"Read file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

Reads UTF-8 ranges as a stream, reports full file line/byte metadata, rejects
binary content, and returns continuation metadata when bounded. The
continuation repeats the workspace-relative path it was given.

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

Inputs: `"cmd"`, `"operation_id"`, `"workdir"`, `"cwd"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`, `"stdin"`, `"tty"`, `"env"`.

Annotations: `{"title":"Execute command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Statuses are `exited`, `running`, `timeout`, `terminated`, or `failed`.
Launch/policy failures use the error envelope with `status: "failed"`; signal
exits use `terminated`. Ordinary non-zero exit codes still use `exited`.
`"workdir"` is workspace-relative and defaults to the workspace root.
`"operation_id"` is optional. Within one workspace it makes retries of an
identical execution idempotent for the retained-command lifetime: a duplicate
returns the existing `command_id`; different execution parameters under the
same id return `OPERATION_CONFLICT`.

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

Annotations: `{"title":"Request permissions","readOnlyHint":true,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

The current server does not advertise MCP elicitation. This tool therefore
returns `ELICITATION_UNSUPPORTED`, except that dangerous and host modes report
the operator's explicit auto-grant policy. It never silently escalates safe mode.

### view_image

Inputs: `"path"`, `"max_bytes"`, `"max_width"`, `"max_height"`, `"auto_resize"`.

Annotations: `{"title":"View image","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

The base64 data appears exactly once, in one MCP image content block. Stable
`structuredContent` contains metadata only; it has no duplicate base64 or data
URL. Pillow is optional and used only for requested auto-resize.

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

Inputs: `"selector"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`.

Annotations: `{"title":"Browser click","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

Clicks the first element matching the supplied Playwright selector.

### browser_type

Inputs: `"selector"`, `"text"`, `"endpoint"`, `"tab_index"`, `"tab_id"`, `"timeout_ms"`, `"clear"`, `"delay_ms"`.

Annotations: `{"title":"Browser type","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

By default fills the first matching element, replacing its current value. With
`clear=false`, it types sequentially and optionally applies `delay_ms` between
characters.

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
