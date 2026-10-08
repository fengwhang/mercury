"""Retained lifecycle/privacy purposes at Mercury's current OMP boundary.

The former subagent_stop plugin callback and child roles do not exist in the
single-OMP engine. Completion is one consolidated event per accepted batch,
with one result per task. Detached workers inherit parent authority/context;
there is no callback-on-caller-thread promise. Parent reinjection formats only
results, not raw tool history. Original declarations are kept one-for-one.
"""
from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace

import pytest

from mercury_constants import (
    get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
)
from tools import async_delegation as ad
from tools import omp_delegation as omp
from tools.delegate_tool import delegate_task
from tools.process_registry import format_process_notification, process_registry


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    """Real dispatch/fanout/finalization; only external RPC is deterministic."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key
    from tools.terminal_tool import set_approval_callback

    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    parent = SimpleNamespace(
        _delegate_depth=0, session_id="parent-live-tip", cwd=str(tmp_path),
        platform="cli", _interrupt_requested=False,
    )
    calls = []
    gate = threading.Event()
    home = tmp_path / "parent-profile"
    token = set_hermes_home_override(home)
    approval_token = set_current_session_key("parent-pre-compression")
    session_tokens = set_session_vars(
        source="tui", session_key="parent-pre-compression", ui_session_id="parent-tab",
    )
    callback = lambda *args, **kwargs: True
    set_approval_callback(callback)

    def rpc(**kwargs):
        assert gate.wait(timeout=5), "test did not release its private RPC gate"
        calls.append({
            "thread": threading.current_thread(), "home": get_hermes_home(),
            "callback": kwargs["approval_callback"], "prompt": kwargs["prompt"],
        })
        return {
            "status": "completed", "summary": "completed " + kwargs["prompt"],
            "exit_reason": "completed", "truncated": False,
            "model": kwargs["model"], "duration_seconds": 0.01,
            "turn_frames": [{"feed": "tool", "text": "PRIVATE_TOOL_ARGUMENT_AND_RESULT"}],
        }

    monkeypatch.setattr(omp, "_omp_delegate_env", lambda *args: ({
        "OMP_MODEL": "fixture/model", "MERCURY_APPROVAL_SOCKET": "/unused",
    }, None))
    monkeypatch.setattr(omp, "_resolve_omp_binary", lambda: "/fixture/omp")
    monkeypatch.setattr(omp, "_render_omp_config_once", lambda *args: None)
    monkeypatch.setattr(omp, "_delegate_batch_base_env", lambda: {})
    monkeypatch.setattr(omp, "_delegate_thinking_level", lambda: "default")
    monkeypatch.setattr(omp, "_delegate_fallback_thinking_level", lambda: "default")
    monkeypatch.setattr("tools.omp_rpc_transport.run_omp_task_rpc", rpc)

    def start(count=1, hidden=None):
        if count == 1 and hidden is None:
            raw = delegate_task(goal="Inspect the selected source contract", parent_agent=parent)
        else:
            tasks = [{
                "goal": f"Inspect selected source module {i} and report its contract",
                "name": f"Module{i}", **(hidden or {}),
            } for i in range(count)]
            raw = delegate_task(tasks=tasks, parent_agent=parent)
        accepted = json.loads(raw)
        assert accepted["status"] == "dispatched", accepted
        assert process_registry.completion_queue.empty()
        gate.set()
        event = process_registry.completion_queue.get(timeout=5)
        assert event["delegation_id"] == accepted["delegation_id"]
        return accepted, event

    yield SimpleNamespace(start=start, calls=calls, parent=parent, home=home, callback=callback)
    gate.set()
    # Wait for the finalizer, then stop every executor/monitor owned by this test.
    executor = ad._executor
    if executor is not None:
        executor.shutdown(wait=True)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    set_approval_callback(None)
    clear_session_vars(session_tokens)
    reset_current_session_key(approval_token)
    reset_hermes_home_override(token)


def _assert_once(accepted, event, count):
    assert event["type"] == "async_delegation"
    assert event["is_batch"] is True
    assert len(event["results"]) == count
    assert sorted(r["task_index"] for r in event["results"]) == list(range(count))
    assert all(r["status"] == "completed" and r["transport"] == "rpc" for r in event["results"])
    # Late duplicate finalization must not create a second completion.
    ad._finalize_batch(accepted["delegation_id"], {"results": event["results"]}, "completed")
    assert process_registry.completion_queue.empty()
    claim = ad.claim_event_delivery(event, "parent-test")
    assert claim
    assert ad.claim_event_delivery(event, "unrelated-consumer") is None
    ad.complete_event_delivery(event, claim)
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


class TestSingleTask:
    def test_fires_once(self, dispatch):
        accepted, event = dispatch.start()
        _assert_once(accepted, event, 1)
        assert len(dispatch.calls) == 1
        text = format_process_notification(event)
        assert text.count("--- ✓ TASK 1/1") == 1
        assert "completed Inspect the selected source contract" in text

    def test_fires_on_parent_thread(self, dispatch):
        """Old callback threading retired; detached execution preserves authority."""
        caller = threading.current_thread()
        _, event = dispatch.start()
        assert event["parent_session_id"] == dispatch.parent.session_id
        assert len(dispatch.calls) == 1
        assert dispatch.calls[0]["thread"] is not caller
        assert dispatch.calls[0]["home"] == dispatch.home
        assert dispatch.calls[0]["callback"] is dispatch.callback

    def test_payload_includes_parent_session_id(self, dispatch):
        _, event = dispatch.start()
        assert event["parent_session_id"] == "parent-live-tip"
        assert event["session_key"] == "parent-live-tip"
        assert event["origin_ui_session_id"] == "parent-tab"
        assert dispatch.parent.session_id == "parent-live-tip"


class TestBatchMode:
    def test_fires_per_child(self, dispatch):
        accepted, event = dispatch.start(3)
        _assert_once(accepted, event, 3)
        assert len(dispatch.calls) == 3
        assert {r["name"] for r in event["results"]} == {"Module0", "Module1", "Module2"}
        text = format_process_notification(event)
        for i in range(3):
            assert text.count(f"--- ✓ TASK {i + 1}/3") == 1
            assert f"completed Inspect selected source module {i}" in text

    def test_all_fires_on_parent_thread(self, dispatch):
        """Old caller-thread hooks retired; every fanout worker inherits context."""
        caller = threading.current_thread()
        _, event = dispatch.start(3)
        assert event["parent_session_id"] == dispatch.parent.session_id
        assert len(dispatch.calls) == 3
        assert all(c["thread"] is not caller for c in dispatch.calls)
        assert all(c["home"] == dispatch.home for c in dispatch.calls)
        assert all(c["callback"] is dispatch.callback for c in dispatch.calls)


class TestPayloadShape:
    def test_includes_redacted_tool_call_history(self, dispatch):
        """Metadata-only hook retired; parent reinjection excludes raw tool history."""
        _, event = dispatch.start()
        assert event["results"][0]["turn_frames"][0]["text"] == "PRIVATE_TOOL_ARGUMENT_AND_RESULT"
        before = json.dumps(event, sort_keys=True)
        text = format_process_notification(event)
        assert "completed Inspect the selected source contract" in text
        assert "PRIVATE_TOOL_ARGUMENT_AND_RESULT" not in text
        assert "turn_frames" not in text
        assert json.dumps(event, sort_keys=True) == before

    def test_result_does_not_leak_child_role_field(self, dispatch):
        """Task is the only role; hidden model fields cannot invent child roles."""
        _, event = dispatch.start(hidden={"_child_role": "PRIVATE_INVENTED_ROLE"})
        assert event["role"] == "task"
        raw = json.dumps(event)
        assert "_child_role" not in raw
        assert "PRIVATE_INVENTED_ROLE" not in raw
        assert all("child_role" not in r for r in event["results"])
        assert "PRIVATE_INVENTED_ROLE" not in dispatch.calls[0]["prompt"]
        assert "Role: task" in format_process_notification(event)
