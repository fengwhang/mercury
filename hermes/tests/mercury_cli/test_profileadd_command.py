"""Profile creation from Hermes chat must stay within the selected installation."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from mercury_cli import profiles


@pytest.fixture(params=["mercury", "mercury-nightly"])
def install(request, tmp_path, monkeypatch):
    command = request.param
    root = tmp_path / (".mercury-nightly" if command.endswith("-nightly") else ".mercury")
    (root / "hermes").mkdir(parents=True)
    (root / "config").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for key, value in {
        "MERCURY_CMD": command, "MERCURY_HOME": root,
        "HERMES_HOME": root / "hermes", "MERCURY_CONFIG": root / "config.yaml",
        "HERMES_OMP_CONFIG": root / "config.yaml", "PI_CODING_AGENT_DIR": root / "omp",
    }.items():
        monkeypatch.setenv(key, str(value))
    (root / "config.yaml").write_text(yaml.safe_dump({
        "models": {"default": "openrouter/default-chat", "delegate_model": "openrouter/default-code"},
        "hermes": {}, "omp": {"tools": {"approvalMode": "write"}},
    }))
    (root / "config" / "AGENTS.md").write_text("DEFAULT-PRIVATE-INSTRUCTIONS")
    return command, root


@pytest.mark.parametrize("surface", ["cli", "tui", "gateway"])
@pytest.mark.asyncio
async def test_profileadd_creates_complete_profile_without_switching(install, surface, capsys):
    command, root = install
    original = (root / "config.yaml").read_bytes()
    env_before = {key: os.environ.get(key) for key in (
        "MERCURY_HOME", "HERMES_HOME", "MERCURY_CONFIG", "PI_CODING_AGENT_DIR",
    )}
    active_before = profiles.get_active_profile_name()
    if surface == "gateway":
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource
        from plugins.platforms.mirc.adapter import bang_to_slash

        runner = GatewayRunner.__new__(GatewayRunner)
        text = bang_to_slash("!profileadd Research")
        assert text == "/profileadd Research"
        event = MessageEvent(text=text, source=SessionSource(
            platform=Platform.MATRIX, chat_id="gateway", chat_type="dm",
        ))
        reply = await runner._handle_profileadd_command(event)
    else:
        from cli import MercuryCLI

        cli = MercuryCLI.__new__(MercuryCLI)
        cli.session_id = "profileadd-session"
        cli.console = MagicMock()
        if surface == "tui":
            from tui_gateway.slash_worker import _run

            reply = _run(cli, "/profileadd Research")
        else:
            assert cli.process_command("/profileadd Research") is True
            reply = capsys.readouterr().out
        assert cli.session_id == "profileadd-session"

    home = root / "hermes" / "profiles" / "research"
    assert f"Profile 'research' created at {home}" in reply
    for name in ("SOUL", "AGENTS", "HERMES", "OMP", "MEMORY", "USER"):
        prompt = home / "config" / f"{name}.md"
        assert prompt.is_file() and not prompt.is_symlink()
        assert "DEFAULT-PRIVATE-INSTRUCTIONS" not in prompt.read_text()
    # Both engines and all profiles use this installation's shared skills library.
    assert (root / "skills" / "autonomous-ai-agents" / "mercury-agent" / "SKILL.md").is_file()
    assert (home / ".env").is_file()
    config = yaml.safe_load((home / "config.yaml").read_text())
    assert "default" not in config["models"]
    from mercury_cli.profile_defaults import resolve_model_defaults
    assert resolve_model_defaults(config, home / "config.yaml")["models"]["default"] == "openrouter/default-chat"
    wrapper = Path.home() / ".local" / "bin" / "research"
    assert wrapper.is_file() and command in wrapper.read_text()
    assert f"Configure: {command} -p research setup" in reply
    assert f"OMP: {command} omp -p research" in reply
    assert profiles.get_active_profile_name() == active_before
    assert {key: os.environ.get(key) for key in env_before} == env_before
    assert (root / "config.yaml").read_bytes() == original
    other = Path.home() / (".mercury" if command.endswith("-nightly") else ".mercury-nightly")
    assert not other.exists()


@pytest.mark.parametrize("args", ["", "one two", "'unfinished", "../outside", "default", "mercury", "bad/name"])
def test_invalid_profileadd_does_not_create_profile(install, args):
    _, root = install
    reply = profiles.profileadd_command(args)
    assert reply.startswith(("Usage:", "Could not create profile:"))
    assert not (root / "hermes" / "profiles").exists()


def test_existing_profileadd_does_not_overwrite_profile(install):
    home = profiles.create_profile("existing", no_alias=True, no_skills=True)
    identity = home / "config" / "AGENTS.md"
    identity.write_text("KEEP-EXISTING-IDENTITY")
    config = (home / "config.yaml").read_bytes()
    reply = profiles.profileadd_command("existing")
    assert "already exists" in reply
    assert identity.read_text() == "KEEP-EXISTING-IDENTITY"
    assert (home / "config.yaml").read_bytes() == config


def test_profileadd_from_named_profile_creates_sibling(install, monkeypatch):
    command, root = install
    parent = profiles.create_profile("parent", no_alias=True, no_skills=True)
    monkeypatch.setenv("HERMES_HOME", str(parent))
    monkeypatch.setenv("MERCURY_CONFIG", str(parent / "config.yaml"))
    reply = profiles.profileadd_command("sibling")
    assert (root / "hermes" / "profiles" / "sibling" / "config" / "AGENTS.md").is_file()
    assert not (parent / "profiles").exists()
    assert os.environ["HERMES_HOME"] == str(parent)
    assert f"Configure: {command} -p sibling setup" in reply
