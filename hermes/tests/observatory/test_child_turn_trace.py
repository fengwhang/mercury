"""Child turns ride the gateway turn machinery (collector + live + replay).

RED test for the unification fix: a hermes child turn must capture
tool calls and thinking through the SAME _TurnEventCollector the
gateway turn uses, live-push them under the CHILD node id, and replay
them into the CHILD room — not run a bare run_conversation whose
trace dies in the worker thread.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import observatory.sidecar_main as sm


class FakeAgent:
    """Agent double that fires display callbacks during its turn."""

    def __init__(self):
        self.turns: list[str] = []
        self.tool_progress_callback = None
        self.thinking_callback = None
        self.reasoning_callback = None

    def run_conversation(self, text):
        self.turns.append(text)
        if callable(self.tool_progress_callback):
            self.tool_progress_callback(
                "tool.started", "terminal",
                args={"command": "ls /tmp"},
            )
        if callable(self.thinking_callback):
            self.thinking_callback("checking the directory listing")
        return {"final_response": f"reply:{text}"}


class FakeRenderer:
    def __init__(self):
        self.tool_calls: list[tuple] = []
        self.thinkings: list[tuple] = []
        self.messages: list[tuple] = []

    async def render_tool_call(self, node_id, tool, args=None, **_kw):
        self.tool_calls.append((node_id, tool, args))
        return []

    async def render_thinking(self, node_id, text):
        self.thinkings.append((node_id, text))
        return []

    async def render_agent_message(self, node_id, text):
        self.messages.append((node_id, text))
        return []


class FakeState:
    def get(self, node_id):
        return {"room_id": "!child:vm", "mxid": "@merc_child:vm"}


def _daemon(monkeypatch, tmp_path) -> sm.SidecarDaemon:
    d = sm.SidecarDaemon.__new__(sm.SidecarDaemon)
    d._gateway_live_seqs = {}
    d._gateway_internal_turns = {}
    d._cot_status_event = {}
    d._child_locks = {}
    d._hermes_inflight = {}
    d._child_resume_errors = {}
    d._child_tasks = set()
    d.renderer = FakeRenderer()
    d.state = FakeState()
    d.control_router = SimpleNamespace(
        cot_enabled=lambda node_id: True,
    )
    import observatory.sidecar_main as _sm

    async def _noop_notice(notice):
        return None

    monkeypatch.setattr(d, "_post_notice", _noop_notice)
    return d


@pytest.mark.asyncio()
async def test_hermes_child_turn_captures_trace_into_child_room(
    monkeypatch, tmp_path,
) -> None:
    """The child turn's tool call + thinking reach the CHILD room."""
    d = _daemon(monkeypatch, tmp_path)
    agent = FakeAgent()
    handle = SimpleNamespace(agent=agent, engine="hermes")
    monkeypatch.setattr(d, "_child_handle", lambda node_id: handle)

    ok = await d._run_hermes_child_turn("child-1", "do the thing")
    assert ok is True

    renderer = d.renderer
    assert renderer.tool_calls, "no tool call reached the child room"
    assert renderer.tool_calls[0][0] == "child-1"
    assert renderer.tool_calls[0][1] == "terminal"
    assert renderer.thinkings, "no thinking reached the child room"
    assert renderer.thinkings[0][0] == "child-1"
    assert renderer.messages, "no reply reached the child room"
    assert renderer.messages[0][0] == "child-1"
    assert "reply:do the thing" in renderer.messages[0][1]
