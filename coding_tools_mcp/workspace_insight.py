from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from . import code_intel
from .project_context import ProjectContext
from .repositories import git_environment


MANIFEST_NAMES = {
    "Cargo.toml": "rust",
    "go.mod": "go",
    "package.json": "node",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.cfg": "python",
    "setup.py": "python",
}
LANGUAGE_SUFFIXES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".go": "Go",
    ".h": "C/C++ header",
    ".hpp": "C++ header",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript JSX",
    ".kt": "Kotlin",
    ".py": "Python",
    ".pyi": "Python typing",
    ".rs": "Rust",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript JSX",
}
ENTRYPOINT_NAMES = {
    "Dockerfile",
    "Makefile",
    "compose.yaml",
    "docker-compose.yml",
    "main.py",
    "manage.py",
}
MAP_EXCLUDED_PARTS = frozenset({".venv", "_internal", "node_modules", "vendor"})
FINGERPRINT_MAX_BYTES = 128 * 1024 * 1024


def _git_files(workspace: Path, *, max_files: int) -> tuple[list[Path], bool]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "ls-files", "-co", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            env=git_environment(dict(os.environ)),
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        raw = sorted(set(item for item in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0") if item))
        truncated = len(raw) > max_files
        git_paths = [workspace / item for item in raw[:max_files]]
        return git_paths, truncated

    fallback_paths: list[Path] = []
    excluded = code_intel.EXCLUDED_DIRS
    for current, directories, files in os.walk(workspace, followlinks=False):
        directories[:] = sorted(name for name in directories if not name.startswith(".") and name not in excluded)
        for name in sorted(files):
            path = Path(current) / name
            if not path.is_file() or path.is_symlink():
                continue
            fallback_paths.append(path)
            if len(fallback_paths) > max_files:
                return sorted(fallback_paths[:max_files]), True
    return sorted(fallback_paths), False


def workspace_overview(workspace: Path, context: ProjectContext, args: dict[str, Any], *, target: Path | None = None) -> dict[str, Any]:
    target = target or workspace
    max_files = int(args.get("max_files", 20_000))
    files, truncated = _git_files(target, max_files=max_files)
    manifests: list[dict[str, str]] = []
    entrypoints: list[str] = []
    languages: Counter[str] = Counter()
    directories: Counter[str] = Counter()
    for path in files:
        try:
            rel = path.relative_to(workspace).as_posix()
        except ValueError:
            continue
        if path.name in MANIFEST_NAMES:
            manifests.append({"path": rel, "ecosystem": MANIFEST_NAMES[path.name]})
        if path.name in ENTRYPOINT_NAMES or path.name.startswith("README"):
            entrypoints.append(rel)
        language = LANGUAGE_SUFFIXES.get(path.suffix.lower())
        if language:
            languages[language] += 1
        top = rel.split("/", 1)[0]
        directories[top] += 1
    return {
        "ok": True,
        "workspace": str(workspace),
        "path": target.relative_to(workspace).as_posix(),
        "files_scanned": len(files),
        "scan_complete": not truncated,
        "truncated": truncated,
        "manifests": sorted(manifests, key=lambda item: item["path"]),
        "entrypoints": sorted(entrypoints)[:100],
        "languages": [
            {"language": name, "files": count}
            for name, count in languages.most_common()
        ],
        "top_level": [
            {"path": name, "files": count}
            for name, count in sorted(directories.items(), key=lambda item: (-item[1], item[0]))[:100]
        ],
        "instructions": {
            "root": [item.path for item in context.root_files],
            "nested": list(context.nested_files),
            "warnings": list(context.warnings),
        },
        "summary": f"Scanned {len(files)} workspace files and found {len(manifests)} project manifests.",
    }


def workspace_fingerprint(workspace: Path, target: Path, *, max_files: int = 20_000) -> dict[str, Any]:
    files, truncated = _git_files(target, max_files=max_files)
    digest = hashlib.sha256()
    digest.update(target.relative_to(workspace).as_posix().encode("utf-8", errors="surrogateescape") + b"\0")
    hashed_bytes = 0
    file_count = 0
    complete = not truncated
    for path in files:
        try:
            path.relative_to(target)
            path.resolve(strict=False).relative_to(workspace)
        except ValueError:
            complete = False
            continue
        try:
            rel = path.relative_to(workspace).as_posix()
            stat_result = path.stat()
        except OSError:
            complete = False
            continue
        digest.update(rel.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if not path.is_file() or path.is_symlink():
            complete = False
            digest.update(f"metadata:{stat_result.st_size}:{stat_result.st_mtime_ns}".encode())
            continue
        if hashed_bytes + stat_result.st_size > FINGERPRINT_MAX_BYTES:
            complete = False
            digest.update(f"metadata:{stat_result.st_size}:{stat_result.st_mtime_ns}".encode())
            continue
        try:
            content = path.read_bytes()
        except OSError:
            complete = False
            continue
        digest.update(content)
        digest.update(b"\0")
        hashed_bytes += len(content)
        file_count += 1
    return {
        "fingerprint": digest.hexdigest(),
        "path": target.relative_to(workspace).as_posix(),
        "scan_complete": complete,
        "files_hashed": file_count,
        "bytes_hashed": hashed_bytes,
    }


def repo_map(workspace: Path, target: Path, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    max_files = int(args.get("max_files", 2_000))
    max_symbols = int(args.get("max_symbols", 300))
    query_words = {word.lower() for word in re.findall(r"[A-Za-z0-9_$]+", query) if len(word) > 1}
    queries = sorted(query_words, key=lambda word: (-len(word), word))[:8] or [""]
    candidate_limit = min(5_000, max(500, max_symbols * 20))
    symbols_by_location: dict[tuple[str, int, str], dict[str, Any]] = {}
    scanned_files = 0
    source_truncated = False
    scan_complete = True
    for word in queries:
        symbol_result = code_intel.symbols(
            workspace,
            target,
            {"query": word, "max_files": max_files, "max_results": candidate_limit},
        )
        scanned_files = max(scanned_files, int(symbol_result.get("scanned_files", 0)))
        source_truncated = source_truncated or bool(symbol_result.get("truncated", False))
        scan_complete = scan_complete and bool(
            symbol_result.get("scan_complete", not symbol_result.get("truncated", False))
        )
        for item in symbol_result.get("symbols", []):
            if isinstance(item, dict):
                if any(part in MAP_EXCLUDED_PARTS for part in Path(str(item.get("path", ""))).parts):
                    continue
                key = (str(item.get("path", "")), int(item.get("line", 0)), str(item.get("qualified_name", "")))
                symbols_by_location[key] = item
    symbols = list(symbols_by_location.values())

    def score(item: dict[str, Any]) -> tuple[int, str, int]:
        haystack = f"{item.get('path', '')} {item.get('qualified_name', '')}".lower()
        match_score = sum(5 for word in query_words if word in haystack)
        return (-match_score, str(item.get("path", "")), int(item.get("line", 0)))

    symbols.sort(key=score)
    candidates_truncated = len(symbols) > max_symbols
    symbols = symbols[:max_symbols]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in symbols:
        path = str(item.get("path", ""))
        grouped.setdefault(path, []).append(
            {
                "name": item.get("name"),
                "qualified_name": item.get("qualified_name"),
                "kind": item.get("kind"),
                "line": item.get("line"),
                "end_line": item.get("end_line"),
                "backend": item.get("backend", "ast" if path.endswith((".py", ".pyi")) else "pattern"),
            }
        )
    files = [{"path": path, "symbols": items} for path, items in grouped.items()]
    return {
        "ok": True,
        "query": query or None,
        "root": target.relative_to(workspace).as_posix() if target != workspace else ".",
        "files": files,
        "file_count": len(files),
        "symbol_count": len(symbols),
        "scanned_files": scanned_files,
        "coverage": "python_ast_and_language_patterns",
        "scan_complete": scan_complete,
        "truncated": bool(source_truncated or candidates_truncated),
        "summary": f"Mapped {len(symbols)} symbols across {len(files)} files.",
    }


def discover_checks(workspace: Path, target: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(check_id: str, command: str, source: str, kind: str) -> None:
        if check_id in seen:
            return
        seen.add(check_id)
        checks.append({"id": check_id, "command": command, "workdir": _rel(workspace, target), "source": source, "kind": kind})

    makefile = target / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        targets = set(re.findall(r"^([A-Za-z0-9_.-]+):(?:\s|$)", text, flags=re.MULTILINE))
        for name, kind in (("test", "test"), ("lint", "lint"), ("typecheck", "typecheck"), ("ci", "aggregate"), ("check", "aggregate")):
            if name in targets:
                add(f"make:{name}", f"make {name}", "Makefile", kind)

    package = target / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            for name in ("test", "lint", "typecheck", "check", "build"):
                if isinstance(scripts.get(name), str):
                    add(f"npm:{name}", f"npm run {name}", "package.json", name)

    pyproject = target / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            data = {}
        if isinstance(data.get("tool"), dict):
            tool = data["tool"]
            if "pytest" in tool:
                add("python:pytest", "python -m pytest", "pyproject.toml", "test")
            if "ruff" in tool:
                add("python:ruff", "python -m ruff check .", "pyproject.toml", "lint")
            if "mypy" in tool:
                add("python:mypy", "python -m mypy .", "pyproject.toml", "typecheck")
    if (target / "pytest.ini").is_file() or (target / "conftest.py").is_file():
        add("python:pytest", "python -m pytest", "pytest configuration", "test")
    if (target / "Cargo.toml").is_file():
        add("cargo:check", "cargo check", "Cargo.toml", "typecheck")
        add("cargo:test", "cargo test", "Cargo.toml", "test")
    if (target / "go.mod").is_file():
        add("go:test", "go test ./...", "go.mod", "test")
    return checks


def _rel(workspace: Path, path: Path) -> str:
    rel = path.relative_to(workspace).as_posix()
    return rel or "."
