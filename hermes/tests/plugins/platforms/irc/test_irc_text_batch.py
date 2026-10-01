"""IRC quiet-window coalescing: rapid untagged lines become one dispatch.

Wire truth: IRC has no multi-line PRIVMSG — a pasted paragraph arrives
as N separate PRIVMSG lines within milliseconds. Dispatching each line
immediately fires N steering interrupts at the agent instead of one
turn. The adapter holds plain text for a short quiet window and flushes
the burst joined with newlines. (Batch-framed peers skip the wait —
see test_irc_multiline_batch.py.)
"""

from __future__ import annotations

import asyncio

import pytest


def _adapter(monkeypatch, delay: float = 0.05):
    from gateway.config import PlatformConfig
    from plugins.platforms.mirc import adapter as adapter_mod

    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL",
                "IRC_USE_TLS", "IRC_MANAGED_BY", "IRC_TEXT_BATCH_DELAY_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "127.0.0.1", "port": 6669,
               "nickname": "nixpi4b_gateway", "channel": "#nixpi4b_gateway",
               "text_batch_delay_seconds": delay},
    )
    return adapter_mod.MIRCAdapter(cfg)


@pytest.mark.asyncio
async def test_rapid_lines_coalesce_into_one_dispatch(monkeypatch) -> None:
    """Three paste lines in one burst → exactly one dispatch, joined."""
    ad = _adapter(monkeypatch)
    seen: list[dict] = []

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :line one")
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :line two")
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :line three")
    await asyncio.sleep(0.3)
    assert len(seen) == 1
    assert seen[0]["text"] == "line one\nline two\nline three"


@pytest.mark.asyncio
async def test_command_flushes_pending_text_first(monkeypatch) -> None:
    """Buffered chat flushes before a command; the command runs alone."""
    ad = _adapter(monkeypatch)
    seen: list[dict] = []

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :first thought")
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :/stop")
    await asyncio.sleep(0.3)
    assert [d["text"] for d in seen] == ["first thought", "/stop"]


@pytest.mark.asyncio
async def test_senders_never_merge(monkeypatch) -> None:
    """Two nicks pasting at once keep their own turns."""
    ad = _adapter(monkeypatch)
    seen: list[dict] = []

    async def fake_dispatch(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(ad, "_dispatch_message", fake_dispatch)
    await ad._handle_line(
        ":owner!relay@nixpi4b PRIVMSG #nixpi4b_gateway :owner line")
    await ad._handle_line(
        ":friend!relay@nixpi4b PRIVMSG #nixpi4b_gateway :friend line")
    await asyncio.sleep(0.3)
    assert sorted(d["text"] for d in seen) == ["friend line", "owner line"]


def test_default_batch_window_is_subperceptual(monkeypatch) -> None:
    """No end-of-paste marker exists on the wire, so a short hold is
    inherent — but it must stay below human perception, not a full second."""
    from gateway.config import PlatformConfig
    from plugins.platforms.mirc import adapter as adapter_mod

    for key in ("IRC_SERVER", "IRC_TEXT_BATCH_DELAY_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "127.0.0.1", "port": 6669,
               "nickname": "nixpi4b_gateway", "channel": "#nixpi4b_gateway"},
    )
    assert adapter_mod.MIRCAdapter(cfg)._mirc_batch_delay == 0.25
