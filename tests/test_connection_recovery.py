from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.gateway import GatewayRuntime, HTTPProjectRuntime, ProjectDefinition, ProjectRegistry, SessionCapacityExceeded, SessionRegistry
from coding_tools_mcp.server import MCPHandler, Runtime, RuntimeHTTPServer, tool_definition


class ConnectionRecoveryTests(unittest.TestCase):
    def test_cold_build_probe_recovers_before_safe_tool_execution(self) -> None:
        proxy = HTTPProjectRuntime(ProjectDefinition("p", "Project", Path("/tmp"), "http://127.0.0.1:1/mcp", "expected"))
        with patch.object(proxy, "_rpc", side_effect=[
            RuntimeError("temporary startup failure"),
            {"structuredContent": {"runtime_build_id": "expected"}},
            {"structuredContent": {"ok": True}},
        ]) as rpc:
            self.assertEqual(proxy.call_tool("read_file", {"path": "a"})["structuredContent"], {"ok": True})
            self.assertEqual([call.args[1]["name"] for call in rpc.call_args_list], ["server_info", "server_info", "read_file"])
            self.assertEqual(proxy.state, "ready")

    def test_cold_probe_failure_is_structured_and_never_starts_a_mutation(self) -> None:
        proxy = HTTPProjectRuntime(ProjectDefinition("p", "Project", Path("/tmp"), "http://127.0.0.1:1/mcp", "expected"))
        with patch.object(proxy, "_rpc", side_effect=RuntimeError("offline")) as rpc:
            result = proxy.call_tool("exec_command", {"cmd": "echo demo"})
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "PROJECT_RUNTIME_UNAVAILABLE")
            self.assertEqual(rpc.call_count, 1)
            self.assertEqual(proxy.state, "unreachable")

    def test_session_capacity_preserves_connections_until_explicit_delete(self) -> None:
        sessions = SessionRegistry(max_sessions=2)
        first = sessions.create(selected_project_id="a")
        second = sessions.create(selected_project_id="b")
        with self.assertRaises(SessionCapacityExceeded):
            sessions.create()
        self.assertEqual(sessions.get(first.session_id).selected_project_id, "a")
        self.assertEqual(sessions.get(second.session_id).selected_project_id, "b")
        self.assertEqual(sessions.statistics()["rejected"], 1)
        sessions.delete(first.session_id)
        sessions.create()
        self.assertEqual(sessions.statistics()["active"], 2)

    def test_http_capacity_returns_busy_and_delete_releases_the_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = Runtime(root, state_root=root / "state")
            definition = ProjectDefinition("p", "Project", root)
            projects = ProjectRegistry(definition, control, runtime_factory=lambda _: control)
            gateway = GatewayRuntime(control, projects, SessionRegistry(max_sessions=1), project_tool_definition=lambda: tool_definition("project_context"))
            server = RuntimeHTTPServer(("127.0.0.1", 0), MCPHandler, gateway)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/mcp"
            data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}).encode()
            headers = {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=3) as response:
                    session_id = response.headers["Mcp-Session-Id"]
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=3)
                self.assertEqual(error.exception.code, 503)
                self.assertEqual(error.exception.headers["Retry-After"], "1")
                error.exception.close()
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Mcp-Session-Id": session_id}, method="DELETE"), timeout=3) as response:
                    self.assertEqual(response.status, 204)
                with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=3) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()

    def test_completed_execution_is_not_repeated_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = {"cmd": "printf 'once\\n' >> count.txt", "operation_id": "one", "yield_time_ms": 1000}
            first = Runtime(root, permission_mode="host", state_root=root / "state")
            try:
                self.assertEqual(first.exec_command(args)["exit_code"], 0)
            finally:
                first.close()
            second = Runtime(root, permission_mode="host", state_root=root / "state")
            try:
                result = second.exec_command(args)
                self.assertTrue(result["deduplicated"])
                self.assertTrue(result["recovered_from_journal"])
                self.assertEqual(result["exit_code"], 0)
                self.assertTrue(second.get_command({"operation_id": "one"})["deduplicated"])
                with self.assertRaises(ToolFailure) as error:
                    second.exec_command({**args, "cmd": "echo changed"})
                self.assertEqual(error.exception.code, "OPERATION_CONFLICT")
            finally:
                second.close()
            self.assertEqual((root / "count.txt").read_text().splitlines(), ["once"])

    def test_unknown_outcome_is_not_reexecuted_and_failed_spawn_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = Runtime(root, permission_mode="host", state_root=root / "state")
            try:
                runtime.runtime_state.reserve_execution("unknown", "digest", "old-instance")
                with self.assertRaises(ToolFailure) as error:
                    runtime._durable_operation_replay("unknown", "digest")
                self.assertEqual(error.exception.code, "OPERATION_OUTCOME_UNKNOWN")
                self.assertFalse(error.exception.retryable)
                args = {"cmd": "echo recovered", "operation_id": "spawn", "yield_time_ms": 1000}
                with patch("coding_tools_mcp.server.spawn_process", side_effect=OSError("test spawn failure")):
                    with self.assertRaises(OSError):
                        runtime.exec_command(args)
                self.assertIsNone(runtime.runtime_state.execution("spawn"))
                self.assertEqual(runtime.exec_command(args)["exit_code"], 0)
            finally:
                runtime.close()

    def test_concurrent_runtime_instances_accept_operation_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtimes = [Runtime(root, permission_mode="host", state_root=root / "state") for _ in range(2)]
            barrier = threading.Barrier(2)
            args = {"cmd": "printf 'once\\n' >> count.txt", "operation_id": "race", "yield_time_ms": 1000}
            def run(runtime: Runtime) -> None:
                barrier.wait()
                try:
                    runtime.exec_command(args)
                except ToolFailure as error:
                    self.assertEqual(error.code, "OPERATION_OUTCOME_UNKNOWN")
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(run, runtimes))
                self.assertEqual((root / "count.txt").read_text().splitlines(), ["once"])
            finally:
                for runtime in runtimes:
                    runtime.close()
