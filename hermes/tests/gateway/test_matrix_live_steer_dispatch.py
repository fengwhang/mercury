"""Matrix room text during a live gateway-session turn (zero new commands).

Failing case (a): the gateway turn is LIVE (mid-tool-loop) when Matrix room
plain text arrives. The text must reach the SAME cached agent object via the
existing redirect-then-steer machinery with the text in its context — no
second turn. Content-agnostic: `stop` and `apple` take the identical path.

Gateway-side live-steer runs inside the `inject` dispatch
(:func:`gateway.run._observatory_inject_dispatch`): ``kind == "steer"``
non-internal text tries :func:`gateway.run._attempt_matrix_live_steer`
first; every miss (idle, empty, no surface, prompt/command/internal)
falls through to the normal fresh turn unchanged.
"""
from __future__ import annotations

import threading

import pytest

from gateway.run import (
    _attempt_matrix_live_steer,
    _matrix_steer_text_targets_live_turn,
    _observatory_inject_dispatch,
)
from observatory import gateway_session as gs


class FakeLiveAgent:
    """Cached-agent double with real redirect/steer delivery semantics."""

    def __init__(self, *, model_active=False, executing_tools=False):
        self.redirect_calls: list = []
        self.steer_calls: list = []
        self._pending_redirect = None
        self._pending_steer = None
        self._model_request_active = threading.Event()
        if model_active:
            self._model_request_active.set()
        self._executing_tools = executing_tools

    def redirect(self, text):
        self.redirect_calls.append(text)
        if not text or not str(text).strip():
            return False
        if self._executing_tools:
            return self.steer(text)
        if not self._model_request_active.is_set():
            return False
        self._pending_redirect = str(text).strip()
        return True

    def steer(self, text):
        self.steer_calls.append(text)
        if not text or not str(text).strip():
            return False
        clean = str(text).strip()
        self._pending_steer = (
            (self._pending_steer + "\n" + clean) if self._pending_steer else clean
        )
        return True


@pytest.fixture()
def live_model_agent():
    saved_agents = dict(gs._session_agents)
    saved_locks = dict(gs._session_locks)
    gs._session_agents.clear()
    gs._session_locks.clear()
    agent = FakeLiveAgent(model_active=True)
    gs._session_agents[gs.GATEWAY_SESSION_ID] = agent
    try:
        yield agent
    finally:
        gs._session_agents.clear()
        gs._session_agents.update(saved_agents)
        gs._session_locks.clear()
        gs._session_locks.update(saved_locks)


@pytest.fixture()
def live_tool_agent():
    saved_agents = dict(gs._session_agents)
    saved_locks = dict(gs._session_locks)
    gs._session_agents.clear()
    gs._session_locks.clear()
    agent = FakeLiveAgent(executing_tools=True)
    gs._session_agents[gs.GATEWAY_SESSION_ID] = agent
    try:
        yield agent
    finally:
        gs._session_agents.clear()
        gs._session_agents.update(saved_agents)
        gs._session_locks.clear()
        gs._session_locks.update(saved_locks)


@pytest.fixture()
def idle_gateway():
    saved_agents = dict(gs._session_agents)
    saved_locks = dict(gs._session_locks)
    gs._session_agents.clear()
    gs._session_locks.clear()
    try:
        yield
    finally:
        gs._session_agents.clear()
        gs._session_agents.update(saved_agents)
        gs._session_locks.clear()
        gs._session_locks.update(saved_locks)


def _fresh_recorder():
    calls: list = []

    def run_prompt_fn(text, *, kind, node_id, room_id, internal):
        calls.append(
            {
                "text": text,
                "kind": kind,
                "node_id": node_id,
                "room_id": room_id,
                "internal": internal,
            }
        )
        return (f"reply:{text}", [])

    run_prompt_fn.calls = calls  # type: ignore[attr-defined]
    return run_prompt_fn


def _steer_params(text, *, kind="steer", internal=False):
    return {
        "text": text,
        "kind": kind,
        "node_id": "gw",
        "room_id": "!gw:x",
        "internal": internal,
    }


