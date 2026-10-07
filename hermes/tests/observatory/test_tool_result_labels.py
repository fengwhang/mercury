"""Replay producer-shaped results, keeping result provenance separate from calls."""
from observatory.gateway_session import _TurnEventCollector
from observatory.message_format import frame_kind
from observatory.progress import hermes_progress_frame
from observatory.rooms import TOOL_PREFIX, format_frame


def test_omp_tool_result_has_result_label_not_invocation():
    frame = {"feed": "message", "role": "tool", "subagent_id": "Fixture",
             "text": "// High-level API\nimport { authPolicyFor } from './policy';"}
    rendered = format_frame(frame)
    assert rendered.startswith("result:")
    assert not rendered.startswith(TOOL_PREFIX)
    assert frame["text"] in rendered
    assert frame_kind(frame) == "tool_output"


def test_hermes_shared_completion_is_result():
    frame = hermes_progress_frame("tool.completed", "terminal", args={"command": "printf output"},
                                  result="output", duration=1)
    assert frame_kind(frame) == "tool_output"
    assert format_frame(frame).startswith("result:")


def test_top_level_collector_result_has_distinct_live_and_replay_type(monkeypatch):
    from observatory import rooms
    calls = []
    monkeypatch.setattr(rooms, "channel_for_node_id", lambda node: "#fixture")
    monkeypatch.setattr(rooms, "say_nowait", lambda channel, text, **kw: calls.append((text, kw["kind"])) or True)
    collector = _TurnEventCollector("fixture")
    collector.tool_progress("tool.started", "terminal", args={"command": "printf output"})
    collector.tool_progress("tool.completed", "terminal", result="output", duration=1)
    assert [e["type"] for e in collector.events()] == ["tool_call", "tool_result"]
    assert [kind for _, kind in calls] == ["tool_input", "tool_output"]
    assert calls[0][0].startswith(TOOL_PREFIX)
    assert calls[1][0].startswith("result:")
