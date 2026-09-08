"""Wizard integration for the Matrix observatory: the 'Matrix Observatory
(bundled)' section in mercury_cli/setup.py + the provision.py helpers it
calls (status_summary / provision_in_wizard).

Laws under test:

- **section rendering per state** — fresh/unprovisioned, provisioned+
  enabled, provisioned+disabled each render their state lines and the
  skip/install outcomes;
- **no secret leaks** — the owner password never appears unless the
  explicit reveal prompt is answered yes (default no);
- **non-interactive path** — state summary + the exact
  ``python -m observatory.provision`` command (no ``mercury observatory``
  wrapper exists), following print_noninteractive_setup_guidance
  conventions;
- **config toggle** — ``observatory.enabled`` is written to config.yaml
  through the standard save_config helper (config, never env);
- **provision helper contract** — status_summary carries booleans/paths
  only (no secrets) and never raises; provision_in_wizard prints the CLI
  lines, returns the summary and raises instead of exiting; the CLI
  output bytes are unchanged.

No network, no systemd: status/provision seams are faked or pointed at a
throwaway home; the homeserver/unit probes are patched.
"""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

import mercury_cli.setup as setup_mod
from mercury_cli.config import get_config_path, load_config
from observatory import provision as provision_mod
from observatory.config_gen import (
    HOMESERVER_PORT_DEFAULT,
    HOMESERVER_UNIT_NAME,
)


PASSWORD = "s3cr3t-observatory-owner-password"
MXID = "@owner:mercury.local"
HOMESERVER_URL = f"http://127.0.0.1:{HOMESERVER_PORT_DEFAULT}"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _status(**over) -> dict:
    """A status_summary-shaped dict (default: fresh/unprovisioned)."""
    st = {
        "provisioned": False,
        "config_exists": False,
        "binary_installed": False,
        "owner_credentials_exist": False,
        "owner_credentials_path": "/nonexistent/owner-credentials.json",
        "homeserver_url": HOMESERVER_URL,
        "homeserver_reachable": False,
        "unit_active": False,
        "unit_name": HOMESERVER_UNIT_NAME,
        "enabled": True,
        "e2ee": False,
        "observatory_dir": "/nonexistent/observatory",
    }
    st.update(over)
    return st


def _write_credentials(home: Path, *, password: str = PASSWORD,
                       user_id: str = MXID) -> Path:
    obs = home / "observatory"
    obs.mkdir(parents=True, exist_ok=True)
    creds = obs / "owner-credentials.json"
    creds.write_text(
        json.dumps(
            {
                "homeserver_url": HOMESERVER_URL,
                "user_id": user_id,
                "password": password,
                "access_token": "",
                "device_id": "",
            }
        ),
        encoding="utf-8",
    )
    return creds


class _FakeProvision:
    """observatory.provision stand-in for the section (records calls)."""

    def __init__(self, statuses, *, provision_error=None):
        self._statuses = list(statuses)
        self._provision_error = provision_error
        self.calls = {"provision": 0, "status": 0}

    def status_summary(self, *a, **k):
        idx = min(self.calls["status"], len(self._statuses) - 1)
        self.calls["status"] += 1
        return dict(self._statuses[idx])

    def provision_in_wizard(self, *a, **k):
        self.calls["provision"] += 1
        if self._provision_error is not None:
            raise self._provision_error
        print("→ Matrix Observatory provisioning (Tuwunel)")
        print("  ✓ tuwunel: current v1.9.0 → /fake/bin/tuwunel")
        return {
            "tuwunel": {
                "action": "current",
                "version": "1.9.0",
                "binary": "/fake/bin/tuwunel",
            }
        }


def _run_section(monkeypatch, capsys, fake, *, choice, yes_no):
    """Run setup_observatory with fakes; returns (stdout, consumed_answers)."""
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    monkeypatch.setattr(
        setup_mod, "prompt_choice",
        lambda q, c, d=0, description=None: choice,
    )
    remaining = list(yes_no)

    def fake_yes_no(question, default=True):
        assert remaining, f"unexpected extra prompt_yes_no: {question!r}"
        return remaining.pop(0)

    monkeypatch.setattr(setup_mod, "prompt_yes_no", fake_yes_no)
    config = load_config()
    setup_mod.setup_observatory(config)
    return capsys.readouterr().out, config, remaining


# ---------------------------------------------------------------------------
# wizard section rendering per state
# ---------------------------------------------------------------------------


