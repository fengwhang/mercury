"""Observatory commands without rerunning the setup wizard."""

from __future__ import annotations

import argparse
import json
import types

import pytest

from mercury_cli.subcommands import observatory as obs_mod


@pytest.fixture(autouse=True)
def isolated_restart_cleanup(monkeypatch):
    # These tests isolate service ordering/verification; the actual durable
    # cleanup and CLI seam run against temporary databases in Observatory tests.
    monkeypatch.setattr("observatory.restart.prepare_room_cleanup",
                        lambda home=None: {"expired_agents": 0})


def _login_args(*extra):
    parser = argparse.ArgumentParser()
    obs_mod.build_observatory_parser(parser.add_subparsers(dest="cmd"))
    return parser.parse_args(["observatory", "login", *extra])


@pytest.mark.parametrize("command, override", [
    ("mercury", False), ("mercury-nightly", False), ("mercury", True),
])
def test_login_reprints_setup_card_from_selected_home(
    monkeypatch, tmp_path, capsys, command, override,
) -> None:
    from observatory import mlounge, provision
    from mercury_cli import setup

    active = tmp_path / (".mercury-nightly" if command.endswith("nightly") else ".mercury")
    home = tmp_path / "custom" if override else active
    monkeypatch.setenv("MERCURY_HOME", str(active))
    monkeypatch.setenv("MERCURY_CMD", command)
    config = home / "observatory" / "ircd.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({
        "server_name": "testnet", "server_host": "100.101.102.103",
        "server_port": 6671, "tls_port": 6698,
    }))
    paths = mlounge.MLoungePaths(home)
    users = paths.home / "users"
    users.mkdir(parents=True)
    paths.conf.write_text('module.exports = {host: "100.101.102.103", port: 9001};')
    (users / "tester.json").write_text('{"password": "synthetic-hash"}')
    (home / ".env").write_text('IRC_CLIENT_PASSWORD="synthetic-secret"\n')
    tailscale = {
        "available": True, "up": True, "ip": "100.101.102.103",
        "dns_name": "test-host.tailnet.ts.net",
    }
    monkeypatch.setattr(provision, "detect_tailscale", lambda: tailscale)
    monkeypatch.setattr(mlounge, "mlounge_unit_active", lambda: False)
    monkeypatch.setattr(mlounge, "mlounge_bin", lambda: tmp_path / "thelounge")

    # Compare against the real setup renderer using the same disk state.
    setup._print_observatory_setup_card(
        provision.status_summary(home), tailscale, mercury_home=home,
    )
    setup_card = capsys.readouterr().out
    before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    args = _login_args(*(["--home", str(home)] if override else []))
    assert args.func(args) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert setup_card in captured.out
    assert "http://test-host.tailnet.ts.net:9001" in captured.out
    assert "this box from another mLounge" in captured.out
    assert "MIRC host:            test-host.tailnet.ts.net" in captured.out
    assert "MIRC port:            6671" in captured.out
    assert str(home / ".env") in captured.out
    assert "100.101.102.103" not in captured.out
    assert "or connect any IRC client" not in captured.out
    assert "user 'tester'" in captured.out
    assert "#testnet_gateway" in captured.out
    assert "test-host.tailnet.ts.net" in captured.out
    assert f"{command} setup gateway" in captured.out
    assert "synthetic-secret" not in captured.out
    assert "synthetic-hash" not in captured.out
    assert before == {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}


@pytest.mark.parametrize("command", ["mercury", "mercury-nightly"])
def test_login_unprovisioned_prints_setup_hint(monkeypatch, tmp_path, capsys, command) -> None:
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("MERCURY_CMD", command)
    assert obs_mod.cmd_observatory(_login_args()) == 1
    captured = capsys.readouterr()
    assert f"{command} setup observatory" in captured.err
    assert not captured.out
    assert not (tmp_path / "empty").exists()


def test_login_without_mlounge_or_tailscale(monkeypatch, tmp_path, capsys) -> None:
    from observatory import mlounge, provision

    home = tmp_path / "mercury"
    config = home / "observatory" / "ircd.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"server_name": "localnet"}')
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setattr(provision, "detect_tailscale", lambda: {})
    monkeypatch.setattr(mlounge, "mlounge_unit_active", lambda: False)
    monkeypatch.setattr(mlounge, "mlounge_bin", lambda: tmp_path / "thelounge")
    monkeypatch.setattr(mlounge, "_local_port_answers", lambda *a: False)
    assert obs_mod.cmd_observatory(_login_args()) == 0
    out = capsys.readouterr().out
    assert "not installed" in out
    assert "Tailscale not detected" in out
    assert "server port:" not in out
    assert "#localnet_gateway" in out


def test_login_reports_status_failure(monkeypatch, capsys) -> None:
    def fail(*args):
        raise OSError("cannot read status")

    monkeypatch.setattr("observatory.provision.read_config", lambda *args: {})
    monkeypatch.setattr("observatory.provision.status_summary", fail)
    assert obs_mod.cmd_observatory(_login_args()) == 1
    assert "observatory login unavailable" in capsys.readouterr().err


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
        "observatory.mlounge.status_mlounge",
        lambda *a, **kw: {"configured": True},
    )
    monkeypatch.setattr(
        "observatory.mlounge.refresh_mlounge_fork",
        lambda *a, **kw: calls.append("mlounge") or "current",
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

        def names(self, channel, timeout=5.0):
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
        "observatory.provision.read_mirc_passwords",
        lambda home=None: {"server": "pw"},
    )
    rc = obs_mod.cmd_observatory(_args())
    assert rc == (0 if duplex and not resync_failures else 1)
    assert calls == ["unit", "mlounge", "gateway"]  # unit re-render BEFORE anything else
    captured = capsys.readouterr()
    out = captured.out
    assert "daemon: restarted onto current code" in out
    assert "mLounge: current" in out
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
        "observatory.mlounge.status_mlounge", lambda *a, **kw: {"configured": False}
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


def test_restart_skips_mlounge_when_unmanaged(monkeypatch, capsys) -> None:
    """No managed mLounge (user declined it): restart must NOT install one."""
    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit",
        lambda *a, **kw: "installed",
    )
    monkeypatch.setattr(
        "observatory.mlounge.status_mlounge",
        lambda *a, **kw: {"configured": False},
    )

    def _boom(*a, **kw):
        raise AssertionError("refresh must not run without a managed install")

    monkeypatch.setattr("observatory.mlounge.refresh_mlounge_fork", _boom)
    monkeypatch.setattr(obs_mod, "_restart_gateway_now", lambda: 0)
    monkeypatch.setattr(
        "mercury_cli.setup._verify_gateway_bot",
        lambda **kw: (True, "nick present"),
    )
    monkeypatch.setattr(obs_mod, "_verify_fleet", lambda args: 0)
    assert obs_mod.cmd_observatory(_args()) == 0
    assert "mLounge: not installed, skipping" in capsys.readouterr().out
