"""Bot must authenticate to the agent listener with the agent password.

Regression: the adapter sent IRC_SERVER_PASSWORD on the agent port, so any
setup with distinct listener passwords 464'd forever (silent, invisible,
unresponsive bot). PASS now prefers IRC_AGENT_PASSWORD.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "hermes")


@pytest.mark.asyncio
async def test_adapter_registers_with_agent_password(tmp_path, monkeypatch) -> None:
    from observatory.ircd import DaemonConfig, IrcDaemon
    from plugins.platforms.irc.adapter import IRCAdapter
    from types import SimpleNamespace

    config = DaemonConfig(
        agent_port=0, server_port=0, state_dir=str(tmp_path),
        password="serverpw1", agent_password="agentpw2-distinct",
    )
    daemon = IrcDaemon(config)
    await daemon.start()
    agent_port = daemon._servers[0].sockets[0].getsockname()[1]
    try:
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
        monkeypatch.setenv("IRC_SERVER", "127.0.0.1")
        monkeypatch.setenv("IRC_PORT", str(agent_port))
        monkeypatch.setenv("IRC_NICKNAME", "testgw")
        monkeypatch.setenv("IRC_CHANNEL", "#testgw")
        monkeypatch.setenv("IRC_USE_TLS", "false")
        monkeypatch.setenv("IRC_SERVER_PASSWORD", "serverpw1")
        monkeypatch.setenv("IRC_AGENT_PASSWORD", "agentpw2-distinct")
        adapter = IRCAdapter(SimpleNamespace(extra={}))
        assert adapter.agent_password == "agentpw2-distinct"
        assert await adapter.connect() is True
        await adapter.disconnect()
    finally:
        await daemon.stop()


def test_adapter_falls_back_to_server_password(tmp_path, monkeypatch) -> None:
    from plugins.platforms.irc.adapter import IRCAdapter
    from types import SimpleNamespace

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("IRC_SERVER", "127.0.0.1")
    monkeypatch.setenv("IRC_CHANNEL", "#x")
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    monkeypatch.setenv("IRC_SERVER_PASSWORD", "only-pw")
    adapter = IRCAdapter(SimpleNamespace(extra={}))
    assert adapter.agent_password in ("", None)
    assert (adapter.agent_password or adapter.server_password) == "only-pw"
