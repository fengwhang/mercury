"""Room manager tests: naming, frame formatting, routing, publish."""

from __future__ import annotations

import pytest

from observatory import rooms
from observatory.rooms import (
    RoomManager,
    child_channel,
    format_frame,
    format_lifecycle,
    gateway_channel,
    spawn_channel,
)


@pytest.fixture(autouse=True)
def _clean_queue():
    """The producer queue is process-global — flush leftovers both ends."""
    from observatory import rooms as _rooms_mod

    def _flush() -> None:
        while True:
            try:
                _rooms_mod._QUEUE.get_nowait()
            except Exception:
                break

    _flush()
    yield
    _flush()


def test_channel_naming() -> None:
    assert gateway_channel("mercury") == "#mercury_gateway"
    assert spawn_channel("Ace") == "#ace"
    assert child_channel("parent", "child") == "#parent-child"
    assert child_channel("My Agent", "Kid 1") == "#my-agent-kid-1"


def test_format_frame_shapes() -> None:
    assert format_frame({"feed": "message", "text": "hello"}) == "hello"
    assert format_frame({"feed": "message", "text": "  "}) is None
    tool = format_frame({
        "feed": "tool",
        "tool": "bash",
        "args": "ls",
        "subagent_id": "",
    })
    assert tool is not None and "bash" in tool and "ls" in tool
    thought = format_frame({"feed": "thought", "text": "hmm", "subagent_id": "grand"})
    assert thought is not None and "[grand]" in thought and "hmm" in thought
    assert format_frame({"feed": "bogus"}) is None
    assert format_frame("nope") is None


def test_format_frame_traces_are_plaintext() -> None:
    """Trace lines carry glyphs and no markdown: the fork renders them
    plaintext (markdown bypass), so backticks/escapes must not ship."""
    tool = format_frame({
        "feed": "tool", "tool": "read",
        "args": '{"path": "/my_dir/f_$x"}', "subagent_id": "",
    })
    assert tool is not None and tool.startswith("🔧 ")
    assert "`" not in tool and "\\$" not in tool
    assert "/my_dir/f_$x" in tool
    out = format_frame({
        "feed": "message", "role": "tool",
        "text": "[f#1A2B] 1:def a_b($x):", "subagent_id": "",
    })
    assert out is not None and out.startswith("🔧 ")
    assert "[f#1A2B]" in out
    reply = format_frame({
        "feed": "message", "role": "assistant",
        "text": "hello **you**", "subagent_id": "",
    })
    assert reply == "hello **you**"


def test_reply_frames_keep_complete_markdown_and_trace_frames_keep_whitespace():
    reply = 'Before\n```bash\n  echo "$A-$B" ' + "*.txt " * 200 + '\n\n```\nAfter $x^2$.'
    assert format_frame({"feed": "message", "role": "assistant", "text": reply}) == reply
    trace = '\n\t**literal** "$A-$B" *.txt  \n'
    assert format_frame({"feed": "message", "role": "tool", "text": trace}) == "🔧 " + trace
    assert format_frame({"feed": "thought", "text": trace}) == "💭 " + trace


def test_format_lifecycle() -> None:
    assert "started" in format_lifecycle("start", name="kid")
    assert "done" in format_lifecycle(
        "stop", name="kid", summary="ok"
    ).lower() or "finished" in format_lifecycle("stop", name="kid", summary="ok")


class FakeState:
    def __init__(self, rows):
        self._rows = rows

    def get_live(self):
        return list(self._rows)

    def get(self, node_id):
        for row in self._rows:
            if row["node_id"] == node_id:
                return row
        raise KeyError(node_id)

    def get_meta(self, key):
        from observatory.state import StateError

        raise StateError(f"unknown metadata: {key}")


class FakeBot:
    def __init__(self):
        self.joined: list[str] = []
        self.said: list[tuple[str, str]] = []
        self.kinds: list[str] = []
        self.destroyed: list[str] = []

    async def join_channel(self, channel: str) -> bool:
        self.joined.append(channel)
        return True

    async def part_channel(self, channel: str) -> bool:
        return True

    async def say(self, channel: str, text: str, *, kind: str = "status") -> bool:
        self.said.append((channel, text))
        self.kinds.append(kind)
        return True

    async def destroy_channel(self, channel: str) -> bool:
        self.destroyed.append(channel)
        return True


