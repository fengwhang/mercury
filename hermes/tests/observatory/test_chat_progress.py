"""Both engine display streams reach chat with their rendering provenance."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from observatory import gateway_session, rooms
from observatory.message_format import frame_kind
from observatory.omp_feed import OmpFeed, StatusEvent
from observatory.omp_feed import agent_turn_frames, child_frame_key
from tools.todo_tool import TodoStore, todo_tool


def test_headless_hermes_streams_authoritative_plan_completion_and_status_without_thinking():
    agent = SimpleNamespace(tool_progress_callback=None, thinking_callback=None,
                            reasoning_callback=None, status_callback=None)
    collector = gateway_session._TurnEventCollector()
    restore = gateway_session._install_collector(agent, collector)
    store = TodoStore()
    try:
        args = {"todos": [{"id": "test", "content": "Check $HOME_path *literally*", "status": "in_progress"}]}
        result = todo_tool(store=store, **args)
        agent.tool_progress_callback("tool.started", "todo", "planning", args)
        agent.tool_progress_callback("tool.completed", "todo", result=result, duration=0.2)
        agent.tool_progress_callback("subagent.complete", "worker", status="completed", goal="Verify transport")
        agent.status_callback("compression", "Context compressed")
        agent.thinking_callback("private scratchpad")
        agent.reasoning_callback("private reasoning")
    finally:
        restore()
    events = collector.events()
    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert statuses == ["To do list:\n• [in_progress] Check $HOME_path *literally*",
                        "Delegate task completed: Verify transport", "Context compressed"]
    assert not any(e["type"] == "thinking" for e in events)
    assert agent.status_callback is None


@pytest.mark.asyncio
async def test_hermes_gateway_streams_completion_plan_and_failure_with_explicit_kinds(monkeypatch):
    from gateway.config import Platform
    from gateway.run import TurnRunner

    sent = []

    async def say(channel, text, *, kind):
        sent.append((channel, text, kind))

    monkeypatch.setattr(rooms, "_current_sink", SimpleNamespace(say=say))
    ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform("irc"), chat_id="#root"),
                          _thinking_enabled=False, _loop_for_step=asyncio.get_running_loop())
    runner = TurnRunner(SimpleNamespace(), ctx)
    args = {"todos": [{"id": "1", "content": "Check routing", "status": "pending"}]}
    result = todo_tool(store=TodoStore(), **args)
    runner.progress_callback("tool.started", "todo", args=args)
    runner.progress_callback("tool.completed", "todo", result=result, duration=0.1)
    runner.progress_callback("tool.completed", "bash", result=json.dumps({"error": "failed"}), is_error=True)
    runner.progress_callback("_thinking", "private scratchpad")
    async with asyncio.timeout(5):
        while len(sent) < 3:
            await asyncio.sleep(0.01)
    assert sent[0][2] == "tool_input"
    assert sent[1] == ("#root", "To do list:\n• [pending] Check routing", "status")
    assert sent[2][2] == "status" and "Tool failed: bash" in sent[2][1]
    assert not any("private scratchpad" in text for _, text, _ in sent)


@pytest.mark.parametrize("sid", ("", "worker"))
def test_omp_streams_tool_results_plans_completions_and_compaction_for_every_depth(sid):
    feed = OmpFeed(None)

    def translate(event):
        if sid:
            return feed._translate({"type": "subagent_event", "payload": {"id": sid, "event": event}})
        return feed._translate_agent_event(event)

    plan = {"phases": [{"name": "Verification", "tasks": [
        {"content": "Verify parent", "status": "completed"},
        {"content": "Check descendants", "status": "blocked", "blocker": "Waiting on parent"},
    ]}]}
    event = {"type": "message_end", "message": {"role": "toolResult", "toolName": "todo",
             "details": plan, "content": [{"type": "text", "text": "short summary"}]}}
    status = translate(event)[0]
    assert isinstance(status, StatusEvent)
    assert status.subagent_id == sid
    assert "• [completed] Verify parent" in status.text
    assert "• [blocked] Check descendants — Waiting on parent" in status.text
    for event in ({"type": "tool_execution_end", "toolName": "bash", "isError": False},
                  {"type": "auto_compaction_start", "reason": "threshold"},
                  {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 2},
                  {"type": "retry_fallback_applied", "from": "test/primary", "to": "test/fallback"},
                  {"type": "retry_fallback_succeeded", "model": "test/fallback"},
                  {"type": "todo_reminder", "todos": [{"content": "Check cleanup"}]},
                  {"type": "todo_auto_clear"}):
        item = translate(event)[0]
        payload = gateway_session._feed_event_to_dict(item)
        assert payload["feed"] == "status" and frame_kind(payload) == "status"
        assert rooms.format_frame(payload)
    output = translate({"type": "message_end", "message": {"role": "toolResult", "toolName": "bash",
                       "content": [{"type": "text", "text": "$HOME_path *literal*"}]}})[0]
    payload = gateway_session._feed_event_to_dict(output)
    assert frame_kind(payload) == "tool_output"
    assert "$HOME_path *literal*" in rooms.format_frame(payload)


def test_replay_preserves_tui_statuses_and_distinguishes_tool_output_from_reply():
    frames = agent_turn_frames([
        {"type": "tool_execution_end", "toolName": "bash", "isError": False},
        {"type": "auto_compaction_start"},
        {"type": "message_end", "message": {"role": "toolResult", "toolName": "todo",
            "details": {"phases": [{"name": "Verification", "tasks": [{"content": "Check replay", "status": "completed"}]}]},
            "content": []}},
    ])
    assert [f["feed"] for f in frames] == ["status", "status", "status"]
    assert "• [completed] Check replay" in frames[-1]["text"]
    assert child_frame_key({"feed": "message", "role": "tool", "text": "hello"}) != child_frame_key(
        {"feed": "message", "role": "assistant", "text": "hello"})


@pytest.mark.asyncio
async def test_omp_extension_notices_and_widgets_stream_without_duplicate_status_updates():
    feed = OmpFeed(None)
    feed._loop = asyncio.get_running_loop()
    feed._on_passive_ui({"type": "extension_ui_request", "method": "notify", "message": "Memory saved"})
    status = {"type": "extension_ui_request", "method": "setStatus", "statusKey": "work", "statusText": "Checking transport"}
    feed._on_passive_ui(status)
    feed._on_passive_ui(status)
    feed._on_passive_ui({"type": "extension_ui_request", "method": "setWidget", "widgetLines": ["Verification", "• Done"]})
    feed._on_passive_ui({"type": "extension_ui_request", "method": "select", "title": "Allow tool"})
    await asyncio.sleep(0)
    output = [feed._queue.get_nowait().text for _ in range(feed._queue.qsize())]
    assert output == ["Memory saved", "Checking transport", "Verification\n• Done"]
