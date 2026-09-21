# Local Web verification without another MCP toolset

Status: implemented and locally tested; real Chromium acceptance is recorded in
the multi-project specification, section 12. This is an optional command-line
acceptance runner, **not** an interactive Chrome MCP integration.

The default MCP inventory does not change. Agents use the existing
`exec_command` to run a small scenario, `read_file` for the structured evidence
or accessibility snapshot, and `view_image` for the resulting screenshot.
The runtime's existing command policy still applies.

## Installation

Install into the **same project Python environment** that runs the command:

```bash
python -m pip install 'coding-tools-mcp[browser]'
python -m playwright install chromium
```

For a source checkout, replace the first line with
`python -m pip install -e '.[browser]'`. Neither importing the module nor calling
`--help` installs packages, downloads a browser, opens a profile or requests OS
screen-recording permissions. Browser support is not a core runtime dependency.

## Scenario

Start the project's development server with its normal command and retain its
command_id. Supply the actual HTTP loopback URL; do not guess which project's
server owns a port. Create a JSON scenario using apply_patch:

```json
{
  "steps": [
    {"action": "fill", "target": {"label": "Name"}, "value": "Example"},
    {"action": "click", "target": {"role": "button", "name": "Save"}},
    {"action": "wait_text", "text": "Saved", "timeout_ms": 5000}
  ]
}
```

Run from the intended project directory using an explicit interpreter:

```bash
/path/to/environment/bin/python -m coding_tools_mcp.browser_check \
  --url http://127.0.0.1:3000 \
  --scenario tests/ui/save.json \
  --output /tmp/ctm-browser-evidence \
  --timeout-ms 30000
```

Without `--scenario`, the runner navigates and records observations only. That
can verify that a page loaded without observed runtime errors, but is **not** a
functional test of its controls. An agent must not call this proof of a complete
application workflow.

Targets use exactly one of `{"role":"button","name":"Save"}`,
`{"label":"Name"}`, or `{"test_id":"save-button"}`. Role/name and label
matching are exact, and Playwright strict locator behavior rejects ambiguous
matches. Supported actions are click, fill, press, wait_visible and wait_text.
press allows Enter, Tab, Escape, Space and the four arrow keys, not global
desktop shortcuts. wait_text requires exact visible text. There are no raw
selectors, arbitrary page scripts, file uploads or coordinate inputs.

## Evidence and success semantics

Each invocation creates a unique `browser-check-*` folder under the requested
output directory, never reusing an old report as evidence for a new run. Standard
output contains JSON with the requested URL (sanitized), actual command working
directory, step receipts, errors, cleanup state and the evidence paths.

The folder contains `report.json`, an available `snapshot.txt` accessibility
tree and an available `screenshot.png`. Capture is a 1280 x 900 viewport, not a
recording of the desktop and not an unbounded full-page capture. Failure still
retains available observations. Raw Playwright exception call logs are omitted
because they can contain fill values; use the failing step index and runtime
events to investigate.

`ok=true` means the requested steps completed, cleanup completed, and no captured
console errors, page errors, failed requests, HTTP 4xx/5xx or blocked requests
were observed during this finite run. It says nothing about behavior after the
run or functionality that the scenario did not assert. Explicit waits are
essential; clicking alone does not prove the asynchronous operation completed.

Exit codes: 0 for a successful finite check; 1 for setup/verification failure;
2 for invalid input or local file errors. Missing optional dependencies return
`BROWSER_DEPENDENCY_MISSING` with installation guidance and do not install them.

Every event category retains at most 100 entries, with omitted counts. Individual
event strings are bounded. Structure text is limited to 32 KiB and PNG data to
5 MiB. Scenarios are limited to 128 KiB and 32 actions, per-step timeouts to 10
seconds, and execution to a configurable 1-120 seconds. Observation and cleanup
have separate short timeouts; an exec timeout should leave room for those.

## Isolation and limitations

Only an explicit `http://localhost`, `http://127.0.0.1` or `http://[::1]` target is
accepted. Browser requests are routed against that exact origin, including its
port. Another project on another port is a different origin. WebSocket routing
uses the corresponding ws origin. Service workers are blocked. This deliberately
does not support an external CDN or a separate API origin in v1: serve them
through the local development proxy rather than silently widening access.

HTTP redirects are refused in v1, including same-origin redirects. Use the final
canonical development URL directly. The runner inspects responses with
`max_redirects=0` before fulfilling them: merely checking the initial routed URL
does not constrain a browser's native redirect chain. A refused redirect is
reported as `redirect_not_supported`, not a successful page observation.

This routing is a browser-level guard, **not an OS network sandbox**, and the
local development server itself may make other requests. Only use trusted local
development applications and disposable data. An allowed localhost page can
still mutate its own backend through the declared steps.

Each invocation launches a disposable headless Chromium with a nonpersistent
context. It does not connect to daily Chrome, reuse cookies, accept downloads,
request camera/microphone permissions or capture other applications. There is
no implicit “active tab” shared across projects. Context, browser and driver
cleanup are attempted even on a deadline or failed assertion.

The disposable context grants Chromium's local-network-access permission only
to the explicitly selected test origin so same-origin development WebSockets
can connect on current Chromium. HTTP/WebSocket routing still rejects other
ports/hosts. This is not an operating-system grant and does not change daily
Chrome settings or disable global browser security checks. The optional package
requires Playwright 1.58 or newer; bundled Chromium should match that installation.

Request headers, Cookie values and response bodies are not collected. URL
credentials, query values and fragments are removed from reports; common
credential-shaped event strings are redacted. **This is not comprehensive DLP**:
page text, screenshots and application-generated error messages can still contain
the test data that the application displays. Do not use production secrets or
personal accounts, and inspect evidence before sharing it.

Chromium does not validate native Tauri/WKWebView integration, system permissions,
menu-bar behavior, global shortcuts, drawing gestures or real ChatGPT image
consumption. Those remain separate App acceptance tests. No existing computer
approval controls are bypassed by this runner.

## Tests

```bash
# Validation tests; real browser cases skip unless explicitly enabled.
python -m unittest tests.test_browser_check -v

# Requires the optional package and its Chromium installation.
CODING_TOOLS_MCP_BROWSER_SMOKE=1 python -m unittest tests.test_browser_check -v
```

The real fixture uses two disposable local servers. It covers a save workflow,
independent evidence folders, HTTP 500 failure, another project's port, redirects,
same-origin WebSockets, blocked cross-project WebSockets and the total execution deadline. It never navigates a user's daily browser or
an existing application. Test results belong in
[the multi-project spec](multi-project-agent-spec.md), not in claims inferred
from the presence of this document.

Implementation references: [Playwright Python library](https://playwright.dev/python/docs/library),
[nonpersistent contexts and routing](https://playwright.dev/python/docs/api/class-browsercontext),
and [locator accessibility snapshots](https://playwright.dev/python/docs/api/class-locator#locator-aria-snapshot).
