# Coding Tools MCP Runtime Contract v0.4

Status: current macOS-first runtime contract. This repository intentionally does not retain older runtime contracts or migration compatibility layers.

## Product boundary

The runtime exposes low-level coding primitives over MCP. Planning, tasks, reviews, checkpoints, Skills, local-agent discovery, Git write workflows, and agent orchestration are deliberately outside the runtime. Native developer tools are invoked through `exec_command` when their behavior is already well-defined by macOS or the underlying CLI.

`apply_patch` is the only direct text/source editing primitive. Project routing is handled by the persistent Project Gateway and is separate from the immutable per-project Runtime.

## Protocol and transport

- Streamable HTTP uses `/mcp`; stdio uses newline-delimited JSON-RPC.
- Supported protocol versions are `2026-07-28`, `2025-11-25`, and `2025-06-18`.
- Normal single-project runtimes do not keep transport session state. Gateway mode uses `Mcp-Session-Id` only for `session -> project` routing.
- `tools/list` is static for the lifetime of a Runtime (`listChanged: false`).
- `content` is bounded model-facing text; `structuredContent` is the complete machine result.
- `notifications/cancelled` does not terminate a running command; use `kill_command`.

## Tool-surface rule

The implementation currently registers exactly 29 tools. CI caps the registered inventory at 32 tools, total input-schema size at 16,000 bytes, and any one schema at 2,048 bytes. Every enabled capability is directly listed; there is no secondary tool-discovery gateway.

New tools require a capability or safety advantage that cannot be obtained cleanly by composing existing primitives. A wrapper around a native CLI is not sufficient justification.

## Command lifecycle

`exec_command` owns process creation. Running commands return `command_id`; `get_command`, `list_commands`, `write_stdin`, `read_output`, and `kill_command` reuse that identity instead of starting duplicate processes. `operation_id` may be used for recoverable execution deduplication.

`kill_command` terminal/status values are: ["terminated", "killed", "exited", "terminating", "not_found"].

## Project and permission boundary

Relative paths and default searches are rooted in the selected project. Permission scope is the maximum authority, not the default search scope. In Gateway mode, project selection is session-local and never follows the Desktop frontmost window.

Permission modes are `safe`, `trusted`, `dangerous`, and `host`. `host` is an explicit full-host development mode; it is not the default project boundary. Network command policy remains independently selectable as `deny`, `allowlist`, or `unrestricted`.

## macOS-native preference

On macOS the runtime prefers existing developer infrastructure instead of MCP wrappers: SourceKit-LSP for Swift semantics, `xcodebuild`/SwiftPM for builds and tests, native `git` for branch/stage/commit/worktree operations, Homebrew and Apple signing/notarization CLIs through `exec_command`, and system metadata exposed by `runtime_doctor`.

## Fixed inventory

### project_context

Exposure: **direct**; gated by `enable_project_gateway`.

List projects registered with a persistent gateway, inspect the project bound to this MCP session, or explicitly bind this session to another project without restarting the gateway.

Inputs: `"action"`, `"project_id"`.

