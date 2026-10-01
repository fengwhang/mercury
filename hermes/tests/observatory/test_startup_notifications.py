"""Readiness replaces transport churn without announcing failed restorations."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from observatory import platform_hook
from plugins.platforms.mirc.adapter import MIRCAdapter
from tests.gateway.restart_test_helpers import make_restart_runner


def adapter(monkeypatch):
    for key in ("IRC_SERVER", "IRC_NICKNAME", "IRC_CHANNEL", "IRC_MANAGED_BY"):
        monkeypatch.delenv(key, raising=False)
    result = MIRCAdapter(PlatformConfig(enabled=True, extra={
        "managed_by": "observatory", "channel": "#vm_gateway",
    }))
    result._conn_generation = 1
    result.say = AsyncMock(return_value=True)
    result.send = AsyncMock(return_value=SendResult(success=True))
    return result


@pytest.mark.asyncio
async def test_resync_emits_one_status_per_room_and_no_extra_gateway_notice(monkeypatch):
    bot = adapter(monkeypatch)
    ready = asyncio.Event()

    async def resync():
        await ready.wait()
        return {"joined": ["#vm_gateway", "#child", "#CHILD"], "failed": []}

    monkeypatch.setattr(platform_hook, "boot_resync", resync)
    bot._observatory_resync_task = asyncio.create_task(bot._resync_observatory(1))
    runner, _ = make_restart_runner()
    runner.adapters = {Platform.MIRC: bot}
    runner.config.platforms = {Platform.MIRC: PlatformConfig(enabled=True, home_channel=HomeChannel(
        platform=Platform.MIRC, chat_id="#vm_gateway", name="Gateway"))}
    notification = asyncio.create_task(runner._send_home_channel_startup_notifications())
    await asyncio.sleep(0)
    bot.say.assert_not_awaited()
    ready.set()
    assert await notification == {("irc", "#vm_gateway", None)}
    assert [call.args[0] for call in bot.say.await_args_list] == ["#child", "#vm_gateway"]
    for call in bot.say.await_args_list:
        assert call.args[1] == "Observatory online - Mercury is back and ready"
        assert call.kwargs == {"kind": "status"}
    bot.send.assert_not_awaited()
    await bot._resync_observatory(1)
    assert bot.say.await_count == 2

    # A genuine reconnect is a new restoration, with one new status per room.
    bot._conn_generation = 2
    bot._observatory_online_channels.clear()
    await bot._resync_observatory(2)
    assert bot.say.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("report", [{"joined": [], "failed": []},
                                   {"joined": ["#child"], "failed": ["child failed"]}])
async def test_incomplete_resync_does_not_claim_observatory_is_ready(monkeypatch, report):
    bot = adapter(monkeypatch)
    monkeypatch.setattr(platform_hook, "boot_resync", AsyncMock(return_value=report))
    await bot._resync_observatory(1)
    bot.say.assert_not_awaited()
    assert await bot.observatory_startup_channels() == set()


@pytest.mark.asyncio
async def test_old_connection_resync_cannot_announce_on_new_connection(monkeypatch):
    bot = adapter(monkeypatch)
    monkeypatch.setattr(platform_hook, "boot_resync", AsyncMock(return_value={
        "joined": ["#child"], "failed": []}))
    bot._conn_generation = 2
    await bot._resync_observatory(1)
    bot.say.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_setting_disables_readiness_posts(monkeypatch):
    bot = adapter(monkeypatch)
    bot.config.gateway_restart_notification = False
    monkeypatch.setattr(platform_hook, "boot_resync", AsyncMock(return_value={
        "joined": ["#child"], "failed": []}))
    await bot._resync_observatory(1)
    bot.say.assert_not_awaited()
