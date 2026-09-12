"""BUG2-SUBAGENT-TRACE RED: gateway child streams its OWN tools+thoughts.

A gateway-origin delegate_task child (omp RPC) runs its task as the child's
MAIN omp session. OmpFeed today only translates subagent_* frames (in-process
grandchildren) — the child's own tool calls and thinking never become events,
so the child room shows lifecycle only. These tests pin the end state:

- OmpFeed translates main-session agent events into self events
  (subagent_id == "" means "the child itself");
- the gateway forwarder preserves self events onto the socket;
- the sidecar renders self tool/thought frames into the CHILD's own room
  (one message per tool call, thinking as a separate message, cot default
  ON for delegation children per spec section 5).
"""

from __future__ import annotations

import json

import pytest

import observatory.sidecar_main as sm
from observatory import gateway_session as gs


# --- OmpFeed: main-session agent events -> self events ------------------------


def test_omp_feed_self_tool_event():
    from observatory.omp_feed import OmpFeed, ToolEvent

    feed = OmpFeed.__new__(OmpFeed)
    feed._states = {}  # type: ignore[attr-defined]
    feed._seq = 0  # type: ignore[attr-defined]
    feed.frame_counts = {}  # type: ignore[attr-defined]
    events = feed._translate_agent_event({
        "type": "tool_execution_start",
        "tool_name": "bash",
        "args": {"command": "ls -la"},
    })
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, ToolEvent)
    assert evt.subagent_id == ""
    assert evt.tool == "bash"
    assert evt.args is not None and "ls -la" in evt.args


def test_omp_feed_self_thought_event():
    from observatory.omp_feed import OmpFeed, ThoughtEvent

    feed = OmpFeed.__new__(OmpFeed)
    feed._states = {}  # type: ignore[attr-defined]
    feed._seq = 0  # type: ignore[attr-defined]
    feed.frame_counts = {}  # type: ignore[attr-defined]
    assert feed._translate_agent_event({
        "type": "message_update",
        "assistant_message_event": {
            "type": "thinking_delta", "contentIndex": 0, "delta": "ponder ",
        },
    }) == []
    events = feed._translate_agent_event({
        "type": "message_update",
        "assistant_message_event": {
            "type": "thinking_end", "contentIndex": 0, "content": "",
        },
    })
    assert len(events) == 1
    assert isinstance(events[0], ThoughtEvent)
    assert events[0].subagent_id == ""
    assert "ponder" in events[0].text


def test_omp_feed_self_message_event():
    from observatory.omp_feed import OmpFeed, MessageEvent

    feed = OmpFeed.__new__(OmpFeed)
    feed._states = {}  # type: ignore[attr-defined]
    feed._seq = 0  # type: ignore[attr-defined]
    feed.frame_counts = {}  # type: ignore[attr-defined]
    events = feed._translate_agent_event({
        "type": "message_end",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "did the thing"}]},
    })
    assert len(events) == 1
    assert isinstance(events[0], MessageEvent)
    assert events[0].subagent_id == ""
    assert "did the thing" in events[0].text


# --- gateway forwarder: self events survive the datagram hop ------------------


def test_feed_event_to_dict_self_tool():
    from observatory.omp_feed import ToolEvent

    payload = gs._feed_event_to_dict(
        ToolEvent(subagent_id="", tool="bash", args="ls"))
    assert payload is not None
    assert payload["feed"] == "tool"
    assert payload.get("subagent_id", "") == ""
    assert payload["tool"] == "bash"


# --- sidecar: self frames render into the CHILD's own room --------------------

from tests.observatory.test_sidecar_live_ingest import (  # noqa: E402
    _sends,
    daemon,  # noqa: F401  (pytest fixture re-export)
    fake_home,  # noqa: F401
)


@pytest.mark.asyncio
async def test_child_self_tool_renders_into_child_room(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        child = "deleg_bug2self/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "self-kid",
        }).encode())
        child_room = daemon.state.get(child)["room_id"]
        assert child_room, "child node must get a planned room"
        before = len(_sends(daemon.client))
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "tool", "subagent_id": "",
                     "tool": "bash", "args": "ls -la"},
        }).encode())
        new_sends = _sends(daemon.client)[before:]
        assert new_sends, "self tool frame must render into the child room"
        assert any("bash" in body for (_, _, body, *_) in new_sends)
        assert any(room == child_room for (_, room, _, *_) in new_sends)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_child_self_thought_renders_into_child_room(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        child = "deleg_bug2think/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "think-kid",
        }).encode())
        # No explicit /cot: delegation children default ON (spec section 5).
        before = len(_sends(daemon.client))
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "thought", "subagent_id": "",
                     "text": "pondering the listing"},
        }).encode())
        bodies = [c[2] for c in _sends(daemon.client)][before:]
        assert any("pondering the listing" in b for b in bodies), (
            "self thought frame must render into the child room"
        )
    finally:
        await daemon.shutdown()

# --- single node identity: discovery room == feed room ------------------------
#
# The discovery poll/hook path (``DiscoveryEngine`` → sidecar
# ``_apply_discovery_event``: ``f"{delegation_id}/{task_index}"``) and the
# gateway datagram path (``_live_child_id`` → watcher ``node_id`` →
# ``_ensure_datagram_child_node``) must name the SAME node so the child gets
# one room, not duplicates. Both writers guard creation with ``state.get``,
# so the second writer is a no-op enrichment.


def test_discovery_and_datagram_share_one_node_id():
    delegation_id, task_index = "deleg_idmatch", 2
    discovery_node_id = f"{delegation_id}/{task_index}"
    import tools.omp_delegation as od

    assert od._live_child_id(delegation_id, task_index) == discovery_node_id


@pytest.mark.asyncio
async def test_second_writer_does_not_duplicate_node(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None
        child = "deleg_idmatch2/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "race-kid",
        }).encode())
        first_room = daemon.state.get(child)["room_id"]
        # Late duplicate start (discovery race) keeps the same room.
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "race-kid",
        }).encode())
        assert daemon.state.get(child)["room_id"] == first_room
    finally:
        await daemon.shutdown()
