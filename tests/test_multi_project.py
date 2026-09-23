from __future__ import annotations

import concurrent.futures
import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from coding_tools_mcp.project_context import ProjectContext
from coding_tools_mcp.lsp import LanguageServer, LSPManager
from coding_tools_mcp.repositories import git_environment
from coding_tools_mcp.server import Runtime
from coding_tools_mcp.workspace_insight import workspace_fingerprint


class MultiProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.a = self.make_repo("project-a")
        self.b = self.make_repo("project-b")
        self.runtime = Runtime(
            self.workspace, permission_mode="dangerous",
            state_root=self.base / "state", project_context=ProjectContext((), (), ("Startup scan truncated",)),
        )
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.runtime.close)

    def git(self, directory: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), *arguments], check=True, capture_output=True,
            text=True, env=git_environment(dict(os.environ)), timeout=20,
        ).stdout

    def make_repo(self, name: str) -> Path:
        path = self.workspace / name
        path.mkdir()
        self.git(path, "init", "-q")
        self.git(path, "config", "user.name", "Multi-project fixture")
        self.git(path, "config", "user.email", "fixture@example.invalid")
        self.git(path, "config", "commit.gpgsign", "false")
        self.git(path, "config", "core.hooksPath", str(self.base / "no-hooks"))
        (path / "src").mkdir()
        (path / "src/same.py").write_text("value = 1\n", encoding="utf-8")
        (path / "AGENTS.md").write_text(f"Rules for {name}\n", encoding="utf-8")
        self.git(path, "add", ".")
        self.git(path, "commit", "-qm", f"Initial {name}")
        return path

    def call(self, name: str, arguments: dict | None = None) -> dict:
        args = arguments or {}
        result = self.result(name, args)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def result(self, name: str, arguments: dict | None = None) -> dict:
        args = arguments or {}
        return self.runtime.call_tool(name, args)

    def model_text(self, name: str, arguments: dict | None = None) -> str:
        result = self.result(name, arguments)
        self.assertFalse(result["isError"], result)
        return "\n".join(
            item["text"]
            for item in result.get("content", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )


    def test_read_tools_resolve_independent_repositories_and_deleted_paths(self) -> None:
        for name, root in (("project-a", self.a), ("project-b", self.b)):
            (root / "src/same.py").write_text(f"value = '{name}'\n", encoding="utf-8")
            status = self.call("git_status", {"path": name})
            self.assertEqual(status["repo_root"], str(root))
            self.assertEqual(status["entries"][0]["path"], "src/same.py")
            diff = self.call("git_diff", {"path": name})
            self.assertIn(f"+value = '{name}'", self.model_text("git_diff", {"path": name}))
            self.assertEqual(diff["diff_source"], "git")
            log = self.runtime.git_log({"path": name})
            self.assertEqual(log["commits"][0]["subject"], f"Initial {name}")
            shown = self.runtime.git_show({"repo_path": name})
            self.assertIn(f"Initial {name}", shown["content"])
            blame = self.runtime.git_blame({"path": f"{name}/src/same.py", "max_lines": 1})
            self.assertEqual(blame["repo_root"], str(root))
        (self.a / "src/same.py").unlink()
        self.assertIn("-value = 1", self.model_text("git_diff", {"path": "project-a/src/same.py"}))

    def test_nested_repo_does_not_use_parent_repo(self) -> None:
        self.git(self.workspace, "init", "-q")
        status = self.call("git_status", {"path": "project-b"})
        self.assertEqual(status["repo_root"], str(self.b))
        self.assertEqual(status["head"], self.git(self.b, "rev-parse", "HEAD").strip())
        self.assertEqual(self.runtime.git_log({"path": "project-b"})["commits"][0]["subject"], "Initial project-b")




    def test_linked_worktree_under_workspace_has_own_identity(self) -> None:
        linked = self.workspace / "linked"
        self.git(self.a, "worktree", "add", "-b", "fixture-linked", str(linked))
        self.assertTrue((linked / ".git").is_file())
        status = self.call("git_status", {"path": "linked"})
        self.assertEqual(status["repo_root"], str(linked))
        self.assertNotEqual(status["index_fingerprint"], self.call("git_status", {"path": "project-a"})["index_fingerprint"])
        (linked / "src/same.py").write_text("value = 9\n", encoding="utf-8")
        self.assertIn("+value = 9", self.model_text("git_diff", {"path": "linked"}))
        self.assertTrue(self.call("git_status", {"path": "project-a"})["clean"])


    def test_status_preserves_special_filenames_and_rename(self) -> None:
        names = ["space name.txt", "arrow -> name.txt", "中文.txt", "file[1].txt"]
        if os.name != "nt":
            names.extend(["line\nbreak.txt", "literal*.txt"])
        for name in names:
            (self.a / name).write_text("fixture", encoding="utf-8")
        entries = self.call("git_status", {"path": "project-a"})["entries"]
        self.assertEqual({entry["path"] for entry in entries}, set(names))
        self.assertTrue(all(entry["original_path"] is None for entry in entries))
        self.git(self.a, "mv", "src/same.py", "src/renamed.py")
        entries = self.call("git_status", {"path": "project-a"})["entries"]
        rename = next(entry for entry in entries if entry["path"] == "src/renamed.py")
        self.assertEqual(rename["original_path"], "src/same.py")


    def test_non_git_fallback_is_explicit_and_explicit_repo_never_falls_back(self) -> None:
        self.assertFalse(self.call("git_status")["is_repo"])
        self.assertEqual(self.call("git_status", {"repo_path": "project-a"})["repo_root"], str(self.a))
        self.assertEqual(self.call("git_status", {"repo_path": "project-b"})["repo_root"], str(self.b))
        dotted = self.call("git_status", {"repo_path": "project-a", "path": "."})
        self.assertEqual(dotted["repo_root"], str(self.a))
        self.assertEqual(self.call("git_diff")["diff_source"], "patch_baseline")
        self.assertFalse(self.call("git_diff")["is_repo"])
        (self.workspace / "not-a-repo").mkdir()
        result = self.runtime.call_tool("git_diff", {"repo_path": "not-a-repo"})
        self.assertEqual(result["structuredContent"]["error"]["code"], "GIT_NOT_REPOSITORY")

    def test_outside_workspace_symlink_cannot_select_repo(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.git(outside, "init", "-q")
        try:
            (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        result = self.runtime.call_tool("git_status", {"repo_path": "escape"})
        self.assertTrue(result["isError"])

    def test_project_rules_are_live_even_with_empty_startup_index(self) -> None:
        (self.workspace / "AGENTS.md").write_text("Parent rule", encoding="utf-8")
        rules = self.call("project_instructions", {"path": "project-a/src/same.py"})
        self.assertEqual([item["path"] for item in rules["instructions"]], ["AGENTS.md", "project-a/AGENTS.md"])
        self.assertNotIn("project-b", str(rules["instructions"]))
        (self.a / "src/AGENTS.md").write_text("New nested rule", encoding="utf-8")
        (self.a / "AGENTS.md").write_text("Changed rule", encoding="utf-8")
        rules = self.call("project_instructions", {"path": "project-a/src/same.py"})
        self.assertEqual(rules["count"], 3)
        self.assertIn("Changed rule", str(rules))
        self.assertIn("New nested rule", str(rules))

    def test_fingerprint_and_overview_only_scan_requested_project(self) -> None:
        for index in range(15):
            (self.b / f"noise-{index}.txt").write_text("noise", encoding="utf-8")
        first = workspace_fingerprint(self.workspace, self.a, max_files=3)
        self.assertTrue(first["scan_complete"])
        (self.b / "src/same.py").write_text("unrelated change", encoding="utf-8")
        self.assertEqual(first["fingerprint"], workspace_fingerprint(self.workspace, self.a, max_files=3)["fingerprint"])
        overview = self.call("workspace_overview", {"path": "project-a", "max_files": 3})
        self.assertTrue(overview["scan_complete"])
        self.assertEqual(overview["path"], "project-a")
        self.assertNotIn("project-b", str(overview["manifests"]))
        (self.a / "src/same.py").write_text("changed", encoding="utf-8")
        self.assertNotEqual(first["fingerprint"], workspace_fingerprint(self.workspace, self.a)["fingerprint"])

    def test_diff_and_rules_stay_with_selected_project(self) -> None:
        (self.a / "src/same.py").write_text("value = 'A change'\n", encoding="utf-8")
        (self.b / "src/same.py").write_text("value = 'B change'\n", encoding="utf-8")
        diff = self.call("git_diff", {"repo_path": "project-a"})
        rules = self.call("project_instructions", {"path": "project-a/src/same.py"})
        self.assertEqual(diff["repo_root"], str(self.a))
        diff_text = self.model_text("git_diff", {"repo_path": "project-a"})
        self.assertIn("A change", diff_text)
        self.assertNotIn("B change", diff_text)
        self.assertNotIn("project-b", str(rules["instructions"]))

    def test_git_diff_includes_untracked_files_by_default_and_can_exclude_them(self) -> None:
        new_file = self.a / "src/new.py"
        new_file.write_text("NEW_FILE_MARKER = True\n", encoding="utf-8")
        included = self.call("git_diff", {"repo_path": "project-a"})
        self.assertTrue(included["include_untracked"])
        self.assertIn("+NEW_FILE_MARKER = True", self.model_text("git_diff", {"repo_path": "project-a"}))

        excluded = self.call(
            "git_diff",
            {"repo_path": "project-a", "include_untracked": False},
        )
        self.assertFalse(excluded["include_untracked"])
        self.assertNotIn(
            "NEW_FILE_MARKER",
            self.model_text("git_diff", {"repo_path": "project-a", "include_untracked": False}),
        )

    def test_commands_keep_workdirs_and_filter_recovery(self) -> None:
        runs = []
        for repo in ("project-a", "project-b"):
            runs.append(self.call("exec_command", {
                "cmd": "echo fixture", "workdir": repo, "operation_id": f"test-{repo}", "yield_time_ms": 1000,
            }))
        for repo, run in zip((self.a, self.b), runs):
            self.assertEqual(run["workdir"], str(repo))
            self.assertEqual(self.call("get_command", {"command_id": run["command_id"]})["workdir"], str(repo))
        filtered = self.call("list_commands", {"workdir": "project-a"})
        self.assertEqual([item["command_id"] for item in filtered["commands"]], [runs[0]["command_id"]])
        rendered = self.runtime.call_tool("list_commands", {"workdir": "project-a"})["content"]
        self.assertIn(str(self.a), str(rendered))
        self.assertNotIn(str(self.b), str(rendered))

    def test_instruction_budget_warning_reaches_text_only_clients(self) -> None:
        (self.workspace / "AGENTS.md").write_text("12345678", encoding="utf-8")
        with mock.patch("coding_tools_mcp.project_context.MAX_APPLICABLE_CONTEXT_BYTES", 8):
            result = self.result("project_instructions", {"path": "project-a"})
        self.assertIn("budget exhausted", str(result["content"]))

    def test_crlf_document_diagnostics_remain_fresh_when_content_matches(self) -> None:
        path = self.a / "src/same.py"
        path.write_bytes(b"value = 1\r\n")
        digest = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
        server = SimpleNamespace(
            command=["fixture-lsp"], workspace=self.a,
            diagnostics_snapshot=lambda *_args, **_kwargs: {
                "diagnostics": [], "freshness": "fresh", "document_version": 1, "diagnostics_version": 1,
            },
        )
        with mock.patch.object(self.runtime, "_lsp_document", return_value=(server, SimpleNamespace(path=path, display="project-a/src/same.py"), path.as_uri(), digest)):
            result = self.call("code_diagnostics", {"path": "project-a/src/same.py"})
        self.assertEqual(result["freshness"], "fresh")

    def test_git_location_environment_cannot_redirect_repository(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.b / ".git"), "GIT_WORK_TREE": str(self.b), "GIT_INDEX_FILE": str(self.b / ".git/index")}):
            # host mode deliberately inherits the host environment; Git's
            # explicit per-call target must still take precedence.
            runtime = Runtime(self.workspace, permission_mode="host", project_context=ProjectContext((), (), ()))
            try:
                result = runtime.git_status({"repo_path": "project-a"})
            finally:
                runtime.close()
        self.assertEqual(result["repo_root"], str(self.a))
        self.assertEqual(result["head"], self.git(self.a, "rev-parse", "HEAD").strip())

    def test_parallel_reads_do_not_have_global_current_project(self) -> None:
        projects = ["project-a", "project-b"] * 8
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda name: self.runtime.git_status({"path": name}), projects))
        self.assertEqual([result["repo_root"] for result in results], [str(self.workspace / name) for name in projects])


class ProjectLSPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.file = self.root / "source.py"
        self.file.write_text("value = 1\n", encoding="utf-8")
        # Exercise real document/diagnostic logic without requiring an installed LSP.
        self.server = LanguageServer.__new__(LanguageServer)
        self.server.language = "python"
        self.server._document_lock = threading.RLock()
        self.server._diagnostics_condition = threading.Condition(self.server._document_lock)
        self.server._opened = {}
        self.server._document_versions = {}
        self.server._diagnostics = {}
        self.server._diagnostic_versions = {}
        self.server._diagnostic_observed_versions = {}
        self.server._closed = False
        self.notifications = []
        self.server.notify = lambda method, params: self.notifications.append((method, params))

    def test_language_roots_stop_at_each_project_boundary(self) -> None:
        manager = LSPManager(self.root, {})
        (self.root / "tsconfig.json").write_text("{}", encoding="utf-8")
        (self.root / "pyproject.toml").write_text("", encoding="utf-8")
        for name in ("a", "b"):
            root = self.root / name
            (root / "src").mkdir(parents=True)
            (root / ".git").mkdir()
            for suffix, language in (("py", "python"), ("ts", "typescript"), ("rs", "rust")):
                path = root / "src" / f"app.{suffix}"
                path.touch()
                self.assertEqual(manager.project_root_for(path, language), root)
        nested = self.root / "a/frontend"
        nested.mkdir()
        (nested / "package.json").write_text("{}", encoding="utf-8")
        (nested / "app.ts").touch()
        self.assertEqual(manager.project_root_for(nested / "app.ts", "typescript"), nested)

    def test_unchanged_document_does_not_increase_version(self) -> None:
        uri, _ = self.server.open_document(self.file)
        self.server.open_document(self.file)
        self.assertEqual(self.server._document_versions[uri], 1)
        self.assertEqual(len(self.notifications), 1)

    def test_old_diagnostics_are_not_fresh_after_change(self) -> None:
        uri, _ = self.server.open_document(self.file)
        self.server._publish_diagnostics({"uri": uri, "version": 1, "diagnostics": [{"message": "old"}]})
        self.file.write_text("value = 2\n", encoding="utf-8")
        uri, digest = self.server.open_document(self.file)
        snapshot = self.server.diagnostics_snapshot(uri, 0, expected_digest=digest)
        self.assertEqual(snapshot["document_version"], 2)
        self.assertEqual(snapshot["freshness"], "stale")
        self.server._publish_diagnostics({"uri": uri, "version": 2, "diagnostics": []})
        snapshot = self.server.diagnostics_snapshot(uri, 0, expected_digest=digest)
        self.assertEqual(snapshot["freshness"], "fresh")
        self.assertEqual(snapshot["diagnostics"], [])
        self.server._publish_diagnostics({"uri": uri, "version": 1, "diagnostics": [{"message": "late old"}]})
        self.assertEqual(self.server.diagnostics_snapshot(uri, 0)["diagnostics"], [])

    def test_no_publication_is_pending_not_clean(self) -> None:
        uri, _ = self.server.open_document(self.file)
        self.assertEqual(self.server.diagnostics_snapshot(uri, 0)["freshness"], "pending")

    def test_unversioned_publication_is_not_claimed_fresh(self) -> None:
        uri, _ = self.server.open_document(self.file)
        self.server._publish_diagnostics({"uri": uri, "diagnostics": []})
        self.assertEqual(self.server.diagnostics_snapshot(uri, 0)["freshness"], "unversioned")

    def test_other_uri_does_not_satisfy_wait_for_current_document(self) -> None:
        uri, digest = self.server.open_document(self.file)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.server.diagnostics_snapshot, uri, 1000, expected_digest=digest)
            self.server._publish_diagnostics({"uri": (self.root / "other.py").as_uri(), "version": 1, "diagnostics": []})
            self.assertFalse(pending.done())
            self.server._publish_diagnostics({"uri": uri, "version": 1, "diagnostics": []})
            self.assertEqual(pending.result(timeout=2)["freshness"], "fresh")

    def test_concurrent_document_change_makes_older_query_stale(self) -> None:
        uri, digest = self.server.open_document(self.file)
        self.file.write_text("value = 2\n", encoding="utf-8")
        self.server.open_document(self.file)
        self.server._publish_diagnostics({"uri": uri, "version": 2, "diagnostics": []})
        self.assertEqual(self.server.diagnostics_snapshot(uri, 0, expected_digest=digest)["freshness"], "stale")


if __name__ == "__main__":
    unittest.main()
