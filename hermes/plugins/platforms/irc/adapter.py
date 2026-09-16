"""
IRC Platform Adapter for Mercury.

A plugin-based gateway adapter that connects to an IRC server and relays
messages to/from the Mercury agent.  Zero external dependencies — uses
Python's stdlib asyncio for the IRC protocol.

Configuration in config.yaml::

    gateway:
      platforms:
        irc:
          enabled: true
          extra:
            server: irc.libera.chat
            port: 6697
            nickname: mercury-bot
            channel: "#mercury"
            use_tls: true
            server_password: ""       # optional server password
            nickserv_password: ""     # optional NickServ identification
            allowed_users: []         # empty = allow all, or list of nicks
            max_message_length: 450   # IRC line limit (safe default)

Or via environment variables (overrides config.yaml):
    IRC_SERVER, IRC_PORT, IRC_NICKNAME, IRC_CHANNEL, IRC_USE_TLS,
    IRC_SERVER_PASSWORD, IRC_NICKSERV_PASSWORD
"""

import asyncio
import logging
import os

from mercury_cli.config import get_env_value
import re
import ssl
import time
from typing import Any, Dict, List, Optional

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import: BasePlatformAdapter and friends live in the main repo.
# We import at function/class level to avoid import errors when the plugin
# is discovered but the gateway hasn't been fully initialised yet.
# ---------------------------------------------------------------------------

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform


# ---------------------------------------------------------------------------
# IRC protocol helpers
# ---------------------------------------------------------------------------

SILENCE_LIMIT = 210.0  # reconnect when the server says nothing this long
WATCHDOG_POLL = 60.0  # silence-check cadence (server PINGs every 60s)


def _enable_keepalive(writer) -> None:
    """TCP keepalive on an IRC connection (best-effort, never raises)."""
    try:
        sock = writer.get_extra_info("socket") if writer is not None else None
        if sock is None:
            return
        import socket as _socket

        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
        for opt, val in (
            (_socket.TCP_KEEPIDLE, 60),
            (_socket.TCP_KEEPINTVL, 30),
            (_socket.TCP_KEEPCNT, 3),
        ):
            try:
                sock.setsockopt(_socket.IPPROTO_TCP, opt, val)
            except Exception:
                pass
    except Exception:
        pass

def _parse_irc_message(raw: str) -> dict:
    """Parse a raw IRC protocol line into components.

    Returns dict with keys: prefix, command, params.
    """
    prefix = ""
    trailing = ""

    if raw.startswith(":"):
        try:
            prefix, raw = raw[1:].split(" ", 1)
        except ValueError:
            prefix = raw[1:]
            raw = ""

    if " :" in raw:
        raw, trailing = raw.split(" :", 1)

    parts = raw.split()
    command = parts[0] if parts else ""
    params = parts[1:] if len(parts) > 1 else []
    if trailing:
        params.append(trailing)

    return {"prefix": prefix, "command": command, "params": params}


def _extract_nick(prefix: str) -> str:
    """Extract nickname from IRC prefix (nick!user@host)."""
    return prefix.split("!")[0] if "!" in prefix else prefix


# ---------------------------------------------------------------------------
# IRC Adapter
# ---------------------------------------------------------------------------

