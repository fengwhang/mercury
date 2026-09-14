"""Server PING sweeps keep the client table truthful (no half-open ghosts)."""

from __future__ import annotations

import asyncio

import pytest

from observatory import ircd as ircd_mod
from observatory.ircd import DaemonConfig, IrcDaemon

from .test_ircd import RawClient, running_daemon


async def _wait_line(client: RawClient, prefix: str, timeout: float = 5.0) -> str:
    async with asyncio.timeout(timeout):
        while True:
            line = await client.lines.get()
            if line.startswith(prefix):
                return line


@pytest.mark.asyncio
async def test_idle_client_gets_ping_and_survives_pong(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(ircd_mod, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(ircd_mod, "PING_TIMEOUT", 0.4)
    async with running_daemon(tmp_path) as (d, agent_port, _):
        c = RawClient()
        await c.connect(agent_port)
        await c.register("pinger")
        ping = await _wait_line(c, f":{d.config.server_name} PING")
        assert ping
        await c.send(f"PONG :{d.config.server_name}")
        await asyncio.sleep(0.15)
        assert "pinger" in d._clients


@pytest.mark.asyncio
async def test_silent_client_is_dropped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ircd_mod, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(ircd_mod, "PING_TIMEOUT", 0.15)
    async with running_daemon(tmp_path) as (d, agent_port, _):
        c = RawClient()
        await c.connect(agent_port)
        await c.register("ghost")
        await _wait_line(c, f":{d.config.server_name} PING")
        # Never answers: the sweep must reap it.
        async with asyncio.timeout(5):
            while "ghost" in d._clients:
                await asyncio.sleep(0.02)
        assert "ghost" not in d._clients