def _rows():
    return [
        {
            "node_id": "gw",
            "engine": "hermes",
            "depth": 0,
            "parent_node_id": None,
            "room_id": "#mercury_gateway",
            "extra": {"kind": "gateway"},
        },
        {
            "node_id": "orch-1",
            "engine": "hermes",
            "depth": 0,
            "parent_node_id": None,
            "room_id": "#ace",
            "extra": {},
        },
        {
            "node_id": "orch-2",
            "engine": "omp",
            "depth": 0,
            "parent_node_id": None,
            "room_id": "#king",
            "extra": {},
        },
        {
            "node_id": "deleg-1",
            "engine": "omp",
            "depth": 1,
            "parent_node_id": "gw",
            "room_id": "#mercury_gateway-cow",
            "extra": {},
        },
    ]


@pytest.mark.asyncio
async def test_frame_kind_is_carried_separately_from_visible_text():
    bot = FakeBot()
    mgr = RoomManager(FakeState(_rows()), bot)
    for feed in [
        {"feed": "tool", "tool": "bash", "args": 'echo "$A-$B" *.txt'},
        {"feed": "message", "role": "tool", "text": "**literal** $x$"},
        {"feed": "thought", "text": "**literal** $x$"},
        {"feed": "message", "role": "assistant", "text": "✅ **Done** $x^2$"},
    ]:
        assert await mgr.publish_frame("#ace", feed)
    assert bot.kinds == ["tool_input", "tool_output", "thinking", "assistant_reply"]
    assert bot.said[-1][1] == "✅ **Done** $x^2$"


def test_inbound_route() -> None:
    mgr = RoomManager(FakeState(_rows()), FakeBot())
    assert mgr.inbound_route("#mercury_gateway")[0] == "gateway"
    assert mgr.inbound_route("#ace")[0] == "spawn-hermes"
    assert mgr.inbound_route("#king")[0] == "spawn-omp"
    assert mgr.inbound_route("#mercury_gateway-cow")[0] == "child"
    assert mgr.inbound_route("#unknown")[0] == "passthrough"


@pytest.mark.parametrize("managed_only", ["true", "false"])
def test_inbound_route_excludes_durable_expiries_only(tmp_path, monkeypatch, managed_only):
    import json
    from observatory import provision
    from observatory.spawn import begin_exit, finish_exit
    from observatory.state import CLOSED_ROOMS_META_KEY, MANAGED_ROOMS_META_KEY

    monkeypatch.setattr(provision, "live_server_name", lambda home=None: "test")
    with _real_state(tmp_path) as state:
        state.add_node("old", engine="hermes", name="kid", slug="kid", mxid="kid",
                       session_ref="old")
        state.set_room_id("old", "#test-root-kid")
        mgr = RoomManager(state, FakeBot())
        record = begin_exit(state, "old")
        assert mgr.inbound_route("#TEST-ROOT-KID") == ("expired", None)
        finish_exit(state, record)
        state.set_meta(MANAGED_ROOMS_META_KEY, managed_only)
        state.set_meta(CLOSED_ROOMS_META_KEY, json.dumps(
            ["#test-root-kid", "#test_gateway"]))
        assert mgr.inbound_route("#test-root-kid") == ("expired", None)
        assert mgr.inbound_route("#unrelated") == ("passthrough", None)
        assert mgr.inbound_route("#TEST_GATEWAY") == ("passthrough", None)
        state.add_node("new", engine="hermes", name="kid", slug="kid", mxid="kid",
                       session_ref="new")
        state.set_room_id("new", "#test-root-kid")
        route, row = mgr.inbound_route("#TEST-ROOT-KID")
        assert route == "spawn-hermes"
        assert row["node_id"] == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_text", ["late queued text", "!spawn unexpected", "!approve"])
