"""The Lounge provisioner: config render, paths, user flow (mocked)."""

from __future__ import annotations

from observatory import lounge as lounge_mod


def test_render_lounge_config() -> None:
    conf = lounge_mod.render_lounge_config(host="100.9.9.9", port=9000)
    assert 'host: "100.9.9.9"' in conf
    assert "port: 9000" in conf
    assert "public: false" in conf
    assert "enable: true" in conf
    assert "maxFileSize: -1" in conf


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


def test_ensure_lounge_installed_uses_prefix_and_ignore_scripts(
        tmp_path, monkeypatch) -> None:
    import types as _types
    from observatory import lounge as lounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    seen = {}

    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        (home / "observatory" / "lounge" / "npm" / "bin").mkdir(
            parents=True)
        (home / "observatory" / "lounge" / "npm" / "bin"
         / "thelounge").write_text("#!/bin/sh\n")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lounge_mod, "_run", _fake_run)
    out = lounge_mod.ensure_lounge_installed()
    assert "--ignore-scripts" in seen["args"]
    assert "--prefix" in seen["args"]
    assert out.endswith("npm/bin/thelounge")
    # prefix-first resolution on the next call (no install attempted)
    monkeypatch.setattr(
        lounge_mod, "_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not reinstall")))
    assert lounge_mod.ensure_lounge_installed() == out


def test_ensure_lounge_user_creates_users_dir(tmp_path) -> None:
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    try:
        lounge_mod.ensure_lounge_user(paths, "owner", "pw")
    except lounge_mod.LoungeError:
        pass
    assert (paths.home / "users").is_dir()


def test_render_lounge_unit_carries_path() -> None:
    from observatory import lounge as lounge_mod

    unit = lounge_mod.render_lounge_unit(
        lounge_bin="/b/thelounge", home="/h", path_extra="/p/bin")
    assert "Environment=PATH=/p/bin:" in unit
    assert "ExecStart=/b/thelounge start" in unit
    assert "THELOUNGE_HOME=/h" in unit


def test_ensure_lounge_user_uses_password_flag(tmp_path, monkeypatch) -> None:
    import json as _json
    import subprocess as _subprocess
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    seen = {}

    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        (paths.home / "users" / "owner.json").write_text(_json.dumps({}))
        import types as _types
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    monkeypatch.setattr(lounge_mod, "lounge_bin", lambda: "/bin/thelounge")
    out = lounge_mod.ensure_lounge_user(paths, "owner", "pw123456")
    assert out == {"action": "created"}
    assert "--password" in seen["args"]
    assert "pw123456" in seen["args"]


def test_provision_applies_password_to_existing_user(
        tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import lounge as lounge_mod

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "mercury"))
    for name in ("ensure_node", "ensure_lounge_installed"):
        monkeypatch.setattr(lounge_mod, name, lambda: "/bin/x")
    monkeypatch.setattr(lounge_mod, "ensure_lounge_config",
                        lambda *a, **k: {"action": "current"})
    users = lounge_mod.LoungePaths(
        tmp_path / "mercury").home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    monkeypatch.setattr(lounge_mod, "ensure_lounge_user",
                        lambda *a, **k: {"action": "current"})
    reset_to = []
    monkeypatch.setattr(
        lounge_mod, "reset_lounge_password",
        lambda paths, user, pw: reset_to.append((user, pw)) or {
            "action": "reset", "user": user})
    monkeypatch.setattr(lounge_mod, "ensure_lounge_unit",
                        lambda *a, **k: "installed")
    monkeypatch.setattr(lounge_mod, "lounge_unit_active", lambda: False)
    out = lounge_mod.provision_lounge(username="owner", password="newpw")
    assert reset_to == [("owner", "newpw")]
    assert out["user"]["action"] == "reset"
    # no password given: existing login untouched
    reset_to.clear()
    out = lounge_mod.provision_lounge(username="owner", password=None)
    assert reset_to == []
    assert out["user"]["action"] == "current"


def test_ensure_non_prepare_failure_raises(tmp_path, monkeypatch) -> None:
    import types as _types
    from observatory import lounge as lounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setattr(
        lounge_mod, "_run",
        lambda *a, **k: _types.SimpleNamespace(
            returncode=1, stdout="", stderr="404 not found"))
    import pytest

    with pytest.raises(lounge_mod.LoungeError, match="404"):
        lounge_mod.ensure_lounge_installed()


def test_ensure_slow_path_links_patched_tree(tmp_path, monkeypatch) -> None:
    import types as _types
    from observatory import lounge as lounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))

    def _fake_run(args, **kwargs):
        return _types.SimpleNamespace(
            returncode=1, stdout="",
            stderr="git dep preparation failed")

    def _fake_patch(npm, path, tmp, mercury_home=None):
        final = (home / "observatory" / "lounge" / "pkg")
        final.mkdir(parents=True)
        (final / "index.js").write_text("#!/usr/bin/env node\n")
        (final / "package.json").write_text("{}")
        return final

    monkeypatch.setattr(lounge_mod, "_run", _fake_run)
    monkeypatch.setattr(lounge_mod, "_patched_lounge_tree", _fake_patch)
    out = lounge_mod.ensure_lounge_installed()
    assert out.endswith("npm/bin/thelounge")
    import os as _os

    assert _os.path.realpath(out).endswith("lounge/pkg/index.js")


