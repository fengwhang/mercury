"""boot_resync heals the lobby subscription without a setup run."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest


class _FakeManager:
    async def drain_queue(self):
        return 0


class _FakeState:
    def get_live(self):
        return []


@pytest.mark.asyncio
async def test_resync_subscribes_lobby(monkeypatch) -> None:
    import observatory.platform_hook as hook
    import observatory.rooms as rooms_mod
    import observatory.spawn as spawn_mod
    import observatory.soju as soju_mod
    from observatory import provision as provision_mod

    monkeypatch.setattr(rooms_mod, "get_bot_sink", lambda: None)
    monkeypatch.setattr(
        rooms_mod, "start_pump", lambda manager: __import__("asyncio").sleep(0))
    monkeypatch.setattr(
        spawn_mod, "replay_purge_journal", lambda state: [])
    monkeypatch.setattr(
        provision_mod, "live_server_name", lambda home=None: "vm")
    seen = []
    monkeypatch.setattr(
        soju_mod, "subscribe_user_channel",
        lambda channel, home=None: seen.append(channel) or True)
    report = await hook.boot_resync(
        manager=_FakeManager(), state=_FakeState())
    assert seen == ["#vm_gateway"]
    assert report.get("lobby") == "#vm_gateway"