async def test_adapter_drops_expired_queued_group_message(tmp_path, monkeypatch, late_text):
    import asyncio
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from observatory import provision
    from observatory.room_reaper import closed_rooms
    from observatory.spawn import begin_exit, finish_exit
    from observatory.state import MANAGED_ROOMS_META_KEY, StateError
    from plugins.platforms.mirc.adapter import MIRCAdapter

    monkeypatch.setattr(provision, "live_server_name", lambda home=None: "test")
    history = tmp_path / "test-root-kid.log"
    history.write_text("completed child transcript\n")
    state = _real_state(tmp_path)
    state.add_node("root", engine="hermes", name="root", slug="root", mxid="root",
                   session_ref="root", extra={"kind": "spawn"})
    state.set_room_id("root", "#test-root")
    state.add_node("old", engine="hermes", name="kid", slug="kid", mxid="kid",
                   session_ref="old", parent_node_id="root", extra={"kind": "delegate"})
    state.set_room_id("old", "#test-root-kid")
    steered = []
    rooms.register_child_steer("old", steered.append)
    record = begin_exit(state, "old")
    finish_exit(state, record)
    state.set_meta(MANAGED_ROOMS_META_KEY, "true")
    state.close()
    # Re-open both the manager and state: expiry is durable, not a process cache.
    state = _real_state(tmp_path)
    monkeypatch.setattr(rooms, "_current_manager", RoomManager(state, FakeBot()))
    adapter = MIRCAdapter(PlatformConfig(enabled=True, extra={
        "server": "127.0.0.1", "port": 1, "nickname": "testbot",
        "channel": "#test_gateway", "use_tls": False,
    }))
    monkeypatch.setattr(adapter, "send", AsyncMock())
    events = []

    async def gateway(event):
        events.append((event.source.chat_id, event.text))

    adapter.set_message_handler(gateway)
    try:
        with pytest.raises(StateError):
            state.get("old")
        assert "#test-root-kid" in closed_rooms(state)
        await adapter._dispatch_message(late_text, "#TEST-ROOT-KID", "group", "owner", "owner")
        await asyncio.gather(*adapter._background_tasks)
        assert events == []
        assert steered == []
        adapter.send.assert_not_awaited()
        assert rooms.route_channel("#test-root-kid") == ("expired", None)

        for channel in ("#unrelated", "#test_gateway"):
            await adapter._dispatch_message("normal input", channel, "group", "owner", "owner")
            await asyncio.gather(*adapter._background_tasks)
        assert events == [("#unrelated", "normal input"), ("#test_gateway", "normal input")]

        state.add_node("new", engine="hermes", name="kid", slug="kid", mxid="kid",
                       session_ref="new", parent_node_id="root", extra={"kind": "delegate"})
        state.set_room_id("new", "#test-root-kid")
        rooms.register_child_steer("new", steered.append)
        await adapter._dispatch_message(
            "successor input", "#test-root-kid", "group", "owner", "owner")
        assert steered == ["successor input"]
        assert len(events) == 2
        assert history.read_text() == "completed child transcript\n"
        assert state.get("root")["status"] == "live"
    finally:
        await adapter.cancel_background_tasks()
        rooms.drop_child_steer("old")
        rooms.drop_child_steer("new")
        state.close()


def test_node_for_channel_case_insensitive() -> None:
    mgr = RoomManager(FakeState(_rows()), FakeBot())
    assert mgr.node_for_channel("#ACE")["node_id"] == "orch-1"


@pytest.mark.asyncio
async def test_publish_frame_and_lifecycle() -> None:
    bot = FakeBot()
    mgr = RoomManager(FakeState(_rows()), bot)
    assert await mgr.publish_frame("#ace", {"feed": "tool", "tool": "bash"})
    assert await mgr.publish_lifecycle("#ace", "start", name="kid")
    assert await mgr.ensure_room("#new", greet="hi")
    assert await mgr.destroy_room("#ace")
    assert bot.joined == ["#new"]
    assert bot.destroyed == ["#ace"]
    assert len(bot.said) == 3


def test_global_sink() -> None:
    assert rooms.get_bot_sink() is None
    bot = FakeBot()
    rooms.set_bot_sink(bot)
    try:
        assert rooms.get_bot_sink() is bot
        mgr = RoomManager(FakeState([]))
        assert mgr.bot is bot
    finally:
        rooms.set_bot_sink(None)


def _real_state(tmp_path):
    from observatory.state import ObservatoryState

    return ObservatoryState(tmp_path / "state.db")


@pytest.mark.asyncio
async def test_direct_ensure_creates_child_room(tmp_path) -> None:
    bot = FakeBot()
    mgr = RoomManager(_real_state(tmp_path), bot)
    mgr.state.add_node("gateway", engine="hermes", name="gateway", slug="gateway", mxid="gateway", session_ref="gateway")
    mgr.state.set_room_id("gateway", "#gateway")
    channel = await mgr._ensure_child_room_for(
        "deleg-1", {"name": "cow", "parent_name": "gateway", "engine": "omp"})
    assert channel == "#gateway-cow"
    await mgr.publish_lifecycle(channel, "start", name="cow")
    await mgr.publish_frame(
        channel, {"feed": "tool", "tool": "bash", "args": "ls", "subagent_id": ""})
    assert bot.joined == ["#gateway-cow"]
    texts = [t for _, t in bot.said]
    assert any("started" in t for t in texts)
    assert any("bash" in t for t in texts)
    row = mgr.node_for_channel("#gateway-cow")
    assert row is not None and row["node_id"] == "deleg-1"
    assert mgr.inbound_route("#gateway-cow")[0] == "child"


