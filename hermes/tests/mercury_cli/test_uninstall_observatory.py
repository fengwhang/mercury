"""Uninstall must remove the custom chat surface: mlounge unit + strays.

The Lounge fork install (unit file, prefix tree, npm cache, mlounge home)
lives partly outside $MERCURY_HOME, so the home rmtree cannot reach it.
Full uninstall must stop/disable/delete mercury-mlounge.service and kill
stray thelounge processes — otherwise `mercury-nightly uninstall`
leaves a zombie chat server behind.
"""

from __future__ import annotations

import pytest


def test_stop_and_remove_units_removes_mlounge_unit(tmp_path, monkeypatch) -> None:
    from observatory import provision as provision_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "mercury-observatory.service").write_text("[Unit]\n")
    (unit_dir / "mercury-lounge.service").write_text("[Unit]\n")
    monkeypatch.setattr(provision_mod, "_systemctl_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        provision_mod, "_run_systemctl",
        lambda args, **kw: calls.append(list(args)))
    removed = provision_mod._stop_and_remove_units()
    assert "mercury-observatory.service" in removed
    assert "mercury-lounge.service" in removed
    assert not (unit_dir / "mercury-lounge.service").exists()
    stops = [c for c in calls if c[0] == "stop"]
    assert ["stop", "mercury-lounge.service"] in stops


def test_kill_stray_mlounge_kills_thelounge(tmp_path, monkeypatch) -> None:
    import os as _os
    import types as _types
    from observatory import provision as provision_mod

    monkeypatch.setattr(
        provision_mod.subprocess, "run",
        lambda *a, **k: _types.SimpleNamespace(
            returncode=0, stdout="4242\n9999\n", stderr=""))
    killed: list[int] = []
    monkeypatch.setattr(
        _os, "kill", lambda pid, sig: killed.append(pid) or None)
    out = provision_mod._kill_stray_mlounge()
    assert out == [4242, 9999]
    assert killed == [4242, 9999]


def test_observatory_data_present_sees_mlounge_only(tmp_path, monkeypatch) -> None:
    from observatory import provision as provision_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    (home / "observatory" / "lounge").mkdir(parents=True)
    assert provision_mod.observatory_data_present(home) is True


def test_remove_units_calls_stray_mlounge_killer(monkeypatch) -> None:
    from mercury_cli import uninstall as uninstall_mod

    calls: list[str] = []
    monkeypatch.setattr(
        "observatory.provision._stop_and_remove_units",
        lambda: calls.append("units") or [])
    monkeypatch.setattr(
        "observatory.provision._kill_stray_tuwunel",
        lambda: calls.append("tuwunel") or [])
    monkeypatch.setattr(
        "observatory.provision._kill_stray_mlounge",
        lambda: calls.append("mlounge") or [])
    uninstall_mod._remove_observatory_units_only()
    assert "mlounge" in calls
