"""A present nick is not proof of a working agent: test actual round trips."""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from observatory import identity, platform_hook, rooms, spawn
from observatory.ircd import DaemonConfig, IrcDaemon
from observatory.doctor import _Probe
from observatory.provision import ensure_gateway_node_in_state
from observatory.state import ObservatoryState
from plugins.platforms.irc.adapter import IRCAdapter
from tests.observatory.test_identity import RawClient, running_daemon


@pytest.mark.asyncio
async def test_reclaimed_nick_keeps_new_connection_in_rooms(tmp_path):
    async with running_daemon(tmp_path, tls_port=0) as (daemon, port):
        old, new, watcher = RawClient(), RawClient(), RawClient()
        try:
            for client, nick in ((old, "agent"), (watcher, "owner")):
                await client.connect(port)
                await client.send(f"NICK {nick}")
                await client.send(f"USER {nick} 0 * :test")
                await client.next_match(" 001 ")
                await client.send("JOIN #room")
                await client.next_match("JOIN #room")
            stale = daemon._clients["agent"]
            await new.connect(port)
            await new.send("NICK agent")
            await new.send("USER agent 0 * :test")
            await new.next_match(" 001 ")
            await new.send("JOIN #room")
            await new.next_match("JOIN #room")
            # Model an old receive task unwinding after the replacement
            # JOIN. Cleanup must be idempotent and ownership-scoped.
            await daemon._quit(stale, "connection closed")
            await watcher.send("PRIVMSG #room :inbound-after-reclaim")
            await new.next_match("inbound-after-reclaim", timeout=1)
        finally:
            for client in (old, new, watcher):
                await client.close()


@pytest.mark.asyncio
async def test_wrong_password_cannot_reclaim_working_gateway(tmp_path):
    async with running_daemon(tmp_path, tls_port=0, agent_password="agent-secret") as (daemon, port):
        gateway, impostor = RawClient(), RawClient()
        try:
            await gateway.connect(port)
            for line in ("PASS agent-secret", "NICK vm_gateway", "USER gateway 0 * :test"):
                await gateway.send(line)
            await gateway.next_match(" 001 ")
            original = daemon._clients["vm_gateway"]
            await impostor.connect(port)
            for line in ("PASS wrong-password", "NICK vm_gateway", "USER impostor 0 * :test"):
                await impostor.send(line)
            await impostor.next_match(" 464 ")
            assert daemon._clients["vm_gateway"] is original
            await gateway.send("PING :still-alive")
            await gateway.next_match("PONG", timeout=1)
        finally:
            await gateway.close()
            await impostor.close()


@pytest.mark.asyncio
async def test_probe_rejects_send_only_gateway_impostor(tmp_path):
    async with running_daemon(tmp_path, tls_port=0, password="test") as (daemon, port):
        conn = identity.IdentityConn(host="127.0.0.1", port=port,
                                     nick="vm_gateway", channel="#vm_gateway", password="test")
        probe = _Probe("127.0.0.1", port, "health-probe", "test")
        try:
            assert await conn.send("online")
            assert await asyncio.to_thread(probe.connect)
            assert "vm_gateway" in await asyncio.to_thread(probe.names, "#vm_gateway")
            assert not await asyncio.to_thread(probe.gateway_roundtrip, "vm_gateway", 0.2)
        finally:
            probe.close()
            await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_password", ["test", "agent-only-test"])
