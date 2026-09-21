"""Bounded, deterministic parsing of common developer-check failures.

The parser is intentionally heuristic. It extracts high-signal locations and
failing test identifiers while preserving raw command output as the source of
truth. It never executes project code or invokes another model.
"""

from __future__ import annotations

import re
from typing import Any

MAX_MESSAGE_CHARS = 500
MAX_FAILING_TESTS = 200

_TSC = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s*"
    r"(?P<severity>error|warning)\s+(?P<code>TS\d+):\s*(?P<message>.+)$",
    re.IGNORECASE,
)
_MYPY = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
    r"(?P<severity>error|warning|note):\s*(?P<message>.*?)(?:\s+\[(?P<code>[^\]]+)\])?$",
    re.IGNORECASE,
)
_RUFF = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<code>[A-Z][A-Z0-9]*\d+)\s+(?P<message>.+)$"
)
_GO = re.compile(
    r"^(?P<path>.+?\.go):(?P<line>\d+):(?P<column>\d+):\s*(?P<message>.+)$"
)
_APPLE_LOCATION = re.compile(
    r"^(?P<path>.+?\.(?:swift|m|mm|c|cc|cpp|h|hpp)):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<severity>error|warning|note):\s*(?P<message>.+)$",
    re.IGNORECASE,
)
_PYTEST_LOCATION = re.compile(
    r"^(?P<path>.+?\.py):(?P<line>\d+):(?:(?P<column>\d+):)?\s*(?P<message>.+)$"
)
_RUST_HEAD = re.compile(
    r"^(?P<severity>error|warning)(?:\[(?P<code>[A-Z]\d+)\])?:\s*(?P<message>.+)$",
    re.IGNORECASE,
)
_RUST_LOCATION = re.compile(r"^\s*-->\s+(?P<path>.+?):(?P<line>\d+):(?P<column>\d+)\s*$")
_ESLINT_LOCATION = re.compile(
    r"^\s*(?P<line>\d+):(?P<column>\d+)\s+"
    r"(?P<severity>error|warning)\s+(?P<message>.+?)(?:\s{2,}(?P<code>[@\w/-]+))?\s*$",
    re.IGNORECASE,
)
_PYTEST_FAILED = re.compile(r"^FAILED\s+(?P<name>\S+)(?:\s+-\s+.*)?$")
_CARGO_FAILED = re.compile(r"^test\s+(?P<name>\S+)\s+\.\.\.\s+FAILED$")
_GO_FAILED = re.compile(r"^--- FAIL:\s+(?P<name>\S+)")
_NODE_FAILED = re.compile(r"^\s*(?:✗|×|FAIL)\s+(?P<name>.+?)\s*$")
_XCTEST_FAILED = re.compile(
    r"^Test Case ['\"](?P<name>.+?)['\"] failed(?: \([0-9.]+ seconds\))?\.?$"
)

_SOURCE_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".swift",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
)


def _clean_message(value: str) -> str:
    return " ".join(value.strip().split())[:MAX_MESSAGE_CHARS]


def _diagnostic(
    *,
    parser: str,
    message: str,
    severity: str = "error",
    path: str | None = None,
    line: int | None = None,
    column: int | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": severity.lower(),
        "message": _clean_message(message),
        "parser": parser,
    }
    if path:
        item["path"] = path.strip()
    if line is not None:
        item["line"] = line
    if column is not None:
        item["column"] = column
    if code:
        item["code"] = code.strip()
    return item


