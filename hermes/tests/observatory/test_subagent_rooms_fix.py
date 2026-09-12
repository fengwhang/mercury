"""Subagent-rooms fix wave: live streams, space purge, resumed-turn visibility.

DEFECT 1 (subagent rooms empty): a working subagent's room must stream
tool calls + thinking + messages live, same as depth-0 rooms.
DEFECT 2 (space tombstone): a dead subagent's space AND room are purged,
leave-then-delete covering both.
DEFECT 3 (resumed turn invisible): the continued parent turn (internal /
quiet follow-up) must render status + tool calls live, same as a normal
turn — internal/quiet gate only the FINAL reply render, never the live
stream. Seq dedupe stays intact.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

import observatory.sidecar_main as sm
from tests.observatory.test_sidecar_live_ingest import (
    FakeLiveTransport,
    _drain_gateway_tasks,
    _gw_room,
    _sends,
    daemon,
    fake_home,
)

__all__ = ["daemon", "fake_home"]


def _gc_room(daemon: sm.SidecarDaemon, node_id: str) -> str:
    assert daemon.state is not None
    return daemon.state.get(node_id)["room_id"]


class ScriptFeed:
    """Canned OmpFeed surface for _run_omp_feed."""

    def __init__(self, events):
        self._events = list(events)
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def events(self):
        for e in self._events:
            yield e


async def _seed_omp_child(daemon: sm.SidecarDaemon, node_id: str = "deleg_seed/0") -> str:
    """One live depth-1 omp child with provisioned space+room."""
    from observatory.identity import assign_slug, virtual_mxid

    assert daemon.state is not None and daemon.renderer is not None
    slug = assign_slug("seed-kid", daemon.state)
    daemon.state.add_node(
        node_id,
        engine="omp",
        name="seed-kid",
        slug=slug,
        mxid=virtual_mxid(slug, server_name="mercury.local"),
        session_ref=f"omp-child:{node_id}",
        parent_node_id=sm.GATEWAY_NODE_ID,
        extra={"engine_child": True},
    )
    import socket as _socket

    await daemon.renderer.apply_plan(daemon.renderer.build_plan(host=_socket.gethostname()))
    return node_id


# --- DEFECT 3: resumed (internal/quiet) parent turn renders live -----------


@pytest.mark.asyncio
async def test_internal_turn_progress_renders_tool_call_live(daemon: sm.SidecarDaemon):
    """In-flight internal turn: tool_call datagram renders to the room
    (seq still recorded for replay dedupe)."""
    await daemon.boot()
    try:
        assert daemon.client is not None
        gw = sm.GATEWAY_NODE_ID
        room = _gw_room(daemon)
        daemon._gateway_internal_turns[gw] = True  # resumed turn in flight
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._handle_turn_progress_datagram({
            "node_id": gw, "seq": 3,
            "event": {"type": "tool_call", "tool": "bash", "args": {"cmd": "ls"}},
        })
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("bash" in b for b in bodies), f"tool call must render live, got {bodies}"
        assert 3 in daemon._gateway_live_seqs.get(gw, set()), "seq dedupe must stay intact"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_internal_replay_renders_tool_history(daemon: sm.SidecarDaemon):
    """Batched replay during an internal turn renders tool calls
    (unseen seqs), same as a normal turn."""
    await daemon.boot()
    try:
        assert daemon.client is not None
        gw = sm.GATEWAY_NODE_ID
        room = _gw_room(daemon)
        daemon._gateway_internal_turns[gw] = True
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._replay_gateway_events(gw, [
            {"seq": 9, "type": "tool_call", "tool": "bash", "args": {}},
        ])
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("bash" in b for b in bodies), f"replay must render tools, got {bodies}"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_quiet_followup_renders_live_but_not_final_reply(daemon: sm.SidecarDaemon):
    """quiet suppresses ONLY the final reply: the resumed turn's tool
    history still renders."""
    await daemon.boot()
    try:
        assert daemon.client is not None
        gw = sm.GATEWAY_NODE_ID
        room = _gw_room(daemon)
        daemon.gateway_transport = FakeLiveTransport(
            reply="continued ok",
            events=[{"seq": 0, "type": "tool_call", "tool": "bash", "args": {}}],
        )
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._deliver_gateway_prompt(gw, "followup", internal=True, quiet=True)
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("bash" in b for b in bodies), f"live tools must render, got {bodies}"
        assert not any("continued ok" in b for b in bodies), "quiet must suppress final reply"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_routine_child_death_resumes_gateway_with_visible_work(daemon: sm.SidecarDaemon):
    """End to end: routine child death injects the gateway follow-up, and
    the resumed turn's tool events render live in the gateway room."""
    from observatory.discovery import NodeEvent

    await daemon.boot()
    try:
        assert daemon.client is not None
        gw = sm.GATEWAY_NODE_ID
        room = _gw_room(daemon)
        daemon.gateway_transport = FakeLiveTransport(
            reply="verified, all green",
            events=[{"seq": 0, "type": "tool_call", "tool": "bash", "args": {}}],
        )
        await daemon._apply_discovery_event(NodeEvent(
            kind="add", delegation_id="deleg_r9", task_index=0,
            parent_session="", name="r9-kid", goal="g",
            status="running", source="poll", seq=1,
        ))
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._apply_discovery_event(NodeEvent(
            kind="death", delegation_id="deleg_r9", task_index=0,
            parent_session="", name="r9-kid", goal="g",
            status="completed", source="poll", seq=2,
            summary="rotation done",
        ))
        await _drain_gateway_tasks(daemon)
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("bash" in b for b in bodies), (
            f"resumed gateway turn must show work live, got {bodies}")
    finally:
        await daemon.shutdown()


