"""Automatic update admission must not recreate stopped or unsupported gateways."""
from types import SimpleNamespace
from unittest.mock import Mock

import mercury_cli.gateway as gateway
from mercury_cli import update_cmd as update


def test_no_running_gateway_requires_no_automatic_service_restart(monkeypatch):
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [])
    restart = Mock(side_effect=AssertionError("no gateway should be forced up"))
    monkeypatch.setattr(gateway, "launchd_restart", restart)
    result = update._restart_gateway_fleet_automatically(trigger="update")
    assert result["verified"] == []
    assert result["failed"] == []
    restart.assert_not_called()


def test_unsupported_live_gateway_has_no_launchd_kickstart_fallback(monkeypatch, tmp_path):
    proc = SimpleNamespace(pid=4242, profile="default", path=tmp_path)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [proc])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", lambda *a, **k: None)
    restart = Mock(side_effect=AssertionError("unsupported admission must fail closed"))
    monkeypatch.setattr(gateway, "launchd_restart", restart)
    result = update._restart_gateway_fleet_automatically(trigger="update")
    assert result["failed"] == ["default"]
    assert result["requested"] == []
    restart.assert_not_called()
