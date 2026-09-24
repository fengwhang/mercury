"""Mercury observatory IRC daemon (replaces the Matrix/tuwunel stack).

One small stdlib-only asyncio server with two listeners on the same
channel state:

- **agent listener** (default ``127.0.0.1:6669``): the gateway and its
  agents connect here. Localhost-bound by default.
- **server listener** (default ``127.0.0.1:6670``): the user connects
  here with any IRC client. Server semantics: the daemon keeps the
  last ``history_limit`` messages per channel (persisted in SQLite so
  they survive restarts) and replays them on JOIN, so a client that
  disconnects and returns sees what it missed. Agent connections stay
  up, so nothing is ever lost server-side.

Protocol: RFC 1459 subset (NICK/USER/PASS/JOIN/PART/PRIVMSG/NOTICE/
TOPIC/NAMES/WHO/PING/PONG/QUIT/MODE-noop, plus OPER/DESTROY for the
gateway bot's /exit room kill). No TLS in v1 — the server binds
localhost or a tailnet address (see ``provision``), never the open
internet. No NickServ, no federation, single network.

Channel lifecycle is the agent lifecycle: IRC channels are created on
first JOIN and destroyed explicitly via :meth:`IrcDaemon.destroy_channel`
(``/exit``), which PARTs every member. History of a destroyed channel
is dropped.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: IRC-safe channel slug: lowercase, letters/digits/-/_ only.
_CLEAN_RE = re.compile(r"[^a-z0-9_-]+")


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


@dataclass
class HistoryMessage:
    ts: float
    sender: str
    target: str
    text: str
    kind: str = "privmsg"  # privmsg | notice | system
    msgid: str = ""  # stable id for draft/chathistory anchors


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
    history_limit: int = 200
    state_dir: Path | str = ""
    network_name: str = "mercury"

PING_INTERVAL = 60.0  # seconds between server PINGs to idle clients
PING_TIMEOUT = 180.0  # drop a registered client silent this long

class _Client:
    __slots__ = (
        "reader",
        "writer",
        "nick",
        "user",
        "realname",
        "registered",
        "pass_ok",
        "pass_attempted",
        "pass_noticed",
        "oper",
        "sasl",
        "addr",
        "away",
        "caps",
        "pending_label",
        "channels",
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
        self.pass_attempted = False
        self.pass_noticed = False
        self.oper = False
        self.sasl = None
        self.caps: set[str] = set()
        self.pending_label: str | None = None
        self.addr = addr
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
    ):
        self.config = config or DaemonConfig()
        self.on_privmsg = on_privmsg  # (sender, target, text) hook, e.g. router
        self._clients: dict[str, _Client] = {}  # folded nick -> client
        self._channels: dict[str, set[str]] = defaultdict(set)  # folded -> nicks
        self._display: dict[str, str] = {}  # folded channel -> display name
        self._topics: dict[str, tuple[str, str, float]] = {}
        self._history: dict[str, deque[HistoryMessage]] = defaultdict(deque)
        self._msg_seq = 0  # fallback msgids when no SQLite (ephemeral tests)
        self._batch_seq = 0  # draft/chathistory BATCH refs
        self._servers: list[asyncio.AbstractServer] = []
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._ping_task: asyncio.Task | None = None

    def _db_path(self) -> Path | None:
        if not self.config.state_dir:
            return None
        root = Path(self.config.state_dir)
        root.mkdir(parents=True, exist_ok=True)
        return root / "irc-history.db"

    # -- persistence ----------------------------------------------------
    def _open_db(self) -> None:
        path = self._db_path()
        if path is None:
            return
        self._db = sqlite3.connect(str(path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS history "
            "(channel TEXT, ts REAL, sender TEXT, target TEXT, text TEXT, kind TEXT)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_channel ON history(channel)"
        )
        self._db.commit()
        limit = int(self.config.history_limit)
        for rowid, chan, ts, sender, target, text, kind in self._db.execute(
            "SELECT rowid, channel, ts, sender, target, text, kind FROM history "
            "ORDER BY ts ASC"
        ):
            hist = self._history[chan]
            hist.append(HistoryMessage(
                ts, sender, target, text, kind or "privmsg", msgid=f"h{rowid}"))
            while len(hist) > limit:
                hist.popleft()

    def _store(self, msg: HistoryMessage) -> None:
        chan = msg.target.lower()
        hist = self._history[chan]
        hist.append(msg)
        limit = int(self.config.history_limit)
        while len(hist) > limit:
            hist.popleft()
        if self._db is not None:
            try:
                cur = self._db.execute(
                    "INSERT INTO history(channel, ts, sender, target, text, kind)"
                    " VALUES (?,?,?,?,?,?)",
                    (chan, msg.ts, msg.sender, msg.target, msg.text, msg.kind),
                )
                msg.msgid = f"h{cur.lastrowid}"
                self._db.execute(
                    "DELETE FROM history WHERE rowid NOT IN "
                    "(SELECT rowid FROM history WHERE channel=? "
                    "ORDER BY ts DESC LIMIT ?)",
                    (chan, limit),
                )
                self._db.commit()
            except Exception:
                logger.debug("ircd: history persist failed", exc_info=True)
        elif not msg.msgid:
            self._msg_seq += 1
            msg.msgid = f"m{self._msg_seq}"
    # -- lifecycle ------------------------------------------------------

    async def start(self) -> "IrcDaemon":
        self._open_db()
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
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    async def _ping_loop(self) -> None:
        """Liveness sweep: PING idle clients, drop the long-silent.

        Without this, a daemon restart leaves every client believing it
        is still connected (half-open): sends vanish, no error surfaces.
        """
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
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
        except asyncio.CancelledError:
            pass

    def channel_names(self) -> list[str]:
        return sorted(self._display.get(k, k) for k in self._channels)

    def channel_history(self, channel: str, limit: int = 50) -> list[HistoryMessage]:
        hist = self._history.get(channel.lower(), ())
        return list(hist)[-max(0, limit) :]

    async def destroy_channel(self, channel: str, reason: str = "room closed") -> int:
        """PART every member and drop the channel + its history.

        Returns the number of members removed. Never raises.
        """
        key = channel.lower()
        async with self._lock:
            members = sorted(self._channels.pop(key, ()))
            self._display.pop(key, None)
            self._topics.pop(key, None)
            self._history.pop(key, None)
            if self._db is not None:
                try:
                    self._db.execute("DELETE FROM history WHERE channel=?", (key,))
                    self._db.commit()
                except Exception:
                    pass
        for nick in members:
            client = self._clients.get(nick)
            if client is None:
                continue
            client.channels.discard(key)
            await self._send(
                client, f":{client.nick}!{client.user}@mercury PART {channel} :{reason}"
            )
        return len(members)

    async def server_notice(self, channel: str, text: str) -> None:
        """Post a server-originated notice into a channel (stored + fanned)."""
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
        # IRCv3 message-tags: strip the @tag block before dispatch so a
        # labeled PRIVMSG still routes; stash +label for labeled-response.
        if line.startswith("@"):
            tagstr, _, line = line[1:].partition(" ")
            for part in tagstr.split(";"):
                k, _, v = part.partition("=")
                if k == "label" and v:
                    client.pending_label = v[:64]
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
            client.pass_attempted = True
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
        if cmd == "AUTHENTICATE":
            await self._cmd_authenticate(client, rest.strip(), listener, password)
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
        elif cmd == "CHATHISTORY":
            await self._cmd_chathistory(client, rest.strip())
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
        elif cmd == "DESTROY":
            await self._cmd_destroy(client, rest.strip())
        elif cmd == "QUIT":
            await self._quit(client, rest.lstrip(":") or "quit")
        elif cmd == "INVITE":
            await self._cmd_invite(client, rest.strip())
        elif cmd == "USERHOST" or cmd == "ISON":
            pass  # accepted, ignored
        else:
            await self._numeric(client, 421, cmd, "Unknown command")
    # -- commands --------------------------------------------------------

    def _who(self, client: _Client) -> str:
        return client.nick or "*"

    #: IRCv3 caps we actually honor (Goguma needs these for background
    #: messaging; each is implemented below, not merely advertised).
    _OFFERED_CAPS = (
        "sasl",
        "message-tags",
        "server-time",
        "batch",
        "echo-message",
        "labeled-response",
        "draft/chathistory",
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
            if "sasl" in ok:
                client.sasl = "negotiated"
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

    async def _cmd_authenticate(
        self, client: _Client, arg: str, listener: str, password: str
    ) -> None:
        """SASL PLAIN against the listener password (Goguma-style clients).

        Flow: ``AUTHENTICATE PLAIN`` → ``AUTHENTICATE +`` → client sends
        base64(``authzid\\0authcid\\0passwd``) → 903 + pass (or 904).
        ``AUTHENTICATE *`` aborts (906). Lenient: PLAIN is accepted even
        without a prior CAP REQ (small private network, no downgrade risk
        worth failing closed over).
        """
        import base64 as _b64

        who = self._who(client)
        token = arg.strip()
        if token == "*":
            client.sasl = None
            await self._numeric(client, 906, who, "SASL authentication aborted")
            return
        if client.sasl == "plain-pending":
            client.sasl = None
            label, client.pending_label = client.pending_label, None
            try:
                decoded = _b64.b64decode(token, validate=True).decode("utf-8", "replace")
            except Exception:
                decoded = ""
            parts = decoded.split("\x00")
            given = parts[-1] if parts else ""
            client.pass_attempted = True
            # Shape-only: field count + attempt length, never content.
            # (Trailing-NUL blobs have an empty last field → 904.)
            logger.info(
                "ircd: %s SASL blob fields=%d len=%d expected=%d -> %s",
                listener, len(parts), len(given), len(password),
                "903" if (not password or given == password) else "904",
            )
            tag = self._tags(client, label=label)
            name = self.config.server_name
            if not password or given == password:
                client.pass_ok = True
                await self._send(
                    client, tag + f":{name} 903 {who} :SASL authentication successful")
                # SASL-after-NICK/USER (the Goguma order): complete
                # registration now, same as the PASS-last path above.
                await self._maybe_register(client, listener, password)
            else:
                await self._send(
                    client, tag + f":{name} 904 {who} :SASL authentication failed")
            return
        if token.upper() == "PLAIN":
            client.sasl = "plain-pending"
            await self._send(client, "AUTHENTICATE +")
            return
        # Shape-only: a stray blob here could carry secret material, so
        # log the mechanism name only for known words, else just length.
        _mech = token.upper()
        _known = {
            "LOGIN", "EXTERNAL", "SCRAM-SHA-1", "SCRAM-SHA-256",
            "SCRAM-SHA-512", "OAUTHBEARER", "ECDSA-NIST256P-CHALLENGE",
        }
        logger.info(
            "ircd: %s SASL mechanism=%s len=%d -> 904",
            listener, _mech if _mech in _known else "blob-like", len(token),
        )
        await self._numeric(client, 904, who, "SASL authentication failed")

    async def _cmd_nick(
        self, client: _Client, nick: str, listener: str, password: str
    ) -> None:
        nick = nick.strip().lstrip(":")[:32]
        if not nick or not re.fullmatch(r"[A-Za-z0-9_\-\[\]\\`^{}|]+", nick):
            await self._numeric(client, 432, nick or "*", "Erroneous nickname")
            return
        key = nick.lower()
        if key in self._clients and self._clients[key] is not client:
            # Server semantics: the newest connection wins (reclaim).
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
            # No 464 before the client has attempted auth: clients latch
            # the first 464 as fatal and ignore a later 903/001 (Goguma
            # sends NICK/USER before its password). Wrong-password
            # attempts still get their 464 from the PASS/SASL handler.
            if not client.pass_attempted:
                # ...but pure silence freezes clients (and browsers
                # pointed at the IRC port) with zero feedback. A NOTICE
                # is automaton-safe: it trips no fatal latch.
                if not client.pass_noticed:
                    client.pass_noticed = True
                    await self._send(
                        client,
                        f":{self.config.server_name} NOTICE * :This server needs PASS "
                        "(the server password from setup) before login "
                        "completes — set it as the server password and "
                        "reconnect")
                return
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
        await self._send(client, f":{name} 004 {nick} {name} mercury-ircd o o")
        await self._send(
            client,
            f":{name} 005 {nick} CHANTYPES=# NICKLEN=32 "
            f"TOPICLEN=256 :are supported by this server",
        )
        # Clients consider login complete at end-of-MOTD; without 376
        # (or 422) Goguma waits forever and reconnects in a loop.
        await self._numeric(client, 422, nick, "MOTD File is missing")

    async def _cmd_join(self, client: _Client, arg: str) -> None:
        if not arg:
            await self._numeric(client, 461, "JOIN", "Not enough parameters")
            return
        joined = []
        async with self._lock:
            for chan in arg.split(","):
                chan = chan.split(" ", 1)[0].strip()
                if not chan.startswith("#") or len(chan) < 2:
                    await self._numeric(client, 403, chan, "No such channel")
                    continue
                key = chan.lower()
                self._channels[key].add(client.nick.lower())
                self._display.setdefault(key, chan)
                client.channels.add(key)
                joined.append((key, self._display[key]))
        for key, display in joined:
            await self._emit_join(client, key, display)

    async def _emit_join(self, peer: _Client, key: str, display: str) -> None:
        """JOIN + topic + names + history replay for a new member."""
        await self._send(
            peer, f":{peer.nick}!{peer.user}@mercury JOIN {display}"
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
        # Server replay: recent history on every JOIN.
        for msg in self.channel_history(
            display, limit=int(self.config.history_limit)
        ):
            await self._send(
                peer,
                self._tags(peer, ts=msg.ts, msgid=msg.msgid)
                + f":{msg.sender}!relay@mercury {msg.kind.upper()} "
                f"{display} :{msg.text}",
            )

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

    async def _cmd_chathistory(self, client: _Client, arg: str) -> None:
        """draft/chathistory LATEST/AFTER/BEFORE against the local backlog."""
        parts = arg.split()
        sub = parts[0].upper() if parts else ""
        if sub not in ("LATEST", "AFTER", "BEFORE"):
            await self._numeric(client, 410, "CHATHISTORY", "Invalid subcommand")
            return
        if len(parts) < 4:
            await self._numeric(client, 461, "CHATHISTORY", "Not enough parameters")
            return
        _, target, anchor, raw_limit = parts[:4]
        try:
            limit = max(1, min(int(raw_limit), 100))
        except ValueError:
            limit = 20
        key = target.lower()
        if key not in self._channels:
            await self._numeric(client, 403, target, "No such channel")
            return
        hist = list(self._history.get(key, ()))
        if sub == "LATEST":
            if anchor == "*":
                msgs = hist[-limit:]
            else:
                at = [i for i, m in enumerate(hist) if m.msgid == anchor]
                msgs = hist[max(0, at[-1] - limit + 1):at[-1] + 1] if at else []
        elif sub == "AFTER":
            at = [i for i, m in enumerate(hist) if m.msgid == anchor]
            msgs = hist[at[-1] + 1:at[-1] + 1 + limit] if at else []
        else:  # BEFORE
            at = [i for i, m in enumerate(hist) if m.msgid == anchor]
            msgs = hist[max(0, at[-1] - limit):at[-1]] if at else []
        name = self.config.server_name
        display = self._display.get(key, target)
        framed = "batch" in client.caps
        ref = ""
        if framed:
            self._batch_seq += 1
            ref = f"ch{self._batch_seq}"
            await self._send(
                client, f":{name} BATCH +{ref} draft/chathistory {display}")
        for msg in msgs:
            await self._send(
                client,
                self._tags(client, ts=msg.ts, msgid=msg.msgid)
                + f":{msg.sender}!relay@mercury {msg.kind.upper()} "
                f"{display} :{msg.text}",
            )
        if framed:
            await self._send(client, f":{name} BATCH -{ref}")

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
            f":{client.nick}!{client.user}@mercury INVITE {peer.nick} :{display}",
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
                        f":{peer.nick}!{peer.user}@mercury JOIN {display}",
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
                    # Keep empty channels (and their history) — the server
                    # replays them on rejoin; only destroy_channel deletes.
                    pass
                await self._send(
                    client,
                    f":{client.nick}!{client.user}@mercury PART "
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
        label, client.pending_label = client.pending_label, None
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
            await self._fanout(msg)
            if "echo-message" in client.caps:
                await self._send(
                    client,
                    self._tags(client, ts=msg.ts, msgid=msg.msgid, label=label)
                    + f":{sender}!{client.user}@mercury {kind.upper()} "
                    f"{display} :{text}",
                )
        else:
            peer = self._clients.get(target.lower())
            if peer is None or not peer.registered:
                await self._numeric(client, 401, target, "No such nick")
                return
            now = time.time()
            await self._send(
                peer,
                self._tags(peer, ts=now)
                + f":{sender}!{client.user}@mercury {kind.upper()} "
                f"{peer.nick} :{text}",
            )
            if "echo-message" in client.caps:
                await self._send(
                    client,
                    self._tags(client, ts=now, label=label)
                    + f":{sender}!{client.user}@mercury {kind.upper()} "
                    f"{peer.nick} :{text}",
                )
            if self.on_privmsg is not None and kind == "privmsg":
                try:
                    self.on_privmsg(sender, peer.nick, text)
                except Exception:
                    logger.debug("ircd: on_privmsg hook failed", exc_info=True)

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

    async def _fanout(self, msg: HistoryMessage) -> None:
        key = msg.target.lower()
        self._store(msg)
        members = sorted(self._channels.get(key, ()))
        display = self._display.get(key, msg.target)
        body = (
            f":{msg.sender}!relay@mercury {msg.kind.upper()} "
            f"{display} :{msg.text}"
        )
        for nick in members:
            if nick == msg.sender.lower():
                continue  # echo-message path in _cmd_msg covers the sender
            peer = self._clients.get(nick)
            if peer is not None:
                await self._send(
                    peer,
                    self._tags(peer, ts=msg.ts, msgid=msg.msgid) + body,
                )

    async def _quit(self, client: _Client, reason: str) -> None:
        key = client.nick.lower() if client.nick else ""
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

    def _tags(
        self,
        client: _Client,
        *,
        ts: float | None = None,
        msgid: str = "",
        label: str | None = None,
    ) -> str:
        """IRCv3 tag prefix, gated on negotiated caps (never sent raw)."""
        parts: list[str] = []
        if ts is not None and "server-time" in client.caps:
            parts.append(f"time={self._iso_time(ts)}")
        if msgid and "message-tags" in client.caps:
            parts.append(f"msgid={msgid}")
        if label and "labeled-response" in client.caps:
            parts.append(f"label={label}")
        return ("@" + ";".join(parts) + " ") if parts else ""

    async def _send(self, client: _Client, line: str) -> None:
        try:
            async with client.send_lock:
                client.writer.write((line + "\r\n").encode("utf-8", "replace"))
                await client.writer.drain()
        except Exception:
            pass

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
        history_limit=_pick("history_limit", 200),
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
    parser.add_argument("--history-limit", type=int, default=None)
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
