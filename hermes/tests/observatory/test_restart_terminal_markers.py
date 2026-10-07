"""Connected boot replays terminal room status before depth-one removal."""
import os
import time

import pytest

from observatory.rooms import RoomManager
from observatory.spawn import OrchestratorRegistry
from observatory.state import ObservatoryState
from tests.observatory.test_subagent_room_lifecycle import _seed
from tools import async_delegation as ad


@pytest.mark.asyncio
async def test_boot_terminal_marker_precedes_room_removal(tmp_path, monkeypatch):
    from observatory import identity, platform_hook, rooms
    from gateway.status import get_process_start_time
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
    async def no_identity(*args, **kwargs):
        return None
    monkeypatch.setattr(identity, "ensure_identity", no_identity)
    state = ObservatoryState(tmp_path / "observatory" / "state.db")
    actions = []
    class Bot:
        async def join_channel(self, channel):
            actions.append(("join", channel, ""))
            return True
        async def say(self, channel, text, **kwargs):
            actions.append(("say", channel, text))
            return True
        async def destroy_channel(self, channel):
            actions.append(("destroy", channel, ""))
            return True
        async def part_channel(self, channel):
            actions.append(("part", channel, ""))
            return True
        async def invite_user(self, *args):
            return True
    bot = Bot()
    monkeypatch.setattr(rooms, "get_bot_sink", lambda: bot)
    manager = RoomManager(state, bot=bot)
    try:
        _seed(state, "root", depth=0, room="#fixture-root", parent=None)
        _seed(state, "durable/0", depth=1, room="#fixture-terminal", parent="root")
        _seed(state, "durable/1", depth=1, room="#fixture-running", parent="root")
        ad.record_child_spawn("durable/0", "durable", name="Terminal")
        ad.record_child_terminal("durable/0", "completed", summary="verified result")
        ad.record_child_spawn("durable/1", "durable", 1, child_pid=os.getpid(),
                              child_started_at=get_process_start_time(os.getpid()))
        report = await platform_hook.boot_resync(manager, state, OrchestratorRegistry())
        assert {row["node_id"] for row in state.get_live()} == {"root", "durable/1"}
        marker = next(i for i, action in enumerate(actions)
                      if action[0] == "say" and action[1] == "#fixture-terminal" and "verified result" in action[2])
        removal = next(i for i, action in enumerate(actions)
                       if action[0] in ("destroy", "part") and action[1] == "#fixture-terminal")
        assert marker < removal
        assert not any(action[0] in ("destroy", "part") and action[1] == "#fixture-root" for action in actions)
    finally:
        state.close()


def test_watcher_disappearance_does_not_fabricate_completed(tmp_path, monkeypatch):
    import asyncio
    from observatory import gateway_session
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = ObservatoryState(tmp_path / "observatory" / "state.db")
    manager = RoomManager(state)
    monkeypatch.setattr(gateway_session, "_watcher_manager", lambda: manager)
    monkeypatch.setattr(gateway_session, "_hop", lambda coro: asyncio.run(coro))
    try:
        _seed(state, "root", depth=0, room="#fixture-root", parent=None)
        _seed(state, "durable/0", depth=2, room="#fixture-retained", parent="root")
        ad.record_child_spawn("durable/0", "durable")
        ad.record_child_terminal("durable/0", "failed", error="real worker failure")
        gateway_session._retire_watcher_room("durable/0")
        assert state.get("durable/0")["extra"]["task_outcome"] == "failed"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_pending_marker_on_retained_child_retries_then_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = ObservatoryState(tmp_path / "observatory" / "state.db")
    sent = []
    class Bot:
        succeeds = False
        async def say(self, channel, text, **kwargs):
            sent.append((channel, text))
            return self.succeeds
    bot = Bot()
    manager = RoomManager(state, bot)
    try:
        _seed(state, "root", depth=0, room="#fixture-root", parent=None)
        _seed(state, "durable/0", depth=2, room="#fixture-retained", parent="root")
        ad.record_child_spawn("durable/0", "durable")
        ad.record_child_terminal("durable/0", "completed", summary="real terminal")
        await manager.reconcile_terminal_children()
        assert not state.get("durable/0")["extra"].get("terminal_marker_delivered")
        bot.succeeds = True
        await manager.reconcile_terminal_children()
        assert state.get("durable/0")["extra"]["terminal_marker_delivered"]
        count = len(sent)
        await manager.reconcile_terminal_children()
        assert len(sent) == count
    finally:
        state.close()


@pytest.mark.asyncio
async def test_daemon_boot_uses_configured_registry_not_foreign_cached_manager(tmp_path, monkeypatch):
    from observatory import rooms
    from tests.observatory.test_ircd import RawClient, running_daemon
    configured = ObservatoryState(tmp_path / "state.db")
    foreign = ObservatoryState(tmp_path / "foreign" / "state.db")
    _seed(configured, "configured", depth=0, room="#configured-child", parent=None)
    _seed(foreign, "foreign", depth=0, room="#foreign-child", parent=None)
    monkeypatch.setattr(rooms, "get_room_manager", lambda: RoomManager(foreign))
    observer = RawClient()
    try:
        async with running_daemon(tmp_path) as (daemon, _, server_port):
            await observer.connect(server_port)
            await observer.register("fixture-observer")
            await observer.next_match("JOIN #configured-child")
            assert "#foreign-child" not in daemon.channel_names()
    finally:
        await observer.close()
        configured.close()
        foreign.close()