def test_section_fresh_unprovisioned_skip(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True]
    )

    assert "Matrix Observatory (bundled)" in out
    assert "Provisioned:          no" in out
    assert f"Homeserver reachable: no  ({HOMESERVER_URL})" in out
    assert f"Unit active:          no  ({HOMESERVER_UNIT_NAME})" in out
    assert "observatory.enabled:  yes  (config.yaml)" in out
    # Skip prints the --skip-observatory equivalent note
    assert "--skip-observatory" in out
    assert fake.calls["provision"] == 0
    # Fresh + skipped: no card, no secrets
    assert "first login" not in out
    assert PASSWORD not in out
    assert remaining == []  # only the toggle prompt ran


def test_section_install_calls_provision_then_card(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_status(), _status(
            provisioned=True,
            config_exists=True,
            binary_installed=True,
            owner_credentials_exist=True,
            owner_credentials_path=str(creds),
            homeserver_reachable=True,
            unit_active=True,
        )]
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0, yes_no=[True, False]
    )

    assert fake.calls["provision"] == 1
    # provision output is shown
    assert "→ Matrix Observatory provisioning (Tuwunel)" in out
    assert "✓ tuwunel: current v1.9.0" in out
    # card printed after provisioning
    assert "Matrix Observatory — first login (Element X)" in out
    assert f"homeserver URL:      {HOMESERVER_URL}" in out
    assert f"owner account:       {MXID}" in out
    assert str(creds) in out
    assert "first gateway start" in out
    # reveal prompt answered no → password stays hidden
    assert PASSWORD not in out
    assert remaining == []  # toggle + reveal both ran


def test_section_provisioned_enabled_state_and_card(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_status(
        provisioned=True,
        config_exists=True,
        binary_installed=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
        homeserver_reachable=True,
        unit_active=True,
    )])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, False]
    )

    assert "Provisioned:          yes" in out
    assert "Homeserver reachable: yes" in out
    assert "Unit active:          yes" in out
    assert "observatory.enabled:  yes  (config.yaml)" in out
    assert "Matrix Observatory — first login (Element X)" in out
    assert "tailscale serve" in out
    assert "QR code does NOT work" in out
    assert "E2EE:                off" in out
    assert "end-to-end encrypted once enabled" in out
    assert PASSWORD not in out
    assert fake.calls["provision"] == 0
    assert remaining == []


def test_section_provisioned_disabled_state(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_status(
        provisioned=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
        enabled=False,
        e2ee=True,
    )])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[False, False]
    )

    assert "observatory.enabled:  no  (config.yaml)" in out
    assert "E2EE:                on" in out
    assert "rooms are end-to-end encrypted" in out
    assert PASSWORD not in out
    assert remaining == []


def test_card_reveals_password_only_on_explicit_yes(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_status(
        provisioned=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
    )])
    out, _config, _remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, True]
    )

    assert PASSWORD in out  # explicit reveal
    assert str(creds) in out  # path is always shown too
    assert "0600" in out


# ---------------------------------------------------------------------------
# toggle + resilience
# ---------------------------------------------------------------------------


def test_toggle_writes_observatory_enabled_to_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_status(
        provisioned=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
    )])
    out, _config, _remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[False, False]
    )

    assert f"observatory.enabled = false written to {get_config_path()}" in out
    reloaded = load_config()
    obs = reloaded.get("observatory")
    assert isinstance(obs, dict) and obs.get("enabled") is False
    # config.yaml (not .env) carries the flag
    assert "observatory" in get_config_path().read_text(encoding="utf-8")


def test_toggle_unchanged_leaves_config_alone(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake = _FakeProvision([_status()])
    out, _config, _remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True]
    )

    assert "Keeping observatory.enabled = true" in out
    assert not get_config_path().exists()


def test_provision_failure_never_kills_the_wizard(monkeypatch, capsys):
    fake = _FakeProvision(
        [_status()],
        provision_error=provision_mod.ProvisionError("boom"),
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0, yes_no=[True]
    )

    assert "Observatory provisioning failed: boom" in out
    assert "the wizard continues" in out
    assert "mercury setup observatory" in out
    assert fake.calls["provision"] == 1
    assert remaining == []


def test_missing_package_degrades_to_hint(monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: None)
    monkeypatch.setattr(
        setup_mod, "prompt_choice",
        lambda *a, **k: pytest.fail("prompt_choice must not run without the package"),
    )
    setup_mod.setup_observatory({})
    out = capsys.readouterr().out

    assert "not found in this install" in out
    assert "matrix-observatory" in out


