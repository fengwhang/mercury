"""Real MIRC connections stay healthy under output and recover after drops."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.mirc import adapter as adapter_mod
from tests.observatory.test_ircd import RawClient, running_daemon


def make_adapter(port, monkeypatch):
    adapter = adapter_mod.MIRCAdapter(PlatformConfig(enabled=True, extra={
        "server": "127.0.0.1", "port": port, "nickname": "testbot",
        "channel": "#test", "use_tls": False,
    }))
    monkeypatch.setattr(adapter, "_resync_observatory", AsyncMock())
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", lambda _ctx: None)
    return adapter


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_successful_connect_reports_eof_and_reconnect_restores_messages(tmp_path, monkeypatch):
    async with running_daemon(tmp_path) as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        recovered = asyncio.Event()
        failures = []

        async def recover(failed):
            failures.append((failed.fatal_error_code, failed.fatal_error_retryable))
            await failed.disconnect()
            assert await failed.connect(is_reconnect=True)
            recovered.set()

        adapter.set_fatal_error_handler(recover)
        messages = []

        async def message(event):
            messages.append(event.text)

        adapter.set_message_handler(message)
        owner = RawClient()
        try:
            assert await adapter.connect()
            assert adapter.is_connected
            await until(lambda: "testbot" in daemon._clients)
            daemon._clients["testbot"].writer.close()
            await asyncio.wait_for(recovered.wait(), 5)
            assert failures == [("connection_lost", True)]
            assert adapter.is_connected
            assert not adapter.has_fatal_error
            await owner.connect(port)
            await owner.register("owner")
            await owner.send("JOIN #test")
            await owner.next_match(" 366 ")
            await owner.send("PRIVMSG #test :after reconnect")
            await until(lambda: "after reconnect" in messages)
        finally:
            await owner.close()
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_busy_output_survives_multiple_inbound_silence_periods(tmp_path, monkeypatch):
    from observatory import mirc

    monkeypatch.setattr(mirc, "PING_INTERVAL", 60)
    monkeypatch.setattr(adapter_mod, "SILENCE_LIMIT", 0.08)
    monkeypatch.setattr(adapter_mod, "WATCHDOG_POLL", 0.02)
    async with running_daemon(tmp_path) as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        failed = AsyncMock()
        adapter.set_fatal_error_handler(failed)
        try:
            assert await adapter.connect()
            await until(lambda: adapter._line_queue.empty())
            # Simulate a handler waiting on a long agent turn. Protocol
            # responses must prove liveness without waiting for this handler.
            adapter._handler_task.cancel()
            await asyncio.gather(adapter._handler_task, return_exceptions=True)
            for _ in range(30):
                await adapter._send_raw("PRIVMSG #test :working")
                await asyncio.sleep(0.02)
            assert adapter.is_connected
            assert "testbot" in daemon._clients
            assert adapter._last_inbound > 0
            failed.assert_not_awaited()
            assert not adapter._watchdog_task.done()
        finally:
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_failed_liveness_probe_updates_status_and_requests_recovery(tmp_path, monkeypatch):
    from gateway import status

    monkeypatch.setattr(adapter_mod, "SILENCE_LIMIT", 0.08)
    monkeypatch.setattr(adapter_mod, "WATCHDOG_POLL", 0.02)
    monkeypatch.setattr(adapter_mod, "WATCHDOG_PROBE_TIMEOUT", 0.08)
    async with running_daemon(tmp_path) as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        failed = AsyncMock()
        adapter.set_fatal_error_handler(failed)
        try:
            assert await adapter.connect()
            original = daemon._line

            async def ignore_probe(client, line, listener, password):
                if line.startswith("PING "):
                    return
                await original(client, line, listener, password)

            monkeypatch.setattr(daemon, "_line", ignore_probe)
            await until(lambda: failed.await_count > 0)
            assert not adapter.is_connected
            assert adapter.fatal_error_code == "connection_lost"
            assert adapter.fatal_error_retryable
            assert adapter._writer is None
            snapshot = status.read_runtime_status()
            assert snapshot["platforms"]["irc"]["state"] == "fatal"
            failed.assert_awaited_once_with(adapter)
        finally:
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_spawnomp_startup_keeps_mirc_heartbeat_responsive(tmp_path, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace

    from gateway.run import GatewayRunner
    from observatory import rooms, spawn
    from observatory.state import ObservatoryState

    entered = threading.Event()
    release = threading.Event()
    state = ObservatoryState(tmp_path / "state.db")
    registry = spawn.OrchestratorRegistry()
    child = SimpleNamespace(model="test-model", stop=lambda: None)

    def build(**kwargs):
        entered.set()
        assert release.wait(5), "test did not release child startup"
        return child

    monkeypatch.setattr(spawn, "build_omp_child", build)
    monkeypatch.setattr(spawn, "omp_session_file", lambda _child: str(tmp_path / "observatory/omp-sessions/session.jsonl"))
    async with running_daemon(tmp_path) as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        runner = object.__new__(GatewayRunner)
        runner._observatory_handles = lambda: ((state, registry), "")
        runner._observatory_gateway_channel = lambda _state: "#test"
        runner._observatory_caller_channel = lambda event: event.source.chat_id
        runner._observatory_live_channels = lambda: {"#test"}
        runner._observatory_mercury_home = lambda: str(tmp_path)
        replies = []

        async def handle(event):
            replies.append(await runner._handle_spawnomp_command(event))

        adapter.set_message_handler(handle)
        owner = RawClient()
        previous_sink = rooms.get_bot_sink()
        try:
            assert await adapter.connect()
            monkeypatch.setattr(spawn, "get_bot_sink", lambda: adapter)
            await owner.connect(port)
            await owner.register("owner")
            await owner.send("JOIN #test")
            await owner.next_match(" 366 ")
            await owner.send("PRIVMSG #test :!spawnomp important")
            await until(entered.is_set)
            peer = daemon._clients["testbot"]
            peer.last_in = time.monotonic() - 61
            await daemon._ping_sweep()
            await until(lambda: not peer.ping_out)
            assert not release.is_set()
            assert adapter.is_connected
            release.set()
            await until(lambda: bool(replies))
            assert "spawned omp" in replies[0]
            assert len(state.get_live()) == 1
            assert state.get_live()[0]["status"] == "live"
            assert registry.get(state.get_live()[0]["node_id"]).rpc is child
        finally:
            release.set()
            for row in state.get_live():
                rooms.drop_omp_room(row["node_id"])
            await owner.close()
            await adapter.disconnect()
            rooms.set_bot_sink(previous_sink)
            state.close()
