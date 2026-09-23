from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_tools_mcp.lsp import LSPManager, lsp_position
from coding_tools_mcp.server import Runtime


class ExtendedToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.state_root = root / "state"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text("# Root rule\n", encoding="utf-8")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "AGENTS.md").write_text("# Source rule\n", encoding="utf-8")
        (self.workspace / "src" / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def runtime(self, *, permission_mode: str = "safe") -> Runtime:
        return Runtime(
            self.workspace,
            state_root=self.state_root,
            permission_mode=permission_mode,
        )

    def result(self, runtime: Runtime, name: str, args: dict[str, object]) -> dict[str, object]:
        return runtime.call_tool(name, args)

    def payload(self, runtime: Runtime, name: str, args: dict[str, object]) -> dict[str, object]:
        result = self.result(runtime, name, args)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def text(self, runtime: Runtime, name: str, args: dict[str, object]) -> str:
        result = self.result(runtime, name, args)
        self.assertFalse(result["isError"], result)
        return "\n".join(item.get("text", "") for item in result["content"] if item.get("type") == "text")

    def test_default_search_folder_changes_only_omitted_search_paths(self) -> None:
        external = Path(self.temp.name) / "shared"
        external.mkdir()
        (external / "outside.txt").write_text("external-marker\n", encoding="utf-8")
        runtime = Runtime(
            self.workspace,
            state_root=self.state_root,
            permission_mode="trusted",
            file_access_roots=(external,),
            default_search_path=str(external),
        )
        try:
            listed = self.payload(runtime, "list_files", {})
            self.assertEqual([Path(item["path"]).name for item in listed["files"]], ["outside.txt"])
            found = self.payload(runtime, "search_text", {"query": "external-marker"})
            self.assertEqual(len(found["matches"]), 1)
            project = self.payload(runtime, "search_text", {"query": "def answer", "path": "."})
            self.assertEqual(len(project["matches"]), 1)
            not_in_project = self.payload(runtime, "search_text", {"query": "external-marker", "path": "."})
            self.assertEqual(not_in_project["matches"], [])
            with mock.patch.object(runtime, "_list_files_with_fd", return_value=None), mock.patch(
                "coding_tools_mcp.server.time.monotonic", side_effect=[100, 111]
            ):
                timed_out = runtime.list_files({})
            self.assertTrue(timed_out["truncated"])
            self.assertIn("time limit", timed_out["warnings"][0])
            with mock.patch.object(runtime, "_search_text_with_rg", return_value=None), mock.patch(
                "coding_tools_mcp.server.time.monotonic", side_effect=[100, 111]
            ):
                timed_out_search = runtime.search_text({"query": "external-marker"})
            self.assertTrue(timed_out_search["truncated"])
            self.assertIn("time limit", timed_out_search["warnings"][0])
        finally:
            runtime.close()







    def test_overview_instructions_and_checks(self) -> None:
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node --test", "build": "tsc"}}), encoding="utf-8"
        )
        runtime = self.runtime()
        try:
            overview = self.payload(runtime, "workspace_overview", {})
            self.assertIn("Python", [item["language"] for item in overview["languages"]])
            instructions = self.payload(runtime, "project_instructions", {"path": "src/app.py"})
            self.assertEqual([item["path"] for item in instructions["instructions"]], ["AGENTS.md", "src/AGENTS.md"])
            self.assertIn("Source rule", self.text(runtime, "project_instructions", {"path": "src/app.py"}))
            checks = self.payload(runtime, "checks_discover", {})
            self.assertEqual({item["id"] for item in checks["checks"]}, {"npm:test", "npm:build"})
        finally:
            runtime.close()

    def test_apple_workspace_metadata_and_checks_are_cli_first(self) -> None:
        (self.workspace / "Package.swift").write_text("// swift-tools-version: 6.0\n", encoding="utf-8")
        swift_source = self.workspace / "Sources" / "Demo" / "App.swift"
        swift_source.parent.mkdir(parents=True)
        swift_source.write_text("public func answer() -> Int { 42 }\n", encoding="utf-8")
        xcodeproj = self.workspace / "Demo.xcodeproj"
        xcodeproj.mkdir()
        (xcodeproj / "project.pbxproj").write_text("// fixture\n", encoding="utf-8")
        runtime = self.runtime()
        try:
            with mock.patch(
                "coding_tools_mcp.workspace_insight.apple_toolchain.probe",
                return_value={
                    "platform": "macos",
                    "is_macos": True,
                    "xcode_available": True,
                    "xcode_version": "Xcode 18.0",
                    "swift_available": True,
                    "swift_version": "Swift 6.2",
                    "sourcekit_lsp": "/usr/bin/sourcekit-lsp",
                    "sdk_summary": ["macOS 26.0 -sdk macosx26.0"],
                },
            ), mock.patch(
                "coding_tools_mcp.workspace_insight.shutil.which",
                side_effect=lambda name, path=None: (
                    "/usr/bin/xcodebuild"
                    if name == "xcodebuild"
                    else "/usr/bin/git"
                    if name == "git"
                    else None
                ),
            ):
                overview = self.payload(runtime, "workspace_overview", {})
                checks = self.payload(runtime, "checks_discover", {})
            self.assertTrue(overview["apple"]["detected"])
            self.assertEqual(overview["apple"]["xcodeproj"], ["Demo.xcodeproj"])
            self.assertEqual(overview["apple"]["swift_packages"], ["Package.swift"])
            self.assertEqual(overview["apple"]["xcode_version"], "Xcode 18.0")
            check_ids = {item["id"] for item in checks["checks"]}
            self.assertIn("swift:build", check_ids)
            self.assertIn("swift:test", check_ids)
            self.assertIn("xcode:list", check_ids)
            xcode_check = next(item for item in checks["checks"] if item["id"] == "xcode:list")
            self.assertIn("-project Demo.xcodeproj -list -json", xcode_check["command"])
        finally:
            runtime.close()

    def test_checks_discover_recommends_existing_checks_from_git_changes(self) -> None:
        (self.workspace / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = '-q'\n[tool.ruff]\nline-length = 100\n[tool.mypy]\npython_version = '3.11'\n",
            encoding="utf-8",
        )
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node --test", "lint": "eslint ."}}),
            encoding="utf-8",
        )
        (self.workspace / "README.md").write_text("# Fixture\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.workspace, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.workspace, check=True)
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.workspace, check=True)

        runtime = self.runtime()
        try:
            (self.workspace / "src" / "app.py").write_text(
                "def answer():\n    return 43\n",
                encoding="utf-8",
            )
            discovered = self.payload(runtime, "checks_discover", {})
            self.assertEqual(discovered["recommendation_source"], "git_status")
            self.assertEqual(discovered["changed_paths"], ["src/app.py"])
            self.assertEqual(
                discovered["recommended_check_ids"],
                ["python:mypy", "python:ruff", "python:pytest"],
            )
            checks_by_id = {item["id"]: item for item in discovered["checks"]}
            self.assertTrue(checks_by_id["python:mypy"]["recommended"])
            self.assertEqual(checks_by_id["python:mypy"]["priority"], "medium")
            self.assertFalse(checks_by_id["npm:test"]["recommended"])
            self.assertTrue(checks_by_id["python:mypy"]["recommendation_reasons"])

            subprocess.run(["git", "checkout", "--", "src/app.py"], cwd=self.workspace, check=True)
            (self.workspace / "README.md").write_text("# Fixture\nDocs only.\n", encoding="utf-8")
            docs_only = self.payload(runtime, "checks_discover", {})
            self.assertEqual(docs_only["changed_paths"], ["README.md"])
            self.assertEqual(docs_only["recommended_check_ids"], [])

            explicit = self.payload(
                runtime,
                "checks_discover",
                {"changed_paths": ["package.json"]},
            )
            self.assertEqual(explicit["recommendation_source"], "explicit")
            self.assertEqual(explicit["recommended_check_ids"], ["npm:lint", "npm:test"])
        finally:
            runtime.close()





    def test_operator_approval_is_exact_one_shot_and_persistent(self) -> None:
        runtime = self.runtime()
        command_args = {"cmd": "echo $(pwd)"}
        try:
            denied = runtime.call_tool("exec_command", command_args)
            self.assertTrue(denied["isError"])
            self.assertEqual(denied["structuredContent"]["error"]["code"], "PERMISSION_REQUIRED")

            requested = self.payload(
                runtime,
                "request_permissions",
                {
                    "tool_name": "exec_command",
                    "permission": "shell_expansion",
                    "reason": "Print the selected workspace path.",
                    "arguments": command_args,
                    "ttl_seconds": 60,
                },
            )
            approval_id = str(requested["approval_id"])
            self.assertEqual(requested["status"], "pending")
            listed = runtime.runtime_state.list_approvals(status="pending")
            self.assertEqual([item["approval_id"] for item in listed["approvals"]], [approval_id])

            database = sqlite3.connect(runtime.runtime_state.db_path)
            try:
                database.execute(
                    "UPDATE approvals SET status='approved', decided_at=strftime('%s','now') WHERE approval_id=?",
                    (approval_id,),
                )
                database.commit()
            finally:
                database.close()

            executed = self.payload(runtime, "exec_command", {**command_args, "approval_ids": [approval_id]})
            self.assertEqual(executed["exit_code"], 0)
            self.assertEqual(runtime.runtime_state.get_approval(approval_id)["status"], "consumed")

            replay = runtime.call_tool("exec_command", {**command_args, "approval_ids": [approval_id]})
            self.assertTrue(replay["isError"])
            self.assertEqual(replay["structuredContent"]["error"]["code"], "APPROVAL_NOT_USABLE")

            mismatch = self.payload(
                runtime,
                "request_permissions",
                {
                    "tool_name": "exec_command",
                    "permission": "shell_expansion",
                    "reason": "Test exact argument binding.",
                    "arguments": command_args,
                },
            )
            mismatch_id = str(mismatch["approval_id"])
            database = sqlite3.connect(runtime.runtime_state.db_path)
            try:
                database.execute("UPDATE approvals SET status='approved' WHERE approval_id=?", (mismatch_id,))
                database.commit()
            finally:
                database.close()
            rejected = runtime.call_tool(
                "exec_command", {"cmd": "printf '%s' $(pwd)", "approval_ids": [mismatch_id]}
            )
            self.assertTrue(rejected["isError"])
            self.assertEqual(rejected["structuredContent"]["error"]["code"], "APPROVAL_SCOPE_MISMATCH")
        finally:
            runtime.close()

    def test_approved_operation_id_replay_does_not_reconsume_approval(self) -> None:
        runtime = self.runtime()
        command_args = {
            "cmd": "echo $(pwd)",
            "operation_id": "approved-operation-replay",
        }
        try:
            requested = self.payload(
                runtime,
                "request_permissions",
                {
                    "tool_name": "exec_command",
                    "permission": "shell_expansion",
                    "reason": "Exercise recoverable approved execution.",
                    "arguments": command_args,
                },
            )
            approval_id = str(requested["approval_id"])
            database = sqlite3.connect(runtime.runtime_state.db_path)
            try:
                database.execute(
                    "UPDATE approvals SET status='approved', decided_at=strftime('%s','now') WHERE approval_id=?",
                    (approval_id,),
                )
                database.commit()
            finally:
                database.close()

            first = self.payload(
                runtime,
                "exec_command",
                {**command_args, "approval_ids": [approval_id]},
            )
            replay = self.payload(
                runtime,
                "exec_command",
                {**command_args, "approval_ids": [approval_id]},
            )
            self.assertEqual(replay["command_id"], first["command_id"])
            self.assertTrue(replay["deduplicated"])
            self.assertEqual(runtime.runtime_state.get_approval(approval_id)["status"], "consumed")
        finally:
            runtime.close()

    def test_host_network_deny_requests_operator_approval_instead_of_false_grant(self) -> None:
        runtime = Runtime(
            self.workspace,
            state_root=self.state_root,
            permission_mode="host",
            network_policy="deny",
        )
        try:
            requested = self.payload(
                runtime,
                "request_permissions",
                {
                    "tool_name": "exec_command",
                    "permission": "network",
                    "reason": "Access a network target under deny-by-default policy.",
                    "arguments": {"cmd": "curl https://example.com"},
                },
            )
            self.assertEqual(requested["status"], "pending")
            self.assertIsInstance(requested.get("approval_id"), str)
            self.assertNotIn("grant_id", requested)
        finally:
            runtime.close()





    def test_lsp_status_degrades_cleanly_and_positions_are_utf16(self) -> None:
        emoji = self.workspace / "src" / "emoji.py"
        emoji.write_text("value = '😀'; answer = value\n", encoding="utf-8")
        self.assertEqual(lsp_position(emoji, 1, 14), {"line": 0, "character": 14})
        with mock.patch.dict(
            "os.environ",
            {
                "CODING_TOOLS_MCP_PYTHON_LSP_COMMAND": "/missing/python-lsp",
                "CODING_TOOLS_MCP_TYPESCRIPT_LSP_COMMAND": "/missing/typescript-lsp",
                "CODING_TOOLS_MCP_RUST_LSP_COMMAND": "/missing/rust-lsp",
                "CODING_TOOLS_MCP_SWIFT_LSP_COMMAND": "/missing/swift-lsp",
            },
        ):
            runtime = self.runtime()
            try:
                status = runtime.runtime_doctor({})["lsp"]
                self.assertFalse(any(item["available"] for item in status["backends"]))
                fallback = self.payload(
                    runtime,
                    "code_definition",
                    {"symbol": "answer", "path": "src/app.py", "line": 1, "column": 5},
                )
                self.assertEqual(fallback["backend"], "syntax")
                self.assertTrue(fallback["fallback_used"])
                self.assertEqual(fallback["fallback_reason"], "LSP_UNAVAILABLE")
                self.assertEqual(fallback["definitions"][0]["path"], "src/app.py")
            finally:
                runtime.close()

    def test_rust_lsp_uses_nearest_cargo_project_root(self) -> None:
        crate = self.workspace / "apps" / "desktop"
        source = crate / "src" / "lib.rs"
        source.parent.mkdir(parents=True)
        (crate / "Cargo.toml").write_text("[package]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
        source.write_text("pub fn answer() -> i32 { 42 }\n", encoding="utf-8")
        manager = LSPManager(self.workspace, {"PATH": ""})
        try:
            self.assertEqual(manager.project_root_for(source, "rust"), crate.resolve())
        finally:
            manager.close()

    def test_swift_lsp_uses_package_root_and_xcrun_discovery(self) -> None:
        package = self.workspace / "swift-demo"
        source = package / "Sources" / "Demo" / "App.swift"
        source.parent.mkdir(parents=True)
        (package / "Package.swift").write_text("// swift-tools-version: 6.0\n", encoding="utf-8")
        source.write_text("public func answer() -> Int { 42 }\n", encoding="utf-8")
        manager = LSPManager(self.workspace, {"PATH": "/usr/bin:/bin"})
        fake_sourcekit = Path(self.temp.name) / "sourcekit-lsp"
        fake_sourcekit.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_sourcekit.chmod(0o755)
        try:
            self.assertEqual(manager.project_root_for(source, "swift"), package.resolve())
            with mock.patch("coding_tools_mcp.lsp.shutil.which") as which, mock.patch(
                "coding_tools_mcp.lsp.subprocess.run"
            ) as run:
                which.side_effect = lambda name, path=None: "/usr/bin/xcrun" if name == "xcrun" else None
                run.return_value = subprocess.CompletedProcess(
                    args=["xcrun", "--find", "sourcekit-lsp"],
                    returncode=0,
                    stdout=f"{fake_sourcekit}\n",
                    stderr="",
                )
                self.assertEqual(manager._command("swift"), [str(fake_sourcekit)])
        finally:
            manager.close()

    def test_lsp_protocol_returns_locations_and_diagnostics(self) -> None:
        fake = Path(self.temp.name) / "fake_lsp.py"
        fake.write_text(
            """
import json, sys
uri = None
def send(value):
    raw = json.dumps(value, separators=(',', ':')).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode() + raw)
    sys.stdout.buffer.flush()
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise SystemExit
        if line in (b'\\r\\n', b'\\n'):
            break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    method = message.get('method')
    if method == 'initialize':
        send({'jsonrpc':'2.0','id':message['id'],'result':{'capabilities':{}}})
    elif method == 'textDocument/didOpen':
        uri = message['params']['textDocument']['uri']
        send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{'uri':uri,'diagnostics':[{'range':{'start':{'line':0,'character':0},'end':{'line':0,'character':3}},'severity':2,'source':'fake','message':'sample'}]}})
    elif method == 'textDocument/definition':
        send({'jsonrpc':'2.0','id':message['id'],'result':[{'uri':uri,'range':{'start':{'line':0,'character':4},'end':{'line':0,'character':10}}}]})
    elif method == 'textDocument/references':
        send({'jsonrpc':'2.0','id':message['id'],'result':[{'uri':uri,'range':{'start':{'line':1,'character':4},'end':{'line':1,'character':10}}}]})
    elif method == 'shutdown':
        send({'jsonrpc':'2.0','id':message['id'],'result':None})
    elif method == 'exit':
        raise SystemExit
""".lstrip(),
            encoding="utf-8",
        )
        command = f"{sys.executable} {fake}"
        rust_crate = self.workspace / "rust-demo"
        rust_source = rust_crate / "src" / "lib.rs"
        rust_source.parent.mkdir(parents=True)
        (rust_crate / "Cargo.toml").write_text(
            "[package]\nname='rust-demo'\nversion='0.1.0'\nedition='2021'\n",
            encoding="utf-8",
        )
        rust_source.write_text("pub fn answer() -> i32 { 42 }\n", encoding="utf-8")
        with mock.patch.dict(
            "os.environ",
            {
                "CODING_TOOLS_MCP_PYTHON_LSP_COMMAND": command,
                "CODING_TOOLS_MCP_RUST_LSP_COMMAND": command,
            },
        ):
            runtime = self.runtime()
            try:
                unified_definition = self.payload(
                    runtime,
                    "code_definition",
                    {"symbol": "answer", "path": "src/app.py", "line": 1, "column": 5},
                )
                self.assertEqual(unified_definition["backend"], "lsp")
                self.assertFalse(unified_definition["fallback_used"])
                self.assertEqual(unified_definition["definitions"][0]["path"], "src/app.py")
                unified_references = self.payload(
                    runtime,
                    "code_references",
                    {"symbol": "answer", "path": "src/app.py", "line": 1, "column": 5},
                )
                self.assertEqual(unified_references["backend"], "lsp")
                self.assertFalse(unified_references["fallback_used"])
                self.assertEqual(unified_references["references"][0]["path"], "src/app.py")
                diagnostics = self.payload(runtime, "code_diagnostics", {"path": "src/app.py"})
                self.assertEqual(diagnostics["diagnostics"][0]["message"], "sample")
                rust_diagnostics = self.payload(
                    runtime, "code_diagnostics", {"path": "rust-demo/src/lib.rs"}
                )
                self.assertEqual(rust_diagnostics["diagnostics"][0]["message"], "sample")
                status = runtime.runtime_doctor({})["lsp"]
                rust_status = next(item for item in status["backends"] if item["language"] == "rust")
                self.assertEqual(rust_status["project_roots"], ["rust-demo"])
            finally:
                runtime.close()



if __name__ == "__main__":
    unittest.main()