def analyze_check_output(
    *,
    check_id: str,
    command: str,
    kind: str,
    stdout: str = "",
    stderr: str = "",
    max_diagnostics: int = 100,
) -> dict[str, Any]:
    """Return a bounded structured view of common check failures."""

    max_diagnostics = max(1, min(int(max_diagnostics), 500))
    text = "\n".join(part for part in (stdout, stderr) if part)
    lines = text.splitlines()
    diagnostics: list[dict[str, Any]] = []
    diagnostic_keys: set[tuple[Any, ...]] = set()
    failing_tests: list[str] = []
    failing_seen: set[str] = set()
    pending_rust: dict[str, Any] | None = None
    eslint_path: str | None = None
    truncated = False
    command_lower = command.casefold()

    def add(item: dict[str, Any]) -> None:
        nonlocal truncated
        key = (
            item.get("path"),
            item.get("line"),
            item.get("column"),
            item.get("code"),
            item.get("message"),
            item.get("severity"),
        )
        if key in diagnostic_keys:
            return
        if len(diagnostics) >= max_diagnostics:
            truncated = True
            return
        diagnostic_keys.add(key)
        diagnostics.append(item)

    def add_test(name: str) -> None:
        name = name.strip()[:500]
        if not name or name in failing_seen:
            return
        if len(failing_tests) >= MAX_FAILING_TESTS:
            return
        failing_seen.add(name)
        failing_tests.append(name)

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue

        for pattern in (_PYTEST_FAILED, _CARGO_FAILED, _GO_FAILED, _NODE_FAILED, _XCTEST_FAILED):
            matched_test = pattern.match(stripped)
            if matched_test:
                add_test(matched_test.group("name"))
                break

        apple_match = _APPLE_LOCATION.match(line)
        if apple_match:
            path = apple_match.group("path")
            add(
                _diagnostic(
                    parser="swiftc" if path.casefold().endswith(".swift") else "clang",
                    path=path,
                    line=int(apple_match.group("line")),
                    column=int(apple_match.group("column")),
                    severity=apple_match.group("severity"),
                    message=apple_match.group("message"),
                )
            )
            continue

        if stripped in {"** BUILD FAILED **", "** TEST FAILED **"}:
            add(
                _diagnostic(
                    parser="xcodebuild",
                    code="BUILD_FAILED" if "BUILD" in stripped else "TEST_FAILED",
                    message=stripped.strip("* "),
                )
            )
            continue

        lowered = stripped.casefold()
        if "notarytool" in command_lower and lowered.startswith(("error:", "error ")):
            add(_diagnostic(parser="notarytool", message=stripped))
            continue
        if "codesign" in command_lower and (
            "errsec" in lowered or "code object is not signed" in lowered or lowered.startswith("error:")
        ):
            add(_diagnostic(parser="codesign", message=stripped))
            continue

        match = _TSC.match(line)
        if match:
            add(
                _diagnostic(
                    parser="tsc",
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    severity=match.group("severity"),
                    code=match.group("code"),
                    message=match.group("message"),
                )
            )
            continue

        match = _MYPY.match(line)
        if match:
            add(
                _diagnostic(
                    parser="mypy",
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")) if match.group("column") else None,
                    severity=match.group("severity"),
                    code=match.group("code"),
                    message=match.group("message"),
                )
            )
            continue

        match = _RUFF.match(line)
        if match:
            add(
                _diagnostic(
                    parser="ruff",
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    code=match.group("code"),
                    message=match.group("message"),
                )
            )
            continue

        match = _GO.match(line)
        if match:
            add(
                _diagnostic(
                    parser="go",
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    message=match.group("message"),
                )
            )
            continue

        rust_head = _RUST_HEAD.match(stripped)
        if rust_head:
            if pending_rust is not None:
                add(pending_rust)
            pending_rust = _diagnostic(
                parser="rustc",
                severity=rust_head.group("severity"),
                code=rust_head.group("code"),
                message=rust_head.group("message"),
            )
            continue

        rust_location = _RUST_LOCATION.match(line)
        if rust_location and pending_rust is not None:
            pending_rust.update(
                path=rust_location.group("path"),
                line=int(rust_location.group("line")),
                column=int(rust_location.group("column")),
            )
            add(pending_rust)
            pending_rust = None
            continue

        pytest_location = _PYTEST_LOCATION.match(line)
        if pytest_location and any(
            token in pytest_location.group("message").lower()
            for token in ("error", "assert", "failed", "exception")
        ):
            add(
                _diagnostic(
                    parser="pytest",
                    path=pytest_location.group("path"),
                    line=int(pytest_location.group("line")),
                    column=(
                        int(pytest_location.group("column"))
                        if pytest_location.group("column")
                        else None
                    ),
                    message=pytest_location.group("message"),
                )
            )
            continue

        if stripped.endswith(_SOURCE_SUFFIXES) and " " not in stripped:
            eslint_path = stripped
            continue
        eslint_location = _ESLINT_LOCATION.match(line)
        if eslint_path and eslint_location:
            add(
                _diagnostic(
                    parser="eslint",
                    path=eslint_path,
                    line=int(eslint_location.group("line")),
                    column=int(eslint_location.group("column")),
                    severity=eslint_location.group("severity"),
                    code=eslint_location.group("code"),
                    message=eslint_location.group("message"),
                )
            )
            continue

    if pending_rust is not None:
        add(pending_rust)

    parser_counts: dict[str, int] = {}
    for item in diagnostics:
        parser = str(item.get("parser", "unknown"))
        parser_counts[parser] = parser_counts.get(parser, 0) + 1

    if diagnostics or failing_tests:
        summary = (
            f"Parsed {len(diagnostics)} diagnostic(s) and {len(failing_tests)} failing test(s) "
            f"from {check_id}."
        )
    else:
        summary = f"No structured failure diagnostics recognized for {check_id}; raw output remains authoritative."

    return {
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "failing_tests": failing_tests,
        "failing_test_count": len(failing_tests),
        "diagnostics_truncated": truncated,
        "diagnostic_parsers": parser_counts,
        "diagnostic_parser_version": "v1",
        "diagnostic_summary": summary,
        "diagnostic_context": {
            "check_id": check_id,
            "kind": kind,
            "command": command,
            "heuristic": True,
            "raw_output_authoritative": True,
        },
    }
