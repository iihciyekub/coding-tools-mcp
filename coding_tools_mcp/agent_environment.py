"""Read-only discovery of local agent environments and reusable capabilities.

The discovery surface is intentionally metadata-only.  It may parse benign
configuration needed to enumerate enabled plugins, but it never returns auth,
cookie, token, browser-session, keychain, or OAuth contents.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


MAX_DISCOVERY_ITEMS = 500


@dataclass(frozen=True)
class AgentSpec:
    id: str
    display_name: str
    executables: tuple[str, ...]
    homes: tuple[Path, ...]


def _home() -> Path:
    return Path.home()


def _agent_specs() -> tuple[AgentSpec, ...]:
    home = _home()
    return (
        AgentSpec("codex", "Codex", ("codex",), (home / ".codex",)),
        AgentSpec("claude", "Claude Code", ("claude",), (home / ".claude",)),
        AgentSpec("gemini", "Gemini CLI", ("gemini",), (home / ".gemini",)),
        AgentSpec("cursor", "Cursor", ("cursor",), (home / ".cursor",)),
        AgentSpec(
            "opencode",
            "OpenCode",
            ("opencode",),
            (home / ".config" / "opencode", home / ".opencode"),
        ),
    )


def _safe_cli_path(names: Iterable[str]) -> str | None:
    for name in names:
        value = shutil.which(name)
        if value:
            try:
                return str(Path(value).resolve())
            except OSError:
                return value
    return None


def _existing_home(candidates: Iterable[Path]) -> Path | None:
    for path in candidates:
        try:
            if path.is_dir():
                return path.resolve()
        except OSError:
            continue
    return None


def _bounded_names(paths: Iterable[Path], *, root: Path, limit: int) -> tuple[list[str], bool]:
    result: list[str] = []
    truncated = False
    for path in paths:
        if len(result) >= limit:
            truncated = True
            break
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        result.append(relative)
    return result, truncated


def _skill_items(root: Path, *, limit: int) -> tuple[list[dict[str, Any]], bool]:
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return [], False
    items: list[dict[str, Any]] = []
    truncated = False
    try:
        # pathlib does not recurse through symlinked directories.  Include a
        # symlink directory itself when its target exposes SKILL.md, but never
        # walk recursively through that external target.
        candidates = set(skills_root.glob("**/SKILL.md"))
        for child in skills_root.rglob("*"):
            if child.is_symlink() and child.is_dir():
                skill_file = child / "SKILL.md"
                if skill_file.is_file():
                    candidates.add(skill_file)
        ordered = sorted(candidates, key=lambda path: path.as_posix())
    except OSError:
        return [], False
    for skill_file in ordered:
        if len(items) >= limit:
            truncated = True
            break
        try:
            relative = skill_file.relative_to(root).as_posix()
            parent_name = skill_file.parent.name
            is_symlink = skill_file.is_symlink() or skill_file.parent.is_symlink()
        except OSError:
            continue
        items.append({
            "name": parent_name,
            "path": relative,
            "source": "agent_home",
            "portability": "unknown",
            "symlink": is_symlink,
        })
    return items, truncated


def _codex_enabled_plugins(root: Path, *, limit: int) -> tuple[list[dict[str, Any]], bool]:
    config = root / "config.toml"
    if not config.is_file():
        return [], False
    try:
        parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return [], False
    plugins = parsed.get("plugins")
    if not isinstance(plugins, dict):
        return [], False
    items: list[dict[str, Any]] = []
    truncated = False
    for plugin_id in sorted(plugins):
        value = plugins.get(plugin_id)
        if not isinstance(plugin_id, str) or not isinstance(value, dict) or value.get("enabled") is not True:
            continue
        if len(items) >= limit:
            truncated = True
            break
        items.append({"id": plugin_id, "enabled": True})
    return items, truncated


def _codex_plugin_skills(root: Path, *, limit: int) -> tuple[list[dict[str, Any]], bool]:
    config_path = root / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        config = {}
    plugins = config.get("plugins") if isinstance(config, dict) else None
    enabled_ids = {
        plugin_id
        for plugin_id, value in (plugins.items() if isinstance(plugins, dict) else ())
        if isinstance(plugin_id, str) and isinstance(value, dict) and value.get("enabled") is True
    }
    marketplaces = config.get("marketplaces") if isinstance(config, dict) else None
    marketplace_config = marketplaces if isinstance(marketplaces, dict) else {}
    cache = root / "plugins" / "cache"
    items: list[dict[str, Any]] = []
    truncated = False
    manifests: list[tuple[str, Path]] = []
    for plugin_id in sorted(enabled_ids):
        plugin_name, separator, marketplace_id = plugin_id.rpartition("@")
        if not separator or not plugin_name or not marketplace_id:
            continue
        if cache.is_dir():
            try:
                manifests.extend(
                    (plugin_id, candidate)
                    for candidate in sorted((cache / marketplace_id / plugin_name).glob("*/.codex-plugin/plugin.json"))
                    if candidate.is_file()
                )
            except OSError:
                pass
        marketplace = marketplace_config.get(marketplace_id)
        if isinstance(marketplace, dict) and marketplace.get("source_type") == "local":
            source = marketplace.get("source")
            if isinstance(source, str) and source:
                candidate = Path(source).expanduser() / "plugins" / plugin_name / ".codex-plugin" / "plugin.json"
                try:
                    if candidate.is_file():
                        manifests.append((plugin_id, candidate.resolve()))
                except OSError:
                    pass
    seen: set[tuple[str, str]] = set()
    for plugin_id, manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        plugin_name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(plugin_name, str) or not plugin_name:
            continue
        skills_dir = manifest.parent.parent / "skills"
        if not skills_dir.is_dir():
            continue
        try:
            skills = sorted(skills_dir.glob("*/SKILL.md"))
        except OSError:
            continue
        for skill_file in skills:
            identity = (plugin_id, str(skill_file))
            if identity in seen:
                continue
            seen.add(identity)
            if len(items) >= limit:
                truncated = True
                break
            try:
                relative_path: str | None = skill_file.relative_to(root).as_posix()
            except ValueError:
                relative_path = None
            items.append({
                "name": skill_file.parent.name,
                "plugin": plugin_name,
                "plugin_id": plugin_id,
                "path": relative_path or str(skill_file),
                "source": "plugin",
                "portability": "host_specific",
                "external_to_agent_home": relative_path is None,
            })
        if truncated:
            break
    return items, truncated


def _codex_metadata(root: Path, *, limit: int) -> dict[str, Any]:
    skills, skills_truncated = _skill_items(root, limit=limit)
    plugins, plugins_truncated = _codex_enabled_plugins(root, limit=limit)
    plugin_skills, plugin_skills_truncated = _codex_plugin_skills(root, limit=limit)

    worktrees_root = root / "worktrees"
    worktrees: list[str] = []
    worktrees_truncated = False
    if worktrees_root.is_dir():
        try:
            worktrees, worktrees_truncated = _bounded_names(
                (item for item in sorted(worktrees_root.iterdir()) if item.is_dir()),
                root=root,
                limit=limit,
            )
        except OSError:
            pass

    rules_root = root / "rules"
    rules: list[str] = []
    rules_truncated = False
    if rules_root.is_dir():
        try:
            rules, rules_truncated = _bounded_names(
                (item for item in sorted(rules_root.iterdir()) if item.is_file()),
                root=root,
                limit=limit,
            )
        except OSError:
            pass

    sensitive = {
        "auth": (root / "auth.json").exists(),
        "browser_sessions": (root / "browser" / "sessions").exists(),
        "mcp_oauth": (root / "mcp-oauth-locks").exists(),
    }
    enabled_ids = {item["id"] for item in plugins}
    capabilities = {
        "browser_plugin_cached": (root / "plugins" / "cache" / "openai-bundled" / "browser").is_dir(),
        "chrome_plugin_cached": (root / "plugins" / "cache" / "openai-bundled" / "chrome").is_dir(),
        "browser_enabled": "browser@openai-bundled" in enabled_ids,
        "chrome_enabled": "chrome@openai-bundled" in enabled_ids,
    }
    return {
        "skills": skills,
        "skills_truncated": skills_truncated,
        "enabled_plugins": plugins,
        "plugins_truncated": plugins_truncated,
        "plugin_skills": plugin_skills,
        "plugin_skills_truncated": plugin_skills_truncated,
        "worktrees": worktrees,
        "worktrees_truncated": worktrees_truncated,
        "rules": rules,
        "rules_truncated": rules_truncated,
        "root_agents_md": (root / "AGENTS.md").is_file(),
        "capabilities": capabilities,
        "sensitive_resources_present": sensitive,
    }


def _generic_metadata(root: Path, *, limit: int) -> dict[str, Any]:
    skills, skills_truncated = _skill_items(root, limit=limit)
    rules: list[str] = []
    rules_truncated = False
    for candidate in (root / "rules", root / ".rules"):
        if not candidate.is_dir():
            continue
        try:
            rules, rules_truncated = _bounded_names(
                (item for item in sorted(candidate.iterdir()) if item.is_file()),
                root=root,
                limit=limit,
            )
        except OSError:
            pass
        break
    return {
        "skills": skills,
        "skills_truncated": skills_truncated,
        "rules": rules,
        "rules_truncated": rules_truncated,
        "sensitive_resources_present": {
            "auth_like_files": any(
                (root / name).exists()
                for name in ("auth.json", "credentials.json", "tokens.json", "oauth.json")
            )
        },
    }


def discover_agent_environment(*, provider: str = "all", max_items: int = 200) -> dict[str, Any]:
    if max_items < 1 or max_items > MAX_DISCOVERY_ITEMS:
        raise ValueError(f"max_items must be between 1 and {MAX_DISCOVERY_ITEMS}")
    specs = _agent_specs()
    known = {spec.id for spec in specs}
    if provider != "all" and provider not in known:
        raise ValueError(f"unknown provider: {provider}")
    providers: list[dict[str, Any]] = []
    for spec in specs:
        if provider != "all" and spec.id != provider:
            continue
        root = _existing_home(spec.homes)
        cli = _safe_cli_path(spec.executables)
        installed = root is not None or cli is not None
        item: dict[str, Any] = {
            "id": spec.id,
            "display_name": spec.display_name,
            "installed": installed,
            "home": str(root) if root is not None else None,
            "cli": cli,
        }
        if root is not None:
            item.update(_codex_metadata(root, limit=max_items) if spec.id == "codex" else _generic_metadata(root, limit=max_items))
        providers.append(item)
    return {
        "ok": True,
        "providers": providers,
        "count": len(providers),
        "metadata_only": True,
        "sensitive_contents_read": False,
        "summary": f"Discovered metadata for {len(providers)} local agent environment(s).",
    }

