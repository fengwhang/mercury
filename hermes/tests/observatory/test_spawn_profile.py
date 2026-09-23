"""!spawn -p <profile>: arg parsing, node binding, dispatch override."""

from __future__ import annotations

import pytest

from observatory import spawn
from observatory.spawn import (
    OrchestratorRegistry,
    parse_spawn_args,
    spawn_orchestrator,
)


def test_parse_spawn_args_orders() -> None:
    assert parse_spawn_args("bravo -p alpha") == ("bravo", "alpha")
    assert parse_spawn_args("-p alpha bravo") == ("bravo", "alpha")
    assert parse_spawn_args("--profile alpha bravo") == ("bravo", "alpha")
    assert parse_spawn_args("--profile=alpha bravo") == ("bravo", "alpha")
    assert parse_spawn_args("bravo") == ("bravo", None)


def test_parse_spawn_args_errors() -> None:
    for bad in ["", "  ", "-p alpha", "a b -p alpha", "bravo -p", "bravo --bogus x"]:
        with pytest.raises(ValueError):
            parse_spawn_args(bad)


def _profile_home(tmp_path, monkeypatch, name="alpha"):
    from pathlib import Path

    hermes_home = tmp_path / "hermes"
    profile_dir = hermes_home / "profiles" / name
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return profile_dir


def _real_state(tmp_path):
    from observatory.state import ObservatoryState

    return ObservatoryState(tmp_path / "state.db")


class FakeBot:
    async def join_channel(self, channel: str) -> bool:
        return True

    async def say(self, channel: str, text: str) -> bool:
        return True

    async def invite_user(self, nick: str, channel: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_spawn_hermes_binds_profile_extra(tmp_path, monkeypatch) -> None:
    _profile_home(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: FakeBot())
    state = _real_state(tmp_path)
    row = await spawn_orchestrator(
        "bravo", "hermes", state=state, registry=OrchestratorRegistry(),
        profile="alpha",
    )
    assert row["extra"].get("profile") == "alpha"
    assert row["room_id"] == "#bravo"


@pytest.mark.asyncio
async def test_spawn_unknown_profile_fails(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: FakeBot())
    state = _real_state(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        await spawn_orchestrator(
            "bravo", "hermes", state=state,
            registry=OrchestratorRegistry(), profile="nope",
        )


@pytest.mark.asyncio
async def test_spawn_omp_child_gets_profile_home(tmp_path, monkeypatch) -> None:
    profile_dir = _profile_home(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: FakeBot())
    seen: dict = {}

    def fake_build(**kwargs):
        seen.update(kwargs)

        class State:
            session_file = str(tmp_path / "s.jsonl")

        class Client:
            def get_state(self):
                return State()

        class Rpc:
            model = "m"
            _client = Client()

        return Rpc()

    monkeypatch.setattr(spawn, "build_omp_child", fake_build)
    state = _real_state(tmp_path)
    row = await spawn_orchestrator(
        "bravo", "omp", state=state, registry=OrchestratorRegistry(),
        omp_child_factory=None, profile="alpha",
        validate_session_ref=False,
    )
    # omp_child_factory=None forces the real build path (mocked above)
    assert row["extra"].get("profile") == "alpha"
    assert seen.get("profile_home") == str(profile_dir)


@pytest.mark.asyncio
async def test_dispatch_wraps_profile_room_turn(tmp_path, monkeypatch) -> None:
    """The adapter override test lives here at the seam level: a room node
    carrying extra.profile resolves that profile's home for the turn."""
    from mercury_constants import get_hermes_home_override

    profile_dir = _profile_home(tmp_path, monkeypatch)
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: FakeBot())
    state = _real_state(tmp_path)
    row = await spawn_orchestrator(
        "bravo", "hermes", state=state, registry=OrchestratorRegistry(),
        profile="alpha",
    )
    channel = row["room_id"]
    from observatory import rooms

    manager = rooms.RoomManager(state, FakeBot())
    node = manager.node_for_channel(channel)
    assert node is not None
    prof = (node.get("extra") or {}).get("profile")
    assert prof == "alpha"

    from mercury_cli.profiles import get_profile_dir
    from mercury_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    assert get_hermes_home_override() is None
    token = set_hermes_home_override(str(get_profile_dir(prof)))
    try:
        assert get_hermes_home_override() == str(profile_dir)
    finally:
        reset_hermes_home_override(token)
    assert get_hermes_home_override() is None
