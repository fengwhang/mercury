"""Loopback socket outage during a real, model-free RPC prompt turn."""
import asyncio
import json
import os
import sys

import pytest

from observatory import gateway_session as gs, identity, platform_hook, rooms, thinking
from observatory.rooms import RoomManager
from observatory.spawn import OrchestratorRegistry
from observatory.state import ObservatoryState
from tests.observatory.test_ircd import RawClient, running_daemon
from tests.observatory.test_mirc_adapter_liveness import make_adapter, until
from tests.observatory.test_subagent_room_lifecycle import _seed
from tools import async_delegation as ad
from tools.omp_rpc_transport import OmpRpcChild
from gateway.status import get_process_start_time


_WORKER = r'''
import json, sys
def emit(frame):
    with open(sys.argv[1], "a") as log:
        log.write(json.dumps({"direction": "out", "frame": frame}) + "\n")
    print(json.dumps(frame), flush=True)
def work(label, finish=False):
    emit({"type": "tool_execution_start", "toolCallId": label, "toolName": "bash",
          "args": {"command": label + " tool"}})
    emit({"type": "message_update", "message": {"role": "assistant"},
          "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0,
                                    "content": label + " thought", "partial": {"role": "assistant"}}})
    if finish:
        emit({"type": "tool_execution_end", "toolCallId": label, "toolName": "bash",
              "result": {"content": [{"type": "text", "text": label + " tool result"}]}, "isError": False})
        emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
              "content": [{"type": "text", "text": label + " result"}]}})
        emit({"type": "agent_end", "messages": [], "message_count": 1, "isTerminal": True})
emit({"type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2],
      "maxFrameBytes": 1048576, "maxReassembledFrameBytes": 67108864})
for line in sys.stdin:
    frame = json.loads(line)
    with open(sys.argv[1], "a") as log:
        log.write(json.dumps({"direction": "in", "frame": frame}) + "\n")
    command = frame["type"]
    data = {"protocolVersion": 2} if command == "negotiate_protocol" else {}
    if command == "prompt":
        data = {"agentInvoked": True}
    emit({"type": "response", "id": frame.get("id"), "command": command,
          "success": True, "data": data})
    if command == "prompt":
        emit({"type": "agent_start"})
        work(frame["message"], frame["message"] == "after")
    if command == "steer":
        work(frame["message"], True)
'''


