from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTEXT_FILE_NAMES = frozenset({"AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"})
SKIPPED_CONTEXT_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".reference",
        "node_modules",
        "target",
        "dist",
        "build",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
    }
)
MAX_ROOT_CONTEXT_BYTES = 32 * 1024
MAX_CONTEXT_FILE_BYTES = 16 * 1024
MAX_NESTED_CONTEXT_FILES = 64
MAX_CONTEXT_SCAN_FILES = 20_000
MAX_CONTEXT_SCAN_DEPTH = 12
MAX_APPLICABLE_CONTEXT_BYTES = 64 * 1024


def instructions_for_path(root: Path, target: Path) -> dict[str, Any]:
    """Read applicable rules now, independently of the startup directory scan."""
    root = root.resolve(strict=True)
    target = target.resolve(strict=False)
    directory = target if target.is_dir() else target.parent
    directory.relative_to(root)
    chain = [directory]
    while chain[-1] != root:
        chain.append(chain[-1].parent)
    instructions: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[tuple[int, int]] = set()
    remaining = MAX_APPLICABLE_CONTEXT_BYTES
    for current in reversed(chain):
        for name in ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"):
            path = current / name
            if not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                info = resolved.stat()
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    continue
                if remaining <= 0:
                    warnings.append("Applicable instruction byte budget exhausted; read remaining rules explicitly.")
                    break
                budget = min(MAX_CONTEXT_FILE_BYTES, remaining)
                with resolved.open("rb") as handle:
                    raw = handle.read(budget + 1)
                content = _decode_utf8_prefix(raw[:budget])
            except (OSError, ValueError) as exc:
                warnings.append(f"Skipped unsafe or unreadable instruction {path.relative_to(root)}: {exc}")
                continue
            seen.add(identity)
            truncated = len(raw) > budget
            remaining -= len(content.encode("utf-8"))
            instructions.append({
                "path": path.relative_to(root).as_posix(), "content": content,
                "truncated": truncated, "scope": current.relative_to(root).as_posix(),
            })
            if truncated:
                warnings.append(f"Instruction truncated: {path.relative_to(root)}")
        if remaining <= 0:
            if not any("budget exhausted" in warning for warning in warnings):
                warnings.append("Applicable instruction byte budget exhausted; read remaining rules explicitly.")
            break
    return {
        "ok": True, "path": target.relative_to(root).as_posix(),
        "instructions": instructions, "count": len(instructions), "warnings": warnings,
        "summary": f"Resolved {len(instructions)} applicable instruction files from the current filesystem.",
    }


@dataclass(frozen=True)
class LoadedContextFile:
    path: str
    content: str
    truncated: bool


@dataclass(frozen=True)
class ProjectContext:
    root_files: tuple[LoadedContextFile, ...]
    nested_files: tuple[str, ...]
    warnings: tuple[str, ...]

    def server_instructions(self) -> str:
        sections = [
            "Keep project, Git, LSP, workflow, and project-instruction operations anchored to the configured workspace.",
            "Use apply_patch for local file modifications within the configured file scope; do not use exec_command as a substitute for local patching.",
        ]
        for item in self.root_files:
            suffix = " [truncated]" if item.truncated else ""
            sections.append(f"Project instructions from {item.path}{suffix}:\n{item.content}")
        if self.nested_files:
            paths = "\n".join(f"- {path}" for path in self.nested_files)
            sections.append(
                "Nested project instruction files are available below. Before modifying files under one of their "
                f"directories, read the applicable instruction file with read_file:\n{paths}"
            )
        if self.warnings:
            sections.append("Project-context warnings:\n" + "\n".join(f"- {warning}" for warning in self.warnings))
        return "\n\n".join(sections)


def load_project_context(root: Path) -> ProjectContext:
    resolved_root = root.expanduser().resolve(strict=True)
    loaded: list[LoadedContextFile] = []
    loaded_file_ids: set[tuple[int, int]] = set()
    warnings: list[str] = []
    remaining = MAX_ROOT_CONTEXT_BYTES
    for name in ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"):
        path = resolved_root / name
        if not path.is_file():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            warnings.append(f"Skipped unsafe root instruction path: {name}")
            continue
        try:
            file_id = (resolved.stat().st_dev, resolved.stat().st_ino)
        except OSError as exc:
            warnings.append(f"Could not stat {name}: {exc}")
            continue
        if file_id in loaded_file_ids:
            continue
        if remaining <= 0:
            warnings.append("Root instruction byte limit reached.")
            break
        budget = min(MAX_CONTEXT_FILE_BYTES, remaining)
        try:
            with resolved.open("rb") as handle:
                data = handle.read(budget + 1)
            content = _decode_utf8_prefix(data[:budget])
        except UnicodeDecodeError:
            warnings.append(f"Skipped non-UTF-8 instruction file: {name}")
            continue
        except OSError as exc:
            warnings.append(f"Could not read {name}: {exc}")
            continue
        truncated = len(data) > budget
        loaded.append(LoadedContextFile(name, content, truncated))
        loaded_file_ids.add(file_id)
        remaining -= len(content.encode("utf-8"))

    loaded_names = {item.path for item in loaded}
    nested: list[str] = []
    for candidate in _discover_context_files(resolved_root, warnings):
        if candidate in loaded_names:
            continue
        try:
            candidate_stat = (resolved_root / candidate).stat()
        except OSError:
            continue
        if (candidate_stat.st_dev, candidate_stat.st_ino) in loaded_file_ids:
            continue
        nested.append(candidate)
    if len(nested) > MAX_NESTED_CONTEXT_FILES:
        nested = nested[:MAX_NESTED_CONTEXT_FILES]
        warnings.append(f"Nested instruction list truncated to {MAX_NESTED_CONTEXT_FILES} files.")
    return ProjectContext(tuple(loaded), tuple(nested), tuple(warnings))


def _discover_context_files(root: Path, warnings: list[str]) -> list[str]:
    git_paths = _git_context_files(root)
    if git_paths is not None:
        return git_paths
    discovered: list[str] = []
    scanned = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in SKIPPED_CONTEXT_DIRS and depth < MAX_CONTEXT_SCAN_DEPTH
        )
        for name in sorted(files):
            scanned += 1
            if scanned > MAX_CONTEXT_SCAN_FILES:
                warnings.append(f"Project-context scan stopped after {MAX_CONTEXT_SCAN_FILES} files.")
                return discovered
            if name not in CONTEXT_FILE_NAMES:
                continue
            path = current_path / name
            if path.is_symlink():
                continue
            discovered.append(path.relative_to(root).as_posix())
    return discovered


def _git_context_files(root: Path) -> list[str] | None:
    pathspecs = sorted(CONTEXT_FILE_NAMES) + [
        f":(glob)**/{name}" for name in sorted(CONTEXT_FILE_NAMES)
    ]
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
                "--",
                *pathspecs,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    paths: list[str] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        parts = Path(path).parts
        if len(parts) > MAX_CONTEXT_SCAN_DEPTH + 1 or any(part in SKIPPED_CONTEXT_DIRS for part in parts[:-1]):
            continue
        if parts and parts[-1] in CONTEXT_FILE_NAMES:
            paths.append(Path(path).as_posix())
        if len(paths) >= MAX_NESTED_CONTEXT_FILES + 1:
            break
    return sorted(set(paths))


def _decode_utf8_prefix(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data":
            raise
        return data[: exc.start].decode("utf-8")
