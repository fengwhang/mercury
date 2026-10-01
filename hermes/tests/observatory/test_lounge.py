"""The Lounge provisioner: config render, paths, user flow (mocked)."""

from __future__ import annotations

from observatory import mlounge as mlounge_mod


def test_render_mlounge_config() -> None:
    conf = mlounge_mod.render_mlounge_config(host="100.9.9.9", port=9000)
    assert 'host: "100.9.9.9"' in conf
    assert "port: 9000" in conf
    assert "public: false" in conf
    assert "enable: true" in conf
    assert "maxFileSize: -1" in conf


def test_ensure_mlounge_config_idempotent(tmp_path) -> None:
    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    assert mlounge_mod.ensure_mlounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "wrote"
    assert mlounge_mod.ensure_mlounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "current"
    assert mlounge_mod.ensure_mlounge_config(
        paths, host="100.9.9.9", port=9000)["action"] == "updated"


def test_ensure_mlounge_user_current_when_present(tmp_path) -> None:
    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text("{}")
    assert mlounge_mod.ensure_mlounge_user(paths, "owner", None) == {
        "action": "current"}


def test_ensure_mlounge_user_needs_password(tmp_path) -> None:
    import pytest

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.ensure_mlounge_user(paths, "owner", None)


def test_status_reports_bind(tmp_path) -> None:
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    mlounge_mod.ensure_mlounge_config(paths, host="100.9.9.9", port=9000)
    st = mlounge_mod.status_mlounge(tmp_path / "mercury")
    assert st["configured"] is True
    assert st["host"] == "100.9.9.9"
    assert st["port"] == 9000


def test_status_external_when_port_answers(tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(mlounge_mod, "mlounge_port_open",
                        lambda *a, **k: True)
    st = mlounge_mod.status_mlounge(tmp_path / "mercury")
    assert st["configured"] is False
    assert st["external"] is True


def test_mlounge_port_open_closed() -> None:
    from observatory import mlounge as mlounge_mod

    assert mlounge_mod.mlounge_port_open("127.0.0.1", 1) is False


def test_local_port_answers_loopback() -> None:
    import asyncio as _asyncio
    from observatory import mlounge as mlounge_mod

    async def _probe() -> None:
        srv = await _asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            assert mlounge_mod._local_port_answers(port) is True
        finally:
            srv.close()
        assert mlounge_mod._local_port_answers(port) is False

    _asyncio.run(_probe())


def test_ensure_mlounge_network_seeds_and_replaces(tmp_path) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps(
        {"networks": [{"name": "vm", "host": "old"}]}))
    out = mlounge_mod.ensure_mlounge_network(
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


def test_ensure_mlounge_network_skipped_without_password(tmp_path) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    out = mlounge_mod.ensure_mlounge_network(
        paths, "owner", net_name="vm", host="127.0.0.1", port=6670,
        server_password="", nick="owner", channel="#vm_gateway")
    assert out["action"] == "skipped"


def test_ensure_node_fails_loudly(monkeypatch) -> None:
    import shutil as _shutil
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(_shutil, "which", lambda *a, **k: None)
    import types as _types
    monkeypatch.setattr(
        mlounge_mod, "_run",
        lambda *a, **k: _types.SimpleNamespace(returncode=1, stdout="",
                                               stderr="no"))
    import pytest

    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.ensure_node()


def test_provision_mlounge_seeds_network_and_restarts(
        tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "mercury"))
    monkeypatch.setattr(mlounge_mod, "ensure_node", lambda: "/bin/node")
    monkeypatch.setattr(mlounge_mod, "ensure_mlounge_installed",
                        lambda: "/bin/thelounge")
    monkeypatch.setattr(mlounge_mod, "ensure_mlounge_unit",
                        lambda *a, **k: "installed")
    users = mlounge_mod.MLoungePaths(
        tmp_path / "mercury").home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    monkeypatch.setattr(mlounge_mod, "ensure_mlounge_user",
                        lambda *a, **k: {"action": "current"})
    monkeypatch.setattr(mlounge_mod, "mlounge_unit_active", lambda: True)
    restarted = []
    monkeypatch.setattr(mlounge_mod, "restart_mlounge",
                        lambda: restarted.append(True))
    out = mlounge_mod.provision_mlounge(
        username="owner", uplink_name="vm", uplink_channel="#vm_gateway",
        uplink_password="pw", uplink_port=6670)
    assert out["network"]["action"] == "seeded"
    assert restarted == [True]
    data = _json.loads((users / "owner.json").read_text())
    assert data["networks"][0]["name"] == "vm"


