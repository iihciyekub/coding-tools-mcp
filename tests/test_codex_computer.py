from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.codex_computer import (
    CodexComputerApp,
    CodexComputerImage,
    CodexComputerObservationService,
    CodexComputerProvider,
    CodexComputerState,
)
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.workflow_store import WorkflowStore


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


class FakeAppServer:
    def __init__(self) -> None:
        self.request_handler = None
        self.next_thread = 1
        self.notifications: list[dict] = []
        self.turns: list[dict] = []
        self.closed = False

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def call(self, method: str, params: dict | None = None, *, timeout: float = 20) -> dict:
        params = params or {}
        if method == "thread/start":
            thread_id = f"thread-{self.next_thread}"
            self.next_thread += 1
            self.notifications.append({
                "method": "mcpServer/startupStatus/updated",
                "params": {"threadId": thread_id, "name": "node_repl", "status": "ready"},
            })
            return {"thread": {"id": thread_id, "turns": []}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": list(self.turns)}}
        if method == "mcpServer/tool/call":
            code = params["arguments"]["code"]
            if "list_apps" in code:
                apps = [
                    {"app_id": "com.example.one", "display_name": "One", "is_running": True,
                     "last_used_date": None, "use_count": 3},
                    {"app_id": "com.example.two", "display_name": "Two", "is_running": False,
                     "last_used_date": None, "use_count": None},
                ]
                return {"content": [{"type": "text", "text": json.dumps(apps)}], "isError": False}
            if "get_app_state" in code:
                assert self.request_handler is not None
                response = self.request_handler("mcpServer/elicitation/request", {
                    "threadId": params["threadId"],
                    "message": 'Allow Computer Use to use "One"?',
                    "_meta": {
                        "connector_id": "computer-use",
                        "tool_name": "get_app_state",
                        "riskLevel": "low",
                        "tool_params": {"app": "com.example.one"},
                    },
                })
                if response.get("action") != "accept":
                    return {"content": [{"type": "text", "text": "Computer Use was not approved"}], "isError": True}
                screenshot = "data:image/png;base64," + base64.b64encode(PNG).decode()
                state = {
                    "app": "com.example.one",
                    "text": "0123456789",
                    "text_truncated": False,
                    "screenshot_url": screenshot,
                }
                # The production JS performs truncation before writing JSON.
                marker = "text:t.slice(0,"
                if marker in code:
                    limit = int(code.split(marker, 1)[1].split(")", 1)[0])
                    state["text_truncated"] = len(state["text"]) > limit
                    state["text"] = state["text"][:limit]
                return {"content": [{"type": "text", "text": json.dumps(state)}], "isError": False}
            raise AssertionError(code)
        if method == "initialize":
            return {}
        raise AssertionError(method)

    def wait_notification(self, method: str, *, timeout: float = 20) -> dict:
        for index, item in enumerate(self.notifications):
            if item.get("method") == method:
                return self.notifications.pop(index)
        raise AssertionError(f"missing notification: {method}")


class CodexComputerProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = CodexComputerProvider(self.root)
        self.fake = FakeAppServer()
        self.fake.request_handler = self.provider._handle_server_request
        self.provider._client = self.fake  # type: ignore[assignment]
        self.addCleanup(self.provider.close)

    def test_list_apps_is_read_only_and_checks_no_model_turns(self) -> None:
        apps = self.provider.list_apps()
        self.assertEqual([app.app_id for app in apps], ["com.example.one", "com.example.two"])
        self.assertTrue(apps[0].is_running)
        self.assertEqual(apps[0].use_count, 3)
        self.assertFalse(any(hasattr(self.provider, name) for name in ("click", "type_text", "drag", "press_key")))

    def test_observation_requires_matching_approval_and_reads_screenshot(self) -> None:
        approvals: list[dict] = []

        def approve(request: dict) -> bool:
            approvals.append(request)
            return True

        with self.provider.open_observation("com.example.one", approve) as session:
            state = session.get_state(max_text_chars=5)
            self.assertEqual(state.app, "com.example.one")
            self.assertEqual(state.text, "01234")
            self.assertTrue(state.text_truncated)
            image = session.read_screenshot(state)
            self.assertEqual(image.data, PNG)
            self.assertEqual(image.mime_type, "image/png")
        self.assertEqual(approvals[0]["app_id"], "com.example.one")
        self.assertEqual(approvals[0]["risk_level"], "low")

    def test_mismatched_or_unregistered_elicitation_is_denied(self) -> None:
        session = self.provider.open_observation("com.example.one", lambda _: True)
        self.addCleanup(session.close)
        wrong_app = self.provider._handle_server_request("mcpServer/elicitation/request", {
            "threadId": session.thread_id,
            "_meta": {
                "connector_id": "computer-use",
                "tool_name": "get_app_state",
                "tool_params": {"app": "com.example.two"},
            },
        })
        self.assertEqual(wrong_app["action"], "decline")
        unknown_thread = self.provider._handle_server_request("mcpServer/elicitation/request", {
            "threadId": "missing",
            "_meta": {
                "connector_id": "computer-use",
                "tool_name": "get_app_state",
                "tool_params": {"app": "com.example.one"},
            },
        })
        self.assertEqual(unknown_thread["action"], "decline")

    def test_model_turn_guard_closes_provider(self) -> None:
        self.fake.turns = [{"id": "unexpected"}]
        with self.assertRaises(ToolFailure) as caught:
            self.provider.list_apps()
        self.assertEqual(caught.exception.code, "CODEX_MODEL_TURN_GUARD_FAILED")
        self.assertTrue(self.fake.closed)

    def test_file_screenshot_is_bounded_and_remote_url_is_rejected(self) -> None:
        image_path = self.root / "shot.png"
        image_path.write_bytes(PNG)
        image = self.provider.read_screenshot(image_path.as_uri())
        self.assertEqual(image.data, PNG)
        self.assertEqual(image.mime_type, "image/png")
        with self.assertRaises(ToolFailure) as caught:
            self.provider.read_screenshot("https://example.com/shot.png")
        self.assertEqual(caught.exception.code, "CODEX_CAPABILITY_UNAVAILABLE")


class FakeObservationSession:
    def __init__(self, app_id: str, approval_decider) -> None:
        self.app_id = app_id
        self.approval_decider = approval_decider
        self.access = "observe"
        self.closed = False
        self.interactions: list[tuple[str, dict]] = []

    def get_state(self, *, disable_diff: bool = True, max_text_chars: int = 256_000) -> CodexComputerState:
        if self.closed:
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "closed", category="permission")
        if not self.approval_decider({"app_id": self.app_id, "risk_level": "low", "message": "Allow?"}):
            raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "approval denied", category="permission")
        text = "fixture-state"[:max_text_chars]
        return CodexComputerState(
            app=self.app_id,
            text=text,
            text_truncated=len("fixture-state") > max_text_chars,
            screenshot_url="data:image/png;base64," + base64.b64encode(PNG).decode(),
        )

    def read_screenshot(self, state: CodexComputerState, *, max_bytes: int = 5 * 1024 * 1024) -> CodexComputerImage:
        return CodexComputerImage(data=PNG, mime_type="image/png")

    def interact(self, operation: str, arguments: dict) -> dict:
        if self.closed:
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "closed", category="permission")
        if self.access != "control":
            raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "observe only", category="permission")
        if not self.approval_decider({
            "app_id": self.app_id,
            "tool_name": operation,
            "risk_level": "medium",
            "message": "Allow control?",
        }):
            raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "approval denied", category="permission")
        self.interactions.append((operation, dict(arguments)))
        return {"ok": True, "result": {"operation": operation}}

    def close(self) -> None:
        self.closed = True


