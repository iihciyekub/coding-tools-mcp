from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from coding_tools_mcp.codex_bridge import (
    CodexAppServerClient,
    discover_codex_executable,
    prepare_isolated_codex_home,
    probe_codex_computer,
    sanitized_broker_env,
)
from coding_tools_mcp.errors import ToolFailure


FAKE_APP_SERVER = r'''\
import json
import sys

for line in sys.stdin:
    try:
        message = json.loads(line)
    except Exception:
        continue
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        print(json.dumps({"id": request_id, "result": {"codexHome": "/fake"}}), flush=True)
    elif method == "thread/start":
        print(json.dumps({"id": request_id, "result": {"thread": {"id": "thread-1", "turns": []}}}), flush=True)
        print(json.dumps({"method": "mcpServer/startupStatus/updated", "params": {"threadId": "thread-1", "name": "node_repl", "status": "ready"}}), flush=True)
    elif method == "mcpServer/tool/call":
        print(json.dumps({"method": "mcpServer/elicitation/request", "id": 90, "params": {"message": "Allow fixture?"}}), flush=True)
        approval = json.loads(sys.stdin.readline())
        if approval.get("id") != 90 or approval.get("result", {}).get("action") != "accept":
            print(json.dumps({"id": request_id, "error": {"code": -1, "message": "approval missing"}}), flush=True)
        else:
            content = [{"type": "text", "text": "[]"}]
            print(json.dumps({"id": request_id, "result": {"content": content, "isError": False}}), flush=True)
    elif method == "thread/read":
        print(json.dumps({"id": request_id, "result": {"thread": {"id": "thread-1", "turns": []}}}), flush=True)
'''


class CodexBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / "fake_app_server.py"
        self.server.write_text(textwrap.dedent(FAKE_APP_SERVER), encoding="utf-8")

    def make_client(self, *, handler=None) -> CodexAppServerClient:
        client = CodexAppServerClient(
            Path(sys.executable),
            cwd=self.root,
            codex_home=self.root,
            request_handler=handler,
            app_server_args=("-u", str(self.server)),
            env={"HOME": str(self.root), "PATH": os.environ.get("PATH", "")},
        )
        self.addCleanup(client.close)
        return client

    def test_model_rpc_is_rejected_before_process_start(self) -> None:
        client = self.make_client()
        with self.assertRaises(ToolFailure) as caught:
            client.call("turn/start", {"input": []})
        self.assertEqual(caught.exception.code, "CODEX_MODEL_TURN_FORBIDDEN")
        self.assertIsNone(client.process)

        with self.assertRaises(ToolFailure) as caught:
            client.call("thread/compact/start", {"threadId": "t"})
        self.assertEqual(caught.exception.code, "CODEX_MODEL_TURN_FORBIDDEN")
        self.assertIsNone(client.process)

    def test_unknown_rpc_is_fail_closed(self) -> None:
        client = self.make_client()
        with self.assertRaises(ToolFailure) as caught:
            client.call("account/read", {})
        self.assertEqual(caught.exception.code, "CODEX_RPC_FORBIDDEN")
        self.assertIsNone(client.process)

    def test_bidirectional_server_request_is_handled_without_model_turn(self) -> None:
        seen: list[tuple[str, dict]] = []

        def handler(method: str, params: dict) -> dict:
            seen.append((method, params))
            return {"action": "accept", "content": {}, "_meta": {"persist": "session"}}

        client = self.make_client(handler=handler)
        self.assertIn("codexHome", client.call("initialize", {"clientInfo": {"name": "test", "version": "1"}}))
        thread = client.call("thread/start", {"ephemeral": True})["thread"]
        self.assertEqual(thread["turns"], [])
        ready = client.wait_notification("mcpServer/startupStatus/updated")
        self.assertEqual(ready["params"]["name"], "node_repl")
        result = client.call("mcpServer/tool/call", {
            "server": "node_repl",
            "threadId": thread["id"],
            "tool": "js",
            "arguments": {"code": "1"},
        })
        self.assertFalse(result["isError"])
        self.assertEqual(seen[0][0], "mcpServer/elicitation/request")
        self.assertEqual(client.call("thread/read", {"threadId": thread["id"]})["thread"]["turns"], [])

    def test_isolated_home_contains_only_sanitized_node_repl(self) -> None:
        source = self.root / ".codex-source"
        target = self.root / ".codex-isolated"
        source.mkdir()
        resources = self.root / "ChatGPT.app/Contents/Resources"
        node_bin = resources / "cua_node/bin"
        node_modules = resources / "cua_node/lib/node_modules"
        node_bin.mkdir(parents=True)
        node_modules.mkdir(parents=True)
        node_repl = node_bin / "node_repl"
        node = node_bin / "node"
        executable = resources / "codex"
        for path in (node_repl, node, executable):
            path.write_text("fixture", encoding="utf-8")
        service = source / "computer-use/Codex Computer Use.app"
        service.mkdir(parents=True)
        (source / "config.toml").write_text(
            f"""
[plugins."computer-use@openai-bundled"]
enabled = true
[plugins."wosaide-next@wosaide-app"]
enabled = true
[mcp_servers.node_repl]
command = {json.dumps(str(node_repl))}
args = []
startup_timeout_sec = 120
[mcp_servers.node_repl.env]
NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS = "1000"
NODE_REPL_NODE_MODULE_DIRS = {json.dumps(str(node_modules))}
NODE_REPL_NODE_PATH = {json.dumps(str(node))}
NODE_REPL_TRUSTED_CODE_PATHS = "/Users/example/.codex:/old/modules"
CODEX_HOME = "/Users/example/.codex"
NODE_REPL_TRUSTED_SERVICES = '{{"browser":"/secret/browser.mjs","sky":"@oai/sky/service"}}'
SKY_CUA_SERVICE_PATH = {json.dumps(str(service))}
CODEX_CLI_PATH = "/old/codex"
[mcp_servers.other]
command = "other"
""".strip(),
            encoding="utf-8",
        )
        isolated_config = prepare_isolated_codex_home(source, target, executable)
        isolated = tomllib.loads(isolated_config.read_text(encoding="utf-8"))
        self.assertEqual(set(isolated), {"mcp_servers"})
        self.assertEqual(set(isolated["mcp_servers"]), {"node_repl"})
        node = isolated["mcp_servers"]["node_repl"]
        self.assertEqual(node["command"], str(node_repl.resolve()))
        env = node["env"]
        self.assertEqual(env["CODEX_HOME"], str(target))
        self.assertEqual(env["CODEX_CLI_PATH"], str(executable))
        self.assertEqual(env["NODE_REPL_TRUSTED_SERVICES"], '{"sky":"@oai/sky/service"}')
        self.assertNotIn("/Users/example/.codex", env["NODE_REPL_TRUSTED_CODE_PATHS"])
        self.assertIn(str(target), env["NODE_REPL_TRUSTED_CODE_PATHS"])
        self.assertNotIn("browser", env["NODE_REPL_TRUSTED_SERVICES"])

    def test_missing_node_repl_fails_isolation_setup(self) -> None:
        source = self.root / ".codex-missing"
        target = self.root / ".codex-target"
        source.mkdir()
        (source / "config.toml").write_text("[plugins.foo]\nenabled=true\n", encoding="utf-8")
        with self.assertRaises(ToolFailure) as caught:
            prepare_isolated_codex_home(source, target, Path("/tmp/codex"))
        self.assertEqual(caught.exception.code, "CODEX_CAPABILITY_UNAVAILABLE")

    def test_broker_environment_drops_ctm_transport_secrets(self) -> None:
        env = sanitized_broker_env({
            "HOME": "/tmp/home",
            "PATH": "/bin",
            "CODING_TOOLS_MCP_AUTH_TOKEN": "secret",
            "CODING_TOOLS_MCP_OAUTH_PASSWORD": "secret",
            "CODING_TOOLS_MCP_SERVER_URL": "https://secret.example",
            "UNRELATED": "ok",
        })
        self.assertEqual(env["UNRELATED"], "ok")
        self.assertNotIn("CODING_TOOLS_MCP_AUTH_TOKEN", env)
        self.assertNotIn("CODING_TOOLS_MCP_OAUTH_PASSWORD", env)
        self.assertNotIn("CODING_TOOLS_MCP_SERVER_URL", env)

    def test_client_forces_requested_codex_home(self) -> None:
        client = CodexAppServerClient(
            Path(sys.executable),
            cwd=self.root,
            codex_home=self.root / "isolated",
            app_server_args=("-u", str(self.server)),
            env={"HOME": str(self.root), "CODEX_HOME": "/wrong"},
        )
        self.addCleanup(client.close)
        self.assertEqual(client.env["CODEX_HOME"], str(self.root / "isolated"))

    @unittest.skipUnless(
        sys.platform == "darwin" and os.environ.get("CODING_TOOLS_MCP_REAL_CODEX_BRIDGE_SMOKE") == "1",
        "set CODING_TOOLS_MCP_REAL_CODEX_BRIDGE_SMOKE=1 on macOS to exercise the installed Codex broker",
    )
    def test_real_codex_computer_probe_is_read_only_and_model_free(self) -> None:
        executable = discover_codex_executable()
        self.assertIsNotNone(executable)
        result = probe_codex_computer(self.root, executable=executable, timeout=30)
        self.assertTrue(result.available, result)
        self.assertTrue(result.compatible, result)
        self.assertEqual(result.started_servers, ("node_repl",))
        self.assertEqual(result.model_turns_created, 0)
        self.assertIsNotNone(result.schema_hash)
        self.assertIsNotNone(result.app_count)


if __name__ == "__main__":
    unittest.main()

