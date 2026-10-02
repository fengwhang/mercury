"""Real profile files and bridge subprocesses must stay in the selected home."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import yaml

from mercury_cli import profiles
from mercury_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def profile_install(tmp_path, monkeypatch):
    root = tmp_path / ".mercury-nightly"
    (root / "hermes").mkdir(parents=True)
    (root / "config").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for key, value in {
        "MERCURY_HOME": root, "HERMES_HOME": root / "hermes",
        "MERCURY_CONFIG": root / "config.yaml", "HERMES_OMP_CONFIG": root / "config.yaml",
        "PI_CODING_AGENT_DIR": root / "omp",
    }.items():
        monkeypatch.setenv(key, str(value))
    (root / "config.yaml").write_text(yaml.safe_dump({
        "models": {"default": "openrouter/default-chat", "delegate_model": "openrouter/default-code"},
        "hermes": {"model": {"default": "default-chat", "provider": "openrouter"}},
        "omp": {"tools": {"approvalMode": "yolo"}},
    }))
    for name in profiles._PROFILE_PROMPT_FILES:
        (root / "config" / name).write_text(f"DEFAULT-PRIVATE-{name}")
    return root


def create(name, **kwargs):
    return profiles.create_profile(name, no_alias=True, no_skills=not bool(kwargs), **kwargs)


def test_create_and_runtime_bind_all_prompt_and_memory_paths(profile_install, tmp_path):
    from agent.prompt_builder import load_agents_md_home, load_hermes_md_home, load_soul_md
    from mercury_cli.config import get_config_path, soul_md_locations
    from mercury_constants import get_config_dir
    from tools.memory_tool import get_memory_dir

    stable_profile = tmp_path / ".mercury" / "profiles" / "stable-only"
    stable_profile.mkdir(parents=True)
    (stable_profile / "SOUL.md").write_text("STABLE-PERSONA")
    alpha = create("alpha")
    beta = create("beta")
    for home in (alpha, beta):
        for name in profiles._PROFILE_PROMPT_FILES[:-1]:
            target = home / "config" / name
            assert target.is_file() and not target.is_symlink()
            assert "DEFAULT-PRIVATE" not in target.read_text()
            target.write_text(f"{home.name.upper()}-{name}")

    token = set_hermes_home_override(str(alpha))
    try:
        assert get_config_dir() == alpha / "config"
        assert get_config_path() == alpha / "config.yaml"
        assert get_memory_dir() == alpha / "config"
        assert soul_md_locations(alpha)[0] == alpha / "config" / "SOUL.md"
        assert load_soul_md() == "ALPHA-SOUL.md"
        assert load_agents_md_home() == "ALPHA-AGENTS.md"
        assert load_hermes_md_home() == "ALPHA-HERMES.md"
        assert load_agents_md_home(home_override=beta) == "BETA-AGENTS.md"
        # Removing a profile file cannot expose the default persona.
        (alpha / "config" / "HERMES.md").unlink()
        assert load_hermes_md_home() is None
    finally:
        reset_hermes_home_override(token)
    assert (profile_install / "config" / "MEMORY.md").read_text() == "DEFAULT-PRIVATE-MEMORY.md"
    assert (stable_profile / "SOUL.md").read_text() == "STABLE-PERSONA"
    assert not (profile_install / "hermes" / "profiles" / "stable-only").exists()


@pytest.mark.parametrize("clone_all", [False, True])
def test_clone_and_archive_are_independent(profile_install, tmp_path, clone_all):
    source = create("source")
    (source / "config" / "AGENTS.md").unlink()
    (source / "config" / "AGENTS.md").symlink_to(profile_install / "config" / "AGENTS.md")
    (source / "config" / "SOUL.md").write_text("SOURCE-PERSONA")
    (source / "config" / "custom.md").symlink_to(profile_install / "config" / "USER.md")
    clone = create("clone", clone_from="source", clone_config=not clone_all, clone_all=clone_all)
    assert (clone / "config" / "SOUL.md").read_text() == "SOURCE-PERSONA"
    copied = clone / "config" / "AGENTS.md"
    assert not copied.is_symlink()
    copied.write_text("CLONE-ONLY")
    custom = clone / "config" / "custom.md"
    assert not custom.is_symlink()
    custom.write_text("CLONE-CUSTOM-ONLY")
    assert (profile_install / "config" / "USER.md").read_text() == "DEFAULT-PRIVATE-USER.md"
    assert (profile_install / "config" / "AGENTS.md").read_text() == "DEFAULT-PRIVATE-AGENTS.md"
    archive = profiles.export_profile("clone", str(tmp_path / "clone.tar.gz"))
    restored = profiles.import_profile(str(archive), name="restored")
    assert (restored / "config" / "AGENTS.md").read_text() == "CLONE-ONLY"
    (restored / "config" / "AGENTS.md").write_text("RESTORED-ONLY")
    assert copied.read_text() == "CLONE-ONLY"


def test_existing_profile_migrates_local_files_and_retains_empty_overrides(profile_install):
    from mercury_cli.config import ensure_hermes_home

    home = profiles.get_profile_dir("legacy")
    (home / "memories").mkdir(parents=True)
    (home / "SOUL.md").write_text("LEGACY-PERSONA")
    (home / "AGENTS.md").write_text("LEGACY-RULES")
    (home / "memories" / "USER.md").write_text("LEGACY-USER")
    (home / "config").mkdir()
    (home / "config" / "OMP.md").write_text("")
    token = set_hermes_home_override(str(home))
    try:
        ensure_hermes_home()
    finally:
        reset_hermes_home_override(token)
    assert (home / "config" / "SOUL.md").read_text() == "LEGACY-PERSONA"
    assert (home / "config" / "USER.md").read_text() == "LEGACY-USER"
    assert (home / "config" / "AGENTS.md").read_text() == "LEGACY-RULES"
    assert (home / "config" / "OMP.md").read_text() == ""


@pytest.mark.parametrize("prefix", [["omp", "-p", "coder"], ["-p", "coder", "omp"], ["omp", "--profile=coder"]])
def test_omp_cli_selects_profile_before_bridge_and_preserves_native_print(profile_install, tmp_path, prefix):
    home = create("coder")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg["models"]["delegate_model"] = "openrouter/profile-code"
    cfg["models"]["delegate_thinking_level"] = "medium"
    cfg["omp"]["tools"]["approvalMode"] = "write"
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    original = (profile_install / "config.yaml").read_bytes()
    binary = tmp_path / "omp-probe"
    binary.write_text(f"#!{sys.executable}\n" +
        "import json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'env': {key: os.environ.get(key) for key in "
        "['MERCURY_HOME','MERCURY_PROFILE_HOME','MERCURY_CONFIG','HERMES_OMP_CONFIG','HERMES_HOME','PI_CODING_AGENT_DIR']}}))\n")
    binary.chmod(0o755)
    env = dict(os.environ, HERMES_OMP_BIN=str(binary), MERCURY_REPO=str(Path(__file__).resolve().parents[3]))
    completed = subprocess.run([sys.executable, "-m", "mercury_cli.main", *prefix, "--", "-p", "literal $HOME * _"],
        cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, timeout=40)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["argv"] == ["--thinking", "medium", "--model", "openrouter/profile-code", "-p", "literal $HOME * _"]
    assert result["env"]["MERCURY_HOME"] == str(profile_install)
    assert result["env"]["MERCURY_CONFIG"] == str(home / "config.yaml")
    assert result["env"]["MERCURY_PROFILE_HOME"] == str(home)
    assert result["env"]["PI_CODING_AGENT_DIR"] == str(home / "omp" / "agent")
    assert yaml.safe_load((home / "config.yaml").read_text())["omp"]["tools"]["approvalMode"] == "write"
    assert (profile_install / "config.yaml").read_bytes() == original


def test_observatory_omp_spawn_and_resume_keep_profile_settings(profile_install, monkeypatch):
    from observatory.spawn import build_omp_child, omp_child_kwargs_for_row
    from tools import omp_delegation, omp_rpc_transport

    home = create("coder")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg["models"].update(delegate_model="openrouter/profile-code", delegate_thinking_level="low")
    cfg["omp"]["tools"]["approvalMode"] = "always-ask"
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(omp_delegation, "_shared_env_overrides", lambda: {})
    class Child:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            pass
    monkeypatch.setattr(omp_rpc_transport, "OmpRpcChild", Child)
    row = {"extra": {"profile": "coder"}, "session_ref": "session.jsonl"}
    child = build_omp_child(**omp_child_kwargs_for_row(row, mercury_home=profile_install), omp_path="/fake/omp")
    assert child.kwargs["model"] == "openrouter/profile-code"
    assert child.kwargs["thinking_level"] == "low"
    assert child.kwargs["env"]["MERCURY_PROFILE_HOME"] == str(home)
    assert child.kwargs["env"]["MERCURY_CONFIG"] == str(home / "config.yaml")
    assert "session.jsonl" in child.kwargs["command_override"]
    assert yaml.safe_load((home / "config.yaml").read_text())["omp"]["tools"]["approvalMode"] == "always-ask"
    assert os.environ["MERCURY_CONFIG"] == str(profile_install / "config.yaml")


def test_profile_cli_writes_and_setup_sync_preserve_both_engines(profile_install):
    from mercury_cli import omp_sync
    from mercury_cli.config import load_config_readonly, save_config

    home = create("coder")
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg["models"]["default"] = "openrouter/profile-chat"
    cfg["hermes"]["model"]["default"] = "profile-chat"
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    default = (profile_install / "config.yaml").read_bytes()
    completed = subprocess.run([sys.executable, "-m", "mercury_cli.main", "-p", "coder", "config", "set", "agent.max_turns", "17"],
        cwd=Path(__file__).resolve().parents[2], env=dict(os.environ), capture_output=True, text=True, timeout=40)
    assert completed.returncode == 0, completed.stderr
    token = set_hermes_home_override(str(home))
    try:
        effective = load_config_readonly()
        assert effective["model"]["default"] == "profile-chat"
        assert effective["agent"]["max_turns"] == 17
        save_config(dict(effective, agent={**effective["agent"], "max_turns": 23}))
        assert omp_sync._render_omp()
    finally:
        reset_hermes_home_override(token)
    written = yaml.safe_load((home / "config.yaml").read_text())
    assert written["hermes"]["agent"]["max_turns"] == 23
    assert written["models"]["default"] == "openrouter/profile-chat"
    assert written["omp"]["tools"]["approvalMode"] == "yolo"
    assert (profile_install / "config.yaml").read_bytes() == default


def test_hermes_full_prompt_uses_profile_identity_on_an_unbound_worker(profile_install, monkeypatch):
    from agent.system_prompt import build_system_prompt
    import run_agent

    home = create("coder")
    for name in ("SOUL.md", "AGENTS.md", "HERMES.md", "OMP.md"):
        (home / "config" / name).write_text(f"CODER-{name}")
    agent = SimpleNamespace(
        load_soul_identity=True, skip_context_files=True, valid_tool_names=[],
        _task_completion_guidance=False, _tool_use_enforcement=False,
        _environment_probe=False, _kanban_worker_guidance="",
        _memory_store=None, _memory_manager=None, model="", provider="", platform="",
        pass_session_id=False, session_id="", _session_db=SimpleNamespace(db_path=home / "state.db"),
    )
    monkeypatch.setattr(run_agent, "build_environment_hints", lambda **kwargs: "")
    result = []
    worker = threading.Thread(target=lambda: result.append(build_system_prompt(agent)))
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    prompt = result[0]
    for name in ("SOUL.md", "AGENTS.md", "HERMES.md"):
        assert f"CODER-{name}" in prompt
    assert "Active Mercury profile: coder" in prompt
    assert "DEFAULT-PRIVATE" not in prompt
    assert "CODER-OMP.md" not in prompt


def test_distribution_updates_canonical_instructions_and_preserves_profile_memory(profile_install, tmp_path):
    from mercury_cli.profile_distribution import install_distribution, update_distribution

    source = tmp_path / "distribution"
    (source / "config").mkdir(parents=True)
    (source / "distribution.yaml").write_text("name: research\nversion: 1.0.0\n")
    (source / "SOUL.md").write_text("RESEARCH-IDENTITY-V1")
    (source / "config" / "OMP.md").write_text("RESEARCH-OMP-V1")
    plan = install_distribution(str(source))
    home = plan.target_dir
    assert (home / "config" / "SOUL.md").read_text() == "RESEARCH-IDENTITY-V1"
    (home / "config" / "MEMORY.md").write_text("LEARNED-PROFILE-MEMORY")
    (source / "SOUL.md").write_text("RESEARCH-IDENTITY-V2")
    (source / "config" / "OMP.md").write_text("RESEARCH-OMP-V2")
    (source / "config" / "MEMORY.md").write_text("AUTHOR-MEMORY-TEMPLATE")
    update_distribution("research")
    assert (home / "config" / "SOUL.md").read_text() == "RESEARCH-IDENTITY-V2"
    assert (home / "config" / "OMP.md").read_text() == "RESEARCH-OMP-V2"
    assert (home / "config" / "MEMORY.md").read_text() == "LEARNED-PROFILE-MEMORY"
    assert (profile_install / "config" / "MEMORY.md").read_text() == "DEFAULT-PRIVATE-MEMORY.md"


def test_profile_slash_reports_selected_prompt_folder(profile_install):
    from mercury_cli.slash_exec import CommandContext, execute_command

    home = create("slash-profile")
    token = set_hermes_home_override(home)
    try:
        local = execute_command("profile", CommandContext(surface="cli"))
        assert local.data["prompt_dir"].endswith("/slash-profile/config")
    finally:
        reset_hermes_home_override(token)
    remote = execute_command("profile", CommandContext(surface="gateway", options={
        "profile_name": "slash-profile", "home_display": str(home),
    }))
    assert remote.data["profile"] == "slash-profile"
    assert remote.data["prompt_dir"].endswith("/slash-profile/config")
