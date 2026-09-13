"""IRC adapter observatory multiplex: dynamic rooms over a live ircd."""
from __future__ import annotations

import asyncio
import types
from contextlib import asynccontextmanager

import pytest

from observatory.ircd import DaemonConfig, IrcDaemon


@asynccontextmanager
async def running_daemon(tmp_path, **kwargs):
    config = DaemonConfig(
        agent_port=0, bouncer_port=0, state_dir=str(tmp_path), **kwargs
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


@pytest.mark.asyncio
async def test_adapter_joins_says_and_tracks_managed(tmp_path) -> None:
    from plugins.platforms.irc.adapter import IRCAdapter

    async with running_daemon(tmp_path) as (d, port):
        adapter = IRCAdapter(_config(port))
        assert await adapter.connect()
        try:
            assert not adapter.is_managed("#ace")
            assert await adapter.join_channel("#ace")
            assert adapter.is_managed("#ace")
            assert await adapter.say("#ace", "hello room")
            await asyncio.sleep(0.3)
            assert [m.text for m in d.channel_history("#ace")] == ["hello room"]
            assert await adapter.part_channel("#ace")
            assert not adapter.is_managed("#ace")
        finally:
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_managed_rooms_skip_addressing(tmp_path) -> None:
    from plugins.platforms.irc.adapter import IRCAdapter

    async with running_daemon(tmp_path) as (_, port):
        adapter = IRCAdapter(_config(port))
        assert await adapter.connect()
        try:
            events = []

            async def _capture(event):  # base awaits the handler
                events.append(event)

            adapter._message_handler = _capture  # type: ignore[assignment]

            async def _handle(line: str) -> None:
                await adapter._handle_line(line)
                # base class processes inbound in the background
                for _ in range(20):
                    if events:
                        break
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.1)

            # managed (gateway) channel: plain text dispatches
            await _handle(":op!u@h PRIVMSG #mercury_gateway :hello gateway")
            assert [e.source.chat_id for e in events] == ["#mercury_gateway"]
            assert events[0].text == "hello gateway"
            events.clear()
            # unmanaged channel: plain text ignored…
            await _handle(":op!u@h PRIVMSG #random :hello random")
            assert events == []
            # …unless addressed
            await _handle(":op!u@h PRIVMSG #random :mercury_gateway: hello random")
            assert [e.source.chat_id for e in events] == ["#random"]
            assert events[0].text == "hello random"
        finally:
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_child_room_routes_to_steer_not_dispatch(tmp_path) -> None:
    from observatory import rooms
    from observatory.state import ObservatoryState
    from plugins.platforms.irc.adapter import IRCAdapter

    state = ObservatoryState(tmp_path / "state.db")
    state.add_node(
        "deleg-1",
        engine="omp",
        name="cow",
        slug="cow",
        mxid="cow",
        session_ref="s",
        extra={"kind": "delegate"},
    )
    state.set_room_id("deleg-1", "#gateway-cow")
    manager = rooms.RoomManager(state, None)
    rooms.set_room_manager(manager)
    try:
        async with running_daemon(tmp_path) as (_, port):
            adapter = IRCAdapter(_config(port))
            assert await adapter.connect()
            try:
                # the bot joins every room it manages (production does this
                # at room creation); unjoined rooms keep addressing rules
                assert await adapter.join_channel("#gateway-cow")
                events: list = []
                replies: list[tuple[str, str]] = []
                async def _send(chat_id: str, content: str, **kw):  # type: ignore[no-untyped-def]
                    replies.append((chat_id, content))
                    from plugins.platforms.irc.adapter import SendResult

                    return SendResult(success=True)

                adapter.send = _send  # type: ignore[method-assign]
                seen: list[str] = []
                rooms.register_child_steer("deleg-1", seen.append)
                try:
                    await adapter._handle_line(":op!u@h PRIVMSG #gateway-cow :stop that")
                finally:
                    rooms.drop_child_steer("deleg-1")
                assert seen == ["stop that"]
                assert events == []  # never reached gateway dispatch
                assert replies and replies[0][0] == "#gateway-cow"
            finally:
                await adapter.disconnect()
    finally:
        rooms.set_room_manager(None)


def test_derive_channel() -> None:
    from plugins.platforms.irc.adapter import _derive_channel

    assert _derive_channel("ace") == "#ace"
    assert _derive_channel("#ace") == "#ace"
    assert _derive_channel("  ") == ""


def test_tls_defaults() -> None:
    from plugins.platforms.irc.adapter import _tls_default_for_host

    assert _tls_default_for_host("127.0.0.1") is False
    assert _tls_default_for_host("localhost") is False
    assert _tls_default_for_host("100.86.76.11") is False
    assert _tls_default_for_host("192.168.1.5") is False
    assert _tls_default_for_host("node.tail123.ts.net") is False
    assert _tls_default_for_host("irc.libera.chat") is True


def test_channel_derivation_order(monkeypatch) -> None:
    import types

    from plugins.platforms.irc.adapter import IRCAdapter

    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_USE_TLS"):
        monkeypatch.delenv(key, raising=False)
    # explicit channel wins over nick derivation
    a = IRCAdapter(types.SimpleNamespace(extra={"server": "x", "nickname": "bot", "channel": "#kept"}))
    assert a.channel == "#kept"
    # nick derives the room
    b = IRCAdapter(types.SimpleNamespace(extra={"server": "x", "nickname": "ace"}))
    assert b.channel == "#ace"


def test_interactive_setup_four_prompts(monkeypatch) -> None:
    import mercury_cli.setup as setup_mod
    from plugins.platforms.irc import adapter as adapter_mod

    answers = iter(["127.0.0.1", "ace", "s3cret", "op"])
    saved: dict[str, str] = {}
    monkeypatch.setattr(setup_mod, "prompt",
                        lambda *a, **k: next(answers))
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: False)
    monkeypatch.setattr(setup_mod, "save_env_value",
                        lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(setup_mod, "get_env_value", lambda k, default="": "")
    monkeypatch.setattr(setup_mod, "print_header", lambda *a: None)
    monkeypatch.setattr(setup_mod, "print_info", lambda *a: None)
    monkeypatch.setattr(setup_mod, "print_warning", lambda *a: None)
    monkeypatch.setattr(setup_mod, "print_success", lambda *a: None)
    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL",
                "IRC_USE_TLS", "IRC_SERVER_PASSWORD",
                "IRC_ALLOWED_USERS", "IRC_ALLOW_ALL_USERS"):
        monkeypatch.delenv(key, raising=False)

    adapter_mod.interactive_setup()
    assert saved["IRC_SERVER"] == "127.0.0.1"
    assert saved["IRC_NICKNAME"] == "ace"
    assert saved["IRC_CHANNEL"] == "#ace"  # derived, never asked
    assert saved["IRC_USE_TLS"] == "false"  # derived from localhost
    assert saved["IRC_SERVER_PASSWORD"] == "s3cret"
    assert saved["IRC_ALLOWED_USERS"] == "op"  # only owner + bots
    assert saved["IRC_ALLOW_ALL_USERS"] == "false"
    assert "IRC_PORT" not in saved  # never asked