def test_ensure_lounge_network_current_when_identical(tmp_path) -> None:
    import json as _json
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": [{
        "name": "vm", "host": "100.9.9.9", "port": 6670,
        "password": "pw", "nick": "owner", "username": "owner",
        "channels": [{"name": "#vm_gateway", "muted": False,
                      "key": ""}]}]}))
    out = lounge_mod.ensure_lounge_network(
        paths, "owner", net_name="vm", host="100.9.9.9", port=6670,
        server_password="pw", nick="owner", channel="#vm_gateway")
    assert out["action"] == "current"


_SNIPPET = "x.splice(e.index||-1,0,n),e.chan.type===`query`&&!e.shouldOpen)return;y()"


def test_frontend_patch_respects_should_open() -> None:
    from observatory import lounge as lounge_mod

    assert lounge_mod.FRONTEND_JOIN_OPEN in _SNIPPET
    patched, changed = lounge_mod.patch_lounge_frontend_text(_SNIPPET)
    assert changed is True
    assert lounge_mod.FRONTEND_JOIN_OPEN not in patched
    assert patched.endswith("!e.shouldOpen)return;y()")


def test_frontend_patch_idempotent_and_drift(tmp_path) -> None:
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    assets = paths.dir / "pkg" / "public" / "assets"
    assets.mkdir(parents=True)
    bundle = assets / "index-abc123.js"
    bundle.write_text("var a=1;" + _SNIPPET)
    first = lounge_mod.patch_lounge_frontend(paths)
    assert first["action"] == "patched"
    assert lounge_mod.patch_lounge_frontend(paths)["action"] == "current"
    assert lounge_mod.FRONTEND_JOIN_OPEN not in bundle.read_text()


def test_frontend_patch_drift_is_loud(tmp_path) -> None:
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    assets = paths.dir / "pkg" / "public" / "assets"
    assets.mkdir(parents=True)
    (assets / "index-zzz.js").write_text("var a=1;")
    assert lounge_mod.patch_lounge_frontend(paths)["action"] == "pattern-missing"


def test_install_pins_lounge_version(tmp_path, monkeypatch) -> None:
    import types as _types
    from observatory import lounge as lounge_mod

    assert lounge_mod.LOUNGE_VERSION == "4.5.2"
    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    seen = {}

    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        (home / "observatory" / "lounge" / "npm" / "bin").mkdir(parents=True)
        (home / "observatory" / "lounge" / "npm" / "bin"
         / "thelounge").write_text("#!/bin/sh\n")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lounge_mod, "_run", _fake_run)
    lounge_mod.ensure_lounge_installed()
    assert "thelounge@4.5.2" in seen["args"]


def test_conf_lives_in_lounge_home(tmp_path) -> None:
    """config.js must be $THELOUNGE_HOME/config.js — the only file read."""
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    assert paths.conf == paths.home / "config.js"


def test_ensure_removes_stale_dir_config(tmp_path) -> None:
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    paths.dir.mkdir(parents=True)
    (paths.dir / "config.js").write_text("// stale pre-0.131 location\n")
    out = lounge_mod.ensure_lounge_config(paths, host="127.0.0.1", port=9000)
    assert out["action"] == "wrote"
    assert not (paths.dir / "config.js").exists()
    live = (paths.home / "config.js").read_text()
    assert "fileUpload" in live
    assert lounge_mod.ensure_lounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "current"


def test_stage_upload_happy_path(tmp_path, monkeypatch) -> None:
    from observatory import lounge as lounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-1.4 data")
    out = lounge_mod.stage_lounge_upload(home, src)
    assert out["url_path"].startswith("uploads/")
    assert out["url_path"].endswith("/report.pdf")
    assert out["filename"] == "report.pdf"
    token = out["url_path"].split("/")[1]
    stored = (home / "observatory" / "lounge" / "home" / "uploads"
              / token[:2] / token)
    assert stored.read_bytes() == b"%PDF-1.4 data"


def test_stage_upload_refusals(tmp_path, monkeypatch) -> None:
    import pytest
    from observatory import lounge as lounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.stage_lounge_upload(home, tmp_path / "missing.txt")
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.stage_lounge_upload(home, "/etc/hostname")
    key = tmp_path / "id_rsa.key"
    key.write_text("x")
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.stage_lounge_upload(home, key)
    d = tmp_path / "sub"
    d.mkdir()
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.stage_lounge_upload(home, d)
