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


@pytest.mark.asyncio
async def test_submit_channel_payload_publishes_direct(
    tmp_path, monkeypatch
) -> None:
    """Live mirror needs no node row and no global manager."""
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    rooms_mod._QUEUE.queue.clear()
    try:
        rooms_mod.submit_channel_payload(
            "#vm_gateway",
            {"feed": "tool", "tool": "delegate_task", "args": "a"})
        assert rooms_mod._QUEUE.qsize() == 1
        assert await mgr.drain_queue() == 1
    finally:
        rooms_mod._QUEUE.queue.clear()
    tools = [text for ch, text in bot.said if ch == "#vm_gateway"]
    assert any("delegate_task" in text for text in tools)


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
    import asyncio as _asyncio
    import observatory.omp_feed as omp_feed_mod

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    state.add_node(
        "bravo-node", engine="omp", name="bravo", slug="bravo",
        mxid="vm_bravo", session_ref="bravo-node",
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("bravo-node", "#vm_bravo")
    live = {"feed": "thought", "text": "live hmm", "subagent_id": ""}
    late = {"feed": "tool", "tool": "bash", "args": "ls", "subagent_id": ""}
    echo = {"feed": "message", "role": "user",
            "text": "[owner over IRC] go", "subagent_id": ""}
    dup = {"feed": "message", "role": "assistant",
           "text": "done", "subagent_id": ""}
    _FakeFeed.live_payloads = [live, echo, dup]
    monkeypatch.setattr(omp_feed_mod, "OmpFeed", _FakeFeed)
    rpc = _FakeRpc([live, late, echo, dup])
    rooms_mod._omp_rooms["bravo-node"] = {
        "channel": "#vm_bravo", "rpc": rpc, "busy": False}
    try:
        reply = await mgr.handle_omp_message("#vm_bravo", "owner", "go")
        assert reply == ""
        # Handler returns at once; the run continues in background.
        assert rooms_mod._omp_rooms["bravo-node"]["busy"] is True
        async with _asyncio.timeout(5):
            while rooms_mod._omp_rooms["bravo-node"]["busy"]:
                await _asyncio.sleep(0.02)
    finally:
        rooms_mod._omp_rooms.pop("bravo-node", None)
        _FakeFeed.live_payloads = []
    said = [text for ch, text in bot.said if ch == "#vm_bravo"]
    assert any("live hmm" in s for s in said)
    assert any("bash" in s for s in said)
    assert sum("live hmm" in s for s in said) == 1
    assert not any("[owner over IRC]" in s for s in said)
    # Assistant frame filtered: the summary below is the only "done".
    assert sum(s.strip() == "done" for s in said) == 1


@pytest.mark.asyncio
async def test_omp_room_busy_steers_without_queueing(
    tmp_path, monkeypatch
) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    state.add_node(
        "bravo-node", engine="omp", name="bravo", slug="bravo",
        mxid="vm_bravo", session_ref="bravo-node",
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("bravo-node", "#vm_bravo")
    steered: list[str] = []

    class _SteerRpc:
        def run_task(self, prompt):
            raise AssertionError("must not run while busy")

        def steer(self, text):
            steered.append(text)

    rooms_mod._omp_rooms["bravo-node"] = {
        "channel": "#vm_bravo", "rpc": _SteerRpc(), "busy": True}
    try:
        reply = await mgr.handle_omp_message("#vm_bravo", "owner", "stop that")
    finally:
        rooms_mod._omp_rooms.pop("bravo-node", None)
    assert reply == "steered mid-run."
    assert steered == ["[owner over IRC] stop that"]


def test_omp_room_skip_predicate() -> None:
    from observatory.rooms import _omp_room_skips_frame as skip

    assert skip({"feed": "message", "role": "user", "text": "hi"}) is True
    assert skip({"feed": "message", "role": "assistant", "text": "hi"}) is True
    assert skip({"feed": "message", "role": "system", "text": "hi"}) is False
    assert skip({"feed": "message", "text": "no role"}) is False
    assert skip({"feed": "tool", "tool": "bash"}) is False
    assert skip({"feed": "thought", "text": "hmm"}) is False
    assert skip("nonsense") is False
    assert skip(None) is False
