from __future__ import annotations

import json
import contextlib
import io
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from coding_tools_mcp.computer import digest
from coding_tools_mcp.computer_contract import COMPUTER_TOOLS, NATIVE_OPERATIONS
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import MCPHandler, Runtime, RuntimeHTTPServer, input_schemas, tool_annotations
from tests.compliance.mcp_client import MCPClient


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="


class FakeComputer:
    def __init__(self) -> None:
        self.app = {"app_id": "pid:123", "pid": 123, "name": "Fixture", "bundle_id": "test.fixture", "instance_id": "123:1", "path": "/Fixture.app"}
        self.helper_id = "helper-1"
        self.actions = 0
        self.value = "before"
        self.fail_action = False
        self.inspect_started = threading.Event()

    def close(self) -> None:
        pass

    def call(self, operation: str, arguments: dict) -> dict:
        result = {"ok": True, "helper_instance_id": self.helper_id}
        if operation == "status":
            return {**result, "operations": sorted(NATIVE_OPERATIONS), "accessibility": True}
        if operation == "resolve_app":
            return {**result, "app": dict(self.app)}
        if operation == "list_apps":
            return {**result, "apps": [dict(self.app)]}
        if arguments["helper_id"] != self.helper_id:
            raise ToolFailure("COMPUTER_HELPER_CHANGED", "Helper changed.")
        if operation == "windows":
            return {**result, "windows": [{"window_id": "w1", "title": "Fixture"}]}
        if operation == "snapshot":
            result.update(window_id="w1", snapshot_id="s1", elements=[{"element_id": "e1", "value": self.value, "actions": ["set_value"]}])
            if arguments.get("include_image", True):
                result.update(_mcp_image_data=PNG, mime_type="image/png", width=1, height=1)
            return result
        if operation == "action":
            self.actions += 1
            if self.fail_action:
                raise ToolFailure("COMPUTER_HELPER_FAILED", "Unknown outcome.")
            self.value = arguments.get("value", "pressed")
            return {**result, "status": "submitted"}
        if operation == "inspect":
            self.inspect_started.set()
            return {**result, "element": {"enabled": True, "value": self.value}}
        raise AssertionError(operation)


class FakeCodexObserver:
    def __init__(self, workflow) -> None:
        self.workflow = workflow
        self.closed = False
        self.sessions: dict[str, dict] = {}
        self.interactions = 0
        self.fail_interact = False
        self.snapshot_id = "codex_snapshot_fixture"

    def close(self) -> None:
        self.closed = True

    def list_apps(self) -> list[dict]:
        return [{"app_id": "com.example.codex", "display_name": "Codex Fixture", "is_running": True,
                 "last_used_date": None, "use_count": 1}]

    def request_access(self, app_id: str, *, access: str = "observe", reason: str, ttl_seconds: int = 600) -> dict:
        scope = {"provider": "codex", "app": {"app_id": app_id, "display_name": "Codex Fixture"},
                 "access": access, "ttl_seconds": ttl_seconds}
        return self.workflow.create_approval(
            tool_name="computer_session_start",
            permission=f"computer_{access}",
            reason=reason,
            arguments_hash=digest(scope),
            displayed_arguments=scope,
            ttl_seconds=300,
        )

    def start_session(self, approval_id: str) -> dict:
        approval = self.workflow.get_approval(approval_id)
        scope = approval["arguments"]
        self.workflow.consume_approvals(
            [approval_id], tool_name="computer_session_start", arguments_hash=digest(scope),
            required_permissions={f"computer_{scope['access']}"},
        )
        session_id = "codex_computer_fixture_" + scope["access"]
        self.sessions[session_id] = {
            "ok": True, "session_id": session_id, "provider": "codex", "access": scope["access"], "status": "active",
            "app": scope["app"], "expires_at": 9999999999.0, "created_at": 1.0,
        }
        return dict(self.sessions[session_id])

    def get_session(self, session_id: str) -> dict:
        if session_id not in self.sessions:
            raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "missing", category="not_found")
        return dict(self.sessions[session_id])

    def list_sessions(self) -> list[dict]:
        return [dict(value) for value in self.sessions.values()]

    def observe(self, session_id: str, **kwargs) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "active":
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "inactive", category="permission")
        result = {"ok": True, "session_id": session_id, "provider": "codex", "app": "Codex Fixture",
                  "text": "AX fixture", "text_truncated": False, "has_screenshot": bool(kwargs.get("include_image", True)),
                  "snapshot_id": self.snapshot_id, "observed_at": 1.0}
        if kwargs.get("include_image", True):
            result.update(_mcp_image_data=PNG, mime_type="image/png")
        return result

    def interact(self, session_id: str, *, snapshot_id: str, action: str, arguments: dict, guard=None) -> dict:
        session = self.get_session(session_id)
        if session["status"] != "active":
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "inactive", category="permission")
        if session["access"] != "control":
            raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "observe only", category="permission")
        if snapshot_id != self.snapshot_id:
            raise ToolFailure("COMPUTER_SNAPSHOT_STALE", "stale", category="conflict")
        if guard is not None:
            guard()
        self.interactions += 1
        if self.fail_interact:
            raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "unknown outcome", category="runtime")
        return {
            "ok": True,
            "session_id": session_id,
            "provider": "codex",
            "action": action,
            "status": "submitted",
            "verified": False,
            "provider_result": {"arguments": arguments},
        }

    def stop_session(self, session_id: str) -> dict:
        session = self.get_session(session_id)
        session["status"] = "stopped"
        self.sessions[session_id] = session
        return dict(session)