def test_unreadable_state_degrades_to_hint(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    fake = SimpleNamespace(status_summary=boom)
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    monkeypatch.setattr(
        setup_mod, "prompt_choice",
        lambda *a, **k: pytest.fail("prompt_choice must not run on unreadable state"),
    )
    setup_mod.setup_observatory({})
    out = capsys.readouterr().out

    assert "Could not read observatory state" in out


# ---------------------------------------------------------------------------
# section placement + parser
# ---------------------------------------------------------------------------


def test_section_registered_after_gateway_in_registry():
    keys = [k for k, _label, _fn in setup_mod.SETUP_SECTIONS]
    assert "observatory" in keys
    assert keys.index("model") < keys.index("observatory")
    assert keys.index("tts") < keys.index("observatory")
    assert keys.index("gateway") < keys.index("observatory")
    assert keys.index("observatory") < keys.index("tools")


def test_setup_parser_accepts_observatory_section():
    import argparse

    from mercury_cli.subcommands.setup import build_setup_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    build_setup_parser(subparsers, cmd_setup=lambda a: None)
    args = subparsers.choices["setup"].parse_args(["observatory"])
    assert args.section == "observatory"


# ---------------------------------------------------------------------------
# non-interactive path
# ---------------------------------------------------------------------------


def test_noninteractive_guidance_state_and_exact_command(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)

    setup_mod.print_noninteractive_observatory_guidance()
    out = capsys.readouterr().out

    assert "Matrix Observatory (bundled)" in out
    assert "Provisioned: no" in out
    assert f"homeserver reachable: no ({HOMESERVER_URL})" in out
    assert f"observatory.enabled: yes (config.yaml)" in out
    # exact headless command: no `mercury observatory` wrapper exists
    assert (
        f"  PYTHONPATH={setup_mod.PROJECT_ROOT} "
        f"{sys.executable} -m observatory.provision" in out
    )
    assert "mercury config set observatory.enabled false" in out
    assert "user-guide/messaging/matrix-observatory" in out


def test_noninteractive_guidance_provisioned_state(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True, enabled=False)])
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)

    setup_mod.print_noninteractive_observatory_guidance()
    out = capsys.readouterr().out

    assert "Provisioned: yes" in out
    assert "observatory.enabled: no" in out
    assert PASSWORD not in out


