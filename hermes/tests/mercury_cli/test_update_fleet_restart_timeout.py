"""One failed idle admission must not abandon the remaining gateway fleet."""
from types import SimpleNamespace

import mercury_cli.gateway as gateway
from mercury_cli import update_cmd as update


def test_admission_failure_on_middle_gateway_continues_remaining_profiles(monkeypatch, tmp_path):
    processes = [SimpleNamespace(pid=pid, profile=name, path=tmp_path / name)
                 for pid, name in [(1, "before"), (2, "failed"), (3, "after")]]
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: processes)
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [1, 2, 3])
    requested = []

    def admit(**kwargs):
        requested.append(kwargs["pid"])
        if kwargs["pid"] == 2:
            raise OSError("control unavailable")
        return {"restarting": True, "deferred": False, "pid": kwargs["pid"]}

    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admit)
    monkeypatch.setattr(update, "_wait_for_automatic_gateway_replacement", lambda *_: True)
    result = update._restart_gateway_fleet_automatically(trigger="update")
    assert result["verified"] == ["before", "after"]
    assert result["failed"] == ["failed"]
    assert requested == [1, 2, 3]


def test_warns_with_exact_unrestarted_units(capsys):
    update._warn_incomplete_gateway_fleet_restart(["mercury-gateway-one", "mercury-gateway-two"])
    out = capsys.readouterr().out
    assert "mercury-gateway-one" in out
    assert "mercury-gateway-two" in out
    assert "pre-update code" in out
