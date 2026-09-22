# Tools And Schemas

The normative behavior is [runtime-contract-v0.4.md](runtime-contract-v0.4.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

The runtime is macOS-first and intentionally exposes a small direct set of coding
primitives rather than an agent workflow engine or a second tool-discovery layer.

## Fixed inventory

The implementation declares exactly 29 tools. A normal macOS project runtime
exposes every enabled capability directly in one stable `tools/list`; Gateway mode
adds only `project_context`. There is no workflow-tool or discovery-gateway mode.

- `project_context`: **gateway-only direct** — List projects registered with a persistent gateway, inspect the project bound to this MCP session, or explicitly bind this session to another project without restarting the gateway.
- `server_info`: **direct** — Return server, workspace, project-context, auth, policy, and fixed-tool metadata.
- `runtime_doctor`: **direct** — Run a non-destructive runtime health check covering common toolchain commands, workspace access, shell snapshot, LSP availability, sandbox status, network policy, and macOS Apple toolchain metadata including Xcode, Swift, SourceKit-LSP, codesign, notarytool, xcresulttool, and Homebrew.
- `read_file`: **direct** — Read a UTF-8 text file slice inside the configured file scope. Relative paths are workspace-relative; host mode also accepts host absolute and ~/... paths.
- `read_files`: **direct** — Read bounded UTF-8 slices from multiple files in the configured file scope.
- `list_dir`: **direct** — List directory entries inside the configured file scope.
- `list_files`: **direct** — List files in the configured file scope using glob filters.
- `search_text`: **direct** — Search UTF-8 files in the configured file scope for text or regex matches.
- `apply_patch`: **direct** — Stage, validate, and atomically apply a patch envelope. Example: *** Begin Patch *** Update File: app.py @@ -old +new *** End Patch
- `exec_command`: **direct** — Run a bounded command under runtime policy. Pass workdir explicitly for reconnect-safe paths. A still-running command returns command_id. Example: {"cmd":"pytest -q","workdir":".","yield_time_ms":30000}. Retained output is bounded per stream; for very large output redirect to a file (cmd > out.log 2>&1) and page it with read_file or search_text.
- `get_command`: **direct** — Read command status without consuming output cursors. Resolve by command_id or operation_id; returned output_refs can be paged with read_output.
- `list_commands`: **direct** — List recent server-managed commands and operation_ids for reconnect/recovery. This is read-only and does not consume command output.
- `write_stdin`: **direct** — Poll or interact with a running command by command_id. Empty chars wait for output; non-empty chars writes to stdin. Example: {"command_id":"abc","chars":"","yield_time_ms":10000}.
- `kill_command`: **direct** — Terminate a server-managed command by command_id. Example: {"command_id":"abc","signal":"KILL"}.
- `read_output`: **direct** — Read retained command output using an output_ref returned by exec_command/write_stdin. Each stream retains the earliest output (head) plus the most recent output (rolling tail); bytes between them may be evicted and are reported via evicted_gap_bytes. Example: {"output_ref":"command:abc:stdout","offset":0,"limit":4096}.
- `git_status`: **direct** — Return git working tree status for the workspace.
- `git_diff`: **direct** — Return unified git diff for workspace changes.
- `git_log`: **direct** — Return recent git commits with bounded structured metadata.
- `git_show`: **direct** — Return bounded git show output for a revision.
- `git_blame`: **direct** — Return bounded git blame metadata for a workspace file.
- `code_diagnostics`: **direct** — Open or refresh a source file and return bounded published language-server diagnostics.
- `request_permissions`: **direct** — Create an exact, expiring operator approval request without silently granting operations.
- `workspace_overview`: **direct** — Summarize project manifests, languages, entry points, top-level areas, and instruction files. Detected Apple projects also include bounded read-only Xcode, Swift, SDK, and SourceKit-LSP metadata.
- `project_instructions`: **direct** — Resolve root and nested project instruction files that apply to one workspace path.
- `checks_discover`: **direct** — Discover test, lint, typecheck, and build commands from project manifests without running them. By default, current Git changes deterministically rank the existing checks and explain why; explicit changed_paths can override the seed.
- `view_image`: **direct when image content is enabled** — Return a workspace image as MCP image content.
- `code_symbols`: **direct** — List bounded language-aware symbol definitions under a workspace path.
- `code_definition`: **direct** — Find language-aware definitions for a symbol under a workspace path. When a file path plus line/column is supplied, line/column are one-based and semantic LSP is preferred; the runtime converts the column to UTF-16 and falls back to the bounded symbol scan if the LSP runtime is unavailable.
- `code_references`: **direct** — Find references for a symbol under a workspace path. When a file path plus line/column is supplied, prefer semantic LSP references using one-based public positions converted internally to UTF-16; fall back to the bounded exact-identifier scan if the LSP runtime is unavailable.

The direct catalog contains file reads/search, `apply_patch`, command lifecycle, Git
evidence, code navigation/diagnostics, project-safe permission requests, project
orientation/instructions, check discovery, and runtime diagnostics. `project_context`
appears only on the persistent Project Gateway; `view_image` is present when image
content is enabled. `listChanged` remains `false` because the catalog is fixed for
the Runtime lifetime.

Planning, task history, review records, check-result persistence, checkpoints,
protocol Tasks, workspace Skills, local-agent discovery, Git write workflows,
and workspace hooks are intentionally not MCP capabilities. The model/client
owns planning and review context; native macOS/Unix developer tools invoked via
`exec_command` own build, Git-write, worktree, and automation workflows;
`command_id` is the single lifecycle for long-running commands.
## Result envelope

Every successful tool call has:

```json
{
  "content": [{"type": "text", "text": "Agent-readable summary or bounded preview"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

`content` is not a JSON mirror. `structuredContent` is the complete machine
interface and retains existing fields where possible. Normal model-facing text
is governed by each tool's own result limits, with a final 2,162,688-byte
defense-in-depth ceiling for pathological count-bounded entries. If that safety
ceiling is reached, the full structured value is still present. Errors use the
same envelope with readable recovery guidance and `isError: true`.

`view_image` is the exception to text-only content: its base64 appears exactly
once in one `image` block. `structuredContent` contains path, media type, byte
count, dimensions, resize metadata, and warnings, but no base64 or data URL.

## Patch behavior

`apply_patch` accepts the standard envelope:

```text
*** Begin Patch
*** Add File: path/to/new.py
+content
*** Update File: path/to/existing.py
@@
 old
-before
+after
*** Move to: path/to/moved.py
*** Delete File: path/to/old.py
*** End Patch
```

All operations are parsed and matched before writes. Context must be unique.
Files are prepared in their destination directories, fsynced, baseline-checked,
and installed with atomic replacement. Multi-file failure restores prior files.
Mode bits, BOM, and newline style are preserved; moves inherit source mode.
Lines are split on `\n` only, so a line containing another Unicode line
boundary (`\x0c`, `\u2028`, `\x85`, …) is one line to both the file and the
patch. A file's final newline is an ordinary line the hunk can add or remove.

## Model-ready examples

One service can operate multiple repositories below its workspace without a
mutable current-project setting. Select a Git worktree explicitly with `repo_path`
or infer it from unambiguous `path` / `paths`. All input file paths remain
workspace-relative, for example `{"repo_path":"project-a","paths":["project-a/src/app.py"]}`.
Git results identify `repo_root` and their output `path_base`; do not feed a
repo-relative filename back as a workspace-relative input without its prefix.
See the [multi-project specification](multi-project-agent-spec.md) for scope,
concurrency, command ownership, and visual-verification acceptance.

Every relative path resolves against the workspace root; there is no
session-scoped working directory. Use explicit paths for multi-call workflows:

```json
{"cmd":"pytest -q","workdir":".","yield_time_ms":30000}
```

If the result is still running, copy its `command_id` exactly:

```json
{"command_id":"abc","chars":"","yield_time_ms":10000}
```

Terminate that command when needed:

```json
{"command_id":"abc","signal":"KILL"}
```

Page a truncated stream using the returned reference:

```json
{"output_ref":"command:abc:stdout","offset":0,"limit":4096}
```

`exec_command.workdir` and each file/Git tool's `path` argument are how a call
targets a subdirectory; both are still confined to the workspace.

## Command and output behavior

`exec_command` and `write_stdin` default `yield_time_ms` to `10000`. Short
commands ordinarily return `status: "exited"` in one call. A still-running
command returns a `command_id` and a machine-readable `next_action` for
`write_stdin` with empty `chars`.

Only truncated terminal output returns a `read_output` next action by default.
`output_ref` values are `command:<id>:stdout` or `command:<id>:stderr`; offsets
are stream-specific absolute byte positions. Runtime limits bound active
commands, retained completed commands, per-command output, total output, and
retention time.

Each stream retains the earliest output (a frozen head segment, one eighth of
the per-stream budget) plus the most recent output (a rolling tail). When a
command produces more output than the budget, bytes between the head and the
tail are evicted permanently; `read_output` reports the loss via
`evicted_gap_bytes` and `omitted_bytes`. For output expected to exceed the
budget, redirect it to a file (`cmd > out.log 2>&1`) and page it with
`read_file` or `search_text` instead of relying on retained output.

Use `tty: true` only when a program requires a terminal. POSIX receives a real
PTY (`isatty()` is true). This build returns `TTY_UNSUPPORTED` on Windows rather
than labeling pipes as a TTY.

## Permission modes

- `safe`: blocks network-looking commands, shell expansion, inline scripts,
  destructive commands, outside-workspace arguments, and secret/loader env.
- `trusted`: enables normal local-development network, expansion, and inline
  snippets while retaining secret and destructive-command checks.
- `dangerous`: disables command permission gates and Landlock; use only inside
  an isolated container or VM.
- `host`: disables command gates and Landlock and preserves the server process's
  full host environment, including HOME, SSH agent, Git credentials, and cache
  locations. Use only with a trusted client and repository.

These modes do not change the tool list. Direct path tools retain workspace
confinement in every mode.

`--dangerously-fake-readonly-annotations` advertises every tool as read-only in
`tools/list` for clients that gate on annotations. It does not change the tool list
either, and it does not stop mutation or execution. `server_info` and the server
card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
