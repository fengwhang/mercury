"""Dispatch filtering: live peer lines always reach the agent.

Regression pin for v0.1.3..v0.1.22: a ``!relay@``-host filter dropped
every inbound channel message (the daemon renders ALL peer fanout as
sender!relay@<server_name>), so the bot announced "Gateway online" and
then answered nothing. History replay — the loop the filter fought — is
gone server-side, so nothing here may key on the message host. The
server name is the operator's custom observatory name (never a constant).
"""

from __future__ import annotations

import pytest


def _adapter(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.irc import adapter as adapter_mod

    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL",
                "IRC_USE_TLS", "IRC_MANAGED_BY"):
        monkeypatch.delenv(key, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "127.0.0.1", "port": 6669,
               "nickname": "nixpi4b_gateway", "channel": "#nixpi4b_gateway"},
    )
    return adapter_mod.IRCAdapter(cfg)


async def _record(monkeypatch):
    ad = _adapter(monkeypatch)
    seen: list[dict] = []

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    return ad, seen


@pytest.mark.asyncio
async def test_live_peer_line_dispatches(monkeypatch) -> None:
    """The exact wire shape of a user's live message must reach the agent."""
    ad, seen = await _record(monkeypatch)
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :hello")
    assert await ad._flush_irc_batch_now(("#nixpi4b_gateway", "owner")) is True
    assert len(seen) == 1
    assert seen[0]["text"] == "hello"
    assert seen[0]["chat_id"] == "#nixpi4b_gateway"


@pytest.mark.asyncio
async def test_live_command_line_dispatches(monkeypatch) -> None:
    """Commands arrive in the same shape — replay is gone, this is live."""
    ad, seen = await _record(monkeypatch)
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :/spawn lago")
    assert len(seen) == 1
    assert seen[0]["text"] == "/spawn lago"


@pytest.mark.asyncio
async def test_self_messages_stay_ignored(monkeypatch) -> None:
    ad, seen = await _record(monkeypatch)
    await ad._handle_line(
        ":nixpi4b_gateway!relay@nixpi4b PRIVMSG #nixpi4b_gateway :my output")
    assert seen == []
