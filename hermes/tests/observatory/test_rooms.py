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


class FakeBot:
    def __init__(self):
        self.joined: list[str] = []
        self.said: list[tuple[str, str]] = []
        self.destroyed: list[str] = []

    async def join_channel(self, channel: str) -> bool:
        self.joined.append(channel)
        return True

    async def part_channel(self, channel: str) -> bool:
        return True

    async def say(self, channel: str, text: str) -> bool:
        self.said.append((channel, text))
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


def test_inbound_route() -> None:
    mgr = RoomManager(FakeState(_rows()), FakeBot())
    assert mgr.inbound_route("#mercury_gateway")[0] == "gateway"
    assert mgr.inbound_route("#ace")[0] == "spawn-hermes"
    assert mgr.inbound_route("#king")[0] == "spawn-omp"
    assert mgr.inbound_route("#mercury_gateway-cow")[0] == "child"
    assert mgr.inbound_route("#unknown")[0] == "passthrough"


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
async def test_drain_queue_creates_child_room(tmp_path) -> None:
    bot = FakeBot()
    mgr = RoomManager(_real_state(tmp_path), bot)
    rooms.submit_lifecycle(
        "deleg-1", "start", name="cow", parent_name="gateway", engine="omp"
    )
    rooms.submit_feed(
        "deleg-1", {"feed": "tool", "tool": "bash", "args": "ls", "subagent_id": ""}
    )
    assert await mgr.drain_queue() == 2
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
    rooms.submit_lifecycle(
        "deleg-9", "start", name="kid", parent_name="gateway", engine="hermes"
    )
    assert await mgr.drain_queue() == 1
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
    rooms.submit_lifecycle("deleg-9", "stop", name="kid", summary="done")
    assert await mgr.drain_queue() == 1
    assert "finished" in (
        await mgr.handle_child_message("#gateway-kid", "op", "again")
    ).lower() or "history" in await mgr.handle_child_message(
        "#gateway-kid", "op", "again"
    )


@pytest.mark.asyncio
async def test_handle_omp_message_task_then_steer(tmp_path) -> None:
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
        reply = await mgr.handle_omp_message("#king", "op", "build x")
        assert reply == "did it"
        assert rpc.tasks and "build x" in rpc.tasks[0]
        assert any("read" in t for _, t in bot.said)
    finally:
        rooms.drop_omp_room("orch-2")
    assert "gone" in await mgr.handle_omp_message("#king", "op", "again")
