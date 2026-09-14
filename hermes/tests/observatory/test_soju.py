"""soju bouncer frontend tests (renders pure, flow mocked)."""

from __future__ import annotations

import json
import subprocess

import pytest

from observatory import soju as soju_mod


def _home(tmp_path, monkeypatch):
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    return home


def test_render_soju_conf() -> None:
    conf = soju_mod.render_soju_conf(
        bouncer_host="100.86.76.11", bouncer_port=6670, tls_port=6697,
        tls_cert="/h/observatory/tls/server.crt",
        tls_key="/h/observatory/tls/server.key",
        server_name="vm", db_path="/h/observatory/soju.db",
        admin_sock="/h/observatory/soju-admin")
    assert "listen irc+insecure://100.86.76.11:6670" in conf
    assert "listen ircs://100.86.76.11:6697" in conf
    assert "listen unix+admin:///h/observatory/soju-admin" in conf
    assert "hostname vm" in conf
    assert "tls /h/observatory/tls/server.crt /h/observatory/tls/server.key" in conf
    assert "db sqlite3 /h/observatory/soju.db" in conf


def test_render_soju_unit() -> None:
    unit = soju_mod.render_soju_unit(
        soju_bin="/x/soju-amd64", config_path="/h/observatory/soju.conf")
    assert "ExecStart=/x/soju-amd64 -config /h/observatory/soju.conf" in unit
    assert "mercury-observatory.service" in unit  # start ordering


def test_soju_bin_missing_raises(tmp_path) -> None:
    with pytest.raises(soju_mod.SojuError):
        soju_mod.soju_bin("soju", root=tmp_path)


def test_soju_bin_resolves_arch(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "observatory" / "soju-binaries"
    bindir.mkdir(parents=True)
    (bindir / "soju-amd64").write_text("x")
    monkeypatch.setattr(soju_mod, "soju_arch", lambda: "amd64")
    assert soju_mod.soju_bin("soju", root=tmp_path).name == "soju-amd64"


def test_set_soju_front_flips_once(tmp_path, monkeypatch) -> None:
    home = _home(tmp_path, monkeypatch)
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "ircd.json").write_text(json.dumps({"server_name": "vm"}))
    assert soju_mod.set_soju_front(home, True) is True
    assert json.loads((obs / "ircd.json").read_text())["soju_front"] is True
    assert soju_mod.set_soju_front(home, True) is False
    assert soju_mod.set_soju_front(home, False) is True


def _ok(out=""):
    return subprocess.CompletedProcess([], 0, out, "")


def test_ensure_soju_user_create_and_change(monkeypatch) -> None:
    import types

    monkeypatch.setattr(soju_mod, "soju_bin", lambda *a, **k: "/bin/soju")

    calls = []

    def fake_run(args, *, input_text=None, timeout=60):
        calls.append((args, input_text))
        if "user" in args and "status" in args:
            return subprocess.CompletedProcess([], 1, "", "no such user")
        return _ok()

    monkeypatch.setattr(soju_mod, "_run", fake_run)
    paths = types.SimpleNamespace(conf="/c/soju.conf")
    assert soju_mod.ensure_soju_user(paths, "owner", "pw12345678") == {"action": "created"}
    create = [c for c in calls if "create-user" in c[0]]
    assert len(create) == 1 and create[0][1] == "pw12345678\n"
    # Existing user + new password -> change-password (never argv).
    calls.clear()

    def fake_run2(args, *, input_text=None, timeout=60):
        calls.append((args, input_text))
        return _ok("owner (admin): 1 networks")

    monkeypatch.setattr(soju_mod, "_run", fake_run2)
    assert soju_mod.ensure_soju_user(paths, "owner", "newpassword") == {
        "action": "password-changed"}
    assert any("change-password" in c[0] for c in calls)
    assert all("newpassword" not in " ".join(c[0]) for c in calls)


def test_ensure_soju_network_create_then_update(monkeypatch) -> None:
    import types

    monkeypatch.setattr(soju_mod, "soju_bin", lambda *a, **k: "/bin/soju")

    paths = types.SimpleNamespace(conf="/c/soju.conf")
    seen = []

    def fake_empty(args, *, input_text=None, timeout=60):
        seen.append(args)
        return _ok("")

    monkeypatch.setattr(soju_mod, "_run", fake_empty)
    out = soju_mod.ensure_soju_network(
        paths, name="vm", addr="irc+insecure://127.0.0.1:6670",
        nick="owner", username="owner", password="up")
    assert out == {"action": "created"}
    assert any("create" in a for a in seen)

    def fake_full(args, *, input_text=None, timeout=60):
        if "status" in args:
            return _ok('vm (irc+insecure://127.0.0.1:6670) [connected as owner]: 1 channels\n')
        seen.append(args)
        return _ok()

    monkeypatch.setattr(soju_mod, "_run", fake_full)
    out = soju_mod.ensure_soju_network(
        paths, name="vm", addr="irc+insecure://127.0.0.1:6670",
        nick="owner", username="owner", password="up")
    assert out == {"action": "converged"}
    assert any("update" in a for a in seen)


