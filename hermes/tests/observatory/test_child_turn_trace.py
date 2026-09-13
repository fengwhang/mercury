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


class FakeOmpRpc:
    """rpc.run_task double: fails loudly unless the feed subscribed first."""

    def __init__(self, feed):
        self._feed = feed
        self.prompts: list[str] = []
        self.observed_started: bool | None = None

    def run_task(self, prompt, timeout=None):
        self.prompts.append(prompt)
        self.observed_started = bool(self._feed.started)
        if not self._feed.started:
            raise AssertionError("run_task ran with no feed subscription")
        return {"status": "completed", "summary": f"omp-reply:{prompt}"}


class FakeFeed:
    """Canned OmpFeed surface: single-shot SELF tool/thinking/message."""

    def __init__(self, rpc=None):
        self.started = False
        self.stopped = False
        self._consumed = False

    async def start(self, *, level="events"):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def events(self):
        from observatory.omp_feed import MessageEvent, ThoughtEvent, ToolEvent

        if self._consumed:
            return
        self._consumed = True
        yield ToolEvent(subagent_id="", tool="terminal", args='{"command": "ls /tmp"}')
        yield ThoughtEvent(subagent_id="", text="checking the directory listing")
        yield MessageEvent(subagent_id="", role="assistant", text="halfway there")


def _omp_daemon(monkeypatch, tmp_path) -> sm.SidecarDaemon:
    d = _daemon(monkeypatch, tmp_path)
    d.omp_feeds = {}
    d._loops = []
    d._child_busy = set()
    d._omp_feed_held = set()
    return d


@pytest.mark.asyncio()
async def test_omp_child_prompt_subscribes_before_turn_and_streams_self(
    monkeypatch, tmp_path,
) -> None:
    """The omp turn subscribes the feed BEFORE run_task and the SELF
    tool/thinking/message stream into the CHILD room before the summary."""
    d = _omp_daemon(monkeypatch, tmp_path)
    feed = FakeFeed()
    rpc = FakeOmpRpc(feed)
    handle = SimpleNamespace(engine="omp", rpc=rpc)
    monkeypatch.setattr(d, "_child_handle", lambda node_id: handle)
    monkeypatch.setattr(d, "_ensure_omp_feed", lambda node_id, handle: feed)

    ok = await d._run_omp_child_prompt("child-1", "do the thing")
    assert ok is True
    assert rpc.observed_started is True, "run_task ran with no feed subscription"

    renderer = d.renderer
    assert renderer.tool_calls == [
        ("child-1", "terminal", '{"command": "ls /tmp"}'),
    ], f"SELF tool call missing: {renderer.tool_calls!r}"
    assert renderer.thinkings == [
        ("child-1", "checking the directory listing"),
    ], f"SELF thinking missing: {renderer.thinkings!r}"
    assert [m[1] for m in renderer.messages] == [
        "halfway there",
        "omp-reply:do the thing",
    ], f"SELF message must precede the summary: {renderer.messages!r}"


@pytest.mark.asyncio()
async def test_omp_child_prompt_quiet_keeps_trace_drops_reply(
    monkeypatch, tmp_path,
) -> None:
    """quiet gates ONLY the final reply — the SELF trace still streams."""
    d = _omp_daemon(monkeypatch, tmp_path)
    feed = FakeFeed()
    rpc = FakeOmpRpc(feed)
    handle = SimpleNamespace(engine="omp", rpc=rpc)
    monkeypatch.setattr(d, "_child_handle", lambda node_id: handle)
    monkeypatch.setattr(d, "_ensure_omp_feed", lambda node_id, handle: feed)

    ok = await d._run_omp_child_prompt("child-1", "do it quietly", quiet=True)
    assert ok is True
    assert rpc.observed_started is True, "run_task ran with no feed subscription"

    renderer = d.renderer
    assert renderer.tool_calls, "quiet must not suppress the SELF tool trace"
    assert renderer.thinkings, "quiet must not suppress the SELF thinking trace"
    assert [m[1] for m in renderer.messages] == ["halfway there"], (
        f"quiet must suppress ONLY the final reply: {renderer.messages!r}"
    )


