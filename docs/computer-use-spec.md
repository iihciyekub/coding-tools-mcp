# Computer use v1 implementation specification

Status: implemented as an opt-in preview for Desktop 0.3.34. Local MCP and Developer ID-signed helper tests passed; actual ChatGPT and fresh-install system-permission acceptance remain pending. This specification supersedes implementation choices in [the initial plan](computer-use-plan.zh-CN.md).

## Goal and scope

ChatGPT calls tools provided by the user's local Coding Tools MCP runtime through the existing authenticated connection. The desktop application installs and supervises the native execution component. No Codex dependency and no additional model API integration are required.

The first vertical slice targets already-running macOS 12.3+ applications: enumerate an app, request access, start a bounded session, inspect a window, press or set an accessible element, wait for a visible state, and stop. Images are window captures, not crops of the shared desktop. There is no global mouse/keyboard fallback. Unsupported controls report a structured error. Windows/Linux retain the existing coding tools and report unsupported computer control.

## Ownership and alignment

- `coding_tools_mcp/computer_contract.py` is the authoritative MCP tool metadata and input schema source. The live registry, discovery, schemas, renderers, and alignment tests consume it.
- `coding_tools_mcp/computer.py` implements policy, approval/session lifecycle, output limits, action receipts, and a versioned native helper client.
- `apps/desktop-client/src-tauri/computer-helper.swift` owns platform APIs. Its JSON-lines protocol has an explicit version and allowlisted operations, checked against the core contract.
- Desktop profiles explicitly enable computer tools. Runtime startup passes the bundled helper path. UI and LLM share workflow approval/session state; approving access remains a human-only desktop action.
- Every supported native operation has an MCP path. Desktop-only authority is limited to feature enablement, permission settings, approval, and emergency revocation. No MCP tool can grant itself those authorities.

The initial implementation reuses the existing workflow SQLite store rather than introducing a second authorization service. A per-app OS file lock coordinates control between runtime processes. Sessions bind a runtime instance and app process identity; shared state is partitioned by workspace. This retains the existing workspace trust boundary, not per-client isolation. Host shell authority remains outside the computer tool policy boundary.

## MCP surface

| Tool | Native operation / state | Result |
| --- | --- | --- |
| `computer_status` | `status` | Helper/protocol/platform permissions and capabilities |
| `computer_request_access` | `resolve_app`, workflow approval | Exact app instance, observe/control scope, session duration, pending decision |
| `computer_session_start` | Consume exact approval, acquire app lock | Bounded session handle |
| `computer_session_get` | Session/approval/receipt state | Resume context, decision or action status |
| `computer_session_stop` | Revoke session and release lock | Idempotent stop |
| `app_list` | `list_apps` | Bounded app identity list, no window contents |
| `app_windows` | `windows` | Window references for the authorized app instance |
| `app_snapshot` | `snapshot` | Elements, optional image, snapshot and coordinate metadata |
| `app_action` | `action` | `press` or `set_value`, supported by the observed element |
| `app_wait` | Repeated bounded observation | Element condition satisfied, timed out or interrupted |

These tools are directly exposed when enabled, including in deferred-workflow mode. Existing workflow discovery remains unchanged. `app_snapshot` returns image bytes once in MCP image content, alongside readable text and structured metadata. No image/base64 is logged or duplicated in structured content. Computer tool annotations are truthful even when the legacy coding-tool compatibility override is enabled.

## Invariants

1. Enabling tools does not grant system permissions or app access. Full Access does not bypass computer approvals.
2. Approval binds canonical app/process identity, observe/control scope, and requested session TTL. First version approvals authorize one session; persistent app allowlists are deferred.
3. Sessions expire and cannot survive a runtime/helper restart as usable handles. Desktop revocation is checked before each native operation and during waits.
4. Native handles belong to the helper process. Actions validate app identity, window, snapshot age, element identity and current state; stale or ambiguous references fail instead of selecting another element.
5. Each action requires an `operation_id`. Replays return receipts. Conflicting arguments fail. An interrupted action with uncertain outcome is not automatically repeated.
6. Read-only sessions cannot act. One app cannot be controlled by two live sessions across workspaces. User interaction can invalidate observations; a successful system call is not task completion evidence.
7. Native calls, screenshots, trees, sessions and waits have explicit bounds. Stop can interrupt a pending wait; already-submitted native actions cannot be undone by stopping.
8. Screenshots and application text are task data. They cannot grant permission. Secure text controls do not expose values through the accessibility tree.
9. The Webview receives only narrow settings/approval/stop commands, not arbitrary helper, shell or script execution. Control sessions for Coding Tools MCP Desktop, its helper and macOS System Settings are rejected, preventing this tool path from clicking its own approval controls. Explicitly approved observation remains possible; host-shell authority is still outside this policy boundary.

## Desktop behavior

The Application control page contains an opt-in application-control switch. A running runtime must be restarted after catalog changes. The application-control page displays pending requests with exact target/scope/duration, approved session state, permissions setup guidance, and stop controls. LLM access requests appear without requiring users to know internal task IDs. Stopping the workspace terminates its helper and invalidates sessions.

