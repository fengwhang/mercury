"""Wizard integration for the Matrix observatory: the 'Matrix Observatory
(bundled)' section in mercury_cli/setup.py + the provision.py helpers it
calls (status_summary / provision_in_wizard).

Laws under test:

- **section rendering per state** — fresh/unprovisioned, provisioned+
  enabled, provisioned+disabled each render their state lines and the
  skip/install outcomes;
- **no secret leaks** — the owner password never appears on screen (no
  reveal prompt exists; it lives in $MERCURY_HOME/.env + credentials file);
:- **non-interactive path** — state summary + the headless auto command
  (``mercury setup observatory --non-interactive``), following
  print_noninteractive_setup_guidance conventions;
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


_TS_ABSENT = {"available": False, "up": False, "ip": None, "dns_name": None}
_TS_DOWN = {"available": True, "up": False, "ip": None, "dns_name": None}
_TS_UP_IP = {"available": True, "up": True, "ip": "100.89.0.5", "dns_name": None}
_TS_UP_DNS = {
    "available": True, "up": True,
    "ip": "100.89.0.5", "dns_name": "box.tail.ts.net",
}


class _FakeProvision:
    """observatory.provision stand-in for the section (records calls)."""

    def __init__(self, statuses, *, provision_error=None, tailscale=None,
                 bind_error=None, bind_address=None, crypto="ready",
                 sidecar="installed", healed=None, tree="converged-3",
                 crypto_error=None, sidecar_error=None, tree_error=None):
        self._statuses = list(statuses)
        self._provision_error = provision_error
        self._tailscale = dict(tailscale) if tailscale is not None else dict(_TS_ABSENT)
        self._bind_error = bind_error
        self._bind_address = bind_address
        self._crypto = crypto
        self._sidecar = sidecar
        self._healed = healed
        self._tree = tree
        self._crypto_error = crypto_error
        self._sidecar_error = sidecar_error
        self._tree_error = tree_error
        self.calls = {"provision": 0, "status": 0, "bind": 0,
                      "crypto": 0, "sidecar": 0, "heal": 0, "tree": 0}
        self.bind_ips: list = []

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

    def ensure_crypto_stack(self, *a, **k):
        self.calls["crypto"] += 1
        if self._crypto_error is not None:
            raise self._crypto_error
        return self._crypto

    def ensure_sidecar_unit(self, *a, **k):
        self.calls["sidecar"] += 1
        if self._sidecar_error is not None:
            raise self._sidecar_error
        return self._sidecar

    def heal_owner_url(self, *a, **k):
        self.calls["heal"] += 1
        return self._healed

    def verify_and_converge_gateway(self, *a, **k):
        self.calls["tree"] += 1
        if self._tree_error is not None:
            raise self._tree_error
        return self._tree

    def detect_tailscale(self, *a, **k):
        return dict(self._tailscale)

    def set_tuwunel_bind(self, ip, *a, **k):
        self.calls["bind"] += 1
        self.bind_ips.append(ip)
        if self._bind_error is not None:
            raise self._bind_error
        return ip

    def current_bind_address(self, *a, **k):
        return self._bind_address


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
        monkeypatch, capsys, fake, choice=0, yes_no=[True]
    )

    assert fake.calls["provision"] == 1
    # auto steps run without further commands: crypto + sidecar + heal/converge
    assert fake.calls["crypto"] == 1
    assert fake.calls["sidecar"] == 1
    assert fake.calls["heal"] == 1
    assert fake.calls["tree"] == 1
    assert "Sidecar unit installed" in out
    assert "Gateway tree converged-3" in out
    # provision output is shown
    assert "→ Matrix Observatory provisioning (Tuwunel)" in out
    assert "✓ tuwunel: current v1.9.0" in out
    # manual-only card printed after provisioning
    assert "Matrix Observatory — first login (FluffyChat)" in out
    assert f"homeserver URL:      {HOMESERVER_URL}" in out
    assert f"owner account:       {MXID}" in out
    assert str(creds) in out
    # everything else is automatic — never a manual TODO on the card
    assert "first gateway start" not in out
    assert "E2EE:" not in out
    assert "local network:" not in out
    assert "space tree:" not in out
    # no reveal prompt exists anymore — the password is never printed
    assert PASSWORD not in out
    assert "MATRIX_OBS_OWNER_PASSWORD" in out
    assert remaining == []  # only the toggle prompt ran

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
        monkeypatch, capsys, fake, choice=1, yes_no=[True]
    )
    assert "Provisioned:          yes" in out
    assert "Homeserver reachable: yes" in out
    assert "Unit active:          yes" in out
    assert "observatory.enabled:  yes  (config.yaml)" in out
    assert "Matrix Observatory — first login (FluffyChat)" in out
    assert "Tailscale not detected" in out
    assert "https://tailscale.com" in out
    assert "headscale" in out
    assert "on this machine:" in out  # localhost line kept for desktop
    assert fake.calls["bind"] == 0  # absent tailnet: no bind offer, no prompt
    assert "in FluffyChat:" in out and "add account" in out
    # manual-only card: E2EE/tree/network lines never print
    assert "E2EE:" not in out
    assert "space tree:" not in out
    assert "local network:" not in out
    assert PASSWORD not in out
    assert fake.calls["provision"] == 0
    # skip leaves everything alone — no auto steps either
    assert fake.calls["crypto"] == 0
    assert fake.calls["sidecar"] == 0
    assert fake.calls["tree"] == 0
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
        monkeypatch, capsys, fake, choice=1, yes_no=[False]
    )

    assert "observatory.enabled:  no  (config.yaml)" in out
    # manual-only card: no E2EE status line even when the flag is on
    assert "E2EE:" not in out
    assert "space tree:" not in out
    assert PASSWORD not in out
    assert remaining == []


def test_card_never_prints_password(monkeypatch, capsys, tmp_path):
    """The owner password is never printed — no reveal branch exists."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_status(
        provisioned=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
    )])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True]
    )

    assert PASSWORD not in out
    assert str(creds) in out  # path is shown, value never is
    assert "MATRIX_OBS_OWNER_PASSWORD" in out
    assert "never printed here" in out
    assert "0600" in out
    assert remaining == []


