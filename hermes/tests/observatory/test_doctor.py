"""Doctor diagnoses the user-to-agent path against a live daemon."""

from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_doctor_finds_present_bot(tmp_path, monkeypatch) -> None:
    import asyncio

    from observatory.doctor import run_doctor
    from observatory.ircd import DaemonConfig, IrcDaemon

    home = tmp_path / "mercury"
    (home / "observatory").mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    for key in ("IRC_SERVER", "IRC_PORT", "IRC_SERVER_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    config = DaemonConfig(
        agent_port=0, server_host="127.0.0.1", server_port=0,
        server_name="vm", password="s3cret", agent_password="a3cret",
        state_dir=str(tmp_path),
    )
    d = IrcDaemon(config)
    await d.start()
    try:
        agent_port = d._servers[0].sockets[0].getsockname()[1]
        server_port = d._servers[1].sockets[0].getsockname()[1]
        (home / "observatory" / "ircd.json").write_text(json.dumps({
            "server_name": "vm", "agent_host": "127.0.0.1",
            "agent_port": agent_port, "server_host": "127.0.0.1",
            "server_port": server_port,
        }))
        (home / ".env").write_text(
            "IRC_AGENT_PASSWORD=a3cret\nIRC_CLIENT_PASSWORD=s3cret\n"
            "IRC_SERVER_PASSWORD=a3cret\nIRC_SERVER=127.0.0.1\n"
            f"IRC_PORT={agent_port}\n")

        # A "bot" holding the gateway nick in the gateway room.
        reader, writer = await asyncio.open_connection("127.0.0.1", agent_port)
        try:
            writer.write(b"PASS a3cret\r\nNICK vm_gateway\r\n"
                         b"USER vm_gateway 0 * :t\r\n")
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.write(b"JOIN #vm_gateway\r\n")
            await writer.drain()
            await asyncio.sleep(0.3)
            results = await asyncio.to_thread(run_doctor, home)
        finally:
            writer.close()

        by_label = {label: (ok, detail) for ok, label, detail in results}
        assert by_label["agent listener"][0] is True
        assert by_label["server listener"][0] is True
        assert by_label["bot credential"][0] is True
        assert by_label["adapter target"][0] is True
        ok, detail = by_label["bot in room"]
        assert ok is True, detail
    finally:
        await d.stop()


def test_doctor_flags_credential_drift(tmp_path, monkeypatch) -> None:
    from observatory.doctor import run_doctor

    import socket as _socket

    def _free_port():
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    home = tmp_path / "mercury"
    (home / "observatory").mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    for key in ("IRC_SERVER", "IRC_PORT", "IRC_SERVER_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    agent_port, server_port = _free_port(), _free_port()
    (home / "observatory" / "ircd.json").write_text(json.dumps({
        "server_name": "vm", "agent_host": "127.0.0.1",
        "agent_port": agent_port, "server_host": "127.0.0.1",
        "server_port": server_port,
    }))
    (home / ".env").write_text(
        "IRC_AGENT_PASSWORD=new-a\nIRC_CLIENT_PASSWORD=b\n"
        "IRC_SERVER_PASSWORD=stale-a\n")
    by_label = {label: (ok, detail)
                for ok, label, detail in run_doctor(home)}
    assert by_label["bot credential"][0] is False
    assert by_label["agent listener"][0] is False  # nothing bound in tmp