def test_reset_mlounge_password_missing_user(tmp_path) -> None:
    import pytest
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.reset_mlounge_password(paths, "ghost", "pw")


def test_reset_mlounge_password_runs_cli(tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    seen = {}

    import types as _types
    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        seen["env"] = (kwargs.get("extra_env") or {}).get("THELOUNGE_HOME")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mlounge_mod, "mlounge_bin", lambda: "/bin/thelounge")
    monkeypatch.setattr(mlounge_mod, "_run", _fake_run)
    out = mlounge_mod.reset_mlounge_password(paths, "owner", "newpw")
    assert out == {"action": "reset", "user": "owner"}
    assert seen["args"][:3] == ["/bin/thelounge", "reset", "--password"]
    assert seen["args"][3:] == ["newpw", "owner"]
    assert seen["env"] == str(paths.home)


def test_ensure_mlounge_installed_uses_shipped_fork(
        tmp_path, monkeypatch) -> None:
    import json as _json
    import types as _types
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    shipped = tmp_path / "fork"
    (shipped / "dist" / "server").mkdir(parents=True)
    (shipped / "package.json").write_text(_json.dumps(
        {"version": "4.5.2-mercury.9"}))
    (shipped / "dist" / "server" / "index.js").write_text("// built\n")
    (shipped / "index.js").write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(
        mlounge_mod, "_fork_source_tree", lambda: shipped)
    seen = {}

    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        assert not any("thelounge@" in str(a) for a in seen["args"])
        final = home / "observatory" / "lounge" / "pkg"
        (final / "node_modules" / "irc-framework").mkdir(parents=True)
        (home / "observatory" / "lounge" / "npm" / "bin").mkdir(
            parents=True)
        (home / "observatory" / "lounge" / "npm" / "bin"
         / "thelounge").write_text("#!/bin/sh\n")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mlounge_mod, "_run", _fake_run)
    out = mlounge_mod.ensure_mlounge_installed()
    assert "--omit=dev" in seen["args"]
    assert "--prefix" in seen["args"]
    assert out.endswith("npm/bin/mlounge")
    # prefix-first resolution on the next call (no install attempted)
    monkeypatch.setattr(
        mlounge_mod, "_run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not reinstall")))
    assert mlounge_mod.ensure_mlounge_installed() == out
def test_ensure_mlounge_user_creates_users_dir(tmp_path) -> None:
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    try:
        mlounge_mod.ensure_mlounge_user(paths, "owner", "pw")
    except mlounge_mod.MLoungeError:
        pass
    assert (paths.home / "users").is_dir()


def test_render_mlounge_unit_carries_path() -> None:
    from observatory import mlounge as mlounge_mod

    unit = mlounge_mod.render_mlounge_unit(
        mlounge_bin="/b/thelounge", home="/h", path_extra="/p/bin")
    assert "Environment=PATH=/p/bin:" in unit
    assert "ExecStart=/b/thelounge start" in unit
    assert "THELOUNGE_HOME=/h" in unit


def test_ensure_mlounge_user_uses_password_flag(tmp_path, monkeypatch) -> None:
    import json as _json
    import subprocess as _subprocess
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    seen = {}

    def _fake_run(args, **kwargs):
        seen["args"] = list(args)
        (paths.home / "users" / "owner.json").write_text(_json.dumps({}))
        import types as _types
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run", _fake_run)
    monkeypatch.setattr(mlounge_mod, "mlounge_bin", lambda: "/bin/thelounge")
    out = mlounge_mod.ensure_mlounge_user(paths, "owner", "pw123456")
    assert out == {"action": "created"}
    assert "--password" in seen["args"]
    assert "pw123456" in seen["args"]