def test_card_asks_no_password_question(monkeypatch, capsys, tmp_path):
    """The card itself runs zero prompts (reveal branch deleted)."""
    creds = _write_credentials(tmp_path)
    status = _provisioned_status(creds)

    def _boom(question, default=True):
        raise AssertionError(f"card must not prompt: {question!r}")

    monkeypatch.setattr(setup_mod, "prompt_yes_no", _boom)
    setup_mod._print_observatory_setup_card(status, dict(_TS_ABSENT))
    out = capsys.readouterr().out

    assert PASSWORD not in out
    assert "MATRIX_OBS_OWNER_PASSWORD" in out


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
        monkeypatch, capsys, fake, choice=1, yes_no=[False]
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


def test_section_registered_before_gateway_in_registry():
    """Observatory is the primary chat: it runs before gateway platforms."""
    keys = [k for k, _label, _fn in setup_mod.SETUP_SECTIONS]
    assert "observatory" in keys
    assert keys.index("model") < keys.index("observatory")
    assert keys.index("tts") < keys.index("observatory")
    assert keys.index("observatory") < keys.index("gateway")
    assert keys.index("observatory") < keys.index("tools")


def test_section_choice_prompt_marks_recommended(monkeypatch, capsys):
    """The observatory install question carries the RECOMMENDED tag."""
    fake = _FakeProvision([_status()])
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    seen: list = []
    monkeypatch.setattr(
        setup_mod, "prompt_choice",
        lambda q, c, d=0, description=None: seen.append(q) or 1,
    )
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
    setup_mod.setup_observatory({})
    assert seen == ["Set up the Matrix observatory now (RECOMMENDED)?"]


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
    # headless auto command (no manual python -m step remains)
    assert "mercury setup observatory --non-interactive" in out
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
    state summary + auto command alongside the generic guidance."""
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
    assert "mercury setup observatory --non-interactive" in out


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


# ---------------------------------------------------------------------------
# tailscale detect-and-assist (detect → card → bind → headless)
# ---------------------------------------------------------------------------


def _mock_tailscale_run(*, status_rc=0, ip_out="100.89.0.5\n", json_out="",
                        calls=None):
    """A subprocess.run stand-in serving the three detect_tailscale probes."""
    import subprocess as _sp

    def _run(argv, **kwargs):
        if calls is not None:
            calls.append(list(argv))
        if list(argv[:2]) == ["tailscale", "status"] and list(argv[2:]) == ["--json"]:
            return _sp.CompletedProcess(argv, 0, json_out, "")
        if list(argv[:2]) == ["tailscale", "status"]:
            return _sp.CompletedProcess(argv, status_rc, "", "")
        if list(argv) == ["tailscale", "ip", "-4"]:
            return _sp.CompletedProcess(argv, 0, ip_out, "")
        raise AssertionError(f"unexpected tailscale argv: {argv!r}")

    return _run


def _provisioned_status(creds, **over):
    st = _status(
        provisioned=True,
        config_exists=True,
        binary_installed=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
    )
    st.update(over)
    return st


def test_detect_tailscale_absent_never_probes(monkeypatch):
    monkeypatch.setattr(provision_mod.shutil, "which", lambda _name: None)

    def _fail(*a, **k):
        raise AssertionError("absent tailscale must not shell out")

    monkeypatch.setattr(provision_mod.subprocess, "run", _fail)
    assert provision_mod.detect_tailscale() == dict(_TS_ABSENT)


def test_detect_tailscale_present_up_captures_ip_and_dns(monkeypatch):
    import json as _json

    monkeypatch.setattr(
        provision_mod.shutil, "which", lambda _name: "/usr/bin/tailscale"
    )
    payload = _json.dumps({
        "Self": {
            "DNSName": "box.tail.ts.net.",
            "TailscaleIPs": ["100.89.0.5", "fd7a:dead:beef::1"],
        }
    })
    calls: list = []
    monkeypatch.setattr(
        provision_mod.subprocess, "run",
        _mock_tailscale_run(json_out=payload, calls=calls),
    )
    d = provision_mod.detect_tailscale()
    assert d == {
        "available": True, "up": True,
        "ip": "100.89.0.5", "dns_name": "box.tail.ts.net",
    }
    assert calls and all(c[0] == "tailscale" for c in calls)


def test_detect_tailscale_present_down(monkeypatch):
    monkeypatch.setattr(
        provision_mod.shutil, "which", lambda _name: "/usr/bin/tailscale"
    )
    calls: list = []
    monkeypatch.setattr(
        provision_mod.subprocess, "run",
        _mock_tailscale_run(status_rc=1, calls=calls),
    )
    d = provision_mod.detect_tailscale()
    assert d["available"] is True
    assert d["up"] is False
    assert d["ip"] is None and d["dns_name"] is None


def test_detect_tailscale_never_raises(monkeypatch):
    monkeypatch.setattr(
        provision_mod.shutil, "which",
        lambda _name: (_ for _ in ()).throw(OSError("no which")),
    )
    assert provision_mod.detect_tailscale()["up"] is False

    monkeypatch.setattr(
        provision_mod.shutil, "which", lambda _name: "/usr/bin/tailscale"
    )

    def _boom(*a, **k):
        raise OSError("no daemon")

    monkeypatch.setattr(provision_mod.subprocess, "run", _boom)
    assert provision_mod.detect_tailscale() == {
        "available": True, "up": False, "ip": None, "dns_name": None,
    }


def test_detect_only_invokes_tailscale_binary(monkeypatch):
    import json as _json

    monkeypatch.setattr(
        provision_mod.shutil, "which", lambda _name: "/usr/bin/tailscale"
    )
    payload = _json.dumps({"Self": {"DNSName": "box.tail.ts.net"}})
    calls: list = []
    monkeypatch.setattr(
        provision_mod.subprocess, "run",
        _mock_tailscale_run(json_out=payload, calls=calls),
    )
    provision_mod.detect_tailscale()
    assert calls, "detection must probe the tailnet when present"
    for argv in calls:
        assert argv[0] == "tailscale"
        assert argv[1] in {"status", "ip"}  # never install/login/up


def test_tailscale_phone_url_selection():
    assert provision_mod.tailscale_phone_url(
        dict(_TS_UP_DNS), HOMESERVER_PORT_DEFAULT
    ) == f"http://box.tail.ts.net:{HOMESERVER_PORT_DEFAULT}"
    assert provision_mod.tailscale_phone_url(
        dict(_TS_UP_IP), HOMESERVER_PORT_DEFAULT
    ) == f"http://100.89.0.5:{HOMESERVER_PORT_DEFAULT}"
    assert provision_mod.tailscale_phone_url(dict(_TS_DOWN)) is None
    assert provision_mod.tailscale_phone_url(dict(_TS_ABSENT)) is None
    assert provision_mod.tailscale_phone_url(
        {"available": True, "up": True, "ip": None, "dns_name": None}
    ) is None


def test_setup_phone_url_prefers_dns_keeps_port():
    assert setup_mod._tailscale_phone_url(
        dict(_TS_UP_DNS), HOMESERVER_URL
    ) == f"http://box.tail.ts.net:{HOMESERVER_PORT_DEFAULT}"
    assert setup_mod._tailscale_phone_url(
        dict(_TS_UP_IP), HOMESERVER_URL
    ) == f"http://100.89.0.5:{HOMESERVER_PORT_DEFAULT}"
    assert setup_mod._tailscale_phone_url(dict(_TS_ABSENT), HOMESERVER_URL) is None


def test_card_detected_shows_phone_url_keeps_localhost(
    monkeypatch, capsys, tmp_path
):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds, homeserver_reachable=True, unit_active=True)],
        tailscale=dict(_TS_UP_DNS),
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, False]
    )
    assert f"homeserver URL:      {HOMESERVER_URL}" in out
    assert f"http://box.tail.ts.net:{HOMESERVER_PORT_DEFAULT}" in out
    assert "over Tailscale" in out
    assert "on this machine:" in out
    assert "Tailscale not detected" not in out
    assert PASSWORD not in out
    assert fake.calls["bind"] == 0
    assert "Keeping the homeserver on its current address." in out
    assert remaining == []


def test_card_falls_back_to_tailnet_ip(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_UP_IP)
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, False]
    )
    assert f"http://100.89.0.5:{HOMESERVER_PORT_DEFAULT}" in out
    assert "over Tailscale" in out
    assert remaining == []


def test_card_down_shows_reconnect_hint_no_bind(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_DOWN)
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True]
    )
    assert "not connected" in out
    assert "tailscale up" in out
    assert fake.calls["bind"] == 0
    assert remaining == []


def test_bind_offer_uses_exact_prompt_text(monkeypatch):
    seen: list = []

    def _ask(question, default=False):
        seen.append(question)
        return False

    monkeypatch.setattr(setup_mod, "prompt_yes_no", _ask)
    setup_mod._offer_tailscale_bind(
        SimpleNamespace(set_tuwunel_bind=lambda ip: ip), dict(_TS_UP_IP)
    )
    assert seen == [
        "Bind homeserver to the Tailscale interface only?"
        " (unreachable from LAN/internet)"
    ]


def test_bind_offer_yes_restart_yes_restarts_unit(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_UP_IP)
    )
    calls: list = []

    def _run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", _run)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, True, True]
    )
    assert fake.calls["bind"] == 1
    assert fake.bind_ips == ["100.89.0.5"]
    assert calls == [["systemctl", "--user", "restart", HOMESERVER_UNIT_NAME]]
    assert "100.89.0.5" in out
    assert HOMESERVER_UNIT_NAME in out
    assert remaining == []


def test_bind_offer_yes_restart_no_keeps_manual_line(
    monkeypatch, capsys, tmp_path
):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_UP_IP)
    )

    def _boom(argv, **kwargs):
        raise AssertionError("restart must not be attempted on 'no'")

    monkeypatch.setattr("subprocess.run", _boom)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, True, False]
    )
    assert fake.calls["bind"] == 1
    assert "100.89.0.5" in out
    assert (
        "Restart the homeserver to apply:"
        f" systemctl --user restart {HOMESERVER_UNIT_NAME}" in out
    )
    assert remaining == []


def test_bind_offer_restart_failure_warns_with_manual_command(
    monkeypatch, capsys, tmp_path
):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_UP_IP)
    )

    def _boom(argv, **kwargs):
        raise OSError("systemd not running")

    monkeypatch.setattr("subprocess.run", _boom)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, True, True]
    )
    assert fake.calls["bind"] == 1
    assert "Could not restart" in out
    assert f"systemctl --user restart {HOMESERVER_UNIT_NAME}" in out
    assert remaining == []


def test_bind_restart_offer_uses_exact_prompt_text(monkeypatch):
    seen: list = []

    def _ask(question, default=True):
        seen.append((question, default))
        return "Bind homeserver" in question

    monkeypatch.setattr(setup_mod, "prompt_yes_no", _ask)
    setup_mod._offer_tailscale_bind(
        SimpleNamespace(set_tuwunel_bind=lambda ip: ip), dict(_TS_UP_IP)
    )
    assert seen == [
        (
            "Bind homeserver to the Tailscale interface only?"
            " (unreachable from LAN/internet)",
            False,
        ),
        (
            "Restart the homeserver now?"
            " (necessary to apply the new bind address)",
            True,
        ),
    ]


def test_bind_offer_no_keeps_address(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_UP_IP)
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, False]
    )
    assert fake.calls["bind"] == 0
    assert "Keeping the homeserver on its current address." in out
    assert remaining == []


def test_bind_failure_degrades_to_hand_edit_hint(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)],
        tailscale=dict(_TS_UP_IP),
        bind_error=provision_mod.ProvisionError("disk on fire"),
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, True]
    )
    assert fake.calls["bind"] == 1
    assert "Could not bind the homeserver to 100.89.0.5" in out
    assert "by hand" in out
    assert "tuwunel.toml" in out
    assert "the wizard continues" in out or "Guide:" in out
    assert remaining == []


def test_set_tuwunel_bind_rewrites_address_line(tmp_path):
    home = tmp_path / "mhome"
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "tuwunel.toml").write_text(
        '[global]\nserver_name = "mercury.local"\n'
        'address = "127.0.0.1"\nport = 18008\n',
        encoding="utf-8",
    )
    got = provision_mod.set_tuwunel_bind("100.89.0.5", home)
    assert got == "100.89.0.5"
    text = (obs / "tuwunel.toml").read_text(encoding="utf-8")
    assert 'address = "100.89.0.5"' in text
    assert 'server_name = "mercury.local"' in text
    assert "port = 18008" in text


def test_set_tuwunel_bind_fails_when_unprovisioned(tmp_path):
    with pytest.raises(
        provision_mod.ProvisionError, match="not provisioned"
    ):
        provision_mod.set_tuwunel_bind("100.89.0.5", tmp_path / "empty-home")


def test_set_tuwunel_bind_rejects_non_ip(tmp_path):
    with pytest.raises(provision_mod.ProvisionError):
        provision_mod.set_tuwunel_bind("not-an-ip", tmp_path)
    with pytest.raises(provision_mod.ProvisionError):
        provision_mod.set_tuwunel_bind("   ", tmp_path)


def test_set_tuwunel_bind_never_touches_the_server():
    import inspect as _inspect

    src = _inspect.getsource(provision_mod.set_tuwunel_bind)
    assert "systemctl" not in src
    assert "subprocess" not in src
    assert "restart" in provision_mod.set_tuwunel_bind.__doc__


def test_noninteractive_includes_tailscale_up(monkeypatch, capsys):
    fake = _FakeProvision(
        [_status(provisioned=True)], tailscale=dict(_TS_UP_DNS)
    )
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    setup_mod.print_noninteractive_observatory_guidance()
    out = capsys.readouterr().out
    assert "Tailscale: up" in out
    assert f"http://box.tail.ts.net:{HOMESERVER_PORT_DEFAULT}" in out


def test_noninteractive_includes_tailscale_absent(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    setup_mod.print_noninteractive_observatory_guidance()
    out = capsys.readouterr().out
    assert "Tailscale: not detected" in out
    assert "https://tailscale.com" in out


def test_no_tailscale_auto_install():
    """Detect-and-assist law: the wizard never installs Tailscale itself."""
    import inspect as _inspect

    src = "".join([
        _inspect.getsource(provision_mod.detect_tailscale),
        _inspect.getsource(provision_mod.set_tuwunel_bind),
        _inspect.getsource(setup_mod._tailscale_status),
        _inspect.getsource(setup_mod._offer_tailscale_bind),
        _inspect.getsource(setup_mod._print_observatory_setup_card),
    ])
    for token in (
        "apt-get", "apt install", "dnf install", "yum install",
        "brew install", "pacman -S", "snap install", "choco install",
        "pip install", "curl -", "wget http",
        "systemctl start", "systemctl restart", "service tailscale",
    ):
        assert token not in src, f"auto-install risk: {token!r} in tailscale path"
    assert "shutil.which" in _inspect.getsource(provision_mod.detect_tailscale)


# ---------------------------------------------------------------------------
# localhost-bind mismatch ACTION (tailnet up, tuwunel still on 127.0.0.1)
# ---------------------------------------------------------------------------


def test_bind_mismatch_line_when_loopback_and_up():
    line = setup_mod._bind_mismatch_action_line("127.0.0.1", dict(_TS_UP_IP))
    assert line is not None
    assert "only listens on localhost" in line
    assert "phones cannot reach it" in line
    assert "mercury setup observatory" in line
    assert "systemctl --user restart" in line
    assert HOMESERVER_UNIT_NAME in line


def test_bind_mismatch_line_covers_ipv6_loopback():
    line = setup_mod._bind_mismatch_action_line("::1", dict(_TS_UP_DNS))
    assert line is not None and "only listens on localhost" in line


@pytest.mark.parametrize("address", ["100.89.0.5", "0.0.0.0", None])
def test_bind_mismatch_absent_when_bound_or_unknown(address):
    assert setup_mod._bind_mismatch_action_line(address, dict(_TS_UP_IP)) is None


@pytest.mark.parametrize("ts", [dict(_TS_ABSENT), dict(_TS_DOWN), None])
def test_bind_mismatch_absent_when_tailnet_down(ts):
    assert setup_mod._bind_mismatch_action_line("127.0.0.1", ts) is None


def test_section_prints_mismatch_action_on_declined_bind(
    monkeypatch, capsys, tmp_path
):
    """Tailnet up + toml still localhost after a declined bind → ACTION."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)],
        tailscale=dict(_TS_UP_IP),
        bind_address="127.0.0.1",
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, False]
    )
    assert "only listens on localhost" in out
    assert "mercury setup observatory" in out
    assert "systemctl --user restart" in out
    assert HOMESERVER_UNIT_NAME in out
    assert remaining == []


