"""Mercury observatory IRC daemon (replaces the Matrix/tuwunel stack).

One small stdlib-only asyncio server with two listeners on the same
channel state:

- **agent listener** (default ``127.0.0.1:6669``): the gateway and its
  agents connect here. Localhost-bound by default.
- **server listener** (default ``127.0.0.1:6670``): the user connects
  here with any IRC client. The daemon keeps NO chat history and never
  replays anything on JOIN — clients keep their own scrollback (The
  Lounge persists its own); server-side replay only ever duplicated
  lines. Agent connections stay up, so nothing is lost server-side.

Protocol: RFC 1459 subset (NICK/USER/PASS/JOIN/PART/PRIVMSG/NOTICE/
TOPIC/NAMES/WHO/PING/PONG/QUIT/MODE-noop, plus OPER/DESTROY for the
gateway bot's /exit room kill). No TLS in v1 — the server binds
localhost or a tailnet address (see ``provision``), never the open
internet. No NickServ, no federation, single network.

Channel lifecycle is the agent lifecycle: IRC channels are created on
first JOIN and destroyed explicitly via :meth:`IrcDaemon.destroy_channel`
(``/exit``), which PARTs every member.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: IRC-safe channel slug: lowercase, letters/digits/-/_ only.
_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


@functools.lru_cache(maxsize=1)
def mercury_version() -> str:
    """Mercury release for the 004 numeric (user directive: one version
    everywhere — no sub-versions). Parsed from the tree: importing
    mercury_cli would drag the daemon into the CLI's dependency wall.
    Falls back to the legacy literal when unreadable."""
    try:
        text = (
            Path(__file__).resolve().parents[1]
            / "mercury_cli"
            / "__init__.py"
        ).read_text(encoding="utf-8")
        m = re.search(r'__version__ = "([^"]+)"', text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "mercury-ircd"



def clean_channel(name: str) -> str:
    """Map an agent/room name to an IRC channel (``#slug``).

    ``parent`` + ``child`` → ``#parent-child`` by convention (callers join
    the parts with ``-`` first, then clean once). Never raises: empty input
    yields ``#unnamed``.
    """
    slug = _CLEAN_RE.sub("-", (name or "").strip().lower()).strip("-_")
    return f"#{slug or 'unnamed'}"


def clean_nick(name: str, fallback: str = "agent") -> str:
    """Map an agent name to an IRC nick (letters/digits/_/- , ≤32 chars)."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip())[:32].strip("_")
    return slug or fallback


def _live_room_ids_from_disk(state_dir: Path | str = "") -> list[str]:
    """Live agent channels from state.db (ircd-process fallback).

    The gateway owns the RoomManager in ITS process; this daemon never
    sees it (separate memory), so a manager-only lookup leaves every
    late-joining server client on just the gateway channel after an
    ircd restart. state.db is the shared file both processes see —
    read its live room_ids directly (stdlib sqlite3 only; never raises).
    """
    import os as _os
    import sqlite3 as _sqlite3

    cands: list[Path] = []
    try:
        if str(state_dir or "").strip():
            cands.append(Path(str(state_dir)).expanduser() / "state.db")
    except Exception:
        pass
    try:
        home = (_os.environ.get("MERCURY_HOME") or "").strip()
        if home:
            cands.append(Path(home).expanduser() / "observatory" / "state.db")
    except Exception:
        pass
    for db in cands:
        try:
            if not db.is_file():
                continue
            con = _sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(
                    "SELECT room_id FROM nodes WHERE status = 'live'"
                    " ORDER BY depth, created_epoch, node_id"
                ).fetchall()
            finally:
                try:
                    con.close()
                except Exception:
                    pass
            out: list[str] = []
            for r in rows:
                try:
                    room = str((r[0] if r else "") or "").strip()
                except Exception:
                    continue
                if room.startswith("#") and room not in out:
                    out.append(room)
            return out
        except Exception:
            continue
    return []

@dataclass
class HistoryMessage:
    ts: float
    sender: str
    target: str
    text: str
    kind: str = "privmsg"  # privmsg | notice | system
    msgid: str = ""  # stable id for client echo dedupe


@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    agent_port: int = 6669
    server_host: str = "127.0.0.1"
    server_port: int = 6670
    server_name: str = "mercury"
    tls_port: int = 6697  # 0 disables the TLS listener entirely
    tls_cert: str = ""
    tls_key: str = ""
    password: str = ""  # required PASS on the server listener when set
    agent_password: str = ""  # required PASS on the agent listener when set
    state_dir: Path | str = ""
    network_name: str = "mercury"

PING_INTERVAL = 60.0  # seconds between server PINGs to idle clients
PING_TIMEOUT = 180.0  # drop a registered client silent this long
SEND_TIMEOUT = 10.0  # one wedged client may never block others longer than this

class _Client:
    __slots__ = (
        "reader",
        "writer",
        "nick",
        "user",
        "realname",
        "registered",
        "pass_ok",
        "oper",
        "addr",
        "away",
        "caps",
        "pending_batch",
        "batch_out",
        "channels",
        "listener",
        "send_lock",
        "last_in",
        "ping_out",
    )

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, addr: str
    ):
        self.reader = reader
        self.writer = writer
        self.nick = ""
        self.user = ""
        self.realname = ""
        self.registered = False
        self.pass_ok = False
        self.oper = False
        self.addr = addr
        self.caps: set[str] = set()
        self.batch_out: dict[str, str] = {}
        self.pending_batch: str | None = None
        self.away: str | None = None
        self.channels: set[str] = set()  # folded channel keys
        self.send_lock = asyncio.Lock()
        self.last_in = time.monotonic()
        self.ping_out = False


