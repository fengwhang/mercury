"""Tests for the IRC platform adapter plugin."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/irc/adapter.py under a unique module name
# (plugin_adapter_irc) so it cannot collide with other plugin adapters
# loaded by sibling tests in the same xdist worker.
_irc_mod = load_plugin_adapter("irc")

_parse_irc_message = _irc_mod._parse_irc_message
_extract_nick = _irc_mod._extract_nick
IRCAdapter = _irc_mod.IRCAdapter
check_requirements = _irc_mod.check_requirements
validate_config = _irc_mod.validate_config
register = _irc_mod.register
_standalone_send = _irc_mod._standalone_send


class TestIRCProtocolHelpers:

    def test_parse_simple_command(self):
        msg = _parse_irc_message("PING :server.example.com")
        assert msg["command"] == "PING"
        assert msg["params"] == ["server.example.com"]
        assert msg["prefix"] == ""


    def test_extract_nick_full_prefix(self):
        assert _extract_nick("nick!user@host") == "nick"


# ── IRC Adapter ──────────────────────────────────────────────────────────


class TestIRCAdapterInit:


    def test_init_from_config_extra(self, monkeypatch):
        # Clear any env vars
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)

        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "server": "irc.libera.chat",
                "port": 6697,
                "nickname": "mercury",
                "channel": "#mercury-dev",
                "use_tls": True,
            },
        )
        adapter = IRCAdapter(cfg)

        assert adapter.server == "irc.libera.chat"
        assert adapter.port == 6697
        assert adapter.nickname == "mercury"
        assert adapter.channel == "#mercury-dev"
        assert adapter.use_tls is True


class TestIRCAdapterSend:

    @pytest.fixture
    def adapter(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "server": "localhost",
                "port": 6667,
                "nickname": "testbot",
                "channel": "#test",
                "use_tls": False,
            },
        )
        return IRCAdapter(cfg)


    @pytest.mark.asyncio
    async def test_send_success(self, adapter):
        writer = MagicMock()
        writer.is_closing = MagicMock(return_value=False)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        adapter._writer = writer

        result = await adapter.send("#test", "hello world")
        assert result.success is True
        assert result.message_id is not None
        # Verify PRIVMSG was sent
        writer.write.assert_called()
        sent_data = writer.write.call_args[0][0]
        assert b"PRIVMSG #test :hello world" in sent_data


class TestIRCAdapterMessageParsing:

    @pytest.fixture
    def adapter(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "server": "localhost",
                "port": 6667,
                "nickname": "mercury",
                "channel": "#test",
                "use_tls": False,
            },
        )
        a = IRCAdapter(cfg)
        a._current_nick = "mercury"
        a._registered = True
        return a


    @pytest.mark.asyncio
    async def test_managed_room_needs_no_addressing(self, adapter):
        """The bot's own room: plain text dispatches; addressed text strips."""
        dispatched = []

        async def capture_dispatch(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture_dispatch
        adapter._message_handler = AsyncMock()

        await adapter._handle_line(":user!u@host PRIVMSG #test :just talking")
        await adapter._handle_line(":user!u@host PRIVMSG #test :mercury: hello there")
        assert len(dispatched) == 2
        assert dispatched[0]["text"] == "just talking"
        assert dispatched[0]["chat_id"] == "#test"
        assert dispatched[1]["text"] == "hello there"

    @pytest.mark.asyncio
    async def test_unmanaged_channel_still_requires_addressing(self, adapter):
        """Channels outside the managed set keep the old addressed-only rule."""
        dispatched = []

        async def capture_dispatch(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture_dispatch
        adapter._message_handler = AsyncMock()

        await adapter._handle_line(":user!u@host PRIVMSG #other :just talking")
        assert len(dispatched) == 0
        await adapter._handle_line(":user!u@host PRIVMSG #other :mercury: hello")
        assert len(dispatched) == 1
        assert dispatched[0]["text"] == "hello"


    @pytest.mark.asyncio
    async def test_ctcp_action_converted(self, adapter):
        """CTCP ACTION (/me) should be converted to text."""
        dispatched = []

        async def capture_dispatch(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture_dispatch
        adapter._message_handler = AsyncMock()

        await adapter._handle_line(":user!u@host PRIVMSG mercury :\x01ACTION waves\x01")
        assert len(dispatched) == 1
        assert dispatched[0]["text"] == "* user waves"


    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self, monkeypatch):
        """Nicks not in allowlist should be ignored."""
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "server": "localhost",
                "port": 6667,
                "nickname": "mercury",
                "channel": "#test",
                "use_tls": False,
                "allowed_users": ["Admin", "BOB"],
            },
        )
        adapter = IRCAdapter(cfg)
        adapter._current_nick = "mercury"
        adapter._registered = True
        dispatched = []

        async def capture_dispatch(**kwargs):
            dispatched.append(kwargs)

        adapter._dispatch_message = capture_dispatch
        adapter._message_handler = AsyncMock()

        await adapter._handle_line(":eve!u@host PRIVMSG #test :mercury: hello")
        assert len(dispatched) == 0


