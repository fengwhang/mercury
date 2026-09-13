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


def test_channel_naming() -> None:
    assert gateway_channel("mercury") == "#mercury_gateway"
    assert spawn_channel("Ace") == "#ace"
    assert child_channel("parent", "child") == "#parent-child"
    assert child_channel("My Agent", "Kid 1") == "#my-agent-kid-1"


def test_format_frame_shapes() -> None:
    assert format_frame({"feed": "message", "text": "hello"}) == "hello"
    assert format_frame({"feed": "message", "text": "  "}) is None
    tool = format_frame({"feed": "tool", "tool": "bash",
                         "args": "ls", "subagent_id": ""})
    assert tool is not None and "bash" in tool and "ls" in tool
    thought = format_frame({"feed": "thought", "text": "hmm",
                            "subagent_id": "grand"})
    assert thought is not None and "[grand]" in thought and "hmm" in thought
    assert format_frame({"feed": "bogus"}) is None
    assert format_frame("nope") is None


def test_format_lifecycle() -> None:
    assert "started" in format_lifecycle("start", name="kid")
    assert "done" in format_lifecycle("stop", name="kid", summary="ok").lower() \
        or "finished" in format_lifecycle("stop", name="kid", summary="ok")


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
        {"node_id": "gw", "engine": "hermes", "depth": 0,
         "parent_node_id": None, "room_id": "#mercury_gateway",
         "extra": {"kind": "gateway"}},
        {"node_id": "orch-1", "engine": "hermes", "depth": 0,
         "parent_node_id": None, "room_id": "#ace", "extra": {}},
        {"node_id": "orch-2", "engine": "omp", "depth": 0,
         "parent_node_id": None, "room_id": "#king", "extra": {}},
        {"node_id": "deleg-1", "engine": "omp", "depth": 1,
         "parent_node_id": "gw", "room_id": "#mercury_gateway-cow",
         "extra": {}},
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
