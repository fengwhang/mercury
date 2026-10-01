"""
MIRC Platform Adapter for Mercury.

A plugin-based gateway adapter that connects to an MIRC server and relays
messages to/from the Mercury agent.  Zero external dependencies — uses
Python's stdlib asyncio for the MIRC protocol.

Configuration in config.yaml::

    gateway:
      platforms:
        mirc:
          enabled: true
          extra:
            server: mirc.libera.chat
            port: 6697
            nickname: mercury-bot
            channel: "#mercury"
            use_tls: true
            server_password: ""       # optional server password
            nickserv_password: ""     # optional NickServ identification
            allowed_users: []         # empty = allow all, or list of nicks
            max_message_length: 450   # MIRC line limit (safe default)

Or via environment variables (overrides config.yaml):
    IRC_SERVER, IRC_PORT, IRC_NICKNAME, IRC_CHANNEL, IRC_USE_TLS,
    IRC_SERVER_PASSWORD, IRC_NICKSERV_PASSWORD
"""

import asyncio
import functools
import logging
import os

from mercury_cli.config import get_env_path, get_env_value
import re
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple

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

from observatory.rooms import OMP_COMMAND_VERBS as OMP_BANG_VERBS
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform


# ---------------------------------------------------------------------------
# MIRC protocol helpers
# ---------------------------------------------------------------------------

SILENCE_LIMIT = 210.0  # reconnect when the server says nothing this long
WATCHDOG_POLL = 60.0  # silence-check cadence (server PINGs every 60s)
#: Bound for one connect-phase drain: a half-open socket must fail loud
#: (retryable reconnect) instead of stalling connect() forever silent.
CONNECT_SEND_TIMEOUT = 10.0


def _enable_keepalive(writer) -> None:
    """TCP keepalive on an MIRC connection (best-effort, never raises)."""
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

