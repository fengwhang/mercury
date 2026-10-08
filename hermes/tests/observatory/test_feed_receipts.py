"""Only socket-send receipts may suppress the existing batched turn replay."""
import asyncio
from types import SimpleNamespace

import pytest

from observatory import gateway_session as gs, rooms
from observatory.omp_feed import ToolEvent, ThoughtEvent, MessageEvent, StatusEvent, TurnFrameDedupe


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no-sink", "rejected", "exception"])
async def test_failed_live_send_remains_replayable(tmp_path, monkeypatch, failure):
    received = []
    emitted = asyncio.Event()
    class Bot:
        available = False
        async def say(self, channel, text, **kwargs):
            if not self.available:
                if failure == "exception":
                    raise ConnectionError("closed socket")
                return False
            received.append(text)
            return True
    bot = Bot()
    frames = [
        {"feed": "tool", "subagent_id": "", "tool": "bash", "args": "outage tool"},
        {"feed": "thought", "subagent_id": "", "text": "outage thought"},
        {"feed": "message", "subagent_id": "", "role": "assistant", "text": "outage result"},
        {"feed": "status", "subagent_id": "", "text": "Tool completed: bash"},
    ]
    class Feed:
        _dispose_listener = None
        _dispose_agent_listener = None
        async def start(self):
            pass
        async def stop(self):
            pass
        async def events(self):
            yield ToolEvent(subagent_id="", tool="bash", args="outage tool")
            yield ThoughtEvent(subagent_id="", text="outage thought")
            yield MessageEvent(subagent_id="", role="assistant", text="outage result")
            yield StatusEvent(subagent_id="", text="Tool completed: bash")
            emitted.set()
            await asyncio.Event().wait()
    monkeypatch.setattr("observatory.omp_feed.OmpFeed", lambda transport: Feed())
    monkeypatch.setattr(gs, "_watcher_manager", lambda: SimpleNamespace(channel_for_node=lambda child: "#receipt"))
    monkeypatch.setattr(rooms, "channel_for_node_id", lambda child: "#receipt")
    old_sink, old_loop = rooms.get_bot_sink(), rooms._loop_now()
    rooms.set_event_loop(asyncio.get_running_loop())
    rooms.set_bot_sink(None if failure == "no-sink" else bot)
    child_id = "receipt/0"
    pump = asyncio.create_task(gs._forward_child_feed(child_id, object(), {}))
    try:
        await asyncio.wait_for(emitted.wait(), 2)
        assert received == []
        bot.available = True
        rooms.set_bot_sink(bot)
        assert await asyncio.to_thread(gs.replay_child_turn_frames, child_id, frames) == 4
        await asyncio.sleep(.05)
        assert len(received) == 4
        assert "outage tool" in received[0]
        assert "outage thought" in received[1]
        assert "outage result" in received[2]
        assert "Tool completed: bash" in received[3]
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        rooms.set_bot_sink(old_sink)
        rooms.set_event_loop(old_loop)


@pytest.mark.asyncio
async def test_watcher_activity_end_clears_thinking_without_channel_sink(monkeypatch):
    from observatory.omp_feed import ActivityEvent

    activities = []
    async def publish_frame(channel, payload):
        activities.append(payload["active"])
        return True
    class Feed:
        async def start(self):
            pass
        async def stop(self):
            pass
        async def events(self):
            yield ActivityEvent(subagent_id="", active=False)
    monkeypatch.setattr("observatory.omp_feed.OmpFeed", lambda transport: Feed())
    monkeypatch.setattr(gs, "_watcher_manager", lambda: SimpleNamespace(
        channel_for_node=lambda child: "#receipt", publish_frame=publish_frame))
    old_sink, old_loop = rooms.get_bot_sink(), rooms._loop_now()
    rooms.set_event_loop(asyncio.get_running_loop())
    rooms.set_bot_sink(None)
    try:
        await asyncio.to_thread(asyncio.run, gs._forward_child_feed("activity/0", object(), {}))
        assert activities == [False]
    finally:
        rooms.set_bot_sink(old_sink)
        rooms.set_event_loop(old_loop)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_accepted", [True, False])
