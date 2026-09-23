"""Read-only, explicitly scoped local Agent Skill and plugin catalog."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


_MAX_VISITED = 4_000
_MAX_DEPTH = 8
_MAX_SKILL_BYTES = 128 * 1024
_MAX_REFERENCE_BYTES = 48 * 1024
_MAX_TOTAL_REFERENCE_BYTES = 144 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class _Entry:
    id: str
    kind: str
    name: str
    description: str
    source: str
    path: Path
    root: Path
    revision: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "revision": self.revision,
            "local_status": "readable" if self.kind == "skill" else "metadata_only",
        }


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "category": "validation",
            "retryable": False,
            "details": {},
        },
    }


def _source(path: Path) -> str:
    parts = set(path.parts)
    for marker, label in (
        (".codex", "codex"),
        (".claude", "claude-code"),
        (".cursor", "cursor"),
        (".gemini", "gemini-cli"),
        (".agents", "agent-skills"),
    ):
        if marker in parts:
            return label
    return "custom"


def _frontmatter_field(lines: list[str], key: str) -> str | None:
    prefix = f"{key}:"
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if value in {"|", "|-", ">", ">-"}:
            following: list[str] = []
            for next_line in lines[index + 1:]:
                if next_line and not next_line[0].isspace():
                    break
                following.append(next_line.strip())
            return ("\n" if value.startswith("|") else " ").join(following).strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                return json.loads(value)
            except ValueError:
                return value[1:-1]
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1].replace("''", "'")
        return value
    return None


def _metadata(path: Path, kind: str) -> tuple[str, str] | None:
    try:
        if path.is_symlink() or path.stat().st_size > (
            _MAX_SKILL_BYTES if kind == "skill" else _MAX_MANIFEST_BYTES
        ):
            return None
        if kind == "skill":
            with path.open("r", encoding="utf-8") as stream:
                prefix = stream.read(12_000)
            if not prefix.startswith("---\n"):
                return None
            closing = prefix.find("\n---", 4)
            if closing < 0:
                return None
            frontmatter = prefix[4:closing].splitlines()
            document = {
                "name": _frontmatter_field(frontmatter, "name"),
                "description": _frontmatter_field(frontmatter, "description"),
            }
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return None
        name = document.get("name")
        description = document.get("description")
        if not isinstance(name, str) or not name.strip():
            return None
        return name.strip()[:160], description.strip()[:600] if isinstance(description, str) else ""
    except (OSError, UnicodeError, ValueError):
        return None


class LocalCapabilityCatalog:
    """Search only directories authorized at gateway startup, never all of HOME."""

    def __init__(self, roots: Sequence[Path | str]) -> None:
        resolved: list[Path] = []
        home = Path.home().resolve()
        for raw in roots:
            path = Path(raw).expanduser().resolve(strict=True)
            if not path.is_dir() or path == home or path == Path(path.anchor):
                raise ValueError(f"Local capability root must be a specific directory: {raw}")
            if path not in resolved:
                resolved.append(path)
        self.roots = tuple(resolved)
        self._lock = threading.Lock()
        self._cache_until = 0.0
        self._entries: tuple[_Entry, ...] = ()
        self._truncated = False

    def _scan(self) -> tuple[_Entry, ...]:
        entries: dict[str, _Entry] = {}
        visited = 0
        self._truncated = False
        for root in self.roots:
            for base, directories, files in os.walk(root, followlinks=False):
                current = Path(base)
                depth = len(current.relative_to(root).parts)
                directories[:] = sorted(
                    name for name in directories
                    if not (current / name).is_symlink()
                    and depth < _MAX_DEPTH
                    and name not in {".git", "node_modules", "target", "__pycache__", ".venv"}
                )
                visited += 1
                if visited > _MAX_VISITED:
                    self._truncated = True
                    break
                candidates = []
                if "SKILL.md" in files:
                    candidates.append((current / "SKILL.md", "skill"))
                if "plugin.json" in files and (current == root or current.name == ".codex-plugin" or "plugins" in current.parts):
                    candidates.append((current / "plugin.json", "plugin"))
                for path, kind in candidates:
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    metadata = _metadata(path, kind)
                    if metadata is None:
                        continue
                    name, description = metadata
                    identity = f"{kind}\0{root}\0{path.relative_to(root)}"
                    entry_id = f"{kind}:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    entries[entry_id] = _Entry(
                        entry_id, kind, name, description, _source(path), path, root,
                        f"{stat.st_mtime_ns:x}-{stat.st_size:x}",
                    )
            if visited > _MAX_VISITED:
                break
        return tuple(sorted(entries.values(), key=lambda entry: (entry.kind, entry.name.casefold(), entry.source, entry.id)))

    def _current(self) -> tuple[_Entry, ...]:
        with self._lock:
            if time.monotonic() >= self._cache_until:
                self._entries = self._scan()
                self._cache_until = time.monotonic() + _CACHE_SECONDS
            return self._entries

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip().casefold()
        if len(query) > 200:
            return _error("INVALID_ARGUMENT", "query is too long.")
        words = re.findall(r"\w+", query)
        kind = str(args.get("kind") or "").strip()
        if kind not in {"", "skill", "plugin"}:
            return _error("INVALID_ARGUMENT", "kind must be skill or plugin.")
        try:
            limit = min(max(int(args.get("limit", 10)), 1), 25)
            offset = int(args.get("cursor") or 0)
        except (TypeError, ValueError):
            return _error("INVALID_ARGUMENT", "limit and cursor must be integers.")
        if offset < 0 or offset > 10_000:
            return _error("INVALID_ARGUMENT", "cursor is out of range.")
        found = [
            entry for entry in self._current()
            if (not kind or entry.kind == kind)
            and all(word in re.sub(r"[-_]", " ", f"{entry.name} {entry.description} {entry.source}".casefold()) for word in words)
        ]
        if words:
            requested_name = " ".join(words)
            found.sort(key=lambda entry: (
                0 if re.sub(r"[-_]", " ", entry.name.casefold()) == requested_name else
                1 if re.sub(r"[-_]", " ", entry.name.casefold()).startswith(requested_name) else 2,
                entry.name.casefold(), entry.source, entry.id,
            ))
        selected = found[offset:offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(found) else None
        lines = [f"{entry.kind} {entry.name} ({entry.source}, {entry.id}): {entry.description}" for entry in selected]
        return {
            "ok": True,
            "items": [entry.public() for entry in selected],
            "count": len(selected),
            "total": len(found),
            "catalog_truncated": self._truncated,
            "next_cursor": next_cursor,
            "summary": "\n".join(lines) if lines else "No matching local capabilities in authorized directories.",
        }

    def _entry(self, raw_id: Any, kind: str) -> _Entry | None:
        return next((entry for entry in self._current() if entry.id == raw_id and entry.kind == kind), None)

    def read_skill(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self._entry(args.get("id"), "skill")
        if entry is None:
            return _error("CAPABILITY_NOT_FOUND", "This Skill is not in the authorized catalog. Search again.")
        try:
            if entry.path.is_symlink() or not entry.path.resolve().is_relative_to(entry.root):
                return _error("CAPABILITY_NOT_FOUND", "Skill path is no longer authorized.")
            raw = entry.path.read_bytes()
            if len(raw) > _MAX_SKILL_BYTES:
                return _error("SKILL_TOO_LARGE", "SKILL.md exceeds the read limit.")
            body = raw.decode("utf-8")
        except (OSError, UnicodeError):
            return _error("SKILL_UNREADABLE", "SKILL.md cannot be read as UTF-8.")
        requested = args.get("resources") or []
        if not isinstance(requested, list) or len(requested) > 3 or any(not isinstance(item, str) for item in requested):
            return _error("INVALID_ARGUMENT", "resources must be a list of at most three relative text paths.")
        resources = []
        total = 0
        for relative in requested:
            relative_path = Path(relative)
            path = entry.path.parent / relative_path
            if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.parts[:1] != ("references",):
                return _error("RESOURCE_NOT_ALLOWED", "Only text files under this Skill's references/ directory may be read.")
            if path.is_symlink() or not path.resolve().is_relative_to(entry.path.parent) or not path.is_file():
                return _error("RESOURCE_NOT_ALLOWED", "Reference path is unavailable or outside this Skill.")
            if path.suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv"}:
                return _error("RESOURCE_NOT_ALLOWED", "Reference must be a supported text file.")
            try:
                raw_resource = path.read_bytes()
                if len(raw_resource) > _MAX_REFERENCE_BYTES or total + len(raw_resource) > _MAX_TOTAL_REFERENCE_BYTES:
                    return _error("RESOURCE_TOO_LARGE", "Requested references exceed the read limit.")
                resources.append({"path": relative, "content": raw_resource.decode("utf-8")})
                total += len(raw_resource)
            except (OSError, UnicodeError):
                return _error("RESOURCE_UNREADABLE", "Reference cannot be read as UTF-8.")
        revision = hashlib.sha256(raw).hexdigest()
        text = f"Skill: {entry.name} ({entry.source})\nRevision: {revision}\n\n{body}"
        for resource in resources:
            text += f"\n\nReference: {resource['path']}\n{resource['content']}"
        return {
            "ok": True,
            **entry.public(),
            "revision": revision,
            "content": body,
            "resources": resources,
            "summary": text,
        }

    def inspect_plugin(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = self._entry(args.get("id"), "plugin")
        if entry is None:
            return _error("CAPABILITY_NOT_FOUND", "This plugin is not in the authorized catalog. Search again.")
        if entry.path.is_symlink() or not entry.path.resolve().is_relative_to(entry.root):
            return _error("CAPABILITY_NOT_FOUND", "Plugin path is no longer authorized.")
        metadata = _metadata(entry.path, "plugin")
        if metadata is None:
            return _error("PLUGIN_UNREADABLE", "Plugin manifest is unavailable or invalid.")
        name, description = metadata
        root = entry.path.parent.parent if entry.path.parent.name == ".codex-plugin" else entry.path.parent
        skill_names = sorted({item.name for item in self._current() if item.kind == "skill" and item.path.is_relative_to(root)})
        return {
            "ok": True,
            **entry.public(),
            "name": name,
            "description": description,
            "skills": skill_names[:50],
            "local_status": "metadata_only",
            "summary": f"Local plugin {name}: {description}\nSkill names: {', '.join(skill_names[:50]) or 'none found'}. Manifest metadata only; this does not make its actions available in ChatGPT.",
        }
