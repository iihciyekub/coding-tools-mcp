from __future__ import annotations

import unittest

from coding_tools_mcp.check_diagnostics import analyze_check_output


class CheckDiagnosticsTests(unittest.TestCase):
    def test_parses_common_diagnostics_and_failing_tests(self) -> None:
        stdout = "\n".join(
            [
                "FAILED tests/test_api.py::test_login - AssertionError: nope",
                "test runtime::tests::resume ... FAILED",
                "--- FAIL: TestHandler (0.01s)",
                "src/app.ts(12,5): error TS2322: Type 'x' is not assignable",
                "pkg/main.go:44:9: undefined: thing",
            ]
        )
        stderr = "\n".join(
            [
                "src/model.py:8:4: error: Incompatible types in assignment  [assignment]",
                "src/lint.py:3:1: F401 imported but unused",
                "error[E0382]: borrow of moved value: `item`",
                "  --> src/lib.rs:55:17",
            ]
        )
        result = analyze_check_output(
            check_id="make:test",
            command="make test",
            kind="test",
            stdout=stdout,
            stderr=stderr,
        )
        self.assertGreaterEqual(result["diagnostic_count"], 5)
        self.assertEqual(
            result["failing_tests"],
            ["tests/test_api.py::test_login", "runtime::tests::resume", "TestHandler"],
        )
        parsers = result["diagnostic_parsers"]
        for parser in ("tsc", "go", "mypy", "ruff", "rustc"):
            self.assertIn(parser, parsers)
        rust = next(item for item in result["diagnostics"] if item["parser"] == "rustc")
        self.assertEqual(rust["path"], "src/lib.rs")
        self.assertEqual(rust["code"], "E0382")
        self.assertTrue(result["diagnostic_context"]["raw_output_authoritative"])

    def test_parser_is_bounded(self) -> None:
        stderr = "\n".join(f"file{i}.py:1: error: problem {i}  [misc]" for i in range(20))
        result = analyze_check_output(
            check_id="python:mypy",
            command="python -m mypy .",
            kind="typecheck",
            stderr=stderr,
            max_diagnostics=5,
        )
        self.assertEqual(result["diagnostic_count"], 5)
        self.assertTrue(result["diagnostics_truncated"])

    def test_parses_apple_toolchain_failures(self) -> None:
        result = analyze_check_output(
            check_id="xcode:test",
            command="xcodebuild -scheme Demo test && codesign --verify Demo.app && xcrun notarytool submit Demo.zip",
            kind="test",
            stdout="\n".join(
                [
                    "/tmp/App.swift:12:7: error: cannot find 'missing' in scope",
                    "/tmp/Bridge.mm:8:3: warning: unused variable 'value'",
                    "Test Case '-[DemoTests.LoginTests testLogin]' failed (0.12 seconds).",
                    "** TEST FAILED **",
                    "codesign: errSecInternalComponent",
                    "Error: submission failed",
                ]
            ),
        )
        parsers = result["diagnostic_parsers"]
        for parser in ("swiftc", "clang", "xcodebuild", "codesign", "notarytool"):
            self.assertIn(parser, parsers)
        self.assertEqual(
            result["failing_tests"],
            ["-[DemoTests.LoginTests testLogin]"],
        )
        swift = next(item for item in result["diagnostics"] if item["parser"] == "swiftc")
        self.assertEqual(swift["path"], "/tmp/App.swift")
        self.assertEqual(swift["line"], 12)


if __name__ == "__main__":
    unittest.main()