Annotations: `{"title":"Project context","readOnlyHint":false,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

### server_info

Exposure: **direct**.

Return server, workspace, project-context, auth, policy, and fixed-tool metadata.

Inputs: none.

Annotations: `{"title":"Server info","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### runtime_doctor

Exposure: **direct**.

Run a non-destructive runtime health check covering common toolchain commands, workspace access, shell snapshot, LSP availability, sandbox status, network policy, and macOS Apple toolchain metadata including Xcode, Swift, SourceKit-LSP, codesign, notarytool, xcresulttool, and Homebrew.

Inputs: none.

Annotations: `{"title":"Runtime doctor","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### read_file

Exposure: **direct**.

Read a UTF-8 text file slice inside the configured file scope. Relative paths are workspace-relative; host mode also accepts host absolute and ~/... paths.

Inputs: `"path"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"max_bytes"`, `"encoding"`.

Required: `"path"`.

Annotations: `{"title":"Read file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### read_files

Exposure: **direct**.

Read bounded UTF-8 slices from multiple files in the configured file scope.

Inputs: `"requests"`, `"max_total_bytes"`.

Required: `"requests"`.

Annotations: `{"title":"Read files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### list_dir

Exposure: **direct**.

List directory entries inside the configured file scope.

Inputs: `"path"`, `"recursive"`, `"max_depth"`, `"max_entries"`, `"include_hidden"`, `"include_ignored"`, `"sort"`.

Annotations: `{"title":"List directory","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### list_files

Exposure: **direct**.

List files in the configured file scope using glob filters.

Inputs: `"path"`, `"patterns"`, `"glob"`, `"exclude_patterns"`, `"include_hidden"`, `"include_ignored"`, `"max_results"`, `"sort"`.

Annotations: `{"title":"List files","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### search_text

Exposure: **direct**.

Search UTF-8 files in the configured file scope for text or regex matches.

Inputs: `"query"`, `"path"`, `"regex"`, `"case_sensitive"`, `"include_globs"`, `"glob"`, `"exclude_globs"`, `"context_lines"`, `"max_results"`, `"max_preview_bytes"`.

Required: `"query"`.

Annotations: `{"title":"Search text","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### apply_patch

Exposure: **direct**.

Stage, validate, and atomically apply a patch envelope. Example: *** Begin Patch
*** Update File: app.py
@@
-old
+new
*** End Patch

Inputs: `"patch"`, `"dry_run"`.

Required: `"patch"`.

Annotations: `{"title":"Apply patch","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

### exec_command

Exposure: **direct**.

Run a bounded command under runtime policy. Pass workdir explicitly for reconnect-safe paths. A still-running command returns command_id. Example: {"cmd":"pytest -q","workdir":".","yield_time_ms":30000}. Retained output is bounded per stream; for very large output redirect to a file (cmd > out.log 2>&1) and page it with read_file or search_text.

Inputs: `"cmd"`, `"approval_ids"`, `"operation_id"`, `"workdir"`, `"cwd"`, `"timeout_ms"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`, `"stdin"`, `"tty"`, `"env"`.

Required: `"cmd"`.

Annotations: `{"title":"Execute command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":true}`.

### get_command

Exposure: **direct**.

Read command status without consuming output cursors. Resolve by command_id or operation_id; returned output_refs can be paged with read_output.

Inputs: `"command_id"`, `"operation_id"`.

Annotations: `{"title":"Get command","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### list_commands

Exposure: **direct**.

List recent server-managed commands and operation_ids for reconnect/recovery. This is read-only and does not consume command output.

Inputs: `"operation_id"`, `"max_results"`, `"workdir"`.

Annotations: `{"title":"List commands","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### write_stdin

Exposure: **direct**.

Poll or interact with a running command by command_id. Empty chars wait for output; non-empty chars writes to stdin. Example: {"command_id":"abc","chars":"","yield_time_ms":10000}.

Inputs: `"command_id"`, `"chars"`, `"yield_time_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Required: `"command_id"`.

Annotations: `{"title":"Write stdin","readOnlyHint":false,"destructiveHint":false,"idempotentHint":false,"openWorldHint":false}`.

### kill_command

Exposure: **direct**.

Terminate a server-managed command by command_id. Example: {"command_id":"abc","signal":"KILL"}.

Inputs: `"command_id"`, `"signal"`, `"wait_ms"`, `"kill_wait_ms"`, `"max_output_bytes"`, `"verbosity"`, `"preview_bytes"`.

Required: `"command_id"`.

Annotations: `{"title":"Kill command","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

### read_output

Exposure: **direct**.

Read retained command output using an output_ref returned by exec_command/write_stdin. Each stream retains the earliest output (head) plus the most recent output (rolling tail); bytes between them may be evicted and are reported via evicted_gap_bytes. Example: {"output_ref":"command:abc:stdout","offset":0,"limit":4096}.

Inputs: `"output_ref"`, `"stream"`, `"offset"`, `"limit"`.

Required: `"output_ref"`.

Annotations: `{"title":"Read output","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_status

Exposure: **direct**.

Return git working tree status for the workspace.

Inputs: `"path"`, `"include_untracked"`, `"max_entries"`, `"repo_path"`.

Annotations: `{"title":"Git status","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_diff

Exposure: **direct**.

Return unified git diff for workspace changes.

Inputs: `"path"`, `"paths"`, `"staged"`, `"unstaged"`, `"context_lines"`, `"max_bytes"`, `"repo_path"`.

Annotations: `{"title":"Git diff","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_log

Exposure: **direct**.

Return recent git commits with bounded structured metadata.

Inputs: `"path"`, `"ref"`, `"max_count"`, `"skip"`, `"repo_path"`.

Annotations: `{"title":"Git log","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_show

Exposure: **direct**.

Return bounded git show output for a revision.

Inputs: `"rev"`, `"path"`, `"paths"`, `"include_diff"`, `"context_lines"`, `"max_bytes"`, `"repo_path"`.

Annotations: `{"title":"Git show","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### git_blame

Exposure: **direct**.

Return bounded git blame metadata for a workspace file.

Inputs: `"path"`, `"rev"`, `"start_line"`, `"end_line"`, `"max_lines"`, `"repo_path"`.

Required: `"path"`.

Annotations: `{"title":"Git blame","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### code_diagnostics

Exposure: **direct**.

Open or refresh a source file and return bounded published language-server diagnostics.

Inputs: `"path"`, `"wait_ms"`, `"max_results"`.

Required: `"path"`.

Annotations: `{"title":"Code diagnostics","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### request_permissions

Exposure: **direct**.

Create an exact, expiring operator approval request without silently granting operations.

Inputs: `"tool_name"`, `"permission"`, `"reason"`, `"arguments"`, `"scope"`, `"ttl_seconds"`.

Required: `"tool_name"`, `"permission"`, `"reason"`, `"arguments"`.

Annotations: `{"title":"Request permissions","readOnlyHint":false,"destructiveHint":true,"idempotentHint":false,"openWorldHint":false}`.

### workspace_overview

Exposure: **direct**.

Summarize project manifests, languages, entry points, top-level areas, and instruction files. Detected Apple projects also include bounded read-only Xcode, Swift, SDK, and SourceKit-LSP metadata.

Inputs: `"path"`, `"max_files"`.

Annotations: `{"title":"Workspace overview","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### project_instructions

Exposure: **direct**.

Resolve root and nested project instruction files that apply to one workspace path.

Inputs: `"path"`.

Annotations: `{"title":"Project instructions","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### checks_discover

Exposure: **direct**.

Discover test, lint, typecheck, and build commands from project manifests without running them. By default, current Git changes deterministically rank the existing checks and explain why; explicit changed_paths can override the seed.

Inputs: `"path"`, `"recommend"`, `"changed_paths"`.

Annotations: `{"title":"Discover checks","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### view_image

Exposure: **direct**; gated by `enable_view_image`.

Return a workspace image as MCP image content.

Inputs: `"path"`, `"max_bytes"`, `"max_width"`, `"max_height"`, `"auto_resize"`.

Required: `"path"`.

Annotations: `{"title":"View image","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### code_symbols

Exposure: **direct**.

List bounded language-aware symbol definitions under a workspace path.

Inputs: `"path"`, `"query"`, `"kind"`, `"max_results"`, `"max_files"`.

Annotations: `{"title":"Code symbols","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### code_definition

Exposure: **direct**.

Find language-aware definitions for a symbol under a workspace path. When a file path plus line/column is supplied, line/column are one-based and semantic LSP is preferred; the runtime converts the column to UTF-16 and falls back to the bounded symbol scan if the LSP runtime is unavailable.

Inputs: `"symbol"`, `"path"`, `"line"`, `"column"`, `"prefer_lsp"`, `"max_results"`, `"max_files"`.

Required: `"symbol"`.

Annotations: `{"title":"Code definition","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

### code_references

Exposure: **direct**.

Find references for a symbol under a workspace path. When a file path plus line/column is supplied, prefer semantic LSP references using one-based public positions converted internally to UTF-16; fall back to the bounded exact-identifier scan if the LSP runtime is unavailable.

Inputs: `"symbol"`, `"path"`, `"line"`, `"column"`, `"prefer_lsp"`, `"include_declaration"`, `"case_sensitive"`, `"max_results"`, `"max_files"`.

Required: `"symbol"`.

Annotations: `{"title":"Code references","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false}`.

## Errors

Tool-domain failures use the standard MCP error envelope with a stable `error.code`, category, retryability, and bounded details. Current literal tool failure codes are:

```json
["ABSOLUTE_PATH_DENIED", "APPROVAL_EXPIRED", "APPROVAL_NOT_FOUND", "APPROVAL_NOT_USABLE", "APPROVAL_SCOPE_MISMATCH", "BINARY_FILE", "COMMAND_CLOSED", "COMMAND_LIMIT_REACHED", "COMMAND_NOT_FOUND", "GATEWAY_REQUIRED", "GIT_ERROR", "GIT_NOT_REPOSITORY", "GIT_REPOSITORY_MISMATCH", "GIT_REPOSITORY_OUTSIDE_WORKSPACE", "GIT_STATE_CONFLICT", "INTERNAL_ERROR", "INVALID_ARGUMENT", "IS_DIRECTORY", "LSP_EDIT_UNSUPPORTED", "LSP_ERROR", "LSP_EXITED", "LSP_LANGUAGE_UNSUPPORTED", "LSP_PATH_OUTSIDE_WORKSPACE", "LSP_TIMEOUT", "LSP_UNAVAILABLE", "NOT_A_DIRECTORY", "NOT_FOUND", "OPERATION_CONFLICT", "OPERATION_NOT_FOUND", "OPERATION_PENDING", "OUTPUT_TOO_LARGE", "PATCH_CONFLICT", "PATCH_CONTEXT_AMBIGUOUS", "PATCH_CONTEXT_NOT_FOUND", "PATCH_FAILED", "PATCH_HUNKS_OVERLAP", "PATCH_ROLLBACK_FAILED", "PATH_OUTSIDE_FILE_SCOPE", "PATH_OUTSIDE_WORKSPACE", "PERMISSION_REQUIRED", "RUNTIME_DIR_UNWRITABLE", "SANDBOX_UNAVAILABLE", "SYMLINK_ESCAPE", "TTY_UNSUPPORTED", "UNSUPPORTED_ENCODING"]
```

Non-retryable model-facing errors explicitly say not to repeat the same call unchanged. Permission failures identify the required operator action; runtime failures preserve bounded diagnostic evidence.

## Result contract

Every successful call returns `isError: false`, model-facing `content`, and complete `structuredContent`. Values required by the next call (for example `command_id`, `output_ref`, project identity, retry metadata, and `next_action`) must remain visible to clients that only forward text as well as in structured content where applicable.

## Deliberately absent capabilities

The MCP does not provide task/plan persistence, review records, checkpoints, Skills execution or indexing, local-agent enumeration, high-level bug-fix/refactor wrappers, Git write/worktree wrappers, model routing, web search, image generation, plugin installation, or subagent orchestration. These belong to the calling agent/client or to native developer tools.
