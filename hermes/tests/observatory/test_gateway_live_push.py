"""Live-progress push for observatory gateway turns (seq + datagrams + dedupe).

Covers the live-push contract in ``observatory.gateway_session``: per-turn
seq from 0 on every captured event, best-effort ``SOCK_DGRAM`` datagrams to
``$MERCURY_HOME/observatory/gateway-progress.sock``, seq riding the final
events, node_id threading (default gw), reply-echo thinking drop, and
_thinking/tool_progress vs thinking-callback double-capture dedupe.
"""

import json
import socket

import pytest

from observatory import gateway_session as gs


class FakeAgent:
    def __init__(self, session_id="gateway"):
        self.session_id = session_id

    def run_conversation(self, text):
        return {"final_response": f"reply:{text}"}

    def close(self):
        pass


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


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    return tmp_path


def _factory():
    made: list[FakeAgent] = []

    def build(session_id):
        inst = FakeAgent(session_id)
        made.append(inst)
        return inst

    build.made = made  # type: ignore[attr-defined]
    return build


def _bind(home):
    path = home / "observatory" / "gateway-progress.sock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.settimeout(2.0)
    return sock


def _recv_all(sock, n):
    return [json.loads(sock.recv(65535).decode("utf-8")) for _ in range(n)]


def test_seq_starts_at_zero_in_capture_order(home):
    def turn(agent, text):
        agent.tool_progress_callback("tool.started", "bash", args={"cmd": "ls"})
        agent.thinking_callback("pondering")
        agent.tool_progress_callback("tool.started", "read", args=None)
        return {"final_response": "done"}

    reply, events = gs.run_gateway_prompt_with_events(
        "hi", session_id="s-seq", agent_factory=_factory(), turn=turn
    )
    assert reply == "done"
    assert events == [
        {"type": "tool_call", "tool": "bash", "args": {"cmd": "ls"}, "seq": 0},
        {"type": "thinking", "text": "pondering", "seq": 1},
        {"type": "tool_call", "tool": "read", "args": {}, "seq": 2},
    ]


def test_live_datagrams_carry_node_seq_event(home):
    sock = _bind(home)
    try:

        def turn(agent, text):
            agent.tool_progress_callback("tool.started", "bash", args={"cmd": "ls"})
            agent.thinking_callback("pondering")
            return {"final_response": "done"}

        reply, events = gs.run_gateway_prompt_with_events(
            "hi", session_id="s-live", agent_factory=_factory(), turn=turn
        )
        msgs = _recv_all(sock, 2)
    finally:
        sock.close()
    assert reply == "done"
    assert msgs == [
        {"node_id": "gw", "seq": 0,
         "event": {"type": "tool_call", "tool": "bash", "args": {"cmd": "ls"}}},
        {"node_id": "gw", "seq": 1,
         "event": {"type": "thinking", "text": "pondering"}},
    ]
    assert [e["seq"] for e in events] == [0, 1]


def test_custom_node_id_threads_to_datagrams(home):
    sock = _bind(home)
    try:

        def turn(agent, text):
            agent.tool_progress_callback("tool.started", "bash", args={})
            return {"final_response": "done"}

        _, events = gs.run_gateway_prompt_with_events(
            "hi", session_id="s-node", node_id="orch-x",
            agent_factory=_factory(), turn=turn,
        )
        msgs = _recv_all(sock, 1)
    finally:
        sock.close()
    assert msgs[0]["node_id"] == "orch-x"
    assert events[0]["seq"] == 0


def test_reply_only_wrapper_threads_node_id(home):
    sock = _bind(home)
    try:

        def turn(agent, text):
            agent.thinking_callback("hmm")
            return {"final_response": "done"}

        assert gs.run_gateway_prompt(
            "hi", session_id="s-wrap", node_id="gw2",
            agent_factory=_factory(), turn=turn,
        ) == "done"
        msgs = _recv_all(sock, 1)
    finally:
        sock.close()
    assert msgs[0] == {
        "node_id": "gw2", "seq": 0,
        "event": {"type": "thinking", "text": "hmm"},
    }


def test_reply_echo_thinking_dropped_from_final_only(home):
    sock = _bind(home)
    try:

        def turn(agent, text):
            agent.thinking_callback("  final   answer ")
            agent.thinking_callback("other thought")
            return {"final_response": "final answer"}

        reply, events = gs.run_gateway_prompt_with_events(
            "hi", session_id="s-echo", agent_factory=_factory(), turn=turn
        )
        msgs = _recv_all(sock, 2)
    finally:
        sock.close()
    assert reply == "final answer"
    # Live datagrams already went out (both captures); final drops the echo.
    assert [m["seq"] for m in msgs] == [0, 1]
    assert events == [{"type": "thinking", "text": "other thought", "seq": 1}]


def test_thinking_double_capture_keeps_first(home):
    sock = _bind(home)
    try:

        def turn(agent, text):
            agent.tool_progress_callback("_thinking", "same thought")
            agent.thinking_callback("same thought")
            agent.reasoning_callback("  same   thought ")
            return {"final_response": "unrelated reply"}

        _, events = gs.run_gateway_prompt_with_events(
            "hi", session_id="s-dedupe", agent_factory=_factory(), turn=turn
        )
        msgs = _recv_all(sock, 1)
    finally:
        sock.close()
    assert events == [{"type": "thinking", "text": "same thought", "seq": 0}]
    assert msgs[0]["seq"] == 0


def test_no_listener_drops_silently(home):
    # No socket bound under the tmp MERCURY_HOME: turn still succeeds.
    def turn(agent, text):
        agent.tool_progress_callback("tool.started", "bash", args={})
        return {"final_response": "done"}

    reply, events = gs.run_gateway_prompt_with_events(
        "hi", session_id="s-nolisten", agent_factory=_factory(), turn=turn
    )
    assert reply == "done"
    assert events == [
        {"type": "tool_call", "tool": "bash", "args": {}, "seq": 0}
    ]


def test_command_dispatch_short_circuits_without_push(home):
    sock = _bind(home)
    sock.settimeout(0.2)
    try:
        reply, events = gs.run_gateway_prompt_with_events(
            "/version", kind="command", session_id="s-cmd",
            agent_factory=_factory(), slash_dispatch=lambda t: "pong",
        )
        assert (reply, events) == ("pong", [])
        with pytest.raises(socket.timeout):
            sock.recv(65535)
    finally:
        sock.close()