async def test_resync_preserves_gateway_and_all_room_roundtrips(tmp_path, monkeypatch, agent_password):
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
    state = ObservatoryState(home / "observatory/state.db")
    ensure_gateway_node_in_state(state, server_name="vm")
    for node_id, engine in (("hermes-room", "hermes"), ("omp-room", "omp")):
        state.add_node(node_id, engine=engine, name=node_id, slug=node_id,
                       mxid=node_id, session_ref=node_id)
        state.set_room_id(node_id, f"#vm_{node_id}")
    manager = rooms.RoomManager(state)
    registry = spawn.OrchestratorRegistry()
    monkeypatch.setattr(rooms, "_current_manager", manager)
    monkeypatch.setattr(identity, "_pool", identity.IdentityPool())
    monkeypatch.setattr(rooms, "_omp_rooms", {})
    monkeypatch.setattr(platform_hook, "LAST_BOOT",
                        SimpleNamespace(state=state, manager=manager, registry=registry,
                                        mercury_home=str(home)))
    # Explicit resync below, so socket registration and the baseline turn
    # finish before the potentially destructive operation under test.
    real_resync = platform_hook.boot_resync

    async def no_auto_resync():
        return {}

    monkeypatch.setattr(platform_hook, "boot_resync", no_auto_resync)
    daemon = IrcDaemon(DaemonConfig(agent_port=0, server_port=0, tls_port=0,
                                    server_name="vm", password="test",
                                    agent_password=agent_password, state_dir=str(home / "observatory")))
    await daemon.start()
    agent_port = daemon._servers[0].sockets[0].getsockname()[1]
    server_port = daemon._servers[1].sockets[0].getsockname()[1]
    for key, value in {"IRC_SERVER": "127.0.0.1", "IRC_PORT": str(agent_port),
                       "IRC_NICKNAME": "vm_gateway", "IRC_CHANNEL": "#vm_gateway",
                       "IRC_USE_TLS": "false", "IRC_MANAGED_BY": "observatory",
                       "IRC_SERVER_PASSWORD": "test", "IRC_AGENT_PASSWORD": agent_password}.items():
        monkeypatch.setenv(key, value)
    adapter = IRCAdapter(PlatformConfig(enabled=True, extra={"text_batch_delay_seconds": 0.01}))
    received = []

    async def handler(event):
        received.append((event.source.chat_id, event.text))
        return f"ACK:{event.text}"

    adapter.set_message_handler(handler)
    watcher = RawClient()
    previous_sink = rooms.get_bot_sink()

    class EchoOmp:
        def run_task(self, text):
            return {"summary": f"ACK:{text}", "turn_frames": []}

    child = EchoOmp()
    registry.register(spawn.OrchestratorHandle(node_id="omp-room", engine="omp",
                      name="omp-room", session_ref="omp-room", rpc=child))
    rooms.register_omp_room("omp-room", "#vm_omp-room", child)
    try:
        assert await adapter.connect()
        adapter._mark_connected()  # BasePlatformAdapter.start() does this.
        gateway_peer = daemon._clients["vm_gateway"]
        await watcher.connect(server_port)
        for line in ("PASS test", "NICK owner", "USER owner 0 * :test"):
            await watcher.send(line)
        await watcher.next_match(" 001 ")
        await watcher.next_match("JOIN #vm_gateway")
        await watcher.send("PRIVMSG #vm_gateway :before-resync")
        await watcher.next_match("ACK:before-resync", timeout=3)

        report = await real_resync(manager=manager, state=state, registry=registry)
        assert not report["failed"]
        await asyncio.sleep(0.05)
        assert daemon._clients["vm_gateway"] is gateway_peer, "resync stole the dispatch connection's nick"
        assert adapter.is_connected
        assert identity.get_pool().get("#vm_gateway") is None
        for channel in ("#vm_gateway", "#vm_hermes-room", "#vm_omp-room"):
            await watcher.send(f"PRIVMSG {channel} :after-resync-{channel}")
            await watcher.next_match(f"ACK:after-resync-{channel}" if channel != "#vm_omp-room"
                                     else f"ACK:[owner over IRC] after-resync-{channel}", timeout=3)
        assert ("#vm_hermes-room", "after-resync-#vm_hermes-room") in received
        # Resync is also used on reconnect: running it again must be safe.
        await real_resync(manager=manager, state=state, registry=registry)
        await watcher.send("PRIVMSG #vm_gateway :second-resync")
        await watcher.next_match("ACK:second-resync", timeout=3)
        # The Lounge fork's multiline paste wire path, not N user turns.
        await watcher.send("CAP REQ :draft/multiline")
        await watcher.next_match("ACK :draft/multiline")
        for line in ("BATCH +paste draft/multiline #vm_gateway",
                     "@batch=paste PRIVMSG #vm_gateway :**bold**",
                     "@batch=paste PRIVMSG #vm_gateway :$x_{i}=1$",
                     "BATCH -paste"):
            await watcher.send(line)
        await watcher.next_match("ACK:**bold**", timeout=3)
        await watcher.next_match("$x_{i}=1$", timeout=3)
        assert ("#vm_gateway", "**bold**\n$x_{i}=1$") in received
        probe = _Probe("127.0.0.1", server_port, "health-probe", "test")
        try:
            assert await asyncio.to_thread(probe.connect)
            names = await asyncio.to_thread(probe.names, "#vm_gateway")
            assert "health-probe" in names  # First sorted nick must not be dropped.
            before_probe = list(received)
            assert await asyncio.to_thread(probe.gateway_roundtrip, "vm_gateway", 1)
            assert received == before_probe  # No model turn for health checks.
        finally:
            probe.close()
    finally:
        for channel in ("#vm_gateway", "#vm_hermes-room", "#vm_omp-room"):
            await identity.drop_identity(channel)
        await watcher.close()
        await adapter.disconnect()
        rooms.set_bot_sink(previous_sink)
        await daemon.stop()
        state.close()
