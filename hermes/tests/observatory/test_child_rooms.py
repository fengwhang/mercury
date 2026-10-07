"""Delegate rooms follow the immediate parent and its depth-based lifetime."""

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
    from observatory import identity
    from unittest.mock import AsyncMock

    monkeypatch.setattr(provision_mod, "live_server_name", lambda home=None: "vm")
    monkeypatch.setattr(identity, "ensure_identity", AsyncMock(return_value=True))
    monkeypatch.setattr(identity, "drop_identity", AsyncMock(return_value=True))
    state = ObservatoryState(tmp_path / "state.db")
    bot = FakeBot()
    subscribed: list[str] = []
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
    assert row["mxid"] == "vm_alpha-bravo"
    assert "#vm_alpha-bravo" in bot.joined
    assert ("owner", "#vm_alpha-bravo") in bot.invited


@pytest.mark.asyncio
async def test_stop_deletes_depth1_room_on_completion(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    await mgr.publish_lifecycle("#vm_alpha-bravo", "stop", name="bravo")
    await mgr._retire_child_room("d1")
    assert "#vm_alpha-bravo" in bot.destroyed
    with pytest.raises(KeyError):
        state.get("d1")
    assert state.get("alpha-node")["status"] == "live"
    stops = [t for c, t in bot.said if c == "#vm_alpha-bravo"]
    assert any("finished" in t for t in stops)


@pytest.mark.asyncio
async def test_deeper_child_survives_completion_until_parent_finishes(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    await mgr._ensure_child_room_for(
        "d2", {"name": "cee", "parent_name": "d1", "engine": "omp"})
    assert state.get("d2")["depth"] == 2
    await mgr._retire_child_room("d2")
    assert state.get("d2")["status"] == "live"
    assert "#vm_alpha-bravo-cee" not in bot.destroyed
    # Depth-1 completion ends its retained subtree, without ending the root.
    await mgr._retire_child_room("d1")
    with pytest.raises(KeyError):
        state.get("d2")
    with pytest.raises(KeyError):
        state.get("d1")
    assert state.get("alpha-node")["status"] == "live"
    assert "#vm_alpha-bravo-cee" in bot.destroyed


@pytest.mark.asyncio
async def test_completion_expires_family_before_parent_notification(tmp_path, monkeypatch):
    """A cancelled parent notification cannot keep a completed family live."""
    import asyncio
    from observatory.state import StateError

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for("d1", {"name": "bravo", "parent_name": "alpha-node"})
    await mgr._ensure_child_room_for("d2", {"name": "cee", "parent_name": "d1"})
    notifying = asyncio.Event()

    async def blocked_publish(*args, **kwargs):
        notifying.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mgr, "publish", blocked_publish)
    retirement = asyncio.create_task(mgr._retire_child_room("d1", summary="done"))
    try:
        await asyncio.wait_for(notifying.wait(), 2)
        retirement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retirement
        for node in ("d1", "d2"):
            with pytest.raises(StateError):
                state.get(node)
        assert state.get("alpha-node")["status"] == "live"
        assert set(bot.destroyed) == {"#vm_alpha-bravo", "#vm_alpha-bravo-cee"}
    finally:
        retirement.cancel()
        await asyncio.gather(retirement, return_exceptions=True)
        state.close()


@pytest.mark.asyncio
async def test_stop_unknown_node_resurrects_nothing(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    await mgr._retire_child_room("ghost")
    assert bot.joined == []
    assert bot.said == []


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_say_nowait_sends_without_rows_or_manager(
    tmp_path, monkeypatch
) -> None:
    """Thread-safe direct send needs no node row and no manager."""
    import asyncio as _asyncio

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    rooms_mod.set_bot_sink(bot)
    rooms_mod.set_event_loop(_asyncio.get_running_loop())
    try:
        assert rooms_mod.say_nowait("#vm_gateway", "hello") is True
        await _asyncio.sleep(0.2)
    finally:
        rooms_mod.set_bot_sink(None)
        rooms_mod.set_event_loop(None)
    assert ("#vm_gateway", "hello") in bot.said


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
        entry = rooms_mod._omp_rooms.get("bravo-node", {})
        pending = [entry["feed_task"]] if entry.get("feed_task") else []
        for task in _asyncio.all_tasks():
            if task.get_name() == "observatory-omp-room-bravo-node":
                task.cancel()
                pending.append(task)
        rooms_mod.drop_omp_room("bravo-node")
        await _asyncio.sleep(0)
        await _asyncio.gather(*pending, return_exceptions=True)
        _FakeFeed.live_payloads = []
    said = [text for ch, text in bot.said if ch == "#vm_bravo"]
    assert any("live hmm" in s for s in said)
    assert any("bash" in s for s in said)
    assert sum("live hmm" in s for s in said) == 1
    assert not any("[owner over IRC]" in s for s in said)
    # The assistant frame and final summary represent the same reply.
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
    assert skip({"feed": "message", "role": "assistant", "text": "hi"}) is False
    assert skip({"feed": "message", "role": "system", "text": "hi"}) is False
    assert skip({"feed": "message", "text": "no role"}) is False
    assert skip({"feed": "tool", "tool": "bash"}) is False
    assert skip({"feed": "thought", "text": "hmm"}) is False
    assert skip("nonsense") is False
    assert skip(None) is False


@pytest.mark.asyncio
async def test_routed_frame_creates_grandchild_room(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    grands: dict[str, str] = {}
    await mgr._publish_routed_frame("d1", "#vm_alpha-bravo", {
        "feed": "tool", "subagent_id": "s9", "tool": "bash", "args": "ls",
    }, grands)
    assert "#vm_alpha-bravo-sub-s9" in bot.joined
    tools = [text for ch, text in bot.said if ch == "#vm_alpha-bravo-sub-s9"]
    assert any("bash" in text for text in tools)
    assert not any("[s9]" in text for text in tools)


@pytest.mark.asyncio
async def test_grandchild_add_death_lifecycle(tmp_path, monkeypatch) -> None:
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    grands: dict[str, str] = {}
    await mgr._publish_routed_frame("d1", "#vm_alpha-bravo", {
        "feed": "node", "kind": "add", "subagent_id": "c1",
        "agent": "charlie", "status": "running",
    }, grands)
    assert "#vm_alpha-bravo-charlie" in bot.joined
    assert state.get("d1/sub-c1")["depth"] == 2
    await mgr._publish_routed_frame("d1", "#vm_alpha-bravo", {
        "feed": "node", "kind": "death", "subagent_id": "c1",
        "agent": "charlie", "status": "completed",
    }, grands)
    # A grandchild survives its own completion, then dies with its parent.
    assert "#vm_alpha-bravo-charlie" not in bot.destroyed
    assert state.get("d1/sub-c1")["status"] == "live"
    await mgr._retire_child_room("d1")
    assert "#vm_alpha-bravo-charlie" in bot.destroyed
    with pytest.raises(KeyError):
        state.get("d1/sub-c1")


@pytest.mark.asyncio
async def test_omp_root_child_ends_on_completion(tmp_path, monkeypatch) -> None:
    """Completion keeps the room under an OMP root too."""
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    state.add_node(
        "bravo-node", engine="omp", name="bravo", slug="bravo",
        mxid="vm_bravo", session_ref="bravo-node",
        parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("bravo-node", "#vm_bravo")
    grands: dict[str, str] = {}
    await mgr._publish_routed_frame("bravo-node", "#vm_bravo", {
        "feed": "node", "kind": "add", "subagent_id": "c9",
        "agent": "zed", "status": "running",
    }, grands)
    assert "#vm_bravo-zed" in bot.joined
    await mgr._publish_routed_frame("bravo-node", "#vm_bravo", {
        "feed": "tool", "subagent_id": "c9", "tool": "read", "args": "f",
    }, grands)
    assert any("read" in text for ch, text in bot.said if ch == "#vm_bravo-zed")
    await mgr._publish_routed_frame("bravo-node", "#vm_bravo", {
        "feed": "node", "kind": "death", "subagent_id": "c9",
        "agent": "zed", "status": "completed",
    }, grands)
    assert "#vm_bravo-zed" in bot.destroyed
    with pytest.raises(KeyError):
        state.get("bravo-node/sub-c9")
    assert state.get("bravo-node")["status"] == "live"


@pytest.mark.asyncio
async def test_live_feed_keeps_lifecycle_after_non_deduplicable_frames(tmp_path, monkeypatch):
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    monkeypatch.setattr(_FakeFeed, "live_payloads", [
        {"feed": "activity", "active": False, "subagent_id": ""},
        {"feed": "node", "kind": "add", "subagent_id": "worker", "name": "worker", "status": "running"},
        {"feed": "status", "text": "To do list:\n• [completed] Verify", "subagent_id": "worker"},
        {"feed": "node", "kind": "death", "subagent_id": "worker", "status": "completed", "summary": "Verified"},
        {"feed": "message", "role": "assistant", "subagent_id": "", "text": "Parent continues"},
    ])
    await mgr._pump_live_omp_feed(_FakeFeed(None), "alpha-node", "#vm_alpha", {}, set())
    assert "#vm_alpha-worker" in bot.destroyed
    with pytest.raises(KeyError):
        state.get("alpha-node/sub-worker")
    assert state.get("alpha-node")["status"] == "live"
    assert any(c == "#vm_alpha" and "Delegate task completed" in t and "Verified" in t for c, t in bot.said)
    assert ("#vm_alpha", "Parent continues") in bot.said


@pytest.mark.asyncio
async def test_pending_purge_cannot_destroy_same_named_successor(tmp_path, monkeypatch):
    from observatory.spawn import begin_exit, replay_purge_journal

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    original = await mgr._ensure_child_room_for("old", {"name": "worker", "parent_name": "alpha-node"})
    begin_exit(state, "old")
    replacement = await mgr._ensure_child_room_for("new", {"name": "worker", "parent_name": "alpha-node"})
    assert replacement == original + "-2"
    assert await replay_purge_journal(state, bot=bot) == []
    assert bot.destroyed == [original]
    assert state.get("new")["status"] == "live"
    # The unsuffixed name becomes reusable once its purge is confirmed.
    assert await mgr._ensure_child_room_for("third", {"name": "worker", "parent_name": "alpha-node"}) == original


@pytest.mark.asyncio
async def test_unresolvable_parent_does_not_fabricate_gateway_ancestry(tmp_path, monkeypatch) -> None:
    """Unknown session ancestry must never be silently assigned to gateway."""
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    state.add_node(
        "gw", engine="hermes", name="gateway agent", slug="gateway",
        mxid="vm_gateway", session_ref="session:gateway",
        parent_node_id=None, extra={"kind": "gateway"})
    state.set_room_id("gw", "#vm_gateway")
    channel = await mgr._ensure_child_room_for(
        "deleg_z/0", {"name": "zed", "parent_name": "20260913_234155_8fad8e3d",
                      "engine": "omp"})
    assert channel == ""
    with pytest.raises(KeyError):
        state.get("deleg_z/0")
    assert bot.joined == []


@pytest.mark.asyncio
async def test_child_room_gets_own_identity(tmp_path, monkeypatch) -> None:
    """Delegate rooms speak as the room name, not vm_gateway."""
    import observatory.identity as identity_mod

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "alpha-node", "alpha", "#vm_alpha")
    seen = []
    monkeypatch.setattr(
        identity_mod, "ensure_identity",
        lambda nick, channel: seen.append((nick, channel)) or True)
    channel = await mgr._ensure_child_room_for(
        "d1", {"name": "bravo", "parent_name": "alpha-node", "engine": "omp"})
    assert seen == [("vm_alpha-bravo", channel)]


@pytest.mark.asyncio
async def test_native_completion_reports_before_level_one_family_is_removed(tmp_path, monkeypatch):
    from observatory.gateway_session import _feed_event_to_dict
    from observatory.omp_feed import OmpFeed

    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "root", "coder", "#vm_coder")
    feed = OmpFeed(None)
    cache = {}

    async def send(sid, parent, status, output=None):
        wire = {"type": "subagent_lifecycle", "payload": {
            "id": sid, "name": sid, "parentAgentId": parent,
            "agent": "task", "status": status, "output": output,
        }}
        for event in feed._translate(wire):
            await mgr._publish_routed_frame("root", "#vm_coder", _feed_event_to_dict(event), cache)

    await send("worker", "Main", "started")
    await send("helper", "worker", "started")
    await send("helper", "worker", "completed", "Verified the change")
    assert state.get("root/sub-helper")["depth"] == 2
    assert any(ch == "#vm_coder-worker" and "Verified the change" in text
               for ch, text in bot.said)
    assert "#vm_coder-worker-helper" not in bot.destroyed

    await send("worker", "Main", "completed", "Change complete; helper verified it")
    assert any(ch == "#vm_coder" and "Change complete; helper verified it" in text
               for ch, text in bot.said)
    with pytest.raises(KeyError):
        state.get("root/sub-worker")
    with pytest.raises(KeyError):
        state.get("root/sub-helper")
    assert state.get("root")["status"] == "live"


@pytest.mark.asyncio
async def test_no_delegate_is_created_as_an_immortal_root_without_a_parent(tmp_path, monkeypatch):
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    channel = await mgr._ensure_child_room_for("orphan", {"name": "worker", "engine": "omp"})
    assert channel == ""
    assert state.get_live() == []
    assert bot.joined == []


@pytest.mark.asyncio
async def test_idle_omp_parent_publishes_automatic_result_followup(tmp_path, monkeypatch):
    mgr, state, bot, _ = _manager(tmp_path, monkeypatch)
    _spawn_row(state, "root", "coder", "#vm_coder")
    _FakeFeed.live_payloads = [
        {"feed": "message", "role": "assistant", "subagent_id": "",
         "text": "My child reported success; continuing the orchestration"},
    ]
    rooms_mod.register_omp_room("root", "#vm_coder", object())
    try:
        await mgr._pump_live_omp_feed(_FakeFeed(None), "root", "#vm_coder", {}, set())
        assert any(ch == "#vm_coder" and "continuing the orchestration" in text
                   for ch, text in bot.said)
    finally:
        rooms_mod.drop_omp_room("root")
        _FakeFeed.live_payloads = []
