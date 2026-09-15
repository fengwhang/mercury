"""Per-agent identity connections (vm_charlie speaks as vm_charlie)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from observatory import identity as identity_mod
from observatory.ircd import DaemonConfig, IrcDaemon


@asynccontextmanager
async def running_daemon(tmp_path, **kwargs):
    config = DaemonConfig(
        agent_port=0, bouncer_port=0, state_dir=str(tmp_path), **kwargs)
    d = IrcDaemon(config)
    await d.start()
    try:
        yield d, d._servers[0].sockets[0].getsockname()[1]
    finally:
        await d.stop()


class RawClient:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.reader = None
        self.writer = None
        self._task = None

    async def connect(self, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        buf = b""
        try:
            while not self.reader.at_eof():
                data = await self.reader.read(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    await self.lines.put(raw.decode("utf-8", errors="replace").rstrip("\r"))
        except asyncio.CancelledError:
            pass

    async def send(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()

    async def next_match(self, fragment: str, timeout: float = 5.0) -> str:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = end - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out waiting for {fragment!r}"
            line = await asyncio.wait_for(self.lines.get(), timeout=remaining)
            if fragment in line:
                return line

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_identity_speaks_as_own_nick(tmp_path) -> None:
    """Messages via the identity arrive stamped with the agent nick."""
    async with running_daemon(tmp_path, password="s3cret") as (d, port):
        conn = identity_mod.IdentityConn(
            host="127.0.0.1", port=port, password="s3cret",
            nick="vm_charlie", channel="#vm_charlie")
        assert await conn.send("hello room") is True
        # A second client in the room sees vm_charlie, not the gateway nick.
        c = RawClient()
        await c.connect(port)
        try:
            await c.send("PASS s3cret")
            await c.send("NICK watcher")
            await c.send("USER watcher 0 * :t")
            await c.next_match(" 001 ")
            await c.send("JOIN #vm_charlie")
            assert await c.next_match("JOIN #vm_charlie")
            await conn.send("second line")
            got = await c.next_match("second line")
            assert got.startswith(":vm_charlie!")
        finally:
            await c.close()
            await conn.close()


@pytest.mark.asyncio
async def test_identity_reconnects_after_drop(tmp_path) -> None:
    """A dead socket heals on the next send (lazy reconnect)."""
    async with running_daemon(tmp_path, password="s3cret") as (d, port):
        conn = identity_mod.IdentityConn(
            host="127.0.0.1", port=port, password="s3cret",
            nick="vm_re", channel="#vm_re")
        assert await conn.send("one") is True
        assert conn._writer is not None
        conn._writer.close()
        conn._reader, conn._writer = None, None
        assert await conn.send("two") is True
        await conn.close()


@pytest.mark.asyncio
async def test_pool_send_prefers_identity(monkeypatch) -> None:
    """send_as_identity hits the pool; missing rooms report False."""
    pool = identity_mod.IdentityPool()
    monkeypatch.setattr(identity_mod, "_pool", pool)

    class FakeConn:
        def __init__(self):
            self.sent: list[str] = []

        async def send(self, text: str) -> bool:
            self.sent.append(text)
            return True

    fake = FakeConn()
    conn = identity_mod.IdentityConn(
        host="x", port=1, password="p", nick="n", channel="#vm_x")
    pool.track(conn)
    async def _fake_send(text: str) -> bool:
        fake.sent.append(text)
        return True
    conn.send = _fake_send  # type: ignore[method-assign]
    assert await identity_mod.send_as_identity("#vm_x", "hi") is True
    assert fake.sent == ["hi"]
    assert await identity_mod.send_as_identity("#vm_missing", "hi") is False


@pytest.mark.asyncio
async def test_ensure_identity_end_to_end(tmp_path, monkeypatch) -> None:
    """ensure_identity resolves env, connects, and tracks the pool."""
    async with running_daemon(tmp_path, password="s3cret") as (d, port):
        monkeypatch.setenv("IRC_SERVER", "127.0.0.1")
        monkeypatch.setenv("IRC_PORT", str(port))
        monkeypatch.setenv("IRC_SERVER_PASSWORD", "s3cret")
        pool = identity_mod.IdentityPool()
        monkeypatch.setattr(identity_mod, "_pool", pool)
        assert await identity_mod.ensure_identity("vm_e2e", "#vm_e2e") is True
        assert pool.get("#vm_e2e") is not None
        assert await identity_mod.send_as_identity("#vm_e2e", "yo") is True
        assert await identity_mod.drop_identity("#vm_e2e") is True
        assert pool.get("#vm_e2e") is None


def test_pool_nicks_lists_tracked_identities() -> None:
    from types import SimpleNamespace

    pool = identity_mod.IdentityPool()
    assert pool.nicks() == set()
    pool.track(SimpleNamespace(channel="#vm_alpha", nick="vm_alpha"))
    pool.track(SimpleNamespace(channel="#vm_beta", nick="VM_Beta"))
    assert pool.nicks() == {"vm_alpha", "vm_beta"}
    pool.drop("#vm_alpha")
    assert pool.nicks() == {"vm_beta"}
