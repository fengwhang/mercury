"""Profile-scoped memory operations use the shipped CLI and provider."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mercury_cli import profiles
from mercury_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(params=["mercury", "mercury-nightly"])
def installation(tmp_path, monkeypatch, request):
    root = tmp_path / (".mercury-nightly" if request.param.endswith("nightly") else ".mercury")
    (root / "hermes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key, value in {"HOME": tmp_path, "MERCURY_HOME": root, "HERMES_HOME": root / "hermes",
                       "MERCURY_CMD": request.param, "MERCURY_CONFIG": root / "config.yaml",
                       "MERCURY_SKILLS_DIR": root / "skills", "MNEMOSYNE_DB_PATH": ""}.items():
        monkeypatch.setenv(key, str(value))
    (root / "config.yaml").write_text(yaml.safe_dump({"hermes": {}, "models": {}}))
    return root


def run_cli(profile, *args):
    repo = Path(__file__).resolve().parents[2]
    return subprocess.run([sys.executable, "-m", "mercury_cli.main", "-p", profile, "memory", *args],
                          cwd=repo, env=os.environ.copy(), capture_output=True, text=True, timeout=60)


def test_cli_receipt_and_recall_are_profile_local(installation):
    root = installation
    home = profiles.create_profile("research", no_alias=True, no_skills=True)
    before = (home / "config.yaml").read_bytes()
    written = run_cli("research", "remember", "Cranberry editor theme is dark.", "--importance", "0.8")
    assert written.returncode == 0, written.stdout + written.stderr
    receipt = json.loads(written.stdout)
    assert receipt["stored"] and receipt["verified"]
    assert receipt["content"] == "Cranberry editor theme is dark."
    assert receipt["bank"] == str(home / "memories" / "mnemopi.db")
    recalled = run_cli("research", "recall", "cranberry editor")
    assert recalled.returncode == 0, recalled.stdout + recalled.stderr
    assert json.loads(recalled.stdout)["results"][0]["id"] == receipt["id"]
    main = run_cli("default", "recall", "cranberry editor")
    assert main.returncode == 0, main.stdout + main.stderr
    assert json.loads(main.stdout)["results"] == []
    assert (root / "memories" / "mnemopi.db").is_file()
    assert (home / "config.yaml").read_bytes() == before


def test_cli_respects_explicit_memory_off(installation):
    root = installation
    config = root / "config.yaml"
    config.write_text(yaml.safe_dump({"hermes": {"memory": {"provider": ""}}}))
    result = run_cli("default", "remember", "do not write")
    assert result.returncode == 1
    assert "not the active memory provider" in json.loads(result.stderr)["error"]
    assert not (root / "memories" / "mnemopi.db").exists()


def test_status_reports_profile_bank_without_creating_it(installation):
    home = profiles.create_profile("viewer", no_alias=True, no_skills=True)
    status = run_cli("viewer", "status")
    assert status.returncode == 0, status.stderr
    assert str(home / "memories" / "mnemopi.db") in status.stdout
    assert "mnemosyne_remember, mnemosyne_recall" in status.stdout
    assert not (home / "memories" / "mnemopi.db").exists()


def test_configured_profile_local_bank_is_used_by_cli_and_runtime(installation):
    from plugins.memory import load_memory_provider
    home = profiles.create_profile("custom", no_alias=True, no_skills=True)
    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.setdefault("hermes", {}).setdefault("memory", {})["mnemosyne"] = {"db_path": "memories/custom.db"}
    config_path.write_text(yaml.safe_dump(config))
    result = run_cli("custom", "remember", "cranberry custom bank")
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["bank"] == str(home / "memories" / "custom.db")
    token = set_hermes_home_override(home)
    provider = load_memory_provider("mnemosyne")
    try:
        provider.initialize("custom-bank-test", mercury_home=str(home))
        hits = json.loads(provider.handle_tool_call("mnemosyne_recall", {"query": "cranberry custom"}))["results"]
        assert hits[0]["id"] == receipt["id"]
        assert provider.get_status_config()["db_path"] == receipt["bank"]
    finally:
        provider.shutdown()
        reset_hermes_home_override(token)


def test_default_provider_tools_and_prompt_share_the_profile(installation):
    from mercury_cli.config import load_config
    from plugins.memory import load_memory_provider
    from agent.memory_manager import MemoryManager, inject_memory_provider_tools

    home = profiles.create_profile("writer", no_alias=True, no_skills=True)
    token = set_hermes_home_override(home)
    manager = MemoryManager()
    try:
        assert load_config()["memory"]["provider"] == "mnemosyne"
        manager.add_provider(load_memory_provider("mnemosyne"))
        manager.initialize_all("profile-test", mercury_home=str(home))
        agent = SimpleNamespace(_memory_manager=manager, enabled_toolsets=["memory"],
                                disabled_toolsets=[], tools=[], valid_tool_names=set())
        inject_memory_provider_tools(agent)
        assert {"mnemosyne_remember", "mnemosyne_recall"} <= agent.valid_tool_names
        provider = manager.providers[0]
        assert str(home / "memories" / "mnemopi.db") in provider.system_prompt_block()
        receipt = json.loads(provider.handle_tool_call("mnemosyne_remember", {"content": "cranberry notebooks"}))
        assert receipt["verified"]
        agent.disabled_toolsets = ["memory"]
        agent.tools = []
        agent.valid_tool_names = set()
        inject_memory_provider_tools(agent)
        assert not agent.valid_tool_names
    finally:
        manager.shutdown_all()
        reset_hermes_home_override(token)


@pytest.mark.parametrize("full", [False, True])
def test_clone_preserves_custom_skills_and_removals_without_shared_files(installation, full):
    source = profiles.create_profile("source", no_alias=True)
    stock = source / "skills" / "memory" / "mnemosyne-memory"
    shutil.rmtree(stock)
    external = installation / "custom-skill"
    external.mkdir()
    (external / "SKILL.md").write_text("---\nname: custom-memory\ndescription: Source custom memory guidance\n---\nSource instructions")
    skill = source / "skills" / "memory" / "custom-memory"
    skill.symlink_to(external, target_is_directory=True)
    view = source / "omp" / "agent" / "skills"
    view.mkdir(parents=True)
    # A bridge entry for an ordinary local skill exercises full clone rebasing.
    manual = source / "skills" / "autonomous-ai-agents" / "mercury-agent"
    (view / "mercury-agent").symlink_to(manual, target_is_directory=True)
    (view / "custom-memory").symlink_to(external, target_is_directory=True)
    clone = profiles.create_profile("copy", clone_from="source", clone_all=full,
                                   clone_config=not full, no_alias=True)
    assert not (clone / "skills" / "memory" / "mnemosyne-memory").exists()
    copied = clone / "skills" / "memory" / "custom-memory" / "SKILL.md"
    assert "Source instructions" in copied.read_text()
    copied.write_text("Clone instructions")
    assert "Source instructions" in (external / "SKILL.md").read_text()
    if full:
        assert (clone / "omp" / "agent" / "skills" / "mercury-agent").resolve() == clone / "skills" / "autonomous-ai-agents" / "mercury-agent"
        assert (clone / "omp" / "agent" / "skills" / "custom-memory").resolve() == copied.parent


def test_rename_keeps_native_skills_attached_to_the_profile(installation):
    from tools.omp_skills_bridge import reconcile_omp_skills
    source = profiles.create_profile("old", no_alias=True)
    token = set_hermes_home_override(source)
    try:
        reconcile_omp_skills(omp_agent_dir=source / "omp" / "agent")
    finally:
        reset_hermes_home_override(token)
    renamed = profiles.rename_profile("old", "new")
    assert (renamed / "omp" / "agent" / "skills" / "mnemosyne-memory").resolve() == renamed / "skills" / "memory" / "mnemosyne-memory"


def test_bridge_replaces_legacy_global_links_and_uses_profile_skills(installation):
    from mercury_cli.omp_command import omp_profile_env
    from tools.omp_skills_bridge import reconcile_omp_skills
    root = installation
    home = profiles.create_profile("coder", no_alias=True)
    global_skill = root / "skills" / "private" / "main-only"
    global_skill.mkdir(parents=True)
    (global_skill / "SKILL.md").write_text("---\nname: main-only\ndescription: Main only\n---\nPrivate")
    agent_dir = home / "omp" / "agent"
    (agent_dir / "skills").mkdir(parents=True)
    (agent_dir / "skills" / "main-only").symlink_to(global_skill, target_is_directory=True)
    assert omp_profile_env(home)["MERCURY_SKILLS_DIR"] == str(home / "skills")
    token = set_hermes_home_override(home)
    try:
        result = reconcile_omp_skills(omp_agent_dir=agent_dir)
        assert result["failed"] == []
        assert not (agent_dir / "skills" / "main-only").exists()
        assert (agent_dir / "skills" / "mnemosyne-memory").resolve() == home / "skills" / "memory" / "mnemosyne-memory"
    finally:
        reset_hermes_home_override(token)