def test_section_hides_mismatch_action_once_bound(
    monkeypatch, capsys, tmp_path
):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)],
        tailscale=dict(_TS_UP_IP),
        bind_address="100.89.0.5",
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True, False]
    )
    assert "only listens on localhost" not in out
    assert remaining == []


def test_noninteractive_prints_mismatch_action_via_toml_fallback(
    monkeypatch, capsys, tmp_path
):
    """Headless guidance reads the real toml when obs lacks the helper."""
    home = tmp_path / "mhome"
    obs_dir = home / "observatory"
    obs_dir.mkdir(parents=True)
    (obs_dir / "tuwunel.toml").write_text(
        '[global]\nserver_name = "mercury.local"\n'
        'address = "127.0.0.1"\nport = 18008\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERCURY_HOME", str(home))
    status = _status(provisioned=True)
    bare = SimpleNamespace(
        status_summary=lambda: dict(status),
        detect_tailscale=lambda: dict(_TS_UP_DNS),
    )
    assert not hasattr(bare, "current_bind_address")
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: bare)
    setup_mod.print_noninteractive_observatory_guidance()
    out = capsys.readouterr().out
    assert "only listens on localhost" in out
    assert "systemctl --user restart" in out


def test_current_bind_address_reads_toml_and_list_forms(tmp_path):
    home = tmp_path / "mhome"
    obs_dir = home / "observatory"
    obs_dir.mkdir(parents=True)
    toml = obs_dir / "tuwunel.toml"
    toml.write_text(
        '[global]\naddress = "127.0.0.1"\nport = 18008\n', encoding="utf-8"
    )
    assert provision_mod.current_bind_address(home) == "127.0.0.1"
    toml.write_text(
        '[global]\naddress = ["100.89.0.5", "127.0.0.1"]\nport = 18008\n',
        encoding="utf-8",
    )
    assert provision_mod.current_bind_address(home) == "100.89.0.5"
    assert provision_mod.current_bind_address(tmp_path / "empty-home") is None