def test_provision_applies_password_to_existing_user(
        tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "mercury"))
    for name in ("ensure_node", "ensure_mlounge_installed"):
        monkeypatch.setattr(mlounge_mod, name, lambda: "/bin/x")
    monkeypatch.setattr(mlounge_mod, "ensure_mlounge_config",
                        lambda *a, **k: {"action": "current"})
    users = mlounge_mod.MLoungePaths(
        tmp_path / "mercury").home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": []}))
    monkeypatch.setattr(mlounge_mod, "ensure_mlounge_user",
                        lambda *a, **k: {"action": "current"})
    reset_to = []
    monkeypatch.setattr(
        mlounge_mod, "reset_mlounge_password",
        lambda paths, user, pw: reset_to.append((user, pw)) or {
            "action": "reset", "user": user})
    monkeypatch.setattr(mlounge_mod, "ensure_mlounge_unit",
                        lambda *a, **k: "installed")
    monkeypatch.setattr(mlounge_mod, "mlounge_unit_active", lambda: False)
    out = mlounge_mod.provision_mlounge(username="owner", password="newpw")
    assert reset_to == [("owner", "newpw")]
    assert out["user"]["action"] == "reset"
    # no password given: existing login untouched
    reset_to.clear()
    out = mlounge_mod.provision_mlounge(username="owner", password=None)
    assert reset_to == []
    assert out["user"]["action"] == "current"


def test_ensure_fork_install_failure_raises(tmp_path, monkeypatch) -> None:
    import json as _json
    import types as _types
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    shipped = tmp_path / "fork"
    (shipped / "dist" / "server").mkdir(parents=True)
    (shipped / "package.json").write_text(_json.dumps(
        {"version": "4.5.2-mercury.9"}))
    (shipped / "dist" / "server" / "index.js").write_text("// built\n")
    monkeypatch.setattr(
        mlounge_mod, "_fork_source_tree", lambda: shipped)
    monkeypatch.setattr(
        mlounge_mod, "_run",
        lambda *a, **k: _types.SimpleNamespace(
            returncode=1, stdout="", stderr="404 not found"))
    import pytest

    with pytest.raises(mlounge_mod.MLoungeError, match="404"):
        mlounge_mod.ensure_mlounge_installed()


def test_ensure_replaces_upstream_with_fork(tmp_path, monkeypatch) -> None:
    import json as _json
    import types as _types
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    shipped = tmp_path / "fork"
    (shipped / "dist" / "server").mkdir(parents=True)
    (shipped / "package.json").write_text(_json.dumps(
        {"version": "4.5.2-mercury.9"}))
    (shipped / "dist" / "server" / "index.js").write_text("// built\n")
    (shipped / "index.js").write_text("#!/usr/bin/env node\n")
    monkeypatch.setattr(
        mlounge_mod, "_fork_source_tree", lambda: shipped)
    # a hand-installed upstream already here
    final = home / "observatory" / "lounge" / "pkg"
    final.mkdir(parents=True)
    (final / "package.json").write_text(_json.dumps({"version": "4.5.2"}))
    (final / "index.js").write_text("// upstream\n")

    def _fake_run(args, **kwargs):
        (final / "node_modules" / "irc-framework").mkdir(parents=True)
        (home / "observatory" / "lounge" / "npm" / "bin").mkdir(
            parents=True)
        (home / "observatory" / "lounge" / "npm" / "bin"
         / "thelounge").write_text("#!/bin/sh\n")
        return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mlounge_mod, "_run", _fake_run)
    out = mlounge_mod.ensure_mlounge_installed()
    assert out.endswith("npm/bin/mlounge")
    import os as _os

    assert _os.path.realpath(out).endswith("lounge/pkg/index.js")
    assert _json.loads((final / "package.json").read_text())["version"] == \
        "4.5.2-mercury.9"


