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


def test_validate_server_password() -> None:
    assert provision.validate_server_password("long-enough") == "long-enough"
    with pytest.raises(ValueError):
        provision.validate_server_password("short")


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


def test_ensure_config_accepts_explicit_over_empty_file(tmp_path, monkeypatch) -> None:
    """A present-but-empty config (crashed first install) must not fail
    against the compiled-in defaults — there is nothing live to protect."""
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    paths = ObservatoryPaths(home)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("", encoding="utf-8")
    result = provision.ensure_config(paths, server_name="vm")
    assert result["action"] == "wrote"
    assert result["config"]["server_name"] == "vm"


def test_ensure_config_accepts_explicit_over_corrupt_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    paths = ObservatoryPaths(home)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("{not json", encoding="utf-8")
    result = provision.ensure_config(paths, server_name="vm")
    assert result["action"] == "wrote"
    assert result["config"]["server_name"] == "vm"


def test_ensure_passwords_generates_once(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_CLIENT_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_BOUNCER_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    first = provision.ensure_passwords(home)
    assert first["action"] == "generated"
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "IRC_CLIENT_PASSWORD=" in env_text
    assert "IRC_AGENT_PASSWORD=" in env_text
    assert "IRC_BOUNCER_PASSWORD=" not in env_text
    monkeypatch.setenv("IRC_CLIENT_PASSWORD", "x" * 16)
    monkeypatch.setenv("IRC_AGENT_PASSWORD", "y" * 16)
    second = provision.ensure_passwords(home)
    assert second["action"] == "current"


def test_provision_full_flow(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_CLIENT_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_BOUNCER_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    summary = provision.provision(home, server_name="mercury", systemd=False)
    assert summary["config"]["action"] == "wrote"
    assert summary["unit"] == "skipped (--no-systemd)"
    assert summary["gateway"] == "mercury_gateway"
    cfg = json.loads((home / "observatory" / "ircd.json").read_text(encoding="utf-8"))
    assert cfg["agent_port"] == 6669 and cfg["server_port"] == 6670
    assert "bouncer_port" not in cfg
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


def test_ensure_tls_cert_generates_once(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    first = provision.ensure_tls_cert(home)
    assert first["action"] == "generated"
    assert "localhost" in first["sans"]
    from observatory.config_gen import ObservatoryPaths

    paths = ObservatoryPaths(home)
    assert paths.tls_ca.is_file()
    assert paths.tls_cert.is_file()
    assert paths.tls_key.is_file()
    # idempotent: second run keeps the same CA (clients stay trusting)
    before = paths.tls_ca.read_bytes()
    second = provision.ensure_tls_cert(home)
    assert second["action"] == "current"
    assert paths.tls_ca.read_bytes() == before


def test_provision_flow_includes_tls(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_CLIENT_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_BOUNCER_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    summary = provision.provision(home, server_name="mercury", systemd=False)
    assert summary["tls"]["action"] in ("generated", "current")
    assert summary["config"]["config"]["tls_port"] == 6697


def test_set_server_password_keeps_agent(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    first = provision.provision(home, server_name="mercury", systemd=False)
    assert first["passwords"]["action"] == "generated"
    out = provision.set_server_password(home, "my-chosen-pw")
    assert out == {"action": "set", "agent": "kept"}
    have = provision.read_irc_passwords(home)
    assert have["server"] == "my-chosen-pw"
    assert have["agent"]  # untouched


def test_set_server_password_rejects_short(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    import pytest

    with pytest.raises(ValueError):
        provision.set_server_password(home, "short")


def test_provision_honors_chosen_password_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv(provision.ENV_CHOSEN_SERVER_PASSWORD, "env-chosen-pw")
    summary = provision.provision(home, server_name="mercury", systemd=False)
    assert summary["chosen_password"]["action"] == "set"
    assert provision.read_irc_passwords(home)["server"] == "env-chosen-pw"


def test_provision_rejects_bad_chosen_password_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv(provision.ENV_CHOSEN_SERVER_PASSWORD, "short")
    import pytest

    with pytest.raises(provision.ProvisionError):
        provision.provision(home, server_name="mercury", systemd=False)


def test_reset_wipes_lounge_fully_keeps_binary(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    obs = home / "observatory"
    obs.mkdir(parents=True)
    lounge = obs / "lounge"
    lounge_home = lounge / "home"
    (lounge_home / "users").mkdir(parents=True)
    (lounge_home / "users" / "owner.json").write_text("{}")
    (lounge / "config.js").write_text("module.exports = {};")
    npm_bin = lounge / "npm" / "bin"
    npm_bin.mkdir(parents=True)
    (npm_bin / "thelounge").write_text("#!/bin/sh\n")
    (obs / "state.db").write_text("tree")
    removed = provision.reset_observatory_data(home)
    assert not lounge_home.exists()
    assert not (lounge / "config.js").exists()
    assert (npm_bin / "thelounge").is_file()
    assert not (obs / "state.db").exists()
    assert any("lounge" in r for r in removed)


def test_reset_blanks_listener_passwords(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "OTHER=keep\nIRC_CLIENT_PASSWORD=new1\nIRC_BOUNCER_PASSWORD=old1\n"
        "IRC_AGENT_PASSWORD=old2\n")
    removed = provision.reset_observatory_data(home)
    rest = (home / ".env").read_text()
    assert "IRC_CLIENT_PASSWORD" not in rest
    assert "IRC_BOUNCER_PASSWORD" not in rest
    assert "IRC_AGENT_PASSWORD" not in rest
    assert "OTHER=keep" in rest
    assert ".env:IRC_CLIENT_PASSWORD" in removed
    # next provision generates fresh secrets
    made = provision.ensure_passwords(home)
    assert made["action"] == "generated"
    assert provision.read_irc_passwords(home)["server"]


def test_remove_legacy_soju(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    fake_home = tmp_path / "userhome"
    (fake_home / ".config" / "systemd" / "user").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    unit = fake_home / ".config" / "systemd" / "user" / "mercury-soju.service"
    unit.write_text("[Unit]")
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "soju.conf").write_text("listen")
    (obs / "soju.db").write_text("backlog")
    removed = provision.remove_legacy_soju(home)
    assert not unit.exists()
    assert not (obs / "soju.conf").exists()
    assert not (obs / "soju.db").exists()
    assert any("mercury-soju.service" in r for r in removed)


def test_status_summary_live_server_name(tmp_path, monkeypatch) -> None:
    import json as _json

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "ircd.json").write_text(_json.dumps({"server_name": "ace"}))
    assert provision.status_summary(home)["server_name"] == "ace"


def test_reset_stops_both_daemons(tmp_path, monkeypatch) -> None:
    import subprocess as _subprocess
    import types as _types

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    (home / "observatory").mkdir(parents=True)
    stopped = []

    def _fake_run(args, **kwargs):
        stopped.append(list(args)[-1])
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    removed = provision.reset_observatory_data(home)
    assert "mercury-observatory.service" in stopped
    assert "mercury-lounge.service" in stopped
    assert "stopped mercury-lounge.service" in removed


def test_restart_unchanged_is_current(monkeypatch) -> None:
    assert provision._restart_ircd_if_changed(
        config_action="current", passwords_made=[],
        tls_action="current") == {"action": "current"}


def test_restart_skipped_when_unit_down(monkeypatch) -> None:
    monkeypatch.setattr(provision, "_ircd_unit_active", lambda: False)
    assert provision._restart_ircd_if_changed(
        config_action="updated", passwords_made=[],
        tls_action="current") == {"action": "started-fresh"}


def test_restart_fires_on_password_regen(monkeypatch) -> None:
    import subprocess as _subprocess
    import types as _types

    monkeypatch.setattr(provision, "_ircd_unit_active", lambda: True)
    calls = []
    monkeypatch.setattr(
        _subprocess, "run",
        lambda args, **kw: calls.append(list(args)) or _types.SimpleNamespace(
            returncode=0, stdout=b"", stderr=b""))
    out = provision._restart_ircd_if_changed(
        config_action="current", passwords_made=["server"],
        tls_action="current")
    assert out == {"action": "restarted"}
    assert any(a[-1] == "daemon-reload" for a in calls)
    assert any("mercury-observatory.service" in a for a in calls)


def test_provision_reports_daemon_key(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    out = provision.provision(home, systemd=False)
    assert out["daemon"]["action"] in (
        "current", "started-fresh", "restarted", "skipped")
    assert out["passwords"]["action"] == "generated"


def test_passwords_prefer_dotenv_over_stale_environ(
        tmp_path, monkeypatch) -> None:
    """The file is daemon truth: a stale exported secret never wins."""
    import os as _os

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    home.mkdir(parents=True)
    (home / ".env").write_text("IRC_BOUNCER_PASSWORD=live-17\n")
    monkeypatch.setenv("IRC_BOUNCER_PASSWORD", "stale-32-char-password-here")
    assert (provision.read_irc_passwords(home)["server"]
            == "live-17")
    assert _os.environ["IRC_BOUNCER_PASSWORD"] == (
        "stale-32-char-password-here")


def test_mirror_syncs_environ(tmp_path, monkeypatch) -> None:
    import os as _os

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_CLIENT_PASSWORD", raising=False)
    monkeypatch.setenv("IRC_BOUNCER_PASSWORD", "stale-b")
    provision.mirror_irc_env(home, "fresh-b", "fresh-a")
    assert _os.environ["IRC_CLIENT_PASSWORD"] == "fresh-b"
    assert _os.environ["IRC_AGENT_PASSWORD"] == "fresh-a"
    assert "IRC_BOUNCER_PASSWORD" not in _os.environ


def test_reset_wipes_npm_cache(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    cache = home / "observatory" / "lounge" / "npm-cache"
    cache.mkdir(parents=True)
    (cache / "index").write_text("cached")
    provision.reset_observatory_data(home)
    assert not cache.exists()


def test_old_keys_honored_as_fallback(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.delenv("IRC_CLIENT_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_BOUNCER_PASSWORD", raising=False)
    monkeypatch.delenv("IRC_AGENT_PASSWORD", raising=False)
    home.mkdir(parents=True)
    (home / ".env").write_text("IRC_BOUNCER_PASSWORD=legacy16chars\n")
    assert (provision.read_irc_passwords(home)["server"]
            == "legacy16chars")
    obs = home / "observatory"
    obs.mkdir(parents=True)
    import json as _json
    (obs / "ircd.json").write_text(_json.dumps(
        {"server_name": "vm", "bouncer_host": "100.9.9.9",
         "bouncer_port": 6670}))
    assert (provision.server_key({"bouncer_host": "x"}, "server_host")
            == "x")
    assert (provision.server_key({"server_host": "y", "bouncer_host": "x"},
                                 "server_host") == "y")
    out = provision.provision(home, server_name="vm", systemd=False)
    cfg = _json.loads((obs / "ircd.json").read_text())
    assert cfg["server_host"] == "100.9.9.9"
    assert cfg["server_port"] == 6670
    assert "bouncer_host" not in cfg and "bouncer_port" not in cfg
    summary = provision.status_summary(home)
    assert summary["server"] == "100.9.9.9:6670"
    assert summary["server_password_set"] is True
    assert out["daemon"]["action"] in (
        "current", "started-fresh", "restarted", "skipped")


def test_mirror_repoints_autowired_bot_credential(tmp_path, monkeypatch) -> None:
    """Rotates must reach the gateway adapter's saved credential: a bot
    authenticating with the previous agent password gets silent 464s
    (running gateway, invisible unresponsive bot)."""
    import os as _os

    home = tmp_path / "mercury"
    home.mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    envf = home / ".env"
    envf.write_text(
        "IRC_CLIENT_PASSWORD=old-b\nIRC_AGENT_PASSWORD=old-a\n"
        "IRC_SERVER_PASSWORD=old-a\n")
    provision.mirror_irc_env(home, "new-b", "new-a")
    text = envf.read_text()
    assert "IRC_SERVER_PASSWORD=new-a" in text
    assert _os.environ["IRC_SERVER_PASSWORD"] == "new-a"


def test_mirror_preserves_hand_customized_bot_credential(tmp_path, monkeypatch) -> None:
    home = tmp_path / "mercury"
    home.mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    envf = home / ".env"
    envf.write_text(
        "IRC_CLIENT_PASSWORD=old-b\nIRC_AGENT_PASSWORD=old-a\n"
        "IRC_SERVER_PASSWORD=hand-set\n")
    provision.mirror_irc_env(home, "new-b", "new-a")
    assert "IRC_SERVER_PASSWORD=hand-set" in envf.read_text()


class _FakeProc:
    returncode = 0
    stdout = b""
    stderr = b""


def test_restart_daemon_force_restarts_without_config_change(monkeypatch) -> None:
    """A code update is invisible to the change gate; force must bounce."""
    calls: list[list[str]] = []
    monkeypatch.setattr(provision, "_ircd_unit_active", lambda: True)
    monkeypatch.setattr(
        provision.subprocess, "run",
        lambda *a, **k: calls.append(list(a[0])) or _FakeProc())
    summary = provision.restart_daemon(force=True)
    assert summary == {"action": "restarted"}
    assert any("restart" in c for c in calls)


def test_restart_gate_stays_closed_without_force(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(provision, "_ircd_unit_active", lambda: True)
    monkeypatch.setattr(
        provision.subprocess, "run",
        lambda *a, **k: calls.append(list(a[0])) or _FakeProc())
    summary = provision._restart_ircd_if_changed(
        config_action="", passwords_made=[], tls_action="")
    assert summary == {"action": "current"}
    assert calls == []
