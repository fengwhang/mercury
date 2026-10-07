"""Manual gateways use live idle admission, never an updater-owned kill sweep."""
from types import SimpleNamespace
from unittest.mock import Mock

import mercury_cli.gateway as gateway
from mercury_cli import update_cmd as update


def test_manual_gateway_restart_does_not_arm_competing_detached_watcher(monkeypatch, tmp_path):
    process = SimpleNamespace(profile="fitness", pid=4242, path=tmp_path)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [process])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    admission = Mock(return_value={"restarting": True, "deferred": False, "pid": 4242})
    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admission)
    monkeypatch.setattr(update, "_wait_for_automatic_gateway_replacement", lambda *_: True)
    watcher = Mock(side_effect=AssertionError("gateway owns its relaunch"))
    monkeypatch.setattr(gateway, "launch_detached_profile_gateway_restart", watcher)
    result = update._restart_gateway_fleet_automatically(trigger="update")
    assert result["verified"] == ["fitness"]
    admission.assert_called_once_with(home=tmp_path, pid=4242, trigger="update")
    watcher.assert_not_called()


def test_unmapped_gateway_remains_running_and_is_reported_not_killed(monkeypatch):
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    kill = Mock(side_effect=AssertionError("unmapped PID must not be killed"))
    monkeypatch.setattr(gateway, "kill_gateway_processes", kill)
    result = update._restart_gateway_fleet_automatically(trigger="update")
    assert result["failed"] == ["unmapped PID 4242"]
    assert result["verified"] == []
    kill.assert_not_called()
