"""Delegate child rooms: prefixed naming, phone visibility, D8 retire."""

from __future__ import annotations

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
    subscribed: list[str] = []
    import observatory.soju as soju_mod

    monkeypatch.setattr(
        soju_mod, "subscribe_user_channel",
        lambda channel, home=None: subscribed.append(channel) or True)
    mgr = RoomManager(state, bot)
    return mgr, state, bot, subscribed


def _spawn_row(state, node_id, name, channel):
    state.add_node(
        node_id, engine="hermes", name=name, slug=name,
        mxid=f"vm_{name}", session_ref=node_id,
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id(node_id, channel)


@pytest.mark.asyncio
async def test_ensure_creates_prefixed_visible_room(tmp_path, monkeypatch) -> None:
    mgr, state, bot, subscribed = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    channel = await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    assert channel == "#vm_alpha-bravo"
    row = state.get("d1")
    assert row["depth"] == 1
    assert row["mxid"] == "vm_bravo"
    assert "#vm_alpha-bravo" in bot.joined
    assert subscribed == ["#vm_alpha-bravo"]
    assert ("owner", "#vm_alpha-bravo") in bot.invited


@pytest.mark.asyncio
async def test_stop_purges_depth1_room(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    await mgr._apply_queued(
        {"op": "lifecycle", "node_id": "d1", "lifecycle": "stop", "name": "bravo"})
    assert "#vm_alpha-bravo" in bot.destroyed
    with pytest.raises(Exception):
        state.get("d1")
    stops = [t for c, t in bot.said if c == "#vm_alpha-bravo"]
    assert any("finished" in t for t in stops)


@pytest.mark.asyncio
async def test_stop_keeps_depth2_room_as_grace(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    await mgr._ensure_child_room_for(
        "d2", {"name": "cee", "parent_name": "d1", "engine": "omp"})
    assert state.get("d2")["depth"] == 2
    await mgr._apply_queued(
        {"op": "lifecycle", "node_id": "d2", "lifecycle": "stop", "name": "cee"})
    assert state.get("d2")["status"] == "dead"
    assert "#vm_bravo-cee" not in bot.destroyed
    # Parent purge cascades to the dead grandchild.
    await mgr._apply_queued(
        {"op": "lifecycle", "node_id": "d1", "lifecycle": "stop", "name": "bravo"})
    with pytest.raises(Exception):
        state.get("d2")
    assert "#vm_bravo-cee" in bot.destroyed


@pytest.mark.asyncio
async def test_stop_unknown_node_resurrects_nothing(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    await mgr._apply_queued(
        {"op": "lifecycle", "node_id": "ghost",
         "lifecycle": "stop", "name": "ghost"})
    assert bot.joined == []
    assert bot.said == []


def test_submit_channel_frame_needs_live_row(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    rooms_mod.set_room_manager(mgr)
    try:
        rooms_mod._QUEUE.queue.clear()
        rooms_mod.submit_channel_frame("#vm_nowhere", {"feed": "tool", "tool": "x"})
        assert rooms_mod._QUEUE.qsize() == 0
        _spawn_row(state, "gw", "gateway", "#vm_gateway")
        rooms_mod.submit_channel_frame(
            "#vm_gateway", {"feed": "tool", "tool": "delegate_task", "args": "a"})
        assert rooms_mod._QUEUE.qsize() == 1
    finally:
        rooms_mod._QUEUE.queue.clear()
        rooms_mod.set_room_manager(None)


class _FakeRpc:
    def __init__(self, frames):
        self._frames = frames

    def run_task(self, prompt):
        return {"summary": "done", "turn_frames": self._frames}


class _FakeFeed:
    live_payloads: list = []

    def __init__(self, rpc):
        self._rpc = rpc

    async def start(self):
        return None

    async def events(self):
        for payload in list(_FakeFeed.live_payloads):
            yield dict(payload)

    async def stop(self):
        return None


@pytest.mark.asyncio
async def test_omp_room_streams_live_then_replays_surplus(
    tmp_path, monkeypatch
) -> None:
    import observatory.omp_feed as omp_feed_mod

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    state.add_node(
        "bravo-node", engine="omp", name="bravo", slug="bravo",
        mxid="vm_bravo", session_ref="bravo-node",
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("bravo-node", "#vm_bravo")
    live = {"feed": "thought", "text": "live hmm", "subagent_id": ""}
    late = {"feed": "tool", "tool": "bash", "args": "ls", "subagent_id": ""}
    _FakeFeed.live_payloads = [live]
    monkeypatch.setattr(omp_feed_mod, "OmpFeed", _FakeFeed)
    rooms_mod._omp_rooms["bravo-node"] = {
        "channel": "#vm_bravo", "rpc": _FakeRpc([live, late]), "busy": False}
    try:
        reply = await mgr.handle_omp_message("#vm_bravo", "owner", "go")
    finally:
        rooms_mod._omp_rooms.pop("bravo-node", None)
        _FakeFeed.live_payloads = []
    assert reply == "done"
    said = [text for ch, text in bot.said if ch == "#vm_bravo"]
    assert any("live hmm" in s for s in said)
    assert any("bash" in s for s in said)
    assert sum("live hmm" in s for s in said) == 1