class TestIRCAdapterSplitting:

    def test_split_respects_byte_limit(self):
        """Multi-byte characters should not exceed IRC byte limit."""
        # 100 japanese chars = 300 bytes in utf-8
        text = "あ" * 100
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True, extra={"server": "x", "channel": "#x"})
        adapter = IRCAdapter(cfg)
        adapter._current_nick = "bot"
        lines = adapter._split_message(text, "#test")
        for line in lines:
            overhead = len(f"PRIVMSG #test :{line}\r\n".encode("utf-8"))
            assert overhead <= 512, f"line over 512 bytes: {overhead}"


class TestIRCProtocolHelpersExtra:

    def test_parse_malformed_no_space(self):
        """A line starting with : but no space should not crash."""
        msg = _parse_irc_message(":justaprefix")
        assert msg["prefix"] == "justaprefix"
        assert msg["command"] == ""
        assert msg["params"] == []


class TestIRCAdapterMarkdown:


    def test_strip_link(self):
        result = IRCAdapter._strip_markdown("[click here](https://example.com)")
        assert result == "click here (https://example.com)"

    def test_strip_image(self):
        result = IRCAdapter._strip_markdown("![alt](https://example.com/img.png)")
        assert result == "https://example.com/img.png"


# ── Requirements / validation ────────────────────────────────────────────


class TestIRCRequirements:

    def test_check_requirements_with_env(self, monkeypatch):
        monkeypatch.setenv("IRC_SERVER", "irc.test.net")
        monkeypatch.setenv("IRC_CHANNEL", "#test")
        assert check_requirements() is True


    def test_validate_config_from_extra(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_CHANNEL"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(extra={"server": "irc.test.net", "channel": "#test"})
        assert validate_config(cfg) is True


# ── Plugin registration ──────────────────────────────────────────────────


class TestIRCPluginRegistration:
    """Test the register() entry point."""

    def test_register_adds_to_registry(self, monkeypatch):
        monkeypatch.setenv("IRC_SERVER", "irc.test.net")
        monkeypatch.setenv("IRC_CHANNEL", "#test")

        from gateway.platform_registry import platform_registry

        # Clean up if already registered
        platform_registry.unregister("irc")

        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        call_kwargs = ctx.register_platform.call_args
        assert call_kwargs[1]["name"] == "irc" or call_kwargs[0][0] == "irc" if call_kwargs[0] else call_kwargs[1]["name"] == "irc"


# ── _standalone_send (out-of-process cron delivery) ──────────────────────


class _FakeIRCConnection:
    """A scripted reader/writer pair used to simulate an IRC server.

    Construct with the lines the server should respond with (already
    framed by ``\\r\\n``).  Captures every line written by the client so
    tests can assert NICK/USER/PRIVMSG/QUIT order.
    """

    def __init__(self, scripted_lines):
        self.writes: list[bytes] = []
        self._closed = False
        self._scripted = list(scripted_lines)
        self._buffer = b""

    # writer side ────────────────────────────────────────────────────
    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closed

    # reader side ────────────────────────────────────────────────────
    async def readuntil(self, separator: bytes = b"\r\n") -> bytes:
        if not self._scripted:
            raise asyncio.IncompleteReadError(b"", None)
        line = self._scripted.pop(0)
        if not line.endswith(b"\r\n"):
            line = line + b"\r\n"
        return line

    async def read(self, n: int = -1) -> bytes:
        return b""


class TestIRCStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_completes_handshake_and_sends_privmsg(self, monkeypatch):
        from gateway.config import PlatformConfig

        monkeypatch.setenv("IRC_SERVER", "irc.test.net")
        monkeypatch.setenv("IRC_CHANNEL", "#cron")
        monkeypatch.setenv("IRC_NICKNAME", "hermesbot")
        monkeypatch.setenv("IRC_USE_TLS", "false")

        # Server greets us with 001 RPL_WELCOME, then nothing for QUIT drain.
        conn = _FakeIRCConnection([b":server 001 hermesbot-cron :Welcome"])

        async def _fake_open(host, port, **kwargs):
            return conn, conn  # reader and writer share the same fake

        monkeypatch.setattr(_irc_mod.asyncio, "open_connection", _fake_open)

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}),
            "#cron",
            "hello from cron",
        )

        assert result["success"] is True
        assert "message_id" in result

        sent_lines = b"".join(conn.writes).decode("utf-8").splitlines()
        # NICK uses the cron-suffixed identity to avoid colliding with the
        # long-running gateway adapter that may already hold the nickname.
        assert any(line.startswith("NICK hermesbot-cron") for line in sent_lines)
        assert any(line.startswith("USER hermesbot-cron 0 * :Mercury (cron)")
                   for line in sent_lines)
        assert any(line == "PRIVMSG #cron :hello from cron" for line in sent_lines)
        assert any(line.startswith("QUIT ") for line in sent_lines)


    @pytest.mark.asyncio
    async def test_standalone_send_returns_error_on_registration_timeout(self, monkeypatch):
        from gateway.config import PlatformConfig

        monkeypatch.setenv("IRC_SERVER", "irc.test.net")
        monkeypatch.setenv("IRC_CHANNEL", "#cron")
        monkeypatch.setenv("IRC_NICKNAME", "hermesbot")
        monkeypatch.setenv("IRC_USE_TLS", "false")

        # No 001 response: the readuntil call returns IncompleteReadError so
        # the registration loop times out via the asyncio wait_for inside.
        conn = _FakeIRCConnection([])

        async def _fake_open(host, port, **kwargs):
            return conn, conn

        monkeypatch.setattr(_irc_mod.asyncio, "open_connection", _fake_open)

        # Patch wait_for to raise TimeoutError immediately so the test is fast
        async def _fast_timeout(coro, timeout):
            try:
                return await coro
            except asyncio.IncompleteReadError:
                raise asyncio.TimeoutError()

        monkeypatch.setattr(_irc_mod.asyncio, "wait_for", _fast_timeout)

        result = await _standalone_send(
            PlatformConfig(enabled=True, extra={}),
            "#cron",
            "hi",
        )

        assert "error" in result
        assert "registration" in result["error"].lower() or "timeout" in result["error"].lower()




