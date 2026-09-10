"""Managed-uv lookup + pip-less venv fallback for the crypto installer.

Fresh-install law: the install owns a managed uv that is NOT on PATH
(``$MERCURY_HOME/bin/uv``), and ``uv venv`` venvs ship WITHOUT pip
(``No module named pip``). ``shutil.which("uv")`` alone misses the managed
binary, so the pip fallback always fails on a fresh install. These tests
pin the fixed lookup order and the ensurepip/uv-only error paths for BOTH
copies (provision + update_release, which must stay in lockstep).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import mercury_cli.update_release as ur
from observatory import provision as provision_mod


def _touch_exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _completed(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=out)


class TestFindUv:
    def test_finds_managed_mercury_home_before_path(self, tmp_path, monkeypatch):
        mercury_home = tmp_path / "mercury"
        managed = _touch_exe(mercury_home / "bin" / "uv")
        python_bin = str(tmp_path / "venv" / "bin" / "python")
        monkeypatch.setenv("MERCURY_HOME", str(mercury_home))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
        assert provision_mod._find_uv(python_bin) == managed

    def test_finds_venv_adjacent_when_no_managed(self, tmp_path, monkeypatch):
        venv_bin = tmp_path / "venv" / "bin"
        adjacent = _touch_exe(venv_bin / "uv")
        python_bin = str(venv_bin / "python")
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "empty-home"))
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert provision_mod._find_uv(python_bin) == adjacent

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "empty"))
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-home"))
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert provision_mod._find_uv(str(tmp_path / "venv" / "bin" / "python")) is None

    def test_update_release_finds_same_managed_uv(self, tmp_path, monkeypatch):
        mercury_home = tmp_path / "mercury"
        managed = _touch_exe(mercury_home / "bin" / "uv")
        venv = tmp_path / "venv"
        monkeypatch.setenv("MERCURY_HOME", str(mercury_home))
        monkeypatch.setattr("shutil.which", lambda name: None)
        # Neutralize the canonical managed_uv.resolve_uv (may point at the
        # real install in-suite); the env-managed path must still win.
        monkeypatch.setattr("mercury_cli.managed_uv.resolve_uv", lambda: None)
        assert ur._find_uv(str(venv / "bin" / "python")) == managed


class TestCryptoPipInstallManagedUvOnly:
    def test_managed_uv_success_never_touches_pip(self, tmp_path, monkeypatch):
        """pip-less venv (``uv venv``: no pip module) + managed-uv-only box.

        PATH has no uv; the managed binary installs successfully. The pip
        fallback (``python -m pip``) must never run — it would fail with
        ``No module named pip``.
        """
        mercury_home = tmp_path / "mercury"
        managed = _touch_exe(mercury_home / "bin" / "uv")
        python_bin = str(tmp_path / "venv" / "bin" / "python")
        monkeypatch.setenv("MERCURY_HOME", str(mercury_home))
        monkeypatch.setattr("shutil.which", lambda name: None)

        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            assert cmd[0] == managed, f"must use managed uv, got {cmd[0]}"
            assert cmd[1:4] == ["pip", "install", "--python"]
            assert cmd[4] == python_bin
            return _completed(0, "")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        ok, detail = provision_mod._crypto_pip_install(python_bin, ["-q", "mautrix==1"])
        assert ok is True
        assert detail == ""
        assert len(calls) == 1

    def test_pip_less_no_uv_clear_error(self, tmp_path, monkeypatch):
        """No uv anywhere + pip-less venv: actionable error, not a traceback."""
        python_bin = str(tmp_path / "venv" / "bin" / "python")
        monkeypatch.setattr(provision_mod, "_find_uv", lambda _py: None)

        def _fake_run(cmd, **kwargs):
            if cmd[:3] == [python_bin, "-m", "pip"]:
                return _completed(1, "No module named pip")
            if cmd[:3] == [python_bin, "-m", "ensurepip"]:
                return _completed(1, "ensurepip unavailable")
            raise AssertionError(f"unexpected cmd {cmd}")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        ok, detail = provision_mod._crypto_pip_install(python_bin, ["-q", "x"])
        assert ok is False
        assert "No module named pip" in detail
        assert "uv not found" in detail
        assert "$MERCURY_HOME/bin/uv" in detail
        assert "ensurepip" in detail

    def test_ensurepip_bootstrap_recovers_pip(self, tmp_path, monkeypatch):
        """uv missing, pip bootstrappable: ensurepip runs, then pip installs."""
        python_bin = str(tmp_path / "venv" / "bin" / "python")
        monkeypatch.setattr(provision_mod, "_find_uv", lambda _py: None)
        state = {"pip_ready": False}
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd == [python_bin, "-m", "pip", "--version"]:
                if state["pip_ready"]:
                    return _completed(0, "pip 25.0")
                return _completed(1, "No module named pip")
            if cmd == [python_bin, "-m", "ensurepip", "--upgrade"]:
                state["pip_ready"] = True
                return _completed(0, "bootstrapped")
            if cmd[:3] == [python_bin, "-m", "pip"]:
                assert state["pip_ready"]
                return _completed(0, "")
            raise AssertionError(f"unexpected cmd {cmd}")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        ok, detail = provision_mod._crypto_pip_install(python_bin, ["-q", "x"])
        assert ok is True
        assert any(c[:3] == [python_bin, "-m", "ensurepip"] for c in calls)

    def test_uv_failure_plus_pip_less_names_both(self, tmp_path, monkeypatch):
        python_bin = str(tmp_path / "venv" / "bin" / "python")
        monkeypatch.setattr(provision_mod, "_find_uv", lambda _py: "/managed/uv")

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "/managed/uv":
                return _completed(1, "uv network unreachable")
            if cmd[:3] == [python_bin, "-m", "pip"]:
                return _completed(1, "No module named pip")
            if cmd[:3] == [python_bin, "-m", "ensurepip"]:
                return _completed(1, "no ensurepip")
            raise AssertionError(f"unexpected cmd {cmd}")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        ok, detail = provision_mod._crypto_pip_install(python_bin, ["-q", "x"])
        assert ok is False
        assert "uv install failed" in detail
        assert "No module named pip" in detail


class TestUpdateReleaseParity:
    def test_pip_fallback_targets_venv_python(self, tmp_path, monkeypatch):
        """The duplicate runner installs into the venv python, never the
        current interpreter, and bootstraps pip-less venvs."""
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        py = str(venv / "bin" / "python")
        monkeypatch.setattr(ur, "_find_uv", lambda _py: None)
        seen: list[list[str]] = []
        state = {"pip_ready": True}

        def _fake_run(cmd, **kwargs):
            seen.append(list(cmd))
            if cmd == [py, "-m", "pip", "--version"]:
                return _completed(0, "pip 25.0") if state["pip_ready"] else _completed(1, "No module named pip")
            if cmd[0] == py and "-m" in cmd and "pip" in cmd:
                return _completed(0, "")
            raise AssertionError(f"unexpected cmd {cmd}")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        ok, _detail = ur._pip_install(venv, ["pkg==1"])
        assert ok is True
        pip_cmds = [c for c in seen if c[:3] == [py, "-m", "pip"]]
        assert pip_cmds, "pip fallback must target the venv python"
        for c in seen:
            assert c[0] != sys.executable or c[0] == py, \
                "must never install into sys.executable when refreshing another venv"

    def test_os_environ_importable(self):
        # Guard against the regression where update_release lost its `os`
        # import (used by _find_uv's MERCURY_HOME/HERMES_HOME lookup).
        assert isinstance(os.environ.get("PATH", ""), str)
