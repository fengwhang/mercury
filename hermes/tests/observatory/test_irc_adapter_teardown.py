"""MIRC adapter drop teardown + bounded connect sends (reconnect hygiene)."""

from __future__ import annotations

import asyncio

import pytest


def _mirc_adapter():
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import PlatformConfig
    from plugins.platforms.mirc.adapter import MIRCAdapter

    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "localhost", "port": 6667,
               "nickname": "testbot", "channel": "#test",
               "use_tls": False},
    )
    adapter = MIRCAdapter(cfg)
    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    adapter._writer = writer
    adapter._line_queue = asyncio.Queue()
    return adapter


def test_drop_teardown_cleans_stale_state(monkeypatch) -> None:
    """A dropped connection leaves no CLOSE-WAIT writer, stale sink,
    set registration, or held identity lock behind."""
    import observatory.rooms as rooms_mod
    from gateway import status as status_mod

    adapter = _mirc_adapter()
    closed: list[bool] = []
    adapter._writer.close = lambda: closed.append(True)  # type: ignore[method-assign]
    adapter._registered = True
    adapter._lock_key = "localhost:6669"
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        status_mod, "release_scoped_lock",
        lambda scope, key: released.append((scope, key)))
    prev_sink = rooms_mod.get_bot_sink()
    rooms_mod.set_bot_sink(adapter)
    try:
        adapter._drop_teardown()
        assert closed == [True]
        assert adapter._writer is None
        assert adapter._registered is False
        assert rooms_mod.get_bot_sink() is None
        assert released == [("irc", "localhost:6669")]
    finally:
        rooms_mod.set_bot_sink(prev_sink)


@pytest.mark.asyncio
async def test_remote_eof_stops_handler_and_reports_retryable_drop(monkeypatch):
    from unittest.mock import AsyncMock

    adapter = _mirc_adapter()
    adapter._reader = asyncio.StreamReader()
    adapter._reader.feed_eof()
    adapter._mark_connected()
    notify = AsyncMock()
    monkeypatch.setattr(adapter, "_notify_fatal_error", notify)
    await adapter._receive_loop()
    assert adapter._writer is None
    assert await asyncio.wait_for(adapter._line_queue.get(), 0.1) is None
    notify.assert_awaited_once()
    assert not adapter.is_connected


@pytest.mark.asyncio
async def test_stale_receiver_cannot_stop_new_handler(monkeypatch):
    from unittest.mock import AsyncMock

    adapter = _mirc_adapter()
    adapter._conn_generation = 1
    adapter._mark_connected()
    writer = adapter._writer

    class OldReader:
        def at_eof(self):
            return False

        async def read(self, size):
            adapter._conn_generation = 2
            return b""

    adapter._reader = OldReader()
    notify = AsyncMock()
    monkeypatch.setattr(adapter, "_notify_fatal_error", notify)
    await adapter._receive_loop()
    assert adapter._line_queue.empty()
    assert adapter._writer is writer
    assert adapter.is_connected
    notify.assert_not_awaited()


def test_drop_teardown_never_raises() -> None:
    """Empty adapter (no writer, sink, or lock) is a safe no-op."""
    adapter = _mirc_adapter()
    adapter._writer = None
    adapter._drop_teardown()


@pytest.mark.asyncio
async def test_send_raw_timeout_bounds_hung_drain() -> None:
    """A half-open socket fails loud instead of stalling connect()."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _mirc_adapter()

    async def _hang():
        await asyncio.sleep(30)

    hanging = MagicMock()
    hanging.is_closing = MagicMock(return_value=False)
    hanging.write = MagicMock()
    hanging.drain = _hang
    adapter._writer = hanging
    with pytest.raises(asyncio.TimeoutError):
        await adapter._send_raw("JOIN #test", timeout=0.05)


def test_stale_generation_leaves_newer_connection_alone() -> None:
    """A receive task unwinding after a newer connect bumped the counter
    must not close the new writer, clear the fresh sink, or drop
    registration — same-nick reconnects would otherwise murder each
    other forever."""
    import observatory.rooms as rooms_mod

    adapter = _mirc_adapter()
    closed: list[bool] = []
    adapter._writer.close = lambda: closed.append(True)  # type: ignore[method-assign]
    adapter._registered = True
    adapter._conn_generation = 2
    prev_sink = rooms_mod.get_bot_sink()
    rooms_mod.set_bot_sink(adapter)
    try:
        adapter._drop_teardown(generation=1)
        assert closed == []
        assert adapter._writer is not None
        assert adapter._registered is True
        assert rooms_mod.get_bot_sink() is adapter
    finally:
        rooms_mod.set_bot_sink(prev_sink)


def test_current_generation_acts() -> None:
    """A matching generation performs the full teardown."""
    adapter = _mirc_adapter()
    closed: list[bool] = []
    adapter._writer.close = lambda: closed.append(True)  # type: ignore[method-assign]
    adapter._conn_generation = 2
    adapter._drop_teardown(generation=2)
    assert closed == [True]
    assert adapter._writer is None


@pytest.mark.asyncio
async def test_send_raw_default_path_unchanged() -> None:
    """Without a timeout the drain awaits normally (today's behavior)."""
    from unittest.mock import AsyncMock

    adapter = _mirc_adapter()
    drained: list[bool] = []

    async def _ok():
        drained.append(True)

    adapter._writer.drain = _ok  # type: ignore[method-assign]
    await adapter._send_raw("PING :x")
    assert drained == [True]
