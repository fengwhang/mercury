"""The managed MIRC gateway room restarts the stack, with mLounge optional."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_gateway_room_restart_launches_observatory_once(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    adapter._observatory_managed = True
    runner.adapters[Platform.MIRC] = adapter
    runner.config.platforms[Platform.MIRC] = PlatformConfig(
        enabled=True, home_channel=HomeChannel(Platform.MIRC, "#pi_gateway", "gateway"),
    )
    runner.request_restart = MagicMock()
    launched = []
    monkeypatch.setattr("gateway.run._resolve_hermes_bin", lambda: ["/bin/mercury-nightly"])
    def launch(command, *, request_id):
        import json
        rows = [json.loads(line) for line in
                (tmp_path / "logs" / "gateway-restart-requests.jsonl").read_text().splitlines()]
        assert rows[0]["actor"]["user_id"] == "owner"
        assert rows[0]["request_id"] == request_id
        assert rows[-1]["state"] == "accepted"
        launched.append(command)

    monkeypatch.setattr("observatory.restart.launch_observatory_restart", launch)
    event = MessageEvent(
        text="/restart", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.MIRC, chat_id="#pi_gateway", chat_type="group", user_id="owner"),
    )
    await runner._handle_restart_command(event)
    await runner._handle_restart_command(event)
    assert launched == [["/bin/mercury-nightly"]]
    runner.request_restart.assert_not_called()
    # Uses the full stack's own online announcement, not a gateway-only receipt.
    assert not (tmp_path / ".restart_notify.json").exists()


@pytest.mark.asyncio
async def test_failed_observatory_launch_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    adapter._observatory_managed = True
    runner.adapters[Platform.MIRC] = adapter
    runner.config.platforms[Platform.MIRC] = PlatformConfig(
        enabled=True, home_channel=HomeChannel(Platform.MIRC, "#pi_gateway", "gateway"),
    )
    monkeypatch.setattr("gateway.run._resolve_hermes_bin", lambda: ["/bin/mercury-nightly"])
    launches = []

    def launch(command, *, request_id):
        launches.append(command)
        if len(launches) == 1:
            raise RuntimeError("launcher unavailable")

    monkeypatch.setattr("observatory.restart.launch_observatory_restart", launch)
    event = MessageEvent(
        text="/restart", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.MIRC, chat_id="#pi_gateway", chat_type="group", user_id="owner"),
    )
    await runner._handle_restart_command(event)
    assert runner._observatory_restart_started is False
    await runner._handle_restart_command(event)
    assert len(launches) == 2


def test_service_restart_helper_escapes_gateway_cgroup_and_preserves_nightly(monkeypatch):
    from observatory import restart
    monkeypatch.setenv("INVOCATION_ID", "gateway-service")
    monkeypatch.setattr(restart.shutil, "which", lambda _name: "/bin/systemd-run")
    env = {"PATH": "/bin", "MERCURY_HOME": "/tmp/nightly", "MERCURY_CMD": "mercury-nightly",
           "_HERMES_GATEWAY": "1", "OPENROUTER_API_KEY": "synthetic-test-key"}
    monkeypatch.setattr("tools.environments.local.build_subprocess_env", lambda **_kwargs: dict(env))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(restart.subprocess, "run", run)
    restart.launch_observatory_restart(["/bin/mercury-nightly"], request_id="platform-receipt")
    argv, kwargs = calls[0]
    assert argv[0] == "/bin/systemd-run"
    assert "--user" in argv and "--collect" in argv
    assert argv[-3:] == ["/bin/mercury-nightly", "observatory", "restart"]
    assert "--setenv=MERCURY_HOME=/tmp/nightly" in argv
    assert "--setenv=MERCURY_RESTART_REQUEST_ID=platform-receipt" in argv
    assert all("synthetic-test-key" not in part for part in argv)
    assert "_HERMES_GATEWAY" not in kwargs["env"]


def test_service_launcher_failure_is_visible(monkeypatch):
    from observatory import restart
    monkeypatch.setenv("INVOCATION_ID", "gateway-service")
    monkeypatch.setattr(restart.shutil, "which", lambda _name: "/bin/systemd-run")
    monkeypatch.setattr("tools.environments.local.build_subprocess_env", lambda **_kwargs: {})
    monkeypatch.setattr(restart.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="launcher failed"):
        restart.launch_observatory_restart(["/bin/mercury-nightly"])


def test_detached_restart_helper_executes_cli_without_frontend(tmp_path, monkeypatch):
    import json
    import sys
    import time

    from observatory import restart

    monkeypatch.delenv("INVOCATION_ID", raising=False)
    receipt = tmp_path / "restart.json"
    cli = tmp_path / "fake_mercury.py"
    cli.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"Path({str(receipt)!r}).write_text(json.dumps({{\n"
        "'argv': sys.argv[1:], 'home': os.environ['MERCURY_HOME'],\n"
        "'gateway': os.environ.get('_HERMES_GATEWAY')}))\n"
    )
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    restart.launch_observatory_restart([sys.executable, str(cli)])
    deadline = time.monotonic() + 6
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert receipt.exists(), "Detached helper did not execute the CLI"
    assert json.loads(receipt.read_text()) == {
        "argv": ["observatory", "restart"], "home": str(tmp_path), "gateway": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("managed, channel", [(False, "#pi_gateway"), (True, "#other")])
async def test_other_mirc_rooms_keep_gateway_restart(tmp_path, monkeypatch, managed, channel):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter._observatory_managed = managed
    runner.adapters[Platform.MIRC] = adapter
    runner.config.platforms[Platform.MIRC] = PlatformConfig(
        enabled=True, home_channel=HomeChannel(Platform.MIRC, "#pi_gateway", "gateway"),
    )
    runner.request_restart = MagicMock()
    launch = MagicMock()
    monkeypatch.setattr("observatory.restart.launch_observatory_restart", launch)
    event = MessageEvent(
        text="/restart", message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.MIRC, chat_id=channel, chat_type="group", user_id="owner"),
    )
    await runner._handle_restart_command(event)
    runner.request_restart.assert_called_once()
    launch.assert_not_called()
    assert (tmp_path / ".restart_notify.json").exists()