# --- DEFECT 1: subagent rooms stream tool + thinking + messages ------------


@pytest.mark.asyncio
async def test_omp_feed_streams_tool_thinking_message(daemon: sm.SidecarDaemon):
    """Direct OmpFeed consume: tool + thinking + assistant message all
    land in the grandchild room."""
    from observatory.omp_feed import MessageEvent, NodeEvent, ThoughtEvent, ToolEvent

    await daemon.boot()
    try:
        assert daemon.client is not None
        child = await _seed_omp_child(daemon)
        feed = ScriptFeed([
            NodeEvent(kind="add", subagent_id="s1", parent_tool_call_id=None,
                      status="running", agent="worker", task="do it"),
            ToolEvent(subagent_id="s1", tool="bash", args="ls"),
            ThoughtEvent(subagent_id="s1", text="listing files"),
            MessageEvent(subagent_id="s1", role="assistant", text="listed two files"),
        ])
        await daemon._run_omp_feed(child, feed)
        gc = f"{child}/gc:s1"
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == _gc_room(daemon, gc)]
        assert any("bash" in b for b in bodies), f"tool missing: {bodies}"
        assert any("listing files" in b for b in bodies), f"thinking missing: {bodies}"
        assert any("listed two files" in b for b in bodies), f"message missing: {bodies}"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_omp_feed_tool_without_box_entry_readopts(daemon: sm.SidecarDaemon):
    """Feed restart (boxes map lost, node row survives): tool frames
    resolve via the deterministic grandchild id instead of dropping."""
    from observatory.omp_feed import NodeEvent, ToolEvent

    await daemon.boot()
    try:
        assert daemon.client is not None
        child = await _seed_omp_child(daemon, "deleg_restart/0")
        gc = f"{child}/gc:s9"
        # earlier generation provisioned the grandchild row + room
        await daemon._render_grandchild(child, _ns(
            kind="add", subagent_id="s9", agent="worker", task="do it"))
        # restarted feed: boxes map is empty, tool frame arrives first
        await daemon._run_omp_feed(child, ScriptFeed([
            ToolEvent(subagent_id="s9", tool="bash", args="pwd"),
        ]))
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == _gc_room(daemon, gc)]
        assert any("bash" in b for b in bodies), f"restarted feed dropped tool: {bodies}"
    finally:
        await daemon.shutdown()