## Implementation checklist

- [x] Contract and spec aligned with runtime registry, discovery, input schemas and readable results.
- [x] Session/approval/action policy implemented with a fake backend regression suite.
- [x] Native helper compiled; status, app identities, windows, snapshots and element actions implemented.
- [x] Desktop switch, helper packaging, approvals and session stop connected to the same core state.
- [x] HTTP MCP image and action loop tested; disabled catalog and ordinary coding tools preserved.
- [x] Python checks and desktop TypeScript/Rust checks passed.
- [x] Native GUI smoke on a disposable fixture (existing development permissions).
- [ ] Native permission-denied / grant flow under the signed installed app.
- [ ] Actual ChatGPT conversation verifies image consumption and a multi-step task.
- [x] Developer ID-signed helper passes the native observation/action/stop fixture.
- [ ] Fresh installed-app permission attribution and denial/grant UX accepted.

Automated and native local evidence does not substitute for the pending user-session acceptance items. Application control remains disabled by default and is released as a preview. Packaging, signing and distribution checks follow [the desktop release contract](desktop-release-maintenance.md). The implementation must report unperformed acceptance explicitly, without treating missing user-controlled ChatGPT/OS access as a reason to leave independently testable code unfinished.


## Bounds and local evidence (2026-09-21)

- At most 8 active sessions per runtime; session TTL 30–1800 seconds; approvals expire after 300 seconds.
- At most 1,000 distinct action receipts per session. Existing receipts remain readable/replayable; reaching the limit never evicts receipts to permit another click.
- Helper requests are at most 128 KiB; replies at most 8 MiB; each call has a 15-second reply deadline. A failed helper is terminated and its old sessions cannot authorize a restarted helper.
- Snapshots retain at most 8 handle sets, at most 500 elements, depth 20, approximately 256 KiB element text, and 5 MiB PNG data. Action snapshots expire after 60 seconds and are invalidated on submission.
- Waits are limited to 10 seconds. Session validity is rechecked after acquiring the native transport slot, so a queued action is rejected after desktop revocation. Already submitted native calls may finish.
- Computer tools skip workspace hooks. Trace arguments contain only references and enumerated action/session metadata, excluding typed values, search queries and reasons. Images appear only in MCP content, not structured content or logs.

Final local results: **316 Python tests run, 4 skipped; 46 Rust tests passed, 3 ignored; 6 frontend tests passed**. Ruff, mypy, TypeScript, Rust formatting and clippy passed. The debug app bundle contains byte-identical current runtime/schema sources. Its packaged helper passed the real GUI smoke, including an assertion that the fixture was still in the background when its button action ran. After explicit inside-out ad-hoc signing with Hardened Runtime, the test app passed `codesign --verify --deep --strict`, and that helper passed the smoke again. Ad-hoc verification is packaging evidence only, not Developer ID/notarization or installed-app TCC acceptance.

Desktop 0.3.34 release preparation also verified the native smoke with the Developer ID-signed helper. The desktop keeps live sessions visible ahead of expired history, preserving access to Stop now. The release workflow was parsed and checked to sign the bundled helper before the containing app with the existing Developer ID identity. Platform selection was exercised for macOS-only, two-platform, legacy and artifact-only modes, with invalid configurations rejected. Production installations are not replaced during local validation. Run Rust checks and Tauri builds sequentially: their differing feature sets share the target directory and concurrent builds can invalidate rustdoc dependency artifacts.

Commands, from the repository root unless stated otherwise:

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m ruff check --ignore E501 coding_tools_mcp tests apps/desktop-client/tests/computer_smoke.py
.venv/bin/python -m mypy --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def coding_tools_mcp
.venv/bin/python apps/desktop-client/tests/computer_smoke.py
# apps/desktop-client:
npm run check
npm run build:bundle
npm run tauri build -- --debug --bundles app
# apps/desktop-client/src-tauri:
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

The native smoke creates its own disposable application and temporary workflow state. It simulates operator approval only for that fixture PID; it never approves or manipulates another running application. It verifies window capture, accessibility text assignment, action receipt replay, button press, observed result and revocation. It cleans up its process/state. Passing a helper executable path selects the helper inside a packaged test app for the same smoke.

Automated HTTP evidence uses the production transport and a fake native backend; native evidence uses the compiled helper and the real fixture. Neither is a real ChatGPT acceptance test. Broad regression also updates two pre-existing compliance assertions to reflect the current explicit file-scope instructions instead of the retired workspace-only wording.

To finish ChatGPT acceptance, connect the built desktop service, refresh its catalog, verify all ten tools are visible, request the fixture app, approve locally, ask the model to describe its screenshot, change its field, press its button, verify the changed label and stop. Record the client/model, permission prompts, tool calls and outcome. Signed-install acceptance must additionally cover first denial, grant, restart, upgrade and helper identity before publication.