class IrcDaemon:
    """One IRC network: channel state + history + two listeners."""

    def __init__(
        self,
        config: DaemonConfig | None = None,
        on_privmsg: Callable[[str, str, str], None] | None = None,
        rebind_interval: float = 30.0,
    ):
        self.config = config or DaemonConfig()
        self.on_privmsg = on_privmsg  # (sender, target, text) hook, e.g. router
        self.rebind_interval = max(0.05, float(rebind_interval))
        self._clients: dict[str, _Client] = {}  # folded nick -> client
        self._channels: dict[str, set[str]] = defaultdict(set)  # folded -> nicks
        self._display: dict[str, str] = {}  # folded channel -> display name
        self._topics: dict[str, tuple[str, str, float]] = {}
        self._msg_seq = 0  # msgid sequence for client echo dedupe
        self._servers: list[asyncio.AbstractServer] = []
        self._lock = asyncio.Lock()
        self._ping_task: asyncio.Task | None = None
        self._pending_binds: list[tuple[str, str, int]] = []
        self._pending_tls: bool = False
        self._rebind_task: asyncio.Task | None = None

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> "IrcDaemon":
        cfg = self.config
        errors: list[str] = []
        agent = await self._listen(
            "agent", cfg.host, cfg.agent_port, errors)
        server = await self._listen(
            "server", cfg.server_host, cfg.server_port, errors)
        tls = await self._listen_tls(errors)
        self._servers = [s for s in (agent, server, tls) if s is not None]
        if not self._servers:
            raise OSError(
                "ircd: no listener bound — "
                + "; ".join(errors))
        # Boot race (tailscaled not up yet): a bound localhost listener
        # keeps the gateway alive while the tailnet bind fails. Retry the
        # failures in the background instead of staying degraded forever.
        self._pending_binds = []
        if agent is None:
            self._pending_binds.append(("agent", cfg.host, cfg.agent_port))
        if server is None:
            self._pending_binds.append(
                ("server", cfg.server_host, cfg.server_port))
        self._pending_tls = tls is None and bool(int(cfg.tls_port or 0))
        if self._pending_binds or self._pending_tls:
            self._rebind_task = asyncio.create_task(self._rebind_loop())
        for err in errors:
            logger.error("ircd: degraded listener: %s", err)
        logger.info(
            "ircd: agent %s:%d server %s:%d (%s)",
            cfg.host,
            cfg.agent_port,
            cfg.server_host,
            cfg.server_port,
            cfg.network_name,
        )
        self._ping_task = asyncio.create_task(self._ping_loop())
        return self
    async def _rebind_loop(self) -> None:
        """Retry failed listener binds until they all succeed or stop().

        Boot race cover: tailscaled may not hold the tailnet IP yet when
        the gateway starts. Without this the server listener stays down
        forever behind a healthy-looking gateway.
        """
        try:
            while self._pending_binds or self._pending_tls:
                await asyncio.sleep(self.rebind_interval)
                errors: list[str] = []
                for entry in list(self._pending_binds):
                    listener, host, port = entry
                    server = await self._listen(listener, host, port, errors)
                    if server is not None:
                        self._servers.append(server)
                        self._pending_binds.remove(entry)
                        logger.error(
                            "ircd: late bind recovered: %s %s:%d",
                            listener, host, port)
                if self._pending_tls:
                    tls_errors: list[str] = []
                    tls = await self._listen_tls(tls_errors)
                    if tls is not None:
                        self._servers.append(tls)
                        self._pending_tls = False
                        logger.error("ircd: late bind recovered: tls")
                    errors.extend(tls_errors)
                for err in errors:
                    logger.debug("ircd: rebind deferred: %s", err)
        except asyncio.CancelledError:
            pass
    async def _listen(self, listener: str, host: str, port: int,
                      errors: list[str]) -> asyncio.AbstractServer | None:
        """Bind one listener; None (plus a loud error) instead of dying."""
        try:
            return await asyncio.start_server(
                lambda r, w, _listener=listener: self._handle(r, w, listener=_listener),
                host,
                port,
            )
        except Exception as exc:
            errors.append(
                f"{listener} {host}:{port} not bound ({exc}) — "
                f"{'check Tailscale / the bind address' if listener == 'server' else 'check for a stale daemon holding the port'}"
            )
            return None

    async def _listen_tls(self, errors: list[str]) -> asyncio.AbstractServer | None:
        """TLS server on the server host (strict clients: TLS default,
        no plaintext toggle). Same rooms, server password. Missing cert,
        zero port, or bind failure degrades to plaintext-only (loud)."""
        import ssl as _ssl

        cfg = self.config
        if not int(cfg.tls_port or 0):
            return None
        if not (cfg.tls_cert and cfg.tls_key):
            errors.append("tls listener skipped (no certificate provisioned)")
            return None
        try:
            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cfg.tls_cert), str(cfg.tls_key))
        except Exception as exc:
            errors.append(f"tls context failed ({exc}) — plaintext only")
            return None
        try:
            return await asyncio.start_server(
                lambda r, w: self._handle(r, w, listener="server-tls"),
                cfg.server_host,
                int(cfg.tls_port),
                ssl=ctx,
            )
        except Exception as exc:
            errors.append(
                f"tls {cfg.server_host}:{cfg.tls_port} not bound ({exc})")
            return None

    async def stop(self) -> None:
        if self._rebind_task is not None:
            self._rebind_task.cancel()
            self._rebind_task = None
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        for server in self._servers:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=5)
            except Exception:
                pass
        self._servers = []
        for client in list(self._clients.values()):
            try:
                client.writer.close()
                try:
                    await asyncio.wait_for(client.writer.wait_closed(), timeout=5)
                except Exception:
                    pass
            except Exception:
                pass
        self._clients.clear()

    async def _ping_loop(self) -> None:
        """Drive the liveness sweep every PING_INTERVAL (see _ping_sweep)."""
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await self._ping_sweep()
        except asyncio.CancelledError:
            pass

    async def _ping_sweep(self) -> None:
        """One liveness tick: PING idle clients, drop the long-silent.

        Without this, a daemon restart leaves every client believing it
        is still connected (half-open): sends vanish, no error surfaces.
        Clients are handled independently and every send is bounded (_send
        drops a wedged socket), so one dead client can never stall the
        sweep for the rest.
        """
        now = time.monotonic()
        for client in list(self._clients.values()):
            try:
                idle = now - client.last_in
                if idle >= PING_TIMEOUT:
                    client.writer.close()
                elif idle >= PING_INTERVAL and not client.ping_out:
                    client.ping_out = True
                    await self._send(
                        client,
                        f":{self.config.server_name} PING "
                        f":{self.config.server_name}",
                    )
            except Exception:
                pass

    def channel_names(self) -> list[str]:
        return sorted(self._display.get(k, k) for k in self._channels)

    async def destroy_channel(self, channel: str, reason: str = "room closed") -> int:
        """PART every member and drop the channel.

        Returns the number of members removed. Never raises.
        """
        key = channel.lower()
        async with self._lock:
            members = sorted(self._channels.pop(key, ()))
            self._display.pop(key, None)
            self._topics.pop(key, None)
        for nick in members:
            client = self._clients.get(nick)
            if client is None:
                continue
            client.channels.discard(key)
            await self._send(
                client, f":{client.nick}!{client.user}@{self.config.server_name} PART {channel} :{reason}"
            )
        return len(members)

    async def server_notice(self, channel: str, text: str) -> None:
        """Post a server-originated notice into a channel (fanned to members)."""
        msg = HistoryMessage(
            time.time(), self.config.server_name, channel, text, kind="notice"
        )
        await self._fanout(msg)

    # -- connection handling --------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, listener: str
    ) -> None:
        peer = writer.get_extra_info("peername")
        addr = str(peer[0]) if peer else "?"
        client = _Client(reader, writer, addr)
        client.listener = listener
        password = (
            self.config.agent_password if listener == "agent" else self.config.password
        )
        buf = b""
        try:
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if line:
                        await self._line(client, line, listener, password)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            logger.debug("ircd: client error", exc_info=True)
        finally:
            await self._quit(client, "connection closed")

    async def _line(
        self, client: _Client, line: str, listener: str, password: str
    ) -> None:
        # IRCv3 message-tags: strip the @tag block before dispatch.
        # Only +batch is honored (fork-only single-message reassembly).
        if line.startswith("@"):
            tagstr, _, line = line[1:].partition(" ")
            for part in tagstr.split(";"):
                k, _, v = part.partition("=")
                if k == "batch" and v:
                    client.pending_batch = v[:64]
            if not line:
                return
        client.last_in = time.monotonic()
        if " " in line:
            cmd, rest = line.split(" ", 1)
        else:
            cmd, rest = line, ""
        cmd = cmd.upper()
        if cmd == "PASS":
            given = rest.strip().lstrip(":")
            client.pass_ok = given == password
            # Shape-only auth log (never the secret): attempt length vs
            # outcome distinguishes truncation/padding from wrong values.
            logger.info(
                "ircd: %s PASS attempt len=%d expected=%d -> %s",
                listener, len(given), len(password),
                "ok" if client.pass_ok else "464",
            )
            if password and not client.pass_ok:
                await self._numeric(client, 464, "*", "Password incorrect")
            else:
                # PASS-last clients (NICK/USER already in): a correct
                # password must complete registration right here, or the
                # client sits unregistered forever behind one early 464.
                await self._maybe_register(client, listener, password)
            return
        if cmd == "NICK":
            await self._cmd_nick(client, rest.strip(), listener, password)
            return
        if cmd == "USER":
            parts = rest.split(" ", 3)
            if len(parts) >= 1:
                client.user = parts[0][:32] or "user"
            if len(parts) >= 4:
                client.realname = parts[3].lstrip(":")[:128]
            await self._maybe_register(client, listener, password)
            return
        if cmd == "CAP":
            await self._cmd_cap(client, rest.strip())
            return
        if not client.registered:
            return
        if cmd == "PING":
            await self._send(
                client,
                f":{self.config.server_name} PONG "
                f"{self.config.server_name} :{rest.lstrip(':')}",
            )
        elif cmd == "PONG":
            client.ping_out = False
        elif cmd == "JOIN":
            await self._cmd_join(client, rest.strip())
        elif cmd == "PART":
            await self._cmd_part(client, rest.strip())
        elif cmd == "PRIVMSG":
            await self._cmd_msg(client, rest, kind="privmsg")
        elif cmd == "NOTICE":
            await self._cmd_msg(client, rest, kind="notice")
        elif cmd == "LIST":
            await self._cmd_list(client, rest.strip())
        elif cmd == "TOPIC":
            await self._cmd_topic(client, rest.strip())
        elif cmd == "NAMES":
            await self._cmd_names(client, rest.strip().lstrip(":"))
        elif cmd == "WHO":
            await self._cmd_who(client, rest.strip().lstrip(":"))
        elif cmd == "AWAY":
            await self._cmd_away(client, rest.strip())
        elif cmd == "MODE":
            target = rest.split(" ", 1)[0] if rest else ""
            await self._numeric(client, 324, f"{client.nick} {target} +", "End of MODE")
        elif cmd == "OPER":
            await self._cmd_oper(client, rest.strip())
        elif cmd == "BATCH":
            await self._cmd_batch(client, rest.strip())
        elif cmd == "INVITE":
            await self._cmd_invite(client, rest.strip())
        elif cmd == "DESTROY":
            await self._cmd_destroy(client, rest.strip())
        elif cmd == "QUIT":
            await self._quit(client, rest.lstrip(":") or "quit")
        elif cmd == "USERHOST" or cmd == "ISON":
            pass  # accepted, ignored
        else:
            await self._numeric(client, 421, cmd, "Unknown command")
    # -- commands --------------------------------------------------------

    def _who(self, client: _Client) -> str:
        return client.nick or "*"

    #: Fork-only caps: the batch protocol every client negotiates.
    #: (No server-time/message-tags/echo/SASL — nobody left uses them.)
    #: (No server-time/message-tags/echo/SASL — nobody left uses them.)
    _OFFERED_CAPS = (
        "batch",
        "draft/multiline",
    )

    async def _cmd_cap(self, client: _Client, arg: str) -> None:
        """IRCv3 negotiation: ACK the offered subset, NAK the rest."""
        parts = arg.split(None, 2)
        sub = (parts[0] if parts else "").upper()
        rest = " ".join(parts[1:]) if len(parts) > 1 else ""
        if sub == "LS" or sub == "LIST":
            await self._send(
                client,
                f":{self.config.server_name} CAP {self._who(client)} "
                f"LS :{' '.join(self._OFFERED_CAPS)}",
            )
        elif sub == "REQ":
            wants = [w.strip().lower().lstrip(":") for w in rest.split()]
            ok = [w for w in wants if w in self._OFFERED_CAPS]
            no = [w for w in wants if w not in self._OFFERED_CAPS]
            client.caps.update(ok)
            if ok:
                await self._send(
                    client,
                    f":{self.config.server_name} CAP {self._who(client)} "
                    f"ACK :{' '.join(ok)}",
                )
            if no:
                await self._send(
                    client,
                    f":{self.config.server_name} CAP {self._who(client)} "
                    f"NAK :{' '.join(no)}",
                )
        elif sub == "END":
            pass  # registration continues with NICK/USER as normal
        # anything else: ignored (no state change)

    async def _cmd_nick(
        self, client: _Client, nick: str, listener: str, password: str
    ) -> None:
        nick = nick.strip().lstrip(":")[:32]
        if not nick or not re.fullmatch(r"[A-Za-z0-9_\-\[\]\\`^{}|]+", nick):
            await self._numeric(client, 432, nick or "*", "Erroneous nickname")
            return
        key = nick.lower()
        if key in self._clients and self._clients[key] is not client:
            # Reclaim is only for authenticated peers. A wrong listener
            # password must not evict the working gateway before USER/PASS
            # registration has even been checked.
            if password and not client.pass_ok:
                await self._numeric(client, 464, "*", "Password incorrect")
                return
            # Server semantics: the newest authenticated connection wins.
            old = self._clients.pop(key)
            try:
                await self._send(old, f"ERROR :nick {nick} reclaimed")
                old.writer.close()
            except Exception:
                pass
            async with self._lock:
                for members in self._channels.values():
                    if key in members:
                        members.discard(key)
                        members.add(key)
        old_key = client.nick.lower() if client.nick else ""
        if old_key and old_key in self._clients and self._clients[old_key] is client:
            del self._clients[old_key]
        client.nick = nick
        self._clients[key] = client
        await self._maybe_register(client, listener, password)

    async def _maybe_register(
        self, client: _Client, listener: str, password: str
    ) -> None:
        if client.registered or not client.nick or not client.user:
            return
        if password and not client.pass_ok:
            # Fork-only: every client sends PASS first, so a missing or
            # wrong password fails fast here with no wait-for-late-PASS.
            await self._numeric(client, 464, "*", "Password incorrect")
            return
        client.registered = True
        logger.info("ircd: %s registered nick=%s", listener, client.nick)
        name = self.config.server_name
        nick = client.nick
        await self._send(client, f":{name} 001 {nick} :Welcome to {name}, {nick}")
        await self._send(client, f":{name} 002 {nick} :Your host is {name}")
        await self._send(
            client, f":{name} 003 {nick} :This server was created for Mercury"
        )
        await self._send(client, f":{name} 004 {nick} {name} {mercury_version()} o o")
        await self._send(
            client,
            f":{name} 005 {nick} CHANTYPES=# NICKLEN=32 "
            f"TOPICLEN=256 :are supported by this server",
        )
        # Clients consider login complete at end-of-MOTD; without 376
        await self._numeric(client, 422, nick, "MOTD File is missing")
        if listener != "agent":
            # Parity: every authenticated user lands in every live agent
            # room, local or remote — zero manual joins. The single-nick
            # INVITE only covers the local Lounge; a remote client would
            # otherwise join an empty server. Gateway first, then the rest
            # in state order.
            base = clean_channel(name or "mercury").lstrip("#")
            targets: list[str] = [f"#{base}_gateway"]
            try:
                from observatory.rooms import get_room_manager

                manager = get_room_manager()
                if manager is not None:
                    for row in manager.live_rows():
                        room = str((row or {}).get("room_id") or "").strip()
                        if room.startswith("#") and room not in targets:
                            targets.append(room)
            except Exception:
                logger.debug("ircd: room list lookup failed", exc_info=True)
            if len(targets) <= 1:
                # Separate-process law: the manager lives in the gateway,
                # never here — without this fallback every client that
                # (re)connects after an ircd restart lands on just the
                # gateway channel and never sees spawned rooms.
                try:
                    for room in _live_room_ids_from_disk(
                        getattr(self.config, "state_dir", "")):
                        if room not in targets:
                            targets.append(room)
                except Exception:
                    logger.debug("ircd: state.db room fallback failed",
                                 exc_info=True)
            for display in targets:
                key = display.lower()
                async with self._lock:
                    self._channels[key].add(nick.lower())
                    self._display.setdefault(key, display)
                    client.channels.add(key)
                await self._emit_join(client, key, display)

    async def _cmd_join(self, client: _Client, arg: str) -> None:
        if not arg:
            await self._numeric(client, 461, "JOIN", "Not enough parameters")
            return
        joined: list[tuple[str, str, bool]] = []
        async with self._lock:
            for chan in arg.split(","):
                chan = chan.split(" ", 1)[0].strip()
                if not chan.startswith("#") or len(chan) < 2:
                    await self._numeric(client, 403, chan, "No such channel")
                    continue
                key = chan.lower()
                is_new = key not in self._channels
                self._channels[key].add(client.nick.lower())
                self._display.setdefault(key, chan)
                client.channels.add(key)
                joined.append((key, self._display[key], is_new))
        for key, display, is_new in joined:
            await self._emit_join(client, key, display)
            if is_new:
                await self._auto_join_server_clients(client, key, display)

    async def _auto_join_server_clients(
        self, origin: _Client, key: str, display: str
    ) -> None:
        """Join every server-listener client to a newly created channel."""
        async with self._lock:
            members = self._channels.get(key)
            if members is None:
                return
            targets: list[_Client] = []
            for nick, peer in list(self._clients.items()):
                if peer is origin:
                    continue
                try:
                    if not peer.registered:
                        continue
                    if getattr(peer, "listener", "server") == "agent":
                        continue
                    if nick in members:
                        continue
                    members.add(nick)
                    peer.channels.add(key)
                    targets.append(peer)
                except Exception:
                    continue
            display_now = self._display.get(key, display)
        for peer in targets:
            try:
                for nick in sorted(self._channels.get(key, ())):
                    other = self._clients.get(nick)
                    if other is not None and other is not peer:
                        await self._send(
                            other,
                            f":{peer.nick}!{peer.user}@{self.config.server_name} JOIN {display_now}",
                        )
                await self._emit_join(peer, key, display_now)
            except Exception:
                logger.debug("ircd: auto-join fanout failed", exc_info=True)

    async def _emit_join(self, peer: _Client, key: str, display: str) -> None:
        """JOIN + topic + names for a new member."""
        await self._send(
            peer, f":{peer.nick}!{peer.user}@{self.config.server_name} JOIN {display}"
        )
        topic = self._topics.get(key)
        if topic is not None:
            text, setter, _ts = topic
            await self._numeric(peer, 332, f"{peer.nick} {display} :{text}")
        else:
            await self._numeric(
                peer, 331, f"{peer.nick} {display}", "No topic is set"
            )
        await self._send_names(peer, key, display)

    async def _send_names(self, client: _Client, key: str, display: str) -> None:
        members = sorted(self._channels.get(key, ()))
        nicks = " ".join(self._clients[n].nick for n in members if n in self._clients)
        await self._numeric(client, 353, f"{client.nick} = {display}", nicks)
        await self._numeric(
            client, 366, f"{client.nick} {display}", "End of NAMES list"
        )

    async def _cmd_names(self, client: _Client, arg: str) -> None:
        if not arg:
            return
        for chan in arg.split(","):
            key = chan.strip().lower()
            if key in self._channels:
                await self._send_names(
                    client, key, self._display.get(key, chan.strip())
                )

    async def _cmd_who(self, client: _Client, arg: str) -> None:
        key = (arg.split(" ", 1)[0] if arg else "").lower()
        members = sorted(self._channels.get(key, ())) if key else []
        for nick in members:
            c = self._clients.get(nick)
            if c is None:
                continue
            flag = "G" if c.away else "H"
            await self._numeric(
                client,
                352,
                f"{client.nick} {self._display.get(key, arg)} {c.user} mercury mercury {c.nick} {flag}",
                f"0 {c.realname or c.nick}",
            )
        await self._numeric(client, 315, f"{client.nick} {arg}", "End of WHO list")

    async def _cmd_away(self, client: _Client, arg: str) -> None:
        """AWAY [:message] — soju sends this on upstream connect; without
        it soju drops the link on our 421. 306/305 reply, state for WHO."""
        msg = arg.lstrip(":").strip()[:160]
        client.away = msg or None
        if client.away:
            await self._numeric(client, 306, client.nick, "You have been marked as being away")
        else:
            await self._numeric(client, 305, client.nick, "You are no longer marked as being away")

    async def _cmd_list(self, client: _Client, arg: str) -> None:
        """RPL_LIST so clients can discover rooms (empty server → headers only)."""
        name = self.config.server_name
        nick = client.nick or "*"
        wanted = (arg.split(" ", 1)[0] if arg else "").strip().lower()
        await self._send(client, f":{name} 321 {nick} Channel :Users Name")
        for key in sorted(self._channels):
            if wanted and key != wanted.lstrip("#"):
                continue
            display = self._display.get(key, key)
            count = len(self._channels[key])
            topic = self._topics.get(key)
            text = topic[0] if topic else ""
            await self._send(client, f":{name} 322 {nick} {display} {count} :{text}")
        await self._send(client, f":{name} 323 {nick} :End of /LIST")

    async def _cmd_invite(self, client: _Client, arg: str) -> None:
        """INVITE <nick> <#channel> — 341 to the sender, relay + join.

        The gateway bot invites the lounge nick on spawn; the target is
        server-joined at the same time (auto-join), since The Lounge
        never joins on INVITE by itself.
        """
        parts = arg.split()
        if len(parts) < 2:
            await self._numeric(client, 461, "INVITE", "Not enough parameters")
            return
        target_nick, chan = parts[0], parts[1].lstrip(":")
        if not chan.startswith("#") or len(chan) < 2:
            await self._numeric(client, 403, chan or "*", "No such channel")
            return
        key = chan.lower()
        if key not in self._channels:
            await self._numeric(client, 403, chan, "No such channel")
            return
        peer = self._clients.get(target_nick.lower())
        if peer is None or not peer.registered:
            await self._numeric(client, 401, target_nick, "No such nick")
            return
        display = self._display.get(key, chan)
        await self._numeric(
            client, 341, f"{client.nick} {peer.nick}", display)
        await self._send(
            peer,
            f":{client.nick}!{client.user}@{self.config.server_name} INVITE {peer.nick} :{display}",
        )
        # Auto-join: on this network an invite IS the join. The Lounge
        # surfaces INVITEs as messages and never joins, so the invited
        # nick is server-added and the JOIN is broadcast (death later
        # PARTs them via destroy_channel, clearing the sidebar).
        async with self._lock:
            members = self._channels.get(key)
            if members is not None and peer.nick.lower() not in members:
                members.add(peer.nick.lower())
                peer.channels.add(key)
            else:
                members = None
        if members is not None:
            for nick in sorted(members):
                other = self._clients.get(nick)
                if other is not None and other is not peer:
                    await self._send(
                        other,
                        f":{peer.nick}!{peer.user}@{self.config.server_name} JOIN {display}",
                    )
            await self._emit_join(peer, key, display)

    async def _cmd_oper(self, client: _Client, arg: str) -> None:
        """OPER <password> — grant channel-destroy rights to the gateway bot.

        Either listener secret works. The old single-secret check
        compared only the agent password, so the bot (which authenticates
        with the server password) got 464 forever and every DESTROY died
        with 481 — rooms lingered after agent death.
        """
        secret = arg.split(" ", 1)[0].lstrip(":")
        secrets = {
            s for s in (self.config.agent_password, self.config.password) if s
        }
        if secret and secret in secrets:
            client.oper = True
            await self._numeric(client, 381, client.nick, "You are now an IRC operator")
        else:
            await self._numeric(client, 464, client.nick, "Password incorrect")

    async def _cmd_destroy(self, client: _Client, arg: str) -> None:
        """DESTROY #channel :reason — oper-only room kill for /exit."""
        if not client.oper:
            await self._numeric(
                client,
                481,
                client.nick,
                "Permission Denied - You're not an IRC operator",
            )
            return
        channel = arg.split(" ", 1)[0].strip()
        if not channel.startswith("#"):
            await self._numeric(client, 403, channel or "*", "No such channel")
            return
        await self.destroy_channel(channel, reason="room closed (/exit)")
        await self._numeric(
            client, 200, f"{client.nick} {channel}", "Channel destroyed"
        )

    async def _cmd_part(self, client: _Client, arg: str) -> None:
        if not arg:
            await self._numeric(client, 461, "PART", "Not enough parameters")
            return
        parts = arg.split(" :", 1)
        reason = parts[1] if len(parts) > 1 else "leaving"
        async with self._lock:
            for chan in parts[0].split(","):
                key = chan.strip().lower()
                members = self._channels.get(key)
                if members is None or client.nick.lower() not in members:
                    await self._numeric(
                        client, 442, chan.strip(), "You're not on that channel"
                    )
                    continue
                members.discard(client.nick.lower())
                client.channels.discard(key)
                if not members:
                    # Keep empty channels around — only destroy_channel deletes.
                    pass
                await self._send(
                    client,
                    f":{client.nick}!{client.user}@{self.config.server_name} PART "
                    f"{self._display.get(key, chan.strip())} :{reason}",
                )

    def _split_msg_rest(self, rest: str) -> tuple[str, str] | None:
        if " :" in rest:
            target, text = rest.split(" :", 1)
        elif " " in rest:
            target, text = rest.split(" ", 1)
            text = text.lstrip(":")
        else:
            return None
        return target.strip(), text

    async def _cmd_msg(self, client: _Client, rest: str, kind: str) -> None:
        split = self._split_msg_rest(rest)
        if split is None:
            await self._numeric(client, 411, "No recipient given", "No recipient")
            return
        target, text = split
        if not target or not text:
            await self._numeric(client, 412, "No text to send", "No text")
            return
        sender = client.nick
        batch, client.pending_batch = client.pending_batch, None
        if target.startswith("#"):
            key = target.lower()
            async with self._lock:
                members = self._channels.get(key)
                if members is None:
                    await self._numeric(client, 403, target, "No such channel")
                    return
                if sender.lower() not in members:
                    await self._numeric(client, 404, target, "Cannot send to channel")
                    return
                display = self._display.get(key, target)
            msg = HistoryMessage(time.time(), sender, display, text, kind=kind)
            await self._fanout(msg, batch=batch)
        else:
            peer = self._clients.get(target.lower())
            if peer is None or not peer.registered:
                await self._numeric(client, 401, target, "No such nick")
                return
            now = time.time()
            await self._send(
                peer,
                self._tags(peer, ts=now, batch=batch)
                + f":{sender}!{client.user}@{self.config.server_name} {kind.upper()} "
                f"{peer.nick} :{text}",
            )
            if self.on_privmsg is not None and kind == "privmsg":
                try:
                    self.on_privmsg(sender, peer.nick, text)
                except Exception:
                    logger.debug("ircd: on_privmsg hook failed", exc_info=True)

    def _tags(
        self,
        client: _Client,
        *,
        ts: float | None = None,
        msgid: str = "",
        label: str | None = None,
        batch: str | None = None,
    ) -> str:
        """IRCv3 tag prefix. The batch tag rides unconditionally
        (fork-only: every client negotiates draft/multiline).
        ts/msgid/label are accepted for call-compat but never emitted —
        no remaining client negotiates those caps."""
        parts: list[str] = []
        if batch:
            parts.append(f"batch={batch}")
        return ("@" + ";".join(parts) + " ") if parts else ""

    async def _cmd_topic(self, client: _Client, arg: str) -> None:
        if not arg:
            return
        if " :" in arg:
            chan, text = arg.split(" :", 1)
        else:
            chan, text = arg, None
        key = chan.strip().lower()
        members = self._channels.get(key)
        if members is None:
            await self._numeric(client, 403, chan.strip(), "No such channel")
            return
        display = self._display.get(key, chan.strip())
        if text is None:
            topic = self._topics.get(key)
            if topic is None:
                await self._numeric(
                    client, 331, f"{client.nick} {display}", "No topic is set"
                )
            else:
                await self._numeric(client, 332, f"{client.nick} {display} :{topic[0]}")
            return
        if client.nick.lower() not in members:
            await self._numeric(client, 442, display, "You're not on that channel")
            return
        self._topics[key] = (text[:256], client.nick, time.time())
        notice = HistoryMessage(
            time.time(), client.nick, display, f"topic: {text[:256]}", kind="notice"
        )
        await self._fanout(notice)

    async def _fanout(self, msg: HistoryMessage, batch: str = "") -> None:
        key = msg.target.lower()
        if not msg.msgid:
            self._msg_seq += 1
            msg.msgid = f"m{self._msg_seq}"
        members = sorted(self._channels.get(key, ()))
        display = self._display.get(key, msg.target)
        body = (
            f":{msg.sender}!relay@{self.config.server_name} {msg.kind.upper()} "
            f"{display} :{msg.text}"
        )
        for nick in members:
            if nick == msg.sender.lower():
                continue  # sender never sees its own lines
            peer = self._clients.get(nick)
            if peer is not None:
                await self._send(
                    peer,
                    self._tags(peer, ts=msg.ts, msgid=msg.msgid, batch=batch) + body,
                )

    def _batch_target_ok(self, client: _Client, target: str) -> bool:
        """A batch may only address where the sender could already speak."""
        if target.startswith("#"):
            members = self._channels.get(target.lower())
            return bool(members) and client.nick.lower() in members
        peer = self._clients.get(target.lower())
        return bool(peer is not None and peer.registered)

    async def _relay_batch_frame(self, client: _Client, target: str, line: str) -> None:
        """Forward one BATCH frame to every member (fork-only: all clients
        negotiate draft/multiline, no capable/legacy split)."""
        if target.startswith("#"):
            members = sorted(self._channels.get(target.lower(), ()))
            for nick in members:
                if nick == client.nick.lower():
                    continue
                peer = self._clients.get(nick)
                if peer is not None:
                    await self._send(peer, line)
        else:
            peer = self._clients.get(target.lower())
            if peer is not None and peer.registered:
                await self._send(peer, line)

    async def _cmd_batch(self, client: _Client, arg: str) -> None:
        """Relay draft/multiline batches; ignore other batch types.

        Open (+ref draft/multiline target) validates membership, records
        ref→target, and forwards the frame to capable recipients. Tagged
        lines then fan out with the batch tag preserved (see _cmd_msg);
        close (-ref) forwards and drops the mapping. Bounded per client.
        """
        parts = arg.split()
        if not parts:
            return
        token = parts[0]
        if token.startswith("+"):
            ref = token[1:]
            if not ref or len(parts) < 3 or parts[1] != "draft/multiline":
                return
            target = parts[2]
            if not self._batch_target_ok(client, target):
                return
            if len(client.batch_out) >= 16:
                oldest = next(iter(client.batch_out))
                del client.batch_out[oldest]
            client.batch_out[ref] = target
            await self._relay_batch_frame(
                client, target, f"BATCH +{ref} draft/multiline {target}")
        elif token.startswith("-"):
            ref = token[1:]
            target = client.batch_out.pop(ref, None)
            if not ref or target is None:
                return
            await self._relay_batch_frame(client, target, f"BATCH -{ref}")

    async def _quit(self, client: _Client, reason: str) -> None:
        key = client.nick.lower() if client.nick else ""
        # A reclaimed nick belongs to the replacement connection. Delayed
        # cleanup of the old socket must not PART that new connection from
        # its rooms, leaving a visible nick with a dead inbound path.
        if key and self._clients.get(key) is client:
            del self._clients[key]
            async with self._lock:
                for chan in list(client.channels):
                    members = self._channels.get(chan)
                    if members is not None:
                        members.discard(key)
        if client.registered and reason != "connection closed":
            try:
                await self._send(client, f"ERROR :{reason}")
            except Exception:
                pass
        try:
            client.writer.close()
        except Exception:
            pass

    # -- wire -----------------------------------------------------------

    @staticmethod
    def _iso_time(ts: float) -> str:
        import datetime as _dt

        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S."
        ) + f"{int(ts * 1000) % 1000:03d}Z"

    async def _send(self, client: _Client, line: str) -> bool:
        """One line to one client. Returns False when the client is dropped.

        A wedged reader (kernel buffer full, peer gone silent) stalls
        drain() forever; every loop that awaits _send would then freeze
        behind that one dead socket — including the PING sweep, which is
        what keeps half-open connections honest. The drain is therefore
        bounded; a client that cannot take the line within SEND_TIMEOUT
        is closed (its read loop reaps it) and the caller moves on.
        """
        try:
            async with client.send_lock:
                client.writer.write((line + "\r\n").encode("utf-8", "replace"))
                await asyncio.wait_for(client.writer.drain(), timeout=SEND_TIMEOUT)
            return True
        except Exception:
            try:
                client.writer.close()
            except Exception:
                pass
            return False

    async def _numeric(self, client: _Client, code: int, tail: str, text: str) -> None:
        await self._send(
            client, f":{self.config.server_name} {code:03d} {tail} :{text}"
        )