class TestIRCAdapterIdentityRouting:
    @pytest.fixture
    def adapter(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from unittest.mock import MagicMock

        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={"server": "localhost", "port": 6667,
                   "nickname": "testbot", "channel": "#test",
                   "use_tls": False},
        )
        from plugins.platforms.irc.adapter import IRCAdapter
        adapter = IRCAdapter(cfg)
        writer = MagicMock()
        writer.is_closing = MagicMock(return_value=False)
        writer.write = MagicMock()
        from unittest.mock import AsyncMock
        writer.drain = AsyncMock()
        adapter._writer = writer
        return adapter

    @pytest.mark.asyncio
    async def test_send_prefers_room_identity(self, adapter, monkeypatch):
        from unittest.mock import AsyncMock

        from observatory import identity as identity_mod

        sent: list[str] = []

        class FakePool:
            def get(self, channel):
                return object() if channel == "#test" else None

        async def fake_send_as(channel, text):
            assert channel == "#test"
            sent.append(text)
            return True

        monkeypatch.setattr(identity_mod, "get_pool", lambda: FakePool())
        monkeypatch.setattr(identity_mod, "send_as_identity", fake_send_as)
        result = await adapter.send("#test", "hello identity")
        assert result.success is True
        assert sent == ["hello identity"]
        adapter._writer.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_falls_back_without_identity(self, adapter, monkeypatch):
        from observatory import identity as identity_mod

        class FakePool:
            def get(self, channel):
                return None

        monkeypatch.setattr(identity_mod, "get_pool", lambda: FakePool())
        result = await adapter.send("#test", "hello main")
        assert result.success is True
        sent_data = adapter._writer.write.call_args[0][0]
        assert b"PRIVMSG #test :hello main" in sent_data


