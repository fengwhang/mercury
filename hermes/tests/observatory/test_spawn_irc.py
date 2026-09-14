"""Spawn/exit tests over IRC channels (no matrix)."""

from __future__ import annotations

import pytest

from observatory import rooms, spawn
from observatory.spawn import (
    OrchestratorRegistry,
    begin_exit,
    exit_orchestrator,
    finish_exit,
    read_purge_journal,
    replay_purge_journal,
    spawn_orchestrator,
)


def _real_state(tmp_path):
    from observatory.state import ObservatoryState

    return ObservatoryState(tmp_path / "state.db")


class FakeBot:
    def __init__(self):
        self.joined: list[str] = []
        self.said: list[tuple[str, str]] = []
        self.destroyed: list[str] = []
        self.invited: list[tuple[str, str]] = []

    async def join_channel(self, channel: str) -> bool:
        self.joined.append(channel)
        return True

    async def part_channel(self, channel: str) -> bool:
        return True

    async def say(self, channel: str, text: str) -> bool:
        self.said.append((channel, text))
        return True

    async def destroy_channel(self, channel: str) -> bool:
        self.destroyed.append(channel)
        return True

    async def invite_user(self, nick: str, channel: str) -> bool:
        self.invited.append((nick, channel))
        return True


class FakeAgent:
    def __init__(self, session_id="sess-1", model="m"):
        self.session_id = session_id
        self.model = model
        self.closed = False

    def close(self):
        self.closed = True


class FakeRpc:
    def __init__(self, session_file: str, model: str = "m"):
        self._session_file = session_file
        self.model = model
        self.stopped = False

    def stop(self):
        self.stopped = True

    def session_file(self):
        return self._session_file


@pytest.mark.asyncio
async def test_spawn_hermes_room(tmp_path, monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator("Ace", "hermes", state=state, registry=registry)
    assert row["room_id"] == "#ace"
    assert row["depth"] == 0
    assert bot.joined == ["#ace"]
    assert any("Ace" in t for _, t in bot.said)
    assert registry.get(row["node_id"]) is not None


@pytest.mark.asyncio
async def test_spawn_omp_room_registers_pump(tmp_path, monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    rpc = FakeRpc(str(tmp_path / "s.jsonl"))
    monkeypatch.setattr(spawn, "omp_session_file", lambda child: child.session_file())
    row = await spawn_orchestrator(
        "King",
        "omp",
        state=state,
        registry=registry,
        omp_child_factory=lambda: rpc,
        validate_session_ref=False,
    )
    assert row["room_id"] == "#king"
    mgr = rooms.RoomManager(state, bot)
    assert mgr.inbound_route("#king")[0] == "spawn-omp"


@pytest.mark.asyncio
async def test_spawn_rejects_blank_and_bad_engine(tmp_path) -> None:
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    with pytest.raises(ValueError):
        await spawn_orchestrator("  ", "hermes", state=state, registry=registry)
    with pytest.raises(ValueError):
        await spawn_orchestrator("x", "bogus", state=state, registry=registry)


@pytest.mark.asyncio
async def test_exit_cascade_destroys_subtree_channels(tmp_path, monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator(
        "Ace",
        "hermes",
        state=state,
        registry=registry,
        agent_factory=lambda: FakeAgent(),
    )
    node_id = row["node_id"]
    # delegate child room under it
    state.add_node(
        "deleg-1",
        engine="hermes",
        name="kid",
        slug="kid",
        mxid="kid",
        session_ref="s",
        parent_node_id=node_id,
        extra={"kind": "delegate"},
    )
    state.set_room_id("deleg-1", "#ace-kid")
    result = await exit_orchestrator(node_id, state=state, registry=registry, bot=bot)
    assert result["deferred"] == []
    assert set(bot.destroyed) == {"#ace", "#ace-kid"}
    with pytest.raises(Exception):
        state.get(node_id)
    assert read_purge_journal(state) == []


@pytest.mark.asyncio
async def test_exit_without_bot_defers_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: None)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator(
        "Solo",
        "hermes",
        state=state,
        registry=registry,
        agent_factory=lambda: FakeAgent(),
    )
    result = await exit_orchestrator(row["node_id"], state=state, registry=registry)
    assert result["deferred"] != []
    assert len(read_purge_journal(state)) == 1
    # replay once the bot is back completes the exit
    bot = FakeBot()
    deferred = await replay_purge_journal(state, bot=bot)
    assert deferred == []
    assert bot.destroyed == ["#solo"]
    assert read_purge_journal(state) == []


def test_begin_exit_rejects_nonzero_depth(tmp_path) -> None:
    state = _real_state(tmp_path)
    state.add_node(
        "root", engine="hermes", name="r", slug="r", mxid="r", session_ref="s"
    )
    state.add_node(
        "kid",
        engine="hermes",
        name="k",
        slug="k",
        mxid="k",
        session_ref="s",
        parent_node_id="root",
    )
    with pytest.raises(ValueError):
        begin_exit(state, "kid")


def test_finish_exit_deletes_deepest_first(tmp_path) -> None:
    state = _real_state(tmp_path)
    state.add_node(
        "root", engine="hermes", name="r", slug="r", mxid="r", session_ref="s"
    )
    state.add_node(
        "kid",
        engine="hermes",
        name="k",
        slug="k",
        mxid="k",
        session_ref="s",
        parent_node_id="root",
    )
    record = begin_exit(state, "root")
    assert record.channels == []  # no rooms joined in this test
    finish_exit(state, record)
    with pytest.raises(Exception):
        state.get("root")
    with pytest.raises(Exception):
        state.get("kid")


@pytest.mark.asyncio
async def test_spawn_same_name_gets_distinct_channels(tmp_path, monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    first = await spawn_orchestrator(
        "Ace", "hermes", state=state, registry=registry,
        agent_factory=lambda: FakeAgent(session_id="s1"))
    second = await spawn_orchestrator(
        "Ace", "hermes", state=state, registry=registry,
        agent_factory=lambda: FakeAgent(session_id="s2"))
    assert first["room_id"] == "#ace"
    assert second["room_id"] == "#ace-2"


@pytest.mark.asyncio
async def test_spawn_invites_phone_user(tmp_path, monkeypatch) -> None:
    """Fresh spawns nudge the phone with an INVITE (tap, no typing)."""
    from observatory import soju as soju_mod

    bot = FakeBot()
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator("Ace", "hermes", state=state, registry=registry)
    assert (soju_mod.SOJU_USER, row["room_id"]) in bot.invited


@pytest.mark.asyncio
async def test_spawn_prefixed_room_and_nick(tmp_path, monkeypatch) -> None:
    """server_name prefixes the room and the agent nick (vm_charlie)."""
    from observatory.rooms import agent_nick, child_channel, spawn_channel

    assert spawn_channel("charlie", server="vm") == "#vm_charlie"
    assert agent_nick("charlie", server="vm") == "vm_charlie"
    assert child_channel("ace", "cow", server="vm") == "#vm_ace-cow"
    assert spawn_channel("charlie") == "#charlie"  # bare legacy

    bot = FakeBot()
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    state = _real_state(tmp_path)
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator(
        "Charlie", "hermes", state=state, registry=registry, server_name="vm")
    assert row["room_id"] == "#vm_charlie"
    assert row["mxid"] == "vm_charlie"
    assert bot.joined == ["#vm_charlie"]
