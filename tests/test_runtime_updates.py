"""Regression coverage for persistent gateway and upstream reliability updates."""

from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from coding_tools_mcp.gateway import ProjectDefinition, ProjectRegistry
from coding_tools_mcp.patching import content_revision
from coding_tools_mcp.server import Runtime


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = Runtime(self.root, state_root=self.root / "state")
        self.addCleanup(self.runtime.close)

    def test_file_pages_keep_physical_lines_and_requested_end(self) -> None:
        for ending in ("\n", "\r\n", "\r"):
            with self.subTest(ending=ending):
                raw = ending.join(("alpha", "bravo", "charlie", "delta")) + ending
                (self.root / "a.txt").write_bytes(raw.encode())
                first = self.runtime.read_file(
                    {
                        "path": "a.txt",
                        "end_line": 2,
                        "max_bytes": 8,
                        "line_numbers": True,
                    }
                )
                self.assertEqual(first["content"], "alpha" + ending)
                self.assertEqual(first["next_action"]["arguments"]["end_line"], 2)
                second = self.runtime.read_file(first["next_action"]["arguments"])
                self.assertEqual(second["content"], "bravo" + ending)
                self.assertIsNone(second["next_start_line"])
                self.assertEqual(first["revision"], content_revision(raw))

    def test_numbered_text_uses_the_same_physical_lines_as_edits(self) -> None:
        (self.root / "a.txt").write_text("first\u2028still-first\nsecond\n")
        result = self.runtime.call_tool(
            "read_file", {"path": "a.txt", "line_numbers": True}
        )
        text = "\n".join(
            item["text"] for item in result["content"] if item["type"] == "text"
        )
        self.assertIn("1: first\u2028still-first\n2: second", text)
        self.assertNotIn("3:", text)

    def test_line_edits_are_atomic_revision_checked_and_retryable(self) -> None:
        (self.root / "a.txt").write_bytes(b"a\r\nb\r\nc\r\n")
        args = {
            "idempotency_key": "once",
            "changes": [
                {
                    "action": "edit",
                    "path": "a.txt",
                    "revision": content_revision("a\r\nb\r\nc\r\n"),
                    "edits": [{"op": "replace", "line": 2, "content": "B"}],
                },
                {"action": "create", "path": "new.txt", "content": "new\n"},
            ],
        }
        first = self.runtime.call_tool("apply_changes", args)
        self.assertFalse(first["isError"], first)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"a\r\nB\r\nc\r\n")
        replay = self.runtime.call_tool("apply_changes", args)
        self.assertTrue(replay["structuredContent"]["deduplicated"])
        self.assertEqual(
            first["structuredContent"]["affected_files"],
            replay["structuredContent"]["affected_files"],
        )
        mismatch = self.runtime.call_tool(
            "apply_changes", {**args, "idempotency_key": "different"}
        )
        self.assertEqual(
            mismatch["structuredContent"]["error"]["code"], "REVISION_MISMATCH"
        )
        self.assertNotIn(
            "current_revision", mismatch["structuredContent"]["error"]["details"]
        )

    def test_failed_second_change_never_commits_first_file(self) -> None:
        (self.root / "a.txt").write_text("before")
        result = self.runtime.call_tool(
            "apply_changes",
            {
                "changes": [
                    {
                        "action": "write",
                        "path": "a.txt",
                        "content": "after",
                        "revision": content_revision("before"),
                    },
                    {
                        "action": "write",
                        "path": "missing.txt",
                        "content": "new",
                        "revision": "0" * 64,
                    },
                ]
            },
        )
        self.assertTrue(result["isError"])
        self.assertEqual((self.root / "a.txt").read_text(), "before")
        self.assertFalse((self.root / "missing.txt").exists())

    def test_structured_writes_keep_workspace_and_symlink_boundaries(self) -> None:
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        for path in ("../outside.txt", "escape/file.txt"):
            result = self.runtime.call_tool(
                "apply_changes",
                {"changes": [{"action": "create", "path": path, "content": "blocked"}]},
            )
            self.assertTrue(result["isError"], result)
        self.assertEqual(list(outside.iterdir()), [])

    def test_both_write_tools_share_a_bounded_replay_cache(self) -> None:
        for i in range(70):
            self.runtime.apply_changes(
                {
                    "idempotency_key": str(i),
                    "changes": [
                        {"action": "create", "path": f"{i}.txt", "content": str(i)}
                    ],
                }
            )
        self.assertEqual(len(self.runtime.patch_replays), 64)
        patch = "*** Begin Patch\n*** Add File: patch.txt\n+hello\n*** End Patch"
        self.runtime.apply_patch({"patch": patch, "idempotency_key": "patch"})
        self.assertEqual(len(self.runtime.patch_replays), 64)
        self.assertTrue(
            self.runtime.apply_patch({"patch": patch, "idempotency_key": "patch"})[
                "deduplicated"
            ]
        )

    def test_already_applied_patch_reports_no_write(self) -> None:
        (self.root / "a.txt").write_text("context\nnew\n")
        args = {
            "patch": "*** Begin Patch\n*** Update File: a.txt\n@@\n context\n-old\n+new\n*** End Patch"
        }
        before = (self.root / "a.txt").stat().st_mtime_ns
        result = self.runtime.apply_patch(args)
        self.assertTrue(result["already_applied"])
        self.assertEqual(result["additions"], 0)
        self.assertEqual((self.root / "a.txt").stat().st_mtime_ns, before)

    def test_pipe_stdin_can_remain_open_across_calls(self) -> None:
        result = self.runtime.call_tool(
            "exec_command", {"cmd": "cat", "keep_stdin_open": True, "yield_time_ms": 0}
        )
        self.assertFalse(result["isError"], result)
        command_id = result["structuredContent"]["command_id"]
        result = self.runtime.call_tool(
            "write_stdin",
            {"command_id": command_id, "chars": "hello\n", "yield_time_ms": 100},
        )
        self.assertFalse(result["isError"], result)
        self.assertIn("hello", result["structuredContent"].get("stdout", ""))
        self.runtime.kill_command({"command_id": command_id})


