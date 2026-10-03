"""Restart verification against real MIRC sockets and a durable agent roster."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from mercury_cli.subcommands import observatory as commands
from observatory.identity import IdentityConn
from observatory.mirc import DaemonConfig, MircDaemon
from observatory.state import ObservatoryState
from plugins.platforms.mirc.adapter import MIRCAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["reconnect", "ended", "missing"])
async def test_fleet_rechecks_missing_identity(tmp_path, monkeypatch, capsys, outcome):
    home = tmp_path / "mercury"
    directory = home / "observatory"
    directory.mkdir(parents=True)
    state = ObservatoryState(directory / "state.db")
    for node_id, nick in [("gw", "nixpi4_gateway"), ("testbot", "nixpi4_testbot")]:
        state.add_node(node_id, engine="hermes", name=node_id, slug=node_id,
                       mxid=nick, session_ref=node_id)
        state.set_room_id(node_id, "#" + nick)
    epoch = time.time()
    state.set_meta("last-resync", json.dumps({"epoch": epoch, "failed": []}))
    monkeypatch.setattr(commands, "_open_state", lambda home: state)
    daemon = MircDaemon(DaemonConfig(
        agent_port=0, server_host="127.0.0.1", server_port=0,
        state_dir=str(directory), server_name="nixpi4"))
    await daemon.start()
    agent_port = daemon._servers[0].sockets[0].getsockname()[1]
    server_port = daemon._servers[1].sockets[0].getsockname()[1]
    (directory / "ircd.json").write_text(json.dumps({
        "server_host": "127.0.0.1", "server_port": server_port}))
    gateway = MIRCAdapter(PlatformConfig(enabled=True, extra={
        "server": "127.0.0.1", "port": agent_port,
        "nickname": "nixpi4_gateway", "channel": "#nixpi4_gateway", "use_tls": False,
    }))
    gateway._observatory_managed = True
    monkeypatch.setattr(gateway, "_resync_observatory", AsyncMock())
    monkeypatch.setattr(gateway, "_wire_plugin_handlers", lambda context: None)
    conn = IdentityConn(host="127.0.0.1", port=agent_port, password="",
                        nick="nixpi4_testbot", channel="#nixpi4_testbot")
    join_requested = asyncio.Event()
    allow_join = asyncio.Event()
    original_join, original_names = daemon._cmd_join, daemon._cmd_names
    snapshots = []

    async def delayed_join(client, arg):
        if client.nick == conn.nick:
            join_requested.set()
            await allow_join.wait()
        await original_join(client, arg)

    async def names(client, arg):
        if arg == conn.channel:
            snapshots.append(conn.nick.lower() in daemon._channels.get(arg, set()))
            if outcome == "reconnect" and len(snapshots) == 2:
                allow_join.set()
            elif outcome == "ended":
                state.mark_dead("testbot")
        await original_names(client, arg)

    monkeypatch.setattr(daemon, "_cmd_join", delayed_join)
    monkeypatch.setattr(daemon, "_cmd_names", names)
    connection = None
    try:
        assert await gateway.connect()
        if outcome == "reconnect":
            connection = asyncio.create_task(conn.send("online"))
            await asyncio.wait_for(join_requested.wait(), 5)
            # A local drain is insufficient: no server JOIN receipt yet.
            handshake_was_pending = not connection.done()
        rc = await asyncio.wait_for(asyncio.to_thread(
            commands._verify_fleet, SimpleNamespace(home=home, _observatory_restart_epoch=epoch)), 15)
        captured = capsys.readouterr()
        assert snapshots[0] is False
        if outcome == "missing":
            assert rc == 1
            assert "nixpi4_testbot NOT present" in captured.out
            assert "1 check(s) failed" in captured.err
        else:
            assert rc == 0
            assert "fleet: FAIL" not in captured.out + captured.err
            count = 2 if outcome == "reconnect" else 1
            assert f"all {count} live agent(s) present" in captured.out
        assert "gateway dispatch transport round-trip ok" in captured.out
        if connection is not None:
            assert handshake_was_pending
            assert await asyncio.wait_for(connection, 5)
            assert snapshots[-1] is True
    finally:
        allow_join.set()
        if connection is not None:
            await asyncio.gather(connection, return_exceptions=True)
        await conn.close()
        await gateway.disconnect()
        await daemon.stop()
        state.close()