@pytest.mark.asyncio
async def test_handle_child_message_steers(tmp_path) -> None:
    bot = FakeBot()
    mgr = RoomManager(_real_state(tmp_path), bot)
    mgr.state.add_node("gateway", engine="hermes", name="gateway", slug="gateway", mxid="gateway", session_ref="gateway")
    mgr.state.set_room_id("gateway", "#gateway")
    await mgr._ensure_child_room_for(
        "deleg-9", {"name": "kid", "parent_name": "gateway", "engine": "hermes"})
    assert "finished" in await mgr.handle_child_message(
        "#gateway-kid", "op", "stop that"
    )
    seen: list[str] = []
    rooms.register_child_steer("deleg-9", seen.append)
    try:
        assert (
            await mgr.handle_child_message("#gateway-kid", "op", "stop that")
            == "steered (as op)."
        )
        assert seen == ["stop that"]
    finally:
        rooms.drop_child_steer("deleg-9")
    await mgr.publish_lifecycle("#gateway-kid", "stop", name="kid",
                                  summary="done")
    await mgr._retire_child_room("deleg-9", summary="done")
    assert "finished" in (
        await mgr.handle_child_message("#gateway-kid", "op", "again")
    ).lower() or "history" in await mgr.handle_child_message(
        "#gateway-kid", "op", "again"
    )


@pytest.fixture
def child_adapter(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gateway.config import PlatformConfig
    from gateway.platforms.base import SendResult
    from mercury_cli import profiles
    from mercury_constants import get_hermes_home_override
    from plugins.platforms.mirc.adapter import MIRCAdapter

    state = _real_state(tmp_path)
    state.add_node(
        "parent", engine="hermes", name="parent", slug="parent", mxid="parent",
        session_ref="parent-session", extra={"kind": "spawn", "profile": "research"},
    )
    state.set_room_id("parent", "#parent")
    state.add_node(
        "child", engine="hermes", name="child", slug="child", mxid="child",
        session_ref="child-session", parent_node_id="parent",
        extra={"kind": "delegate", "profile": "research"},
    )
    state.set_room_id("child", "#parent-child")
    manager = RoomManager(state, FakeBot())
    monkeypatch.setattr(rooms, "_current_manager", manager)
    profile_home = tmp_path / "profiles" / "research"
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: profile_home)
    monkeypatch.setattr("observatory.thinking.thinking_started", lambda channel: None)
    adapter = MIRCAdapter(PlatformConfig(enabled=True, extra={}, typing_indicator=False))
    events = []
    replies = []

    async def handle(event):
        assert get_hermes_home_override() == str(profile_home)
        events.append(event)

    async def send(chat_id, content, **kwargs):
        replies.append((chat_id, content))
        return SendResult(success=True)

    adapter.set_message_handler(handle)
    monkeypatch.setattr(adapter, "handle_message", handle)
    monkeypatch.setattr(adapter, "send", send)
    yield SimpleNamespace(
        manager=manager, adapter=adapter, events=events, replies=replies,
    )
    rooms.drop_child_steer("child")
    state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "!exit", "/exit", "!approve", "/approve", "!status", "/status", "/unknown",
])
async def test_child_gateway_commands_dispatch_once_without_steering(child_adapter, text):
    from gateway.session import build_session_key
    from mercury_constants import get_hermes_home_override

    env = child_adapter
    steered = []
    rooms.register_child_steer("child", steered.append)
    await env.adapter._dispatch_message(text, "#parent-child", "group", "owner", "owner")
    assert steered == []
    assert env.replies == []
    assert len(env.events) == 1
    event = env.events[0]
    assert event.text == ("/" + text[1:] if text.startswith("!") else text)
    assert event.source.chat_id == "#parent-child"
    assert event.source.profile == "research"
    source = env.adapter.build_source(
        "#parent-child", chat_type="group", user_id="owner", user_name="owner",
    )
    source.profile = "research"
    assert build_session_key(event.source, profile=event.source.profile) == build_session_key(
        source, profile="research",
    )
    assert get_hermes_home_override() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "refused", "finished", "sync-error", "async-error"])
