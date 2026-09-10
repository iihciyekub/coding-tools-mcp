from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Iterator


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}

SUPPORTED_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".rs",
    ".swift",
    ".go",
    ".java",
    ".kt",
    ".kts",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".cxx",
    ".hpp",
    ".hh",
}


PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "javascript": (
        ("function", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\b")),
        ("class", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)\b")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)\b")),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\b")),
        ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)\b")),
        ("variable", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b")),
    ),
    "rust": (
        ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)\b")),
        ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_][\w]*)\b")),
        ("enum", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_][\w]*)\b")),
        ("trait", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_][\w]*)\b")),
        ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?type\s+([A-Za-z_][\w]*)\b")),
        ("constant", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+([A-Za-z_][\w]*)\b")),
    ),
    "swift": (
        ("function", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+|open\s+)?func\s+([A-Za-z_][\w]*)\b")),
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+|open\s+)?class\s+([A-Za-z_][\w]*)\b")),
        ("struct", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+)?struct\s+([A-Za-z_][\w]*)\b")),
        ("enum", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+)?enum\s+([A-Za-z_][\w]*)\b")),
        ("protocol", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+)?protocol\s+([A-Za-z_][\w]*)\b")),
        ("variable", re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+)?(?:let|var)\s+([A-Za-z_][\w]*)\b")),
    ),
    "go": (
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\b")),
        ("type", re.compile(r"^\s*type\s+([A-Za-z_][\w]*)\b")),
        ("variable", re.compile(r"^\s*(?:var|const)\s+([A-Za-z_][\w]*)\b")),
    ),
    "jvm": (
        ("class", re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|open|final|sealed|data|static)\s+)*(?:class|record)\s+([A-Za-z_][\w]*)\b")),
        ("interface", re.compile(r"^\s*(?:(?:public|private|protected|internal|abstract|sealed)\s+)*interface\s+([A-Za-z_][\w]*)\b")),
        ("enum", re.compile(r"^\s*(?:(?:public|private|protected|internal)\s+)*enum(?:\s+class)?\s+([A-Za-z_][\w]*)\b")),
        ("function", re.compile(r"^\s*(?:(?:public|private|protected|internal|open|override|suspend|inline|tailrec|operator|infix)\s+)*fun\s+([A-Za-z_][\w]*)\b")),
        ("function", re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native|strictfp|default)\s+)*(?:<[^>]+>\s+)?[A-Za-z_$][\w$<>\[\], ?.&]*\s+([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:throws\s+[^\{]+)?\{")),
    ),
    "c_family": (
        ("class", re.compile(r"^\s*(?:class|struct|enum)\s+([A-Za-z_][\w]*)\b")),
        ("function", re.compile(r"^\s*(?:[A-Za-z_][\w:<>,*&\s]+\s+)+([A-Za-z_~][\w:]*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{")),
    ),
}


def _language(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyi"}:
        return "python"
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return "javascript"
    if suffix == ".rs":
        return "rust"
    if suffix == ".swift":
        return "swift"
    if suffix == ".go":
        return "go"
    if suffix in {".java", ".kt", ".kts"}:
        return "jvm"
    if suffix in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}:
        return "c_family"
    return None


def _safe_file(workspace: Path, path: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(workspace.resolve(strict=False))
    except OSError:
        return False


def _iter_code_files(workspace: Path, target: Path, max_files: int) -> Iterator[Path]:
    count = 0
    if target.is_file():
        if target.suffix.lower() in SUPPORTED_SUFFIXES and _safe_file(workspace, target):
            yield target
        return
    for current, dirs, files in os.walk(target, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED_DIRS)
        current_path = Path(current)
        for name in sorted(files):
            candidate = current_path / name
            if candidate.suffix.lower() not in SUPPORTED_SUFFIXES or not _safe_file(workspace, candidate):
                continue
            yield candidate
            count += 1
            if count >= max_files:
                return


def _scan_code_files(workspace: Path, target: Path, max_files: int) -> tuple[list[Path], bool]:
    """Return a deterministic bounded scan plus whether more code files exist."""

    candidates = list(_iter_code_files(workspace, target, max_files + 1))
    return candidates[:max_files], len(candidates) > max_files


def _display_path(workspace: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(workspace.resolve(strict=False)).as_posix()
    except ValueError:
        return path.name


def _read_lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, lines: list[str], display_path: str) -> None:
        self.lines = lines
        self.display_path = display_path
        self.scope: list[str] = []
        self.items: list[dict[str, Any]] = []

    def _add(self, node: ast.AST, name: str, kind: str) -> None:
        line = int(getattr(node, "lineno", 1))
        column = int(getattr(node, "col_offset", 0)) + 1
        qualified = ".".join([*self.scope, name]) if self.scope else name
        preview = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        self.items.append(
            {
                "name": name,
                "qualified_name": qualified,
                "kind": kind,
                "path": self.display_path,
                "line": line,
                "column": column,
                "preview": preview[:500],
            }
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, node.name, "class")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add(node, node.name, "function")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add(node, node.name, "function")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _python_symbols(path: Path, display_path: str, lines: list[str]) -> list[dict[str, Any]]:
    try:
        tree = ast.parse("\n".join(lines), filename=display_path)
    except SyntaxError:
        return []
    visitor = _PythonSymbolVisitor(lines, display_path)
    visitor.visit(tree)
    return visitor.items


def _pattern_symbols(path: Path, display_path: str, lines: list[str]) -> list[dict[str, Any]]:
    language = _language(path)
    patterns = PATTERNS.get(language or "", ())
    items: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            items.append(
                {
                    "name": name,
                    "qualified_name": name,
                    "kind": kind,
                    "path": display_path,
                    "line": line_number,
                    "column": match.start(1) + 1,
                    "preview": line.strip()[:500],
                }
            )
            break
    return items


def _file_symbols(workspace: Path, path: Path) -> list[dict[str, Any]]:
    lines = _read_lines(path)
    if lines is None:
        return []
    display_path = _display_path(workspace, path)
    if _language(path) == "python":
        return _python_symbols(path, display_path, lines)
    return _pattern_symbols(path, display_path, lines)


def symbols(workspace: Path, target: Path, args: dict[str, Any]) -> dict[str, Any]:
    max_results = int(args.get("max_results", 500))
    max_files = int(args.get("max_files", 2000))
    query = str(args.get("query") or "").casefold()
    requested_kind = str(args.get("kind") or "").casefold()
    items: list[dict[str, Any]] = []
    scanned_files = 0
    paths, scan_truncated = _scan_code_files(workspace, target, max_files)
    for path in paths:
        scanned_files += 1
        for item in _file_symbols(workspace, path):
            if query and query not in str(item["qualified_name"]).casefold():
                continue
            if requested_kind and requested_kind != str(item["kind"]).casefold():
                continue
            items.append(item)
            if len(items) >= max_results:
                return {
                    "ok": True,
                    "symbols": items,
                    "count": len(items),
                    "scanned_files": scanned_files,
                    "truncated": True,
                    "truncated_by": "max_results",
                    "scan_complete": False,
                }
    result = {
        "ok": True,
        "symbols": items,
        "count": len(items),
        "scanned_files": scanned_files,
        "truncated": scan_truncated,
        "scan_complete": not scan_truncated,
    }
    if scan_truncated:
        result["truncated_by"] = "max_files"
    return result


def definition(workspace: Path, target: Path, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args["symbol"])
    max_results = int(args.get("max_results", 50))
    max_files = int(args.get("max_files", 2000))
    items: list[dict[str, Any]] = []
    scanned_files = 0
    paths, scan_truncated = _scan_code_files(workspace, target, max_files)
    for path in paths:
        scanned_files += 1
        for item in _file_symbols(workspace, path):
            if item["name"] != name and item["qualified_name"] != name:
                continue
            items.append(item)
            if len(items) >= max_results:
                return {
                    "ok": True,
                    "symbol": name,
                    "definitions": items,
                    "count": len(items),
                    "scanned_files": scanned_files,
                    "truncated": True,
                    "truncated_by": "max_results",
                    "scan_complete": False,
                }
    result = {
        "ok": True,
        "symbol": name,
        "definitions": items,
        "count": len(items),
        "scanned_files": scanned_files,
        "truncated": scan_truncated,
        "scan_complete": not scan_truncated,
    }
    if scan_truncated:
        result["truncated_by"] = "max_files"
    return result


def references(workspace: Path, target: Path, args: dict[str, Any]) -> dict[str, Any]:
    name = str(args["symbol"])
    max_results = int(args.get("max_results", 500))
    max_files = int(args.get("max_files", 2000))
    case_sensitive = bool(args.get("case_sensitive", True))
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])", flags)
    items: list[dict[str, Any]] = []
    scanned_files = 0
    paths, scan_truncated = _scan_code_files(workspace, target, max_files)
    for path in paths:
        scanned_files += 1
        lines = _read_lines(path)
        if lines is None:
            continue
        display_path = _display_path(workspace, path)
        for line_number, line in enumerate(lines, start=1):
            for match in pattern.finditer(line):
                items.append(
                    {
                        "path": display_path,
                        "line": line_number,
                        "column": match.start() + 1,
                        "preview": line.strip()[:500],
                    }
                )
                if len(items) >= max_results:
                    return {
                        "ok": True,
                        "symbol": name,
                        "references": items,
                        "count": len(items),
                        "scanned_files": scanned_files,
                        "truncated": True,
                        "truncated_by": "max_results",
                        "scan_complete": False,
                    }
    result = {
        "ok": True,
        "symbol": name,
        "references": items,
        "count": len(items),
        "scanned_files": scanned_files,
        "truncated": scan_truncated,
        "scan_complete": not scan_truncated,
    }
    if scan_truncated:
        result["truncated_by"] = "max_files"
    return result
