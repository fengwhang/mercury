"""Exercise identity liveness against Mercury's real IRC daemon."""
import asyncio
import time

import pytest

from observatory.identity import IdentityConn
from tests.observatory.test_identity import RawClient, running_daemon


@pytest.mark.asyncio
async def test_eof_cannot_report_success_without_reconnect(monkeypatch):
    conn = IdentityConn(host="unused", port=1, password="test",
                        nick="agent", channel="#agent")
    reader = asyncio.StreamReader()
    reader.feed_eof()

    class Writer:
        writes = []

        def is_closing(self):
            return False

        def write(self, data):
            self.writes.append(data)

        async def drain(self):
            pass  # A successful drain does not prove peer liveness.

        def close(self):
            pass

    writer = Writer()
    conn._reader, conn._writer = reader, writer

    async def reconnect():
        return False

    monkeypatch.setattr(conn, "_connect", reconnect)
    assert not await conn.send("must not vanish")
    assert not writer.writes


@pytest.mark.asyncio
async def test_gateway_identity_reuses_dispatch_connection(monkeypatch):
    from types import SimpleNamespace
    from observatory import identity, rooms

    pool = identity.IdentityPool()
    monkeypatch.setattr(identity, "_pool", pool)
    monkeypatch.setattr(rooms, "_current_sink", SimpleNamespace(nickname="vm_gateway"))
    assert await identity.ensure_identity("VM_GATEWAY", "#vm_gateway")
    assert pool.get("#vm_gateway") is None


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_idle_identity_answers_server_ping(tmp_path):
    async with running_daemon(tmp_path, password="test") as (daemon, port):
        conn = IdentityConn(host="127.0.0.1", port=port, password="test",
                            nick="agent", channel="#agent")
        try:
            assert await conn.send("online")
            await eventually(lambda: any(c.nick == "agent" for c in daemon._clients.values()))
            peer = next(c for c in daemon._clients.values() if c.nick == "agent")
            await asyncio.sleep(0.02)
            peer.last_in = time.monotonic() - 61
            await daemon._ping_sweep()
            await eventually(lambda: not peer.ping_out)
            assert time.monotonic() - peer.last_in < 2
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_first_reply_after_remote_eof_is_delivered(tmp_path):
    async with running_daemon(tmp_path, password="test") as (daemon, port):
        conn = IdentityConn(host="127.0.0.1", port=port, password="test",
                            nick="agent", channel="#agent")
        watcher = RawClient()
        try:
            assert await conn.send("online")
            await watcher.connect(port)
            for line in ("PASS test", "NICK watcher", "USER watcher 0 * :test"):
                await watcher.send(line)
            await watcher.next_match(" 001 ")
            await watcher.send("JOIN #agent")
            await watcher.next_match("JOIN #agent")
            peer = next(c for c in daemon._clients.values() if c.nick == "agent")
            peer.writer.close()
            # Wait for remote EOF, not local writer.close(): the old test
            # manually cleared the handle and missed false-positive sends.
            reader = conn._reader
            await eventually(reader.at_eof)
            await asyncio.sleep(0.02)
            assert await conn.send_batch(["first reply", "second line"])
            assert "first reply" in await watcher.next_match("first reply", timeout=1)
            assert "second line" in await watcher.next_match("second line", timeout=1)
        finally:
            await watcher.close()
            await conn.close()
