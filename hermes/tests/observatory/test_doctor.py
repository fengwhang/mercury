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


def test_code_version_reads_declared_version(tmp_path) -> None:
    from observatory.doctor import _code_version

    hermes = tmp_path / "hermes"
    (hermes / "mercury_cli").mkdir(parents=True)
    (hermes / "mercury_cli" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8")
    assert _code_version(hermes) == "1.2.3"
    assert _code_version(tmp_path / "nope") is None


def test_listening_pid_finds_own_listener() -> None:
    import os as _os
    import socket as _socket

    from observatory.doctor import _listening_pid

    srv = _socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert _listening_pid(port) == _os.getpid()
    finally:
        srv.close()


def test_probe_server_caps_sees_multiline(tmp_path) -> None:
    """The caps probe sees draft/multiline on a live daemon, empty on refusal."""
    import socket as _socket

    from observatory import doctor as doctor_mod
    from observatory.ircd import DaemonConfig, IrcDaemon

    async def _run() -> None:
        config = DaemonConfig(
            agent_port=0, server_port=0,
            server_name="vm", password="s3cret", agent_password="a3cret",
            state_dir=str(tmp_path))
        d = IrcDaemon(config)
        await d.start()
        try:
            port = d._servers[1].sockets[0].getsockname()[1]
            caps = await __import__("asyncio").to_thread(
                doctor_mod._probe_server_caps, "127.0.0.1", port, "s3cret")
            assert "draft/multiline" in caps
        finally:
            await d.stop()

    import asyncio as _asyncio
    _asyncio.run(_run())
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert doctor_mod._probe_server_caps("127.0.0.1", free, "") == set()


@pytest.mark.asyncio
async def test_doctor_reports_multiline_caps(tmp_path, monkeypatch) -> None:
    """run_doctor gains a multiline-caps row when the server listener is up."""
    import asyncio
    import json as _json

    from observatory.doctor import run_doctor
    from observatory.ircd import DaemonConfig, IrcDaemon

    home = tmp_path / "mercury"
    (home / "observatory").mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    for key in ("IRC_SERVER", "IRC_PORT", "IRC_SERVER_PASSWORD",
                "IRC_CLIENT_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    config = DaemonConfig(
        agent_port=0, server_host="127.0.0.1", server_port=0,
        server_name="vm", password="s3cret", agent_password="a3cret",
        state_dir=str(tmp_path),
    )
    d = IrcDaemon(config)
    await d.start()
    try:
        server_port = d._servers[1].sockets[0].getsockname()[1]
        (home / "observatory" / "ircd.json").write_text(_json.dumps({
            "server_name": "vm", "agent_host": "127.0.0.1",
            "agent_port": 1, "server_host": "127.0.0.1",
            "server_port": server_port,
        }))
        (home / ".env").write_text("IRC_CLIENT_PASSWORD=s3cret\n")
        results = await asyncio.to_thread(run_doctor, home)
        by_label = {label: (ok, detail) for ok, label, detail in results}
        ok, detail = by_label["multiline caps"]
        assert ok is True, detail
    finally:
        await d.stop()


def test_doctor_frontend_row_shows_fork_versions(tmp_path, monkeypatch) -> None:
    """The frontend row names installed vs shipped fork (currency proof)."""
    import json as _json

    import observatory.lounge as lounge_mod
    from observatory.doctor import run_doctor

    home = tmp_path / "mercury"
    (home / "observatory").mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    for key in ("IRC_SERVER", "IRC_PORT", "IRC_SERVER_PASSWORD",
                "IRC_CLIENT_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    (home / "observatory" / "ircd.json").write_text(_json.dumps({
        "server_name": "vm", "agent_host": "127.0.0.1",
        "agent_port": 1, "server_host": "127.0.0.1",
        "server_port": 1,
    }))
    (home / ".env").write_text("IRC_CLIENT_PASSWORD=x\n")
    monkeypatch.setattr(lounge_mod, "lounge_unit_active", lambda: True)
    monkeypatch.setattr(
        lounge_mod, "fork_versions", lambda home=None: ("4.5.2-mercury.2", "4.5.2-mercury.3"))
    by_label = {label: (ok, detail)
                for ok, label, detail in run_doctor(home)}
    ok, detail = by_label["chat frontend"]
    assert ok is False
    assert "4.5.2-mercury.2" in detail and "4.5.2-mercury.3" in detail
