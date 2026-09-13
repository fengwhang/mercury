"""IRC daemon tests: join/msg fanout, bouncer replay, destroy, auth."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from observatory.ircd import DaemonConfig, IrcDaemon, clean_channel, clean_nick


def test_clean_channel() -> None:
    assert clean_channel("parent-child") == "#parent-child"
    assert clean_channel("My Agent!") == "#my-agent"
    assert clean_channel("") == "#unnamed"


def test_clean_nick() -> None:
    assert clean_nick("my gateway") == "my_gateway"
    assert clean_nick("") == "agent"


class RawClient:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self._task = asyncio.create_task(self._pump())

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
                        raw.decode("utf-8", errors="replace").rstrip("\r")
                    )
        except asyncio.CancelledError:
            pass

    async def send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()

    async def register(self, nick: str, password: str = "") -> None:
        if password:
            await self.send(f"PASS {password}")
        await self.send(f"NICK {nick}")
        await self.send(f"USER {nick} 0 * :test")
        for _ in range(100):
            line = await asyncio.wait_for(self.lines.get(), timeout=5)
            if " 001 " in line:
                return
        raise AssertionError("no welcome")

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


@asynccontextmanager
async def running_daemon(tmp_path, **kwargs):
    """Start an IrcDaemon on ephemeral ports (fixtures stay sync per convention)."""
    config = DaemonConfig(
        agent_port=0, bouncer_port=0, state_dir=str(tmp_path), **kwargs
    )
    d = IrcDaemon(config)
    await d.start()
    agent_port = d._servers[0].sockets[0].getsockname()[1]
    bouncer_port = d._servers[1].sockets[0].getsockname()[1]
    try:
        yield d, agent_port, bouncer_port
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_join_and_privmsg_fanout(tmp_path) -> None:
    async with running_daemon(tmp_path) as (d, agent_port, _):
        a, b = RawClient(), RawClient()
        await a.connect(agent_port)
        await b.connect(agent_port)
        try:
            await a.register("alice")
            await b.register("bob")
            await a.send("JOIN #parent-child")
            await a.next_match("JOIN #parent-child")
            await b.send("JOIN #parent-child")
            await b.next_match("JOIN #parent-child")
            await a.send("PRIVMSG #parent-child :hello bob")
            got = await b.next_match("PRIVMSG #parent-child :hello bob")
            assert "alice" in got
            assert [m.text for m in d.channel_history("#parent-child")] == ["hello bob"]
        finally:
            await a.close()
            await b.close()


@pytest.mark.asyncio
async def test_bouncer_replay_on_join(tmp_path) -> None:
    async with running_daemon(tmp_path) as (_, agent_port, bouncer_port):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("bot")
            await a.send("JOIN #replay")
            await a.next_match("JOIN #replay")
            await a.send("PRIVMSG #replay :first")
            await a.send("PRIVMSG #replay :second")
            await asyncio.sleep(0.2)
        finally:
            await a.close()
        u = RawClient()
        await u.connect(bouncer_port)
        try:
            await u.register("user")
            await u.send("JOIN #replay")
            await u.next_match("JOIN #replay")
            got1 = await u.next_match(":first")
            got2 = await u.next_match(":second")
            assert "PRIVMSG #replay" in got1 and "PRIVMSG #replay" in got2
        finally:
            await u.close()


@pytest.mark.asyncio
async def test_destroy_channel_parts_members(tmp_path) -> None:
    async with running_daemon(tmp_path) as (d, agent_port, _):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("alice")
            await a.send("JOIN #doomed")
            await a.next_match("JOIN #doomed")
            removed = await d.destroy_channel("#doomed")
            assert removed == 1
            assert await a.next_match("PART #doomed")
            assert d.channel_names() == []
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_bouncer_password_enforced(tmp_path) -> None:
    async with running_daemon(tmp_path, password="s3cret") as (_, __, bouncer_port):
        u = RawClient()
        await u.connect(bouncer_port)
        try:
            await u.send("NICK user")
            await u.send("USER user 0 * :test")
            assert await u.next_match("464")
        finally:
            await u.close()
        v = RawClient()
        await v.connect(bouncer_port)
        try:
            await v.register("user2", password="s3cret")
        finally:
            await v.close()


@pytest.mark.asyncio
async def test_history_survives_restart(tmp_path) -> None:
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("alice")
            await a.send("JOIN #persist")
            await a.next_match("JOIN #persist")
            await a.send("PRIVMSG #persist :remember me")
            await asyncio.sleep(0.3)
        finally:
            await a.close()
    async with running_daemon(tmp_path) as (d2, _, __):
        assert [m.text for m in d2.channel_history("#persist")] == ["remember me"]


@pytest.mark.asyncio
async def test_oper_destroy_kills_room(tmp_path) -> None:
    async with running_daemon(tmp_path, agent_password="op-secret") as (
        d,
        agent_port,
        _,
    ):
        bot = RawClient()
        await bot.connect(agent_port)
        mem = RawClient()
        await mem.connect(agent_port)
        try:
            await bot.register("bot", password="op-secret")
            await mem.register("mem", password="op-secret")
            await bot.send("JOIN #doomed")
            await bot.next_match("JOIN #doomed")
            await mem.send("JOIN #doomed")
            await mem.next_match("JOIN #doomed")
            # unprivileged destroy is refused
            await mem.send("DESTROY #doomed")
            await mem.next_match("481")
            assert "#doomed" in d.channel_names()
            # oper destroy PARTs members and drops history
            await bot.send("OPER op-secret")
            await bot.next_match("381")
            await bot.send("DESTROY #doomed")
            await mem.next_match("PART #doomed")
            assert d.channel_names() == []
            assert d.channel_history("#doomed") == []
        finally:
            await bot.close()
            await mem.close()
