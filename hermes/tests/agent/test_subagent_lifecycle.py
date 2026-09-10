"""Contract tests for the public plugin subagent lifecycle API.

RETIRED (DEAD DEPTH): the hermes-side child-agent engine was removed and
``delegate_task`` routes exclusively through the omp engine, so
``SubagentLifecycleService.launch`` fails loudly with
``SubagentLifecycleError`` instead of forking a second engine. These tests
pin the retired contract: launch refuses (after request validation),
forged/foreign handles stay UNKNOWN, and the agent-turn parent binding
still works.
"""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.subagent_lifecycle import (
    SubagentLaunchRequest,
    SubagentLifecycleError,
    SubagentLifecycleService,
    SubagentState,
    bind_subagent_parent,
    get_active_subagent_parent,
)


@pytest.fixture
def lifecycle():
    parent = SimpleNamespace(session_id="parent-1", enabled_toolsets=["file"])
    return SubagentLifecycleService(lambda: parent)


def _forged_handle():
    from agent.subagent_lifecycle import SubagentHandle

    return SubagentHandle(
        contract_version=1,
        subagent_id="sa-forged",
        parent_session_id="parent-1",
        correlation_id=None,
        created_at=time.time(),
        provider=None,
        model=None,
        role="leaf",
        depth=1,
        capability="forged",
    )


def test_launch_retired_fails_loudly(lifecycle):
    with pytest.raises(SubagentLifecycleError, match="DEAD DEPTH"):
        lifecycle.launch(SubagentLaunchRequest(goal="x"))


def test_launch_validates_request_before_retiring(lifecycle):
    # Field-level validation still runs first: a malformed request keeps
    # its validation error, not the retirement message.
    with pytest.raises(SubagentLifecycleError, match="goal must be"):
        lifecycle.launch(SubagentLaunchRequest(goal="   "))


def test_forged_and_foreign_handles_are_unknown(lifecycle):
    forged = _forged_handle()
    assert lifecycle.status(forged).state is SubagentState.UNKNOWN
    assert lifecycle.result(forged).error_classification == "UNKNOWN_HANDLE"
    assert lifecycle.cancel(forged, reason="test").unknown_handle
    other_parent = SimpleNamespace(session_id="different-parent")
    other_service = SubagentLifecycleService(lambda: other_parent)
    assert other_service.status(forged).state is SubagentState.UNKNOWN


def test_launch_without_parent_still_reports_no_session():
    service = SubagentLifecycleService(lambda: None)
    with pytest.raises(SubagentLifecycleError, match="No active Mercury parent"):
        service.launch(SubagentLaunchRequest(goal="x"))


def test_public_lifecycle_runs_host_aggregation(monkeypatch):
    memory = Mock()
    parent = SimpleNamespace(
        session_id="parent-aggregate",
        enabled_toolsets=["file"],
        _memory_manager=memory,
        _current_turn_id="turn-1",
        session_estimated_cost_usd=1.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )

    service = SubagentLifecycleService(lambda: parent)
    # Aggregation rode the hermes-side run path, which no longer exists:
    # launch refuses before any child, hook, or cost mutation happens.
    with pytest.raises(SubagentLifecycleError, match="DEAD DEPTH"):
        service.launch(SubagentLaunchRequest(goal="aggregate me"))
    memory.on_delegation.assert_not_called()


def test_agent_turn_binds_and_clears_lifecycle_parent(monkeypatch):
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    observed = []

    def run_conversation(parent, *_args, **_kwargs):
        observed.append(get_active_subagent_parent())
        return {"final_response": "ok"}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", run_conversation)

    assert agent.run_conversation("hello") == {"final_response": "ok"}
    assert observed == [agent]
    assert get_active_subagent_parent() is None
