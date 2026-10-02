"""The Mercury operating manual must be discoverable with usable references."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from agent.skill_commands import scan_skill_commands
from tools import skills_tool

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / "skills" / "autonomous-ai-agents" / "mercury-agent"


@pytest.fixture
def installed_manual(tmp_path, monkeypatch):
    library = tmp_path / "skills"
    dest = library / "autonomous-ai-agents" / "mercury-agent"
    shutil.copytree(SKILL_DIR, dest)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", library)
    return dest


def test_manual_is_discoverable_and_references_can_be_loaded(installed_manual):
    commands = scan_skill_commands()
    assert "/mercury-agent" in commands
    payload = json.loads(skills_tool.skill_view("mercury-agent"))
    assert payload["success"]
    text = (installed_manual / "SKILL.md").read_text()
    targets = set(re.findall(r'\]\(((?:references|templates)/[^)]+)\)', text))
    assert targets
    for target in sorted(targets):
        result = json.loads(skills_tool.skill_view("mercury-agent", file_path=target))
        assert result["success"], target


def test_all_manual_references_are_reachable(installed_manual):
    text = (installed_manual / "SKILL.md").read_text()
    targets = set(re.findall(r'\]\((references/[^)]+)\)', text))
    references = {f"references/{p.name}" for p in (installed_manual / "references").glob("*.md")}
    assert references <= targets