@pytest.mark.asyncio()
async def test_ensure_omp_feed_returns_feed_and_attaches(
    monkeypatch, tmp_path,
) -> None:
    """_ensure_omp_feed hands back the feed (prompt path awaits start
    BEFORE the turn) and attaches exactly one background consumer."""
    import observatory.omp_feed as omp_feed_mod

    d = _omp_daemon(monkeypatch, tmp_path)
    feed = FakeFeed()
    monkeypatch.setattr(omp_feed_mod, "OmpFeed", lambda rpc: feed)
    handle = SimpleNamespace(engine="omp", rpc=object())

    got = d._ensure_omp_feed("child-9", handle)
    assert got is feed
    assert d.omp_feeds["child-9"] is feed
    again = d._ensure_omp_feed("child-9", handle)
    assert again is feed
    assert len([t for t in d._loops if "child-9" in t.get_name()]) == 1
    for _ in range(250):
        if feed.started and d.renderer.tool_calls:
            break
        await asyncio.sleep(0.02)
    assert feed.started, "background feed task must subscribe"
    assert d.renderer.tool_calls, "background feed must stream SELF frames"
    for task in list(d._loops):
        task.cancel()
    for task in list(d._loops):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass



class SpawnFakeOmpRpc(FakeOmpRpc):
    """Spawn double: FakeOmpRpc plus the started-client session surface."""

    def __init__(self, feed, session_file="/tmp/spawn-sess.jsonl"):
        super().__init__(feed)
        self._client = SimpleNamespace(
            get_state=lambda: SimpleNamespace(session_file=session_file),
        )


@pytest.mark.asyncio()
async def test_spawnomp_first_turn_streams_self_trace(monkeypatch, tmp_path):
    """A /spawnomp 0-agent's first OmpPrompt attaches the feed and streams
    the SELF trace into its own room before the summary."""
    import observatory.omp_feed as omp_feed_mod
    from observatory.spawn import OrchestratorRegistry, spawn_orchestrator
    from observatory.state import ObservatoryState

    d = _omp_daemon(monkeypatch, tmp_path)
    d.state = ObservatoryState(tmp_path / "state.db")
    feed = FakeFeed()
    rpc = SpawnFakeOmpRpc(feed)
    monkeypatch.setattr(omp_feed_mod, "OmpFeed", lambda rpc: feed)
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator(
        "tracer", "omp", server_name="mercury.local",
        state=d.state, registry=registry, renderer=None,
        omp_child_factory=lambda: rpc,
    )
    node_id = row["node_id"]
    handle = registry.get(node_id)
    assert handle is not None and getattr(handle, "rpc") is rpc
    monkeypatch.setattr(d, "_child_handle", lambda nid: registry.get(nid))

    ok = await d._run_omp_child_prompt(node_id, "first decree")
    assert ok is True
    assert rpc.observed_started is True, "run_task ran with no feed subscription"
    assert d.omp_feeds.get(node_id) is feed, "first turn must attach the feed"

    renderer = d.renderer
    assert renderer.tool_calls == [(node_id, "terminal", '{"command": "ls /tmp"}')]
    assert renderer.thinkings == [(node_id, "checking the directory listing")]
    assert [m[1] for m in renderer.messages] == [
        "halfway there", "omp-reply:first decree",
    ]
    for task in list(d._loops):
        task.cancel()
    for task in list(d._loops):
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio()
async def test_spawn_hermes_first_turn_captures_trace(monkeypatch, tmp_path):
    """A /spawn hermes 0-agent's first turn captures tool+thinking+reply
    through the gateway collector into its own room."""
    from observatory.spawn import OrchestratorRegistry, spawn_orchestrator
    from observatory.state import ObservatoryState

    d = _omp_daemon(monkeypatch, tmp_path)
    d.state = ObservatoryState(tmp_path / "state.db")
    agent = FakeAgent()
    agent.session_id = "sess-spawn-1"
    registry = OrchestratorRegistry()
    row = await spawn_orchestrator(
        "thinker", "hermes", server_name="mercury.local",
        state=d.state, registry=registry, renderer=None,
        agent_factory=lambda: agent,
    )
    node_id = row["node_id"]
    monkeypatch.setattr(d, "_child_handle", lambda nid: registry.get(nid))

    ok = await d._run_hermes_child_turn(node_id, "first thought")
    assert ok is True

    renderer = d.renderer
    assert renderer.tool_calls == [(node_id, "terminal", '{"command": "ls /tmp"}')]
    assert renderer.thinkings == [(node_id, "checking the directory listing")]
    assert len(renderer.messages) == 1 and renderer.messages[0][0] == node_id
    assert "reply:first thought" in renderer.messages[0][1]