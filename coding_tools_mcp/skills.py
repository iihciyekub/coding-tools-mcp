from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .errors import ToolFailure


MAX_SKILL_BYTES = 128 * 1024


def list_skills(workspace: Path, *, max_results: int = 200) -> dict[str, Any]:
    candidates = sorted(workspace.glob(".agents/skills/**/SKILL.md"))
    items: list[dict[str, Any]] = []
    for path in candidates[:max_results]:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(workspace)
            raw = resolved.read_bytes()
        except (OSError, ValueError):
            continue
        text = raw[:MAX_SKILL_BYTES].decode("utf-8", errors="replace")
        metadata = _frontmatter(text)
        items.append(
            {
                "name": metadata.get("name") or path.parent.name,
                "description": metadata.get("description") or "",
                "path": path.relative_to(workspace).as_posix(),
                "scope": "workspace",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "truncated": len(raw) > MAX_SKILL_BYTES,
            }
        )
    return {
        "ok": True,
        "skills": items,
        "count": len(items),
        "truncated": len(candidates) > max_results,
        "summary": f"Found {len(items)} workspace skills.",
    }


def read_skill(workspace: Path, path: Path) -> dict[str, Any]:
    parts = path.relative_to(workspace).parts
    valid_scope = len(parts) >= 4 and parts[0] == ".agents" and parts[1] == "skills"
    if path.name != "SKILL.md" or not valid_scope:
        raise ToolFailure("INVALID_ARGUMENT", "Skill path must name a workspace .agents/skills/**/SKILL.md file.", category="validation")
    raw = path.read_bytes()
    if len(raw) > MAX_SKILL_BYTES:
        raise ToolFailure(
            "OUTPUT_TOO_LARGE",
            "Skill exceeds the supported size.",
            category="validation",
            details={"bytes": len(raw), "max_bytes": MAX_SKILL_BYTES},
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolFailure("UNSUPPORTED_ENCODING", "Skill is not valid UTF-8.", category="validation") from exc
    return {
        "ok": True,
        "path": path.relative_to(workspace).as_posix(),
        "metadata": _frontmatter(content),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "summary": f"Read skill {path.parent.name}.",
    }


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^(name|description):\s*(.*)$", line)
        if match:
            metadata[match.group(1)] = match.group(2).strip().strip('"\'')
    return metadata
