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
