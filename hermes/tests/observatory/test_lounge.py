"""The Lounge provisioner: config render, paths, user flow (mocked)."""

from __future__ import annotations

from observatory import lounge as lounge_mod


def test_render_lounge_config() -> None:
    conf = lounge_mod.render_lounge_config(host="100.9.9.9", port=9000)
    assert 'host: "100.9.9.9"' in conf
    assert "port: 9000" in conf
    assert "public: false" in conf


def test_ensure_lounge_config_idempotent(tmp_path) -> None:
    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    assert lounge_mod.ensure_lounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "wrote"
    assert lounge_mod.ensure_lounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "current"
    assert lounge_mod.ensure_lounge_config(
        paths, host="100.9.9.9", port=9000)["action"] == "updated"


def test_ensure_lounge_user_current_when_present(tmp_path) -> None:
    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text("{}")
    assert lounge_mod.ensure_lounge_user(paths, "owner", None) == {
        "action": "current"}


def test_ensure_lounge_user_needs_password(tmp_path) -> None:
    import pytest

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.ensure_lounge_user(paths, "owner", None)


def test_status_reports_bind(tmp_path) -> None:
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    lounge_mod.ensure_lounge_config(paths, host="100.9.9.9", port=9000)
    st = lounge_mod.status_lounge(tmp_path / "mercury")
    assert st["configured"] is True
    assert st["host"] == "100.9.9.9"
    assert st["port"] == 9000


def test_status_external_when_port_answers(tmp_path, monkeypatch) -> None:
    from observatory import lounge as lounge_mod

    monkeypatch.setattr(lounge_mod, "lounge_port_open",
                        lambda *a, **k: True)
    st = lounge_mod.status_lounge(tmp_path / "mercury")
    assert st["configured"] is False
    assert st["external"] is True


def test_lounge_port_open_closed() -> None:
    from observatory import lounge as lounge_mod

    assert lounge_mod.lounge_port_open("127.0.0.1", 1) is False


def test_local_port_answers_loopback() -> None:
    import asyncio as _asyncio
    from observatory import lounge as lounge_mod

    async def _probe() -> None:
        srv = await _asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            assert lounge_mod._local_port_answers(port) is True
        finally:
            srv.close()
        assert lounge_mod._local_port_answers(port) is False

    _asyncio.run(_probe())


def test_ensure_lounge_network_seeds_and_replaces(tmp_path) -> None:
    import json as _json
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps(
        {"networks": [{"name": "vm", "host": "old"}]}))
    out = lounge_mod.ensure_lounge_network(
        paths, "owner", net_name="vm", host="127.0.0.1", port=6670,
        server_password="pw", nick="owner", channel="#vm_gateway")
    assert out["action"] == "seeded"
    data = _json.loads((users / "owner.json").read_text())
    assert len(data["networks"]) == 1
    net = data["networks"][0]
    assert net["host"] == "127.0.0.1"
    assert net["port"] == 6670
    assert net["password"] == "pw"
    assert net["channels"] == [{"name": "#vm_gateway", "muted": False,
                                "key": ""}]
    assert net["tls"] is False
    assert net["uuid"]


def test_ensure_lounge_network_skipped_without_password(tmp_path) -> None:
    import json as _json
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    out = lounge_mod.ensure_lounge_network(
        paths, "owner", net_name="vm", host="127.0.0.1", port=6670,
        server_password="", nick="owner", channel="#vm_gateway")
    assert out["action"] == "skipped"


def test_ensure_node_fails_loudly(monkeypatch) -> None:
    import shutil as _shutil
    from observatory import lounge as lounge_mod

    monkeypatch.setattr(_shutil, "which", lambda *a, **k: None)
    import types as _types
    monkeypatch.setattr(
        lounge_mod, "_run",
        lambda *a, **k: _types.SimpleNamespace(returncode=1, stdout="",
                                               stderr="no"))
    import pytest

    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.ensure_node()


def test_provision_lounge_seeds_network_and_restarts(
        tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import lounge as lounge_mod

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "mercury"))
    monkeypatch.setattr(lounge_mod, "ensure_node", lambda: "/bin/node")
    monkeypatch.setattr(lounge_mod, "ensure_lounge_installed",
                        lambda: "/bin/thelounge")
    monkeypatch.setattr(lounge_mod, "ensure_lounge_unit",
                        lambda *a, **k: "installed")
    users = lounge_mod.LoungePaths(
        tmp_path / "mercury").home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    monkeypatch.setattr(lounge_mod, "ensure_lounge_user",
                        lambda *a, **k: {"action": "current"})
    monkeypatch.setattr(lounge_mod, "lounge_unit_active", lambda: True)
    restarted = []
    monkeypatch.setattr(lounge_mod, "restart_lounge",
                        lambda: restarted.append(True))
    out = lounge_mod.provision_lounge(
        username="owner", uplink_name="vm", uplink_channel="#vm_gateway",
        uplink_password="pw", uplink_port=6670)
    assert out["network"]["action"] == "seeded"
    assert restarted == [True]
    data = _json.loads((users / "owner.json").read_text())
    assert data["networks"][0]["name"] == "vm"


def test_reset_lounge_password_missing_user(tmp_path) -> None:
    import pytest
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.reset_lounge_password(paths, "ghost", "pw")


def test_reset_lounge_password_runs_cli(tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    seen = {}

    import types as _types
    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen["env"] = (kwargs.get("extra_env") or {}).get("THELOUNGE_HOME")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lounge_mod, "lounge_bin", lambda: "/bin/thelounge")
    monkeypatch.setattr(lounge_mod, "_run", _fake_run)
    out = lounge_mod.reset_lounge_password(paths, "owner", "newpw")
    assert out == {"action": "reset", "user": "owner"}
    assert seen["args"][:3] == ["/bin/thelounge", "reset", "--password"]
    assert seen["args"][3:] == ["newpw", "owner"]
    assert seen["env"] == str(paths.home)