@pytest.mark.asyncio
@pytest.mark.parametrize("ambient", [False, True])
async def test_actual_outage_replays_unseen_self_frames_once_keeps_owner_and_listeners(tmp_path, monkeypatch, ambient):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    monkeypatch.setattr(identity, "_pool", identity.IdentityPool())
    script = tmp_path / "worker.py"
    script.write_text(_WORKER)
    rpc_log = tmp_path / "rpc-wire.jsonl"
    child = OmpRpcChild(omp_path=sys.executable, model="fixture/no-model",
                        command_override=[sys.executable, str(script), str(rpc_log)],
                        env=dict(os.environ), startup_timeout=5)
    await asyncio.to_thread(child.start)
    state = ObservatoryState(tmp_path / "state.db")
    observer = RawClient()
    old_sink, old_loop, old_manager = rooms.get_bot_sink(), rooms._loop_now(), rooms.get_room_manager()
    pump = capture = turn = None
    observed = []
    feeds = {}
    release = asyncio.Event()
    down = asyncio.Event()
    recovered = asyncio.Event()
    child_id = "outage/0"
    channel = "#owned-outage"
    try:
        async with running_daemon(tmp_path, password="fixture", agent_password="fixture") as (daemon, port, _):
            if ambient:
                for key, value in {
                    "IRC_SERVER": "127.0.0.1", "IRC_PORT": str(port),
                    "IRC_NICKNAME": "nixpad_gateway", "IRC_CHANNEL": "#ambient",
                    "IRC_AGENT_PASSWORD": "ambient-agent", "IRC_OPER_PASSWORD": "ambient-oper",
                }.items():
                    monkeypatch.setenv(key, value)
            monkeypatch.setattr(identity, "_endpoint", lambda: ("127.0.0.1", port, "fixture"))
            adapter = make_adapter(port, monkeypatch)
            assert adapter.nickname == "testbot" and adapter.channel == "#test"
            assert adapter.agent_password == adapter.oper_password == ""
            adapter.agent_password = adapter.oper_password = "fixture"
            async def recover(failed):
                await failed.disconnect()
                down.set()
                await release.wait()
                assert await failed.connect(is_reconnect=True)
                recovered.set()
            adapter.set_fatal_error_handler(recover)
            try:
                assert await adapter.connect()
                _seed(state, "root", depth=0, room="#root", parent=None)
                _seed(state, child_id, depth=1, room=channel, parent="root")
                ad.record_child_spawn(child_id, "outage", child_pid=child.pid,
                                      child_started_at=get_process_start_time(child.pid))
                manager = RoomManager(state, adapter)
                rooms.set_room_manager(manager)
                await adapter.join_channel(channel)
                await observer.connect(port)
                await observer.register("observer", password="fixture")
                await observer.send("JOIN " + channel)
                await observer.next_match(" 366 ")
                async def record():
                    while True:
                        observed.append(await observer.lines.get())
                capture = asyncio.create_task(record())
                pump = asyncio.create_task(gs._forward_child_feed(child_id, child, feeds))
                await until(lambda: state.get(child_id)["extra"].get("execution_state") == "ready")
                await until(lambda: any("execution ready" in line for line in observed))
                feed = feeds[child_id]
                listeners = (feed._dispose_listener, feed._dispose_agent_listener)
                pid = child.pid
                turn = asyncio.create_task(asyncio.to_thread(child.run_task, "before", 10))
                await until(lambda: any("before thought" in line for line in observed))
                assert channel in thinking._tasks
                daemon._clients[adapter.nickname].writer.close()
                await asyncio.wait_for(down.wait(), 5)
                await asyncio.to_thread(child.steer, "outage")
                result = await turn
                assert result["status"] == "completed" and result["summary"] == "outage result"
                assert state.get(child_id)["extra"]["task_state"] == "running"
                assert not any("finished" in line or "died before" in line for line in observed)
                await until(lambda: channel not in thinking._tasks)
                assert not any("outage thought" in line or "outage result" in line for line in observed)
                assert not pump.done() and child.pid == pid and child.proc.poll() is None
                release.set()
                await asyncio.wait_for(recovered.wait(), 5)
                await platform_hook.boot_resync(manager, state, OrchestratorRegistry())
                assert state.get(child_id)["extra"]["execution_state"] == "ready"
                assert state.get(child_id)["extra"]["feed_attached"] is True
                assert await asyncio.to_thread(gs.replay_child_turn_frames, child_id, result["turn_frames"]) == 4
                await until(lambda: any("outage result" in line for line in observed))
                assert (feed._dispose_listener, feed._dispose_agent_listener) == listeners
                after = await asyncio.to_thread(child.run_task, "after", 5)
                await until(lambda: any("after result" in line for line in observed))
                assert await asyncio.to_thread(gs.replay_child_turn_frames, child_id, after["turn_frames"]) == 0
                await asyncio.sleep(.1)
                for text in ("before tool", "before thought", "outage tool", "outage thought",
                             "outage result", "after tool", "after thought", "after result"):
                    assert sum(text in line for line in observed) == 1, (text, observed)
                assert sum("Tool completed: bash" in line for line in observed) == 2
                assert channel not in thinking._tasks
                wire = [json.loads(line) for line in rpc_log.read_text().splitlines()]
                commands = [record["frame"]["type"] for record in wire if record["direction"] == "in"]
                assert commands.count("set_subagent_subscription") == 1 and "abort" not in commands
                assert child.pid == pid and child.proc.poll() is None
                ad.record_child_terminal(child_id, "completed", summary="verified task completion")
                await manager.reconcile_terminal_children()
                await until(lambda: any("finished" in line and "verified task completion" in line for line in observed))
                assert sum("finished" in line for line in observed) == 1
                with pytest.raises(KeyError):
                    state.get(child_id)
                print(json.dumps({"smoke": "real-loopback-outage", "pid_before": pid, "pid_after": child.pid,
                                  "listener_unchanged": True, "subscription_count": 1, "terminal_markers": 1,
                                  "irc_agent_port": port, "agent_listener_authenticated": True,
                                  "ambient_state_replay": ambient, "listener_ids_before": [id(fn) for fn in listeners],
                                  "listener_ids_after": [id(feed._dispose_listener), id(feed._dispose_agent_listener)],
                                  "abort_count": 0, "replay_accepted": 4, "rpc_wire": wire,
                                  "irc_wire": observed, "isolated_home": str(tmp_path)}))
            finally:
                release.set()
                if capture:
                    capture.cancel()
                    await asyncio.gather(capture, return_exceptions=True)
                await observer.close()
                for target in ("#root", channel):
                    await identity.drop_identity(target)
                await adapter.disconnect()
    finally:
        if pump:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if turn and not turn.done():
            await asyncio.to_thread(child.stop)
            await asyncio.gather(turn, return_exceptions=True)
        await asyncio.to_thread(child.stop)
        thinking.thinking_done(channel)
        state.close()
        rooms.set_bot_sink(old_sink)
        rooms.set_event_loop(old_loop)
        rooms.set_room_manager(old_manager)