# ---------------------------------------------------------------------------
# end-of-setup login card reprint
# ---------------------------------------------------------------------------


def test_reprint_card_after_summary_when_provisioned(
    monkeypatch, capsys, tmp_path
):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_ABSENT)
    )
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    setup_mod._reprint_observatory_login_card()
    out = capsys.readouterr().out
    assert "Save this — Matrix login" in out
    assert "Matrix Observatory — first login (FluffyChat)" in out
    assert f"homeserver URL:      {HOMESERVER_URL}" in out
    assert PASSWORD not in out


def test_reprint_card_silent_when_unprovisioned(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    setup_mod._reprint_observatory_login_card()
    assert capsys.readouterr().out == ""


def test_reprint_card_never_raises(monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: None)
    setup_mod._reprint_observatory_login_card()

    def _boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(
        setup_mod, "_load_observatory_provision",
        lambda: SimpleNamespace(status_summary=_boom),
    )
    setup_mod._reprint_observatory_login_card()
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# observatory Guide pointer (in-repo until the docs site publishes the page)
# ---------------------------------------------------------------------------


def test_observatory_guide_lines_never_link_the_unpublished_page():
    """No Guide: line may print the fictitious docs-site matrix-observatory URL."""
    import inspect as _inspect

    src = _inspect.getsource(setup_mod)
    assert "_OBSERVATORY_DOCS_URL" not in src
    for line in src.splitlines():
        if "Guide:" in line:
            assert "hermes-agent.nousresearch.com" not in line


def test_observatory_section_prints_in_repo_guide(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_provisioned_status(creds)], tailscale=dict(_TS_ABSENT)
    )
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=1, yes_no=[True]
    )
    assert "Guide: docs/design/matrix-observatory.md" in out
    assert "website/docs/user-guide/messaging/matrix-observatory.md" in out
    assert "hermes-agent.nousresearch.com" not in out
    assert remaining == []


