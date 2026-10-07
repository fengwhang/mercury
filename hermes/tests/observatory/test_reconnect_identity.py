"""Bootstrap identities follow durable task fate, not stale room rows."""

import os

import pytest

from observatory import identity, platform_hook, rooms, spawn
from observatory.rooms import RoomManager
from observatory.spawn import OrchestratorRegistry
from observatory.state import ObservatoryState
from tests.observatory.test_subagent_room_lifecycle import _seed
from tools import async_delegation as ad


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_ready", [True, False])
async def test_exact_delegation_terminal_boot_never_recreates_identity(
    tmp_path, monkeypatch, transport_ready
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    monkeypatch.setattr(identity, "_pool", identity.IdentityPool())
    state = ObservatoryState(tmp_path / "observatory" / "state.db")
    actions = []

    class Bot:
        async def join_channel(self, room):
            actions.append(("join", room))
            return transport_ready or room != channel

        async def say(self, channel, text, **kwargs):
            actions.append(("say", channel, text))
            return True

        async def destroy_channel(self, channel):
            actions.append(("destroy", channel))
            return True

        async def invite_user(self, *args):
            return True

        async def part_channel(self, channel):
            return True

    async def ensure(nick, channel):
        actions.append(("identity", channel, nick))
        return True

    bot = Bot()
    monkeypatch.setattr(rooms, "get_bot_sink", lambda: bot)
    monkeypatch.setattr(spawn, "get_bot_sink", lambda: bot)
    monkeypatch.setattr(identity, "ensure_identity", ensure)
    channel = "#nixpad_mercurator-mercury-stats-both-engines"
    child_id = "deleg_97e27028/0"
    try:
        _seed(state, "root", depth=0, room="#root", parent=None)
        _seed(state, child_id, depth=1, room=channel, parent="root")
        state.update_extra(child_id, kind="delegate")
        ad.record_child_spawn(
            child_id, "deleg_97e27028", name="mercury-stats-both-engines"
        )
        ad.record_child_terminal(
            child_id, "interrupted", error="worker killed after owner exit 75"
        )
        report = await platform_hook.boot_resync(
            RoomManager(state, bot), state, OrchestratorRegistry()
        )
        assert not any(
            action[0] == "identity" and action[1] == channel for action in actions
        )
        assert channel not in report["joined"]
        expected = {"root"} if transport_ready else {"root", child_id}
        assert {row["node_id"] for row in state.get_live()} == expected
        assert (
            any(
                action[0] == "say" and "worker killed" in action[2]
                for action in actions
            )
            == transport_ready
        )
        if not transport_ready:
            assert report["failed"]
            assert not state.get(child_id)["extra"].get("terminal_marker_delivered")
        assert ("identity", "#root", "root") in actions
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fate", ["live", "missing", "unverified", "dead", "pending", "completed"]
)
async def test_boot_descendant_identity_requires_live_lineage(
    tmp_path, monkeypatch, fate
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from gateway.status import get_process_start_time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    monkeypatch.setattr(identity, "_pool", identity.IdentityPool())
    state = ObservatoryState(tmp_path / "observatory" / "state.db")
    bot = SimpleNamespace(
        join_channel=AsyncMock(return_value=True),
        say=AsyncMock(return_value=True),
        invite_user=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(rooms, "get_bot_sink", lambda: bot)
    ensure = AsyncMock(return_value=True)
    monkeypatch.setattr(identity, "ensure_identity", ensure)
    child_id = "deleg_97e27028/0"
    native_id = child_id + "/sub-worker"
    try:
        _seed(state, "root", depth=0, room="#root", parent=None)
        _seed(state, child_id, depth=1, room="#owned-child", parent="root")
        _seed(state, native_id, depth=2, room="#native-child", parent=child_id)
        pid = os.getpid() if fate not in {"unverified", "dead"} else None
        started = get_process_start_time(pid) if pid else None
        if fate == "dead":
            # A mismatched birth fingerprint proves this is not the worker.
            pid, started = os.getpid(), get_process_start_time(os.getpid()) + 1
        if fate != "missing":
            ad.record_child_spawn(
                child_id, "deleg_97e27028", child_pid=pid, child_started_at=started
            )
        if fate in {"pending", "completed"}:
            state.update_extra(native_id, task_state=fate)
        await platform_hook.boot_resync(
            RoomManager(state, bot), state, OrchestratorRegistry()
        )
        restored = {call.args[1] for call in ensure.await_args_list}
        assert "#root" in restored
        assert ("#owned-child" in restored) == (
            fate not in {"missing", "unverified", "dead"}
        )
        assert ("#native-child" in restored) == (fate == "live")
        joined = {call.args[0] for call in bot.join_channel.await_args_list}
        assert joined == restored
        # Unverifiable/dead process identity alone never authorizes deletion.
        assert {row["node_id"] for row in state.get_live()} == {
            "root",
            child_id,
            native_id,
        }
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
async def test_retained_legacy_delegation_cannot_keep_dead_owner_readiness(tmp_path, monkeypatch, pooled):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    monkeypatch.setattr(identity, "_pool", identity.IdentityPool())
    state = ObservatoryState(tmp_path / "state.db")
    bot = SimpleNamespace(join_channel=AsyncMock(return_value=True),
                          say=AsyncMock(return_value=True), invite_user=AsyncMock(return_value=True))
    monkeypatch.setattr(rooms, "get_bot_sink", lambda: bot)
    ensure = AsyncMock(return_value=True)
    monkeypatch.setattr(identity, "ensure_identity", ensure)
    child_id = "deleg_97e27028/0"
    channel = "#nixpad_mercurator-mercury-stats-both-engines"
    if pooled:
        monkeypatch.setattr(identity, "_pool", SimpleNamespace(get=lambda room: object() if room == channel else None))
    try:
        _seed(state, "root", depth=0, room="#root", parent=None)
        _seed(state, child_id, depth=1, room=channel, parent="root")
        state.update_extra(child_id, kind="delegate", execution_state="ready",
                           feed_attached=True, execution_pid=99999999, execution_started_at=1)
        report = await platform_hook.boot_resync(RoomManager(state, bot), state, OrchestratorRegistry())
        row = state.get(child_id)
        assert row["status"] == "live", "unknown topology is retained, not execution proof"
        assert row["extra"]["execution_state"] == "unverified"
        assert row["extra"]["feed_attached"] is False
        assert row["extra"]["task_state"] == "pending"
        assert channel not in report["joined"]
        assert channel not in {call.args[1] for call in ensure.await_args_list}
        assert not any("online" in str(call) for call in bot.say.await_args_list)
    finally:
        state.close()


_ONGOING_RPC = r"""
import json, sys
def emit(frame):
    print(json.dumps(frame), flush=True)
emit({"type": "ready", "protocolVersion": 1,
      "supportedProtocolVersions": [1, 2], "maxFrameBytes": 1048576,
      "maxReassembledFrameBytes": 67108864})
for line in sys.stdin:
    frame = json.loads(line)
    with open(sys.argv[1], "a") as log:
        log.write(json.dumps(frame) + "\n")
    command = frame["type"]
    data = {"protocolVersion": 2} if command == "negotiate_protocol" else {}
    emit({"type": "response", "id": frame.get("id"),
          "command": command, "success": True, "data": data})
    if command == "steer":
        label = frame.get("message", "")
        emit({"type": "subagent_event", "payload": {"id": "worker", "event": {
            "type": "message_update", "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0,
                                      "content": label + " thinking"}}}})
        emit({"type": "subagent_progress", "payload": {"id": "worker", "progress": {
            "id": "worker", "status": "running", "currentTool": "bash",
            "currentToolArgs": label + " tool"}}})
"""


@pytest.mark.asyncio
async def test_real_reconnect_keeps_worker_feed_and_native_thinking_visible(
    tmp_path, monkeypatch
):
    import asyncio
    import json
    import sys
    from gateway.status import get_process_start_time
    from observatory.omp_feed import OmpFeed
    from tests.observatory.test_ircd import RawClient, running_daemon
    from tests.observatory.test_mirc_adapter_liveness import make_adapter, until
    from tools.omp_rpc_transport import OmpRpcChild

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    pool = identity.IdentityPool()
    monkeypatch.setattr(identity, "_pool", pool)
    script = tmp_path / "ongoing_rpc.py"
    script.write_text(_ONGOING_RPC)
    wire_log = tmp_path / "rpc-wire.jsonl"
    child = OmpRpcChild(
        omp_path=sys.executable,
        model="fixture/ongoing",
        command_override=[sys.executable, str(script), str(wire_log)],
        env=dict(os.environ),
        startup_timeout=5,
    )
    await asyncio.to_thread(child.start)
    state = ObservatoryState(tmp_path / "state.db")
    observer = RawClient()
    feed = OmpFeed(child)
    pump = None
    previous_sink = rooms.get_bot_sink()
    previous_loop = rooms._loop_now()
    try:
        async with running_daemon(tmp_path, password="fixture", agent_password="fixture") as (daemon, port, _):
            monkeypatch.setattr(
                identity, "_endpoint", lambda: ("127.0.0.1", port, "fixture")
            )
            adapter = make_adapter(port, monkeypatch)
            adapter.agent_password = "fixture"
            adapter.oper_password = "fixture"
            recovered = asyncio.Event()

            async def recover(failed):
                await failed.disconnect()
                assert await failed.connect(is_reconnect=True)
                recovered.set()

            adapter.set_fatal_error_handler(recover)
            try:
                assert await adapter.connect()
                _seed(state, "root", depth=0, room="#root", parent=None)
                _seed(state, "active/0", depth=1, room="#owned", parent="root")
                _seed(
                    state,
                    "active/0/sub-worker",
                    depth=2,
                    room="#native",
                    parent="active/0",
                )
                _seed(
                    state,
                    "deleg_97e27028/0",
                    depth=1,
                    room="#nixpad_mercurator-mercury-stats-both-engines",
                    parent="root",
                )
                ad.record_child_spawn(
                    "active/0",
                    "active",
                    child_pid=child.pid,
                    child_started_at=get_process_start_time(child.pid),
                )
                ad.record_child_spawn("deleg_97e27028/0", "deleg_97e27028")
                ad.record_child_terminal(
                    "deleg_97e27028/0", "interrupted", error="proven prior worker death"
                )
                manager = RoomManager(state, bot=adapter)
                await platform_hook.boot_resync(manager, state, OrchestratorRegistry())
                conn = pool.get("#native")
                assert conn is not None
                await observer.connect(port)
                await observer.register("observer", password="fixture")
                await observer.send("JOIN #native")
                await observer.next_match(" 366 ")
                await feed.start()
                pump = asyncio.create_task(
                    manager._pump_live_omp_feed(feed, "active/0", "#owned", {}, set())
                )
                pid = child.pid
                await asyncio.to_thread(child.steer, "before")
                assert "before thinking" in await observer.next_match("before thinking")
                assert "before tool" in await observer.next_match("before tool")
                old_peer = daemon._clients[conn.nick]
                daemon._clients[adapter.nickname].writer.close()
                old_peer.writer.close()
                await asyncio.wait_for(recovered.wait(), 5)
                await until(
                    lambda: (
                        conn.nick in daemon._clients
                        and daemon._clients[conn.nick] is not old_peer
                    )
                )
                await platform_hook.boot_resync(manager, state, OrchestratorRegistry())
                assert pool.get("#native") is conn
                assert child.pid == pid and child.proc.poll() is None
                assert not pump.done() and not feed._stopped
                await asyncio.to_thread(child.steer, "after")
                assert "after thinking" in await observer.next_match("after thinking")
                assert "after tool" in await observer.next_match("after tool")
                commands = [
                    json.loads(line)["type"]
                    for line in wire_log.read_text().splitlines()
                ]
                assert commands.count("set_subagent_subscription") == 1
                assert "abort" not in commands
                assert (
                    "#nixpad_mercurator-mercury-stats-both-engines"
                    not in daemon.channel_names()
                )
                assert all("deleg_97e27028" not in nick for nick in daemon._clients)
                print(
                    f"loopback pid={pid} unchanged; before/after thinking+tool visible; "
                    "one feed subscription; no abort; stale terminal room absent"
                )
            finally:
                await observer.close()
                for channel in ("#root", "#owned", "#native"):
                    await identity.drop_identity(channel)
                await adapter.disconnect()
    finally:
        if pump is not None:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        await feed.stop()
        await asyncio.to_thread(child.stop)
        state.close()
        rooms.set_bot_sink(previous_sink)
        rooms.set_event_loop(previous_loop)


@pytest.mark.asyncio
async def test_real_boot_terminal_marker_visible_before_room_expiry(
    tmp_path, monkeypatch
):
    from tests.observatory.test_ircd import RawClient, running_daemon
    from tests.observatory.test_mirc_adapter_liveness import make_adapter

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    pool = identity.IdentityPool()
    monkeypatch.setattr(identity, "_pool", pool)
    state = ObservatoryState(tmp_path / "state.db")
    previous_sink = rooms.get_bot_sink()
    previous_loop = rooms._loop_now()
    observer = RawClient()
    channel = "#nixpad_mercurator-mercury-stats-both-engines"
    try:
        async with running_daemon(tmp_path, password="fixture", agent_password="fixture") as (daemon, port, _):
            adapter = make_adapter(port, monkeypatch)
            adapter.agent_password = adapter.oper_password = "fixture"
            monkeypatch.setattr(
                identity, "_endpoint", lambda: ("127.0.0.1", port, "fixture")
            )
            try:
                assert await adapter.connect()
                _seed(state, "root", depth=0, room="#root", parent=None)
                _seed(state, "deleg_97e27028/0", depth=1, room=channel, parent="root")
                ad.record_child_spawn("deleg_97e27028/0", "deleg_97e27028")
                ad.record_child_terminal(
                    "deleg_97e27028/0",
                    "interrupted",
                    error="verified original worker death",
                )
                await observer.connect(port)
                await observer.register("observer", password="fixture")
                await observer.send(f"JOIN {channel}")
                await observer.next_match(" 366 ")
                assert channel not in daemon._clients[adapter.nickname].channels
                report = await platform_hook.boot_resync(
                    RoomManager(state, bot=adapter), state, OrchestratorRegistry()
                )
                marker = await observer.next_match(
                    "subagent '0': interrupted", timeout=1
                )
                assert f"PRIVMSG {channel}" in marker
                assert adapter.nickname in marker
                assert pool.get(channel) is None
                assert channel not in report["joined"]
                assert channel not in daemon.channel_names()
                assert {row["node_id"] for row in state.get_live()} == {"root"}
                print(
                    "terminal marker received over actual IRC before own-room expiry; "
                    "bot initially absent; no stale child identity created"
                )
            finally:
                await observer.close()
                await identity.drop_identity("#root")
                await adapter.disconnect()
    finally:
        state.close()
        rooms.set_bot_sink(previous_sink)
        rooms.set_event_loop(previous_loop)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_join_waits_for_membership_with_blocked_handler_and_coalesces(
    tmp_path, monkeypatch, cancel_waiter
):
    import asyncio
    from tests.observatory.test_ircd import running_daemon
    from tests.observatory.test_mirc_adapter_liveness import make_adapter

    async with running_daemon(tmp_path) as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        joins = []
        original = daemon._cmd_join

        async def blocked_join(client, channel):
            if channel == "#await-receipt":
                joins.append(channel)
                entered.set()
                await release.wait()
            await original(client, channel)

        monkeypatch.setattr(daemon, "_cmd_join", blocked_join)
        tasks = []
        try:
            assert await adapter.connect()
            adapter._handler_task.cancel()
            await asyncio.gather(adapter._handler_task, return_exceptions=True)
            tasks = [
                asyncio.create_task(adapter.join_channel("#await-receipt"))
                for _ in range(2)
            ]
            await asyncio.wait_for(entered.wait(), 1)
            assert not any(task.done() for task in tasks)
            if cancel_waiter:
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
            release.set()
            remaining = tasks[1:] if cancel_waiter else tasks
            assert await asyncio.wait_for(asyncio.gather(*remaining), 1) == [
                True
            ] * len(remaining)
            assert "testbot" in daemon._channels["#await-receipt"]
            assert joins == ["#await-receipt"]
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "expired", "timeout", "disconnect"])
async def test_join_never_reports_success_without_membership(
    tmp_path, monkeypatch, failure
):
    import asyncio
    from plugins.platforms.mirc import adapter as adapter_mod
    from tests.observatory.test_ircd import running_daemon
    from tests.observatory.test_mirc_adapter_liveness import make_adapter

    monkeypatch.setattr(adapter_mod, "ROOM_CONTROL_TIMEOUT", 0.2)
    async with running_daemon(tmp_path) as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        entered = asyncio.Event()
        original = daemon._cmd_join

        async def failed_join(client, channel):
            if channel != "#no-membership":
                return await original(client, channel)
            entered.set()
            if failure == "rejected":
                await daemon._numeric(client, 475, channel, "Bad channel key")
            elif failure == "expired":
                await daemon._send(
                    client, f":{client.nick}!relay@mercury PART {channel} :room expired"
                )

        monkeypatch.setattr(daemon, "_cmd_join", failed_join)
        task = None
        try:
            assert await adapter.connect()
            task = asyncio.create_task(adapter.join_channel("#no-membership"))
            await asyncio.wait_for(entered.wait(), 1)
            if failure == "disconnect":
                await adapter.disconnect()
            assert await asyncio.wait_for(task, 1) is False
            peer = daemon._clients.get("testbot")
            assert peer is None or "#no-membership" not in peer.channels
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_disconnected_join_keeps_intent_without_claiming_membership(monkeypatch):
    from tests.observatory.test_mirc_adapter_liveness import make_adapter

    adapter = make_adapter(1, monkeypatch)
    assert not await adapter.join_channel("#offline")
    assert "#offline" in adapter.extra_channels


@pytest.mark.asyncio
async def test_identity_creation_announces_transport_not_execution(
    tmp_path, monkeypatch
):
    from tests.observatory.test_ircd import RawClient, running_daemon

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(identity, "_pool", identity.IdentityPool())
    observer = RawClient()
    try:
        async with running_daemon(tmp_path, password="fixture") as (_, port, _):
            monkeypatch.setattr(
                identity, "_endpoint", lambda: ("127.0.0.1", port, "fixture")
            )
            await observer.connect(port)
            await observer.register("observer", password="fixture")
            await observer.send("JOIN #transport-only")
            await observer.next_match(" 366 ")
            assert await identity.ensure_identity("transport-only", "#transport-only")
            marker = await observer.next_match("PRIVMSG #transport-only")
            assert marker.endswith(":transport-only transport connected.")
            assert (
                "online" not in marker
                and "ready" not in marker
                and "running" not in marker
            )
    finally:
        await observer.close()
        await identity.drop_identity("#transport-only")