def test_resolver_soju_front_binds_localhost(tmp_path) -> None:
    from observatory.ircd import _resolve_daemon_config
    import types

    cfg = {"server_name": "vm", "bouncer_host": "100.86.76.11",
           "bouncer_port": 6670, "soju_front": True}
    (tmp_path / "ircd.json").write_text(json.dumps(cfg))
    args = types.SimpleNamespace(
        host=None, agent_port=None, bouncer_host=None, bouncer_port=None,
        tls_port=None, password=None, agent_password=None,
        history_limit=None, tls_cert=None, tls_key=None,
        state_dir="", config=str(tmp_path / "ircd.json"))
    resolved = _resolve_daemon_config(args)
    assert resolved.bouncer_host == "127.0.0.1"
    assert resolved.bouncer_port == 6670  # ports unchanged


def test_provision_soju_flow(tmp_path, monkeypatch) -> None:
    """Full layer with mocked runners (catches wiring bugs like #63)."""
    home = _home(tmp_path, monkeypatch)
    obs = home / "observatory"
    (obs / "tls").mkdir(parents=True)
    (obs / "tls" / "server.crt").write_text("c")
    (obs / "tls" / "server.key").write_text("k")
    (obs / "ircd.json").write_text(json.dumps({
        "server_name": "vm", "bouncer_host": "127.0.0.1",
        "bouncer_port": 6670, "tls_port": 6697}))
    (home / ".env").write_text(
        "IRC_BOUNCER_PASSWORD=downstream-pw-123\nIRC_AGENT_PASSWORD=x\n")
    monkeypatch.setattr(soju_mod, "soju_bin", lambda *a, **k: "/bin/soju")
    monkeypatch.setattr(
        soju_mod, "_run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(
        soju_mod, "ensure_soju_config", lambda *a, **k: {"action": "wrote"})
    monkeypatch.setattr(soju_mod, "ensure_soju_unit", lambda *a, **k: "installed")
    monkeypatch.setattr(
        soju_mod, "ensure_soju_user", lambda *a, **k: {"action": "created"})
    monkeypatch.setattr(
        soju_mod, "ensure_soju_network", lambda *a, **k: {"action": "created"})
    monkeypatch.setattr(soju_mod, "_systemctl_available", lambda: False)
    summary = soju_mod.provision_soju(str(home))
    assert summary["user"] == {"action": "created"}
    assert summary["network"] == {"action": "created"}
    assert "soju_front" in json.loads((obs / "ircd.json").read_text())


def test_ensure_soju_channel_subscribes_once(monkeypatch) -> None:
    import types

    saved = {"out": ""}

    def fake_run(args, *, input_text=None, timeout=60):
        if "status" in args:
            return subprocess.CompletedProcess([], 0, saved["out"], "")
        saved["out"] = "#vm_gateway \n"
        return subprocess.CompletedProcess([], 0, "created", "")

    monkeypatch.setattr(soju_mod, "_run", fake_run)
    monkeypatch.setattr(soju_mod, "soju_bin", lambda *a, **k: "/bin/soju")
    paths = types.SimpleNamespace(conf="/c/soju.conf")
    assert soju_mod.ensure_soju_channel(paths, "#vm_gateway") == {
        "action": "subscribed"}
    assert soju_mod.ensure_soju_channel(paths, "#vm_gateway") == {
        "action": "current"}
    with pytest.raises(soju_mod.SojuError):
        soju_mod.ensure_soju_channel(paths, "not-a-channel")


def test_live_network_reads_mercury_home(tmp_path, monkeypatch) -> None:
    """_live_network resolves ircd.json under the mercury home itself."""
    import json as _json

    home = _home(tmp_path, monkeypatch)
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "ircd.json").write_text(_json.dumps({"server_name": "vm"}))
    spaths = soju_mod.SojuPaths(home)
    assert soju_mod._live_network(spaths) == "vm"