def test_noninteractive_observatory_prints_in_repo_guide(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True)])
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    setup_mod.print_noninteractive_observatory_guidance()
    out = capsys.readouterr().out
    assert "Guide: docs/design/matrix-observatory.md" in out
    assert "website/docs/user-guide/messaging/matrix-observatory.md" in out
    assert "hermes-agent.nousresearch.com" not in out


# ---------------------------------------------------------------------------
# cua-driver persistent telemetry-off (setup_telemetry tail)
# ---------------------------------------------------------------------------

_CUA_PERSISTENT_HELPER = (
    "tools.computer_use.cua_backend.cua_driver_telemetry_disable_persistent"
)


def test_setup_telemetry_disables_cua_telemetry_persistently_once(
    monkeypatch, capsys
):
    """Default policy flips the driver's persistent switch exactly once."""
    calls: list = []

    def _off(**kwargs):
        calls.append(1)
        return True

    monkeypatch.setattr(_CUA_PERSISTENT_HELPER, _off)
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no", lambda q, default=True: default
    )
    setup_mod.setup_telemetry({})
    out = capsys.readouterr().out
    assert len(calls) == 1
    assert "persistently" in out  # printed confirmation
    assert "CUA_DRIVER_RS_TELEMETRY_ENABLED=0" in out  # env line kept


