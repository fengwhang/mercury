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


def test_dedupe_replay_renders_only_misses():
    dd = TurnFrameDedupe()
    a = ("tool", "", "bash", '"ls"')
    b = ("thought", "", "listing")
    assert dd.live_hit(a) is False  # live forwarded A first
    assert dd.replay_indexes([a, b]) == [1]  # only B still missing
    assert dd.live_hit(b) is True  # B arrives late — replay covered it
    assert dd.live_hit(("tool", "", "new-tool", '"x"')) is False


def test_dedupe_genuine_repeats_survive():
    dd = TurnFrameDedupe()
    a = ("tool", "", "sleep", '"5"')
    assert dd.live_hit(a) is False
    assert dd.live_hit(a) is False  # the turn really ran it twice
    assert dd.replay_indexes([a, a]) == []  # both already live
    dd2 = TurnFrameDedupe()
    assert dd2.live_hit(a) is False  # live saw one of two
    assert dd2.replay_indexes([a, a]) == [1]  # the missed repeat replays


@pytest.fixture()
def _clean_registries():
    for table in (gs._child_dedupe, gs._child_live_feeds):
        table.clear()
    yield
    for table in (gs._child_dedupe, gs._child_live_feeds):
        table.clear()


def test_replay_pushes_full_turn_when_live_missed_all(monkeypatch, _clean_registries):
    pushed = []
    monkeypatch.setattr(gs, "push_child_feed_event",
                        lambda node_id, feed: pushed.append((node_id, feed)))
    frames = agent_turn_frames(_turn_events())
    assert gs.replay_child_turn_frames("deleg_r/0", frames) == 3
    assert [n for n, _ in pushed] == ["deleg_r/0"] * 3
    assert pushed[0][1]["feed"] == "tool"


def test_replay_skips_live_covered_occurrences(monkeypatch, _clean_registries):
    pushed = []
    monkeypatch.setattr(gs, "push_child_feed_event",
                        lambda node_id, feed: pushed.append((node_id, feed)))
    frames = agent_turn_frames(_turn_events())
    tool_key = child_frame_key(frames[0])
    gs._child_dedupe["deleg_s/0"] = dd = TurnFrameDedupe()
    assert dd.live_hit(tool_key) is False  # live forwarded the tool call
    assert gs.replay_child_turn_frames("deleg_s/0", frames) == 2
    assert [f["feed"] for _, f in pushed] == ["thought", "message"]
    # A second identical replay is a new turn's worth of occurrences: with no
    # live coverage recorded for it, the full turn pushes again.
    assert gs.replay_child_turn_frames("deleg_s/0", frames) == 3


def test_replay_detaches_live_listeners_first(monkeypatch, _clean_registries):
    pushed = []
    monkeypatch.setattr(gs, "push_child_feed_event",
                        lambda node_id, feed: pushed.append((node_id, feed)))
    detached = []
    feed = SimpleNamespace(
        _dispose_listener=lambda: detached.append("subagent"),
        _dispose_agent_listener=lambda: detached.append("agent"),
    )
    gs._child_live_feeds["deleg_d/0"] = feed
    assert gs.replay_child_turn_frames("deleg_d/0", agent_turn_frames(_turn_events())) == 3
    assert sorted(detached) == ["agent", "subagent"]
    assert feed._dispose_listener is None
    assert pushed, "detach must not swallow the replay"


def test_replay_ignores_non_self_and_empty(monkeypatch, _clean_registries):
    pushed = []
    monkeypatch.setattr(gs, "push_child_feed_event",
                        lambda node_id, feed: pushed.append((node_id, feed)))
    assert gs.replay_child_turn_frames("deleg_e/0", []) == 0
    assert gs.replay_child_turn_frames("", agent_turn_frames(_turn_events())) == 0
    assert gs.replay_child_turn_frames("deleg_e/0", [
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
