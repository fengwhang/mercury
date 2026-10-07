"""OMP rooms reuse Hermes' delayed face store across live room routes."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from observatory import gateway_session as gs
from observatory import rooms as rooms_mod
from observatory import thinking
from observatory.omp_feed import OmpFeed
from observatory.rooms import RoomManager
from observatory.state import ObservatoryState


@pytest_asyncio.fixture
async def room_env(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from observatory import identity, provision
    from plugins.platforms.mirc.adapter import MIRCAdapter

    frames: list[str] = []
    face_sent = asyncio.Event()
    faces = thinking.thinking_faces()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(thinking.random, "choice", lambda store: store[0])
    monkeypatch.setattr(thinking, "THINKING_FACE_DELAY_S", 30.0)

    class Writer:
        def is_closing(self):
            return False

        def write(self, data):
            text = data.decode()
            frames.append(text)
            if "+mercury/kind=thinking" in text and faces[0] in text:
                face_sent.set()

        async def drain(self):
            pass

    bot = MIRCAdapter(PlatformConfig(enabled=True, extra={
        "server": "localhost", "port": 6667, "nickname": "testbot",
        "channel": "#vm_gateway", "use_tls": False,
    }))
    bot._writer = Writer()
    monkeypatch.setattr(identity, "get_pool", lambda: SimpleNamespace(get=lambda _: None))
    monkeypatch.setattr(identity, "ensure_identity", AsyncMock())
    monkeypatch.setattr(provision, "live_server_name", lambda home=None: "vm")
    monkeypatch.setattr(provision, "get_mlounge_nick", lambda home=None: "owner")
    state = ObservatoryState(tmp_path / "state.db")
    state.add_node("parent", engine="omp", name="parent", slug="parent",
                   mxid="vm_parent", session_ref="parent", extra={"kind": "spawn"})
    state.set_room_id("parent", "#vm_parent")
    manager = RoomManager(state, bot)
    rooms_mod.set_room_manager(manager)
    rooms_mod.set_bot_sink(bot)
    rooms_mod.set_event_loop(asyncio.get_running_loop())
    yield SimpleNamespace(manager=manager, bot=bot, state=state, frames=frames,
                          face_sent=face_sent, faces=faces)
    for channel in list(thinking._tasks):
        thinking.thinking_done(channel)
    rooms_mod.set_room_manager(None)
    rooms_mod.set_bot_sink(None)
    rooms_mod.set_event_loop(None)


@pytest.mark.asyncio
async def test_spawned_turn_posts_existing_face_while_rpc_runs(room_env, monkeypatch):
    env = room_env
    monkeypatch.setattr(thinking, "THINKING_FACE_DELAY_S", 0)
    monkeypatch.setattr(env.manager, "_start_live_omp_feed", AsyncMock(return_value=None))
    release = threading.Event()

    class RPC:
        def run_task(self, prompt):
            assert release.wait(5)
            return {"summary": "finished"}

    turn = asyncio.create_task(env.manager._run_spawned_omp_task(
        "#vm_parent", "parent", "owner", "work", RPC()))
    try:
        await asyncio.wait_for(env.face_sent.wait(), 2)
        assert not turn.done()
        assert sum(env.faces[0] in frame for frame in env.frames) == 1
    finally:
        release.set()
        await turn
    assert "#vm_parent" not in thinking._tasks
    assert any("finished" in frame for frame in env.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_fast_and_failed_spawned_turns_clear_pending_face(room_env, monkeypatch, fail):
    env = room_env
    monkeypatch.setattr(env.manager, "_start_live_omp_feed", AsyncMock(return_value=None))

    class RPC:
        def run_task(self, prompt):
            if fail:
                raise RuntimeError("fake RPC failure")
            return {"summary": "quick reply"}

    await env.manager._run_spawned_omp_task("#vm_parent", "parent", "owner", "work", RPC())
    assert "#vm_parent" not in thinking._tasks
    assert not env.face_sent.is_set()
    expected = "task failed" if fail else "quick reply"
    assert any(expected in frame for frame in env.frames)


@pytest.mark.asyncio
async def test_native_descendant_uses_same_face_and_clears_on_death(room_env, monkeypatch):
    env = room_env
    monkeypatch.setattr(thinking, "THINKING_FACE_DELAY_S", 0)
    feed = OmpFeed(None)
    cache = {}
    event = feed._translate({"type": "subagent_lifecycle", "payload": {
        "id": "child", "agent": "task", "status": "started",
    }})[0]
    await env.manager._publish_routed_frame("parent", "#vm_parent", gs._feed_event_to_dict(event), cache)
    channel = cache["child"]
    await asyncio.wait_for(env.face_sent.wait(), 2)
    assert any(f"PRIVMSG {channel} :{env.faces[0]}" in frame for frame in env.frames)
    end = feed._translate({"type": "subagent_lifecycle", "payload": {
        "id": "child", "agent": "task", "status": "failed",
    }})[0]
    await env.manager._publish_routed_frame("parent", "#vm_parent", gs._feed_event_to_dict(end), cache)
    assert channel not in thinking._tasks
    assert "child" not in cache


@pytest.mark.asyncio
async def test_watcher_room_waits_for_actual_activity_before_thinking(room_env):
    env = room_env
    channel = await asyncio.to_thread(gs._ensure_watcher_room, "deleg/0", {
        "name": "worker", "owner_session_id": "parent",
    })
    assert channel not in thinking._tasks
    feed = OmpFeed(None)
    # A retained room is topology, not proof that a task has begun thinking.
    start = gs._feed_event_to_dict(feed._translate_agent_event({"type": "agent_start"})[0])
    await gs._publish_live_payload("deleg/0", start, {})
    assert channel in thinking._tasks
    end = gs._feed_event_to_dict(feed._translate_agent_event({"type": "agent_end"})[0])
    await gs._publish_live_payload("deleg/0", end, {})
    assert channel not in thinking._tasks
    # Control frames never become chat-history status messages.
    assert not any("agent_start" in frame or "agent_end" in frame for frame in env.frames)


@pytest.mark.asyncio
async def test_watcher_descendant_restarts_indicator_for_later_runs(room_env):
    env = room_env
    await asyncio.to_thread(gs._ensure_watcher_room, "deleg/0", {
        "name": "worker", "owner_session_id": "parent",
    })
    feed = OmpFeed(None)
    cache = {}
    for event_type in ("agent_start", "agent_end", "agent_start"):
        event = feed._translate({"type": "subagent_event", "payload": {
            "id": "grandchild", "event": {"type": event_type},
        }})[0]
        await gs._publish_live_payload("deleg/0", gs._feed_event_to_dict(event), cache)
        channel = cache["grandchild"]
        assert (channel in thinking._tasks) == (event_type == "agent_start")
    assert "#vm_parent" not in thinking._tasks
    from tools import async_delegation as ad
    ad.record_child_spawn("deleg/0", "deleg")
    ad.record_child_terminal("deleg/0", "completed", summary="verified task completion")
    await asyncio.to_thread(gs._retire_watcher_room, "deleg/0", name="worker")
    assert channel not in thinking._tasks


@pytest.mark.asyncio
async def test_steering_ack_keeps_active_omp_room_indicator(room_env):
    env = room_env
    steered = []
    rooms_mod._omp_rooms["parent"] = {
        "channel": "#vm_parent", "busy": True,
        "rpc": SimpleNamespace(steer=steered.append),
    }
    thinking.thinking_started("#vm_parent")
    pending = thinking._tasks["#vm_parent"]
    try:
        await env.bot._dispatch_message(
            "change direction", "#vm_parent", "group", "user", "owner")
        assert steered == ["[owner over IRC] change direction"]
        assert thinking._tasks["#vm_parent"] is pending
        assert not pending.cancelling()
        assert any("+mercury/kind=status" in frame and "steered mid-run" in frame
                   for frame in env.frames)
    finally:
        rooms_mod._omp_rooms.pop("parent", None)


@pytest.mark.asyncio
async def test_failed_rpc_clears_start_frame_drained_during_teardown(room_env, monkeypatch):
    env = room_env
    disconnected = asyncio.Event()

    class BufferedFeed:
        async def events(self):
            await disconnected.wait()
            yield {"feed": "activity", "subagent_id": "", "active": True}

        async def stop(self):
            disconnected.set()

    class RPC:
        def run_task(self, prompt):
            raise RuntimeError("lost RPC connection")

    monkeypatch.setattr(env.manager, "_start_live_omp_feed", AsyncMock(return_value=BufferedFeed()))
    await env.manager._run_spawned_omp_task("#vm_parent", "parent", "owner", "work", RPC())
    assert any("task failed" in frame for frame in env.frames)
    assert "#vm_parent" not in thinking._tasks
    assert not env.face_sent.is_set()


@pytest.mark.asyncio
async def test_late_run_end_cannot_create_a_descendant_room(room_env):
    env = room_env
    feed = OmpFeed(None)
    event = feed._translate({"type": "subagent_event", "payload": {
        "id": "gone", "event": {"type": "agent_end"},
    }})[0]
    payload = gs._feed_event_to_dict(event)
    await gs._publish_live_payload("parent", payload, {})
    await env.manager._publish_routed_frame("parent", "#vm_parent", payload, {})
    assert env.manager.channel_for_node("parent/sub-gone") == ""
    assert env.frames == []
    assert "#vm_parent-sub-gone" not in thinking._tasks
