"""OMP-room slash routing: omp verbs stay in-engine, gateway owns the rest.

Regression: `!help` in an omp room used to pump "/help" into the omp
task AND fall through to the hermes help reply (wrong-engine answer
plus system-context leakage into the task). Now omp-owned verbs never
reach gateway dispatch, and gateway-owned verbs never enter the task.
"""

from __future__ import annotations

import pytest

from observatory.rooms import classify_omp_slash


def test_classify_omp_slash() -> None:
    assert classify_omp_slash("hello there") == "chat"
    assert classify_omp_slash("!help") == "chat"  # bang is adapter-layer
    assert classify_omp_slash("/help") == "omp"
    assert classify_omp_slash("/HELP me") == "omp"
    assert classify_omp_slash("/model opus") == "omp"  # both engines: omp wins
    assert classify_omp_slash("/compact") == "omp"
    assert classify_omp_slash("/exit") == "observatory"
    assert classify_omp_slash("/stop now") == "observatory"
    assert classify_omp_slash("/approve") == "observatory"
    assert classify_omp_slash("/spawnomp x") == "observatory"
    assert classify_omp_slash("/whoami") == "gateway"
    assert classify_omp_slash("/nope123") == "gateway"
    assert classify_omp_slash("/") == "gateway"
    assert classify_omp_slash("") == "chat"


class _FakeManager:
    def __init__(self):
        self.pumped: list[str] = []

    async def handle_omp_message(self, channel, sender, text):
        self.pumped.append(text)
        return ""


def _adapter(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.irc import adapter as adapter_mod

    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL",
                "IRC_USE_TLS"):
        monkeypatch.delenv(key, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "127.0.0.1", "port": 6669,
               "nickname": "bot", "channel": "#bot"},
    )
    return adapter_mod.IRCAdapter(cfg)


@pytest.mark.asyncio
async def test_omp_room_routing(monkeypatch) -> None:
    import observatory.rooms as rooms_mod
    from plugins.platforms.irc import adapter as adapter_mod

    ad = _adapter(monkeypatch)
    mgr = _FakeManager()
    monkeypatch.setattr(rooms_mod, "get_room_manager", lambda: mgr)
    monkeypatch.setattr(
        rooms_mod, "route_channel", lambda channel: ("spawn-omp", {}))
    gatewayed: list[str] = []

    async def _spy(event):
        gatewayed.append(event.text)

    monkeypatch.setattr(ad, "handle_message", _spy)
    ad._message_handler = _spy

    async def _send(text):
        await ad._dispatch_message(
            text, "#vm_ace", "group", "user", "owner")

    await _send("!help")       # omp owns -> task only
    await _send("!model opus")  # both own -> omp only
    await _send("hello")       # chat -> task only
    await _send("!exit")       # observatory -> gateway only
    await _send("!whoami")     # hermes-only -> gateway only
    await _send("/nope123")    # unknown slash -> gateway owns the error
    await _send("!nope123")    # unknown bang stays chat -> task
    assert mgr.pumped == ["/help", "/model opus", "hello", "!nope123"]
    assert gatewayed == ["/exit", "/whoami", "/nope123"]
