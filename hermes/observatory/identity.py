"""Per-agent MIRC identities: ``vm_charlie`` speaks as ``vm_charlie``.

The gateway bot connection (``<server>_gateway``) cannot speak as
another nick, so every message in an agent room arrives stamped with
the wrong identity. Each spawned agent gets a lightweight SEND-ONLY
connection under its own nick on the agent listener.

Self-healing: connect on first send, retry transport failures while idle,
drop on ``/exit``, rebuild on gateway resync. Incoming
traffic is ignored (the main bot owns room dispatch for every room);
a receive task answers PINGs and invalidates connections on EOF.

Never raises out of the public functions (best-effort by design —
the main bot always remains the fallback sender).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Fixed-width batch refs (``i<ms13>-<seq06>``): the adapter reserves the
# tag overhead before splitting, so the ref length must be constant.
# 7 ("@batch=") + 21 (ref) + 1 (space) = 29.
BATCH_TAG_OVERHEAD = 29
PART_TAG_OVERHEAD = len(";draft/multiline-concat;+mercury/empty=1")

_batch_seq = itertools.count()
SEND_TIMEOUT = 10.0


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
        self._recv_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closed = False
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
            await asyncio.wait_for(self._writer.drain(), SEND_TIMEOUT)

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
            # drain() only queues the JOIN locally. Wait for the server's
            # membership receipt before resync advertises this identity ready.
            async with asyncio.timeout(15):
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("eof before join confirmation")
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("PING "):
                        await send_raw(f"PONG {line[5:]}")
                        continue
                    parts = line.split()
                    if len(parts) >= 4 and parts[3].lower() == self.channel.lower():
                        if parts[1] == "366":
                            break
                        if parts[1] in ("403", "471", "474", "475"):
                            raise ConnectionError("identity JOIN rejected")
        except Exception:
            logger.debug("identity: register failed for %s", self.nick,
                         exc_info=True)
            try:
                writer.close()
            except Exception:
                pass
            self._reader, self._writer = None, None
            return False
        self._recv_task = asyncio.create_task(self._receive(reader, writer))
        return True

    async def _receive(self, reader, writer) -> None:
        """Keep an idle identity alive; never treat a peer's EOF as success."""
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    line = line.partition(" ")[2]
                command, _, payload = line.partition(" ")
                if command.upper() == "PING":
                    async with self._lock:
                        if self._writer is not writer:
                            return
                        writer.write(f"PONG {payload}\r\n".encode("utf-8"))
                        await asyncio.wait_for(writer.drain(), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("identity: receive failed for %s", self.nick, exc_info=True)
        finally:
            writer.close()
            # An old receiver must never clear a replacement connection.
            if self._writer is writer:
                self._reader, self._writer = None, None
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._closed or (self._reconnect_task is not None and not self._reconnect_task.done()):
            return
        self._reconnect_task = asyncio.create_task(self._recover_connection())

    async def _recover_connection(self) -> None:
        """Keep the same identity/session present without requiring new output."""
        delay = 0.5
        while not self._closed:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._closed:
                    return
                if self._writer is not None and not self._writer.is_closing():
                    return
                if await self._connect():
                    return
            delay = min(delay * 2, 10)

    async def _write_lines(self, payloads: list[str]) -> bool:
        """Write raw lines with one reconnect retry. Caller holds no lock."""
        async with self._lock:
            if self._closed:
                return False
            for attempt in (0, 1):
                if self._writer is None or self._writer.is_closing():
                    if not await self._connect():
                        self._schedule_reconnect()
                        return False
                try:
                    if self._reader is not None and self._reader.at_eof():
                        raise ConnectionError("identity peer closed connection")
                    assert self._writer is not None
                    for text in payloads:
                        self._writer.write(
                            f"{text}\r\n".encode("utf-8", "replace"))
                    await asyncio.wait_for(self._writer.drain(), SEND_TIMEOUT)
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
            self._schedule_reconnect()
            return False

    async def send(self, text: str, *, kind: str = "status") -> bool:
        """Send one message, connecting (or reconnecting) as needed."""
        from observatory.message_format import message_tags

        return await self._write_lines([f"{message_tags(kind)}PRIVMSG {self.channel} :{text}"])

    async def send_batch(self, lines: list[str], *, kind: str = "status",
                         concat: list[bool] | None = None) -> bool:
        """Send lines as one draft/multiline batch: open, tagged lines,
        close. The daemon relays to capable peers (membership-gated, no
        sender caps needed); legacy peers get the bare packed lines."""
        if not lines:
            return True
        if len(lines) == 1:
            return await self.send(lines[0], kind=kind)
        ref = f"i{int(time.time() * 1000):013d}-{next(_batch_seq) % 1000000:06d}"
        # Blank chunks ride as one space: the daemon 412s empty text,
        # and the reassembled row keeps the paragraph gap.
        from observatory.message_format import message_tags

        return await self._write_lines(
            [f"BATCH +{ref} draft/multiline {self.channel}"] +
            [f"{message_tags(kind, batch=ref, concat=bool(concat and concat[i]), empty=ln == '')}"
             f"PRIVMSG {self.channel} :{ln or ' '}" for i, ln in enumerate(lines)] +
            [f"BATCH -{ref}"])

    async def close(self) -> None:
        self._closed = True
        reconnect, self._reconnect_task = self._reconnect_task, None
        if reconnect is not None:
            reconnect.cancel()
            await asyncio.gather(reconnect, return_exceptions=True)
        task, self._recv_task = self._recv_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            try:
                if self._writer is not None:
                    self._writer.write(b"QUIT :identity drop\r\n")
                    await asyncio.wait_for(self._writer.drain(), SEND_TIMEOUT)
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
    # This connection dials the agent listener, just like MIRCAdapter.
    # Using the server/client secret fails on split-password installs.
    password = (get_env_value("IRC_AGENT_PASSWORD")
                or get_env_value("IRC_SERVER_PASSWORD") or "").strip()
    if not password:
        return None
    return host, port, password


async def ensure_identity(nick: str, channel: str) -> bool:
    """Create (and connect) an identity; True when ready to send."""
    # Defence in depth for other callers: the gateway's receive connection
    # IS its identity. Mercury's newest-nick-wins server would evict it if
    # we opened a send-only connection under the same name.
    from observatory.rooms import get_bot_sink

    bot = get_bot_sink()
    if bot is not None and nick and nick.lower() in {
        str(getattr(bot, "nickname", "") or "").lower(),
        str(getattr(bot, "_current_nick", "") or "").lower(),
    }:
        return True
    ep = _endpoint()
    if not ep:
        return False
    host, port, password = ep
    pool = get_pool()
    conn = pool.get(channel)
    if conn is not None:
        # A reconnect assigns its writer before registration and JOIN finish.
        # The connection lock covers that handshake; don't report ready early.
        async with conn._lock:
            if conn._closed:
                return False
            if conn._writer is None or conn._writer.is_closing():
                conn._schedule_reconnect()
                return False
            return True
    conn = IdentityConn(host=host, port=port, password=password,
                        nick=nick, channel=channel)
    pool.track(conn)
    ok = await conn.send(f"{nick} online.")
    return ok


async def send_as_identity(channel: str, text: str, *, kind: str = "assistant_reply") -> bool:
    """Send via the room's identity; False when none exists (caller falls
    back to the main bot). Never raises."""
    try:
        conn = get_pool().get(channel)
        if conn is None:
            return False
        return await conn.send(text, kind=kind)
    except Exception:
        logger.debug("identity: send_as failed for %s", channel, exc_info=True)
        return False


async def send_multiline(channel: str, lines: list[str], *, kind: str = "assistant_reply",
                         concat: list[bool] | None = None) -> bool:
    """Send lines as one draft/multiline batch via the room's identity;
    False when none exists (caller falls back to the main bot, which
    batches the same way). Never raises."""
    try:
        conn = get_pool().get(channel)
        if conn is None:
            return False
        ok = await conn.send_batch(lines, kind=kind, concat=concat)
        logger.debug("identity: batch %d lines -> %s (%s)",
                     len(lines), channel, "ok" if ok else "FAIL")
        return ok
    except Exception:
        logger.debug("identity: send_ml failed for %s", channel, exc_info=True)
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
