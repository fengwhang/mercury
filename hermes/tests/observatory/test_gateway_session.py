"""Unit tests for observatory/gateway_session.py.

The gateway agent's headless turn, with fakes standing in for the engine:
agent construction, reply extraction, validation, failure semantics. No
gateway process, no model, no network.
"""

import pytest

from observatory import gateway_session as gs


class FakeAgent:
    def __init__(self, session_id="gateway"):
        self.session_id = session_id
        self.turns: list[str] = []
        self.closed = False

    def run_conversation(self, text):
        self.turns.append(text)
        return {"final_response": f"reply:{text}"}

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_registry():
    gs._session_agents.clear()
    gs._session_locks.clear()
    yield
    for agent in list(gs._session_agents.values()):
        try:
            agent.close()
        except Exception:
            pass
    gs._session_agents.clear()
    gs._session_locks.clear()


def _factory(agent=None):
    made: list[FakeAgent] = []

    def build(session_id):
        inst = agent if agent is not None and not made else FakeAgent(session_id)
        made.append(inst)
        return inst

    build.made = made  # type: ignore[attr-defined]
    return build


@pytest.fixture()
def builds(monkeypatch):
    """Stand-in for the real engine builder (no config, no keys, no DB)."""
    made: list[FakeAgent] = []

    def fake_default(session_id):
        agent = FakeAgent(session_id)
        made.append(agent)
        return agent

    monkeypatch.setattr(gs, "_default_agent", fake_default)
    return made


def test_prompt_returns_final_response():
    factory = _factory()
    assert gs.run_gateway_prompt("hello?", agent_factory=factory) == "reply:hello?"
    assert factory.made[0].turns == ["hello?"]


def test_consecutive_prompts_share_one_transcript(builds):
    # default (cached) path shares the session agent across turns
    gs.run_gateway_prompt("first")
    gs.run_gateway_prompt("second")
    assert len(builds) == 1
    assert builds[0].turns == ["first", "second"]

def test_explicit_factory_builds_per_call():
    factory = _factory()
    gs.run_gateway_prompt("a", agent_factory=factory)
    gs.run_gateway_prompt("b", agent_factory=factory)
    assert [a.turns for a in factory.made] == [["a"], ["b"]]


def test_empty_text_rejected():
    with pytest.raises(ValueError):
        gs.run_gateway_prompt("   ", agent_factory=_factory())


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        gs.run_gateway_prompt("hi", kind="teleport", agent_factory=_factory())


def test_empty_session_id_rejected():
    with pytest.raises(ValueError):
        gs.run_gateway_prompt("hi", session_id="", agent_factory=_factory())


def test_turn_error_propagates_and_evicts_cached_agent(builds):
    def boom(agent, text):
        raise RuntimeError("engine down")

    with pytest.raises(RuntimeError, match="engine down"):
        gs.run_gateway_prompt("hi", turn=boom)
    assert gs.GATEWAY_SESSION_ID not in gs._session_agents

    # next prompt rebuilds and succeeds
    assert gs.run_gateway_prompt("retry") == "reply:retry"
    assert len(builds) == 2


def test_non_dict_result_rejected():
    factory = _factory()
    with pytest.raises(RuntimeError, match="not a result dict"):
        gs.run_gateway_prompt("hi", agent_factory=factory, turn=lambda a, t: ["nope"])


def test_missing_reply_extracts_empty_string():
    factory = _factory()
    assert gs.run_gateway_prompt(
        "hi", agent_factory=factory, turn=lambda a, t: {}
    ) == ""

def test_drop_cached_agent_forces_rebuild(builds):
    gs.run_gateway_prompt("first")
    first = gs._session_agents[gs.GATEWAY_SESSION_ID]
    gs.drop_cached_agent()
    assert first.closed
    assert gs.GATEWAY_SESSION_ID not in gs._session_agents
    gs.run_gateway_prompt("second")
    assert gs._session_agents[gs.GATEWAY_SESSION_ID] is not first
    assert len(builds) == 2
