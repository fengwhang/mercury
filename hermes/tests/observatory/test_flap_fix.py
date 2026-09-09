"""Flap + crash-loop regression tests (field bug 2026-09-09).

Live-VM findings (ground truth): a stuck setup wizard called provision()
~1/sec and every call unconditionally ``systemctl restart``-ed BOTH
observatory units even when the unit files were unchanged; the sidecar
additionally crashed at import (aiohttp guard dead code below the
appservice import); the crypto path never ensured aiohttp; the installed
unit had drifted to ``sidecar.log`` vs the template's ``sidecar.log``;
and ``Requires=`` tied the sidecar's lifetime to the homeserver.

Laws pinned here:

- unchanged unit file ⇒ NO restart (active: zero systemctl calls;
  inactive: ``start``, never ``restart``); only a content change
  reloads + restarts; a fresh install ``start``s;
- ``import observatory.sidecar_main`` without aiohttp ⇒ SystemExit,
  never ModuleNotFoundError/ImportError;
- ``ensure_crypto_stack`` treats olm-ready-but-no-aiohttp as missing
  (installs, passing an aiohttp pin to pip) — never ``"ready"``;
- template and renderer agree on the ``sidecar.log`` filename;
- the sidecar unit orders after the homeserver via ``Wants=`` + ``After=``
  (no ``Requires=`` — a homeserver bounce must not SIGTERM the sidecar).
"""
from __future__ import annotations

import builtins
import importlib
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import observatory.e2ee as e2ee_mod
from observatory import provision as provision_mod
from observatory.config_gen import (
    HOMESERVER_UNIT_NAME,
    SIDECAR_UNIT_NAME,
    ObservatoryPaths,
)