def test_setup_telemetry_skips_persistent_disable_on_opt_in(
    monkeypatch, capsys
):
    """computer_use.cua_telemetry opt-in leaves the driver default alone."""

    def _boom(**kwargs):
        raise AssertionError("persistent off-switch must not run on opt-in")

    monkeypatch.setattr(_CUA_PERSISTENT_HELPER, _boom)
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no", lambda q, default=True: default
    )
    setup_mod.setup_telemetry({"computer_use": {"cua_telemetry": True}})
    out = capsys.readouterr().out
    assert "opt-in" in out


def test_setup_telemetry_persistent_failure_degrades_to_env_line(
    monkeypatch, capsys
):
    """A failed persistent flip warns but keeps the per-invocation env line."""
    monkeypatch.setattr(_CUA_PERSISTENT_HELPER, lambda **kwargs: False)
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no", lambda q, default=True: default
    )
    setup_mod.setup_telemetry({})
    out = capsys.readouterr().out
    assert "Could not persistently disable" in out
    assert "on every cua-driver invocation" in out


# ---------------------------------------------------------------------------
# recommended client: FluffyChat (VM-feedback — Element X broken on the VM)
# ---------------------------------------------------------------------------


def test_card_renders_fluffychat_flow(capsys, tmp_path):
    """First-login card names FluffyChat with its generic add-account flow —
    no Element-specific labels anywhere in the output."""
    creds = _write_credentials(tmp_path)
    setup_mod._print_observatory_setup_card(
        {
            "owner_credentials_path": str(creds),
            "homeserver_url": HOMESERVER_URL,
            "e2ee": False,
        },
        dict(_TS_ABSENT),
    )
    out = capsys.readouterr().out
    assert "Matrix Observatory — first login (FluffyChat)" in out
    assert "in FluffyChat:" in out
    assert "add account" in out
    assert "homeserver URL" in out
    assert "Element X" not in out
    assert "Element Classic" not in out
    assert "Use account instead" not in out
    assert PASSWORD not in out


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("rel", [
    "hermes/mercury_cli/setup.py",
    "hermes/observatory/provision.py",
    "hermes/observatory/e2ee.py",
    "website/docs/user-guide/messaging/matrix-observatory.md",
])
def test_no_user_facing_string_recommends_element(rel):
    """Source guard: Element X / Element Classic must not be named as the
    recommendation in any user-facing string (FluffyChat is)."""
    text = (_repo_root() / rel).read_text(encoding="utf-8")
    assert "Element X" not in text
    assert "Element Classic" not in text


def test_user_guide_recommends_fluffychat():
    """The user guide names FluffyChat the tested recommendation and keeps
    the honest line that other clients work for reading."""
    text = (
        _repo_root() / "website/docs/user-guide/messaging/matrix-observatory.md"
    ).read_text(encoding="utf-8")
    assert "FluffyChat" in text
    assert "Other" in text and "work for reading" in text
    assert "First login with FluffyChat" in text