async def test_child_chat_never_gateway_dispatches(child_adapter, outcome):
    env = child_adapter
    steered = []

    def steer(text):
        steered.append(text)
        if outcome.endswith("error"):
            raise RuntimeError("transport ended")
        if outcome == "refused":
            return False

    async def async_steer(text):
        return steer(text)

    if outcome != "finished":
        rooms.register_child_steer("child", async_steer if outcome == "async-error" else steer)
    await env.adapter._dispatch_message("change direction", "#parent-child", "group", "owner", "owner")
    assert env.events == []
    assert steered == ([] if outcome == "finished" else ["change direction"])
    expected = {
        "accepted": "steered (as owner).",
        "refused": "subagent is no longer accepting input.",
        "finished": "that subagent already finished — its room is history now.",
        "sync-error": "steer failed: transport ended",
        "async-error": "steer failed: transport ended",
    }
    assert env.replies == [("#parent-child", expected[outcome])]


@pytest.mark.asyncio
async def test_root_approval_keeps_profile_and_session_source(child_adapter):
    from gateway.session import build_session_key

    env = child_adapter
    steered = []
    rooms.register_child_steer("child", steered.append)
    await env.adapter._dispatch_message("!approve", "#parent", "group", "owner", "owner")
    assert steered == []
    assert env.replies == []
    assert len(env.events) == 1
    event = env.events[0]
    assert event.text == "/approve"
    assert event.source.chat_id == "#parent"
    assert event.source.profile == "research"
    source = env.adapter.build_source(
        "#parent", chat_type="group", user_id="owner", user_name="owner",
    )
    source.profile = "research"
    assert build_session_key(event.source, profile=event.source.profile) == build_session_key(
        source, profile="research",
    )


@pytest.mark.asyncio
async def test_handle_omp_message_task_then_steer(tmp_path, monkeypatch) -> None:
    class FakeRpc:
        def __init__(self):
            self.tasks: list[str] = []
            self.steers: list[str] = []

        def run_task(self, prompt: str) -> dict:
            self.tasks.append(prompt)
            return {
                "summary": "did it",
                "turn_frames": [{"feed": "tool", "tool": "read"}],
            }

        def steer(self, text: str) -> None:
            self.steers.append(text)

    bot = FakeBot()
    state = _real_state(tmp_path)
    mgr = RoomManager(state, bot)
    state.add_node(
        "orch-2", engine="omp", name="king", slug="king", mxid="king", session_ref="s"
    )
    state.set_room_id("orch-2", "#king")
    rpc = FakeRpc()
    rooms.register_omp_room("orch-2", "#king", rpc)
    try:
        import asyncio as _asyncio

        reply = await mgr.handle_omp_message("#king", "op", "build x")
        assert reply == ""
        async with _asyncio.timeout(5):
            while rooms._omp_rooms["orch-2"]["busy"]:
                await _asyncio.sleep(0.02)
        assert rpc.tasks and "build x" in rpc.tasks[0]
        assert any("read" in t for _, t in bot.said)
        assert any("did it" in t for _, t in bot.said)
    finally:
        rooms.drop_omp_room("orch-2")
    # A missing child of a live row rebuilds transparently; pin the
    # rebuild to fail here so this asserts the session-preserving error.
    import observatory.spawn as spawn_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("no omp binary in tests")

    monkeypatch.setattr(spawn_mod, "resurrect_omp_handle", _boom)
    reply = await mgr.handle_omp_message("#king", "op", "again")
    assert "temporarily unavailable" in reply
    assert "history are preserved" in reply