async def test_live_replay_race_waits_for_socket_receipt(live_accepted):
    dd = TurnFrameDedupe()
    key = ("message", "", "assistant", "result")
    started, release = asyncio.Event(), asyncio.Event()
    wire = []
    async def live_send():
        started.set()
        await release.wait()
        if live_accepted:
            wire.append("result")
        return live_accepted
    async def replay_send(index):
        wire.append("result")
        return True
    live = asyncio.create_task(dd.publish_live(key, live_send))
    await started.wait()
    replay = asyncio.create_task(dd.publish_replay([key], replay_send))
    await asyncio.sleep(0)
    assert not replay.done(), "scheduled send is not a delivery receipt"
    release.set()
    assert await live is live_accepted
    assert await replay == (0 if live_accepted else 1)
    assert wire == ["result"]


@pytest.mark.asyncio
async def test_rejected_replay_does_not_suppress_late_live():
    dd = TurnFrameDedupe()
    key = ("message", "", "assistant", "result")
    wire = []
    async def failed(index):
        return False
    async def accepted():
        wire.append("result")
        return True
    assert await dd.publish_replay([key], failed) == 0
    assert await dd.publish_live(key, accepted) is True
    assert wire == ["result"]


@pytest.mark.asyncio
async def test_replaying_known_failed_live_does_not_cover_identical_next_work():
    dd = TurnFrameDedupe()
    key = ("status", "", "Tool completed: bash")
    async def failed():
        return False
    async def accepted(*args):
        return True
    assert await dd.publish_live(key, failed) is False
    assert await dd.publish_replay([key], accepted) == 1
    assert await dd.publish_live(key, accepted) is True
    assert await dd.publish_replay([key], accepted) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("live_accepted", [True, False])
async def test_watcher_loop_and_replay_worker_share_gateway_receipt_transaction(monkeypatch, live_accepted):
    import threading

    gateway_thread = threading.get_ident()
    threads = {}
    entered, release = asyncio.Event(), asyncio.Event()
    stop = threading.Event()
    wire = []
    attempts = 0
    class Bot:
        async def say(self, channel, text, **kwargs):
            nonlocal attempts
            assert threading.get_ident() == gateway_thread
            attempts += 1
            if attempts == 1:
                entered.set()
                await release.wait()
                if not live_accepted:
                    return False
            wire.append(text)
            return True
    class Feed:
        async def start(self):
            pass
        async def stop(self):
            pass
        async def events(self):
            threads["watcher"] = threading.get_ident()
            yield ToolEvent(subagent_id="", tool="bash", args="thread-race")
            await asyncio.to_thread(stop.wait)
    monkeypatch.setattr("observatory.omp_feed.OmpFeed", lambda transport: Feed())
    monkeypatch.setattr(gs, "_watcher_manager", lambda: SimpleNamespace(channel_for_node=lambda child: "#receipt"))
    monkeypatch.setattr(rooms, "channel_for_node_id", lambda child: "#receipt")
    old_sink, old_loop = rooms.get_bot_sink(), rooms._loop_now()
    rooms.set_event_loop(asyncio.get_running_loop())
    rooms.set_bot_sink(Bot())
    def replay():
        threads["replay"] = threading.get_ident()
        return gs.replay_child_turn_frames("thread-race/0", [
            {"feed": "tool", "subagent_id": "", "tool": "bash", "args": "thread-race"}])
    watcher = asyncio.create_task(asyncio.to_thread(
        asyncio.run, gs._forward_child_feed("thread-race/0", object(), {})))
    replay_task = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        replay_task = asyncio.create_task(asyncio.to_thread(replay))
        await asyncio.sleep(.05)
        assert not replay_task.done()
        release.set()
        assert await asyncio.wait_for(replay_task, 2) == (0 if live_accepted else 1)
        assert len(wire) == 1
        assert threads["watcher"] != threads["replay"]
        assert gateway_thread not in threads.values()
        print(f"canonical gateway={gateway_thread} watcher={threads['watcher']} replay={threads['replay']}; accepted={live_accepted}; one wire frame")
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(watcher, 2)
        if replay_task:
            await replay_task
        rooms.set_bot_sink(old_sink)
        rooms.set_event_loop(old_loop)


@pytest.mark.asyncio
async def test_cancelled_live_socket_wait_releases_transaction_for_replay():
    dd = TurnFrameDedupe()
    entered = asyncio.Event()
    key = ("message", "", "assistant", "result")
    async def blocked():
        entered.set()
        await asyncio.Event().wait()
    live = asyncio.create_task(dd.publish_live(key, blocked))
    await entered.wait()
    live.cancel()
    await asyncio.gather(live, return_exceptions=True)
    async def accepted(index):
        return True
    assert await asyncio.wait_for(dd.publish_replay([key], accepted), 1) == 1
