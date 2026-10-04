"""Exhausted inference cannot swallow local commands in an OMP room."""
import asyncio
import sys
from unittest.mock import AsyncMock

import pytest

from observatory import rooms
from observatory.state import ObservatoryState
from tools.omp_rpc_transport import OmpRpcChild


SERVER = r'''
import json, sys
model = "test/exhausted"
def emit(frame):
    print(json.dumps(frame), flush=True)
emit({"type": "ready", "protocolVersion": 1})
for line in sys.stdin:
    req = json.loads(line)
    kind = req["type"]
    data = {}
    if kind == "prompt":
        text = req["message"]
        if text.startswith("/model"):
            parts = text.split(None, 1)
            if len(parts) > 1:
                model = parts[1]
            emit({"type": "command_output", "text": "Current model: " + model})
            data = {"agentInvoked": False}
        elif text == "/help":
            emit({"type": "command_output", "text": "Available commands: /model, /help"})
            data = {"agentInvoked": False}
        else:
            emit({"type": "agent_start"})
            message = {"role": "assistant", "content": [], "api": "openai-responses",
                "provider": "test", "model": "exhausted", "stopReason": "error",
                "errorMessage": "The usage limit has been reached (code=usage_limit_reached)",
                "timestamp": 0, "usage": {"input": 0, "output": 0, "cacheRead": 0,
                    "cacheWrite": 0, "totalTokens": 0,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}}
            emit({"type": "message_end", "message": message})
            emit({"type": "agent_end", "messages": [message]})
            data = {"agentInvoked": True}
    emit({"type": "response", "id": req.get("id"), "command": kind, "success": True, "data": data})
'''


@pytest.mark.asyncio
async def test_commands_remain_local_during_exhaustion_and_busy_turns(tmp_path, monkeypatch):
    script = tmp_path / "fake_provider.py"
    script.write_text(SERVER)
    child = OmpRpcChild(omp_path=sys.executable, model="test/exhausted",
                        command_override=[sys.executable, str(script)])
    await asyncio.to_thread(child.start)
    state = ObservatoryState(tmp_path / "state.db")
    state.add_node("root", engine="omp", name="root", slug="root", mxid="root",
                   session_ref="root", parent_node_id=None, extra={"kind": "spawn"})
    state.set_room_id("root", "#root")
    bot = type("Bot", (), {"say": AsyncMock(return_value=True)})()
    manager = rooms.RoomManager(state, bot)
    rooms.register_omp_room("root", "#root", child)
    monkeypatch.setattr(manager, "_start_live_omp_feed", AsyncMock(return_value=None))
    try:
        await manager.handle_omp_message("#root", "owner", "continue")
        async with asyncio.timeout(5):
            while rooms._omp_rooms["root"]["busy"]:
                await asyncio.sleep(0.01)
        notices = [call.args[1] for call in bot.say.await_args_list]
        assert any("usage limit" in text and "!model" in text for text in notices)
        assert "(no output)" not in notices
        # Commands do not wait for, steer, or invoke the exhausted provider.
        rooms._omp_rooms["root"]["busy"] = True
        assert await manager.handle_omp_message("#root", "owner", "/model") == "Current model: test/exhausted"
        assert await manager.handle_omp_message("#root", "owner", "/model test/working") == "Current model: test/working"
        assert rooms._omp_rooms["root"]["busy"] is True
        rooms._omp_rooms["root"]["busy"] = False
        bot.say.reset_mock()
        await manager.handle_omp_message("#root", "owner", "/help")
        async with asyncio.timeout(5):
            while rooms._omp_rooms["root"]["busy"]:
                await asyncio.sleep(0.01)
        assert [call.args[1] for call in bot.say.await_args_list] == ["Available commands: /model, /help"]
        assert child._client._scheduled_agent_runs == child._client._completed_agent_runs
        assert not child._client._inflight_runs
    finally:
        rooms.drop_omp_room("root")
        await asyncio.to_thread(child.stop)
        state.close()
