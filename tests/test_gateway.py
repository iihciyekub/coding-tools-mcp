from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from coding_tools_mcp.gateway import (
    GatewayRuntime,
    HTTPProjectRuntime,
    ProjectDefinition,
    ProjectRegistry,
    SessionRegistry,
)
from coding_tools_mcp.local_capabilities import LocalCapabilityCatalog
from coding_tools_mcp.server import tool_definition
from tests.compliance.mcp_client import MCPClient, free_port, safe_server_env
from tests.compliance.test_support import structured_payload


class _Telemetry:
    def record_request(self, era: str, method: str) -> None:
        del era, method


class _FakeRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.telemetry = _Telemetry()
        self.closed = False

    def exposed_tool_names(self) -> list[str]:
        return ["read_file"]

    def list_tools(self) -> dict[str, Any]:
        return {"tools": [{"name": "read_file"}]}

    def tool_usage_instructions(self) -> str:
        return "workspace instructions"

    def discover_payload(self) -> dict[str, Any]:
        return {"instructions": self.tool_usage_instructions()}

    def initialize(self, client_info: dict[str, Any] | None, protocol_version: str) -> dict[str, Any]:
        del client_info
        return {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.server_identity(),
            "instructions": self.tool_usage_instructions(),
        }

    def initialize_result(self, protocol_version: str) -> dict[str, Any]:
        return self.initialize(None, protocol_version)

    def server_identity(self) -> dict[str, Any]:
        return {"name": "fake", "title": "Fake", "version": "0"}

    def protocol_tasks_enabled(self) -> bool:
        return False

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        del arguments, context
        return {
            "content": [],
            "structuredContent": {
                "ok": True,
                "tool": name,
                "root": str(self.root),
            },
            "isError": False,
        }

    def close(self) -> None:
        self.closed = True


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class GatewayUnitTests(unittest.TestCase):
    def test_registry_discovers_direct_child_git_projects_without_registry_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "child-project"
            child.mkdir()
            (child / ".git").mkdir()
            ignored = root / "plain-dir"
            ignored.mkdir()
            bootstrap = ProjectDefinition("root", "Root", root)
            runtime = _FakeRuntime(root)
            projects = ProjectRegistry(
                bootstrap,
                runtime,
                runtime_factory=lambda definition: _FakeRuntime(definition.path),
            )
            names = {item.name for item in projects.definitions()}
            self.assertIn("Root", names)
            self.assertIn("child-project", names)
            self.assertNotIn("plain-dir", names)

    def test_authorized_local_skill_tools_are_gateway_only_and_readable_before_project_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            sample = skills / "review"
            sample.mkdir(parents=True)
            (sample / "SKILL.md").write_text(
                "---\nname: review\ndescription: Review a project.\n---\n\nCheck the tests.\n",
                encoding="utf-8",
            )
            runtime = _FakeRuntime(root)
            gateway = GatewayRuntime(
                runtime,
                ProjectRegistry(
                    ProjectDefinition("one", "One", root),
                    runtime,
                    runtime_factory=lambda definition: _FakeRuntime(definition.path),
                ),
                SessionRegistry(),
                project_tool_definition=lambda: tool_definition("project_context"),
                local_capabilities=LocalCapabilityCatalog([skills]),
                local_tool_definition=lambda name: tool_definition(name),
            )
            names = {item["name"] for item in gateway.list_tools()["tools"]}
            self.assertTrue({"local_capabilities_search", "local_skill_read", "local_plugin_inspect"} <= names)
            bound = gateway.bind(None)
            found = structured_payload(bound.call_tool("local_capabilities_search", {"query": "review"}))
            self.assertEqual(found["count"], 1)
            read = structured_payload(bound.call_tool("local_skill_read", {"id": found["items"][0]["id"]}))
            self.assertIn("Check the tests.", read["content"])

    def test_project_proxy_leaves_headroom_for_max_yield_command(self) -> None:
        definition = ProjectDefinition(
            "project",
            "Project",
            Path("/tmp/project"),
            "http://127.0.0.1:12345/mcp",
        )
        runtime = HTTPProjectRuntime(definition)
        captured: dict[str, float] = {}

        def fake_urlopen(_request: object, *, timeout: float) -> _FakeHTTPResponse:
            captured["timeout"] = timeout
            return _FakeHTTPResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [],
                        "structuredContent": {"ok": True, "status": "running"},
                        "isError": False,
                    },
                }
            )

        with mock.patch(
            "coding_tools_mcp.gateway.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            result = runtime.call_tool(
                "exec_command",
                {"cmd": "sleep 32", "yield_time_ms": 30000, "timeout_ms": 45000},
            )
        self.assertFalse(result["isError"])
        self.assertGreaterEqual(captured["timeout"], 35.0)

    def test_project_proxy_retries_one_safe_read_after_transient_failure(self) -> None:
        definition = ProjectDefinition(
            "project",
            "Project",
            Path("/tmp/project"),
            "http://127.0.0.1:12345/mcp",
        )
        runtime = HTTPProjectRuntime(definition)
        self.assertEqual(runtime.state, "configured")
        response = _FakeHTTPResponse(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "content": [{"type": "text", "text": "ok"}],
                    "structuredContent": {"ok": True, "path": "x"},
                    "isError": False,
                },
            }
        )
        with mock.patch(
            "coding_tools_mcp.gateway.urllib.request.urlopen",
            side_effect=[OSError("transient"), response],
        ) as opened:
            result = runtime.call_tool("read_file", {"path": "x"})
        self.assertFalse(result["isError"])
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(runtime.state, "ready")

    def test_project_proxy_does_not_retry_non_idempotent_input(self) -> None:
        definition = ProjectDefinition(
            "project",
            "Project",
            Path("/tmp/project"),
            "http://127.0.0.1:12345/mcp",
        )
        runtime = HTTPProjectRuntime(definition)
        with mock.patch(
            "coding_tools_mcp.gateway.urllib.request.urlopen",
            side_effect=OSError("offline"),
        ) as opened:
            result = runtime.call_tool("write_stdin", {"command_id": "abc", "chars": "yes\n"})
        self.assertTrue(result["isError"])
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(runtime.state, "unreachable")

    def test_sessions_keep_project_selection_isolated_and_runtimes_are_lazy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            registry_file = root / "projects.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "default_project_id": "first",
                        "projects": [
                            {"id": "second", "name": "Second", "path": str(second)},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            created: list[str] = []
            first_runtime = _FakeRuntime(first)

            def runtime_factory(definition: ProjectDefinition) -> _FakeRuntime:
                created.append(definition.id)
                return _FakeRuntime(definition.path)

            projects = ProjectRegistry(
                ProjectDefinition("first", "First", first),
                first_runtime,
                runtime_factory=runtime_factory,
                registry_file=registry_file,
            )
            sessions = SessionRegistry()
            gateway = GatewayRuntime(
                first_runtime,
                projects,
                sessions,
                project_tool_definition=lambda: tool_definition("project_context"),
            )
            session_a = sessions.create(selected_project_id="first")
            session_b = sessions.create(selected_project_id="first")
            bound_a = gateway.bind(session_a.session_id)
            bound_b = gateway.bind(session_b.session_id)

            selected = structured_payload(
                bound_a.call_tool(
                    "project_context",
                    {"action": "select", "project_id": "second"},
                )
            )
            self.assertEqual(selected["project_id"], "second")
            self.assertEqual(created, [], "select should not eagerly create a project runtime")

            routed_a = structured_payload(bound_a.call_tool("read_file", {"path": "x"}))
            routed_b = structured_payload(bound_b.call_tool("read_file", {"path": "x"}))
            self.assertEqual(Path(routed_a["root"]).resolve(), second.resolve())
            self.assertEqual(Path(routed_b["root"]).resolve(), first.resolve())
            self.assertEqual(created, ["second"])

    def test_registry_removal_clears_bound_session_without_switching_to_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            registry_file = root / "projects.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "default_project_id": "first",
                        "projects": [{"id": "second", "path": str(second)}],
                    }
                ),
                encoding="utf-8",
            )
            first_runtime = _FakeRuntime(first)
            projects = ProjectRegistry(
                ProjectDefinition("first", "First", first),
                first_runtime,
                runtime_factory=lambda definition: _FakeRuntime(definition.path),
                registry_file=registry_file,
            )
            sessions = SessionRegistry()
            gateway = GatewayRuntime(
                first_runtime,
                projects,
                sessions,
                project_tool_definition=lambda: tool_definition("project_context"),
            )
            session = sessions.create(selected_project_id="second")
            bound = gateway.bind(session.session_id)

            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 2,
                        "default_project_id": "first",
                        "projects": [],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(registry_file, None)
            payload = structured_payload(bound.call_tool("project_context", {"action": "current"}))
            self.assertIsNone(payload["project_id"])
            self.assertIsNone(payload["current"])

            denied = structured_payload(bound.call_tool("read_file", {"path": "x"}))
            self.assertEqual(denied["error"]["code"], "PROJECT_NOT_SELECTED")

    def test_endpoint_change_invalidates_proxy_without_clearing_project_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            registry_file = root / "projects.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "default_project_id": "project",
                        "projects": [
                            {
                                "id": "project",
                                "path": str(project),
                                "endpoint": "http://127.0.0.1:30101/mcp",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            control = _FakeRuntime(root)
            projects = ProjectRegistry(
                None,
                None,
                runtime_factory=lambda definition: _FakeRuntime(definition.path),
                registry_file=registry_file,
            )
            sessions = SessionRegistry()
            gateway = GatewayRuntime(
                control,
                projects,
                sessions,
                project_tool_definition=lambda: tool_definition("project_context"),
            )
            session = sessions.create(selected_project_id="project")
            bound = gateway.bind(session.session_id)
            self.assertEqual(
                structured_payload(bound.call_tool("project_context", {"action": "current"}))[
                    "project_id"
                ],
                "project",
            )
            time.sleep(0.002)
            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 2,
                        "default_project_id": "project",
                        "projects": [
                            {
                                "id": "project",
                                "path": str(project),
                                "endpoint": "http://127.0.0.1:30102/mcp",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.utime(registry_file, None)
            current = structured_payload(bound.call_tool("project_context", {"action": "current"}))
            self.assertEqual(current["project_id"], "project")


class GatewayHTTPTests(unittest.TestCase):
    def test_one_url_routes_two_sessions_to_different_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            local_skill = root / "shared-skills" / "review"
            local_skill.mkdir(parents=True)
            (local_skill / "SKILL.md").write_text(
                "---\nname: review\ndescription: Review a selected project.\n---\n\nCheck project tests.\n",
                encoding="utf-8",
            )
            (first / "marker.txt").write_text("FIRST_ONLY_MARKER\n", encoding="utf-8")
            (second / "marker.txt").write_text("SECOND_ONLY_MARKER\n", encoding="utf-8")
            registry_file = root / "projects.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "default_project_id": "first",
                        "projects": [
                            {"id": "second", "name": "Second", "path": str(second)},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            port = free_port()
            env = safe_server_env()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coding_tools_mcp",
                    "--workspace",
                    str(first),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--project-gateway",
                    "--project-id",
                    "first",
                    "--project-name",
                    "First",
                    "--project-registry-file",
                    str(registry_file),
                    "--local-capability-root",
                    str(local_skill.parent),
                ],
                cwd=str(first),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.time() + 10
                while time.time() < deadline:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate(timeout=1)
                        self.fail(f"gateway exited early: stdout={stdout!r} stderr={stderr!r}")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("gateway did not start")

                url = f"http://127.0.0.1:{port}/mcp"
                with MCPClient(first, url=url) as client_a, MCPClient(first, url=url) as client_b:
                    self.assertIsNotNone(client_a.session_id)
                    self.assertIsNotNone(client_b.session_id)
                    self.assertNotEqual(client_a.session_id, client_b.session_id)
                    tool_names = {item["name"] for item in client_a.list_tools()}
                    self.assertIn("project_context", tool_names)
                    self.assertIn("local_capabilities_search", tool_names)
                    catalog = structured_payload(client_a.call_tool("local_capabilities_search", {"query": "review"}))
                    self.assertEqual(catalog["count"], 1)
                    skill = structured_payload(client_a.call_tool("local_skill_read", {"id": catalog["items"][0]["id"]}))
                    self.assertIn("Check project tests.", skill["content"])

                    current_a = structured_payload(client_a.call_tool("project_context", {"action": "current"}))
                    self.assertEqual(current_a["project_id"], "first")
                    switched = structured_payload(
                        client_a.call_tool(
                            "project_context",
                            {"action": "select", "project_id": "second"},
                        )
                    )
                    self.assertEqual(switched["project_id"], "second")

                    current_b = structured_payload(client_b.call_tool("project_context", {"action": "current"}))
                    self.assertEqual(current_b["project_id"], "first")

                    second_search = structured_payload(
                        client_a.call_tool("search_text", {"query": "SECOND_ONLY_MARKER"})
                    )
                    second_wrong_project = structured_payload(
                        client_a.call_tool("search_text", {"query": "FIRST_ONLY_MARKER"})
                    )
                    first_search = structured_payload(
                        client_b.call_tool("search_text", {"query": "FIRST_ONLY_MARKER"})
                    )
                    first_wrong_project = structured_payload(
                        client_b.call_tool("search_text", {"query": "SECOND_ONLY_MARKER"})
                    )
                    self.assertEqual(second_search["total_matches"], 1)
                    self.assertEqual(first_search["total_matches"], 1)
                    self.assertEqual(second_wrong_project["total_matches"], 0)
                    self.assertEqual(first_wrong_project["total_matches"], 0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, 15)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, 9)
                        process.wait(timeout=3)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_registry_only_gateway_proxies_an_isolated_loopback_project_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root / "control"
            project = root / "project"
            control.mkdir()
            project.mkdir()
            (project / "marker.txt").write_text("PROXIED_PROJECT_MARKER\n", encoding="utf-8")
            child_port = free_port()
            gateway_port = free_port()
            registry_file = root / "projects.json"
            registry_file.write_text(
                json.dumps(
                    {
                        "generation": 1,
                        "default_project_id": "project",
                        "projects": [
                            {
                                "id": "project",
                                "name": "Project",
                                "path": str(project),
                                "endpoint": f"http://127.0.0.1:{child_port}/mcp",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = safe_server_env()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coding_tools_mcp",
                    "--workspace",
                    str(project),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(child_port),
                ],
                cwd=str(project),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            gateway = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coding_tools_mcp",
                    "--workspace",
                    str(control),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(gateway_port),
                    "--project-gateway",
                    "--project-registry-only",
                    "--project-registry-file",
                    str(registry_file),
                ],
                cwd=str(control),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            processes = [child, gateway]
            try:
                for port, process in [(child_port, child), (gateway_port, gateway)]:
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        if process.poll() is not None:
                            stdout, stderr = process.communicate(timeout=1)
                            self.fail(
                                f"runtime exited early: stdout={stdout!r} stderr={stderr!r}"
                            )
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                                break
                        except OSError:
                            time.sleep(0.05)
                    else:
                        self.fail(f"runtime on port {port} did not start")

                with MCPClient(
                    control,
                    url=f"http://127.0.0.1:{gateway_port}/mcp",
                ) as client:
                    current = structured_payload(
                        client.call_tool("project_context", {"action": "current"})
                    )
                    self.assertEqual(current["project_id"], "project")
                    result = structured_payload(
                        client.call_tool("search_text", {"query": "PROXIED_PROJECT_MARKER"})
                    )
                    self.assertEqual(result["total_matches"], 1)
            finally:
                for process in processes:
                    if process.poll() is None:
                        os.killpg(process.pid, 15)
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, 9)
                            process.wait(timeout=3)
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()


if __name__ == "__main__":
    unittest.main()
