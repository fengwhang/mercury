"""``mercury observatory restart`` — the setup-free freshen path."""

from __future__ import annotations

import argparse
import types

import pytest

from mercury_cli.subcommands import observatory as obs_mod


def _args(**kw) -> types.SimpleNamespace:
    return types.SimpleNamespace(observatory_action="restart", **kw)


def test_restart_wired_in_parser() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    obs_mod.build_observatory_parser(sub)
    args = parser.parse_args(["observatory", "restart"])
    assert args.observatory_action == "restart"
    assert args.func is obs_mod.cmd_observatory


@pytest.mark.parametrize("duplex, resync_failures", [(True, []), (False, []), (True, ["omp startup failed"])])
def test_restart_rerenders_unit_then_gateway_then_verifies(monkeypatch, capsys, duplex, resync_failures) -> None:
    import json as _json
    import time as _time

    calls: list[str] = []
    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit",
        lambda *a, **kw: calls.append("unit") or "installed",
    )
    monkeypatch.setattr(
        "observatory.lounge.status_lounge",
        lambda *a, **kw: {"configured": True},
    )
    monkeypatch.setattr(
        "observatory.lounge.refresh_lounge_fork",
        lambda *a, **kw: calls.append("lounge") or "current",
    )
    monkeypatch.setattr(
        obs_mod, "_restart_gateway_now", lambda: calls.append("gateway") or 0)
    monkeypatch.setattr(
        "mercury_cli.setup._verify_gateway_bot",
        lambda **kw: (True, "nick present"),
    )

    class _FakeState:
        def get_live(self):
            return [{
                "node_id": "gw", "engine": "hermes", "status": "live",
                "name": "gateway agent", "session_ref": "session:gateway",
                "room_id": "#vm_gateway", "mxid": "vm_gateway",
            }]

        def get_meta(self, key):
            assert key == "last-resync"
            return _json.dumps({"epoch": _time.time(), "joined": ["#vm_gateway"],
                                "resumed": [], "failed": resync_failures})

    monkeypatch.setattr(obs_mod, "_open_state", lambda home: _FakeState())

    class _FakeProbe:
        def __init__(self, *args):
            pass

        def connect(self):
            return True

        def names(self, channel):
            assert channel == "#vm_gateway"
            return ["owner", "vm_gateway"]

        def gateway_roundtrip(self, nick):
            assert nick == "vm_gateway"
            return duplex

        def close(self):
            pass

    monkeypatch.setattr("observatory.doctor._Probe", _FakeProbe)
    monkeypatch.setattr(
        "observatory.provision.read_config",
        lambda home=None: {"server_host": "127.0.0.1", "server_port": 6670},
    )
    monkeypatch.setattr(
        "observatory.provision.read_irc_passwords",
        lambda home=None: {"server": "pw"},
    )
    rc = obs_mod.cmd_observatory(_args())
    assert rc == (0 if duplex and not resync_failures else 1)
    assert calls == ["unit", "lounge", "gateway"]  # unit re-render BEFORE anything else
    captured = capsys.readouterr()
    out = captured.out
    assert "daemon: restarted onto current code" in out
    assert "lounge: current" in out
    assert "bot: nick present" in out
    if duplex and not resync_failures:
        assert "fleet: all 1 live agent(s) present" in out
        assert "gateway transport verified" in out
        assert "provider replies not tested" in out
    else:
        assert "fleet: all 1 live agent(s) present" not in out
    if not duplex:
        assert "presence alone is not a working agent" in captured.err


def test_restart_fails_when_fleet_never_resyncs(monkeypatch, capsys) -> None:
    """A stale resync marker fails the restart loudly instead of
    declaring victory on the gateway room alone."""
    import json as _json

    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit", lambda *a, **kw: "installed"
    )
    monkeypatch.setattr(
        "observatory.lounge.status_lounge", lambda *a, **kw: {"configured": False}
    )
    monkeypatch.setattr(obs_mod, "_restart_gateway_now", lambda: 0)
    monkeypatch.setattr(
        "mercury_cli.setup._verify_gateway_bot", lambda **kw: (True, "nick present")
    )

    class _StaleState:
        def get_live(self):
            return [{
                "node_id": "gw", "engine": "hermes", "status": "live",
                "name": "gateway agent", "session_ref": "session:gateway",
                "room_id": "#vm_gateway", "mxid": "vm_gateway",
            }]

        def get_meta(self, key):
            return _json.dumps({"epoch": 1.0, "joined": [], "resumed": [],
                                "failed": []})

    monkeypatch.setattr(obs_mod, "_open_state", lambda home: _StaleState())
    monkeypatch.setattr("time.sleep", lambda s: None)
    rc = obs_mod.cmd_observatory(_args())
    assert rc == 1
    assert "no resync completed" in capsys.readouterr().err


def test_restart_stops_on_unit_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit",
        lambda *a, **kw: "installed (start failed: boom)",
    )

    def _boom() -> int:
        raise AssertionError("gateway must not restart after a daemon failure")

    monkeypatch.setattr(obs_mod, "_restart_gateway_now", _boom)
    rc = obs_mod.cmd_observatory(_args())
    assert rc == 1
    assert "FAILED" in capsys.readouterr().err


def test_restart_prints_full_diagnosis_when_bot_never_joins(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit",
        lambda *a, **kw: "installed",
    )
    monkeypatch.setattr(obs_mod, "_restart_gateway_now", lambda: 0)
    monkeypatch.setattr(
        "mercury_cli.setup._verify_gateway_bot",
        lambda **kw: (False, "nick NOT in #x — bot is down"),
    )
    monkeypatch.setattr(
        "observatory.doctor.run_doctor",
        lambda: [(True, "daemon code", "PID 1 runs 9.9.9"),
                 (False, "bot connection", "nothing connected")],
    )
    rc = obs_mod.cmd_observatory(_args())
    assert rc == 1
    captured = capsys.readouterr()
    text = captured.err + captured.out
    assert "nick NOT in #x" in text
    assert "[ok] daemon code" in text
    assert "[FAIL] bot connection" in text


def test_restart_skips_lounge_when_unmanaged(monkeypatch, capsys) -> None:
    """No managed Lounge (user declined it): restart must NOT install one."""
    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit",
        lambda *a, **kw: "installed",
    )
    monkeypatch.setattr(
        "observatory.lounge.status_lounge",
        lambda *a, **kw: {"configured": False},
    )

    def _boom(*a, **kw):
        raise AssertionError("refresh must not run without a managed install")

    monkeypatch.setattr("observatory.lounge.refresh_lounge_fork", _boom)
    monkeypatch.setattr(obs_mod, "_restart_gateway_now", lambda: 0)
    monkeypatch.setattr(
        "mercury_cli.setup._verify_gateway_bot",
        lambda **kw: (True, "nick present"),
    )
    monkeypatch.setattr(obs_mod, "_verify_fleet", lambda args: 0)
    assert obs_mod.cmd_observatory(_args()) == 0
    assert "lounge: not installed, skipping" in capsys.readouterr().out