async def serve_forever(config: DaemonConfig) -> None:
    """Run the daemon until cancelled (systemd unit entry point)."""
    daemon = await IrcDaemon(config).start()
    try:
        await asyncio.Event().wait()
    finally:
        await daemon.stop()


def _config_file_candidates(args: Any) -> list[Path]:
    """ircd.json locations: explicit --config, then <state-dir>/ircd.json,
    then $MERCURY_HOME/observatory/ircd.json. Existing files only."""
    import os as _os

    cands: list[Path] = []
    explicit = str(getattr(args, "config", "") or "").strip()
    if explicit:
        cands.append(Path(explicit).expanduser())
    state_dir = str(getattr(args, "state_dir", "") or "").strip()
    if state_dir:
        cands.append(Path(state_dir).expanduser() / "ircd.json")
    home = _os.environ.get("MERCURY_HOME", "").strip()
    if home:
        cands.append(Path(home).expanduser() / "observatory" / "ircd.json")
    else:
        cands.append(Path.home() / ".mercury" / "observatory" / "ircd.json")
    return [p for p in cands if p.is_file()]


def _resolve_daemon_config(args: Any) -> DaemonConfig:
    """Layer explicit flags over ircd.json over compiled defaults.

    The systemd unit passes only --state-dir, so a bind change in
    ircd.json takes effect on plain restart — no unit re-render needed.
    Passwords: explicit flags win, else env (never the config file).
    Never raises for a missing/unreadable file (defaults apply).
    """
    import os as _os

    file_cfg: dict = {}
    for path in _config_file_candidates(args):
        try:
            import json as _json

            data = _json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                file_cfg = data
                break
        except Exception:
            continue

    def _pick(name: str, default: Any) -> Any:
        explicit = getattr(args, name, None)
        if explicit is not None:
            return explicit
        value = file_cfg.get(name)
        if isinstance(default, int) and isinstance(value, int):
            return value
        if isinstance(default, str) and isinstance(value, str):
            return value
        return default

    password = getattr(args, "password", None)
    if password is None:
        password = _os.environ.get("IRC_CLIENT_PASSWORD", "") or _os.environ.get(
            "IRC_BOUNCER_PASSWORD", "")
    agent_password = getattr(args, "agent_password", None)
    if agent_password is None:
        agent_password = _os.environ.get("IRC_AGENT_PASSWORD", "")
    state_dir = str(getattr(args, "state_dir", "") or "")
    tls_cert = getattr(args, "tls_cert", None) or ""
    tls_key = getattr(args, "tls_key", None) or ""
    if not tls_cert and state_dir:
        cand = Path(state_dir).expanduser() / "tls" / "server.crt"
        if cand.is_file():
            tls_cert = str(cand)
    if not tls_key and state_dir:
        cand = Path(state_dir).expanduser() / "tls" / "server.key"
        if cand.is_file():
            tls_key = str(cand)
    # set_ircd_bind writes `agent_host` (provision/config_gen vocabulary);
    # the daemon historically read `host`. Prefer agent_host so the
    # tailnet pin actually moves the agent listener — the gateway
    # connects here, and a silent localhost fallback breaks it with
    # ECONNREFUSED while the config claims the tailnet IP.
    _agent_host = file_cfg.get("agent_host")
    if isinstance(_agent_host, str) and _agent_host.strip():
        file_cfg = dict(file_cfg, host=_agent_host.strip())
    # The client listener binds per ircd.json directly — direct IRC
    # clients and The Lounge connect here. Pre-rename files still use the
    # old client-listener keys: honor them in memory (provision pops
    # them on its next write).
    for _new, _old in (("server_host", "bouncer_host"),
                       ("server_port", "bouncer_port")):
        if _new not in file_cfg and _old in file_cfg:
            file_cfg = dict(file_cfg, **{_new: file_cfg[_old]})
    server_host = _pick("server_host", "127.0.0.1")
    return DaemonConfig(
        host=_pick("host", "127.0.0.1"),
        agent_port=_pick("agent_port", 6669),
        server_host=server_host,
        server_port=_pick("server_port", 6670),
        tls_port=_pick("tls_port", 6697),
        tls_cert=tls_cert,
        tls_key=tls_key,
        server_name=_pick("server_name", "mercury"),
        password=password or "",
        agent_password=agent_password or "",
        state_dir=state_dir,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Mercury observatory IRC daemon")
    # Network flags default to None = "read ircd.json, else compiled
    # default" (see _resolve_daemon_config). The unit passes only
    # --state-dir so bind edits never go stale.
    parser.add_argument("--host", default=None)
    parser.add_argument("--agent-port", type=int, default=None)
    parser.add_argument("--server-name", default=None)
    # Passwords: explicit flags win; env fallback keeps secrets out of
    # ps output (the systemd unit passes none — EnvironmentFile only).
    parser.add_argument("--password", default=None)
    parser.add_argument("--agent-password", default=None)
    parser.add_argument("--tls-port", type=int, default=None)
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument("--state-dir", default="")
    parser.add_argument(
        "--config",
        default="",
        help="explicit ircd.json path (default: <state-dir>/ircd.json, then $MERCURY_HOME/observatory/ircd.json)",
    )
    args = parser.parse_args(argv)
    config = _resolve_daemon_config(args)
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve_forever(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