class IRCAdapter(BasePlatformAdapter):
    """Async IRC adapter implementing the BasePlatformAdapter interface.

    This class is instantiated by the adapter_factory passed to
    register_platform().
    """

    def __init__(self, config, **kwargs):
        platform = Platform("irc")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Connection settings (env vars override config.yaml)
        self.server = get_env_value("IRC_SERVER") or extra.get("server", "")
        env_port = get_env_value("IRC_PORT") or None
        self.use_tls = (
            (get_env_value("IRC_USE_TLS") or "").lower() in {"1", "true", "yes"}
            if get_env_value("IRC_USE_TLS")
            else extra.get("use_tls", True)
        )
        try:
            self.port = int(env_port or extra.get("port") or (6697 if self.use_tls else 6667))
        except (ValueError, TypeError):
            self.port = 6697 if self.use_tls else 6667
        self.nickname = get_env_value("IRC_NICKNAME") or extra.get("nickname", "mercury-bot")
        # One name for bot and room: an explicit channel wins (back-compat),
        # otherwise `#nick`.
        self.channel = (
            get_env_value("IRC_CHANNEL")
            or extra.get("channel", "")
            or _derive_channel(self.nickname)
        )
        self.server_password = _get_scoped_secret("IRC_SERVER_PASSWORD") or extra.get("server_password", "")
        self.nickserv_password = _get_scoped_secret("IRC_NICKSERV_PASSWORD") or extra.get("nickserv_password", "")
        self.oper_password = _get_scoped_secret("IRC_OPER_PASSWORD") or extra.get("oper_password", "") or self.server_password
        # Observability rooms: extra agent channels the bot joins dynamically
        # (/spawn rooms, #parent-child subagent rooms). Managed channels
        # never require nick-addressing: every message there is for the agent.
        self.extra_channels: set[str] = set()

        # Auth
        self.allowed_users: list = extra.get("allowed_users", [])
        # IRC nicks are case-insensitive — normalise for lookups
        self._allowed_users_lower: set = {u.lower() for u in self.allowed_users if isinstance(u, str)}

        # IRC limits
        max_msg = extra.get("max_message_length")
        if max_msg is None:
            try:
                from gateway.platform_registry import platform_registry
                entry = platform_registry.get("irc")
                if entry and entry.max_message_length:
                    max_msg = entry.max_message_length
            except Exception:
                pass
        self.max_message_length = int(max_msg or 450)

        # Runtime state
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._handler_task: Optional[asyncio.Task] = None
        self._line_queue: Optional[asyncio.Queue] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_inbound = 0.0
        self._registered = False  # IRC registration complete
        self._registration_event = asyncio.Event()
        self._current_nick = self.nickname

    @property
    def name(self) -> str:
        return "IRC"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the IRC server, register, and join the channel."""
        if not self.server or not self.channel:
            logger.error("IRC: server and channel must be configured")
            self._set_fatal_error(
                "config_missing",
                "IRC_SERVER and IRC_CHANNEL must be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from using the same IRC identity
        try:
            from gateway.status import acquire_scoped_lock, release_scoped_lock
            lock_key = f"{self.server}:{self.nickname}"
            if not acquire_scoped_lock("irc", lock_key):
                logger.error("IRC: %s@%s already in use by another profile", self.nickname, self.server)
                self._set_fatal_error("lock_conflict", "IRC identity in use by another profile", retryable=False)
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None  # status module not available (e.g. tests)

        try:
            ssl_ctx = None
            if self.use_tls:
                ssl_ctx = ssl.create_default_context()

            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.server, self.port, ssl=ssl_ctx),
                timeout=30.0,
            )
        except Exception as e:
            logger.error("IRC: failed to connect to %s:%s — %s", self.server, self.port, e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        # IRC registration sequence
        if self.server_password:
            await self._send_raw(f"PASS {self.server_password}")
        await self._send_raw(f"NICK {self.nickname}")
        await self._send_raw(f"USER {self.nickname} 0 * :Mercury")

        # Start receive loop + ordered handler (PINGs bypass the queue)
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._line_queue = asyncio.Queue()
        self._handler_task = asyncio.create_task(self._handle_task())

        # Wait for registration (001 RPL_WELCOME) with timeout
        try:
            await asyncio.wait_for(self._registration_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("IRC: registration timed out")
            await self.disconnect()
            self._set_fatal_error("registration_timeout", "IRC server did not send RPL_WELCOME", retryable=True)
            return False

        # NickServ identification
        if self.nickserv_password:
            await self._send_raw(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}")
            await asyncio.sleep(2)  # Give NickServ time to process

        # Join the gateway channel plus any managed agent rooms. IRC creates
        # a channel on first JOIN; the observatory resync pass re-adds live
        # rooms after a reconnect via join_channel().
        await self._send_raw(f"JOIN {self.channel}")
        for extra in sorted(self.extra_channels):
            await self._send_raw(f"JOIN {extra}")

        # OPER for the observatory /exit room kill (no-op when unconfigured).
        if self.oper_password:
            try:
                await self._send_raw(f"OPER {self.oper_password}")
            except Exception:
                logger.debug("IRC: OPER failed", exc_info=True)

        try:
            from observatory.rooms import set_bot_sink, set_event_loop
            set_bot_sink(self)
            try:
                import asyncio as _asyncio

                set_event_loop(_asyncio.get_running_loop())
            except Exception:
                pass
            # Post-connect resync: join live state channels, drain the
            # frame queue, replay the exit journal, resume omp handles.
            # Fire-and-forget (idempotent, never breaks connect).
            try:
                from observatory.platform_hook import boot_resync as _resync
                asyncio.create_task(_resync())
            except Exception:
                logger.warning("IRC: resync schedule skipped", exc_info=True)
        except Exception:
            logger.debug("IRC: bot-sink register skipped", exc_info=True)
        logger.info("IRC: connected to %s:%s as %s, joined %s", self.server, self.port, self._current_nick, self.channel)
        # Plugin-registered native handlers (ctx.register_platform_handler).
        self._wire_plugin_handlers(None)
        _enable_keepalive(self._writer)
        self._last_inbound = time.monotonic()
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._silence_watchdog())
        return True

    async def disconnect(self) -> None:
        """Quit and close the connection."""
        # Release the scoped lock so another profile can use this identity
        if getattr(self, "_lock_key", None):
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("irc", self._lock_key)
            except Exception:
                pass
        self._mark_disconnected()
        if self._writer and not self._writer.is_closing():
            try:
                await self._send_raw("QUIT :Mercury shutting down")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._handler_task and not self._handler_task.done():
            self._handler_task.cancel()
            try:
                await self._handler_task
            except asyncio.CancelledError:
                pass
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

        self._reader = None
        self._writer = None
        self._registered = False
        self._registration_event.clear()
        try:
            from observatory.rooms import get_bot_sink, set_bot_sink
            if get_bot_sink() is self:
                set_bot_sink(None)
        except Exception:
            pass

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        if not self._writer or self._writer.is_closing():
            return SendResult(success=False, error="Not connected")

        target = chat_id  # channel name or nick for DMs
        # Per-agent identity first: rooms with a live identity speak as
        # their own nick (vm_charlie, not vm_gateway). All-or-nothing
        # per message (a split identity looks worse than a fallback).
        try:
            from observatory import identity as _identity

            if _identity.get_pool().get(target) is not None:
                lines = self._split_message(content, target)
                ok = True
                for line in lines:
                    ok = await _identity.send_as_identity(target, line) and ok
                    await asyncio.sleep(0.3)
                if ok:
                    return SendResult(
                        success=True, message_id=str(int(time.time() * 1000)))
        except Exception:
            logger.debug("IRC: identity send failed, using main bot",
                         exc_info=True)
        lines = self._split_message(content, target)

        for line in lines:
            try:
                await self._send_raw(f"PRIVMSG {target} :{line}")
                # Basic rate limiting to avoid excess flood
                await asyncio.sleep(0.3)
            except Exception as e:
                return SendResult(success=False, error=str(e))

        return SendResult(success=True, message_id=str(int(time.time() * 1000)))
    # ── Observatory rooms (BotSink surface for observatory.rooms) ──────────

    def managed_channels(self) -> set[str]:
        """Channels that never require nick-addressing (gateway + agent rooms)."""
        return {self.channel.lower(), *(c.lower() for c in self.extra_channels)}

    def is_managed(self, target: str) -> bool:
        return bool(target) and target.lower() in self.managed_channels()

    async def join_channel(self, channel: str) -> bool:
        """JOIN an agent room now (and on every reconnect). Never raises."""
        if channel:
            self.extra_channels.add(channel)
        if not self._writer or self._writer.is_closing():
            return channel in self.extra_channels
        try:
            await self._send_raw(f"JOIN {channel}")
            return True
        except Exception:
            logger.debug("IRC: join %s failed", channel, exc_info=True)
            return False

    async def invite_user(self, nick: str, channel: str) -> bool:
        """INVITE a nick to a room (phone surfaces it as a tap). Never raises."""
        if not self._writer or self._writer.is_closing():
            return False
        try:
            await self._send_raw(f"INVITE {nick} :{channel}")
            return True
        except Exception:
            logger.debug("IRC: invite %s to %s failed", nick, channel, exc_info=True)
            return False

    async def part_channel(self, channel: str) -> bool:
        """PART an agent room. Never raises."""
        self.extra_channels.discard(channel)
        if not self._writer or self._writer.is_closing():
            return True
        try:
            await self._send_raw(f"PART {channel} :room closed")
            return True
        except Exception:
            logger.debug("IRC: part %s failed", channel, exc_info=True)
            return False

    async def say(self, channel: str, text: str) -> bool:
        """PRIVMSG into a room (BotSink naming for observatory.rooms)."""
        result = await self.send(channel, text)
        return bool(getattr(result, "success", False))

    async def destroy_channel(self, channel: str) -> bool:
        """Server-side room kill for /exit (OPER DESTROY); PART fallback."""
        if self._writer and not self._writer.is_closing():
            try:
                await self._send_raw(f"DESTROY {channel} :room closed (/exit)")
                await asyncio.sleep(0.5)
            except Exception:
                logger.debug("IRC: destroy %s failed", channel, exc_info=True)
        return await self.part_channel(channel)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """IRC has no typing indicator — no-op."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_channel = chat_id.startswith("#") or chat_id.startswith("&")
        return {
            "name": chat_id,
            "type": "group" if is_channel else "dm",
        }

    # ── Message splitting ─────────────────────────────────────────────────

    def _split_message(self, content: str, target: str) -> List[str]:
        """Split a long message into IRC-safe chunks.

        IRC has a ~512 byte line limit.  After accounting for protocol
        overhead (``PRIVMSG <target> :``), we split content into chunks.
        """
        # Strip markdown formatting that doesn't render in IRC
        content = self._strip_markdown(content)

        overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2  # +2 for \r\n
        max_bytes = 510 - overhead
        user_limit = self.max_message_length

        lines: List[str] = []
        for paragraph in content.split("\n"):
            if not paragraph.strip():
                continue
            while True:
                para_bytes = paragraph.encode("utf-8")
                limit = min(user_limit, max_bytes)
                if len(para_bytes) <= limit:
                    if paragraph.strip():
                        lines.append(paragraph)
                    break
                # Binary search for a safe character boundary <= limit
                low, high = 1, len(paragraph)
                best = 0
                while low <= high:
                    mid = (low + high) // 2
                    if len(paragraph[:mid].encode("utf-8")) <= limit:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                split_at = best
                # Prefer a space boundary
                space = paragraph.rfind(" ", 0, split_at)
                if space > split_at // 3:
                    split_at = space
                lines.append(paragraph[:split_at].rstrip())
                paragraph = paragraph[split_at:].lstrip()

        return lines if lines else [""]

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Convert basic markdown to plain text for IRC."""
        # Bold: **text** or __text__ → text
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        # Italic: *text* or _text_ → text
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
        # Inline code: `text` → text
        text = re.sub(r"`(.+?)`", r"\1", text)
        # Code blocks: ```...``` → content
        text = re.sub(r"```\w*\n?", "", text)
        # Images: ![alt](url) → url  (must come BEFORE links)
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)
        # Links: [text](url) → text (url)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        return text

    # ── Raw IRC I/O ──────────────────────────────────────────────────────

    async def _send_raw(self, line: str) -> None:
        """Send a raw IRC protocol line."""
        if not self._writer or self._writer.is_closing():
            return
        encoded = (line + "\r\n").encode("utf-8")
        self._writer.write(encoded)
        await self._writer.drain()

    async def _receive_loop(self) -> None:
        """Main receive loop — reads lines, PONGs fast, queues the rest.

        PING answers and the watchdog arrival stamp happen HERE, never
        behind a multi-minute turn: the handler task below owns all slow
        work. Ordering is preserved (single consumer).
        """
        buffer = b""
        try:
            while self._reader and not self._reader.at_eof():
                data = await self._reader.read(4096)
                if not data:
                    break
                buffer += data
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    try:
                        decoded = line.decode("utf-8", errors="replace")
                        self._last_inbound = time.monotonic()
                        if self._is_ping(decoded):
                            await self._answer_ping(decoded)
                        else:
                            self._line_queue.put_nowait(decoded)
                    except Exception as e:
                        logger.warning("IRC: error handling line: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("IRC: receive loop error: %s", e)
        finally:
            try:
                self._line_queue.put_nowait(None)
            except Exception:
                pass
            if self.is_connected:
                logger.warning("IRC: connection lost, marking disconnected")
                self._set_fatal_error("connection_lost", "IRC connection closed unexpectedly", retryable=True)
                await self._notify_fatal_error()

    @staticmethod
    def _is_ping(raw: str) -> bool:
        """True when a raw line is a server PING (answer inline)."""
        try:
            text = raw.strip()
            if text.upper().startswith("PING"):
                return True
            parts = text.split(" ", 2)
            return len(parts) > 1 and parts[1].upper() == "PING"
        except Exception:
            return False

    async def _answer_ping(self, raw: str) -> None:
        """Reply PONG without touching the handler queue."""
        try:
            msg = _parse_irc_message(raw)
            params = msg.get("params") or []
            payload = params[0] if params else ""
            await self._send_raw(f"PONG :{payload}")
        except Exception as e:
            logger.warning("IRC: error answering ping: %s", e)

    async def _handle_task(self) -> None:
        """Consume queued lines in order (the slow path)."""
        try:
            while True:
                try:
                    line = await self._line_queue.get()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return
                if line is None:
                    return
                try:
                    await self._handle_line(line)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("IRC: error handling line: %s", e)
        except asyncio.CancelledError:
            raise


    async def _silence_watchdog(self) -> None:
        """Reconnect when the server goes quiet past SILENCE_LIMIT.

        A live server PINGs idle clients every minute, so sustained
        silence means the connection is half-open (e.g. the daemon
        restarted underneath us). Closing the writer drives the normal
        connection_lost path, which the reconnect watcher rebuilds.
        """
        try:
            while True:
                await asyncio.sleep(WATCHDOG_POLL)
                try:
                    if self._writer is None or self._writer.is_closing():
                        return
                    if time.monotonic() - self._last_inbound > SILENCE_LIMIT:
                        logger.warning(
                            "IRC: server silent %.0fs — assuming half-open, reconnecting",
                            time.monotonic() - self._last_inbound,
                        )
                        try:
                            self._writer.close()
                        except Exception:
                            pass
                        try:
                            if self._recv_task and not self._recv_task.done():
                                self._recv_task.cancel()
                        except Exception:
                            pass
                        # Drive the rebuild directly: a receive loop stuck
                        # in read() may never notice the closed writer,
                        # leaving the bot dead with no reconnect.
                        try:
                            if self.is_connected:
                                self._set_fatal_error(
                                    "connection_lost",
                                    "IRC server went silent (watchdog)",
                                    retryable=True)
                                await self._notify_fatal_error()
                        except Exception:
                            pass
                        return
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
    async def _handle_line(self, raw: str) -> None:
        """Dispatch a single IRC protocol line."""
        self._last_inbound = time.monotonic()
        msg = _parse_irc_message(raw)
        command = msg["command"]
        params = msg["params"]

        # PING/PONG keepalive
        if command == "PING":
            payload = params[0] if params else ""
            await self._send_raw(f"PONG :{payload}")
            return

        # RPL_WELCOME (001) — registration complete
        if command == "001":
            self._registered = True
            self._registration_event.set()
            if params:
                # Server may confirm our nick in the first param
                self._current_nick = params[0]
            return

        # ERR_NICKNAMEINUSE (433) — nick collision during registration
        if command == "433":
            # Retry with incrementing suffix: hermes_, hermes_1, hermes_2...
            base = self.nickname.rstrip("_0123456789")
            suffix_match = re.search(r"_(\d+)$", self._current_nick)
            if suffix_match:
                next_num = int(suffix_match.group(1)) + 1
                self._current_nick = f"{base}_{next_num}"
            elif self._current_nick == self.nickname:
                self._current_nick = self.nickname + "_"
            else:
                self._current_nick = self.nickname + "_1"
            await self._send_raw(f"NICK {self._current_nick}")
            return

        # PRIVMSG — incoming message (channel or DM)
        if command == "PRIVMSG" and len(params) >= 2:
            sender_nick = _extract_nick(msg["prefix"])
            target = params[0]
            text = params[1]

            # Ignore our own messages
            if sender_nick.lower() == self._current_nick.lower():
                return
            try:
                # Agent identities speaking in their rooms are never user
                # turns — routing them back would make agents answer
                # themselves in a loop.
                from observatory.identity import get_pool

                if sender_nick.lower() in get_pool().nicks():
                    return
            except Exception:
                pass

            # CTCP ACTION (/me) — convert to text
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = f"* {sender_nick} {text[8:-1]}"

            # Ignore other CTCP
            if text.startswith("\x01"):
                return

            # Determine if this is a channel message or DM
            is_channel = target.startswith("#") or target.startswith("&")
            chat_id = target if is_channel else sender_nick
            chat_type = "group" if is_channel else "dm"

            # Addressing (nick: or nick,): stripped everywhere, but only
            # REQUIRED outside managed rooms. In the bot's own rooms every
            # message is for the agent, like CLI.
            if is_channel:
                addressed = False
                for prefix in (f"{self._current_nick}:", f"{self._current_nick},",
                               f"{self._current_nick} "):
                    if text.lower().startswith(prefix.lower()):
                        text = text[len(prefix):].strip()
                        addressed = True
                        break
                if not addressed and not self.is_managed(target):
                    return  # Ignore unaddressed channel messages

            # Auth check (case-insensitive)
            if self._allowed_users_lower and sender_nick.lower() not in self._allowed_users_lower:
                logger.debug("IRC: ignoring message from unauthorized user %s", sender_nick)
                return

            await self._dispatch_message(
                text=text,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=sender_nick,
                user_name=sender_nick,
            )

        # NICK — track our own nick changes
        if command == "NICK" and _extract_nick(msg["prefix"]).lower() == self._current_nick.lower():
            if params:
                self._current_nick = params[0]

    async def _dispatch_message(
        self,
        text: str,
        chat_id: str,
        chat_type: str,
        user_id: str,
        user_name: str,
    ) -> None:
        """Build a MessageEvent and hand it to the base class handler.

        Delegate-child rooms (#parent-child) and spawned-omp rooms never
        reach gateway dispatch: the RoomManager steers the live child /
        pumps the omp task and the ack goes straight back to the room.
        """
        text = bang_to_slash(text)
        # Inbound milestone (routing only, never content): proves room
        # messages reach the engine when lower levels are hidden.
        try:
            from observatory.rooms import route_channel as _diag_route

            _diag = _diag_route(chat_id)[0] if chat_type == "group" else "dm"
        except Exception:
            _diag = "route-error"
        logger.info(
            "IRC: inbound chat=%s route=%s handler=%s",
            chat_id, _diag, bool(self._message_handler),
        )
        if chat_type == "group":
            try:
                from observatory.rooms import get_room_manager, route_channel
                route, _row = route_channel(chat_id)
                manager = get_room_manager()
                if manager is not None and route in ("child", "spawn-omp"):
                    if route == "child":
                        reply = await manager.handle_child_message(chat_id, user_name, text)
                    else:
                        reply = await manager.handle_omp_message(chat_id, user_name, text)
                    if reply:
                        await self.send(chat_id, reply)
                    # The room owned this text: a gateway turn here would
                    # answer a second time in someone else's room. Slash
                    # commands still fall through (exit/status/...).
                    if not text.lstrip().startswith("/"):
                        return
            except Exception:
                logger.debug("IRC: room route failed, falling through", exc_info=True)
        if not self._message_handler:
            return
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(int(time.time() * 1000)),
            timestamp=__import__("datetime").datetime.now(),
        )

        await self.handle_message(event)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def _derive_channel(nickname: str) -> str:
    """``#nick`` — one name for the bot and its room, period."""
    nick = str(nickname or "").strip().lstrip("#")
    return f"#{nick}" if nick else ""


def _configured_channel(extra: dict | None = None) -> str:
    """Effective channel: explicit env/config value, else ``#nick``."""
    extra = extra or {}
    return (
        (get_env_value("IRC_CHANNEL") or "").strip()
        or str(extra.get("channel", "") or "").strip()
        or _derive_channel((get_env_value("IRC_NICKNAME") or "") or extra.get("nickname", ""))
    )


def check_requirements() -> bool:
    """Check if IRC is configured.

    Only requires the server and a bot name — no external pip packages needed.
    """
    server = (get_env_value("IRC_SERVER") or "")
    # Also accept config.yaml-only configuration (no env vars).
    # The gateway passes PlatformConfig; we just check env for the
    # mercury setup / requirements check path.
    return bool(server and _configured_channel())


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    server = get_env_value("IRC_SERVER") or extra.get("server", "")
    return bool(server and _configured_channel(extra))


def _tls_default_for_host(host: str) -> bool:
    """TLS unless the host is loopback, private, or tailnet (our ircd is
    plaintext-only; public networks assume TLS). Pure — never touches env."""
    h = str(host or "").strip().lower().rstrip(".")
    if h in ("localhost",) or h.startswith("localhost."):
        return False
    if h.endswith(".ts.net") or h.endswith(".ts.net."):
        return False
    try:
        import ipaddress as _ip

        addr = _ip.ip_address(h)
        if addr.is_loopback or addr.is_private:
            return False
        # Tailscale CGNAT range (not covered by is_private everywhere).
        return addr not in _ip.ip_network("100.64.0.0/10")
    except ValueError:
        return True


def interactive_setup() -> None:
    """Interactive `mercury gateway setup` flow for the IRC platform.

    Four prompts: server, bot name (= nick AND ``#channel``), server
    password, owner nick. Everything else derives: TLS from the host,
    the allowlist is exactly the owner (only owner + bots exist).
    NickServ/ports/multiple channels stay env-only (see plugin.yaml).

    Lazy-imports ``mercury_cli.setup`` helpers so the plugin stays importable
    in non-CLI contexts (gateway runtime, tests).
    """
    from mercury_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("IRC")
    existing_server = get_env_value("IRC_SERVER")
    if existing_server:
        nick = get_env_value("IRC_NICKNAME") or ""
        if (get_env_value("IRC_MANAGED_BY") or "").strip().lower() == "observatory":
            print_info(f"IRC is managed by the observatory (server: {existing_server}"
                       f"{f', bot: {nick}' if nick else ''}) — the gateway bot lives here.")
            print_info("   A second, personal IRC connection is not supported: there is")
            print_info("   one bot identity per network. Taking over repoints the bot")
            print_info("   and BREAKS the observatory rooms — manage the bot via")
            print_info("   `mercury setup observatory` instead.")
            if not prompt_yes_no("Take over manually anyway (breaks observatory)?", False):
                return
            save_env_value("IRC_MANAGED_BY", "")
            print_warning("Observatory management released — the bot is now yours.")
        else:
            print_info(f"IRC: already configured (server: {existing_server}"
                       f"{f', bot: {nick}' if nick else ''})")
            if not prompt_yes_no("Reconfigure IRC?", False):
                return

    print_info("Connect Mercury to an IRC network. Uses Python stdlib — no extra packages needed.")
    print_info("   One name covers the bot and its channel: bot `ace` lives in `#ace`.")
    print()

    server = prompt("IRC server hostname (e.g. 127.0.0.1, tailnet IP, irc.libera.chat)",
                    default=existing_server or "")
    if not server:
        print_warning("Server is required — skipping IRC setup")
        return
    server = server.strip()
    save_env_value("IRC_SERVER", server)

    use_tls = _tls_default_for_host(server)
    save_env_value("IRC_USE_TLS", "true" if use_tls else "false")
    print_info(f"TLS: {'on' if use_tls else 'off'} (override: IRC_USE_TLS)")

    nickname = prompt(
        "Bot name (nick AND channel: `ace` → `#ace`)",
        default=get_env_value("IRC_NICKNAME") or "",
    )
    if not nickname:
        print_warning("Bot name is required — skipping IRC setup")
        return
    nickname = nickname.strip().lstrip("#")
    save_env_value("IRC_NICKNAME", nickname)
    save_env_value("IRC_CHANNEL", _derive_channel(nickname))

    print()
    server_password = prompt("Server password (PASS — blank for none)",
                             default="", password=True)
    if server_password:
        save_env_value("IRC_SERVER_PASSWORD", server_password)

    print()
    print_info("Only you and the bots exist here — nobody else may command the bot.")
    owner = prompt(
        "Your IRC nick (the only nick allowed to talk to the bot)",
        default=get_env_value("IRC_ALLOWED_USERS") or "",
    )
    save_env_value("IRC_ALLOW_ALL_USERS", "false")
    if owner and owner.strip():
        save_env_value("IRC_ALLOWED_USERS", owner.strip().replace(" ", ""))
        print_success(f"Only {owner.strip()} may talk to the bot")
    else:
        save_env_value("IRC_ALLOWED_USERS", "")
        print_warning("No owner nick — the bot will ignore everyone until you set one.")

    print()
    print_success("IRC configuration saved to ~/.mercury/.env")
    print_info("Restart the gateway for changes to take effect: mercury gateway restart")


def is_connected(config) -> bool:
    """Check whether IRC is configured (env or config.yaml)."""
    extra = getattr(config, "extra", {}) or {}
    server = get_env_value("IRC_SERVER") or extra.get("server", "")
    return bool(server and _configured_channel(extra))


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook (landed in the
    generic-plugin-interface migration) BEFORE adapter construction, so
    ``gateway status`` and ``get_connected_platforms()`` reflect env-only
    configuration without instantiating the IRC client.  Returns ``None``
    when IRC isn't minimally configured; the caller skips auto-enabling.

    The special ``home_channel`` key in the returned dict is handled by
    the core hook — it becomes a proper ``HomeChannel`` dataclass on the
    ``PlatformConfig`` rather than being merged into ``extra``.
    """
    server = (get_env_value("IRC_SERVER") or "").strip()
    channel = _configured_channel()
    if not (server and channel):
        return None
    seed: dict = {
        "server": server,
        "channel": channel,
    }
    port = (get_env_value("IRC_PORT") or "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass
    nickname = (get_env_value("IRC_NICKNAME") or "").strip()
    if nickname:
        seed["nickname"] = nickname
    use_tls = (get_env_value("IRC_USE_TLS") or "").strip().lower()
    if use_tls:
        seed["use_tls"] = use_tls in {"1", "true", "yes"}
    # Passwords live in PlatformConfig.extra as well for back-compat with
    # existing config.yaml users; env-reads at construct time still win.
    if _get_scoped_secret("IRC_SERVER_PASSWORD"):
        seed["server_password"] = _get_scoped_secret("IRC_SERVER_PASSWORD")
    if _get_scoped_secret("IRC_NICKSERV_PASSWORD"):
        seed["nickserv_password"] = _get_scoped_secret("IRC_NICKSERV_PASSWORD")
    # Optional home-channel (usually the same as IRC_CHANNEL, but can be a
    # dedicated reports channel).  Defaults to IRC_CHANNEL so cron jobs
    # with ``deliver=irc`` have a sensible target without extra config.
    home = get_env_value("IRC_HOME_CHANNEL") or channel
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": (get_env_value("IRC_HOME_CHANNEL_NAME") or home),
        }
    return seed


def _strip_irc_control_chars(text: str) -> str:
    """Strip IRC line terminators and the NUL byte from ``text``.

    IRC commands are CRLF-delimited; a bare ``\\r`` or ``\\n`` in user
    content lets an attacker inject arbitrary IRC commands (CTCP, JOIN,
    KICK).  ``\\x00`` is a protocol-illegal byte.  Everything else is
    valid in PRIVMSG payloads.
    """
    return text.replace("\r", " ").replace("\n", " ").replace("\x00", "")


def _is_irc_channel(target: str) -> bool:
    return bool(target) and target[0] in "#&+!"


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Open an ephemeral IRC connection, send a PRIVMSG, and quit.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``mercury cron`` running as a
    separate process from ``mercury gateway``).  Without this hook,
    ``deliver=irc`` cron jobs fail with ``No live adapter for platform``.

    The standalone client uses a distinct nick suffix (``-cron``) so it
    does not collide with the long-running gateway adapter that may already
    be holding the configured nickname on the same network.  When the
    target is a channel, the client JOINs it before sending PRIVMSG so
    networks with the default ``+n`` (no external messages) channel mode
    accept the delivery.

    ``thread_id`` and ``media_files`` are accepted for signature parity but
    are not meaningful on IRC: IRC has no native thread or attachment
    primitive.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    server = get_env_value("IRC_SERVER") or extra.get("server", "")
    channel = _configured_channel(extra)
    if not server or not channel:
        return {"error": "IRC standalone send: IRC_SERVER and a bot name (IRC_NICKNAME) must be configured"}
    port_value = get_env_value("IRC_PORT") or extra.get("port", 6697)
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        return {"error": f"IRC standalone send: invalid port {port_value!r}"}

    nickname = get_env_value("IRC_NICKNAME") or extra.get("nickname", "mercury-bot")
    use_tls_env = get_env_value("IRC_USE_TLS")
    if use_tls_env is not None:
        use_tls = use_tls_env.lower() in {"1", "true", "yes"}
    else:
        use_tls = bool(extra.get("use_tls", True))

    server_password = _get_scoped_secret("IRC_SERVER_PASSWORD") or extra.get("server_password", "")
    nickserv_password = _get_scoped_secret("IRC_NICKSERV_PASSWORD") or extra.get("nickserv_password", "")

    # Reject control characters in chat_id to block IRC command injection.
    raw_target = chat_id or channel
    if any(ch in raw_target for ch in ("\r", "\n", "\x00", " ")):
        return {"error": "IRC standalone send: chat_id contains illegal IRC characters"}
    target = raw_target

    # Distinct nick prevents NICK collision with a live gateway adapter
    # that may already be holding the configured nickname.  Cap to 24 chars
    # so subsequent collision retries do not overflow the 30-char NICKLEN
    # most networks enforce.
    nick_base = nickname.rstrip("_0123456789-")[:24] or "mercury-bot"
    standalone_nick = f"{nick_base}-cron"[:30]
    plain = IRCAdapter._strip_markdown(message)

    ssl_ctx = ssl.create_default_context() if use_tls else None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(server, port, ssl=ssl_ctx),
            timeout=15.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"error": f"IRC standalone connect failed: {e}"}

    async def _raw(line: str) -> None:
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    nick_attempts = 0
    max_nick_attempts = 5
    try:
        if server_password:
            await _raw(f"PASS {_strip_irc_control_chars(server_password)}")
        await _raw(f"NICK {standalone_nick}")
        await _raw(f"USER {standalone_nick} 0 * :Mercury (cron)")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        registered = False
        while not registered:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"error": "IRC standalone send: registration timeout (no RPL_WELCOME)"}
            try:
                raw_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=remaining)
            except asyncio.TimeoutError:
                return {"error": "IRC standalone send: registration timeout (no RPL_WELCOME)"}
            except asyncio.IncompleteReadError:
                return {"error": "IRC standalone send: server closed connection during registration"}
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            msg = _parse_irc_message(decoded)
            cmd = msg["command"]
            if cmd == "PING":
                payload = msg["params"][0] if msg["params"] else ""
                await _raw(f"PONG :{payload}")
            elif cmd == "001":
                registered = True
            elif cmd in {"432", "433"}:
                nick_attempts += 1
                if nick_attempts > max_nick_attempts:
                    return {"error": "IRC standalone send: too many nick collisions"}
                # Build the next nick from the stable base, not the
                # mutated value, so the suffix stays bounded.
                standalone_nick = f"{nick_base}-cron-{nick_attempts}"[:30]
                await _raw(f"NICK {standalone_nick}")
            elif cmd in {"464", "465"}:
                return {"error": f"IRC standalone send: server rejected client ({cmd})"}

        if nickserv_password:
            await _raw(f"PRIVMSG NickServ :IDENTIFY {_strip_irc_control_chars(nickserv_password)}")
            await asyncio.sleep(2)

        # JOIN before PRIVMSG.  IRC channels with the default ``+n`` mode
        # (no external messages: Libera, OFTC, EFnet, IRCNet, undernet)
        # silently drop PRIVMSG from non-members.  Do not JOIN bare nicks
        # (DM target) or server queries.
        if _is_irc_channel(target):
            await _raw(f"JOIN {target}")
            join_deadline = loop.time() + 5.0
            joined = False
            while not joined:
                remaining = join_deadline - loop.time()
                if remaining <= 0:
                    # Timed out waiting for a JOIN ack: proceed anyway, the
                    # server may still deliver the PRIVMSG depending on mode.
                    break
                try:
                    raw_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=remaining)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    break
                decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                jmsg = _parse_irc_message(decoded)
                jcmd = jmsg["command"]
                if jcmd == "PING":
                    payload = jmsg["params"][0] if jmsg["params"] else ""
                    await _raw(f"PONG :{payload}")
                elif jcmd in {"366", "JOIN"}:
                    joined = True
                elif jcmd in {"403", "405", "471", "473", "474", "475"}:
                    return {"error": f"IRC standalone send: JOIN {target} rejected ({jcmd})"}

        # Bytes-aware per-line splitting so multi-line plain text never
        # exceeds the IRC 510-byte protocol limit.  Reuses the same
        # algorithm as IRCAdapter._split_message, with control-character
        # stripping per line to block CRLF injection from message content.
        overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2
        max_bytes = 510 - overhead
        sent_any = False
        for paragraph in plain.split("\n"):
            paragraph = _strip_irc_control_chars(paragraph).rstrip()
            if not paragraph:
                continue
            while paragraph:
                encoded = paragraph.encode("utf-8")
                if len(encoded) <= max_bytes:
                    await _raw(f"PRIVMSG {target} :{paragraph}")
                    await asyncio.sleep(0.3)
                    sent_any = True
                    break
                # Binary search for largest prefix that fits within max_bytes
                low, high, best = 1, len(paragraph), 0
                while low <= high:
                    mid = (low + high) // 2
                    if len(paragraph[:mid].encode("utf-8")) <= max_bytes:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                split_at = best
                space = paragraph.rfind(" ", 0, split_at)
                if space > split_at // 3:
                    split_at = space
                await _raw(f"PRIVMSG {target} :{paragraph[:split_at].rstrip()}")
                await asyncio.sleep(0.3)
                sent_any = True
                paragraph = paragraph[split_at:].lstrip()

        if not sent_any:
            return {"error": "IRC standalone send: empty message after stripping"}

        await _raw("QUIT :delivered")
        try:
            await asyncio.wait_for(reader.read(1024), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        return {"success": True, "message_id": str(int(time.time() * 1000))}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("IRC standalone send raised", exc_info=True)
        return {"error": f"IRC standalone send failed: {e}"}
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass


def register(ctx):
    """Plugin entry point: called by the Mercury plugin system."""
    ctx.register_platform(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: IRCAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["IRC_SERVER", "IRC_NICKNAME"],
        install_hint="No extra packages needed (stdlib only)",
        setup_fn=interactive_setup,
        # Env-driven auto-configuration: seeds PlatformConfig.extra with
        # server/channel/port/tls + home_channel so env-only setups show
        # up in gateway status without instantiating the adapter.
        env_enablement_fn=_env_enablement,
        # Cron home-channel delivery support.  IRC_HOME_CHANNEL defaults to
        # IRC_CHANNEL (see _env_enablement), so cron jobs with
        # deliver=irc route to the joined channel by default.
        cron_deliver_env_var="IRC_HOME_CHANNEL",
        # Out-of-process cron delivery.  Without this hook, deliver=irc
        # cron jobs fail with "No live adapter" when cron runs separately
        # from the gateway.
        standalone_sender_fn=_standalone_send,
        # Auth env vars for _is_user_authorized() integration
        allowed_users_env="IRC_ALLOWED_USERS",
        allow_all_env="IRC_ALLOW_ALL_USERS",
        # IRC line limit after protocol overhead
        max_message_length=450,
        # Display
        emoji="💬",
        # IRC doesn't have phone numbers to redact
        pii_safe=False,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via IRC. IRC does not support markdown formatting "
            "— use plain text only. Messages are limited to ~450 characters per "
            "line (long messages are automatically split). In channels, users "
            "address you by prefixing your nick. Keep responses concise and "
            "conversational."
        ),
    )


#: Goguma intercepts /commands client-side (they never reach the bot),
#: so !verb aliases /verb for the verbs that matter. Anything else
#: starting with ! is plain chat (never rewritten).
BANG_VERBS = frozenset({"spawn", "spawnomp", "exit", "stop", "approve", "deny"})


def bang_to_slash(text: str) -> str:
    """Rewrite a leading !verb to /verb for known verbs only."""
    if text.startswith("!") and not text.startswith("!!"):
        verb, _, rest = text[1:].partition(" ")
        if verb.lower() in BANG_VERBS:
            return "/" + verb.lower() + (" " + rest if rest.strip() else "")
    return text
