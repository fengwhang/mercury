"""boot_resync invites the lounge client to the lobby without a setup run."""

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
    from observatory import provision as provision_mod

    invited = []

    class FakeBot:
        async def invite_user(self, nick, channel):
            invited.append((nick, channel))
            return True

    monkeypatch.setattr(rooms_mod, "get_bot_sink", lambda: FakeBot())
    monkeypatch.setattr(
        spawn_mod, "replay_purge_journal", lambda state: [])
    monkeypatch.setattr(
        provision_mod, "live_server_name", lambda home=None: "vm")
    monkeypatch.setattr(
        provision_mod, "get_lounge_nick", lambda home=None: "owner")
    report = await hook.boot_resync(
        manager=_FakeManager(), state=_FakeState())
    assert report.get("lobby") == "#vm_gateway"
    assert invited == [("owner", "#vm_gateway")]
    assert report.get("lobby_invited") is True


class _FakeRegistry:
    def __init__(self):
        self.handles = {}

    def get(self, node_id):
        return self.handles.get(node_id)

    def register(self, handle):
        self.handles[handle.node_id] = handle


@pytest.mark.asyncio
async def test_resync_rebuilds_live_omp_child(monkeypatch) -> None:
    """A gateway restart must bring spawned omp agents back: live omp
    rows with no registry handle get a resumed child + room pump."""
    import observatory.platform_hook as hook
    import observatory.rooms as rooms_mod
    import observatory.spawn as spawn_mod
    from observatory import provision as provision_mod

    built = {}

    class FakeChild:
        model = "m"

    def fake_build(**kwargs):
        built.update(kwargs)
        return FakeChild()

    pumped = []
    joined = []

    class FakeBot:
        async def join_channel(self, channel):
            joined.append(channel)
            return True

        async def invite_user(self, nick, channel):
            return True

    class _LiveState:
        def get_live(self):
            return [{
                "node_id": "orch-1", "engine": "omp", "status": "live",
                "name": "king", "session_ref": "s.jsonl",
                "room_id": "#king", "mxid": "",
            }]

    monkeypatch.setattr(rooms_mod, "get_bot_sink", lambda: FakeBot())
    monkeypatch.setattr(spawn_mod, "replay_purge_journal", lambda state: [])
    monkeypatch.setattr(spawn_mod, "build_omp_child", fake_build)
    monkeypatch.setattr(
        rooms_mod, "register_omp_room",
        lambda node_id, channel, child: pumped.append((node_id, channel)))
    monkeypatch.setattr(
        provision_mod, "live_server_name", lambda home=None: "vm")
    monkeypatch.setattr(
        provision_mod, "get_lounge_nick", lambda home=None: "owner")
    registry = _FakeRegistry()
    report = await hook.boot_resync(
        manager=None, state=_LiveState(), registry=registry)
    assert built.get("resume_session") == "s.jsonl"
    assert registry.get("orch-1") is not None
    assert pumped == [("orch-1", "#king")]
    assert "#king" in joined
    assert "orch-1" in report.get("resumed", [])