class BackgroundIndexTests(unittest.TestCase):
    def test_slow_discovery_never_blocks_routing_or_loses_new_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            registry = ProjectRegistry(
                None,
                None,
                runtime_factory=lambda _: None,
                workspace_roots=(ProjectDefinition("workspace", "Workspace", root),),
            )
            self.addCleanup(registry.close)
            entered, release = threading.Event(), threading.Event()
            original = registry._scan_workspace_paths

            def slow_scan(*args):
                entered.set()
                release.wait(3)
                return original(*args)

            with mock.patch.object(
                registry, "_scan_workspace_paths", side_effect=slow_scan
            ):
                registry._last_scan = 0
                registry.refresh()
                self.assertTrue(entered.wait(1))
                fresh = root / "new-during-scan"
                fresh.mkdir()
                definition, _ = registry.resolve_selector(str(fresh))
                self.assertEqual(definition.path, fresh)
                release.set()
                registry._scan_thread.join(2)
                self.assertEqual(registry.get(definition.id).path, fresh)

    def test_stale_scan_cannot_restore_revoked_workspaces(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "workspace"
            root.mkdir()
            path = base / "registry.json"
            path.write_text(
                json.dumps({"workspaces": [{"id": "w", "path": str(root)}]})
            )
            registry = ProjectRegistry(
                None, None, runtime_factory=lambda _: None, registry_file=path
            )
            self.addCleanup(registry.close)
            entered, release = threading.Event(), threading.Event()
            original = registry._scan_workspace_paths

            def slow_scan(*args):
                entered.set()
                release.wait(3)
                return original(*args)

            with mock.patch.object(
                registry, "_scan_workspace_paths", side_effect=slow_scan
            ):
                registry._last_scan = 0
                registry.refresh()
                self.assertTrue(entered.wait(1))
                path.write_text(json.dumps({"workspaces": []}))
                os.utime(path, None)
                registry.refresh(force=True)
                release.set()
                registry._scan_thread.join(2)
                self.assertIsNone(registry.resolve_selector(str(root))[0])
