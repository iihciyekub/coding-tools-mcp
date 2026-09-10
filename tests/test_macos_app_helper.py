from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_tools_mcp import macos_apps
from coding_tools_mcp.errors import ToolFailure


class MacOSAppHelperDelegationTests(unittest.TestCase):
    def _helper(self, body: str) -> Path:
        directory = tempfile.mkdtemp(prefix="cmt-app-helper-")
        helper = Path(directory) / "helper"
        helper.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return helper

    def test_list_apps_prefers_configured_helper_even_off_macos(self) -> None:
        helper = self._helper("cat >/dev/null; printf '%s\\n' '{\"ok\":true,\"helper\":true,\"apps\":[],\"count\":0,\"truncated\":false}'")
        with patch.dict(os.environ, {"CODING_TOOLS_MCP_APP_HELPER": str(helper)}):
            result = macos_apps.list_apps({"max_results": 1})
        self.assertTrue(result["helper"])
        self.assertEqual(result["count"], 0)

    def test_helper_structured_error_maps_to_tool_failure(self) -> None:
        helper = self._helper("cat >/dev/null; printf '%s\\n' '{\"ok\":false,\"error\":{\"code\":\"ACCESSIBILITY_PERMISSION_REQUIRED\",\"message\":\"grant access\",\"category\":\"permission\",\"retryable\":true}}'")
        with patch.dict(os.environ, {"CODING_TOOLS_MCP_APP_HELPER": str(helper)}):
            with self.assertRaises(ToolFailure) as captured:
                macos_apps.accessibility({})
        self.assertEqual(captured.exception.code, "ACCESSIBILITY_PERMISSION_REQUIRED")
        self.assertTrue(captured.exception.retryable)


if __name__ == "__main__":
    unittest.main()
