"""History replay is display-only: never executed as commands."""

from __future__ import annotations

import pytest


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
async def test_relay_host_privmsg_never_dispatches(monkeypatch) -> None:
    ad = _adapter(monkeypatch)
    seen = []
    ad._message_handler = None

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    await ad._handle_line(
        ":owner!relay@mercury PRIVMSG #vm_gateway :/spawn lago")
    await ad._handle_line(
        ":owner!relay@mercury PRIVMSG #vm_gateway :mercury --version")
    assert seen == []


@pytest.mark.asyncio
async def test_live_privmsg_still_dispatches(monkeypatch) -> None:
    ad = _adapter(monkeypatch)

    seen = []

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    await ad._handle_line(":owner!owner@mercury PRIVMSG #bot :hello")
    assert len(seen) == 1 and seen[0]["text"] == "hello"
