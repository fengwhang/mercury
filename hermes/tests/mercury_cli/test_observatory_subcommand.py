"""``mercury observatory restart`` — the setup-free freshen path."""

from __future__ import annotations

import argparse
import types

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


def test_restart_rerenders_unit_then_gateway_then_verifies(monkeypatch, capsys) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "observatory.provision.ensure_observatory_unit",
        lambda *a, **kw: calls.append("unit") or "installed",
    )
    monkeypatch.setattr(
        obs_mod, "_restart_gateway_now", lambda: calls.append("gateway") or 0)
    monkeypatch.setattr(
        "mercury_cli.setup._verify_gateway_bot",
        lambda **kw: (True, "nick present"),
    )
    rc = obs_mod.cmd_observatory(_args())
    assert rc == 0
    assert calls == ["unit", "gateway"]  # unit re-render BEFORE anything else
    out = capsys.readouterr().out
    assert "daemon: restarted onto current code" in out
    assert "bot: nick present" in out


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