def test_ensure_mlounge_network_current_when_identical(tmp_path) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": [{
        "name": "vm", "host": "100.9.9.9", "port": 6670,
        "password": "pw", "nick": "owner", "username": "owner",
        "channels": [{"name": "#vm_gateway", "muted": False,
                      "key": ""}]}]}))
    out = mlounge_mod.ensure_mlounge_network(
        paths, "owner", net_name="vm", host="100.9.9.9", port=6670,
        server_password="pw", nick="owner", channel="#vm_gateway")
    assert out["action"] == "current"


_SNIPPET = "x.splice(e.index||-1,0,n),e.chan.type===`query`&&!e.shouldOpen)return;y()"


def test_frontend_patch_respects_should_open() -> None:
    from observatory import mlounge as mlounge_mod

    assert mlounge_mod.FRONTEND_JOIN_OPEN in _SNIPPET
    patched, changed = mlounge_mod.patch_mlounge_frontend_text(_SNIPPET)
    assert changed is True
    assert mlounge_mod.FRONTEND_JOIN_OPEN not in patched
    assert patched.endswith("!e.shouldOpen)return;y()")


def test_frontend_patch_idempotent_and_drift(tmp_path) -> None:
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    assets = paths.dir / "pkg" / "public" / "assets"
    assets.mkdir(parents=True)
    bundle = assets / "index-abc123.js"
    bundle.write_text("var a=1;" + _SNIPPET)
    first = mlounge_mod.patch_mlounge_frontend(paths)
    assert first["action"] == "patched"
    assert mlounge_mod.patch_mlounge_frontend(paths)["action"] == "current"
    assert mlounge_mod.FRONTEND_JOIN_OPEN not in bundle.read_text()


def test_frontend_patch_drift_is_loud(tmp_path) -> None:
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    assets = paths.dir / "pkg" / "public" / "assets"
    assets.mkdir(parents=True)
    (assets / "index-zzz.js").write_text("var a=1;")
    assert mlounge_mod.patch_mlounge_frontend(paths)["action"] == "pattern-missing"


def test_install_pins_fork_version(tmp_path, monkeypatch) -> None:
    import json as _json
    from pathlib import Path

    from mercury_cli import __version__ as mercury_version
    from observatory import mlounge as mlounge_mod

    tree = Path(mlounge_mod.__file__).resolve().parents[2] / "third_party" / "mlounge"
    pkg = _json.loads((tree / "package.json").read_text(encoding="utf-8"))
    assert pkg.get("mercuryFork") is True
    assert mlounge_mod._fork_tree_version(tree) == mercury_version


def test_frontend_patch_skips_fork_bundle(tmp_path) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    pkg = paths.dir / "pkg"
    assets = pkg / "public" / "assets"
    assets.mkdir(parents=True)
    (pkg / "package.json").write_text(_json.dumps(
        {"version": "4.5.2-mercury.1"}))
    bundle = assets / "index-abc123.js"
    bundle.write_text("var a=1;")
    assert mlounge_mod.patch_mlounge_frontend(paths)["action"] == "skipped"
    assert bundle.read_text() == "var a=1;"


def test_frontend_patch_recognizes_product_version_and_ignores_old_bundle_failures(tmp_path) -> None:
    import json

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    pkg = paths.dir / "pkg"
    assets = pkg / "public" / "assets"
    assets.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": "0.3.4", "mercuryFork": True}))
    bundle = assets / "index-current.js"
    bundle.write_text("already fixed in fork source")
    (paths.dir / "frontend-patch.json").write_text(json.dumps({"index-old.js:1:1": "pattern-missing"}))
    assert mlounge_mod.patch_mlounge_frontend(paths)["action"] == "skipped"
    assert bundle.read_text() == "already fixed in fork source"
    # Legacy bundles still use patching, but an obsolete asset cannot keep
    # a successfully patched current bundle in the drift-warning state.
    (pkg / "package.json").write_text(json.dumps({"version": "4.5.2"}))
    bundle.write_text(_SNIPPET)
    assert mlounge_mod.patch_mlounge_frontend(paths)["action"] == "patched"
    assert mlounge_mod.patch_mlounge_frontend(paths)["action"] == "current"


