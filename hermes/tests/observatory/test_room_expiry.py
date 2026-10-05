"""Automatic expiry removes a real MIRC family from every observer."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from observatory.rooms import RoomManager
from observatory.spawn import read_purge_journal
from observatory.state import ObservatoryState, StateError
from tests.observatory.test_ircd import RawClient, running_daemon
from tests.observatory.test_mirc_adapter_liveness import make_adapter


@pytest.mark.asyncio
async def test_automatic_expiry_ejects_all_users_even_during_slow_message_handling(tmp_path, monkeypatch):
    from observatory import identity

    monkeypatch.setattr(identity, "drop_identity", AsyncMock())
    async with running_daemon(tmp_path, agent_password="test-secret") as (daemon, port, server_port):
        adapter = make_adapter(port, monkeypatch)
        adapter.agent_password = adapter.oper_password = "test-secret"
        state = ObservatoryState(tmp_path / "state.db")
        manager = RoomManager(state, adapter)
        rooms = {"root": "#test", "child": "#test-child", "grandchild": "#test-child-grandchild"}
        for node, parent in (("root", None), ("child", "root"), ("grandchild", "child")):
            state.add_node(node, engine="omp", name=node, slug=node, mxid=node,
                           session_ref=node, parent_node_id=parent)
            state.set_room_id(node, rooms[node])
        observers = [RawClient(), RawClient()]
        blocked, release = asyncio.Event(), asyncio.Event()
        original = adapter._handle_line

        async def slow_handler(line):
            if " PRIVMSG " in line and "hold-handler" in line:
                blocked.set()
                await release.wait()
            await original(line)

        monkeypatch.setattr(adapter, "_handle_line", slow_handler)
        try:
            assert await adapter.connect()
            for room in rooms.values():
                assert await adapter.join_channel(room)
            for client, nick in zip(observers, ("desktop", "phone")):
                await client.connect(server_port)
                await client.register(nick)
                for room in rooms.values():
                    await client.send(f"JOIN {room}")
                    await client.next_match(" 366 ")
            await manager._retire_child_room("grandchild")
            assert state.get("grandchild")["extra"]["task_state"] == "completed"
            assert rooms["grandchild"] in daemon.channel_names()
            channels_before = set(daemon.channel_names())
            await observers[0].send("PRIVMSG #test :hold-handler")
            await asyncio.wait_for(blocked.wait(), 5)
            # Control acknowledgements must bypass the blocked handler.
            await asyncio.wait_for(manager._retire_child_room("child"), 3)
            for client in observers:
                removed = {rooms["child"], rooms["grandchild"]}
                while removed:
                    line = await client.next_match(" PART ")
                    removed.discard(line.split(" PART ", 1)[1].split()[0])
            assert set(daemon.channel_names()) == channels_before - {rooms["child"], rooms["grandchild"]}
            assert state.get("root")["status"] == "live"
            for node in ("child", "grandchild"):
                with pytest.raises(StateError):
                    state.get(node)
            assert read_purge_journal(state) == []
            # Already destroyed is still an acknowledged, idempotent success.
            assert await adapter.destroy_channel(rooms["child"])
            # Cached clients cannot recreate either expired room.
            for client in observers:
                await client.send(f"JOIN {rooms['child']}")
                # Cached JOINs already queued during expiry can leave an earlier
                # destruction PART in the socket queue. Match this JOIN refusal.
                await client.next_match(f" PART {rooms['child']} :room expired")
            assert rooms["child"] not in daemon.channel_names()
            # Explicitly spawning a new generation permits name reuse.
            state.add_node("replacement", engine="omp", name="child", slug="child", mxid="child",
                           session_ref="replacement", parent_node_id="root")
            state.set_room_id("replacement", rooms["child"])
            await observers[0].send(f"JOIN {rooms['child']}")
            await observers[0].next_match(f"JOIN {rooms['child']}")
            assert rooms["child"] in daemon.channel_names()
        finally:
            release.set()
            for client in observers:
                await client.close()
            await adapter.disconnect()
            state.close()


@pytest.mark.asyncio
async def test_refused_destroy_frees_row_now_and_retries_channel_later(tmp_path, monkeypatch):
    """A refused OPER costs a retry, never a permanent zombie.

    Previously the node row was held until ``OPER DESTROY`` converged, so a
    wrong/absent oper password left ``status='dead'`` rows and empty rooms
    behind forever. The contract now: the row and the sidebar entry are
    released at death time; only the channel destroy stays queued.
    """
    from observatory import identity

    monkeypatch.setattr(identity, "drop_identity", AsyncMock())
    async with running_daemon(tmp_path, agent_password="test-secret") as (daemon, port, _):
        adapter = make_adapter(port, monkeypatch)
        adapter.agent_password = "test-secret"
        adapter.oper_password = "wrong-secret"
        state = ObservatoryState(tmp_path / "family.db")
        for node, parent in (("root", None), ("child", "root")):
            state.add_node(node, engine="omp", name=node, slug=node, mxid=node,
                           session_ref=node, parent_node_id=parent)
            state.set_room_id(node, f"#test-{node}")
        manager = RoomManager(state, adapter)
        try:
            assert await adapter.connect()
            assert await adapter.join_channel("#test-child")
            # join_channel reports intent; wait for the daemon to actually
            # materialise the room so the assertions below are deterministic
            # under full-suite load.
            async with asyncio.timeout(5):
                while "#test-child" not in daemon.channel_names():
                    await asyncio.sleep(0.01)
            await manager._retire_child_room("child")

            # THE FIX: the row is already gone although the destroy failed.
            with pytest.raises(StateError):
                state.get("child")
            assert state.get("root")["status"] == "live"
            # Only the channel destroy is still owed.
            entries = read_purge_journal(state)
            assert entries, "channel destroy must stay queued for retry"
            assert entries[0]["rows"] == []
            assert entries[0]["channels"] == ["#test-child"]
            assert "#test-child" in daemon.channel_names()

            # Fix the oper secret and let the background retry converge.
            adapter.oper_password = "test-secret"
            async with asyncio.timeout(5):
                while read_purge_journal(state):
                    await asyncio.sleep(0.02)
            assert read_purge_journal(state) == []
            assert "#test-child" not in daemon.channel_names()
        finally:
            await adapter.disconnect()
            state.close()