def test_run_setup_wizard_noninteractive_prints_observatory_block(
    monkeypatch, capsys, tmp_path
):
    """End-to-end wiring: a headless `mercury setup` prints the observatory
    state summary + provision command alongside the generic guidance."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        provision_mod, "_homeserver_reachable",
        lambda url, timeout=2.0: False,
    )
    monkeypatch.setattr(provision_mod, "_unit_active", lambda: False)

    args = Namespace(
        non_interactive=True, section=None, reset=False,
        portal=False, quick=False, reconfigure=False,
    )
    setup_mod.run_setup_wizard(args)
    out = capsys.readouterr().out

    assert "Mercury Setup — Non-interactive mode" in out  # generic guidance first
    assert "Matrix Observatory (bundled)" in out
    assert "-m observatory.provision" in out
    assert f"  PYTHONPATH={setup_mod.PROJECT_ROOT} {sys.executable}" in out


# ---------------------------------------------------------------------------
# provision.py helper contracts
# ---------------------------------------------------------------------------


def _patch_probes(monkeypatch, *, reachable=False, unit=False):
    monkeypatch.setattr(
        provision_mod, "_homeserver_reachable",
        lambda url, timeout=2.0: reachable,
    )
    monkeypatch.setattr(provision_mod, "_unit_active", lambda: unit)


def test_status_summary_unprovisioned_contract(tmp_path, monkeypatch):
    _patch_probes(monkeypatch)
    st = provision_mod.status_summary(tmp_path)

    assert st["provisioned"] is False
    assert st["config_exists"] is False
    assert st["binary_installed"] is False
    assert st["owner_credentials_exist"] is False
    assert st["homeserver_url"] == HOMESERVER_URL
    assert st["homeserver_reachable"] is False
    assert st["unit_active"] is False
    assert st["unit_name"] == HOMESERVER_UNIT_NAME
    assert st["enabled"] is True   # D1: default on, unreadable config stays on
    # delegates to observatory.e2ee.e2ee_enabled — the DEFAULT is owned
    # there (flipped to true 2026-09-08 by the e2ee track); we assert the
    # delegation, not a hardcoded default.
    from observatory.e2ee import e2ee_enabled

    assert st["e2ee"] == e2ee_enabled(tmp_path)
    assert st["observatory_dir"] == str(tmp_path / "observatory")


def test_status_summary_provisioned_no_secrets(tmp_path, monkeypatch):
    _patch_probes(monkeypatch, reachable=True, unit=True)
    _write_credentials(tmp_path)
    obs = tmp_path / "observatory"
    (obs / "tuwunel.toml").write_text("[global]\n", encoding="utf-8")
    (obs / "bin").mkdir()
    (obs / "bin" / "tuwunel").write_text("#!/bin/sh\n", encoding="utf-8")

    st = provision_mod.status_summary(tmp_path)

    assert st["provisioned"] is True
    assert st["config_exists"] is True
    assert st["binary_installed"] is True
    assert st["owner_credentials_exist"] is True
    assert st["owner_credentials_path"] == str(obs / "owner-credentials.json")
    assert st["homeserver_reachable"] is True
    assert st["unit_active"] is True
    # no-secrets law: the summary never carries the password/tokens
    assert PASSWORD not in json.dumps(st)


def test_status_summary_reads_e2ee_flag_from_config(tmp_path, monkeypatch):
    _patch_probes(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "observatory:\n  e2ee: true\n", encoding="utf-8"
    )
    assert provision_mod.status_summary(tmp_path)["e2ee"] is True


def test_provision_in_wizard_prints_and_returns_summary(tmp_path, monkeypatch, capsys):
    summary = {
        "tuwunel": {
            "action": "current",
            "version": "1.9.0",
            "binary": str(tmp_path / "observatory" / "bin" / "tuwunel"),
        },
        "config": "kept",
        "appservice": "kept",
        "owner": "exists",
        "unit": "started",
    }
    monkeypatch.setattr(provision_mod, "provision", lambda *a, **k: summary)

    got = provision_mod.provision_in_wizard(tmp_path)
    out = capsys.readouterr().out

    assert got == summary
    assert "→ Matrix Observatory provisioning (Tuwunel)" in out
    assert "  ✓ tuwunel: current v1.9.0" in out
    assert f"  ✓ systemd unit: started ({HOMESERVER_UNIT_NAME})" in out


def test_provision_in_wizard_raises_never_exits(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise provision_mod.ProvisionError("no tuwunel for you")

    monkeypatch.setattr(provision_mod, "provision", boom)
    with pytest.raises(provision_mod.ProvisionError):
        provision_mod.provision_in_wizard(tmp_path)


def _cli_summary(home: Path, unit: str) -> dict:
    return {
        "tuwunel": {
            "action": "current",
            "version": "1.9.0",
            "binary": str(home / "observatory" / "bin" / "tuwunel"),
        },
        "config": "kept",
        "appservice": "kept",
        "owner": "exists",
        "unit": unit,
    }


def test_cli_main_output_bytes_unchanged(tmp_path, monkeypatch, capsys):
    """Byte-compat guard: extracting _print_summary changed nothing."""
    monkeypatch.setattr(
        provision_mod, "provision", lambda *a, **k: _cli_summary(tmp_path, "started")
    )
    assert provision_mod.main([]) == 0
    out = capsys.readouterr().out
    assert out == (
        "→ Matrix Observatory provisioning (Tuwunel)\n"
        f"  ✓ tuwunel: current v1.9.0 → {tmp_path}/observatory/bin/tuwunel\n"
        "  ✓ config: kept (tuwunel.toml)\n"
        "  ✓ appservice registration: kept\n"
        "  ✓ owner account: exists\n"
        f"  ✓ systemd unit: started ({HOMESERVER_UNIT_NAME})\n"
    )


def test_cli_main_skipped_unit_and_failure_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        provision_mod, "provision", lambda *a, **k: _cli_summary(tmp_path, "skipped")
    )
    assert provision_mod.main([]) == 0
    out = capsys.readouterr().out
    assert (
        "  ⚠ systemd user unit skipped (systemd unavailable or --no-systemd);\n"
        f"    start manually: {tmp_path}/observatory/bin/tuwunel"
        " -c <mercury-home>/observatory/tuwunel.toml\n"
    ) in out

    def boom(*a, **k):
        raise provision_mod.ProvisionError("nope")

    monkeypatch.setattr(provision_mod, "provision", boom)
    assert provision_mod.main([]) == 1
    assert "✗ observatory provisioning failed: nope" in capsys.readouterr().out
