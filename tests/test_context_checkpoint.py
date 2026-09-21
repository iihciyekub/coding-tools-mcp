from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_tools_mcp.server import Runtime


class ContextCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime = Runtime(
            self.workspace,
            enable_workflow_tools=True,
            state_root=self.root / "state",
        )
        self.addCleanup(self.runtime.close)

    def call(self, arguments: dict) -> dict:
        result = self.runtime.call_tool("context_checkpoint", arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_create_get_list_and_staleness_are_model_free(self) -> None:
        created = self.call({
            "action": "create",
            "label": "phase five",
            "summary": "Implemented the local context checkpoint runtime.",
            "decisions": ["Do not invoke Codex compact."],
            "unresolved": ["Browser remains unverified."],
            "next_steps": ["Run full regression."],
        })
        checkpoint_id = created["context_checkpoint_id"]
        self.assertTrue(checkpoint_id.startswith("ctx_"))
        self.assertEqual(created["semantic"]["summary"], "Implemented the local context checkpoint runtime.")
        self.assertIn("fingerprints", created["deterministic"])

        fresh = self.call({"action": "get", "context_checkpoint_id": checkpoint_id})
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["stale_reasons"], [])

        # Test-only mutation of a runtime capability flag demonstrates that
        # restore/read detects a changed execution environment without a model.
        self.runtime.enable_agent_environment = True
        stale = self.call({"action": "get", "context_checkpoint_id": checkpoint_id})
        self.assertTrue(stale["stale"])
        self.assertIn("capabilities_changed", stale["stale_reasons"])

        listed = self.call({"action": "list", "max_results": 10})
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["context_checkpoints"][0]["context_checkpoint_id"], checkpoint_id)

    def test_create_requires_semantic_summary(self) -> None:
        result = self.runtime.call_tool("context_checkpoint", {"action": "create", "label": "missing"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "CONTEXT_CHECKPOINT_INVALID")

    def test_unknown_checkpoint_is_structured_error(self) -> None:
        result = self.runtime.call_tool(
            "context_checkpoint",
            {"action": "get", "context_checkpoint_id": "ctx_missing"},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "CONTEXT_CHECKPOINT_NOT_FOUND")

    def test_task_context_includes_attached_context_checkpoints(self) -> None:
        task_result = self.runtime.call_tool(
            "task_create",
            {"title": "Resume fixture", "objective": "Verify cross-session context recovery."},
        )
        self.assertFalse(task_result["isError"], task_result)
        task_id = task_result["structuredContent"]["task_id"]
        created = self.call({
            "action": "create",
            "task_id": task_id,
            "label": "attached",
            "summary": "Resume from this point.",
        })
        context_result = self.runtime.call_tool("task_context", {"task_id": task_id})
        self.assertFalse(context_result["isError"], context_result)
        context = context_result["structuredContent"]
        self.assertEqual(context["context_checkpoints"][0]["context_checkpoint_id"], created["context_checkpoint_id"])
        self.assertIn("1 context checkpoints", context["summary"])


if __name__ == "__main__":
    unittest.main()

