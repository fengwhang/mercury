"""Boot-path sends are best-effort: they may never abort gateway startup.

Regression: with ``drain_timeout=0`` the boot-send task is awaited
UNGUADED (``else: await boot_task``), so one poisoned delivery-ledger row
raised inside obligation redelivery — AFTER the home-channel announce —
and killed the gateway into a silent restart loop. The bot only ever
flashed into its room; the room showed "Gateway online" once and then
never answered anything.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_boot_sends_never_abort_startup(monkeypatch) -> None:
    runner = GatewayRunner(GatewayConfig())

    async def _boom(*a, **k):
        raise RuntimeError("poisoned")

    monkeypatch.setattr(runner, "_claim_pending_obligations", _boom)
    monkeypatch.setattr(runner, "_send_restart_notification", _boom)
    monkeypatch.setattr(
        runner, "_send_home_channel_startup_notifications", _boom)
    monkeypatch.setattr(runner, "_redeliver_claimed_obligations", _boom)
    monkeypatch.setattr(
        "gateway.run._clear_planned_restart_notification", lambda: None)
    # Must return cleanly: every step above raises.
    await runner._await_startup_boot_sends(planned_restart_notification_pending=True)


@pytest.mark.asyncio
async def test_redeliver_skips_malformed_rows() -> None:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    rows = [
        {"platform": "irc", "chat_id": "#x"},  # no content
        {"obligation_id": 1},                  # no platform
        "junk",                                # not even a dict
        {"platform": "definitely-not-real",
         "content": "hi", "obligation_id": 2},  # unknown platform
    ]
    assert await runner._redeliver_claimed_obligations(rows) == 0