class ComputerAvailabilityTests(unittest.TestCase):
    def test_opt_in_without_native_helper_has_actionable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Path(temp), enable_computer_tools=True, state_root=Path(temp) / "state")
            try:
                result = runtime.call_tool("computer_status", {})
                self.assertFalse(result["isError"])
                self.assertFalse(result["structuredContent"]["available"])
                self.assertIn(result["structuredContent"]["reason"], {"UNSUPPORTED_PLATFORM", "COMPUTER_UNAVAILABLE"})
            finally:
                runtime.close()


@unittest.skipIf(os.name == "nt", "The v1 app-control lease backend uses POSIX file locks; native control is macOS only.")
class ComputerToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeComputer()
        self.runtime = self.make_runtime("one", self.backend)

    def make_runtime(self, name: str, backend: FakeComputer) -> Runtime:
        workspace = self.root / name
        workspace.mkdir(exist_ok=True)
        runtime = Runtime(workspace, state_root=self.root / "state", permission_mode="host",
                          enable_computer_tools=True, computer_backend=backend, enable_workflow_tools=True,
                          defer_workflow_tools=True, fake_readonly_annotations=True)
        runtime.computer.lock_root = self.root / "locks"
        self.addCleanup(runtime.close)
        return runtime

    def call(self, name: str, arguments: dict, runtime: Runtime | None = None) -> dict:
        result = (runtime or self.runtime).call_tool(name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def error(self, name: str, arguments: dict, code: str, runtime: Runtime | None = None) -> None:
        result = (runtime or self.runtime).call_tool(name, arguments)
        self.assertTrue(result["isError"], result)
        self.assertEqual(result["structuredContent"]["error"]["code"], code, result)

    def request(self, access: str = "control", runtime: Runtime | None = None) -> str:
        return self.call("computer_request_access", {"app_id": "pid:123", "access": access, "reason": "Exercise fixture"}, runtime)["approval_id"]

    def approve(self, approval_id: str, runtime: Runtime | None = None) -> None:
        # Test-only simulation of the desktop operator; no MCP approval endpoint.
        with (runtime or self.runtime).computer.state.connect() as db:
            db.execute("UPDATE approvals SET status='approved' WHERE approval_id=?", (approval_id,))

    def session(self, access: str = "control", runtime: Runtime | None = None) -> str:
        approval_id = self.request(access, runtime)
        self.approve(approval_id, runtime)
        return self.call("computer_session_start", {"approval_id": approval_id}, runtime)["session_id"]

    def action(self, session_id: str, operation_id: str = "op1") -> dict:
        return {"session_id": session_id, "window_id": "w1", "snapshot_id": "s1", "element_id": "e1",
                "action": "set_value", "value": "after", "operation_id": operation_id}

    def test_disabled_catalog_and_direct_discovery_alignment(self) -> None:
        default = Runtime(self.runtime.workspace.root)
        self.addCleanup(default.close)
        self.assertEqual(len(default.exposed_tool_names()), 28)
        self.assertFalse(set(COMPUTER_TOOLS) & set(default.exposed_tool_names()))
        self.assertTrue(set(COMPUTER_TOOLS) <= set(self.runtime.exposed_tool_names()))
        for name, spec in COMPUTER_TOOLS.items():
            self.assertEqual(input_schemas()[name], spec.schema)
            self.assertEqual(tool_annotations(name, fake_readonly=True), tool_annotations(name))
        found = self.call("tool_search", {"query": "app_snapshot"})
        self.assertIn("app_snapshot", json.dumps(found))
        swift = (Path(__file__).resolve().parents[1] / "apps/desktop-client/src-tauri/computer-helper.swift").read_text()
        declaration = re.search(r"let supportedOperations = (\[.*\])", swift).group(1)
        self.assertEqual(set(json.loads(declaration)), NATIVE_OPERATIONS)

    def test_codex_preview_routes_through_existing_approval_and_session_tools(self) -> None:
        assert self.runtime.computer is not None
        if self.runtime.computer.codex_observer is not None:
            self.runtime.computer.codex_observer.close()
        fake_codex = FakeCodexObserver(self.runtime.workflow_store)
        self.runtime.computer.codex_observer = fake_codex

        apps = self.call("app_list", {"provider": "codex", "query": "fixture", "max_results": 10})
        self.assertEqual(apps["provider"], "codex")
        self.assertEqual(apps["apps"][0]["app_id"], "com.example.codex")
        approval = self.call(
            "computer_request_access",
            {"provider": "codex", "app_id": "com.example.codex", "access": "observe", "reason": "preview fixture"},
        )
        self.approve(approval["approval_id"])
        session = self.call("computer_session_start", {"approval_id": approval["approval_id"]})
        self.assertTrue(session["session_id"].startswith("codex_computer_"))
        self.assertEqual(session["provider"], "codex")

        observed_result = self.runtime.call_tool(
            "app_observe",
            {"session_id": session["session_id"], "include_image": True, "max_text_chars": 1000},
        )
        self.assertFalse(observed_result["isError"], observed_result)
        observed = observed_result["structuredContent"]
        self.assertEqual(observed["text"], "AX fixture")
        self.assertTrue(any(item.get("type") == "image" for item in observed_result["content"] if isinstance(item, dict)))

        sessions = self.call("computer_session_get", {})["sessions"]
        self.assertTrue(any(item.get("session_id") == session["session_id"] for item in sessions))
        stopped = self.call("computer_session_stop", {"session_id": session["session_id"]})
        self.assertEqual(stopped["status"], "stopped")

    def test_codex_control_requires_fresh_snapshot_and_deduplicates_actions(self) -> None:
        assert self.runtime.computer is not None
        if self.runtime.computer.codex_observer is not None:
            self.runtime.computer.codex_observer.close()
        fake_codex = FakeCodexObserver(self.runtime.workflow_store)
        self.runtime.computer.codex_observer = fake_codex

        observe_approval = self.call(
            "computer_request_access",
            {"provider": "codex", "app_id": "com.example.codex", "access": "observe", "reason": "observe only"},
        )
        self.approve(observe_approval["approval_id"])
        observe_session = self.call("computer_session_start", {"approval_id": observe_approval["approval_id"]})
        observed = self.call("app_observe", {"session_id": observe_session["session_id"], "include_image": False})
        self.error(
            "app_interact",
            {
                "session_id": observe_session["session_id"], "snapshot_id": observed["snapshot_id"],
                "action": "press_key", "key": "Escape", "operation_id": "obs-op",
            },
            "COMPUTER_CONTROL_REQUIRED",
        )

        control_approval = self.call(
            "computer_request_access",
            {"provider": "codex", "app_id": "com.example.codex", "access": "control", "reason": "control fixture"},
        )
        self.approve(control_approval["approval_id"])
        control_session = self.call("computer_session_start", {"approval_id": control_approval["approval_id"]})
        control_observed = self.call("app_observe", {"session_id": control_session["session_id"], "include_image": False})
        action = {
            "session_id": control_session["session_id"], "snapshot_id": control_observed["snapshot_id"],
            "action": "press_key", "key": "Escape", "operation_id": "codex-op-1",
        }
        first = self.call("app_interact", action)
        self.assertFalse(first["verified"])
        self.assertEqual(fake_codex.interactions, 1)
        replay = self.call("app_interact", action)
        self.assertTrue(replay["replayed"])
        self.assertEqual(fake_codex.interactions, 1)
        self.error("app_interact", {**action, "key": "Enter"}, "OPERATION_CONFLICT")
        receipt = self.call(
            "computer_session_get",
            {"session_id": control_session["session_id"], "operation_id": "codex-op-1"},
        )
        self.assertEqual(receipt["operation"]["status"], "completed")
        self.error(
            "app_interact",
            {**action, "operation_id": "codex-op-stale", "snapshot_id": "stale"},
            "COMPUTER_SNAPSHOT_STALE",
        )

    def test_desktop_style_db_revocation_stops_codex_session(self) -> None:
        assert self.runtime.computer is not None
        if self.runtime.computer.codex_observer is not None:
            self.runtime.computer.codex_observer.close()
        fake_codex = FakeCodexObserver(self.runtime.workflow_store)
        self.runtime.computer.codex_observer = fake_codex
        approval = self.call(
            "computer_request_access",
            {"provider": "codex", "app_id": "com.example.codex", "access": "observe", "reason": "desktop revoke"},
        )
        self.approve(approval["approval_id"])
        session = self.call("computer_session_start", {"approval_id": approval["approval_id"]})
        with self.runtime.computer.state.connect() as db:
            db.execute("UPDATE computer_sessions SET status='stopped' WHERE session_id=?", (session["session_id"],))
        for _ in range(20):
            if fake_codex.sessions[session["session_id"]]["status"] == "stopped":
                break
            threading.Event().wait(0.05)
        self.assertEqual(fake_codex.sessions[session["session_id"]]["status"], "stopped")
        self.error("app_observe", {"session_id": session["session_id"]}, "COMPUTER_SESSION_INACTIVE")

    def test_codex_preview_is_unavailable_outside_host_mode(self) -> None:
        workspace = self.root / "safe"
        workspace.mkdir(exist_ok=True)
        runtime = Runtime(
            workspace,
            state_root=self.root / "safe-state",
            enable_computer_tools=True,
            computer_backend=FakeComputer(),
        )
        self.addCleanup(runtime.close)
        self.error("app_list", {"provider": "codex"}, "CODEX_CAPABILITY_UNAVAILABLE", runtime)

    def test_host_still_requires_exact_single_use_operator_approval(self) -> None:
        approval_id = self.request()
        self.error("computer_session_start", {"approval_id": approval_id}, "APPROVAL_NOT_USABLE")
        self.approve(approval_id)
        self.call("computer_session_start", {"approval_id": approval_id})
        # An observation session avoids the control lock obscuring consumed status.
        second = self.request("observe")
        self.approve(second)
        self.call("computer_session_start", {"approval_id": second})
        self.error("computer_session_start", {"approval_id": second}, "APPROVAL_NOT_USABLE")

    def test_changed_app_identity_and_scope_tampering_fail(self) -> None:
        approval_id = self.request()
        self.approve(approval_id)
        self.backend.app["instance_id"] = "123:2"
        self.error("computer_session_start", {"approval_id": approval_id}, "COMPUTER_APP_CHANGED")
        approval_id = self.request("observe")
        self.approve(approval_id)
        with self.runtime.computer.state.connect() as db:
            scope = self.runtime.workflow_store.get_approval(approval_id)["arguments"]
            scope["access"] = "control"
            db.execute("UPDATE approvals SET arguments_json=? WHERE approval_id=?", (json.dumps(scope), approval_id))
        self.error("computer_session_start", {"approval_id": approval_id}, "APPROVAL_SCOPE_MISMATCH")

    def test_model_cannot_control_its_own_approval_or_permission_ui(self) -> None:
        for bundle in ("com.codingtoolsmcp.desktop", "com.codingtoolsmcp.desktop.app-helper", "com.apple.systempreferences"):
            self.backend.app["bundle_id"] = bundle
            self.error("computer_request_access", {"app_id": "pid:123", "access": "control", "reason": "Approve another app"}, "COMPUTER_PROTECTED_APP")
        self.assertEqual(self.runtime.workflow_store.list_approvals()["approvals"], [])

    def test_observe_session_cannot_act_and_helpers_cannot_change(self) -> None:
        session = self.session("observe")
        self.error("app_action", self.action(session), "COMPUTER_CONTROL_REQUIRED")
        self.assertEqual(self.backend.actions, 0)
        self.backend.helper_id = "new-helper"
        self.error("app_windows", {"session_id": session}, "COMPUTER_HELPER_CHANGED")

    def test_action_receipts_deduplicate_and_reject_conflicts(self) -> None:
        session = self.session()
        action = self.action(session)
        first = self.call("app_action", action)
        self.assertFalse(first["verified"])
        self.assertTrue(self.call("app_action", action)["replayed"])
        self.error("app_action", {**action, "value": "different"}, "OPERATION_CONFLICT")
        self.assertEqual(self.backend.actions, 1)
        receipt = self.call("computer_session_get", {"session_id": session, "operation_id": "op1"})
        self.assertEqual(receipt["operation"]["status"], "completed")

    def test_uncertain_action_is_not_retried(self) -> None:
        session = self.session()
        self.backend.fail_action = True
        self.error("app_action", self.action(session), "COMPUTER_HELPER_FAILED")
        self.error("app_action", self.action(session), "COMPUTER_ACTION_UNKNOWN")
        self.assertEqual(self.backend.actions, 1)

    def test_receipt_limit_does_not_evict_or_repeat_existing_actions(self) -> None:
        session = self.session()
        with mock.patch("coding_tools_mcp.computer.MAX_OPERATIONS_PER_SESSION", 1):
            self.call("app_action", self.action(session))
            self.error("app_action", self.action(session, "op2"), "COMPUTER_OPERATION_LIMIT")
            self.assertTrue(self.call("app_action", self.action(session))["replayed"])
        self.assertEqual(self.backend.actions, 1)

    def test_control_lock_coordinates_workspaces_and_stop_releases_it(self) -> None:
        session = self.session()
        other = self.make_runtime("two", FakeComputer())
        approval = self.request(runtime=other)
        self.approve(approval, other)
        self.error("computer_session_start", {"approval_id": approval}, "COMPUTER_APP_BUSY", other)
        self.call("computer_session_stop", {"session_id": session})
        self.call("computer_session_stop", {"session_id": session})
        self.call("computer_session_start", {"approval_id": approval}, other)
        self.error("app_windows", {"session_id": session}, "COMPUTER_SESSION_INACTIVE")
        self.error("app_windows", {"session_id": session}, "COMPUTER_SESSION_NOT_FOUND", other)

    def test_expiry_and_desktop_revocation_interrupt_wait(self) -> None:
        session = self.session("observe")
        with self.runtime.computer.state.connect() as db:
            db.execute("UPDATE computer_sessions SET expires_at=0 WHERE session_id=?", (session,))
        self.error("app_windows", {"session_id": session}, "COMPUTER_SESSION_INACTIVE")
        session = self.session("observe")
        result = []
        def wait() -> None:
            result.append(self.runtime.call_tool("app_wait", {"session_id": session, "window_id": "w1", "snapshot_id": "s1", "element_id": "e1", "condition": "value_equals", "value": "never", "timeout_ms": 10000}))
        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(self.backend.inspect_started.wait(2))
        with self.runtime.computer.state.connect() as db:
            db.execute("UPDATE computer_sessions SET status='stopped' WHERE session_id=?", (session,))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["structuredContent"]["error"]["code"], "COMPUTER_SESSION_INACTIVE")

    def test_stop_prevents_an_action_queued_for_the_native_transport(self) -> None:
        session = self.session()
        result = []
        service = self.runtime.computer
        pending = threading.Event()
        original = service._native
        def queued(*args, **kwargs):
            pending.set()
            return original(*args, **kwargs)
        with service.native_lock, mock.patch.object(service, "_native", side_effect=queued):
            thread = threading.Thread(target=lambda: result.append(self.runtime.call_tool("app_action", self.action(session))))
            thread.start()
            self.assertTrue(pending.wait(2))
            with service.state.connect() as db:
                db.execute("UPDATE computer_sessions SET status='stopped' WHERE session_id=?", (session,))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.backend.actions, 0)
        self.assertEqual(result[0]["structuredContent"]["error"]["code"], "COMPUTER_SESSION_INACTIVE")

    def test_trace_does_not_log_typed_text_or_request_reason(self) -> None:
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"CODING_TOOLS_MCP_TRACE": "1"}), contextlib.redirect_stderr(output):
            session = self.session()
            self.call("app_action", {**self.action(session), "value": "PRIVATE_TYPED_CONTENT"})
        self.assertNotIn("PRIVATE_TYPED_CONTENT", output.getvalue())
        self.assertNotIn("Exercise fixture", output.getvalue())
        self.assertIn("app_action", output.getvalue())

    def test_http_image_content_and_action_observation_loop(self) -> None:
        server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, self.runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = MCPClient(self.runtime.workspace.root, url=f"http://127.0.0.1:{server.server_port}/mcp")
        client.initialize()
        self.assertIn("app_snapshot", {tool["name"] for tool in client.list_tools()})
        request = client.call_tool("computer_request_access", {"app_id": "pid:123", "access": "control", "reason": "HTTP fixture"})["structuredContent"]
        self.approve(request["approval_id"])
        session = client.call_tool("computer_session_start", {"approval_id": request["approval_id"]})["structuredContent"]["session_id"]
        windows = client.call_tool("app_windows", {"session_id": session})["structuredContent"]
        self.assertEqual(windows["windows"][0]["window_id"], "w1")
        args = {"session_id": session, "window_id": "w1"}
        snapshot = client.call_tool("app_snapshot", args)
        self.assertFalse(snapshot["isError"], snapshot)
        images = [item for item in snapshot["content"] if item["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["data"], PNG)
        self.assertNotIn(PNG, json.dumps(snapshot["structuredContent"]))
        self.assertIn('"snapshot_id":"s1"', next(item["text"] for item in snapshot["content"] if item["type"] == "text"))
        no_image = client.call_tool("app_snapshot", {**args, "include_image": False})
        self.assertFalse(any(item["type"] == "image" for item in no_image["content"]))
        self.assertFalse(client.call_tool("app_action", self.action(session))["isError"])
        waited = client.call_tool("app_wait", {**args, "snapshot_id": "s1", "element_id": "e1", "condition": "value_equals", "value": "after"})
        self.assertEqual(waited["structuredContent"]["status"], "satisfied")
        client.call_tool("computer_session_stop", {"session_id": session})


if __name__ == "__main__":
    unittest.main()
