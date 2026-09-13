"""Mercury observatory IRC daemon (replaces the Matrix/tuwunel stack).

One small stdlib-only asyncio server with two listeners on the same
channel state:

- **agent listener** (default ``127.0.0.1:6669``): the gateway and its
  agents connect here. Localhost-bound by default.
- **bouncer listener** (default ``127.0.0.1:6670``): the user connects
  here with any IRC client. Bouncer semantics: the daemon keeps the
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


@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    agent_port: int = 6669
    bouncer_host: str = "127.0.0.1"
    bouncer_port: int = 6670
    server_name: str = "mercury.local"
    password: str = ""  # required PASS on the bouncer listener when set
    agent_password: str = ""  # required PASS on the agent listener when set
    history_limit: int = 200
    state_dir: Path | str = ""
    network_name: str = "mercury"


class _Client:
    __slots__ = ("reader", "writer", "nick", "user", "realname",
                 "registered", "pass_ok", "oper", "addr", "channels", "send_lock")

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, addr: str):
        self.reader = reader
        self.writer = writer
        self.nick = ""
        self.user = ""
        self.realname = ""
        self.registered = False
        self.pass_ok = False
        self.oper = False
        self.addr = addr
        self.channels: set[str] = set()  # folded channel keys
        self.send_lock = asyncio.Lock()


class IrcDaemon:
    """One IRC network: channel state + history + two listeners."""

    def __init__(self, config: DaemonConfig | None = None,
                 on_privmsg: Callable[[str, str, str], None] | None = None):
        self.config = config or DaemonConfig()
        self.on_privmsg = on_privmsg  # (sender, target, text) hook, e.g. router
        self._clients: dict[str, _Client] = {}  # folded nick -> client
        self._channels: dict[str, set[str]] = defaultdict(set)  # folded -> nicks
        self._display: dict[str, str] = {}  # folded channel -> display name
        self._topics: dict[str, tuple[str, str, float]] = {}
        self._history: dict[str, deque[HistoryMessage]] = defaultdict(deque)
        self._servers: list[asyncio.AbstractServer] = []
        self._db: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

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
        for (chan, ts, sender, target, text, kind) in self._db.execute(
            "SELECT channel, ts, sender, target, text, kind FROM history "
            "ORDER BY ts ASC"
        ):
            hist = self._history[chan]
            hist.append(HistoryMessage(ts, sender, target, text, kind or "privmsg"))
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
                self._db.execute(
                    "INSERT INTO history(channel, ts, sender, target, text, kind)"
                    " VALUES (?,?,?,?,?,?)",
                    (chan, msg.ts, msg.sender, msg.target, msg.text, msg.kind),
                )
                self._db.execute(
                    "DELETE FROM history WHERE rowid NOT IN "
                    "(SELECT rowid FROM history WHERE channel=? "
                    "ORDER BY ts DESC LIMIT ?)",
                    (chan, limit),
                )
                self._db.commit()
            except Exception:
                logger.debug("ircd: history persist failed", exc_info=True)

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> "IrcDaemon":
        self._open_db()
        cfg = self.config
        agent = await asyncio.start_server(
            lambda r, w: self._handle(r, w, listener="agent"),
            cfg.host, cfg.agent_port,
        )
        bouncer = await asyncio.start_server(
            lambda r, w: self._handle(r, w, listener="bouncer"),
            cfg.bouncer_host, cfg.bouncer_port,
        )
        self._servers = [agent, bouncer]
        logger.info("ircd: agent %s:%d bouncer %s:%d (%s)",
                    cfg.host, cfg.agent_port,
                    cfg.bouncer_host, cfg.bouncer_port, cfg.network_name)
        return self

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers = []
        for client in list(self._clients.values()):
            try:
                client.writer.close()
            except Exception:
                pass
        self._clients.clear()
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    def channel_names(self) -> list[str]:
        return sorted(self._display.get(k, k) for k in self._channels)

    def channel_history(self, channel: str, limit: int = 50) -> list[HistoryMessage]:
        hist = self._history.get(channel.lower(), ())
        return list(hist)[-max(0, limit):]

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
            await self._send(client,
                             f":{client.nick}!{client.user}@mercury PART {channel} :{reason}")
        return len(members)

    async def server_notice(self, channel: str, text: str) -> None:
        """Post a server-originated notice into a channel (stored + fanned)."""
        msg = HistoryMessage(time.time(), self.config.server_name,
                             channel, text, kind="notice")
        await self._fanout(msg)

    # -- connection handling --------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter, listener: str) -> None:
        peer = writer.get_extra_info("peername")
        addr = str(peer[0]) if peer else "?"
        client = _Client(reader, writer, addr)
        password = (self.config.agent_password if listener == "agent"
                    else self.config.password)
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

    async def _line(self, client: _Client, line: str,
                    listener: str, password: str) -> None:
        if " " in line:
            cmd, rest = line.split(" ", 1)
        else:
            cmd, rest = line, ""
        cmd = cmd.upper()
        if cmd == "PASS":
            client.pass_ok = (rest.strip().lstrip(":") == password)
            if password and not client.pass_ok:
                await self._numeric(client, 464, "*", "Password incorrect")
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
        if not client.registered:
            return
        if cmd == "PING":
            await self._send(client, f":{self.config.server_name} PONG "
                                    f"{self.config.server_name} :{rest.lstrip(':')}")
        elif cmd == "PONG":
            pass
        elif cmd == "JOIN":
            await self._cmd_join(client, rest.strip())
        elif cmd == "PART":
            await self._cmd_part(client, rest.strip())
        elif cmd == "PRIVMSG":
            await self._cmd_msg(client, rest, kind="privmsg")
        elif cmd == "NOTICE":
            await self._cmd_msg(client, rest, kind="notice")
        elif cmd == "TOPIC":
            await self._cmd_topic(client, rest.strip())
        elif cmd == "NAMES":
            await self._cmd_names(client, rest.strip().lstrip(":"))
        elif cmd == "WHO":
            await self._cmd_who(client, rest.strip().lstrip(":"))
        elif cmd == "MODE":
            target = rest.split(" ", 1)[0] if rest else ""
            await self._numeric(client, 324, f"{client.nick} {target} +",
                                "End of MODE")
        elif cmd == "OPER":
            await self._cmd_oper(client, rest.strip())
        elif cmd == "DESTROY":
            await self._cmd_destroy(client, rest.strip())
        elif cmd == "QUIT":
            await self._quit(client, rest.lstrip(":") or "quit")
        elif cmd == "USERHOST" or cmd == "ISON":
            pass  # accepted, ignored
        else:
            await self._numeric(client, 421, cmd, "Unknown command")

    # -- commands --------------------------------------------------------

    async def _cmd_nick(self, client: _Client, nick: str,
                        listener: str, password: str) -> None:
        nick = nick.strip().lstrip(":")[:32]
        if not nick or not re.fullmatch(r"[A-Za-z0-9_\-\[\]\\`^{}|]+", nick):
            await self._numeric(client, 432, nick or "*",
                                "Erroneous nickname")
            return
        key = nick.lower()
        if key in self._clients and self._clients[key] is not client:
            # Bouncer semantics: the newest connection wins (reclaim).
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

    async def _maybe_register(self, client: _Client,
                              listener: str, password: str) -> None:
        if client.registered or not client.nick or not client.user:
            return
        if password and not client.pass_ok:
            await self._numeric(client, 464, "*", "Password incorrect")
            return
        client.registered = True
        name = self.config.server_name
        nick = client.nick
        await self._send(client, f":{name} 001 {nick} :Welcome to {name}, {nick}")
        await self._send(client, f":{name} 002 {nick} :Your host is {name}")
        await self._send(client, f":{name} 003 {nick} :This server was created for Mercury")
        await self._send(client, f":{name} 004 {nick} {name} mercury-ircd o o")
        await self._send(client, f":{name} 005 {nick} CHANTYPES=# NICKLEN=32 "
                                 f"TOPICLEN=256 :are supported by this server")

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
            await self._send(client, f":{client.nick}!{client.user}@mercury JOIN {display}")
            topic = self._topics.get(key)
            if topic is not None:
                text, setter, _ts = topic
                await self._numeric(client, 332, f"{client.nick} {display} :{text}")
            else:
                await self._numeric(client, 331, f"{client.nick} {display}",
                                    "No topic is set")
            await self._send_names(client, key, display)
            # Bouncer replay: recent history on every JOIN.
            for msg in self.channel_history(display,
                                            limit=int(self.config.history_limit)):
                await self._send(
                    client,
                    f":{msg.sender}!relay@mercury {msg.kind.upper()} "
                    f"{display} :{msg.text}")

    async def _send_names(self, client: _Client, key: str, display: str) -> None:
        members = sorted(self._channels.get(key, ()))
        nicks = " ".join(self._clients[n].nick for n in members
                         if n in self._clients)
        await self._numeric(client, 353, f"{client.nick} = {display}", nicks)
        await self._numeric(client, 366, f"{client.nick} {display}",
                            "End of NAMES list")

    async def _cmd_names(self, client: _Client, arg: str) -> None:
        if not arg:
            return
        for chan in arg.split(","):
            key = chan.strip().lower()
            if key in self._channels:
                await self._send_names(client, key,
                                       self._display.get(key, chan.strip()))

    async def _cmd_who(self, client: _Client, arg: str) -> None:
        key = (arg.split(" ", 1)[0] if arg else "").lower()
        members = sorted(self._channels.get(key, ())) if key else []
        for nick in members:
            c = self._clients.get(nick)
            if c is None:
                continue
            await self._numeric(client, 352,
                                f"{client.nick} {self._display.get(key, arg)} {c.user} mercury mercury {c.nick} H",
                                f"0 {c.realname or c.nick}")
        await self._numeric(client, 315, f"{client.nick} {arg}",
                            "End of WHO list")

    def _oper_password(self) -> str:
        return self.config.agent_password or self.config.password

    async def _cmd_oper(self, client: _Client, arg: str) -> None:
        """OPER <password> — grant channel-destroy rights to the gateway bot."""
        secret = arg.split(" ", 1)[0].lstrip(":")
        want = self._oper_password()
        if want and secret == want:
            client.oper = True
            await self._numeric(client, 381, client.nick,
                                "You are now an IRC operator")
        else:
            await self._numeric(client, 464, client.nick,
                                "Password incorrect")

    async def _cmd_destroy(self, client: _Client, arg: str) -> None:
        """DESTROY #channel :reason — oper-only room kill for /exit."""
        if not client.oper:
            await self._numeric(client, 481, client.nick,
                                "Permission Denied - You're not an IRC operator")
            return
        channel = arg.split(" ", 1)[0].strip()
        if not channel.startswith("#"):
            await self._numeric(client, 403, channel or "*",
                                "No such channel")
            return
        await self.destroy_channel(channel, reason="room closed (/exit)")
        await self._numeric(client, 200, f"{client.nick} {channel}",
                            "Channel destroyed")

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
                    await self._numeric(client, 442, chan.strip(),
                                        "You're not on that channel")
                    continue
                members.discard(client.nick.lower())
                client.channels.discard(key)
                if not members:
                    # Keep empty channels (and their history) — the bouncer
                    # replays them on rejoin; only destroy_channel deletes.
                    pass
                await self._send(client,
                                 f":{client.nick}!{client.user}@mercury PART "
                                 f"{self._display.get(key, chan.strip())} :{reason}")

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
            await self._numeric(client, 411, "No recipient given",
                                "No recipient")
            return
        target, text = split
        if not target or not text:
            await self._numeric(client, 412, "No text to send", "No text")
            return
        sender = client.nick
        if target.startswith("#"):
            key = target.lower()
            async with self._lock:
                members = self._channels.get(key)
                if members is None:
                    await self._numeric(client, 403, target, "No such channel")
                    return
                if sender.lower() not in members:
                    await self._numeric(client, 404, target,
                                        "Cannot send to channel")
                    return
                display = self._display.get(key, target)
            msg = HistoryMessage(time.time(), sender, display, text, kind=kind)
            await self._fanout(msg)
            if self.on_privmsg is not None and kind == "privmsg":
                try:
                    self.on_privmsg(sender, display, text)
                except Exception:
                    logger.debug("ircd: on_privmsg hook failed", exc_info=True)
        else:
            peer = self._clients.get(target.lower())
            if peer is None or not peer.registered:
                await self._numeric(client, 401, target, "No such nick")
                return
            await self._send(peer, f":{sender}!{client.user}@mercury "
                                   f"{kind.upper()} {peer.nick} :{text}")
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
                await self._numeric(client, 331, f"{client.nick} {display}",
                                    "No topic is set")
            else:
                await self._numeric(client, 332,
                                    f"{client.nick} {display} :{topic[0]}")
            return
        if client.nick.lower() not in members:
            await self._numeric(client, 442, display,
                                "You're not on that channel")
            return
        self._topics[key] = (text[:256], client.nick, time.time())
        notice = HistoryMessage(time.time(), client.nick, display,
                                f"topic: {text[:256]}", kind="notice")
        await self._fanout(notice)

    async def _fanout(self, msg: HistoryMessage) -> None:
        key = msg.target.lower()
        self._store(msg)
        members = sorted(self._channels.get(key, ()))
        line = (f":{msg.sender}!relay@mercury {msg.kind.upper()} "
                f"{self._display.get(key, msg.target)} :{msg.text}")
        for nick in members:
            if nick == msg.sender.lower():
                continue
            peer = self._clients.get(nick)
            if peer is not None:
                await self._send(peer, line)

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

    async def _send(self, client: _Client, line: str) -> None:
        try:
            async with client.send_lock:
                client.writer.write((line + "\r\n").encode("utf-8", "replace"))
                await client.writer.drain()
        except Exception:
            pass

    async def _numeric(self, client: _Client, code: int, tail: str, text: str) -> None:
        await self._send(client, f":{self.config.server_name} {code:03d} "
                                 f"{tail} :{text}")


async def serve_forever(config: DaemonConfig) -> None:
    """Run the daemon until cancelled (systemd unit entry point)."""
    daemon = await IrcDaemon(config).start()
    try:
        await asyncio.Event().wait()
    finally:
        await daemon.stop()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Mercury observatory IRC daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--agent-port", type=int, default=6669)
    parser.add_argument("--bouncer-host", default="127.0.0.1")
    parser.add_argument("--bouncer-port", type=int, default=6670)
    parser.add_argument("--server-name", default="mercury.local")
    parser.add_argument("--password", default="")
    parser.add_argument("--agent-password", default="")
    parser.add_argument("--history-limit", type=int, default=200)
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    config = DaemonConfig(
        host=args.host, agent_port=args.agent_port,
        bouncer_host=args.bouncer_host, bouncer_port=args.bouncer_port,
        server_name=args.server_name, password=args.password,
        agent_password=args.agent_password,
        history_limit=args.history_limit, state_dir=args.state_dir,
    )
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve_forever(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