class TestGate:
    @pytest.mark.parametrize("kind", ["prompt", "command"])
    def test_non_steer_kinds_never_target_live_turn(self, kind):
        assert _matrix_steer_text_targets_live_turn(kind, False) is False

    def test_internal_followup_never_targets_live_turn(self):
        assert _matrix_steer_text_targets_live_turn("steer", True) is False

    def test_room_steer_targets_live_turn(self):
        assert _matrix_steer_text_targets_live_turn("steer", False) is True


class TestLiveModelRequest:
    @pytest.mark.parametrize("text", ["stop", "apple"])
    def test_redirect_cuts_live_request_no_second_turn(self, live_model_agent, text):
        run_prompt_fn = _fresh_recorder()
        out = _observatory_inject_dispatch(_steer_params(text), run_prompt_fn)

        assert out == {"reply": "", "events": [], "steered": True}
        assert run_prompt_fn.calls == []
        assert gs._session_agents[gs.GATEWAY_SESSION_ID] is live_model_agent
        assert live_model_agent.redirect_calls == [text]
        assert live_model_agent._pending_redirect == text

    @pytest.mark.parametrize("text", ["stop", "apple"])
    def test_attempt_reports_steered_with_text_in_context(
        self, live_model_agent, text
    ):
        out = _attempt_matrix_live_steer(text)
        assert out.get("steered") is True
        assert live_model_agent._pending_redirect == text


class TestLiveToolExecution:
    @pytest.mark.parametrize("text", ["stop", "apple"])
    def test_steer_buffer_lands_mid_turn_no_second_turn(
        self, live_tool_agent, text
    ):
        run_prompt_fn = _fresh_recorder()
        out = _observatory_inject_dispatch(_steer_params(text), run_prompt_fn)

        assert out == {"reply": "", "events": [], "steered": True}
        assert run_prompt_fn.calls == []
        assert gs._session_agents[gs.GATEWAY_SESSION_ID] is live_tool_agent
        assert live_tool_agent.redirect_calls == [text]
        assert live_tool_agent._pending_steer == text


class TestIdleFallbackUnchanged:
    @pytest.mark.parametrize("text", ["stop", "apple"])
    def test_idle_runs_single_fresh_turn(self, idle_gateway, text):
        run_prompt_fn = _fresh_recorder()
        out = _observatory_inject_dispatch(_steer_params(text), run_prompt_fn)

        assert out == {"reply": f"reply:{text}"}
        assert len(run_prompt_fn.calls) == 1
        assert run_prompt_fn.calls[0]["text"] == text
        assert run_prompt_fn.calls[0]["kind"] == "steer"

    def test_empty_text_never_steers_falls_through(self, live_model_agent):
        run_prompt_fn = _fresh_recorder()
        out = _observatory_inject_dispatch(_steer_params("   "), run_prompt_fn)

        assert live_model_agent.redirect_calls == []
        assert live_model_agent.steer_calls == []
        assert len(run_prompt_fn.calls) == 1

    @pytest.mark.parametrize(
        "params",
        [
            _steer_params("stop", kind="prompt"),
            _steer_params("stop", kind="command"),
            _steer_params("stop", internal=True),
        ],
    )
    def test_non_room_text_skips_live_steer(self, live_model_agent, params):
        run_prompt_fn = _fresh_recorder()
        out = _observatory_inject_dispatch(params, run_prompt_fn)

        assert live_model_agent.redirect_calls == []
        assert live_model_agent.steer_calls == []
        assert len(run_prompt_fn.calls) == 1
        assert out["reply"].startswith("reply:")

    def test_event_cap_preserved_on_fresh_turn(self, idle_gateway):
        def big_run(text, *, kind, node_id, room_id, internal):
            return ("r", [{"seq": i} for i in range(250)])

        out = _observatory_inject_dispatch(_steer_params("stop"), big_run)
        assert out["reply"] == "r"
        assert len(out["events"]) == 200