def _parse_mirc_message(raw: str) -> dict:
    """Parse a raw MIRC protocol line into components.

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
    """Extract nickname from MIRC prefix (nick!user@host)."""
    return prefix.split("!")[0] if "!" in prefix else prefix


# ---------------------------------------------------------------------------
# MIRC Adapter
# ---------------------------------------------------------------------------

class MIRCAdapter(BasePlatformAdapter):
    """Async MIRC adapter implementing the BasePlatformAdapter interface.

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
        # The bot dials the AGENT listener, which takes the AGENT password —
        # not the server password (that's for the client/bouncer listener).
        self.agent_password = _get_scoped_secret("IRC_AGENT_PASSWORD") or extra.get("agent_password", "")
        self.nickserv_password = _get_scoped_secret("IRC_NICKSERV_PASSWORD") or extra.get("nickserv_password", "")
        self.oper_password = _get_scoped_secret("IRC_OPER_PASSWORD") or extra.get("oper_password", "") or self.server_password
        # Mercury's PRIMARY agent surface (design D7): MIRC here is not a
        # secondary messaging platform. The observatory perimeter (PASS-
        # authed agent listener on localhost/tailnet) IS the authorization,
        # exactly like the relay's trusted upstream — so pairing/allowlist
        # policies must never gate the agent interface. Public (non-
        # observatory) MIRC keeps the ordinary allowlist policy.
        self._observatory_managed = (
            (get_env_value("IRC_MANAGED_BY") or "").strip().lower()
            == "observatory"
            or str(extra.get("managed_by") or "").strip().lower()
            == "observatory"
        )
        # Observability rooms: extra agent channels the bot joins dynamically
        # (/spawn rooms, #parent-child subagent rooms). Managed channels
        # never require nick-addressing: every message there is for the agent.
        self.extra_channels: set[str] = set()
        # Multi-line paste coalescing: MIRC has no multi-line PRIVMSG, so a
        # pasted paragraph arrives as N rapid lines. Hold plain text for a
        # short quiet window and flush once joined with newlines — one
        # steering event instead of N interrupts (weixin pattern). Paste
        # lines land in the same TCP burst (microseconds apart), so 250ms
        # catches the burst while staying below human perception; there is
        # no end-of-paste marker on the wire, so a short hold is inherent.
        # Tune with extra text_batch_delay_seconds / IRC_TEXT_BATCH_DELAY_SECONDS.
        try:
            self._mirc_batch_delay = float(
                extra.get("text_batch_delay_seconds")
                or get_env_value("IRC_TEXT_BATCH_DELAY_SECONDS")
                or 0.25)
        except (TypeError, ValueError):
            self._mirc_batch_delay = 0.25
        self._mirc_batches: dict = {}

        # Auth
        self.allowed_users: list = extra.get("allowed_users", [])
        # MIRC nicks are case-insensitive — normalise for lookups
        self._allowed_users_lower: set = {u.lower() for u in self.allowed_users if isinstance(u, str)}

        # MIRC limits
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
        self._registered = False  # MIRC registration complete
        #: Connection generation: bumped on every connect() attempt so a
        #: stale receive task unwinding late cannot tear down a newer
        #: connection's writer/sink/registration (same-nick reconnects
        #: would otherwise murder each other forever).
        self._conn_generation = 0
        self._observatory_resync_task: Optional[asyncio.Task] = None
        self._observatory_online_channels: set[str] = set()
        self._oper = False  # set by 381, cleared by 464/481
        self._registration_event = asyncio.Event()
        self._current_nick = self.nickname
        # draft/multiline negotiation state (learned per connect; cleared
        # on disconnect). Untagged traffic keeps the quiet-window timer.
        self._server_caps: set = set()
        self._server_multiline = False
        self._cap_event = asyncio.Event()
        self._in_batches: dict = {}
        self._batch_seq = 0

    @property
    def name(self) -> str:
        return "IRC"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def _ensure_multiline(self) -> None:
        """Late CAP REQ for a multiline LS that arrived after the 2s
        connect-time wait (slow daemon at restart). No-op when already
        negotiated or the server never advertised it. Best-effort."""
        try:
            if self._server_multiline or "draft/multiline" not in self._server_caps:
                return
            self._cap_event.clear()
            await self._send_raw("CAP REQ :draft/multiline", timeout=CONNECT_SEND_TIMEOUT)
            await asyncio.wait_for(self._cap_event.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the MIRC server, register, and join the channel."""
        if not self.server or not self.channel:
            logger.error("MIRC: server and channel must be configured")
            self._set_fatal_error(
                "config_missing",
                "IRC_SERVER and IRC_CHANNEL must be set",
                retryable=False,
            )
            return False

        # Prevent two profiles from using the same MIRC identity
        try:
            from gateway.status import acquire_scoped_lock, release_scoped_lock
            lock_key = f"{self.server}:{self.nickname}"
            acquired, _existing = acquire_scoped_lock("irc", lock_key)
            if not acquired:
                logger.error("MIRC: %s@%s already in use by another profile", self.nickname, self.server)
                self._set_fatal_error("lock_conflict", "IRC identity in use by another profile", retryable=False)
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None  # status module not available (e.g. tests)
        self._conn_generation = getattr(self, "_conn_generation", 0) + 1
        self._observatory_online_channels = set()
        try:
            ssl_ctx = None
            if self.use_tls:
                ssl_ctx = ssl.create_default_context()

            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.server, self.port, ssl=ssl_ctx),
                timeout=30.0,
            )
        except Exception as e:
            logger.error("MIRC: failed to connect to %s:%s — %s", self.server, self.port, e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        # MIRC registration sequence. PASS carries the agent password when
        # set (observatory agent listener); otherwise the server password
        # (public MIRC servers, back-compat setups with a single password).
        reg_password = self.agent_password or self.server_password
        if reg_password:
            await self._send_raw(f"PASS {reg_password}", timeout=CONNECT_SEND_TIMEOUT)
        await self._send_raw(f"NICK {self.nickname}", timeout=CONNECT_SEND_TIMEOUT)
        await self._send_raw(f"USER {self.nickname} 0 * :Mercury", timeout=CONNECT_SEND_TIMEOUT)

        # Start receive loop + ordered handler (PINGs bypass the queue)
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._line_queue = asyncio.Queue()
        self._handler_task = asyncio.create_task(self._handle_task())

        # Wait for registration (001 RPL_WELCOME) with timeout
        try:
            await asyncio.wait_for(self._registration_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("MIRC: registration timed out")
            await self.disconnect()
            self._set_fatal_error("registration_timeout", "IRC server did not send RPL_WELCOME", retryable=True)
            return False
        # IRCv3 multiline: learn server caps (best-effort, short timeouts —
        # a server without CAP support just means packed PRIVMSGs as before).
        self._server_caps = set()
        self._server_multiline = False
        self._cap_event.clear()
        try:
            await self._send_raw("CAP LS 302", timeout=CONNECT_SEND_TIMEOUT)
            await asyncio.wait_for(self._cap_event.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        if "draft/multiline" in self._server_caps:
            self._cap_event.clear()
            try:
                await self._send_raw("CAP REQ :draft/multiline", timeout=CONNECT_SEND_TIMEOUT)
                await asyncio.wait_for(self._cap_event.wait(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        # NickServ identification
        if self.nickserv_password:
            await self._send_raw(f"PRIVMSG NickServ :IDENTIFY {self.nickserv_password}", timeout=CONNECT_SEND_TIMEOUT)
            await asyncio.sleep(2)  # Give NickServ time to process

        # Join the gateway channel plus any managed agent rooms. MIRC creates
        # a channel on first JOIN; the observatory resync pass re-adds live
        # rooms after a reconnect via join_channel().
        await self._send_raw(f"JOIN {self.channel}", timeout=CONNECT_SEND_TIMEOUT)
        for extra in sorted(self.extra_channels):
            await self._send_raw(f"JOIN {extra}", timeout=CONNECT_SEND_TIMEOUT)
        await self._ensure_multiline()
        logger.info("MIRC: multiline %s (server caps: %s)",
                    "on" if self._server_multiline else "off",
                    ",".join(sorted(self._server_caps)) or "none")

        # OPER for the observatory /exit room kill (no-op when unconfigured).
        if self.oper_password:
            try:
                await self._send_raw(f"OPER {self.oper_password}", timeout=CONNECT_SEND_TIMEOUT)
            except Exception:
                logger.debug("MIRC: OPER failed", exc_info=True)

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
                self._observatory_resync_task = asyncio.create_task(
                    self._resync_observatory(self._conn_generation))
            except Exception:
                logger.warning("MIRC: resync schedule skipped", exc_info=True)
        except Exception:
            logger.debug("MIRC: bot-sink register skipped", exc_info=True)
        logger.info("MIRC: connected to %s:%s as %s, joined %s", self.server, self.port, self._current_nick, self.channel)
        # Plugin-registered native handlers (ctx.register_platform_handler).
        self._wire_plugin_handlers(None)
        _enable_keepalive(self._writer)
        self._last_inbound = time.monotonic()
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._silence_watchdog())
        return True

    async def _resync_observatory(self, generation: int) -> None:
        """Restore rooms, then post one readiness status per connected room."""
        from observatory.platform_hook import boot_resync

        try:
            report = await boot_resync()
            if (not self._observatory_managed or not self.config.gateway_restart_notification
                    or not report.get("joined")
                    or report.get("failed") or generation != self._conn_generation):
                return
            channels = {str(channel).lower() for channel in report["joined"]}
            channels.add(self.channel.lower())
            for channel in sorted(channels):
                if generation != self._conn_generation:
                    return
                if channel in self._observatory_online_channels:
                    continue
                if await self.say(channel, "Observatory online - Mercury is back and ready", kind="status"):
                    self._observatory_online_channels.add(channel)
        except Exception:
            logger.warning("MIRC: Observatory startup notification failed", exc_info=True)

    async def observatory_startup_channels(self) -> set[str]:
        """Let gateway startup avoid adding another online notice after resync."""
        task = self._observatory_resync_task
        if task is not None:
            await asyncio.shield(task)
        return set(self._observatory_online_channels)

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
        for _buf in list(getattr(self, "_mirc_batches", {}).values()):
            _task = _buf.get("task")
            if _task is not None and not _task.done():
                _task.cancel()
        if hasattr(self, "_mirc_batches"):
            self._mirc_batches.clear()
        self._writer = None
        self._registered = False
        self._registration_event.clear()
        self._server_multiline = False
        self._server_caps = set()
        try:
            self._cap_event.clear()
        except Exception:
            pass
        if hasattr(self, "_in_batches"):
            self._in_batches.clear()
        try:
            from observatory.rooms import get_bot_sink, set_bot_sink
            if get_bot_sink() is self:
                set_bot_sink(None)
        except Exception:
            pass
    def _drop_teardown(self, generation: int | None = None) -> None:
        """Best-effort local teardown after connection loss (sync only).

        Closes the dead writer (no waiting), clears a bot sink pointing
        at this adapter, resets registration state, and releases the
        scoped identity lock so a fresh connect starts clean. Called
        from the receive loop's finally and the silence-watchdog drop
        path — never awaits foreign tasks (self-deadlock proof: the
        full disconnect() awaits this task). Never raises.

        ``generation`` is the connection generation the caller served;
        a stale task unwinding after a newer connect bumped the counter
        must NOT touch the new connection's writer/sink/registration.
        ``None`` (tests, legacy callers) always acts.
        """
        try:
            if (generation is not None and generation != getattr(
                    self, "_conn_generation", generation)):
                return
        except Exception:
            pass
        try:
            writer = self._writer
            self._writer = None
            if writer is not None and not writer.is_closing():
                try:
                    writer.close()
                except Exception:
                    pass
        except Exception:
            pass
        self._registered = False
        try:
            self._registration_event.clear()
        except Exception:
            pass
        self._server_caps = set()
        self._server_multiline = False
        try:
            from observatory.rooms import get_bot_sink, set_bot_sink

            if get_bot_sink() is self:
                set_bot_sink(None)
        except Exception:
            pass
        try:
            if getattr(self, "_lock_key", None):
                from gateway.status import release_scoped_lock

                release_scoped_lock("irc", self._lock_key)
        except Exception:
            pass

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        if not self._writer or self._writer.is_closing():
            return SendResult(success=False, error="Not connected")

        try:
            from observatory.thinking import is_interim_notice, thinking_done, thinking_progress

            if is_interim_notice(content):
                # Provider memory progress (recall/retain line): the turn
                # is alive, so restart the pending face's delay instead of
                # killing it — otherwise every turn with recall active
                # cancels its face ~1s in and only notice-free rooms (the
                # gateway) ever show faces.
                thinking_progress(chat_id)
            else:
                thinking_done(chat_id)
        except Exception:
            pass
        target = chat_id  # channel name or nick for DMs
        from observatory.message_format import MESSAGE_KINDS, message_tags

        kind = (metadata or {}).get("mercury_kind")
        if kind not in MESSAGE_KINDS:
            kind = "status" if (metadata or {}).get("_interim_send") else "assistant_reply"
        tag_overhead = len(message_tags(kind).encode("utf-8"))
        content = self._expand_media_tags(content)
        # Per-agent identity first: rooms with a live identity speak as
        # their own nick (vm_charlie, not vm_gateway). All-or-nothing
        # per message (a split identity looks worse than a fallback).
        try:
            from observatory import identity as _identity

            if _identity.get_pool().get(target) is not None:
                batched = False
                if self._server_multiline:
                    parts = self._message_parts(
                        content, target,
                        extra_overhead=_identity.BATCH_TAG_OVERHEAD + tag_overhead + _identity.PART_TAG_OVERHEAD)
                    lines = [part[0] for part in parts]
                    if len(lines) > 1:
                        batched = await _identity.send_multiline(target, lines, kind=kind,
                                                               concat=[part[1] for part in parts])
                if not batched:
                    lines = self._split_message(content, target, extra_overhead=tag_overhead)
                    ok = True
                    for line in lines:
                        ok = await _identity.send_as_identity(target, line, kind=kind) and ok
                    if not ok:
                        raise RuntimeError("identity send failed")
                return SendResult(
                    success=True, message_id=str(int(time.time() * 1000)))
        except Exception:
            logger.debug("MIRC: identity send failed, using main bot",
                         exc_info=True)
        parts = self._message_parts(content, target, extra_overhead=tag_overhead)
        lines = [part[0] for part in parts]
        batch_ref = ""
        if self._server_multiline and len(lines) > 1:
            # One logical message: BATCH frames + tagged lines for capable
            # peers; legacy peers still get the bare packed lines via the
            # daemon relay. Re-split accounting for the tag prefix so
            # tagged lines still fit the wire limit.
            batch_ref = f"m{int(time.time() * 1000)}-{self._batch_seq}"
            self._batch_seq += 1
            parts = self._message_parts(content, target, extra_overhead=len(
                message_tags(kind, batch=batch_ref, concat=True, empty=True)))
            lines = [part[0] for part in parts]
            if len(lines) == 1:
                batch_ref = ""
        if batch_ref:
            try:
                await self._send_raw(f"BATCH +{batch_ref} draft/multiline {target}")
            except Exception as e:
                return SendResult(success=False, error=str(e))

        for line, concat in parts:
            try:
                if batch_ref:
                    # Blank chunks ride as one space: the daemon 412s
                    # empty text, and the reassembled row keeps the gap.
                    await self._send_raw(f"{message_tags(kind, batch=batch_ref, concat=concat, empty=line == '')}"
                                         f"PRIVMSG {target} :{line or ' '}")
                else:
                    await self._send_raw(f"{message_tags(kind)}PRIVMSG {target} :{line}")
                # No pacing sleeps: every line of an agent turn goes out
                # back-to-back. Flood pacing against our own localhost
                # daemon only ever delayed first paint.
            except Exception as e:
                return SendResult(success=False, error=str(e))
        if batch_ref:
            try:
                await self._send_raw(f"BATCH -{batch_ref}")
            except Exception as e:
                return SendResult(success=False, error=str(e))

        return SendResult(success=True, message_id=str(int(time.time() * 1000)))
    # ── mLounge file delivery (paperclip parity) ──────────────────────────
    # MIRC has no attachment primitive, so files go out as mLounge links:
    # stage into the uploads dir, verify it serves, post the URL. Same
    # scheme as a human clicking upload — the room sees a plain link.

    async def _stage_media_link(self, path: str) -> str | None:
        """Stage a local file; return its mLounge URL or None. Never raises."""
        try:
            from observatory import mlounge as mlounge_mod

            staged = mlounge_mod.stage_mlounge_upload(None, path)
            url = mlounge_mod.mlounge_base_url(None) + "/" + staged["url_path"]
            if mlounge_mod.check_upload_serves(url):
                return url
            logger.debug("MIRC: staged link does not serve, dropping")
            return None
        except Exception:
            logger.debug("MIRC: media stage failed", exc_info=True)
            return None

    def _expand_media_tags(self, content: str) -> str:
        """Replace MEDIA:<path> tags with mLounge links (best-effort).

        Synchronous: staging is a local file copy. No serve-verify here
        (the dedicated overrides verify); a failed stage keeps the raw
        tag so nothing is silently lost.
        """
        import re as _re

        def _one(match) -> str:
            try:
                from observatory import mlounge as mlounge_mod

                staged = mlounge_mod.stage_mlounge_upload(None, match.group(1))
                return mlounge_mod.mlounge_base_url(None) + "/" + staged["url_path"]
            except Exception:
                return match.group(0)

        try:
            return _re.sub(r"MEDIA:([^\s]+)", _one, content)
        except Exception:
            return content

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Deliver a file as a mLounge link (MIRC has no attachments)."""
        url = await self._stage_media_link(file_path)
        if url is None:
            return SendResult(success=False,
                              error="could not stage file for Lounge link")
        text = f"{caption}\n{url}".strip() if caption else url
        return await self.send(chat_id=chat_id, content=text)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Deliver images as mLounge links (one line per file)."""
        from urllib.parse import unquote as _unquote

        lines = []
        for image_url, _alt in images or []:
            path = image_url[7:] if image_url.startswith("file://") else image_url
            path = _unquote(path)
            if image_url.startswith("http"):
                lines.append(image_url)
                continue
            url = await self._stage_media_link(path)
            lines.append(url or f"(could not attach {path})")
            if human_delay > 0:
                await asyncio.sleep(human_delay)
        if lines:
            await self.send(chat_id=chat_id, content="\n".join(lines))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Deliver audio as a mLounge link (no voice bubbles on MIRC)."""
        return await self.send_document(
            chat_id, audio_path, caption=caption, metadata=metadata)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Deliver video as a mLounge link (no inline playback on MIRC)."""
        return await self.send_document(
            chat_id, video_path, caption=caption, metadata=metadata)

    # ── Observatory rooms (BotSink surface for observatory.rooms) ──────────

    @property
    def authorization_is_upstream(self) -> bool:
        """Observatory perimeter is this surface's authorization.

        Overridden from ``BasePlatformAdapter`` (default False): the
        observatory-managed bot talks only over the PASS-authed agent
        listener on localhost/tailnet (design D7) — a trusted upstream,
        like the relay. Pairing/allowlist policy must never gate
        Mercury's primary agent interface. Public MIRC stays False.
        """
        return bool(getattr(self, "_observatory_managed", False))

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
            logger.debug("MIRC: join %s failed", channel, exc_info=True)
            return False

    async def invite_user(self, nick: str, channel: str) -> bool:
        """INVITE a nick to a room (phone surfaces it as a tap). Never raises."""
        if not self._writer or self._writer.is_closing():
            return False
        try:
            await self._send_raw(f"INVITE {nick} :{channel}")
            return True
        except Exception:
            logger.debug("MIRC: invite %s to %s failed", nick, channel, exc_info=True)
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
            logger.debug("MIRC: part %s failed", channel, exc_info=True)
            return False

    async def say(self, channel: str, text: str, *, kind: str = "status") -> bool:
        """PRIVMSG into a room (BotSink naming for observatory.rooms)."""
        result = await self.send(channel, text, metadata={"mercury_kind": kind})
        return bool(getattr(result, "success", False))

    async def destroy_channel(self, channel: str) -> bool:
        """Server-side room kill: OPER refresh, DESTROY, PART, undirect.

        Never raises. Returns part's outcome; a failed destroy is
        WARNING-loud (a silent one strands visible rooms).
        """
        if self._writer and not self._writer.is_closing():
            try:
                if self.oper_password and not self._oper:
                    await self._send_raw(f"OPER {self.oper_password}")
                    await asyncio.sleep(1.0)
                await self._send_raw(f"DESTROY {channel} :room closed")
                await asyncio.sleep(0.5)
                if not self._oper:
                    logger.warning(
                        "MIRC: destroy %s sent without oper — likely 481",
                        channel)
            except Exception as exc:
                logger.warning("MIRC: destroy %s failed: %s", channel, exc)
        self.extra_channels.discard(channel)
        return await self.part_channel(channel)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """MIRC has no typing indicator — no-op."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_channel = chat_id.startswith("#") or chat_id.startswith("&")
        return {
            "name": chat_id,
            "type": "group" if is_channel else "dm",
        }

    # ── Message splitting ─────────────────────────────────────────────────

    def _split_message(self, content: str, target: str,
                       extra_overhead: int = 0) -> List[str]:
        return [part[0] for part in self._message_parts(content, target, extra_overhead)]

    def _message_parts(self, content: str, target: str,
                       extra_overhead: int = 0) -> List[tuple[str, bool]]:
        """Split a message into MIRC-safe chunks, preserving line breaks.

        MIRC has a ~512 byte line limit.  After accounting for protocol
        overhead (``PRIVMSG <target> :``), we emit one chunk per source
        line (blank lines kept as paragraph gaps); only overlong single
        lines are chunk-split by bytes.  Batching (draft/multiline)
        reunites the chunks into ONE row with real line breaks, so
        packing lines with spaces is gone — and markdown goes through
        untouched (the client renders it).  ``extra_overhead`` reserves
        room for a tag prefix (batch sends).
        """
        # Leave room for the daemon's sender prefix as well as the client
        # command. Continuation tags let us split without changing the text.
        overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2 + 64
        max_bytes = 510 - overhead - extra_overhead
        user_limit = self.max_message_length

        chunks: List[tuple[str, bool]] = []
        for line in content.split("\n"):
            para = line
            concat = False
            if not para:
                chunks.append(("", False))
                continue
            while True:
                para_bytes = para.encode("utf-8")
                limit = min(user_limit, max_bytes)
                if len(para_bytes) <= limit:
                    chunks.append((para, concat))
                    break
                # Binary search for a safe character boundary <= limit
                low, high = 1, len(para)
                best = 0
                while low <= high:
                    mid = (low + high) // 2
                    if len(para[:mid].encode("utf-8")) <= limit:
                        best = mid
                        low = mid + 1
                    else:
                        high = mid - 1
                split_at = best
                chunks.append((para[:split_at], concat))
                para = para[split_at:]
                concat = True
        return chunks

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Convert basic markdown to plain text for non-rendering surfaces
        (standalone senders without batching). NEVER on the wire path:
        the client renders markdown, so the gateway sends it through."""
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

    # ── Raw MIRC I/O ──────────────────────────────────────────────────────

    async def _send_raw(self, line: str, *, timeout: float | None = None) -> None:
        """Send a raw MIRC protocol line.

        ``timeout`` bounds the drain: connect-phase sends pass one so a
        half-open socket fails loud (retryable reconnect) instead of
        stalling ``connect()`` forever with no error and no rooms.
        Steady-state sends keep timeout=None (today's behavior).
        """
        if not self._writer or self._writer.is_closing():
            return
        encoded = (line + "\r\n").encode("utf-8")
        self._writer.write(encoded)
        if timeout is None:
            await self._writer.drain()
        else:
            await asyncio.wait_for(self._writer.drain(), timeout)

    async def _receive_loop(self) -> None:
        """Main receive loop — reads lines, PONGs fast, queues the rest.

        PING answers and the watchdog arrival stamp happen HERE, never
        behind a multi-minute turn: the handler task below owns all slow
        work. Ordering is preserved (single consumer).
        """
        buffer = b""
        my_generation = getattr(self, "_conn_generation", 0)
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
                        logger.warning("MIRC: error handling line: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("MIRC: receive loop error: %s", e)
        finally:
            # Guard ALL drop effects, not just writer teardown: a stale
            # receiver must not stop the new handler or reconnect it again.
            if my_generation == getattr(self, "_conn_generation", 0):
                try:
                    self._line_queue.put_nowait(None)
                except Exception:
                    pass
                try:
                    self._drop_teardown(my_generation)
                except Exception:
                    pass
                if self.is_connected:
                    logger.warning("MIRC: connection lost, marking disconnected")
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
            msg = _parse_mirc_message(raw)
            params = msg.get("params") or []
            payload = params[0] if params else ""
            await self._send_raw(f"PONG :{payload}", timeout=CONNECT_SEND_TIMEOUT)
        except Exception as e:
            logger.warning("MIRC: error answering ping: %s", e)

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
                    logger.warning("MIRC: error handling line: %s", e)
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
                            "MIRC: server silent %.0fs — assuming half-open, reconnecting",
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
                        # Same local teardown as the receive-loop drop path:
                        # the cancelled task may never unwind (stuck read),
                        # so clean here too (idempotent if it does).
                        # Generation-guarded: a newer connect in flight keeps
                        # its state.
                        try:
                            self._drop_teardown(
                                getattr(self, "_conn_generation", None))
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
        """Dispatch a single MIRC protocol line."""
        self._last_inbound = time.monotonic()
        batch = ""
        if raw.startswith("@"):
            tagstr, _, raw = raw[1:].partition(" ")
            for part in tagstr.split(";"):
                k, _, v = part.partition("=")
                if k == "batch" and v:
                    batch = v[:64]
            if not raw:
                return
        msg = _parse_mirc_message(raw)
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

        # CAP negotiation replies (draft/multiline discovery)
        if command == "CAP" and len(params) >= 2:
            sub = params[1].upper()
            if sub == "LS" and len(params) >= 3:
                rest = params[2:]
                if rest and rest[0] == "*":
                    self._server_caps.update(" ".join(rest[1:]).split())
                else:
                    self._server_caps.update(" ".join(rest).split())
                    self._cap_event.set()
            elif sub in ("ACK", "NAK"):
                if sub == "ACK" and "draft/multiline" in " ".join(params[2:]).split():
                    self._server_multiline = True
                self._cap_event.set()
            return

        # RPL_YOUREOPER (381) / ERR_PASSWDMISMATCH (464) — oper state.
        if command == "381":
            self._oper = True
            return
        if command in {"464", "481"}:
            self._oper = False
            logger.warning("MIRC: oper/auth refused (%s) — room destroys will fail",
                           command)
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

        # BATCH frames (draft/multiline reassembly)
        if command == "BATCH":
            await self._close_in_batch(params)
            return

        # PRIVMSG — incoming message (channel or DM)
        if command == "PRIVMSG" and len(params) >= 2:
            sender_nick = _extract_nick(msg["prefix"])
            target = params[0]
            text = params[1]
            if batch:
                self._accumulate_in_batch(sender_nick, batch, target, text)
                return
            await self._route_text(sender_nick, target, text)

    def _accumulate_in_batch(self, sender_nick: str, ref: str, target: str, text: str) -> None:
        """Hold one tagged line; BATCH -ref flushes the burst as one turn."""
        key = (sender_nick.lower(), ref)
        entry = self._in_batches.get(key)
        if entry is None:
            if len(self._in_batches) >= 32:
                oldest = next(iter(self._in_batches))
                del self._in_batches[oldest]
            entry = {"sender": sender_nick, "target": target, "texts": []}
            self._in_batches[key] = entry
        entry["texts"].append(text)

    async def _close_in_batch(self, params: list) -> None:
        """BATCH -ref: join the burst, route once with explicit boundaries."""
        ref = next((p[1:] for p in params if p.startswith("-")), "")
        if not ref:
            return
        for key in [k for k in self._in_batches if k[1] == ref]:
            entry = self._in_batches.pop(key, None)
            if not entry or not entry["texts"]:
                continue
            await self._route_text(
                entry["sender"], entry["target"], "\n".join(entry["texts"]),
                immediate=True)

    async def _route_text(self, sender_nick: str, target: str, text: str,
                          *, immediate: bool = False) -> None:
        """Addressing, auth, then dispatch — now or after the quiet window.

        Batch-complete lines carry explicit boundaries, so they dispatch
        immediately; untagged lines wait out the quiet window so a paste
        becomes one turn instead of N interrupts.
        """
        # Ignore our own messages
        if sender_nick.lower() == self._current_nick.lower():
            return
        # NO host-based "relay" filter here — and never reintroduce one.
        # The daemon renders EVERY peer-to-peer channel line as
        # sender!relay@<server_name> (MIRC daemon _fanout; server_name is the
        # operator's custom observatory name, not a constant). A
        # !relay@ drop therefore swallows LIVE traffic: it shipped
        # once (v0.1.3..v0.1.22) and the symptom was a bot that
        # announces "Gateway online" and then answers nothing. The
        # loop it guarded against — history replay re-executing old
        # commands on reconnect — is gone SERVER-SIDE (no JOIN replay,
        # no history storage): there is nothing to re-execute.
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

        # A transport-only challenge proves that the nick is owned by the
        # receive/dispatch adapter, not an idle send-only identity. Private,
        # authenticated observatory traffic only; never invoke a model.
        probe = re.fullmatch(r"\x01MERCURY-PROBE ([a-f0-9]{32})\x01", text)
        if (probe and self._observatory_managed
                and not target.startswith(("#", "&"))):
            await self._send_raw(
                f"NOTICE {sender_nick} :\x01MERCURY-PROBE {probe.group(1)}\x01",
                timeout=CONNECT_SEND_TIMEOUT,
            )
            return
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
            logger.debug("MIRC: ignoring message from unauthorized user %s", sender_nick)
            return

        key = self._mirc_batch_key(chat_id, sender_nick)
        if immediate or self._is_mirc_command(text):
            # Batch-complete lines carry explicit boundaries; commands
            # run alone. Either way flush buffered text first so
            # ordering is preserved, then dispatch immediately.
            await self._flush_mirc_batch_now(key)
            await self._dispatch_message(
                text=text,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=sender_nick,
                user_name=sender_nick,
            )
        else:
            self._enqueue_mirc_text(
                key,
                text=text,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=sender_nick,
                user_name=sender_nick,
            )

    @staticmethod
    def _is_mirc_command(text: str) -> bool:
        """Slash commands and known bang verbs bypass the text batch."""
        stripped = (text or "").lstrip()
        if stripped.startswith("/"):
            return True
        return bang_to_slash(stripped) != stripped

    @staticmethod
    def _mirc_batch_key(chat_id: str, sender_nick: str) -> tuple:
        """Batch scope: one burst per sender per chat (no cross-talk)."""
        return (chat_id, sender_nick.lower())

    def _enqueue_mirc_text(self, key, *, text, chat_id, chat_type, user_id, user_name) -> None:
        """Buffer one line and restart the quiet-window flush timer."""
        buf = self._mirc_batches.get(key)
        if buf is None:
            buf = {"texts": [], "kwargs": {}, "task": None}
            self._mirc_batches[key] = buf
        buf["texts"].append(text)
        buf["kwargs"] = {"chat_id": chat_id, "chat_type": chat_type,
                         "user_id": user_id, "user_name": user_name}
        old = buf.get("task")
        if old is not None and not old.done():
            old.cancel()
        buf["task"] = asyncio.create_task(
            self._flush_mirc_batch_after(key, self._mirc_batch_delay))

    async def _flush_mirc_batch_after(self, key, delay: float) -> None:
        """Quiet-window timer: flush the burst as one dispatch."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._flush_mirc_batch_now(key)

    async def _flush_mirc_batch_now(self, key) -> bool:
        """Dispatch the buffered burst joined with newlines, once."""
        buf = self._mirc_batches.pop(key, None)
        if not buf or not buf["texts"]:
            return False
        kwargs = dict(buf["kwargs"])
        kwargs["text"] = "\n".join(buf["texts"])
        await self._dispatch_message(**kwargs)
        return True

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
        source = self.build_source(
            chat_id=chat_id, chat_name=chat_id, chat_type=chat_type,
            user_id=user_id, user_name=user_name,
        )

        # Inbound milestone (routing only, never content): proves room
        # messages reach the engine when lower levels are hidden.
        try:
            from observatory.rooms import route_channel as _diag_route

            _diag = _diag_route(chat_id)[0] if chat_type == "group" else "dm"
        except Exception:
            _diag = "route-error"
        logger.info(
            "MIRC: inbound chat=%s route=%s handler=%s",
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
                        if reply:
                            await self.send(chat_id, reply)
                        # The room owned this text: a gateway turn here would
                        # answer a second time in someone else's room. Slash
                        # commands still fall through (exit/status/...).
                        if not text.lstrip().startswith("/"):
                            return
                    else:
                        from observatory.rooms import classify_omp_slash
                        kind = classify_omp_slash(text)
                        if kind in ("observatory", "gateway"):
                            # Gateway-owned (room lifecycle, hermes-only,
                            # unknown): never pump into the omp task — the
                            # task would chew a command as a job (and leak
                            # system context answering it). Gateway dispatch
                            # below owns the reply.
                            pass
                        else:
                            store = getattr(self, "_session_store", None)
                            approval_session_key = store._generate_session_key(source) if store else None
                            reply = await manager.handle_omp_message(
                                chat_id, user_name, text, approval_session_key=approval_session_key,
                            )
                            if reply:
                                await self.send(chat_id, reply)
                            if kind == "omp":
                                # OMP owns it: answered (or silently started)
                                # above — gateway stays out, no hermes-flavored
                                # double answer.
                                return
                            # Plain chat: room owned it, no gateway turn.
                            return
            except Exception:
                logger.debug("MIRC: room route failed, falling through", exc_info=True)
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

        # Profile-spawned rooms (!spawn <name> -p <profile>): run the
        # gateway turn under the profile's home via the context-local
        # override (same seam as embedded /chat --open-profile) so the
        # agent reads the profile's config/memories/skills. Reset in
        # finally — the adapter serves many rooms concurrently.
        _profile_token = None
        if chat_type == "group":
            try:
                from observatory.rooms import get_room_manager
                from mercury_cli.profiles import get_profile_dir
                from mercury_constants import (
                    reset_hermes_home_override,
                    set_hermes_home_override,
                )
                _manager = get_room_manager()
                _row = _manager.node_for_channel(chat_id) if _manager else None
                _prof = ((_row.get("extra") or {}).get("profile")
                         if isinstance(_row, dict) else None)
                if _prof:
                    _profile_token = set_hermes_home_override(
                        str(get_profile_dir(_prof)))
            except Exception:
                logger.debug("MIRC: profile override lookup failed", exc_info=True)
        try:
            from observatory.thinking import thinking_started

            thinking_started(chat_id)
        except Exception:
            pass
        try:
            await self.handle_message(event)
        finally:
            if _profile_token is not None:
                try:
                    from mercury_constants import reset_hermes_home_override
                    reset_hermes_home_override(_profile_token)
                except Exception:
                    pass


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
    """Check if MIRC is configured.

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
    """TLS unless the host is loopback, private, or tailnet (our MIRC daemon is
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
    """Interactive `mercury gateway setup` flow for the MIRC platform.

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
            print_info(f"MIRC: already configured (server: {existing_server}"
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
    print_success(f"IRC configuration saved to {get_env_path()}")
    print_info("Restart the gateway for changes to take effect: mercury gateway restart")


def is_connected(config) -> bool:
    """Check whether MIRC is configured (env or config.yaml)."""
    extra = getattr(config, "extra", {}) or {}
    server = get_env_value("IRC_SERVER") or extra.get("server", "")
    return bool(server and _configured_channel(extra))


def _env_enablement() -> dict | None:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook (landed in the
    generic-plugin-interface migration) BEFORE adapter construction, so
    ``gateway status`` and ``get_connected_platforms()`` reflect env-only
    configuration without instantiating the MIRC client.  Returns ``None``
    when MIRC isn't minimally configured; the caller skips auto-enabling.

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


def _strip_mirc_control_chars(text: str) -> str:
    """Strip MIRC line terminators and the NUL byte from ``text``.

    MIRC commands are CRLF-delimited; a bare ``\\r`` or ``\\n`` in user
    content lets an attacker inject arbitrary MIRC commands (CTCP, JOIN,
    KICK).  ``\\x00`` is a protocol-illegal byte.  Everything else is
    valid in PRIVMSG payloads.
    """
    return text.replace("\r", " ").replace("\n", " ").replace("\x00", "")


def _is_mirc_channel(target: str) -> bool:
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
    """Open an ephemeral MIRC connection, send a PRIVMSG, and quit.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``mercury cron`` running as a
    separate process from ``mercury gateway``).  Without this hook,
    ``deliver=mirc`` cron jobs fail with ``No live adapter for platform``.

    The standalone client uses a distinct nick suffix (``-cron``) so it
    does not collide with the long-running gateway adapter that may already
    be holding the configured nickname on the same network.  When the
    target is a channel, the client JOINs it before sending PRIVMSG so
    networks with the default ``+n`` (no external messages) channel mode
    accept the delivery.

    ``thread_id`` and ``media_files`` are accepted for signature parity but
    are not meaningful on MIRC: MIRC has no native thread or attachment
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
    # Agent listener takes the agent password; public servers take PASS too.
    reg_password = _get_scoped_secret("IRC_AGENT_PASSWORD") or extra.get("agent_password", "") or server_password
    nickserv_password = _get_scoped_secret("IRC_NICKSERV_PASSWORD") or extra.get("nickserv_password", "")

    # Reject control characters in chat_id to block MIRC command injection.
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
    plain = MIRCAdapter._strip_markdown(message)

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
        if reg_password:
            await _raw(f"PASS {_strip_mirc_control_chars(reg_password)}")
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
            msg = _parse_mirc_message(decoded)
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
            await _raw(f"PRIVMSG NickServ :IDENTIFY {_strip_mirc_control_chars(nickserv_password)}")
            await asyncio.sleep(2)

        # JOIN before PRIVMSG.  MIRC channels with the default ``+n`` mode
        # (no external messages: Libera, OFTC, EFnet, IRCNet, undernet)
        # silently drop PRIVMSG from non-members.  Do not JOIN bare nicks
        # (DM target) or server queries.
        if _is_mirc_channel(target):
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
                jmsg = _parse_mirc_message(decoded)
                jcmd = jmsg["command"]
                if jcmd == "PING":
                    payload = jmsg["params"][0] if jmsg["params"] else ""
                    await _raw(f"PONG :{payload}")
                elif jcmd in {"366", "JOIN"}:
                    joined = True
                elif jcmd in {"403", "405", "471", "473", "474", "475"}:
                    return {"error": f"IRC standalone send: JOIN {target} rejected ({jcmd})"}

        # Bytes-aware per-line splitting so multi-line plain text never
        # exceeds the MIRC 510-byte protocol limit.  Reuses the same
        # algorithm as MIRCAdapter._split_message, with control-character
        # stripping per line to block CRLF injection from message content.
        overhead = len(f"PRIVMSG {target} :".encode("utf-8")) + 2
        max_bytes = 510 - overhead
        sent_any = False
        for paragraph in plain.split("\n"):
            paragraph = _strip_mirc_control_chars(paragraph).rstrip()
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
        label="MIRC",
        adapter_factory=lambda cfg: MIRCAdapter(cfg),
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
        # MIRC line limit after protocol overhead
        max_message_length=450,
        # Display
        emoji="💬",
        # MIRC doesn't have phone numbers to redact
        pii_safe=False,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via a fork of IRC called the observatory. "
            "This platform fully supports multi-line markdown with "
            "in-line LaTeX. Put commands, paths and literal snippets in backtick "
            "code spans or fenced code blocks, so shell dollars, underscores and "
            "asterisks remain literal. Only prose outside code is formatted. "
            "In channels, users may address you by your nick."
        ),
    )




def _is_hermes_known_verb(verb: str) -> bool:
    """is_gateway_known_command, cached (plugin scan must not run per message)."""
    try:
        from mercury_cli.commands import is_gateway_known_command
    except Exception:
        raise
    return bool(is_gateway_known_command(verb))


_is_hermes_known_verb = functools.lru_cache(maxsize=512)(_is_hermes_known_verb)


def _is_known_verb(verb: str) -> bool:
    """True when `verb` is a command on either engine.

    OMP verbs are a static mirror (see above); hermes verbs resolve
    dynamically so plugin commands work too. Results for the
    hermes side are cached — plugin discovery must not run per
    message. Never raises; on lookup failure only the core
    verbs still rewrite (the bot never breaks).
    """
    if verb in OMP_BANG_VERBS:
        return True
    try:
        return bool(_is_hermes_known_verb(verb))
    except Exception:
        return verb in ("spawn", "spawnomp", "exit", "stop",
                          "approve", "deny")


def bang_to_slash(text: str) -> str:
    """Rewrite a leading !verb to /verb for known verbs only.

    `!!` escapes the rewrite; anything else starting with `!` whose
    verb is unknown on both engines stays plain chat.
    """
    if text.startswith("!") and not text.startswith("!!"):
        verb, _, rest = text[1:].partition(" ")
        lowered = verb.lower()
        if lowered and _is_known_verb(lowered):
            return "/" + lowered + (" " + rest if rest.strip() else "")
    return text

# Compatibility class alias for existing platform extensions.
IRCAdapter = MIRCAdapter
