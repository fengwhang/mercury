"""Provision tests: config idempotence, passwords, gateway row, bind, reset."""

from __future__ import annotations

import json

import pytest

from observatory import provision
from observatory.config_gen import ObservatoryPaths


def test_validate_server_name() -> None:
    assert provision.validate_server_name("Mercury-1") == "mercury-1"
    with pytest.raises(ValueError):
        provision.validate_server_name("has space!")
    with pytest.raises(ValueError):
        provision.validate_server_name("")


def test_validate_bouncer_password() -> None:
    assert provision.validate_bouncer_password("long-enough") == "long-enough"
    with pytest.raises(ValueError):
        provision.validate_bouncer_password("short")


def test_ensure_config_idempotent(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    paths = ObservatoryPaths(home)
    first = provision.ensure_config(paths, server_name="mercury")
    assert first["action"] == "wrote"
    second = provision.ensure_config(paths)
    assert second["action"] == "current"
    with pytest.raises(provision.ProvisionError):
        provision.ensure_config(paths, server_name="other")


def test_ensure_passwords_generates_once(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_BOUNCER_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    first = provision.ensure_passwords(home)
    assert first["action"] == "generated"
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "IRC_BOUNCER_PASSWORD=" in env_text
    assert "IRC_AGENT_PASSWORD=" in env_text
    monkeypatch.setenv("IRC_BOUNCER_PASSWORD", "x" * 16)
    monkeypatch.setenv("IRC_AGENT_PASSWORD", "y" * 16)
    second = provision.ensure_passwords(home)
    assert second["action"] == "current"


def test_provision_full_flow(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_BOUNCER_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    summary = provision.provision(home, server_name="mercury", systemd=False)
    assert summary["config"]["action"] == "wrote"
    assert summary["unit"] == "skipped (--no-systemd)"
    assert summary["gateway"] == "mercury_gateway"
    cfg = json.loads((home / "observatory" / "ircd.json").read_text(encoding="utf-8"))
    assert cfg["agent_port"] == 6669 and cfg["bouncer_port"] == 6670
    # gateway row carries the channel
    from observatory.state import ObservatoryState, default_state_db_path

    state = ObservatoryState(default_state_db_path(home))
    try:
        row = state.get("gw")
        assert row["room_id"] == "#mercury_gateway"
        assert row["extra"]["kind"] == "gateway"
    finally:
        state.close()


def test_set_ircd_bind(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    provision.provision(home, server_name="mercury", systemd=False)
    assert provision.set_ircd_bind("100.64.0.1", home) == "100.64.0.1"
    assert "100.64.0.1" in provision.current_listen_addresses(home)
    with pytest.raises(provision.ProvisionError):
        provision.set_ircd_bind("127.0.0.1", home)
    with pytest.raises(provision.ProvisionError):
        provision.set_ircd_bind("not-an-ip", home)


def test_status_summary_unprovisioned(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    summary = provision.status_summary(home)
    assert summary["provisioned"] is False
    assert summary["enabled"] is True


def test_reset_clears_data(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    provision.provision(home, server_name="mercury", systemd=False)
    removed = provision.reset_observatory_data(home)
    assert removed
    assert provision.live_server_name(home) is None
