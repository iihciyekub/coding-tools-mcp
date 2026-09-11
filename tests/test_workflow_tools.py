from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_tools_mcp.lsp import LSPManager, lsp_position
from coding_tools_mcp.protocol import (
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    MODERN_PROTOCOL_VERSIONS,
    TASKS_EXTENSION,
    dispatch_rpc,
)
from coding_tools_mcp.server import Runtime, build_parser, build_runtime, runtime_policy_from_args


class WorkflowToolTests(unittest.TestCase):
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
            enable_workflow_tools=True,
            state_root=self.state_root,
            permission_mode=permission_mode,
        )

    def payload(self, runtime: Runtime, name: str, args: dict[str, object]) -> dict[str, object]:
        result = runtime.call_tool(name, args)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def text(self, runtime: Runtime, name: str, args: dict[str, object]) -> str:
        result = runtime.call_tool(name, args)
        self.assertFalse(result["isError"], result)
        return "\n".join(item.get("text", "") for item in result["content"] if item.get("type") == "text")

    def modern_rpc(
        self,
        runtime: Runtime,
        request_id: int,
        method: str,
        params: dict[str, object] | None = None,
        *,
        tasks: bool = True,
    ) -> dict[str, object]:
        capabilities: dict[str, object] = {}
        if tasks:
            capabilities["extensions"] = {TASKS_EXTENSION: {}}
        payload = dict(params or {})
        payload["_meta"] = {
            META_PROTOCOL_VERSION: MODERN_PROTOCOL_VERSIONS[0],
            META_CLIENT_CAPABILITIES: capabilities,
            META_CLIENT_INFO: {"name": "workflow-test", "version": "1"},
        }
        response = dispatch_rpc(
            runtime,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": payload},
        )
        self.assertIsNotNone(response)
        assert response is not None
        return response

    def test_workflow_tools_are_opt_in(self) -> None:
        default = Runtime(self.workspace, state_root=self.state_root)
        enhanced = self.runtime()
        try:
            self.assertNotIn("workspace_overview", default.exposed_tool_names())
            self.assertIn("workspace_overview", enhanced.exposed_tool_names())
            self.assertEqual(len(default.exposed_tool_names()), 51)
            self.assertEqual(len(enhanced.exposed_tool_names()), 104)
        finally:
            default.close()
            enhanced.close()

    def test_cli_flag_builds_enhanced_runtime_with_selected_state_root(self) -> None:
        args = build_parser().parse_args(
            [
                "--workspace",
                str(self.workspace),
                "--state-root",
                str(self.state_root),
                "--enable-workflow-tools",
            ]
        )
        runtime = build_runtime(args, runtime_policy_from_args(args), emit_warning=False)
        try:
            self.assertEqual(len(runtime.exposed_tool_names()), 104)
            self.assertEqual(runtime.workflow_store.root.parent.parent, self.state_root)
        finally:
            runtime.close()

    def test_modern_tasks_extension_is_workflow_only_and_checks_run_can_be_polled(self) -> None:
        (self.workspace / "Makefile").write_text(
            "lint:\n\t@printf 'fast-check-ok\\n'\n"
            "test:\n\t@python3 -c \"import time; time.sleep(0.35); print('task-check-ok')\"\n",
            encoding="utf-8",
        )
        default = Runtime(self.workspace, state_root=self.state_root, permission_mode="dangerous")
        enhanced = self.runtime(permission_mode="dangerous")
        try:
            default_discovery = self.modern_rpc(default, 1, "server/discover", tasks=False)["result"]
            enhanced_discovery = self.modern_rpc(enhanced, 2, "server/discover", tasks=False)["result"]
            self.assertNotIn("extensions", default_discovery["capabilities"])
            self.assertIn(TASKS_EXTENSION, enhanced_discovery["capabilities"]["extensions"])

            immediate = self.modern_rpc(
                enhanced,
                3,
                "tools/call",
                {
                    "name": "checks_run",
                    "arguments": {"check_id": "make:lint", "yield_time_ms": 5000, "timeout_ms": 5000},
                },
            )["result"]
            self.assertEqual(immediate["resultType"], "complete", immediate)
            self.assertFalse(immediate["isError"])

            started = self.modern_rpc(
                enhanced,
                4,
                "tools/call",
                {
                    "name": "checks_run",
                    "arguments": {"check_id": "make:test", "yield_time_ms": 0, "timeout_ms": 5000},
                },
            )["result"]
            self.assertEqual(started["resultType"], "task", started)
            self.assertEqual(started["status"], "working")
            task_id = str(started["taskId"])
            self.assertRegex(task_id, r"^[0-9a-f]{32}$")

            time.sleep(0.5)
            completed = self.modern_rpc(enhanced, 5, "tasks/get", {"taskId": task_id})["result"]
            self.assertEqual(completed["resultType"], "complete")
            self.assertEqual(completed["status"], "completed", completed)
            self.assertFalse(completed["result"]["isError"])
            self.assertEqual(completed["result"]["structuredContent"]["status"], "passed")

            update = self.modern_rpc(
                enhanced,
                6,
                "tasks/update",
                {"taskId": task_id, "inputResponses": {"unused": {"action": "decline"}}},
            )["result"]
            self.assertEqual(update["resultType"], "complete")
        finally:
            default.close()
            enhanced.close()

        reopened = self.runtime(permission_mode="dangerous")
        try:
            persisted = self.modern_rpc(reopened, 7, "tasks/get", {"taskId": task_id})["result"]
            self.assertEqual(persisted["status"], "completed")
            self.assertEqual(persisted["result"]["structuredContent"]["status"], "passed")
        finally:
            reopened.close()

    def test_protocol_tasks_require_capability_and_support_cooperative_cancel(self) -> None:
        (self.workspace / "Makefile").write_text(
            "test:\n\t@python3 -c \"import time; time.sleep(5)\"\n",
            encoding="utf-8",
        )
        runtime = self.runtime(permission_mode="dangerous")
        try:
            started = self.modern_rpc(
                runtime,
                1,
                "tools/call",
                {
                    "name": "checks_run",
                    "arguments": {"check_id": "make:test", "yield_time_ms": 0, "timeout_ms": 10000},
                },
            )["result"]
            task_id = str(started["taskId"])

            missing_capability = self.modern_rpc(
                runtime,
                2,
                "tasks/get",
                {"taskId": task_id},
                tasks=False,
            )
            self.assertEqual(missing_capability["error"]["code"], -32003)

            cancelled = self.modern_rpc(runtime, 3, "tasks/cancel", {"taskId": task_id})["result"]
            self.assertEqual(cancelled["resultType"], "complete")
            state = self.modern_rpc(runtime, 4, "tasks/get", {"taskId": task_id})["result"]
            self.assertEqual(state["status"], "cancelled", state)

            missing = self.modern_rpc(runtime, 5, "tasks/get", {"taskId": "0" * 32})
            self.assertEqual(missing["error"]["code"], -32602)
        finally:
            runtime.close()

    def test_browser_upload_can_reuse_a_runtime_managed_download(self) -> None:
        runtime = self.runtime()
        try:
            download_id = "a" * 24
            managed = runtime._browser_download_root() / download_id
            managed.mkdir()
            payload = managed / "payload.txt"
            payload.write_text("managed", encoding="utf-8")
            with mock.patch("coding_tools_mcp.server.browser_tools.upload") as upload:
                upload.return_value = {"ok": True, "file_count": 1, "tab": {"index": 0, "url": "about:blank"}}
                result = runtime.call_tool(
                    "browser_upload",
                    {"selector": "#file", "download_ids": [download_id]},
                )
            self.assertFalse(result["isError"], result)
            self.assertEqual(upload.call_args.args[0]["_resolved_files"], [str(payload)])
        finally:
            runtime.close()

    def test_overview_map_instructions_skills_and_checks(self) -> None:
        skills_dir = self.workspace / ".agents" / "skills" / "verify"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: verify\ndescription: Run repository checks.\n---\n\nUse the discovered checks.\n",
            encoding="utf-8",
        )
        (self.workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "node --test", "build": "tsc"}}), encoding="utf-8"
        )
        runtime = self.runtime()
        try:
            overview = self.payload(runtime, "workspace_overview", {})
            self.assertIn("Python", [item["language"] for item in overview["languages"]])
            mapping = self.payload(runtime, "repo_map", {"query": "answer"})
            self.assertEqual(mapping["symbol_count"], 1)
            instructions = self.payload(runtime, "project_instructions", {"path": "src/app.py"})
            self.assertEqual([item["path"] for item in instructions["instructions"]], ["AGENTS.md", "src/AGENTS.md"])
            self.assertIn("Source rule", self.text(runtime, "project_instructions", {"path": "src/app.py"}))
            skills = self.payload(runtime, "skills_list", {})
            self.assertEqual(skills["skills"][0]["name"], "verify")
            skill = self.payload(runtime, "skills_read", {"path": ".agents/skills/verify/SKILL.md"})
            self.assertIn("Use the discovered checks", skill["content"])
            self.assertIn("Use the discovered checks", self.text(runtime, "skills_read", {"path": ".agents/skills/verify/SKILL.md"}))
            checks = self.payload(runtime, "checks_discover", {})
            self.assertEqual({item["id"] for item in checks["checks"]}, {"npm:test", "npm:build"})
        finally:
            runtime.close()

    def test_tasks_persist_and_reject_stale_revisions(self) -> None:
        first = self.runtime()
        created = self.payload(first, "task_create", {"title": "Fix", "objective": "Fix the bug"})
        task_id = str(created["task_id"])
        updated = self.payload(
            first,
            "task_update",
            {"task_id": task_id, "expected_revision": 1, "status": "running", "details": {"next": "test"}},
        )
        self.assertEqual(updated["revision"], 2)
        planned = self.payload(
            first,
            "task_plan_update",
            {
                "task_id": task_id,
                "expected_revision": 2,
                "steps": [
                    {"step_id": "inspect", "title": "Inspect the failure", "status": "completed"},
                    {"step_id": "fix", "title": "Apply the fix", "status": "in_progress"},
                ],
            },
        )
        self.assertEqual(planned["revision"], 3)
        self.assertEqual(self.payload(first, "task_plan_get", {"task_id": task_id})["steps"], planned["steps"])
        invalid = first.call_tool(
            "task_plan_update",
            {
                "task_id": task_id,
                "expected_revision": 3,
                "steps": [
                    {"step_id": "a", "title": "A", "status": "in_progress"},
                    {"step_id": "b", "title": "B", "status": "in_progress"},
                ],
            },
        )
        self.assertTrue(invalid["isError"])
        stale = first.call_tool("task_update", {"task_id": task_id, "expected_revision": 1, "status": "completed"})
        self.assertTrue(stale["isError"])
        self.assertEqual(stale["structuredContent"]["error"]["code"], "TASK_CONFLICT")
        first.close()

        second = self.runtime()
        try:
            restored = self.payload(second, "task_get", {"task_id": task_id})
            self.assertEqual(restored["status"], "running")
            self.assertEqual(restored["details"]["next"], "test")
            self.assertEqual(len(restored["details"]["plan"]), 2)
        finally:
            second.close()

    def test_checks_run_reuses_command_manager_and_deduplication(self) -> None:
        (self.workspace / "Makefile").write_text("test:\n\t@printf 'check passed\\n'\n", encoding="utf-8")
        runtime = self.runtime(permission_mode="dangerous")
        try:
            first = self.payload(
                runtime,
                "checks_run",
                {"check_id": "make:test", "operation_id": "workflow-check-1", "yield_time_ms": 30000},
            )
            self.assertEqual(first["exit_code"], 0)
            self.assertEqual(first["check"]["id"], "make:test")
            replay = self.payload(
                runtime,
                "checks_run",
                {"check_id": "make:test", "operation_id": "workflow-check-1", "yield_time_ms": 30000},
            )
            self.assertEqual(replay["command_id"], first["command_id"])
            self.assertTrue(replay["deduplicated"])
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
            listed = self.payload(runtime, "approval_list", {"status": "pending"})
            self.assertEqual([item["approval_id"] for item in listed["approvals"]], [approval_id])

            database = sqlite3.connect(runtime.workflow_store.db_path)
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
            self.assertEqual(self.payload(runtime, "approval_get", {"approval_id": approval_id})["status"], "consumed")

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
            database = sqlite3.connect(runtime.workflow_store.db_path)
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

    def test_task_context_links_events_checks_and_checkpoints(self) -> None:
        (self.workspace / "Makefile").write_text("test:\n\t@printf 'check passed\\n'\n", encoding="utf-8")
        runtime = self.runtime(permission_mode="dangerous")
        try:
            task = self.payload(runtime, "task_create", {"title": "Verify", "objective": "Keep evidence"})
            task_id = str(task["task_id"])
            self.payload(
                runtime,
                "task_event_add",
                {"task_id": task_id, "event_type": "progress", "message": "Located the relevant module."},
            )
            check = self.payload(
                runtime,
                "checks_run",
                {"check_id": "make:test", "task_id": task_id, "yield_time_ms": 30000},
            )
            evidence = self.payload(runtime, "checks_result", {"check_run_id": check["check_run_id"]})
            self.assertEqual(evidence["status"], "passed")
            self.assertFalse(evidence["stale"])
            self.payload(
                runtime,
                "checkpoint_create",
                {"paths": ["src/app.py"], "label": "verified", "task_id": task_id},
            )
            context = self.payload(runtime, "task_context", {"task_id": task_id})
            self.assertEqual(context["latest_check_status"], "passed")
            self.assertEqual(len(context["checks"]), 1)
            self.assertEqual(len(context["checkpoints"]), 1)
            self.assertGreaterEqual(len(context["events"]), 4)
            (self.workspace / "src" / "app.py").write_text("def answer():\n    return 43\n", encoding="utf-8")
            stale = self.payload(runtime, "checks_result", {"check_run_id": check["check_run_id"]})
            self.assertTrue(stale["stale"])
        finally:
            runtime.close()

    def test_checkpoint_restore_requires_current_preview(self) -> None:
        runtime = self.runtime()
        file_path = self.workspace / "src" / "app.py"
        try:
            checkpoint = self.payload(
                runtime, "checkpoint_create", {"paths": ["src/app.py", "src/new.py"], "label": "before"}
            )
            checkpoint_id = str(checkpoint["checkpoint_id"])
            file_path.write_text("changed once\n", encoding="utf-8")
            (self.workspace / "src" / "new.py").write_text("new\n", encoding="utf-8")
            preview = self.payload(runtime, "checkpoint_diff", {"checkpoint_id": checkpoint_id})
            self.assertEqual(preview["changed_count"], 2)
            file_path.write_text("changed twice\n", encoding="utf-8")
            conflict = runtime.call_tool(
                "checkpoint_restore",
                {"checkpoint_id": checkpoint_id, "restore_token": preview["restore_token"]},
            )
            self.assertTrue(conflict["isError"])
            self.assertEqual(conflict["structuredContent"]["error"]["code"], "CHECKPOINT_CONFLICT")

            current = self.payload(runtime, "checkpoint_diff", {"checkpoint_id": checkpoint_id})
            restored = self.payload(
                runtime,
                "checkpoint_restore",
                {"checkpoint_id": checkpoint_id, "restore_token": current["restore_token"]},
            )
            self.assertEqual(restored["file_count"], 2)
            self.assertEqual(file_path.read_text(encoding="utf-8"), "def answer():\n    return 42\n")
            self.assertFalse((self.workspace / "src" / "new.py").exists())
        finally:
            runtime.close()

    def test_structured_git_write_flow_checks_head_index_and_scope(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "AGENTS.md", "src/app.py", "src/AGENTS.md"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-qm", "initial"], check=True)
        runtime = self.runtime(permission_mode="dangerous")
        try:
            state = self.payload(runtime, "git_status", {})
            branches = self.payload(runtime, "git_branch_list", {})
            self.assertEqual(branches["head"], state["head"])
            self.payload(
                runtime,
                "git_branch_create",
                {
                    "name": "feature/test",
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            file_path = self.workspace / "src" / "app.py"
            file_path.write_text("def answer():\n    return 43\n", encoding="utf-8")
            staged = self.payload(
                runtime,
                "git_stage",
                {
                    "paths": ["src/app.py"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            stale = runtime.call_tool(
                "git_unstage",
                {
                    "paths": ["src/app.py"],
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            self.assertTrue(stale["isError"])
            self.assertEqual(stale["structuredContent"]["error"]["code"], "GIT_STATE_CONFLICT")
            mismatch = runtime.call_tool(
                "git_commit",
                {
                    "paths": ["AGENTS.md"],
                    "message": "wrong scope",
                    "expected_head": staged["head"],
                    "expected_index_fingerprint": staged["index_fingerprint"],
                },
            )
            self.assertTrue(mismatch["isError"])
            self.assertEqual(mismatch["structuredContent"]["error"]["code"], "GIT_COMMIT_SCOPE_MISMATCH")
            committed = self.payload(
                runtime,
                "git_commit",
                {
                    "paths": ["src/app.py"],
                    "message": "update answer",
                    "expected_head": staged["head"],
                    "expected_index_fingerprint": staged["index_fingerprint"],
                },
            )
            self.assertNotEqual(committed["head"], state["head"])
            self.assertEqual(self.payload(runtime, "git_conflicts", {})["count"], 0)
        finally:
            runtime.close()

    def test_managed_git_worktree_create_list_and_clean_remove(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "AGENTS.md", "src/app.py", "src/AGENTS.md"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-qm", "initial"], check=True)
        runtime = self.runtime(permission_mode="dangerous")
        try:
            state = self.payload(runtime, "git_status", {})
            created = self.payload(
                runtime,
                "git_worktree_create",
                {
                    "worktree_id": "task-one",
                    "branch": "feature/worktree",
                    "expected_head": state["head"],
                    "expected_index_fingerprint": state["index_fingerprint"],
                },
            )
            worktree = Path(str(created["path"]))
            self.assertTrue(worktree.is_dir())
            self.assertFalse(worktree.is_relative_to(self.workspace))
            listed = self.payload(runtime, "git_worktree_list", {})
            managed = [item for item in listed["worktrees"] if item["managed"]]
            self.assertEqual([item["worktree_id"] for item in managed], ["task-one"])

            changed = worktree / "src" / "app.py"
            changed.write_text("dirty\n", encoding="utf-8")
            dirty = runtime.call_tool("git_worktree_remove", {"worktree_id": "task-one"})
            self.assertTrue(dirty["isError"])
            self.assertEqual(dirty["structuredContent"]["error"]["code"], "GIT_WORKTREE_DIRTY")
            subprocess.run(["git", "-C", str(worktree), "restore", "src/app.py"], check=True)
            self.payload(runtime, "git_worktree_remove", {"worktree_id": "task-one"})
            self.assertFalse(worktree.exists())
            self.assertIn(
                "feature/worktree",
                [item["name"] for item in self.payload(runtime, "git_branch_list", {})["branches"]],
            )
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
            },
        ):
            runtime = self.runtime()
            try:
                status = self.payload(runtime, "lsp_status", {})
                self.assertFalse(any(item["available"] for item in status["backends"]))
                unavailable = runtime.call_tool(
                    "lsp_definition", {"path": "src/app.py", "line": 1, "column": 5}
                )
                self.assertTrue(unavailable["isError"])
                self.assertEqual(unavailable["structuredContent"]["error"]["code"], "LSP_UNAVAILABLE")
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

    def test_lsp_protocol_returns_locations_diagnostics_and_rename_preview(self) -> None:
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
    elif method == 'textDocument/rename':
        send({'jsonrpc':'2.0','id':message['id'],'result':{'changes':{uri:[{'range':{'start':{'line':0,'character':4},'end':{'line':0,'character':10}},'newText':message['params']['newName']}]}}})
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
                definition = self.payload(
                    runtime, "lsp_definition", {"path": "src/app.py", "line": 1, "column": 5}
                )
                self.assertEqual(definition["definitions"][0]["path"], "src/app.py")
                references = self.payload(
                    runtime, "lsp_references", {"path": "src/app.py", "line": 1, "column": 5}
                )
                self.assertEqual(references["count"], 1)
                diagnostics = self.payload(runtime, "lsp_diagnostics", {"path": "src/app.py"})
                self.assertEqual(diagnostics["diagnostics"][0]["message"], "sample")
                preview = self.payload(
                    runtime,
                    "lsp_rename_preview",
                    {"path": "src/app.py", "line": 1, "column": 5, "new_name": "result"},
                )
                self.assertFalse(preview["applied"])
                self.assertEqual(preview["changes"][0]["edits"][0]["new_text"], "result")

                rust_definition = self.payload(
                    runtime,
                    "lsp_definition",
                    {"path": "rust-demo/src/lib.rs", "line": 1, "column": 8},
                )
                self.assertEqual(rust_definition["definitions"][0]["path"], "rust-demo/src/lib.rs")
                rust_references = self.payload(
                    runtime,
                    "lsp_references",
                    {"path": "rust-demo/src/lib.rs", "line": 1, "column": 8},
                )
                self.assertEqual(rust_references["count"], 1)
                rust_diagnostics = self.payload(
                    runtime, "lsp_diagnostics", {"path": "rust-demo/src/lib.rs"}
                )
                self.assertEqual(rust_diagnostics["diagnostics"][0]["message"], "sample")
                rust_preview = self.payload(
                    runtime,
                    "lsp_rename_preview",
                    {"path": "rust-demo/src/lib.rs", "line": 1, "column": 8, "new_name": "result"},
                )
                self.assertFalse(rust_preview["applied"])
                status = self.payload(runtime, "lsp_status", {})
                rust_status = next(item for item in status["backends"] if item["language"] == "rust")
                self.assertEqual(rust_status["project_roots"], ["rust-demo"])
            finally:
                runtime.close()

    def test_review_snapshot_records_findings_and_becomes_stale(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-qm", "initial"], check=True)
        (self.workspace / "src" / "app.py").write_text("def answer():\n    return 43\n", encoding="utf-8")
        runtime = self.runtime()
        try:
            task = self.payload(runtime, "task_create", {"title": "Review", "objective": "Review change"})
            review = self.payload(runtime, "review_prepare", {"task_id": task["task_id"]})
            self.assertIn("return 43", review["snapshot"]["diff"]["diff"])
            recorded = self.payload(
                runtime,
                "review_record",
                {
                    "review_id": review["review_id"],
                    "expected_revision": 1,
                    "status": "changes_requested",
                    "findings": [
                        {
                            "path": "src/app.py",
                            "line": 2,
                            "priority": 1,
                            "title": "Unexpected value",
                            "body": "Confirm the changed result.",
                        }
                    ],
                },
            )
            self.assertEqual(recorded["revision"], 2)
            self.assertEqual(recorded["finding_count"], 1)
            current = self.payload(runtime, "review_get", {"review_id": review["review_id"]})
            self.assertFalse(current["stale"])
            (self.workspace / "src" / "app.py").write_text("def answer():\n    return 44\n", encoding="utf-8")
            stale = self.payload(runtime, "review_get", {"review_id": review["review_id"]})
            self.assertTrue(stale["stale"])
            context = self.payload(runtime, "task_context", {"task_id": task["task_id"]})
            self.assertEqual(len(context["reviews"]), 1)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
