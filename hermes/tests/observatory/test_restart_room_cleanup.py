"""Restart expiry survives daemon replacement and cached mLounge joins."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from observatory.restart import prepare_room_cleanup
from observatory.spawn import read_purge_journal, replay_purge_journal
from observatory.state import ObservatoryState, StateError
from tests.observatory.test_ircd import RawClient, running_daemon


def provision_family(tmp_path, monkeypatch):
    from observatory import provision, rooms

    home = tmp_path / "installation"
    folder = home / "observatory"
    folder.mkdir(parents=True)
    monkeypatch.setattr(provision, "read_config", lambda selected=None: {"server_name": "nixpad"})
    monkeypatch.setattr(rooms, "get_room_manager", lambda: None)
    state = ObservatoryState(folder / "state.db")
    provision.ensure_gateway_node_in_state(state, server_name="nixpad")
    for node, parent, engine in (("hermes-root", None, "hermes"), ("omp-root", None, "omp"),
                               ("child", "hermes-root", "omp"), ("grandchild", "child", "hermes"),
                               ("dead-root", None, "hermes")):
        state.add_node(node, engine=engine, name=node, slug=node, mxid=node,
                       session_ref=node, parent_node_id=parent)
        state.set_room_id(node, f"#nixpad_{node}")
    state.mark_dead("dead-root")
    return home, folder, state


@pytest.mark.asyncio
async def test_restart_removes_descendants_and_orphans_not_custom_gateway(tmp_path, monkeypatch):
    home, folder, state = provision_family(tmp_path, monkeypatch)
    try:
        stale = ("#mercury_gateway", "#nixpad_ghost", "#nixpad_child", "#nixpad_grandchild", "#nixpad_dead-root")
        cleanup = prepare_room_cleanup(home)
        assert cleanup["expired_agents"] == 3
        assert cleanup["gateway"] == "#nixpad_gateway"
        assert {row["node_id"] for row in state.get_live()} == {"gw", "hermes-root", "omp-root"}
        assert read_purge_journal(state)
        for _ in range(2):
            async with running_daemon(folder, server_name="nixpad") as (daemon, port, server_port):
                observers, bot = [RawClient(), RawClient()], RawClient()
                try:
                    for observer, nick in zip(observers, ("desktop", "phone")):
                        await observer.connect(server_port)
                        await observer.register(nick)
                        await observer.next_match("JOIN #nixpad_omp-root")
                        for room in stale:
                            await observer.send(f"JOIN {room}")
                            await observer.next_match(f" PART {room} :room expired")
                    await bot.connect(port)
                    await bot.register("nixpad_gateway")
                    await bot.send("JOIN #mercury_gateway")
                    await bot.next_match(" PART #mercury_gateway :room expired")
                    assert set(daemon.channel_names()) == {
                        "#nixpad_gateway", "#nixpad_hermes-root", "#nixpad_omp-root"}
                    daemon._clients["nixpad_gateway"].oper = True
                    await bot.send("DESTROY #nixpad_gateway")
                    await bot.next_match("configured gateway room is protected")
                    assert "#nixpad_gateway" in daemon.channel_names()
                finally:
                    await bot.close()
                    for observer in observers:
                        await observer.close()
        sink = SimpleNamespace(destroy_channel=AsyncMock(return_value=True))
        assert await replay_purge_journal(state, bot=sink) == []
        for node in ("child", "grandchild", "dead-root"):
            with pytest.raises(StateError):
                state.get(node)
        state.add_node("replacement", engine="omp", name="child", slug="child", mxid="child",
                       session_ref="new-generation", parent_node_id="hermes-root")
        state.set_room_id("replacement", "#nixpad_child")
        async with running_daemon(folder, server_name="nixpad") as (daemon, port, _):
            bot = RawClient()
            try:
                await bot.connect(port)
                await bot.register("replacement")
                await bot.send("JOIN #nixpad_child")
                await bot.next_match("JOIN #nixpad_child")
            finally:
                await bot.close()
    finally:
        state.close()


def test_restart_cleanup_is_idempotent_and_protects_dead_gateway(tmp_path, monkeypatch):
    home, folder, state = provision_family(tmp_path, monkeypatch)
    try:
        state.mark_dead("gw")
        prepare_room_cleanup(home)
        assert state.get("gw")["status"] == "live"
        assert state.get("gw")["room_id"] == "#nixpad_gateway"
        first = read_purge_journal(state)
        prepare_room_cleanup(home)
        assert read_purge_journal(state) == first
    finally:
        state.close()


def test_cli_cleans_before_restart_with_mlounge_absent(tmp_path, monkeypatch):
    from mercury_cli.subcommands import observatory as command

    home, folder, state = provision_family(tmp_path, monkeypatch)
    events = []
    def restart_unit():
        assert {row["node_id"] for row in state.get_live()} == {"gw", "hermes-root", "omp-root"}
        events.append("daemon")
        return "installed"
    monkeypatch.setattr("observatory.provision.ensure_observatory_unit", restart_unit)
    monkeypatch.setattr("observatory.mlounge.status_mlounge", lambda: {"configured": False})
    monkeypatch.setattr(command, "_restart_gateway_now", lambda: events.append("gateway") or 0)
    monkeypatch.setattr("mercury_cli.setup._verify_gateway_bot", lambda **kwargs: (True, "present"))
    monkeypatch.setattr(command, "_verify_fleet", lambda args: 0)
    try:
        assert command._cmd_restart(SimpleNamespace(home=str(home))) == 0
        assert events == ["daemon", "gateway"]
    finally:
        state.close()


@pytest.mark.asyncio
async def test_destroying_an_orphan_persists_expiry_without_an_agent_row(tmp_path):
    async with running_daemon(tmp_path, server_name="nixpad") as (daemon, port, _):
        client = RawClient()
        try:
            await client.connect(port)
            await client.register("owner")
            await client.send("JOIN #old-zombie")
            await client.next_match("JOIN #old-zombie")
            assert await daemon.destroy_channel("#old-zombie") == 1
            await client.next_match(" PART #old-zombie ")
        finally:
            await client.close()
    async with running_daemon(tmp_path, server_name="nixpad") as (daemon, port, _):
        client = RawClient()
        try:
            await client.connect(port)
            await client.register("owner")
            await client.send("JOIN #old-zombie")
            await client.next_match(" PART #old-zombie :room expired")
            assert "#old-zombie" not in daemon.channel_names()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_late_join_fanout_cannot_recreate_frontend_room_after_destroy(tmp_path, monkeypatch):
    import asyncio

    async with running_daemon(tmp_path, server_name="nixpad") as (daemon, port, _):
        queued, release = asyncio.Event(), asyncio.Event()
        emit = daemon._emit_join
        async def delayed(peer, key, display):
            queued.set()
            await release.wait()
            await emit(peer, key, display)
        monkeypatch.setattr(daemon, "_emit_join", delayed)
        client = RawClient()
        try:
            await client.connect(port)
            await client.register("owner")
            await client.send("JOIN #zombie")
            await asyncio.wait_for(queued.wait(), 2)
            assert await daemon.destroy_channel("#zombie") == 1
            await client.next_match(" PART #zombie ")
            release.set()
            with pytest.raises(TimeoutError):
                await client.next_match(" JOIN #zombie", timeout=0.1)
            assert "#zombie" not in daemon.channel_names()
        finally:
            release.set()
            await client.close()
