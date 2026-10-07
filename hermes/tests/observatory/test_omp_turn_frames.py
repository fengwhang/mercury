"""Delegation turn trace: PromptTurn.events preserved, live race closed.

The gateway feed watcher attaches up to a poll interval after the child
starts, so early main-session frames never reach its listener. The transport
therefore preserves the turn's own frames (JSON-safe ``turn_frames``) and the
delegation engine replays the live path's misses into the child room — the
multiset renders only occurrences the live forward missed, so genuine repeats
survive and nothing doubles.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OMP_RPC_SRC = REPO_ROOT / "omp" / "python" / "omp-rpc" / "src"
sys.path.insert(0, str(OMP_RPC_SRC))

from observatory import gateway_session as gs  # noqa: E402
from observatory.omp_feed import (  # noqa: E402
    TurnFrameDedupe,
    agent_turn_frames,
    child_frame_key,
)
from tools import omp_delegation as od  # noqa: E402


def _turn_events():
    return [
        {"type": "tool_execution_start", "tool_name": "terminal",
         "args": {"command": "ls /tmp"}},
        {"type": "message_update", "assistant_message_event": {
            "type": "thinking_delta", "contentIndex": 0, "delta": "check"}},
        {"type": "message_update", "assistant_message_event": {
            "type": "thinking_delta", "contentIndex": 0, "delta": "ing it"}},
        {"type": "message_update", "assistant_message_event": {
            "type": "thinking_end", "contentIndex": 0, "content": ""}},
        {"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "done here"}]}},
        {"type": "bogus-frame"},
        {"nope": True},
    ]


def test_agent_turn_frames_translates_self_stream():
    frames = agent_turn_frames(_turn_events())
    assert frames == [
        {"feed": "tool", "subagent_id": "", "tool": "terminal",
         "args": '{"command": "ls /tmp"}'},
        {"feed": "thought", "subagent_id": "", "text": "checking it"},
        {"feed": "message", "subagent_id": "", "role": "assistant",
         "text": "done here"},
    ]


def test_agent_turn_frames_json_safe_and_blank_free():
    frames = agent_turn_frames(_turn_events())
    json.dumps(frames)  # delegation entries cross a json.dumps boundary
    assert frames, "a turn with tools/thinking/text must preserve frames"
    assert agent_turn_frames([]) == []
    assert agent_turn_frames(None) == []
    assert agent_turn_frames([None, {"type": "message_end", "message": None}]) == []


def test_agent_turn_frames_real_dataclass_event():
    from omp_rpc.protocol import ToolExecutionStartEvent

    frames = agent_turn_frames([
        ToolExecutionStartEvent(
            tool_call_id="tc-1", tool_name="bash", args={"cmd": "pwd"}),
    ])
    assert frames == [
        {"feed": "tool", "subagent_id": "", "tool": "bash",
         "args": '{"cmd": "pwd"}'},
    ]


def test_frame_key_identities():
    tool = {"feed": "tool", "subagent_id": "", "tool": "bash", "args": "ls"}
    assert child_frame_key(tool) == child_frame_key(dict(tool))
    assert child_frame_key({"feed": "tool", "subagent_id": "",
                            "tool": "bash", "args": "ls"}) == child_frame_key(tool)
    assert child_frame_key({"feed": "tool", "subagent_id": "sa-1",
                            "tool": "bash", "args": "ls"}) != child_frame_key(tool)
    assert child_frame_key({"feed": "node", "subagent_id": "sa-1"}) is None
    assert child_frame_key({"nope": True}) is None
    assert child_frame_key(None) is None


async def _accepted(*args):
    return True


@pytest.mark.asyncio
async def test_dedupe_replay_renders_only_misses():
    dd = TurnFrameDedupe()
    a = ("tool", "", "bash", '"ls"')
    b = ("thought", "", "listing")
    assert await dd.publish_live(a, _accepted) is True
    indexes = []
    async def publish(index):
        indexes.append(index)
        return True
    assert await dd.publish_replay([a, b], publish) == 1
    assert indexes == [1]
    assert await dd.publish_live(b, _accepted) is False
    assert await dd.publish_live(("tool", "", "new-tool", '"x"'), _accepted) is True


@pytest.mark.asyncio
async def test_dedupe_genuine_repeats_survive():
    dd = TurnFrameDedupe()
    a = ("tool", "", "sleep", '"5"')
    assert await dd.publish_live(a, _accepted) is True
    assert await dd.publish_live(a, _accepted) is True
    assert await dd.publish_replay([a, a], _accepted) == 0
    dd2 = TurnFrameDedupe()
    assert await dd2.publish_live(a, _accepted) is True
    assert await dd2.publish_replay([a, a], _accepted) == 1


@pytest.mark.asyncio
async def test_dedupe_consumes_late_replay_coverage_before_parent_followup():
    dd = TurnFrameDedupe()
    key = ("status", "", "Tool completed: bash")
    assert await dd.publish_replay([key, key], _accepted) == 2
    assert await dd.publish_live(key, _accepted) is False
    assert await dd.publish_live(key, _accepted) is False
    assert await dd.publish_live(key, _accepted) is True


@pytest.fixture()
def _clean_registries():
    for table in (gs._child_dedupe, gs._child_live_feeds):
        table.clear()
    yield
    for table in (gs._child_dedupe, gs._child_live_feeds):
        table.clear()


@pytest.mark.asyncio
async def test_replay_pushes_full_turn_when_live_missed_all(monkeypatch, _clean_registries):
    pushed = []
    from observatory import rooms as _rooms
    monkeypatch.setattr(_rooms, "channel_for_node_id", lambda nid: "#t")
    async def say(channel, text, **kw):
        pushed.append((channel, text))
        return True
    monkeypatch.setattr(_rooms, "say", say)
    frames = agent_turn_frames(_turn_events())
    assert await gs._replay_child_turn_frames("deleg_r/0", frames) == 3
    assert [n for n, _ in pushed] == ["#t"] * 3
    assert "terminal" in pushed[0][1]


@pytest.mark.asyncio
async def test_replay_skips_live_covered_occurrences(monkeypatch, _clean_registries):
    pushed = []
    from observatory import rooms as _rooms
    monkeypatch.setattr(_rooms, "channel_for_node_id", lambda nid: "#t")
    async def say(channel, text, **kw):
        pushed.append((channel, text))
        return True
    monkeypatch.setattr(_rooms, "say", say)
    frames = agent_turn_frames(_turn_events())
    tool_key = child_frame_key(frames[0])
    gs._child_dedupe["deleg_s/0"] = dd = TurnFrameDedupe()
    assert await dd.publish_live(tool_key, _accepted) is True
    assert await gs._replay_child_turn_frames("deleg_s/0", frames) == 2
    assert len(pushed) == 2
    # A second identical replay is a new turn's worth of occurrences: with no
    # live coverage recorded for it, the full turn pushes again.
    assert await gs._replay_child_turn_frames("deleg_s/0", frames) == 3


@pytest.mark.asyncio
async def test_replay_retains_ongoing_live_listeners(monkeypatch, _clean_registries):
    pushed = []
    from observatory import rooms as _rooms
    monkeypatch.setattr(_rooms, "channel_for_node_id", lambda nid: "#t")
    async def say(channel, text, **kw):
        pushed.append((channel, text))
        return True
    monkeypatch.setattr(_rooms, "say", say)
    detached = []
    feed = SimpleNamespace(
        _dispose_listener=lambda: detached.append("subagent"),
        _dispose_agent_listener=lambda: detached.append("agent"),
    )
    gs._child_live_feeds["deleg_d/0"] = feed
    assert await gs._replay_child_turn_frames("deleg_d/0", agent_turn_frames(_turn_events())) == 3
    assert detached == []
    assert callable(feed._dispose_listener)
    assert callable(feed._dispose_agent_listener)
    assert pushed, "replay must not require a feed resubscription"


@pytest.mark.asyncio
async def test_replay_ignores_non_self_and_empty(monkeypatch, _clean_registries):
    pushed = []
    from observatory import rooms as _rooms
    monkeypatch.setattr(_rooms, "channel_for_node_id", lambda nid: "#t")
    async def say(channel, text, **kw):
        pushed.append((channel, text))
        return True
    monkeypatch.setattr(_rooms, "say", say)
    assert await gs._replay_child_turn_frames("deleg_e/0", []) == 0
    assert await gs._replay_child_turn_frames("", agent_turn_frames(_turn_events())) == 0
    assert await gs._replay_child_turn_frames("deleg_e/0", [
        {"feed": "tool", "subagent_id": "sa-9", "tool": "bash", "args": "x"},
        {"feed": "node", "subagent_id": "sa-9"},
    ]) == 0
    assert pushed == []


def test_subscribe_child_feed_at_task_start():
    seen = []

    class _Feedable:
        def set_subagent_subscription(self, level):
            seen.append(level)
            return {}

    od._subscribe_child_feed(_Feedable())
    assert seen == ["events"]
    od._subscribe_child_feed(object())  # kill-only transports: silent no-op


def test_replay_bridge_calls_session_bridge(monkeypatch):
    calls = []
    monkeypatch.setattr(gs, "replay_child_turn_frames",
                        lambda cid, frames: calls.append((cid, frames)) or 2)
    od._replay_child_turn("deleg_b/0", [{"feed": "tool"}])
    assert calls == [("deleg_b/0", [{"feed": "tool"}])]
    od._replay_child_turn("deleg_b/0", [])
    od._replay_child_turn("", [{"feed": "tool"}])
    assert len(calls) == 1, "empty frames/child must not touch the bridge"