class FakeObservationProvider:
    def __init__(self) -> None:
        self.apps = [CodexComputerApp("com.example.one", "One", True)]
        self.sessions: list[FakeObservationSession] = []
        self.closed = False
        self.fail_open = False

    def list_apps(self) -> list[CodexComputerApp]:
        return list(self.apps)

    def open_observation(self, app_id: str, approval_decider) -> FakeObservationSession:
        return self.open_session(app_id, "observe", approval_decider)

    def open_session(self, app_id: str, access: str, approval_decider) -> FakeObservationSession:
        if self.fail_open:
            raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "broker failed", category="runtime")
        session = FakeObservationSession(app_id, approval_decider)
        session.access = access
        self.sessions.append(session)
        return session

    def close(self) -> None:
        self.closed = True
        for session in self.sessions:
            session.close()


class CodexComputerObservationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workflow = WorkflowStore(self.root, state_root=self.root / "state")
        self.provider = FakeObservationProvider()
        self.service = CodexComputerObservationService(self.workflow, self.provider)  # type: ignore[arg-type]
        self.addCleanup(self.service.close)

    def approve(self, approval_id: str) -> None:
        with self.workflow._lock, self.workflow._connection() as db:  # test-only operator simulation
            db.execute("UPDATE approvals SET status='approved' WHERE approval_id=?", (approval_id,))

    def test_human_approval_session_observe_and_stop(self) -> None:
        approval = self.service.request_access("com.example.one", reason="Inspect fixture", ttl_seconds=60)
        with self.assertRaises(ToolFailure) as caught:
            self.service.start_session(approval["approval_id"])
        self.assertEqual(caught.exception.code, "APPROVAL_NOT_USABLE")
        self.approve(approval["approval_id"])
        session = self.service.start_session(approval["approval_id"])
        self.assertEqual(session["provider"], "codex")
        self.assertEqual(session["access"], "observe")
        observed = self.service.observe(session["session_id"], max_text_chars=7)
        self.assertEqual(observed["text"], "fixture")
        self.assertTrue(observed["text_truncated"])
        self.assertEqual(observed["mime_type"], "image/png")
        self.assertIn("_mcp_image_data", observed)
        stopped = self.service.stop_session(session["session_id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(self.provider.sessions[-1].closed)

    def test_approval_is_single_use_and_app_identity_is_rechecked(self) -> None:
        approval = self.service.request_access("com.example.one", reason="Inspect fixture")
        self.approve(approval["approval_id"])
        self.provider.apps = [CodexComputerApp("com.example.one", "Renamed", True)]
        with self.assertRaises(ToolFailure) as caught:
            self.service.start_session(approval["approval_id"])
        self.assertEqual(caught.exception.code, "COMPUTER_APP_CHANGED")

        self.provider.apps = [CodexComputerApp("com.example.one", "One", True)]
        approval = self.service.request_access("com.example.one", reason="Inspect fixture")
        self.approve(approval["approval_id"])
        self.service.start_session(approval["approval_id"])
        with self.assertRaises(ToolFailure) as caught:
            self.service.start_session(approval["approval_id"])
        self.assertEqual(caught.exception.code, "APPROVAL_NOT_USABLE")

    def test_provider_open_failure_does_not_consume_approval(self) -> None:
        approval = self.service.request_access("com.example.one", reason="Inspect fixture")
        self.approve(approval["approval_id"])
        self.provider.fail_open = True
        with self.assertRaises(ToolFailure) as caught:
            self.service.start_session(approval["approval_id"])
        self.assertEqual(caught.exception.code, "CODEX_CAPABILITY_UNAVAILABLE")
        self.assertEqual(self.workflow.get_approval(approval["approval_id"])["status"], "approved")

    def test_expired_session_denies_nested_approval_and_is_reaped(self) -> None:
        approval = self.service.request_access("com.example.one", reason="Inspect fixture", ttl_seconds=30)
        self.approve(approval["approval_id"])
        session = self.service.start_session(approval["approval_id"])
        lease = self.service._sessions[session["session_id"]]
        lease.expires_at = 0
        allowed = self.service._approve_internal(session["session_id"], {"app_id": "com.example.one"})
        self.assertFalse(allowed)
        self.assertEqual(lease.status, "expired")
        state = self.service.get_session(session["session_id"])
        self.assertEqual(state["status"], "expired")
        self.assertTrue(self.provider.sessions[-1].closed)

    def test_request_is_observe_only(self) -> None:
        approval = self.service.request_access("com.example.one", reason="Read only")
        self.assertEqual(approval["arguments"]["access"], "observe")
        self.assertEqual(approval["arguments"]["provider"], "codex")

    def test_control_requires_control_scope_and_fresh_snapshot(self) -> None:
        observe_approval = self.service.request_access("com.example.one", reason="Observe fixture")
        self.approve(observe_approval["approval_id"])
        observe_session = self.service.start_session(observe_approval["approval_id"])
        observed = self.service.observe(observe_session["session_id"], include_image=False)
        with self.assertRaises(ToolFailure) as caught:
            self.service.interact(
                observe_session["session_id"],
                snapshot_id=observed["snapshot_id"],
                action="press_key",
                arguments={"key": "Escape"},
            )
        self.assertEqual(caught.exception.code, "COMPUTER_CONTROL_REQUIRED")

        control_approval = self.service.request_access(
            "com.example.one", access="control", reason="Control fixture"
        )
        self.assertEqual(control_approval["arguments"]["access"], "control")
        self.approve(control_approval["approval_id"])
        control_session = self.service.start_session(control_approval["approval_id"])
        control_observed = self.service.observe(control_session["session_id"], include_image=False)
        result = self.service.interact(
            control_session["session_id"],
            snapshot_id=control_observed["snapshot_id"],
            action="press_key",
            arguments={"key": "Escape"},
        )
        self.assertEqual(result["status"], "submitted")
        self.assertFalse(result["verified"])
        self.assertEqual(self.provider.sessions[-1].interactions, [("press_key", {"key": "Escape"})])

        refreshed = self.service.observe(control_session["session_id"], include_image=False)
        with self.assertRaises(ToolFailure) as caught:
            self.service.interact(
                control_session["session_id"],
                snapshot_id="codex_snapshot_stale",
                action="press_key",
                arguments={"key": "Escape"},
            )
        self.assertEqual(caught.exception.code, "COMPUTER_SNAPSHOT_STALE")
        self.assertTrue(refreshed["snapshot_id"].startswith("codex_snapshot_"))


if __name__ == "__main__":
    unittest.main()

