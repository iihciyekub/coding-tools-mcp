from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coding_tools_mcp import code_intel
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.server import Runtime


class CodeIntelTests(unittest.TestCase):
    def test_python_symbols_definitions_and_references(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "sample.py").write_text(
                "class Worker:\n"
                "    async def run(self):\n"
                "        return helper(1)\n\n"
                "def helper(value):\n"
                "    return value\n",
                encoding="utf-8",
            )
            (workspace / "consumer.py").write_text(
                "from sample import helper\nresult = helper(2)\n",
                encoding="utf-8",
            )

            listed = code_intel.symbols(workspace, workspace, {})
            names = {item["qualified_name"] for item in listed["symbols"]}
            self.assertIn("Worker", names)
            self.assertIn("Worker.run", names)
            self.assertIn("helper", names)

            definition = code_intel.definition(workspace, workspace, {"symbol": "Worker.run"})
            self.assertEqual(definition["count"], 1)
            self.assertEqual(definition["definitions"][0]["path"], "sample.py")
            self.assertEqual(definition["definitions"][0]["line"], 2)

            references = code_intel.references(workspace, workspace, {"symbol": "helper"})
            self.assertGreaterEqual(references["count"], 4)
            self.assertTrue(any(item["path"] == "consumer.py" for item in references["references"]))

    def test_javascript_typescript_symbols_are_language_aware(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "component.ts"
            source.write_text(
                "export interface Props { title: string }\n"
                "export function render() { return 1 }\n"
                "export const Widget = () => render()\n"
                "export class Panel {}\n",
                encoding="utf-8",
            )

            listed = code_intel.symbols(workspace, source, {})
            by_name = {item["name"]: item["kind"] for item in listed["symbols"]}
            self.assertEqual(by_name["Props"], "interface")
            self.assertEqual(by_name["render"], "function")
            self.assertEqual(by_name["Widget"], "variable")
            self.assertEqual(by_name["Panel"], "class")

    def test_runtime_code_tools_keep_workspace_path_confinement(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "main.py").write_text("def local_symbol():\n    return 1\n", encoding="utf-8")
            runtime = Runtime(workspace)

            result = runtime.code_definition({"symbol": "local_symbol", "path": "."})
            self.assertEqual(result["count"], 1)
            with self.assertRaises(ToolFailure):
                runtime.code_symbols({"path": "../"})


if __name__ == "__main__":
    unittest.main()