def _ns(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


@pytest.mark.asyncio
async def test_child_datagram_message_renders(daemon: sm.SidecarDaemon):
    """Gateway-forwarded message frames render into the grandchild room."""
    await daemon.boot()
    try:
        assert daemon.client is not None
        child = await _seed_omp_child(daemon, "deleg_msg/0")
        gc = f"{child}/gc:sm1"
        await daemon._render_grandchild(child, _ns(
            kind="add", subagent_id="sm1", agent="worker", task="do it"))
        await daemon._handle_child_feed_datagram({
            "node_id": child,
            "feed": {"feed": "message", "subagent_id": "sm1",
                     "role": "assistant", "text": "halfway there"},
        })
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == _gc_room(daemon, gc)]
        assert any("halfway there" in b for b in bodies), f"message missing: {bodies}"
    finally:
        await daemon.shutdown()


def test_feed_event_to_dict_forwards_message():
    """Producer parity: MessageEvent forwards as feed=message."""
    from observatory import gateway_session as gs
    from observatory.omp_feed import MessageEvent

    out = gs._feed_event_to_dict(MessageEvent(
        subagent_id="sa-1", role="assistant", text="hi"))
    assert out is not None and out.get("feed") == "message"
    assert out.get("text") == "hi"


@pytest.mark.asyncio
async def test_discovery_death_purges_space_and_room(daemon: sm.SidecarDaemon):
    """VM-observed subagent path: discovery death purges the child's
    space AND room, then drops the rows."""
    from observatory.discovery import NodeEvent
    from observatory.state import StateError

    await daemon.boot()
    try:
        assert daemon.client is not None and daemon.state is not None
        await daemon._apply_discovery_event(NodeEvent(
            kind="add", delegation_id="deleg_d9", task_index=0,
            parent_session="", name="d9-kid", goal="g",
            status="running", source="poll", seq=1,
        ))
        row = daemon.state.get("deleg_d9/0")
        room_id, space_id = row["room_id"], row["space_id"]
        assert room_id and space_id, "child must have room+space before death"
        await daemon._apply_discovery_event(NodeEvent(
            kind="death", delegation_id="deleg_d9", task_index=0,
            parent_session="", name="d9-kid", goal="g",
            status="completed", source="poll", seq=2,
            summary="did stuff",
        ))
        deletes = {c[1] for c in daemon.client.calls if c[0] == "delete"}
        assert room_id in deletes, f"room {room_id} not purged: {deletes}"
        assert space_id in deletes, f"space {space_id} left as tombstone: {deletes}"
        detaches = [c for c in daemon.client.calls if c[0] == "child" and c[5]]
        assert any(c[2] == space_id for c in detaches), (
            f"space {space_id} never detached from surviving parent: {detaches}")
        with pytest.raises(StateError):
            daemon.state.get("deleg_d9/0")
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_lifecycle_stop_purges_space_and_room(daemon: sm.SidecarDaemon):
    """Datagram lifecycle path: child stop purges space AND room."""
    from observatory.state import StateError

    await daemon.boot()
    try:
        assert daemon.client is not None and daemon.state is not None
        await daemon._handle_child_lifecycle_datagram({
            "node_id": "deleg_c9/0", "lifecycle": "start", "name": "c9-kid",
        })
        row = daemon.state.get("deleg_c9/0")
        room_id, space_id = row["room_id"], row["space_id"]
        assert room_id and space_id, "child must have room+space before death"
        await daemon._handle_child_lifecycle_datagram({
            "node_id": "deleg_c9/0", "lifecycle": "stop",
            "status": "completed", "summary": "done",
        })
        deletes = {c[1] for c in daemon.client.calls if c[0] == "delete"}
        assert room_id in deletes, f"room {room_id} not purged: {deletes}"
        assert space_id in deletes, f"space {space_id} left as tombstone: {deletes}"
        with pytest.raises(StateError):
            daemon.state.get("deleg_c9/0")
    finally:
        await daemon.shutdown()