@pytest.mark.asyncio
async def test_dead_child_resurrects_transparently(tmp_path, monkeypatch) -> None:
    """A provably-exited child process rebuilds on the next room message
    instead of leaving the room silent until gateway reconnect."""
    import observatory.spawn as spawn_mod

    class DeadRpc:
        class _Proc:
            def poll(self):
                return 1

        proc = _Proc()

    built: dict = {}

    class FreshRpc:
        def __init__(self):
            self.tasks: list[str] = []

        def run_task(self, prompt: str) -> dict:
            self.tasks.append(prompt)
            return {"summary": "back", "turn_frames": []}

        def steer(self, text: str) -> None:
            raise AssertionError("no steer expected")

    fresh = FreshRpc()

    def _fake_resurrect(*, state, registry, node_id, channel, mercury_home=None):
        built.update(node_id=node_id, channel=channel)
        rooms.register_omp_room(node_id, channel, fresh)
        return fresh

    monkeypatch.setattr(spawn_mod, "resurrect_omp_handle", _fake_resurrect)
    bot = FakeBot()
    state = _real_state(tmp_path)
    mgr = RoomManager(state, bot)
    state.add_node(
        "orch-9", engine="omp", name="zed", slug="zed", mxid="zed", session_ref="s"
    )
    state.set_room_id("orch-9", "#zed")
    rooms.register_omp_room("orch-9", "#zed", DeadRpc())
    try:
        import asyncio as _asyncio

        reply = await mgr.handle_omp_message("#zed", "op", "you back?")
        assert reply == ""
        assert built.get("node_id") == "orch-9"
        async with _asyncio.timeout(5):
            while rooms._omp_rooms["orch-9"]["busy"]:
                await _asyncio.sleep(0.02)
        assert fresh.tasks and "you back?" in fresh.tasks[0]
        assert any("back" in t for _, t in bot.said)
    finally:
        rooms.drop_omp_room("orch-9")


@pytest.mark.asyncio
async def test_live_child_never_rebuilds(tmp_path, monkeypatch) -> None:
    """A running child (None poll) and test doubles (no proc) take
    today's path — no rebuild attempted."""
    import observatory.spawn as spawn_mod

    def _boom(*args, **kwargs):
        raise AssertionError("must not rebuild a live child")

    monkeypatch.setattr(spawn_mod, "resurrect_omp_handle", _boom)

    class LiveRpc:
        class _Proc:
            def poll(self):
                return None

        proc = _Proc()

        def __init__(self):
            self.tasks: list[str] = []

        def run_task(self, prompt: str) -> dict:
            self.tasks.append(prompt)
            return {"summary": "ok", "turn_frames": []}

        def steer(self, text: str) -> None:
            pass

    bot = FakeBot()
    state = _real_state(tmp_path)
    mgr = RoomManager(state, bot)
    state.add_node(
        "orch-7", engine="omp", name="live", slug="live", mxid="live", session_ref="s"
    )
    state.set_room_id("orch-7", "#live")
    rpc = LiveRpc()
    rooms.register_omp_room("orch-7", "#live", rpc)
    try:
        import asyncio as _asyncio

        assert await mgr.handle_omp_message("#live", "op", "go") == ""
        async with _asyncio.timeout(5):
            while rooms._omp_rooms["orch-7"]["busy"]:
                await _asyncio.sleep(0.02)
        assert rpc.tasks
    finally:
        rooms.drop_omp_room("orch-7")


def test_omp_child_kwargs_for_row() -> None:
    from observatory.spawn import omp_child_kwargs_for_row

    row = {
        "session_ref": "s.jsonl",
        "extra": {"model": "m", "profile": ""},
    }
    kwargs = omp_child_kwargs_for_row(row, mercury_home="/h")
    assert kwargs == {
        "model": "m",
        "mercury_home": "/h",
        "resume_session": "s.jsonl",
        "profile_home": None,
    }
    assert omp_child_kwargs_for_row({})["resume_session"] is None


@pytest.mark.asyncio
async def test_omp_room_approval_reaches_its_owner_and_returns_to_waiting_child(monkeypatch):
    import importlib
    approval = importlib.import_module("tools.approval")
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 1)
    key = "test:irc:group:#task-room:owner"
    prompts = []
    decisions = []
    manager = RoomManager(FakeState([]), FakeBot())
    async def no_feed(*args):
        return None
    monkeypatch.setattr(manager, "_start_live_omp_feed", no_feed)
    def notify(channel, text, **kwargs):
        prompts.append((channel, text))
        assert approval.resolve_gateway_approval("unrelated", "once") == 0
        assert approval.resolve_gateway_approval(key, "once") == 1
        return True
    monkeypatch.setattr(rooms, "say_nowait", notify)
    class RpcTurn:
        def run_task(self, prompt):
            decisions.append(approval.request_tool_approval("write", "child asks to write", require_human=True)["approved"])
            return {"summary": "approved write", "turn_frames": []}
    before = approval.get_current_session_key()
    await manager._run_spawned_omp_task("#task-room", "node-task", "owner", "write", RpcTurn(), approval_session_key=key)
    assert decisions == [True]
    assert prompts[0][0] == "#task-room"
    assert "!approve" in prompts[0][1]
    assert manager.bot.said == [("#task-room", "approved write")]
    assert approval.get_current_session_key() == before
    assert key not in approval._gateway_notify_cbs
