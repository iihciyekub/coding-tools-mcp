# Tools And Schemas

The normative behavior is [runtime-contract-v0.3.md](runtime-contract-v0.3.md).
Live JSON Schemas come from `tools/list`; CI compares their names, input
properties, annotations, and error codes with the contract.

## Fixed inventory

The default catalog contains exactly 49 tools:

- `server_info`: server, workspace, automatic project context, policy, runtime,
  auth, protocol, and fixed-catalog metadata.
- `check_exec_environment`: lightweight execution policy and Landlock status.
- `read_file`: stream a bounded UTF-8 range without loading the whole file.
- `list_dir`: list immediate or bounded-recursive directory entries.
- `list_files`: iterate files with glob, ignore, hidden-file, sort, and cap
  controls.
- `search_text`: literal or regex search; ripgrep stops after the result cap.
- `apply_patch`: stage and atomically commit add/update/delete/move envelopes.
- `exec_command`: run a bounded command and wait up to 10 seconds by default.
- `write_stdin`: poll or interact with a running command.
- `kill_command`: terminate one runtime-owned command.
- `read_output`: page retained stdout or stderr using absolute byte offsets.
- `git_status`: structured working-tree status.
- `git_diff`: bounded unified staged/unstaged diff.
- `git_log`: structured bounded commit history.
- `git_show`: bounded revision metadata/content/diff.
- `git_blame`: structured bounded line attribution.
- `request_permissions`: report elicitation status without silently granting.
- `view_image`: one MCP image content block plus structured metadata.
- `browser_status`: connect to a loopback Chrome CDP endpoint through Playwright.
- `browser_tabs`: list inspectable Chrome tabs.
- `browser_active_tab`: return the currently visible Chrome tab when detectable.
- `browser_snapshot`: return bounded visible text and simplified interactive DOM elements.
- `browser_screenshot`: capture the selected tab as one MCP PNG image content block.
- `browser_evaluate`: evaluate JavaScript in the selected tab.
- `browser_click`: click an element using a Playwright selector.
- `browser_type`: fill or type into an element using a Playwright selector.
- `browser_console`: capture bounded console messages and page errors.
- `browser_network`: inspect current resource timing and bounded network events.
- `browser_inspect`: inspect one element's geometry, style, HTML, parents, and animations.
- `code_symbols`: list bounded language-aware symbol definitions.
- `code_definition`: find definitions for one symbol.
- `code_references`: find exact identifier references for one symbol.
- `chrome_extension_install`: install the local Native Messaging manifest and unpacked bridge extension files.
- `chrome_extension_status`: report bridge install/connectivity state.
- `chrome_extensions`: list installed Chrome extensions through the bridge.
- `chrome_extension_tabs`: list Chrome tabs through extension APIs.
- `chrome_extension_execute`: evaluate JavaScript through Chrome's debugger API.
- `chrome_extension_send`: send an external message to an extension that permits it.
- `app_accessibility`: report Accessibility trust and optionally open System Settings.
- `app_list`: list running macOS applications.
- `app_launch`: launch an application by name or bundle identifier.
- `app_activate`: bring an application to the foreground.
- `app_windows`: list Accessibility window metadata.
- `app_snapshot`: return a bounded Accessibility UI hierarchy.
- `app_click`: press or click a matched Accessibility element.
- `app_type`: set/type text using Accessibility and keyboard events.
- `app_press`: send a key plus modifiers.
- `app_menu`: choose a hierarchical app menu item.
- `app_screenshot`: capture an app window as one MCP PNG image block.

`view_image` may be disabled when an installation cannot accept binary image
content. That capability gate is not a tool profile. The other 48 tools are
always advertised, and `listChanged` is `false`.

## Chrome extension bridge

`chrome_extension_install` writes a per-user Chrome Native Messaging host
manifest and copies the bundled Manifest V3 bridge extension to the user's
application-support directory. Google Chrome requires the user to enable
Developer mode and choose **Load unpacked** once; official Chrome builds no
longer accept command-line unpacked-extension loading. By default the install
tool opens `chrome://extensions` after staging the files. The bridge has a stable
extension id and uses a local user-only Unix socket between the Native Messaging
host and MCP runtimes. macOS release builds bundle one self-contained PyInstaller
`onedir` MCP runtime plus a tiny Native Messaging launcher that `exec`s that
same signed runtime in `--chrome-native-host` mode. This avoids a second Python
bundle and avoids temporary extracted libraries under Hardened Runtime.

`chrome_extension_send` can message another extension only when that target
extension explicitly permits external messaging. Chrome's extension isolation
is not bypassed.

## macOS application control

The `app_*` tools use macOS Accessibility/CGEvent APIs. UI inspection and input
require Accessibility permission for Coding Tools MCP; `app_screenshot` may
also require Screen Recording permission. Unsupported platforms return a
structured `UNSUPPORTED_PLATFORM` failure instead of emulating GUI control.

## Local Chrome connection

The browser tools use Python Playwright only and attach to an already-running
Chromium CDP endpoint. The default is `http://127.0.0.1:9222`; set
`CODING_TOOLS_MCP_BROWSER_CDP_URL` or pass `endpoint` to override it. Remote
hosts are rejected: the endpoint must be loopback (`127.0.0.1`, `localhost`, or
`::1`).

On macOS, a simple development launch is:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/coding-tools-mcp-chrome
```

Recent Chrome releases may ignore remote debugging against the normal default
profile, so a non-default `--user-data-dir` is the reliable configuration. The
MCP server never launches a browser and never calls `browser.close()` on an
attached Chrome instance; each tool call opens a short-lived Playwright CDP
client connection and drops only that connection when the call finishes.

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
interface and retains existing fields where possible. Model-facing text is
bounded at 16 KiB; if it is shortened, the full structured value is still
present. Errors use the same envelope with readable recovery guidance and
`isError: true`.

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