_LOG_BASENAME = "sidecar.log"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _redirect_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Point Path.home() at tmp (unit files land under it, never ~/.config)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class _Systemctl:
    """Records _run_systemctl calls; always succeeds."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs) -> SimpleNamespace:
        self.calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


def _wire_systemd(monkeypatch: pytest.MonkeyPatch, *, active: bool) -> _Systemctl:
    sysctl = _Systemctl()
    monkeypatch.setattr(provision_mod, "_systemctl_available", lambda: True)
    monkeypatch.setattr(provision_mod, "_run_systemctl", sysctl)
    monkeypatch.setattr(provision_mod, "_unit_is_active", lambda unit: active)
    return sysctl


def _homeserver_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[ObservatoryPaths, Path, str]:
    home = _redirect_home(monkeypatch, tmp_path / "home")
    paths = ObservatoryPaths(tmp_path / "mercury")
    unit = provision_mod.config_gen.render_homeserver_unit(
        exec_path=str(paths.binary),
        config_path=str(paths.toml),
        log_dir=str(paths.logs_dir),
    )
    unit_path = home / ".config" / "systemd" / "user" / HOMESERVER_UNIT_NAME
    return paths, unit_path, unit


def _sidecar_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, str]:
    home = _redirect_home(monkeypatch, tmp_path / "home")
    mercury_home = tmp_path / "mercury"
    from observatory.sidecar_main import render_sidecar_unit

    # Same args ensure_sidecar_unit() itself uses (sys.executable +
    # hermes root derived from provision.__file__), or the "identical"
    # file would never compare equal.
    unit = render_sidecar_unit(
        python_bin=sys.executable,
        hermes_root=str(Path(provision_mod.__file__).resolve().parent.parent),
        mercury_home=str(mercury_home),
        log_dir=str(mercury_home / "observatory" / "logs"),
    )
    unit_path = home / ".config" / "systemd" / "user" / SIDECAR_UNIT_NAME
    return mercury_home, unit_path, unit


# ---------------------------------------------------------------------------
# (a) unchanged unit ⇒ no restart
# ---------------------------------------------------------------------------


class TestUnchangedUnitNoRestart:
    def test_homeserver_unchanged_active_zero_systemctl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        paths, unit_path, unit = _homeserver_setup(monkeypatch, tmp_path)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit, encoding="utf-8")
        sysctl = _wire_systemd(monkeypatch, active=True)

        assert provision_mod.ensure_systemd_unit(paths) == "started"
        assert sysctl.calls == []

    def test_homeserver_unchanged_inactive_starts_never_restarts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        paths, unit_path, unit = _homeserver_setup(monkeypatch, tmp_path)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit, encoding="utf-8")
        sysctl = _wire_systemd(monkeypatch, active=False)

        assert provision_mod.ensure_systemd_unit(paths) == "started"
        assert sysctl.calls == [["start", HOMESERVER_UNIT_NAME]]

    def test_homeserver_changed_restarts_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        paths, unit_path, unit = _homeserver_setup(monkeypatch, tmp_path)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text("stale unit", encoding="utf-8")
        sysctl = _wire_systemd(monkeypatch, active=True)

        assert provision_mod.ensure_systemd_unit(paths) == "refreshed"
        assert sysctl.verbs().count("restart") == 1
        assert ["restart", HOMESERVER_UNIT_NAME] in sysctl.calls
        assert unit_path.read_text(encoding="utf-8") == unit

    def test_homeserver_fresh_install_starts_not_restarts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        paths, unit_path, _unit = _homeserver_setup(monkeypatch, tmp_path)
        sysctl = _wire_systemd(monkeypatch, active=False)

        assert provision_mod.ensure_systemd_unit(paths) == "installed"
        assert sysctl.calls == [
            ["daemon-reload"],
            ["enable", HOMESERVER_UNIT_NAME],
            ["start", HOMESERVER_UNIT_NAME],
        ]

    def test_sidecar_unchanged_active_zero_systemctl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mercury_home, unit_path, unit = _sidecar_setup(monkeypatch, tmp_path)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit, encoding="utf-8")
        sysctl = _wire_systemd(monkeypatch, active=True)

        assert provision_mod.ensure_sidecar_unit(mercury_home) == "started"
        assert sysctl.calls == []

    def test_sidecar_unchanged_inactive_starts_never_restarts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mercury_home, unit_path, unit = _sidecar_setup(monkeypatch, tmp_path)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit, encoding="utf-8")
        sysctl = _wire_systemd(monkeypatch, active=False)

        assert provision_mod.ensure_sidecar_unit(mercury_home) == "started"
        assert sysctl.calls == [["start", SIDECAR_UNIT_NAME]]

    def test_sidecar_changed_restarts_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        mercury_home, unit_path, unit = _sidecar_setup(monkeypatch, tmp_path)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        # stale installed unit (e.g. the field's sidecar.log drift): one
        # deliberate restart heals it, then later calls are no-ops.
        unit_path.write_text("stale unit", encoding="utf-8")
        sysctl = _wire_systemd(monkeypatch, active=True)

        assert provision_mod.ensure_sidecar_unit(mercury_home) == "refreshed"
        assert ["restart", SIDECAR_UNIT_NAME] in sysctl.calls
        assert unit_path.read_text(encoding="utf-8") == unit


# ---------------------------------------------------------------------------
# (b) aiohttp guard fires before any aiohttp-dependent import
# ---------------------------------------------------------------------------


class TestAiohttpGuardOrder:
    def test_missing_aiohttp_is_systemexit(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Without aiohttp the sidecar must exit handled (SystemExit), not
        crash with ModuleNotFoundError from the appservice import."""
        monkeypatch.setitem(sys.modules, "aiohttp", None)
        monkeypatch.setitem(sys.modules, "aiohttp.web", None)
        for name in (
            "observatory.sidecar_main",
            "observatory.appservice",
            "observatory.matrix_client",
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
        with pytest.raises(SystemExit, match="aiohttp"):
            importlib.import_module("observatory.sidecar_main")

    def test_guard_precedes_appservice_import(self):
        """Static backstop: the aiohttp guard must sit above every
        observatory import in sidecar_main (a later reordering that puts
        appservice first resurrects the ModuleNotFoundError crash)."""
        src = Path(provision_mod.__file__).with_name("sidecar_main.py").read_text(
            encoding="utf-8"
        )
        guard = src.index("from aiohttp import web")
        first_obs = min(
            src.index("from observatory import e2ee"),
            src.index("from observatory.appservice import"),
            src.index("from observatory.matrix_client import"),
        )
        assert guard < first_obs


# ---------------------------------------------------------------------------
# (c) crypto stack ensures aiohttp
# ---------------------------------------------------------------------------


class TestCryptoEnsuresAiohttp:
    def test_companions_pin_aiohttp(self):
        assert "aiohttp==3.14.3" in provision_mod._CRYPTO_PY_DEPS

    def test_olm_ready_without_aiohttp_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Olm imports but aiohttp missing ⇒ install path (not 'ready'),
        and the install args carry the aiohttp pin."""
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)
        monkeypatch.setattr(provision_mod, "_aiohttp_available", lambda: False)
        calls: list = []

        def _fake_install(python_bin: str, args: list[str]):
            calls.append((python_bin, list(args)))
            return True, ""

        monkeypatch.setattr(provision_mod, "_crypto_pip_install", _fake_install)
        real_import = builtins.__import__

        def _fake_import(name: str, *a, **k):
            if name in ("olm", "aiohttp"):
                return types.ModuleType(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        assert provision_mod.ensure_crypto_stack() == "installed"
        assert len(calls) == 1
        assert "aiohttp==3.14.3" in calls[0][1]


# ---------------------------------------------------------------------------
# (d) log-filename agreement
# ---------------------------------------------------------------------------


def _appended_log_basenames(unit: str) -> set[str]:
    return set(re.findall(r"append:\S+/(\S+\.log)", unit))


class TestLogFilenameAgreement:
    def test_template_and_renderer_agree(self, tmp_path: Path):
        from observatory.sidecar_main import render_sidecar_unit

        template = (
            Path(provision_mod.__file__).parent
            / "templates"
            / "mercury-observatory.service"
        ).read_text(encoding="utf-8")
        rendered = render_sidecar_unit(
            python_bin="/usr/bin/python3",
            hermes_root="/opt/hermes",
            mercury_home="/home/u/.mercury",
            log_dir="/home/u/.mercury/observatory/logs",
        )
        # stdout + stderr append to the SAME canonical filename in both
        # the checked-in template and the renderer output — a typo in
        # either (the field's stale-unit drift) fails here.
        assert _appended_log_basenames(template) == {_LOG_BASENAME}
        assert _appended_log_basenames(rendered) == {_LOG_BASENAME}
        assert rendered.count(f"/{_LOG_BASENAME}") == 2
        assert f"append:/home/u/.mercury/observatory/logs/{_LOG_BASENAME}" in rendered


# ---------------------------------------------------------------------------
# (e) unit dependency shape: Wants + After, never Requires
# ---------------------------------------------------------------------------


class TestUnitDependencyShape:
    def test_sidecar_wants_not_requires(self):
        from observatory.sidecar_main import render_sidecar_unit

        template = (
            Path(provision_mod.__file__).parent
            / "templates"
            / "mercury-observatory.service"
        ).read_text(encoding="utf-8")
        rendered = render_sidecar_unit(
            python_bin="/usr/bin/python3",
            hermes_root="/opt/hermes",
            mercury_home="/home/u/.mercury",
            log_dir="/home/u/.mercury/observatory/logs",
        )
        for unit in (template, rendered):
            assert "Wants=mercury-observatory-homeserver.service" in unit
            assert "After=mercury-observatory-homeserver.service" in unit
            assert "Requires=" not in unit
