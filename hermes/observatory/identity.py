"""Per-agent IRC identities: ``vm_charlie`` speaks as ``vm_charlie``.

The gateway bot connection (``<server>_gateway``) cannot speak as
another nick, so every message in an agent room arrives stamped with
the wrong identity. Each spawned agent gets a lightweight SEND-ONLY
connection under its own nick on the agent listener.

Lazy + self-healing: connect on first send, reconnect once per send
on failure, drop on ``/exit``, rebuild on gateway resync. Incoming
traffic is ignored (the main bot owns room dispatch for every room);
an opportunistic drain keeps kernel buffers from filling.

Never raises out of the public functions (best-effort by design —
the main bot always remains the fallback sender).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)


class IdentityConn:
    """One send-only agent connection (owns its reader/writer/lock)."""

    def __init__(self, *, host: str, port: int, password: str,
                 nick: str, channel: str) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.nick = nick
        self.channel = channel
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self.last_ok = 0.0

    async def _connect(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=15.0)
        except Exception:
            logger.debug("identity: connect failed for %s", self.nick)
            return False
        self._reader, self._writer = reader, writer
        try:
            import socket as _socket

            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

        async def send_raw(line: str) -> None:
            assert self._writer is not None
            self._writer.write((line + "\r\n").encode("utf-8", "replace"))
            await self._writer.drain()

        try:
            if self.password:
                await send_raw(f"PASS {self.password}")
            await send_raw(f"NICK {self.nick}")
            await send_raw(f"USER {self.nick} 0 * :Mercury")
            async with asyncio.timeout(15):
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("eof before welcome")
                    if b" 001 " in raw:
                        break
            await send_raw(f"JOIN {self.channel}")
        except Exception:
            logger.debug("identity: register failed for %s", self.nick,
                         exc_info=True)
            try:
                writer.close()
            except Exception:
                pass
            self._reader, self._writer = None, None
            return False
        return True

    async def _drain(self) -> None:
        for _ in range(5):
            try:
                assert self._reader is not None
                data = await asyncio.wait_for(self._reader.read(4096), 0.01)
                if not data:
                    break
            except (asyncio.TimeoutError, AssertionError):
                break
            except Exception:
                break

    async def send(self, text: str) -> bool:
        """Send one message, connecting (or reconnecting) as needed."""
        async with self._lock:
            for attempt in (0, 1):
                if self._writer is None or self._writer.is_closing():
                    if not await self._connect():
                        return False
                try:
                    await self._drain()
                    assert self._writer is not None
                    self._writer.write(
                        f"PRIVMSG {self.channel} :{text}\r\n"
                        .encode("utf-8", "replace"))
                    await self._writer.drain()
                    self.last_ok = time.time()
                    return True
                except Exception:
                    logger.debug("identity: send failed for %s (try %d)",
                                 self.nick, attempt)
                    try:
                        if self._writer is not None:
                            self._writer.close()
                    except Exception:
                        pass
                    self._reader, self._writer = None, None
            return False

    async def close(self) -> None:
        async with self._lock:
            try:
                if self._writer is not None:
                    self._writer.write(b"QUIT :identity drop\r\n")
                    await self._writer.drain()
                    self._writer.close()
            except Exception:
                pass
            self._reader, self._writer = None, None


class IdentityPool:
    """Live identity connections keyed by folded channel."""

    def __init__(self) -> None:
        self._conns: dict[str, IdentityConn] = {}
        self._lock = threading.Lock()

    def get(self, channel: str) -> IdentityConn | None:
        with self._lock:
            return self._conns.get(channel.lower())

    def track(self, conn: IdentityConn) -> None:
        with self._lock:
            self._conns[conn.channel.lower()] = conn

    def drop(self, channel: str) -> IdentityConn | None:
        with self._lock:
            return self._conns.pop(channel.lower(), None)

    def nicks(self) -> set[str]:
        """Lowered nicks of every tracked identity (never raises)."""
        with self._lock:
            return {c.nick.lower() for c in self._conns.values() if c.nick}


_pool_lock = threading.Lock()
_pool: IdentityPool | None = None


def get_pool() -> IdentityPool:
    """Process-wide pool (created on demand)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = IdentityPool()
        return _pool


def _endpoint() -> tuple[str, int, str] | None:
    """Agent-listener (host, port, password) from file-backed config."""
    try:
        from mercury_cli.config import get_env_value
    except Exception:
        return None
    host = (get_env_value("IRC_SERVER") or "").strip() or "127.0.0.1"
    try:
        port = int((get_env_value("IRC_PORT") or "").strip() or 6669)
    except ValueError:
        port = 6669
    password = (get_env_value("IRC_SERVER_PASSWORD") or "").strip()
    if not password:
        return None
    return host, port, password


async def ensure_identity(nick: str, channel: str) -> bool:
    """Create (and connect) an identity; True when ready to send."""
    ep = _endpoint()
    if not ep:
        return False
    host, port, password = ep
    pool = get_pool()
    if pool.get(channel) is not None:
        return True
    conn = IdentityConn(host=host, port=port, password=password,
                        nick=nick, channel=channel)
    pool.track(conn)
    ok = await conn.send(f"{nick} online.")
    if not ok:
        pool.drop(channel)
    return ok


async def send_as_identity(channel: str, text: str) -> bool:
    """Send via the room's identity; False when none exists (caller falls
    back to the main bot). Never raises."""
    try:
        conn = get_pool().get(channel)
        if conn is None:
            return False
        return await conn.send(text)
    except Exception:
        logger.debug("identity: send_as failed for %s", channel, exc_info=True)
        return False


async def drop_identity(channel: str) -> bool:
    """Forget (and close) a room's identity. Never raises."""
    try:
        conn = get_pool().drop(channel)
        if conn is None:
            return False
        await conn.close()
        return True
    except Exception:
        logger.debug("identity: drop failed for %s", channel, exc_info=True)
        return False