class TestIRCSilenceWatchdog:
    def test_enable_keepalive_no_writer(self):
        from plugins.platforms.irc.adapter import _enable_keepalive
        _enable_keepalive(None)  # never raises

    def test_enable_keepalive_sets_socket_opt(self):
        import socket as _socket
        from plugins.platforms.irc.adapter import _enable_keepalive

        class FakeSock:
            def __init__(self):
                self.opts = []
            def setsockopt(self, *args):
                self.opts.append(args)

        class FakeWriter:
            def __init__(self, sock):
                self._sock = sock
            def get_extra_info(self, key):
                return self._sock if key == "socket" else None

        sock = FakeSock()
        _enable_keepalive(FakeWriter(sock))
        assert (_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1) in sock.opts

    @pytest.mark.asyncio
    async def test_watchdog_closes_silent_connection(self, monkeypatch):
        import time as _time
        from plugins.platforms.irc import adapter as adapter_mod
        from gateway.config import PlatformConfig
        from plugins.platforms.irc.adapter import IRCAdapter
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        cfg = PlatformConfig(
            enabled=True,
            extra={"server": "localhost", "port": 6667, "nickname": "watchbot",
                   "channel": "#test", "use_tls": False},
        )
        adapter = IRCAdapter(cfg)
        closed = []

        class FakeWriter:
            def is_closing(self):
                return False
            def close(self):
                closed.append(True)

        adapter._writer = FakeWriter()
        adapter._last_inbound = _time.monotonic() - 1000.0
        monkeypatch.setattr(adapter_mod, "WATCHDOG_POLL", 0.01)
        monkeypatch.setattr(adapter_mod, "SILENCE_LIMIT", 0.05)
        await adapter._silence_watchdog()
        assert closed == [True]

    @pytest.mark.asyncio
    async def test_watchdog_quiet_when_traffic_flows(self, monkeypatch):
        import time as _time
        from plugins.platforms.irc import adapter as adapter_mod
        from gateway.config import PlatformConfig
        from plugins.platforms.irc.adapter import IRCAdapter
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        cfg = PlatformConfig(
            enabled=True,
            extra={"server": "localhost", "port": 6667, "nickname": "watchbot",
                   "channel": "#test", "use_tls": False},
        )
        adapter = IRCAdapter(cfg)
        polls = []
        closed = []

        class FakeWriter:
            def is_closing(self):
                return False
            def close(self):
                closed.append(True)

        async def stop_after_first_sleep(delay):
            polls.append(delay)
            raise RuntimeError("stop")

        monkeypatch.setattr(adapter_mod.asyncio, "sleep", stop_after_first_sleep)
        adapter._writer = FakeWriter()
        adapter._last_inbound = _time.monotonic()
        with pytest.raises(RuntimeError):
            await adapter._silence_watchdog()
        assert polls
        assert closed == []


