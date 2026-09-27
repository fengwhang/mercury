"""draft/multiline batches: one logical message, zero quiet-window wait.

The 250ms ingress timer is a fallback for dumb clients. Between
batch-capable peers (our adapter, our Lounge fork) a multiline message
travels as one BATCH with explicit boundaries — no waiting, no guessing.
"""

from __future__ import annotations

import asyncio
import types
from contextlib import asynccontextmanager

import pytest

from observatory.ircd import DaemonConfig, IrcDaemon


@asynccontextmanager
async def running_daemon(tmp_path, **kwargs):
    config = DaemonConfig(
        agent_port=0, server_port=0, state_dir=str(tmp_path), **kwargs
    )
    d = IrcDaemon(config)
    await d.start()
    try:
        yield d, d._servers[0].sockets[0].getsockname()[1]
    finally:
        await d.stop()


def _config(port, **kw):
    extra = {
        "server": "127.0.0.1",
        "port": port,
        "nickname": "mercury_gateway",
        "channel": "#mercury_gateway",
        "use_tls": False,
    }
    extra.update(kw)
    return types.SimpleNamespace(extra=extra)


class _Listener:
    """Raw multiline-capable listener: negotiates the cap, records lines."""

    def __init__(self) -> None:
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self, port: int, nick: str) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", port)
        self._task = asyncio.create_task(self._pump())
        self.send(f"CAP REQ :draft/multiline")
        self.send(f"NICK {nick}")
        self.send(f"USER {nick} 0 * :t")
        await self.expect(" 001 ")

    def send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write((line + "\r\n").encode())

    async def _pump(self) -> None:
        buf = b""
        try:
            while not self.reader.at_eof():  # type: ignore[union-attr]
                data = await self.reader.read(4096)  # type: ignore[union-attr]
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    await self.lines.put(
                        raw.decode("utf-8", errors="replace").rstrip("\r"))
        except asyncio.CancelledError:
            pass

    async def expect(self, fragment: str, timeout: float = 5.0) -> str:
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
async def test_adapter_sends_multiline_batch(tmp_path) -> None:
    """Multi-chunk content goes out as one BATCH, not N dribbles."""
    from plugins.platforms.irc.adapter import IRCAdapter

    async with running_daemon(tmp_path) as (d, port):
        listener = _Listener()
        await listener.connect(port, "watcher")
        adapter = IRCAdapter(_config(port))
        assert await adapter.connect()
        try:
            listener.send("JOIN #mercury_gateway")
            await listener.expect("JOIN #mercury_gateway")
            long_one = "a" * 300
            long_two = "b" * 300
            assert await adapter.say(
                "#mercury_gateway", f"{long_one}\n\n{long_two}")
            open_frame = await listener.expect("BATCH +")
            assert "draft/multiline #mercury_gateway" in open_frame
            first = await listener.expect(long_one[:40])
            assert "batch=" in first
            second = await listener.expect(long_two[:40])
            assert "batch=" in second
            await listener.expect("BATCH -")
        finally:
            await adapter.disconnect()
            await listener.close()


@pytest.mark.asyncio
async def test_adapter_negotiates_multiline_cap(tmp_path) -> None:
    """connect() learns draft/multiline from the server."""
    from plugins.platforms.irc.adapter import IRCAdapter

    async with running_daemon(tmp_path) as (d, port):
        adapter = IRCAdapter(_config(port))
        assert await adapter.connect()
        try:
            assert adapter._server_multiline is True
        finally:
            await adapter.disconnect()

@pytest.mark.asyncio
async def test_adapter_reassembles_inbound_batch(monkeypatch) -> None:
    """Tagged lines + close reassemble into ONE dispatch, no waiting."""
    from gateway.config import PlatformConfig
    from plugins.platforms.irc import adapter as adapter_mod

    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL",
                "IRC_USE_TLS", "IRC_MANAGED_BY"):
        monkeypatch.delenv(key, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "127.0.0.1", "port": 6669,
               "nickname": "nixpi4b_gateway", "channel": "#nixpi4b_gateway"},
    )
    ad = adapter_mod.IRCAdapter(cfg)
    seen: list[dict] = []

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    await ad._handle_line(
        "@batch=r9 :owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :batched one")
    await ad._handle_line(
        "@batch=r9 :owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :batched two")
    await ad._handle_line("BATCH -r9 draft/multiline #nixpi4b_gateway")
    assert len(seen) == 1
    assert seen[0]["text"] == "batched one\nbatched two"
