"""Exercise skill upgrade ownership and live discovery in temporary homes."""
import shutil

import pytest

from agent.skill_utils import is_excluded_skill_path
from tools import skills_sync


def write_skill(root, category, name, text):
    dest = root / category / name
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A test workflow.\n---\n{text}\n")
    return dest


def active_names(root):
    return {skills_sync._read_skill_name(p, p.parent.name) for p in root.rglob("SKILL.md")
            if not is_excluded_skill_path(p)}


@pytest.mark.parametrize("customized", [False, True])
def test_upgrade_retires_pristine_predecessors_and_preserves_edits(tmp_path, monkeypatch, customized):
    bundled = tmp_path / "bundled"
    installed = tmp_path / "installed"
    monkeypatch.setattr(skills_sync, "SKILLS_DIR", installed)
    monkeypatch.setattr(skills_sync, "MANIFEST_FILE", installed / ".bundled_manifest")
    monkeypatch.setattr(skills_sync, "_get_bundled_dir", lambda: bundled)
    monkeypatch.setattr(skills_sync, "_get_optional_dir", lambda: tmp_path / "optional")
    monkeypatch.setattr(skills_sync, "_build_external_skill_index", lambda: set())
    monkeypatch.setattr(skills_sync, "_read_suppressed_names", lambda: set())
    old = write_skill(bundled, "productivity", "grilling", "The original interview.")
    write_skill(bundled, "agents", "hermes-agent", "The original operating guide.")
    skills_sync.sync_skills(quiet=True)
    installed_old = installed / "productivity" / "grilling"
    if customized:
        (installed_old / "SKILL.md").write_text((installed_old / "SKILL.md").read_text() + "Personal interview rules.\n")
    shutil.rmtree(old)
    shutil.rmtree(bundled / "agents" / "hermes-agent")
    write_skill(bundled, "productivity", "grill-me", "The consolidated interview.")
    write_skill(bundled, "agents", "mercury-agent", "The Mercury operating guide.")
    skills_sync.sync_skills(quiet=True)
    assert {"grill-me", "mercury-agent"} <= active_names(installed)
    assert "hermes-agent" not in active_names(installed)
    assert ("grilling" in active_names(installed)) == customized
    if customized:
        assert "Personal interview rules." in (installed_old / "SKILL.md").read_text()
    else:
        assert (installed / ".archive" / "bundled-replacements" / "productivity" / "grilling" / "SKILL.md").is_file()
    skills_sync.sync_skills(quiet=True)
    assert ("grilling" in active_names(installed)) == customized


def test_blank_profile_seeds_mercury_manual_into_shared_library(tmp_path, monkeypatch):
    root = tmp_path / ".mercury-nightly"
    home = root / "hermes" / "profiles" / "blank"
    home.mkdir(parents=True)
    (home / ".no-bundled-skills").touch()
    monkeypatch.setenv("MERCURY_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(skills_sync, "HERMES_HOME", skills_sync._HERMES_HOME_AT_IMPORT)
    monkeypatch.setattr(skills_sync, "SKILLS_DIR", skills_sync._SKILLS_DIR_AT_IMPORT)
    monkeypatch.setattr(skills_sync, "MANIFEST_FILE", skills_sync._MANIFEST_FILE_AT_IMPORT)
    monkeypatch.setattr(skills_sync, "_build_external_skill_index", lambda: set())
    monkeypatch.setattr(skills_sync, "_read_suppressed_names", lambda: set())
    result = skills_sync.sync_skills(quiet=True)
    assert "mercury-agent" in result["copied"]
    assert active_names(root / "skills") == {"mercury-agent"}
    assert not (home / "skills").exists()
