from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from . import apple_toolchain, code_intel
from .project_context import ProjectContext
from .repositories import git_environment


MANIFEST_NAMES = {
    "Cargo.toml": "rust",
    "go.mod": "go",
    "package.json": "node",
    "Package.swift": "swift",
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


def workspace_overview(
    workspace: Path,
    context: ProjectContext,
    args: dict[str, Any],
    *,
    target: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    target = target or workspace
    max_files = int(args.get("max_files", 20_000))
    files, truncated = _git_files(target, max_files=max_files)
    manifests: list[dict[str, str]] = []
    entrypoints: list[str] = []
    languages: Counter[str] = Counter()
    directories: Counter[str] = Counter()
    xcode_projects: set[str] = set()
    xcode_workspaces: set[str] = set()
    swift_packages: set[str] = set()
    for path in files:
        try:
            rel = path.relative_to(workspace).as_posix()
        except ValueError:
            continue
        if path.name in MANIFEST_NAMES:
            manifests.append({"path": rel, "ecosystem": MANIFEST_NAMES[path.name]})
            if path.name == "Package.swift":
                swift_packages.add(rel)
        parts = Path(rel).parts
        for index, part in enumerate(parts):
            if part.endswith(".xcodeproj"):
                xcode_projects.add(Path(*parts[: index + 1]).as_posix())
            elif part.endswith(".xcworkspace"):
                xcode_workspaces.add(Path(*parts[: index + 1]).as_posix())
        if path.name in ENTRYPOINT_NAMES or path.name.startswith("README"):
            entrypoints.append(rel)
        language = LANGUAGE_SUFFIXES.get(path.suffix.lower())
        if language:
            languages[language] += 1
        top = rel.split("/", 1)[0]
        directories[top] += 1

    apple_detected = bool(xcode_projects or xcode_workspaces or swift_packages or languages.get("Swift"))
    apple: dict[str, Any] = {
        "detected": apple_detected,
        "xcodeproj": sorted(xcode_projects),
        "xcworkspace": sorted(xcode_workspaces),
        "swift_packages": sorted(swift_packages),
    }
    if apple_detected:
        apple.update(
            apple_toolchain.probe(
                cwd=target,
                env=dict(env or os.environ),
                include_sdks=True,
            )
        )

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
        "apple": apple,
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






def _is_test_path(path: str) -> bool:
    lower = path.casefold()
    parts = Path(lower).parts
    name = Path(lower).name
    return (
        any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or ".test." in name
        or ".spec." in name
        or name.endswith("tests.swift")
    )


def _impact_language_family(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        return "python"
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return "javascript"
    if suffix == ".rs":
        return "rust"
    if suffix == ".go":
        return "go"
    if suffix == ".swift":
        return "swift"
    if suffix in {".java", ".kt", ".kts"}:
        return "jvm"
    if suffix in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}:
        return "c_family"
    return None




def discover_checks(
    workspace: Path,
    target: Path,
    *,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
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
    if (target / "Package.swift").is_file():
        add("swift:build", "swift build", "Package.swift", "build")
        add("swift:test", "swift test", "Package.swift", "test")

    command_env = dict(env or os.environ)
    path_value = command_env.get("PATH") or command_env.get("Path") or ""
    xcodebuild = shutil.which("xcodebuild", path=path_value)
    xcode_containers = sorted(
        [*target.glob("*.xcworkspace"), *target.glob("*.xcodeproj")],
        key=lambda path: path.name,
    )
    if xcodebuild and len(xcode_containers) == 1:
        container = xcode_containers[0]
        flag = "-workspace" if container.suffix == ".xcworkspace" else "-project"
        command = f"{shlex.quote(xcodebuild)} {flag} {shlex.quote(container.name)} -list -json"
        add("xcode:list", command, container.name, "metadata")
    return checks


CHECK_ECOSYSTEM_BY_LANGUAGE = {
    "python": {"python"},
    "javascript": {"npm"},
    "rust": {"cargo"},
    "go": {"go"},
    "swift": {"swift"},
    "c_family": {"xcode"},
}


def _check_change_ecosystems(path: str) -> set[str]:
    name = Path(path).name
    lower = path.casefold()
    result: set[str] = set()
    language = _impact_language_family(path)
    if language:
        result.update(CHECK_ECOSYSTEM_BY_LANGUAGE.get(language, set()))
    if name in {"pyproject.toml", "pytest.ini", "mypy.ini", "ruff.toml", ".ruff.toml"} or name.startswith(
        "requirements"
    ):
        result.add("python")
    if name in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"}:
        result.add("npm")
    if name in {"Cargo.toml", "Cargo.lock"}:
        result.add("cargo")
    if name in {"go.mod", "go.sum"}:
        result.add("go")
    if name in {"Package.swift", "Package.resolved"}:
        result.add("swift")
    if name == "Makefile" or name.endswith(".mk"):
        result.add("make")
    if ".xcodeproj/" in lower or ".xcworkspace/" in lower or name == "project.pbxproj":
        result.add("xcode")
    return result


def recommend_checks(checks: list[dict[str, Any]], changed_paths: list[str]) -> list[dict[str, Any]]:
    """Rank existing discovered checks from bounded changed-path metadata only."""

    changed = list(dict.fromkeys(path for path in changed_paths if path))[:100]
    changed_ecosystems: set[str] = set()
    for path in changed:
        changed_ecosystems.update(_check_change_ecosystems(path))
    code_paths = [path for path in changed if _impact_language_family(path) is not None]
    test_paths = [path for path in code_paths if _is_test_path(path)]
    xcode_config_changed = "xcode" in changed_ecosystems

    ranked: list[dict[str, Any]] = []
    for check in checks:
        item = dict(check)
        check_id = str(item.get("id", ""))
        ecosystem = check_id.split(":", 1)[0]
        kind = str(item.get("kind", ""))
        score = 0
        reasons: list[str] = []

        if ecosystem in changed_ecosystems:
            score += 60
            reasons.append(f"changed paths affect the {ecosystem} ecosystem")
        elif ecosystem == "make" and code_paths:
            score += 25
            reasons.append("Make checks are a cross-language fallback for changed source files")

        if code_paths and ecosystem in changed_ecosystems:
            if kind in {"lint", "typecheck"}:
                score += 20
                reasons.append(f"{kind} is a fast validation for changed source files")
            elif kind == "test":
                score += 10
                reasons.append("tests validate behavior affected by changed source files")
            elif kind == "build":
                score += 5
                reasons.append("build validation covers changed source files")

        if test_paths and kind == "test" and ecosystem in changed_ecosystems:
            score += 25
            reasons.append("test files changed directly")

        if kind == "aggregate" and score:
            score = max(1, score - 20)
            reasons.append("aggregate checks are broader, so run focused checks first")

        if kind == "metadata" and ecosystem == "xcode":
            if xcode_config_changed:
                score = max(score, 80)
                reasons.append("Xcode project/workspace metadata changed")
            else:
                score = 0
                reasons = []

        priority = "high" if score >= 90 else "medium" if score >= 60 else "low" if score >= 40 else "none"
        item.update(
            recommended=score >= 40,
            priority=priority,
            recommendation_score=score,
            recommendation_reasons=reasons,
        )
        ranked.append(item)

    ranked.sort(
        key=lambda item: (
            -int(item.get("recommendation_score", 0)),
            str(item.get("id", "")),
        )
    )
    return ranked


def _rel(workspace: Path, path: Path) -> str:
    rel = path.relative_to(workspace).as_posix()
    return rel or "."
