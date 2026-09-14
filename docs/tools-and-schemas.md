# Tools And Schemas

The normative behavior is [runtime-contract-v0.3.md](runtime-contract-v0.3.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

For the category directory, progressive parameter loading, and tool selection
strategy, see [Progressive tool discovery](tool-discovery.md). Desktop-launched
runtimes now select deferred workflow exposure; the CLI defaults are unchanged.

## Fixed inventory

The implementation declares exactly 69 tools. With the default `view_image`
capability enabled, the default catalog exposes 28 tools:

- `server_info`: server, workspace, automatic project context, policy, runtime,
  auth, protocol, and fixed-catalog metadata.
- `check_exec_environment`: lightweight execution policy and Landlock status.
- `runtime_doctor`: non-destructive runtime health report with actionable warnings for common toolchain commands, workspace access, shell snapshot, hooks, LSP, sandbox state, and network policy.
- `hooks_status`: report whether opt-in workspace hooks are enabled and summarize loaded rules.
- `shell_snapshot`: capture or refresh the stable command environment reused by later `exec_command` calls.
- `read_file`: stream a bounded UTF-8 range without loading the whole file.
- `read_files`: batch several bounded UTF-8 file reads under one total byte budget.
- `list_dir`: list immediate or bounded-recursive directory entries.
- `list_files`: iterate files with glob, ignore, hidden-file, sort, and cap
  controls.
- `search_text`: literal or regex search; ripgrep stops after the result cap.
- `tool_search`: browse enabled categories and tool summaries, or search English/Chinese intent and exact names; deferred search matches include invocation metadata and schemas in text and structured results.
- `apply_patch`: stage and atomically commit add/update/delete/move envelopes.
- `exec_command`: run a bounded command and optionally deduplicate retries with `operation_id`.
- `get_command`: read one command's status by `command_id` or `operation_id` without consuming output.
- `list_commands`: list recent active/retained commands for reconnect recovery.
- `write_stdin`: poll or interact with a running command.
- `kill_command`: terminate one runtime-owned command.
- `read_output`: page retained stdout or stderr using absolute byte offsets.
- `git_status`: structured working-tree status.
- `git_diff`: bounded unified staged/unstaged diff.
- `git_log`: structured bounded commit history.
- `git_show`: bounded revision metadata/content/diff.
- `git_blame`: structured bounded line attribution.
- `request_permissions`: request an exact, expiring one-shot desktop approval when workflow tools are enabled.
- `view_image`: one MCP image content block plus structured metadata.
- `code_symbols`: list bounded language-aware symbol definitions.
- `code_definition`: find definitions for one symbol.
- `code_references`: find exact identifier references for one symbol.

When deferred workflow exposure is selected, one additional registered gateway
is directly exposed:

- `tool_invoke`: validate and dispatch one workflow tool returned by `tool_search` while keeping that workflow tool out of `tools/list`.

Starting the server with `--enable-workflow-tools` adds these 40 tools. The
selection is fixed for that runtime; it does not change during a connection:

- `workspace_overview`: summarize manifests, languages, entry points, top-level areas, and instruction files.
- `repo_map`: return a bounded file/symbol map with query ranking and coverage metadata.
- `project_instructions`: return root and nested instruction files applicable to one path.
- `skills_list`: list metadata for workspace `.agents/skills/**/SKILL.md` entries.
- `skills_read`: read one selected workspace Skill without executing scripts.
- `checks_discover`: discover project-defined verification commands without executing them.
- `checks_run`: execute one currently discovered check through the existing command policy.
- `checks_result`: read persisted check evidence and detect code changes after the check.
- `task_create`: create a persistent workspace task record.
- `task_get`: read one persistent task record.
- `task_list`: list persistent task records, optionally by status.
- `task_update`: update a task with optimistic revision checking.
- `task_event_add`: append a progress, decision, evidence, or note event to a task.
- `task_events`: list the recent event history for a task.
- `task_context`: restore a task summary with its recent events, checks, and checkpoints.
- `task_plan_get`: read the ordered plan and current task revision.
- `task_plan_update`: atomically replace plan steps with task revision checking.
- `git_branch_list`: list local branches and the current HEAD/index fingerprints.
- `git_branch_create`: create a validated branch against reviewed Git state.
- `git_conflicts`: list unmerged paths and their index stages.
- `git_stage`: stage only explicit paths after HEAD/index concurrency checks.
- `git_unstage`: unstage only explicit paths after HEAD/index concurrency checks.
- `git_commit`: commit exactly the declared staged path set.
- `git_worktree_list`: list repository worktrees and identify runtime-managed entries.
- `git_worktree_create`: create an isolated managed worktree after Git state checks.
- `git_worktree_remove`: remove a clean managed worktree while preserving its branch.
- `lsp_status`: report optional Python, TypeScript/JavaScript, and Rust language-server availability and process state.
- `lsp_definition`: resolve semantic definitions at a source position.
- `lsp_references`: resolve semantic references at a source position.
- `lsp_diagnostics`: return bounded diagnostics published for one source file.
- `lsp_rename_preview`: return workspace-confined rename edits and source hashes without writing files.
- `review_prepare`: persist a bounded diff, applicable rules, task evidence, and code fingerprint.
- `review_record`: record structured findings with optimistic revision checking.
- `review_get`: read a review and report whether its code snapshot is stale.
- `approval_get`: read one persistent approval request and its current state.
- `approval_list`: list bounded approval requests, optionally filtered by state.
- `checkpoint_create`: snapshot an explicit bounded set of UTF-8 files outside Git state.
- `checkpoint_list`: list persistent workspace checkpoints.
- `checkpoint_diff`: compare a checkpoint to current files and issue a state-bound restore token.
- `checkpoint_restore`: atomically restore checkpoint files when the preview token is still current.

`view_image` may be disabled when an installation cannot accept binary image
content. The workflow toolset is opt-in. With normal workflow exposure the
direct catalog contains 68 tools. Adding `--defer-workflow-tools` (selected by
the desktop launcher) keeps those 40
workflow capabilities available to `tool_search` but removes them from the
direct `tools/list`; `tool_invoke` is exposed instead, producing a 29-tool
direct catalog and 40 deferred tools. A deferred intent-search result always includes
its input schema and `invoke_via: "tool_invoke"`. This is a static gateway
selected at startup, not a dynamic tool-list mutation, so `listChanged` remains
`false`.

Network command gating has three startup-selected modes: `deny`, `allowlist`,
and `unrestricted`. `--network-allow-domain` may be repeated and
`CODING_TOOLS_MCP_NETWORK_ALLOW_DOMAINS` accepts a comma-separated list.
Allowlist rules are exact domains or `*.example.com` subdomain patterns.
This is command-policy enforcement: statically detected targets outside the
allowlist, plus network-intent commands whose target cannot be resolved from
the command line, require explicit permission. It is deliberately not described
as an OS-level egress firewall; that belongs to a future sandbox layer.

Workspace hooks are separately opt-in with `--enable-hooks`. The default config
path is `.agents/hooks.json` (override with `--hooks-file` or
`CODING_TOOLS_MCP_HOOKS_FILE`). Supported events are `before_tool`,
`after_tool`, and `tool_error`. Hooks execute under the same command policy and
filesystem sandbox as `exec_command`; a failing blocking `before_tool` hook
rejects the target call. Hook arguments/results are redacted before being sent
to hook stdin, and hook stdout/stderr is bounded.

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
