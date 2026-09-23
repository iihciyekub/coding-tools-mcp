"""Gateway catalog must stay inside explicitly authorized Skill roots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_tools_mcp.local_capabilities import LocalCapabilityCatalog


def _skill(root: Path, name: str = "paper-review") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: paper-review\ndescription: Review a paper using project evidence.\n---\n\nRead references/checklist.md.\n",
        encoding="utf-8",
    )
    (skill / "references").mkdir()
    (skill / "references" / "checklist.md").write_text("Check citations.\n", encoding="utf-8")
    return skill


def test_catalog_finds_and_reads_only_authorized_content(tmp_path: Path) -> None:
    root = tmp_path / ".codex" / "skills"
    root.mkdir(parents=True)
    skill = _skill(root)
    outside = tmp_path / "private.txt"
    outside.write_text("private material", encoding="utf-8")
    (skill / "references" / "outside.txt").symlink_to(outside)
    catalog = LocalCapabilityCatalog([root])

    found = catalog.search({"query": "paper", "kind": "skill"})
    assert found["ok"] and found["count"] == 1
    item = found["items"][0]
    assert item["source"] == "codex"
    assert str(root) not in found["summary"]

    read = catalog.read_skill({"id": item["id"], "resources": ["references/checklist.md"]})
    assert read["ok"] and "Check citations." in read["summary"]
    assert "private material" not in read["summary"]
    assert catalog.read_skill({"id": item["id"], "resources": ["references/outside.txt"]})["ok"] is False
    assert catalog.read_skill({"id": item["id"], "resources": ["../private.txt"]})["ok"] is False


def test_plugin_metadata_does_not_claim_actions_are_available(tmp_path: Path) -> None:
    root = tmp_path / ".codex" / "plugins" / "cache"
    plugin = root / "example" / ".codex-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example", "description": "Local workflow", "mcpServers": {"secret": "https://private.example"}}),
        encoding="utf-8",
    )
    catalog = LocalCapabilityCatalog([root])
    found = catalog.search({"query": "example", "kind": "plugin"})
    assert found["count"] == 1
    inspected = catalog.inspect_plugin({"id": found["items"][0]["id"]})
    assert inspected["local_status"] == "metadata_only"
    assert "https://private.example" not in inspected["summary"]


def test_catalog_rejects_home_and_does_not_follow_skill_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalCapabilityCatalog([Path.home()])
    root = tmp_path / "skills"
    root.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    _skill(hidden)
    (root / "linked").symlink_to(hidden, target_is_directory=True)
    assert LocalCapabilityCatalog([root]).search({"query": "paper"})["count"] == 0
