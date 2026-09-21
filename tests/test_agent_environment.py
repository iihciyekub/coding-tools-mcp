from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_tools_mcp import agent_environment
from coding_tools_mcp.server import Runtime


class AgentEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def _codex_fixture(self) -> Path:
        root = self.home / ".codex"
        (root / "skills" / "local-skill").mkdir(parents=True)
        (root / "skills" / "local-skill" / "SKILL.md").write_text("secret body must not be read", encoding="utf-8")
        external = self.home / "external-skill"
        external.mkdir()
        (external / "SKILL.md").write_text("external body must not be read", encoding="utf-8")
        (root / "skills" / "linked-skill").symlink_to(external, target_is_directory=True)
        (root / "worktrees" / "annotation-engine-v3").mkdir(parents=True)
        (root / "rules").mkdir(parents=True)
        (root / "rules" / "default.rules").write_text("rule body", encoding="utf-8")
        (root / "browser" / "sessions").mkdir(parents=True)
        (root / "mcp-oauth-locks").mkdir(parents=True)
        (root / "auth.json").write_text('{"token":"TOP-SECRET-TOKEN"}', encoding="utf-8")
        (root / "config.toml").write_text(
            """
[plugins."browser@openai-bundled"]
enabled = true
[plugins."disabled@example"]
enabled = false
""".strip(),
            encoding="utf-8",
        )
        plugin = root / "plugins" / "cache" / "openai-bundled" / "browser" / "1"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text('{"name":"browser"}', encoding="utf-8")
        (plugin / "skills" / "browser-skill").mkdir(parents=True)
        (plugin / "skills" / "browser-skill" / "SKILL.md").write_text("browser instructions", encoding="utf-8")
        # Capability cache presence is metadata only.
        (root / "plugins" / "cache" / "openai-bundled" / "chrome").mkdir(parents=True)
        return root

    def test_codex_discovery_returns_metadata_without_sensitive_contents(self) -> None:
        root = self._codex_fixture()
        with mock.patch.object(agent_environment, "_home", return_value=self.home), mock.patch(
            "coding_tools_mcp.agent_environment.shutil.which",
            side_effect=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
        ):
            result = agent_environment.discover_agent_environment(provider="codex")
        self.assertTrue(result["metadata_only"])
        self.assertFalse(result["sensitive_contents_read"])
        provider = result["providers"][0]
        self.assertTrue(provider["installed"])
        self.assertEqual(provider["home"], str(root.resolve()))
        self.assertEqual([item["name"] for item in provider["skills"]], ["linked-skill", "local-skill"])
        self.assertTrue(provider["skills"][0]["symlink"])
        self.assertEqual(provider["enabled_plugins"], [{"id": "browser@openai-bundled", "enabled": True}])
        self.assertEqual(provider["plugin_skills"][0]["name"], "browser-skill")
        self.assertIn("worktrees/annotation-engine-v3", provider["worktrees"])
        self.assertIn("rules/default.rules", provider["rules"])
        self.assertTrue(provider["sensitive_resources_present"]["auth"])
        self.assertTrue(provider["sensitive_resources_present"]["browser_sessions"])
        rendered = json.dumps(result)
        self.assertNotIn("TOP-SECRET-TOKEN", rendered)
        self.assertNotIn("secret body must not be read", rendered)
        self.assertNotIn("external body must not be read", rendered)
        self.assertNotIn("browser instructions", rendered)

    def test_generic_agent_reports_skill_names_and_secret_presence_only(self) -> None:
        root = self.home / ".claude"
        (root / "skills" / "review").mkdir(parents=True)
        (root / "skills" / "review" / "SKILL.md").write_text("DO NOT RETURN THIS BODY", encoding="utf-8")
        (root / "credentials.json").write_text('{"secret":"NOPE"}', encoding="utf-8")
        with mock.patch.object(agent_environment, "_home", return_value=self.home), mock.patch(
            "coding_tools_mcp.agent_environment.shutil.which", return_value=None
        ):
            result = agent_environment.discover_agent_environment(provider="claude")
        provider = result["providers"][0]
        self.assertTrue(provider["installed"])
        self.assertEqual(provider["skills"][0]["name"], "review")
        self.assertTrue(provider["sensitive_resources_present"]["auth_like_files"])
        rendered = json.dumps(result)
        self.assertNotIn("DO NOT RETURN THIS BODY", rendered)
        self.assertNotIn("NOPE", rendered)

    def test_agent_environment_tool_requires_host_mode(self) -> None:
        workspace = self.home / "workspace"
        workspace.mkdir()
        self._codex_fixture()
        with mock.patch.object(agent_environment, "_home", return_value=self.home), mock.patch(
            "coding_tools_mcp.agent_environment.shutil.which", return_value=None
        ):
            safe = Runtime(workspace)
            self.addCleanup(safe.close)
            self.assertNotIn("agent_environment", safe.exposed_tool_names())
            runtime = Runtime(workspace, permission_mode="host")
            self.addCleanup(runtime.close)
            self.assertIn("agent_environment", runtime.exposed_tool_names())
            result = runtime.call_tool("agent_environment", {"provider": "codex", "max_items": 20})
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["providers"][0]["id"], "codex")


if __name__ == "__main__":
    unittest.main()

