"""Exercise !model through MIRC routing into a newly spawned profile room."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from mercury_cli import profiles
from mercury_constants import get_hermes_home_override
from observatory import rooms, spawn
from observatory.state import ObservatoryState
from plugins.platforms.mirc.adapter import MIRCAdapter


@pytest_asyncio.fixture(params=["mercury", "mercury-nightly"])
async def profile_room(tmp_path, monkeypatch, request):
    import gateway.run as gateway_run

    root = tmp_path / (".mercury-nightly" if request.param.endswith("nightly") else ".mercury")
    (root / "hermes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key, value in {"HOME": tmp_path, "MERCURY_HOME": root,
                       "HERMES_HOME": root / "hermes", "MERCURY_CONFIG": root / "config.yaml",
                       "MERCURY_CMD": request.param}.items():
        monkeypatch.setenv(key, str(value))
    main = {"models": {"default": "nous/vendor/main-chat", "fallback": "nous/vendor/retry",
                       "delegate_model": "nous/vendor/main-code", "delegate_fallback": "nous/vendor/code-retry"},
            "hermes": {}, "omp": {"tools": {"approvalMode": "yolo"}}}
    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(main))
    home = profiles.create_profile("research", no_alias=True, no_skills=True)
    assert "models" not in yaml.safe_load((home / "config.yaml").read_text())
    monkeypatch.setattr(gateway_run, "_hermes_home", root / "hermes")
    monkeypatch.setattr("mercury_cli.model_switch.list_authenticated_providers", lambda **_kwargs: [])
    monkeypatch.setattr("observatory.thinking.thinking_started", lambda _channel: None)
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: None)
    state = ObservatoryState(root / "observatory/state.db")
    manager = rooms.RoomManager(state)
    monkeypatch.setattr(rooms, "_current_manager", manager)
    row = await spawn.spawn_orchestrator(
        "research-bot", "hermes", state=state, registry=spawn.OrchestratorRegistry(), profile="research")
    adapter = MIRCAdapter(PlatformConfig(enabled=True, extra={}, typing_indicator=False))
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    seen = []

    async def handle(event):
        assert event.text == "/model"
        assert event.source.profile == "research"
        assert get_hermes_home_override() == str(home)
        return await runner._handle_model_command(event)

    async def send(chat_id, content, **_kwargs):
        seen.append((chat_id, content))
        return SendResult(success=True, message_id="model-status")

    adapter.set_message_handler(handle)
    monkeypatch.setattr(adapter, "send", send)

    async def model_status():
        seen.clear()
        await adapter._dispatch_message("!model", row["room_id"], "group", "owner", "owner")
        pending = list(adapter._session_tasks.values())
        if pending:
            await asyncio.gather(*pending)
        assert get_hermes_home_override() is None
        assert len(seen) == 1
        assert seen[0][0] == row["room_id"]
        return seen[0][1]

    try:
        yield SimpleNamespace(main=main, config_path=config_path, model_status=model_status)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_new_profile_model_status_tracks_inherited_models(profile_room):
    original = profile_room.config_path.read_bytes()
    response = await profile_room.model_status()
    assert "vendor/main-chat" in response.splitlines()[0]
    assert "unknown" not in response.lower()
    assert profile_room.config_path.read_bytes() == original
    profile_room.main["models"]["default"] = "nous/vendor/updated-chat"
    profile_room.config_path.write_text(yaml.safe_dump(profile_room.main))
    assert "vendor/updated-chat" in (await profile_room.model_status()).splitlines()[0]


@pytest.mark.asyncio
async def test_model_status_uses_complete_profile_override(profile_room):
    profile_room.main["profile_models"] = {"research": {
        "default": "nous/vendor/profile-chat", "delegate_model": "nous/vendor/profile-code"}}
    profile_room.config_path.write_text(yaml.safe_dump(profile_room.main))
    original = profile_room.config_path.read_bytes()
    response = await profile_room.model_status()
    assert "vendor/profile-chat" in response.splitlines()[0]
    assert "vendor/main-chat" not in response
    assert profile_room.config_path.read_bytes() == original


@pytest.mark.asyncio
async def test_model_status_reports_invalid_override_instead_of_unknown(profile_room):
    profile_room.main["profile_models"] = {"research": {"default": "nous/vendor/incomplete"}}
    profile_room.config_path.write_text(yaml.safe_dump(profile_room.main))
    response = await profile_room.model_status()
    assert "profile_models.research" in response
    assert "delegate_model is required" in response
    assert "unknown" not in response.lower()
    assert "vendor/main-chat" not in response
