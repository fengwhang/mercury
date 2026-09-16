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

    async def say(self, channel: str, text: str) -> bool:
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
    import observatory.soju as soju_mod

    monkeypatch.setattr(
        soju_mod, "subscribe_user_channel",
        lambda channel, home=None: True)
    monkeypatch.setattr(
        soju_mod, "unsubscribe_user_channel",
        lambda channel, home=None: True)
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
async def test_watcher_start_streams_stop_purges(tmp_path, monkeypatch) -> None:
    import observatory.gateway_session as gs

    state = ObservatoryState(tmp_path / "state.db")
    state.add_node(
        "alpha-node", engine="hermes", name="alpha", slug="alpha",
        mxid="vm_alpha", session_ref="alpha-node",
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("alpha-node", "#vm_alpha")
    bot = FakeBot()
    import observatory.soju as soju_mod
    from observatory import provision as provision_mod

    monkeypatch.setattr(provision_mod, "live_server_name", lambda home=None: "vm")
    monkeypatch.setattr(
        soju_mod, "subscribe_user_channel", lambda channel, home=None: True)
    monkeypatch.setattr(
        soju_mod, "unsubscribe_user_channel", lambda channel, home=None: True)
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
        assert any("started" in t for _, t in bot.said)
        # Live SELF frame streams straight into the room.
        gs._publish_live_payload("deleg_1/0", {
            "feed": "tool", "subagent_id": "", "tool": "bash",
            "args": "ls"}, {})
        await asyncio.sleep(0.3)
        assert any("bash" in t for c, t in bot.said if c == "#vm_alpha-bravo")
        # Stop retires: depth-1 purges inline.
        await asyncio.to_thread(
            gs._retire_watcher_room, "deleg_1/0", name="bravo")
        assert "#vm_alpha-bravo" in bot.destroyed
        with pytest.raises(Exception):
            state.get("deleg_1/0")
    finally:
        rooms_mod.set_room_manager(None)
        rooms_mod.set_bot_sink(None)
        rooms_mod.set_event_loop(None)
