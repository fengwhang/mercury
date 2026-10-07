"""Direct watcher path: ensure/stream/retire with no queue."""

from __future__ import annotations

import asyncio

import pytest

from observatory import rooms as rooms_mod
from observatory.rooms import RoomManager
from observatory.state import ObservatoryState


class FakeBot:
    def __init__(self):
        self.joined: list[str] = []
        self.said: list[tuple[str, str]] = []
        self.destroyed: list[str] = []
        self.invited: list[tuple[str, str]] = []

    async def join_channel(self, channel: str) -> bool:
        self.joined.append(channel)
        return True

    async def say(self, channel: str, text: str, *, kind: str = "status") -> bool:
        self.said.append((channel, text))
        return True

    async def destroy_channel(self, channel: str) -> bool:
        self.destroyed.append(channel)
        return True

    async def invite_user(self, nick: str, channel: str) -> bool:
        self.invited.append((nick, channel))
        return True


def _manager(tmp_path, monkeypatch):
    from observatory import provision as provision_mod

    monkeypatch.setattr(provision_mod, "live_server_name", lambda home=None: "vm")
    state = ObservatoryState(tmp_path / "state.db")
    bot = FakeBot()
    mgr = RoomManager(state, bot)
    rooms_mod.set_room_manager(mgr)
    rooms_mod.set_bot_sink(bot)
    rooms_mod.set_event_loop(asyncio.get_running_loop())
    try:
        yield mgr, state, bot
    finally:
        rooms_mod.set_room_manager(None)
        rooms_mod.set_bot_sink(None)
        rooms_mod.set_event_loop(None)


@pytest.mark.asyncio
async def test_watcher_start_streams_stop_retires_depth1_session(tmp_path, monkeypatch) -> None:
    import observatory.gateway_session as gs

    state = ObservatoryState(tmp_path / "state.db")
    state.add_node(
        "alpha-node", engine="hermes", name="alpha", slug="alpha",
        mxid="vm_alpha", session_ref="alpha-node",
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("alpha-node", "#vm_alpha")
    bot = FakeBot()
    from observatory import provision as provision_mod

    monkeypatch.setattr(provision_mod, "live_server_name", lambda home=None: "vm")
    monkeypatch.setattr(provision_mod, "get_mlounge_nick", lambda home=None: "owner")
    mgr = RoomManager(state, bot)
    rooms_mod.set_room_manager(mgr)
    rooms_mod.set_bot_sink(bot)
    rooms_mod.set_event_loop(asyncio.get_running_loop())
    try:
        meta = {"name": "bravo",
                "owner_session_id": "agent:main:irc:group:#vm_alpha:owner",
                "goal": "do things", "engine": "omp"}
        channel = await asyncio.to_thread(
            gs._ensure_watcher_room, "deleg_1/0", meta)
        assert channel == "#vm_alpha-bravo"
        assert "#vm_alpha-bravo" in bot.joined
        assert ("owner", "#vm_alpha-bravo") in bot.invited
        await asyncio.sleep(0.3)
        assert not any("started" in t for _, t in bot.said), "room transport is not execution readiness"
        # Live SELF frame streams straight into the room.
        gs._publish_live_payload("deleg_1/0", {
            "feed": "tool", "subagent_id": "", "tool": "bash",
            "args": "ls"}, {})
        await asyncio.sleep(0.3)
        assert any("bash" in t for c, t in bot.said if c == "#vm_alpha-bravo")
        # Depth-1 completion removes this task's room and leaves its root alive.
        from tools import async_delegation
        async_delegation.record_child_spawn("deleg_1/0", "deleg_1", name="bravo")
        async_delegation.record_child_terminal("deleg_1/0", "completed", summary="verified completion")
        await asyncio.to_thread(
            gs._retire_watcher_room, "deleg_1/0", name="bravo")
        assert "#vm_alpha-bravo" in bot.destroyed
        with pytest.raises(KeyError):
            state.get("deleg_1/0")
        assert len(state.get_live()) == 1
    finally:
        rooms_mod.set_room_manager(None)
        rooms_mod.set_bot_sink(None)
        rooms_mod.set_event_loop(None)


@pytest.mark.asyncio
async def test_execution_ready_requires_verified_child_and_attached_feed(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    import observatory.gateway_session as gs
    from gateway.status import get_process_start_time
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    setup = _manager(tmp_path, monkeypatch)
    manager, state, bot = next(setup)
    try:
        state.add_node("root", engine="hermes", name="root", slug="root",
            mxid="vm_root", session_ref="root", depth=0)
        state.add_node("ready/0", engine="omp", depth=1, parent_node_id="root",
            name="worker", slug="ready", mxid="vm_ready", session_ref="ready/0", extra={"kind": "delegate"})
        state.set_room_id("ready/0", "#vm_ready")
        ad.record_child_spawn("ready/0", "ready", child_pid=os.getpid(),
            child_started_at=get_process_start_time(os.getpid()))
        feed = SimpleNamespace(_dispose_listener=lambda: None, _dispose_agent_listener=None)
        transport = SimpleNamespace(pid=os.getpid())
        assert await asyncio.to_thread(gs._mark_child_execution_ready, "ready/0", transport, feed) is False
        assert bot.said == []
        feed._dispose_agent_listener = lambda: None
        assert await asyncio.to_thread(gs._mark_child_execution_ready, "ready/0", transport, feed) is True
        assert state.get("ready/0")["extra"]["execution_state"] == "ready"
        assert any("started" in text for _, text in bot.said)
        before = list(bot.said)
        assert await asyncio.to_thread(gs._mark_child_execution_ready, "ready/0", transport, feed) is True
        assert bot.said == before
        ad.record_child_terminal("ready/0", "interrupted", error="verified interruption")
        assert await asyncio.to_thread(gs._mark_child_execution_ready, "ready/0", transport, feed) is False
    finally:
        try:
            next(setup)
        except StopIteration:
            pass
        state.close()