class TestIRCAgentEchoGuard:
    def _adapter(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        from plugins.platforms.irc.adapter import IRCAdapter
        cfg = PlatformConfig(
            enabled=True,
            extra={"server": "localhost", "port": 6667, "nickname": "watchbot",
                   "channel": "#test", "use_tls": False},
        )
        adapter = IRCAdapter(cfg)
        adapter.extra_channels = {"#vm_alpha"}
        return adapter

    @pytest.mark.asyncio
    async def test_agent_nick_message_dropped(self, monkeypatch):
        from types import SimpleNamespace
        from observatory import identity as identity_mod

        adapter = self._adapter(monkeypatch)
        pool = identity_mod.IdentityPool()
        pool.track(SimpleNamespace(channel="#vm_alpha", nick="vm_alpha"))
        monkeypatch.setattr(identity_mod, "_pool", pool)
        calls = []
        async def fake_dispatch(**kwargs):
            calls.append(kwargs)
        monkeypatch.setattr(adapter, "_dispatch_message", fake_dispatch)
        await adapter._handle_line(":vm_alpha!relay@mercury PRIVMSG #vm_alpha :my own output")
        assert calls == []

    @pytest.mark.asyncio
    async def test_human_message_still_dispatched(self, monkeypatch):
        from observatory import identity as identity_mod

        adapter = self._adapter(monkeypatch)
        monkeypatch.setattr(identity_mod, "_pool", identity_mod.IdentityPool())
        calls = []
        async def fake_dispatch(**kwargs):
            calls.append(kwargs)
        monkeypatch.setattr(adapter, "_dispatch_message", fake_dispatch)
        await adapter._handle_line(":owner!u@mercury PRIVMSG #test :watchbot: hello")
        assert len(calls) == 1
        assert calls[0]["chat_id"] == "#test"


class _StubRoomManager:
    def __init__(self, reply):
        self._reply = reply

    async def handle_omp_message(self, channel, sender, text):
        return self._reply

    async def handle_child_message(self, channel, sender, text):
        return self._reply


class TestIRCRoomOwnedDispatch:
    def _adapter(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        from plugins.platforms.irc.adapter import IRCAdapter
        cfg = PlatformConfig(
            enabled=True,
            extra={"server": "localhost", "port": 6667, "nickname": "watchbot",
                   "channel": "#test", "use_tls": False},
        )
        adapter = IRCAdapter(cfg)
        adapter.extra_channels = {"#vm_bravo"}
        return adapter

    def _room_route(self, monkeypatch, reply):
        from observatory import rooms as rooms_mod
        monkeypatch.setattr(
            rooms_mod, "route_channel", lambda channel: ("spawn-omp", {}))
        monkeypatch.setattr(
            rooms_mod, "get_room_manager", lambda: _StubRoomManager(reply))
        from observatory import identity as identity_mod
        monkeypatch.setattr(identity_mod, "_pool", identity_mod.IdentityPool())

    @pytest.mark.asyncio
    async def test_plain_text_no_gateway_turn(self, monkeypatch):
        adapter = self._adapter(monkeypatch)
        self._room_route(monkeypatch, "bravo says hi")
        sent = []
        async def fake_send(chat_id, content, *a, **k):
            sent.append((chat_id, content))
            from plugins.platforms.irc.adapter import SendResult
            return SendResult(success=True, message_id="1")
        monkeypatch.setattr(adapter, "send", fake_send)
        gateway_calls = []
        async def fake_handle(event):
            gateway_calls.append(event)
        monkeypatch.setattr(adapter, "handle_message", fake_handle)
        monkeypatch.setattr(adapter, "_message_handler", lambda event: None)
        await adapter._handle_line(":owner!u@mercury PRIVMSG #vm_bravo :hello")
        assert sent == [("#vm_bravo", "bravo says hi")]
        assert gateway_calls == []

    @pytest.mark.asyncio
    async def test_slash_falls_through(self, monkeypatch):
        adapter = self._adapter(monkeypatch)
        self._room_route(monkeypatch, "room ack")
        sent = []
        async def fake_send(chat_id, content, *a, **k):
            sent.append((chat_id, content))
            from plugins.platforms.irc.adapter import SendResult
            return SendResult(success=True, message_id="1")
        monkeypatch.setattr(adapter, "send", fake_send)
        gateway_calls = []
        async def fake_handle(event):
            gateway_calls.append(event)
        monkeypatch.setattr(adapter, "handle_message", fake_handle)
        monkeypatch.setattr(adapter, "_message_handler", lambda event: None)
        await adapter._handle_line(":owner!u@mercury PRIVMSG #vm_bravo :/exit")
        assert sent == [("#vm_bravo", "room ack")]
        assert len(gateway_calls) == 1


class TestIRCReadHandleSplit:
    def _adapter(self, monkeypatch):
        for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        from plugins.platforms.irc.adapter import IRCAdapter
        cfg = PlatformConfig(
            enabled=True,
            extra={"server": "localhost", "port": 6667, "nickname": "splitbot",
                   "channel": "#test", "use_tls": False},
        )
        return IRCAdapter(cfg)

    def test_is_ping_shapes(self):
        from plugins.platforms.irc.adapter import IRCAdapter
        assert IRCAdapter._is_ping("PING :abc") is True
        assert IRCAdapter._is_ping(":srv PING :srv") is True
        assert IRCAdapter._is_ping(":n!u@h PRIVMSG #t :hi") is False
        assert IRCAdapter._is_ping("") is False

    @pytest.mark.asyncio
    async def test_ping_answered_without_handler(self, monkeypatch):
        import asyncio as _asyncio
        adapter = self._adapter(monkeypatch)
        adapter._line_queue = _asyncio.Queue()
        written = []

        class FakeWriter:
            def is_closing(self):
                return False
            def write(self, data):
                written.append(data)
            async def drain(self):
                pass

        adapter._writer = FakeWriter()

        async def boom(line):
            raise AssertionError("handler must not see PING")

        monkeypatch.setattr(adapter, "_handle_line", boom)

        class FakeReader:
            def __init__(self):
                self._calls = 0
            def at_eof(self):
                return False
            async def read(self, n):
                self._calls += 1
                if self._calls == 1:
                    return b"PING :srv\r\n"
                return b""

        adapter._reader = FakeReader()
        await adapter._receive_loop()
        assert any(b"PONG" in w for w in written)
        # Only the EOF sentinel reached the queue, never the PING line.
        assert adapter._line_queue.qsize() == 1
        assert await adapter._line_queue.get() is None

    @pytest.mark.asyncio
    async def test_handler_task_consumes_in_order(self, monkeypatch):
        import asyncio as _asyncio
        adapter = self._adapter(monkeypatch)
        adapter._line_queue = _asyncio.Queue()
        seen = []

        async def fake_handle(line):
            seen.append(line)

        monkeypatch.setattr(adapter, "_handle_line", fake_handle)
        await adapter._line_queue.put(":a PRIVMSG #t :one")
        await adapter._line_queue.put(":a PRIVMSG #t :two")
        await adapter._line_queue.put(None)
        await adapter._handle_task()
        assert seen == [":a PRIVMSG #t :one", ":a PRIVMSG #t :two"]
