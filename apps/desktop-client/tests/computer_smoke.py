"""Run from the repo root with .venv/bin/python; only controls our temporary fixture."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from coding_tools_mcp.computer import NativeHelper  # noqa: E402
from coding_tools_mcp.server import Runtime  # noqa: E402


def main() -> int:
    helper = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "apps/desktop-client/src-tauri/resources/computer/Coding Tools MCP App Helper.app/Contents/MacOS/coding-tools-computer-helper"
    with tempfile.TemporaryDirectory(prefix="computer-smoke-") as temp:
        root = Path(temp)
        executable = root / "ComputerFixture"
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", str(Path(__file__).with_name("computer-fixture.swift")), "-o", str(executable)], check=True)
        fixture = subprocess.Popen([str(executable)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        backend = NativeHelper(helper)
        runtime = Runtime(root, enable_computer_tools=True, computer_backend=backend, state_root=root / "state")
        try:
            def call(name: str, args: dict) -> dict:
                result = runtime.call_tool(name, args)
                assert not result["isError"], result
                return result["structuredContent"]

            status = call("computer_status", {})
            print(json.dumps({"stage": "permissions", "accessibility": status.get("accessibility"), "screen_recording": status.get("screen_recording")}))
            deadline = time.monotonic() + 10
            while True:
                apps = call("app_list", {"max_results": 100})["apps"]
                app = next((app for app in apps if app["pid"] == fixture.pid), None)
                if app:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Fixture did not start")
                time.sleep(0.1)
            request = call("computer_request_access", {"app_id": app["app_id"], "access": "control", "reason": "Disposable integration fixture", "ttl_seconds": 60})
            # Human approval simulation is restricted to our isolated temporary state and PID.
            assert request["arguments"]["app"]["pid"] == fixture.pid
            with runtime.computer.state.connect() as db:
                db.execute("UPDATE approvals SET status='approved' WHERE approval_id=?", (request["approval_id"],))
            session = call("computer_session_start", {"approval_id": request["approval_id"]})["session_id"]
            if not status.get("accessibility"):
                denied = runtime.call_tool("app_windows", {"session_id": session})
                assert denied["structuredContent"]["error"]["code"] == "ACCESSIBILITY_PERMISSION_REQUIRED", denied
                print(json.dumps({"stage": "permission_denied", "passed": True, "gui_acceptance": "blocked"}))
                return 2
            windows = call("app_windows", {"session_id": session})["windows"]
            target = next(window for window in windows if window.get("title") == "Coding Tools MCP Test Fixture")
            args = {"session_id": session, "window_id": target["window_id"]}
            observed = runtime.call_tool("app_snapshot", {**args, "include_image": bool(status.get("screen_recording"))})
            assert not observed["isError"], observed
            snapshot = observed["structuredContent"]
            if status.get("screen_recording"):
                assert sum(item["type"] == "image" for item in observed["content"]) == 1
                assert "_mcp_image_data" not in snapshot
            field = next(element for element in snapshot["elements"] if element.get("identifier") == "fixture-input")
            action = {**args, "snapshot_id": snapshot["snapshot_id"], "element_id": field["element_id"], "operation_id": "fixture-set", "action": "set_value", "value": "Updated in background"}
            call("app_action", action)
            assert call("app_action", action)["replayed"]
            waited = call("app_wait", {**args, "snapshot_id": snapshot["snapshot_id"], "element_id": field["element_id"], "condition": "value_equals", "value": action["value"]})
            assert waited["status"] == "satisfied", waited
            snapshot = call("app_snapshot", {**args, "include_image": False})
            button = next(element for element in snapshot["elements"] if element.get("identifier") == "fixture-button")
            call("app_action", {**args, "snapshot_id": snapshot["snapshot_id"], "element_id": button["element_id"], "operation_id": "fixture-press", "action": "press"})
            snapshot = call("app_snapshot", {**args, "include_image": False})
            label = next(element for element in snapshot["elements"] if element.get("identifier") == "fixture-status")
            assert label["value"] == "Pressed successfully", label
            focus = next(element for element in snapshot["elements"] if element.get("identifier") == "fixture-focus")
            assert focus["value"] == "Background", focus
            call("computer_session_stop", {"session_id": session})
            denied = runtime.call_tool("app_windows", {"session_id": session})
            assert denied["structuredContent"]["error"]["code"] == "COMPUTER_SESSION_INACTIVE"
            print(json.dumps({"stage": "native_loop", "passed": True, "image": bool(status.get("screen_recording")), "set_value": True, "press": True, "background": True, "receipt_replay": True, "stop": True}))
            return 0
        finally:
            runtime.close()
            fixture.terminate()
            fixture.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
