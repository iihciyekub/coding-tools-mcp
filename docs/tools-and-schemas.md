# Tools And Schemas

The normative behavior is [runtime-contract-v0.4.md](runtime-contract-v0.4.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

The runtime is macOS-first and intentionally exposes a small direct set of coding
primitives rather than an agent workflow engine or a second tool-discovery layer.

## Fixed inventory

The implementation declares exactly 32 tools as contracts, but the normal macOS
project runtime intentionally exposes a smaller model-facing core. Diagnostic
and Git-history helpers can remain implemented without occupying `tools/list`.
Gateway mode adds `project_context` and, only when explicit local capability
roots are configured, three read-only catalog tools. There is no workflow-tool mode.

- `project_context`: **gateway-only direct** — List projects registered with a persistent gateway, inspect the project bound to this MCP session, or bind this session using an exact project id, display name, directory name, or registered root path.
- `local_capabilities_search`: **optional gateway-only read-only** — Search Skill and plugin metadata in explicitly authorized directories.
- `local_skill_read`: **optional gateway-only read-only** — Read a selected `SKILL.md` and bounded text references; it does not execute the Skill.
- `local_plugin_inspect`: **optional gateway-only read-only** — Inspect public plugin metadata; it does not activate plugin actions or hooks.
- `server_info`: **direct** — Return server, project-root, auth, policy, and runtime metadata when configuration or environment details matter. Desktop-managed runtimes also expose `runtime_build_id`, allowing the Gateway and operators to verify that schema and execution come from the same bundled runtime build.
- `runtime_doctor`: **implemented diagnostic helper; hidden from the default model surface** — Run a non-destructive runtime health check covering common toolchain commands, workspace access, shell snapshot, LSP availability, sandbox status, network policy, and macOS Apple toolchain metadata including Xcode, Swift, SourceKit-LSP, codesign, notarytool, xcresulttool, and Homebrew.
- `read_file`: **direct** — Read a UTF-8 text file slice inside the configured file scope. Relative paths are Project-relative; host mode also accepts host absolute and ~/... paths.
- `read_files`: **direct** — Read bounded UTF-8 slices from multiple files in the configured file scope.
- `list_dir`: **direct** — List directory entries inside the configured file scope.
- `list_files`: **direct** — List files using `include_globs` / `exclude_globs`. An omitted `path` uses the configured default search folder; an explicit relative path uses the bound Project root.
- `search_text`: **direct** — Search UTF-8 files for text or regex matches. An omitted `path` uses the configured default search folder; an explicit relative path uses the bound Project root.
- `apply_patch`: **direct** — Stage, validate, and atomically apply a patch envelope. Example: *** Begin Patch *** Update File: app.py @@ -old +new *** End Patch
- `exec_command`: **direct** — Run a bounded command under runtime policy. Pass workdir explicitly for reconnect-safe paths. A still-running command returns command_id. Example: {"cmd":"pytest -q","workdir":".","yield_time_ms":30000}. Retained output is bounded per stream; for very large output redirect to a file (cmd > out.log 2>&1) and page it with read_file or search_text.
- `get_command`: **direct** — Read or wait for command status without consuming output cursors. Resolve by command_id or operation_id and use `wait_ms` to wait for completion; returned output_refs can be paged with read_output.
- `list_commands`: **direct** — List recent server-managed commands and operation_ids for reconnect/recovery. This is read-only and does not consume command output.
- `write_stdin`: **direct** — Send non-empty input to an interactive running command. Waiting belongs to `get_command`. Example: {"command_id":"abc","chars":"yes\n"}.
- `kill_command`: **direct** — Terminate a server-managed command by command_id. Example: {"command_id":"abc","signal":"KILL"}.
- `read_output`: **direct** — Read retained command output using an output_ref returned by exec_command/write_stdin. Each stream retains the earliest output (head) plus the most recent output (rolling tail); bytes between them may be evicted and are reported via evicted_gap_bytes. Example: {"output_ref":"command:abc:stdout","offset":0,"limit":4096}.
- `git_status`: **direct** — Return git working tree status for the workspace.
- `git_diff`: **direct** — Return unified git diff for workspace changes.
- `git_log`: **implemented helper; hidden from the default model surface** — Native `git log` through `exec_command` is the default agent path.
- `git_show`: **implemented helper; hidden from the default model surface** — Native `git show` through `exec_command` is the default agent path.
- `git_blame`: **implemented helper; hidden from the default model surface** — Native `git blame` through `exec_command` is the default agent path.
- `code_diagnostics`: **direct** — Open or refresh a source file and return bounded published language-server diagnostics.
- `request_permissions`: **direct** — Create an exact, expiring operator approval request without silently granting operations.
- `project_overview`: **direct** — Summarize the bound Project's manifests, languages, entry points, top-level areas, applicable instructions, and recommended checks. Detected Apple projects also include bounded read-only Xcode, Swift, SDK, and SourceKit-LSP metadata.
- `project_instructions`: **direct** — Resolve root and nested project instruction files that apply to one Project-relative path.
- `checks_discover`: **implemented helper; hidden from the default model surface** — Check discovery and recommendations are included in `project_overview` so project orientation does not require another tool call.
- `view_image`: **direct when image content is enabled** — Return an image from the bound Project as MCP image content.
- `code_symbols`: **implemented helper; hidden from the default model surface** — Definition/reference tools and text search cover the normal agent path; the lightweight symbol scanner remains available internally as a fallback primitive.
- `code_definition`: **direct** — Find language-aware definitions for a symbol under a workspace path. When a file path plus line/column is supplied, line/column are one-based and semantic LSP is preferred; the runtime converts the column to UTF-16 and falls back to the bounded symbol scan if the LSP runtime is unavailable.
- `code_references`: **direct** — Find references for a symbol under a workspace path. When a file path plus line/column is supplied, prefer semantic LSP references using one-based public positions converted internally to UTF-16; fall back to the bounded exact-identifier scan if the LSP runtime is unavailable.

The direct catalog contains file reads/search, `apply_patch`, command lifecycle, Git
evidence, code navigation/diagnostics, project-safe permission requests, project
orientation/instructions, check discovery, and runtime diagnostics. `project_context`
appears only on the persistent Project Gateway; the three local capability tools
appear only when capability roots are configured; `view_image` is present when image
content is enabled. `listChanged` remains `false` because tool definitions are fixed
for the Runtime lifetime; Skill files can change within that fixed catalog.

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

`content` is not a JSON mirror. Large human/model-facing text appears only in
`content`; `structuredContent` carries metadata, identifiers, offsets, status,
and bounded structural records without duplicating file bodies, diffs, command
output, or search previews. Normal model-facing text
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
`@@ <scope>` headers can disambiguate repeated code inside an enclosing
function/class-like scope, and `*** End of File` can anchor a hunk to the file
tail. Matching is exact first, then conservatively ignores trailing whitespace;
indentation is never fuzzed. Successful updates report `match_quality`,
`changed_ranges`, and the resulting SHA-256 `revision`.
Files are prepared in their destination directories, fsynced, baseline-checked,
and installed with atomic replacement. Multi-file failure restores prior files.
Mode bits, BOM, and newline style are preserved; moves inherit source mode.
Lines are split on `\n` only, so a line containing another Unicode line
boundary (`\x0c`, `\u2028`, `\x85`, …) is one line to both the file and the
patch. A file's final newline is an ordinary line the hunk can add or remove.

`read_file` returns a SHA-256 `revision`. A later `read_file` can pass that
value as `if_revision`; if the file is unchanged, the result sets
`unchanged=true` and omits the file content instead of resending it. This is
intended for long agent sessions that revisit already-read files.
`apply_patch.expected_revisions` may bind selected paths to revisions for
optimistic concurrency. An optional `idempotency_key` makes retries replay the
original result instead of applying the same patch twice.

## Model-ready examples

One bound Project can contain multiple Git repositories. Select a Git worktree
explicitly with `repo_path` or infer it from unambiguous `path` / `paths`. All
input file paths remain Project-relative, for example
`{"repo_path":"packages/app","paths":["packages/app/src/app.py"]}`.
Git results identify `repo_root` and their output `path_base`; do not feed a
repo-relative filename back as a Project-relative input without its prefix.
See the [multi-project specification](multi-project-agent-spec.md) for scope,
concurrency, command ownership, and visual-verification acceptance.

Every relative path resolves against the workspace root; there is no
session-scoped working directory. Use explicit paths for multi-call workflows:

```json
{"cmd":"pytest -q","workdir":".","yield_time_ms":30000}
```

`list_files` and `search_text` are the exception when `path` is omitted: they use
the optional `--default-search-path` directory, which defaults to the workspace.
The configured directory must already be inside the runtime's file access scope.
This setting changes the search starting point, not file or command permissions;
an explicit `"path":"."` still targets the workspace.

If the result is still running, copy its `command_id` exactly:

```json
{"command_id":"abc","wait_ms":10000}
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
targets a subdirectory. In safe/trusted/dangerous modes direct file paths remain
inside the workspace plus explicitly configured file-access roots. In `host`
mode ordinary file tools and `apply_patch` may also use host absolute and
home-relative paths; project-scoped Git/LSP/check/context behavior remains
anchored to the bound Project root.

## Command and output behavior

`exec_command` defaults `yield_time_ms` to `10000`. Short commands ordinarily
return `status: "exited"` in one call. A still-running command returns a
`command_id` and a machine-readable `next_action` for `get_command` with
`wait_ms`. `write_stdin` is reserved for non-empty interactive input.

`exec_command.timeout_ms` is the process lifetime limit and defaults to
`300000` (5 minutes). `yield_time_ms` is only how long one call waits before
returning a running command handle; it does not shorten the process lifetime.
Command results expose `operation_outcome` separately from MCP/tool-call
success, so `ok: true` with `exit_code: 1` is represented as
`operation_outcome: "exited_nonzero"`.

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

These modes do not change the tool list. Host mode changes the maximum file
scope as described above; the other permission modes retain workspace/file-root
confinement.

`--dangerously-fake-readonly-annotations` advertises every tool as read-only in
`tools/list` for clients that gate on annotations. It does not change the tool list
either, and it does not stop mutation or execution. `server_info` and the server
card keep reporting the real annotations. See
[permission-modes.md](permission-modes.md).