def test_conf_lives_in_mlounge_home(tmp_path) -> None:
    """config.js must be $THELOUNGE_HOME/config.js — the only file read."""
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    assert paths.conf == paths.home / "config.js"


def test_ensure_removes_stale_dir_config(tmp_path) -> None:
    from observatory import mlounge as mlounge_mod

    paths = mlounge_mod.MLoungePaths(tmp_path / "mercury")
    paths.dir.mkdir(parents=True)
    (paths.dir / "config.js").write_text("// stale pre-0.131 location\n")
    out = mlounge_mod.ensure_mlounge_config(paths, host="127.0.0.1", port=9000)
    assert out["action"] == "wrote"
    assert not (paths.dir / "config.js").exists()
    live = (paths.home / "config.js").read_text()
    assert "fileUpload" in live
    assert mlounge_mod.ensure_mlounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "current"


def test_stage_upload_happy_path(tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    src = tmp_path / "report.pdf"
    src.write_bytes(b"%PDF-1.4 data")
    out = mlounge_mod.stage_mlounge_upload(home, src)
    assert out["url_path"].startswith("uploads/")
    assert out["url_path"].endswith("/report.pdf")
    assert out["filename"] == "report.pdf"
    token = out["url_path"].split("/")[1]
    stored = (home / "observatory" / "lounge" / "home" / "uploads"
              / token[:2] / token)
    assert stored.read_bytes() == b"%PDF-1.4 data"


def test_stage_upload_refusals(tmp_path, monkeypatch) -> None:
    import pytest
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.stage_mlounge_upload(home, tmp_path / "missing.txt")
    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.stage_mlounge_upload(home, "/etc/hostname")
    key = tmp_path / "id_rsa.key"
    key.write_text("x")
    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.stage_mlounge_upload(home, key)
    d = tmp_path / "sub"
    d.mkdir()
    with pytest.raises(mlounge_mod.MLoungeError):
        mlounge_mod.stage_mlounge_upload(home, d)


def test_refresh_mlounge_fork_current_touches_nothing(tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(
        mlounge_mod, "fork_staleness", lambda home=None: "current")
    called = []
    monkeypatch.setattr(
        mlounge_mod, "ensure_mlounge_installed",
        lambda *a, **k: called.append("ensure"))
    monkeypatch.setattr(
        mlounge_mod, "restart_mlounge", lambda: called.append("restart"))
    assert mlounge_mod.refresh_mlounge_fork(tmp_path / "mercury") == "current"
    assert called == []


def test_refresh_mlounge_fork_reinstalls_and_restarts_on_drift(
        tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(
        mlounge_mod, "fork_staleness", lambda home=None: "stale-version")
    called = []
    monkeypatch.setattr(
        mlounge_mod, "ensure_mlounge_installed",
        lambda *a, **k: called.append("ensure"))
    monkeypatch.setattr(
        mlounge_mod, "restart_mlounge", lambda: called.append("restart"))
    assert mlounge_mod.refresh_mlounge_fork(tmp_path / "mercury") == "reinstalled"
    assert called == ["ensure", "restart"]


def test_refresh_mlounge_fork_skipped_without_shipped_tree(
        tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(
        mlounge_mod, "fork_staleness", lambda home=None: "no-shipped")
    called = []
    monkeypatch.setattr(
        mlounge_mod, "ensure_mlounge_installed",
        lambda *a, **k: called.append("ensure"))
    out = mlounge_mod.refresh_mlounge_fork(tmp_path / "mercury")
    assert out.startswith("skipped")
    assert called == []


def test_refresh_mlounge_fork_reports_failed_bounce(
        tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(
        mlounge_mod, "fork_staleness", lambda home=None: "stale-version")
    monkeypatch.setattr(
        mlounge_mod, "ensure_mlounge_installed", lambda *a, **k: None)

    def _boom() -> None:
        raise mlounge_mod.MLoungeError("restart failed: boom")

    monkeypatch.setattr(mlounge_mod, "restart_mlounge", _boom)
    out = mlounge_mod.refresh_mlounge_fork(tmp_path / "mercury")
    assert out.startswith("reinstalled-no-restart")


def test_refresh_reinstalls_current_shipped_tree(tmp_path, monkeypatch) -> None:
    """End-to-end with the REAL shipped version: an older install revends.

    Regression guard for the forgotten-bump class of staleness — the
    reinstall decision must track the actual tree content version."""
    from observatory import mlounge as mlounge_mod
    from pathlib import Path as _Path

    shipped = _Path(mlounge_mod.__file__).resolve().parents[2] / "third_party" / "mlounge"
    want = mlounge_mod._fork_tree_version(shipped)
    from mercury_cli import __version__ as mercury_version

    assert want == mercury_version  # fork IS the release: one version
    monkeypatch.setattr(mlounge_mod, "_fork_source_tree", lambda: shipped)
    monkeypatch.setattr(
        mlounge_mod, "_installed_fork_version", lambda final: "0.0.0")
    called = []
    monkeypatch.setattr(
        mlounge_mod, "ensure_mlounge_installed",
        lambda *a, **k: called.append("ensure"))
    monkeypatch.setattr(
        mlounge_mod, "restart_mlounge", lambda: called.append("restart"))
    assert mlounge_mod.refresh_mlounge_fork(tmp_path / "mercury") == "reinstalled"
    assert called == ["ensure", "restart"]


def test_fork_versions_reports_pair(tmp_path, monkeypatch) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    fake = tmp_path / "shipped"
    (fake).mkdir()
    (fake / "package.json").write_text(_json.dumps({"version": "9.9"}))
    monkeypatch.setattr(mlounge_mod, "_fork_source_tree", lambda: fake)
    home = tmp_path / "mercury"
    have, want = mlounge_mod.fork_versions(home)
    assert want == "9.9"
    assert have is None  # nothing installed under the fake home


def test_is_fork_tree_markers(tmp_path) -> None:
    import json as _json
    from observatory import mlounge as mlounge_mod

    def _tree(marker: bool, dist: bool) -> object:
        root = tmp_path / f"t{marker}{dist}"
        root.mkdir(exist_ok=True)
        pkg = {"version": "0.0.0"}
        if marker:
            pkg["mercuryFork"] = True
        (root / "package.json").write_text(_json.dumps(pkg))
        if dist:
            srv = root / "dist" / "server"
            srv.mkdir(parents=True)
            (srv / "index.js").write_text("// built\n")
        return root

    assert mlounge_mod._is_fork_tree(_tree(True, True)) is True
    assert mlounge_mod._is_fork_tree(_tree(True, False)) is False
    assert mlounge_mod._is_fork_tree(_tree(False, True)) is False


def _fake_shipped(root, *, version="0.0.2", sha="BBB", marker=True):
    import json as _json

    root.mkdir(parents=True, exist_ok=True)
    pkg = {"version": version}
    if marker:
        pkg["mercuryFork"] = True
    (root / "package.json").write_text(_json.dumps(pkg))
    (root / "dist" / "server").mkdir(parents=True, exist_ok=True)
    (root / "dist" / "server" / "index.js").write_text("// built\n")
    (root / ".mercury-fork-build.json").write_text(_json.dumps(
        {"fork_version": version, "source_sha": sha}))
    (root / "index.js").write_text("#!/usr/bin/env node\n")
    return root


def _fake_installed(home, *, version="0.0.2", sha="BBB"):
    import json as _json

    final = home / "observatory" / "lounge" / "pkg"
    (final).mkdir(parents=True, exist_ok=True)
    (final / "package.json").write_text(_json.dumps({"version": version}))
    (final / ".mercury-fork-build.json").write_text(_json.dumps(
        {"fork_version": version, "source_sha": sha}))
    (final / "node_modules" / "irc-framework").mkdir(parents=True, exist_ok=True)
    prefix_bin = home / "observatory" / "lounge" / "npm" / "bin"
    prefix_bin.mkdir(parents=True, exist_ok=True)
    (prefix_bin / "thelounge").write_text("#!/bin/sh\n")
    return final


def test_fork_staleness_tiers(tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    shipped = _fake_shipped(tmp_path / "fork")
    monkeypatch.setattr(mlounge_mod, "_fork_source_tree", lambda: shipped)
    assert mlounge_mod.fork_staleness(home) == "missing"
    _fake_installed(home, version="0.0.1", sha="AAA")
    assert mlounge_mod.fork_staleness(home) == "stale-version"
    _fake_installed(home, version="0.0.2", sha="AAA")
    assert mlounge_mod.fork_staleness(home) == "stale-content"
    _fake_installed(home, version="0.0.2", sha="BBB")
    assert mlounge_mod.fork_staleness(home) == "current"


def test_fork_staleness_no_shipped(tmp_path, monkeypatch) -> None:
    from observatory import mlounge as mlounge_mod

    monkeypatch.setattr(mlounge_mod, "_fork_source_tree", lambda: None)
    assert mlounge_mod.fork_staleness(tmp_path / "mercury") == "no-shipped"


def test_fork_staleness_missing_fingerprint_is_current(tmp_path, monkeypatch) -> None:
    """No fingerprint either side + matching versions: can't prove drift."""
    import json as _json
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    shipped = tmp_path / "fork"
    shipped.mkdir(parents=True)
    (shipped / "package.json").write_text(_json.dumps(
        {"version": "0.0.2", "mercuryFork": True}))
    monkeypatch.setattr(mlounge_mod, "_fork_source_tree", lambda: shipped)
    final = _fake_installed(home, version="0.0.2", sha="BBB")
    (final / ".mercury-fork-build.json").unlink()
    (shipped / ".mercury-fork-build.json").unlink(missing_ok=True)
    assert mlounge_mod.fork_staleness(home) == "current"


def test_refresh_reinstalls_content_drift(tmp_path, monkeypatch) -> None:
    """Same version, different shasums: revends and bounces, honestly labeled."""
    from observatory import mlounge as mlounge_mod

    home = tmp_path / "mercury"
    shipped = _fake_shipped(tmp_path / "fork", version="0.0.2", sha="BBB")
    monkeypatch.setattr(mlounge_mod, "_fork_source_tree", lambda: shipped)
    _fake_installed(home, version="0.0.2", sha="AAA")
    called = []
    monkeypatch.setattr(
        mlounge_mod, "ensure_mlounge_installed",
        lambda *a, **k: called.append("ensure"))
    monkeypatch.setattr(
        mlounge_mod, "restart_mlounge", lambda: called.append("restart"))
    assert mlounge_mod.refresh_mlounge_fork(home) == "reinstalled-content"
    assert called == ["ensure", "restart"]


def test_upload_refuses_symlinked_credential_directory(tmp_path, monkeypatch):
    """Neither a credential path nor its external target may become a web upload."""
    import pytest
    from observatory import mlounge as mlounge_mod

    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    credentials = tmp_path / "external-keys"
    credentials.mkdir()
    key = credentials / "id_ed25519"
    key.write_text("synthetic-private-key")
    (operator_home / ".ssh").symlink_to(credentials, target_is_directory=True)
    monkeypatch.setenv("HOME", str(operator_home))
    mercury_home = operator_home / ".mercury"
    for source in (operator_home / ".ssh" / key.name, key):
        with pytest.raises(mlounge_mod.MLoungeError):
            mlounge_mod.stage_mlounge_upload(mercury_home, source)
    assert not (mercury_home / "observatory" / "lounge" / "home" / "uploads").exists()
