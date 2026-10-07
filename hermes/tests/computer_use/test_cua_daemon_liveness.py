"""Only explicitly configured, safely parsed serve sockets are probed."""
import subprocess
from unittest.mock import patch

import pytest
from tools.computer_use import doctor, cua_backend_driver


def _unit(tmp_path, text, desktop=False):
    directory = tmp_path / ("autostart" if desktop else "systemd/user")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("driver.desktop" if desktop else "driver.service")).write_text(text)


def _guard(tmp_path, monkeypatch, answer):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    report = {"platform": "linux", "overall": "ok", "checks": []}
    with patch.object(cua_backend_driver, "cua_daemon_listening", return_value=answer, create=True) as probe:
        doctor._apply_daemon_liveness_guard(report, "/fixture/cua-driver")
    return report, probe


@pytest.mark.parametrize("answer,status,overall", [(True, "pass", "ok"), (False, "fail", "degraded"), (None, "skip", "ok")])
def test_configured_socket_has_explicit_tristate(tmp_path, monkeypatch, answer, status, overall):
    _unit(tmp_path, '[Service]\nExecStart="/fixture dir/cua-driver" serve --socket="%h/socket dir/driver.sock"\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    report, probe = _guard(tmp_path, monkeypatch, answer)
    probe.assert_called_once_with("/fixture/cua-driver", str(tmp_path / "socket dir/driver.sock"))
    assert report["overall"] == overall
    assert report["checks"][0]["status"] == status
    assert report["checks"][0]["data"]["listening"] is answer
    if answer is False:
        assert "reinstall" in report["checks"][0]["hint"]
        assert "not running" not in report["checks"][0]["message"]


@pytest.mark.parametrize("command", [
    'cua-driver serve --socket "/tmp/socket dir/a.sock"',
    'cua-driver serve --socket=/tmp/a.sock',
    '-%h/.cua-driver/packages/current/cua-driver serve',
])
def test_safe_quoted_and_default_socket_commands(tmp_path, monkeypatch, command):
    _unit(tmp_path, "[Service]\nExecStart=" + command + "\n")
    report, probe = _guard(tmp_path, monkeypatch, True)
    assert probe.call_count == 1
    assert report["checks"][0]["status"] == "pass"


@pytest.mark.parametrize("text,desktop", [
    ("[Service]\nExecStart=cua-driver call serve\n", False),
    ("[Service]\nExecStart=cua-driver mcp --label serve\n", False),
    ("[Service]\nExecStart=not-cua-driver serve\n", False),
    ("[Unit]\nExecStart=cua-driver serve\n", False),
    ("[Service]\nExecStart=cua-driver serve\nExecStart=\n", False),
    ("[Desktop Entry]\nHidden=true\nExec=cua-driver serve\n", True),
    ("[Desktop Entry]\nX-GNOME-Autostart-enabled=false\nExec=cua-driver serve\n", True),
])
def test_non_daemon_and_disabled_entries_never_probe(tmp_path, monkeypatch, text, desktop):
    _unit(tmp_path, text, desktop)
    report, probe = _guard(tmp_path, monkeypatch, False)
    probe.assert_not_called()
    assert report["overall"] == "ok"
    assert not report["checks"]


@pytest.mark.parametrize("command", [
    'cua-driver serve --socket "unterminated',
    'cua-driver serve --socket',
    'cua-driver serve --socket=',
    'cua-driver serve --socket=/tmp/a --socket=/tmp/b',
    'cua-driver serve --socket=$RUNTIME_DIR/driver.sock',
    'cua-driver serve --socket=%t/driver.sock',
    'cua-driver serve --socket=/tmp/a; echo secret',
])
def test_unsupported_parser_cases_are_unknown_without_probe(tmp_path, monkeypatch, command):
    _unit(tmp_path, "[Service]\nExecStart=" + command + "\n")
    report, probe = _guard(tmp_path, monkeypatch, False)
    probe.assert_not_called()
    assert report["overall"] == "ok"
    assert report["checks"][0]["status"] == "skip"
    assert report["checks"][0]["data"]["listening"] is None
    assert "secret" not in str(report)


def test_unconfigured_daemon_never_probes_default_socket(tmp_path, monkeypatch):
    report, probe = _guard(tmp_path, monkeypatch, False)
    probe.assert_not_called()
    assert report["checks"] == []


@pytest.mark.parametrize("returncode,stdout,answer", [(0, "ready", True), (1, "Daemon not running", False), (1, "permission denied", None)])
def test_status_probe_is_argv_only_sanitized_and_tristate(returncode, stdout, answer, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setenv("CUA_DRIVER_RS_TELEMETRY_ENABLED", "1")
    with patch.object(cua_backend_driver.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode, stdout, "")) as run:
        assert cua_backend_driver.cua_daemon_listening("/fixture/cua-driver", "/tmp/socket dir/a.sock") is answer
    args, kwargs = run.call_args
    assert args[0] == ["/fixture/cua-driver", "status", "--socket", "/tmp/socket dir/a.sock"]
    assert not kwargs.get("shell", False)
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert kwargs["env"]["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"


def test_status_probe_spawn_failure_is_unknown():
    with patch.object(cua_backend_driver.subprocess, "run", side_effect=OSError("unavailable")):
        assert cua_backend_driver.cua_daemon_listening("/fixture/cua-driver") is None
