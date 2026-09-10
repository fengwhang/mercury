"""VM-report slice 4: explicit wipe choice (archive vs annihilate).

Residual installs break clean reinstalls (stale server_name pins, orphaned
MXIDs, device keys bound to a dead DB). ``wipe_observatory_data`` covers
tuwunel data — toml, DB dir (RocksDB + archived WALs), owner credentials,
appservice registrations, renderer state.db, crypto stores — plus the
generated user units and the stale .env owner mirror. The tuwunel binary +
logs are install artifacts and survive both modes.

Real files throughout (tmp MERCURY_HOME + fake unit dir); systemd is
disabled via _systemctl_available=False so no host unit is ever touched.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from observatory import provision as provision_mod
from observatory.config_gen import (
    HOMESERVER_UNIT_NAME,
    SIDECAR_UNIT_NAME,
    ObservatoryPaths,
)


def _seed(home: Path, units: Path) -> ObservatoryPaths:
    paths = ObservatoryPaths(home)
    paths.db_dir.mkdir(parents=True)
    (paths.db_dir / "data.mdb").write_text("db", encoding="utf-8")
    (paths.db_dir / "archived-wal.log").write_text("wal", encoding="utf-8")
    paths.toml.write_text('server_name = "mercury.local"\n', encoding="utf-8")
    paths.owner_credentials.write_text('{"user_id": "@o:x"}\n', encoding="utf-8")
    paths.appservices_dir.mkdir(parents=True)
    (paths.appservices_dir / "merc-observatory.yaml").write_text(
        "as_token: s\n", encoding="utf-8")
    (paths.root / "state.db").write_text("state", encoding="utf-8")
    (paths.root / "crypto").mkdir()
    (paths.root / "crypto" / "store.db").write_text("keys", encoding="utf-8")
    # Install artifacts that must SURVIVE both modes.
    paths.bin_dir.mkdir(parents=True)
    (paths.bin_dir / "tuwunel").write_text("binary", encoding="utf-8")
    paths.logs_dir.mkdir(parents=True)
    (paths.logs_dir / "homeserver.log").write_text("log", encoding="utf-8")
    (home / ".env").write_text(
        "MATRIX_OBS_OWNER_USER_ID=@o:x\n"
        "MATRIX_OBS_OWNER_PASSWORD=old-password-12345\n"
        "OTHER_KEY=keepme\n",
        encoding="utf-8",
    )
    units.mkdir(parents=True)
    (units / HOMESERVER_UNIT_NAME).write_text("[unit hs]\n", encoding="utf-8")
    (units / SIDECAR_UNIT_NAME).write_text("[unit sidecar]\n", encoding="utf-8")
    return paths


def _no_systemd(monkeypatch):
    monkeypatch.setattr(provision_mod, "_systemctl_available", lambda: False)


def test_archive_moves_data_keeps_artifacts(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    units = tmp_path / "units"
    paths = _seed(home, units)

    summary = provision_mod.wipe_observatory_data(
        home, mode="archive", unit_dir=units)

    assert summary["mode"] == "archive"
    zip_path = Path(summary["archived_to"])
    assert zip_path.parent == paths.root
    assert zip_path.suffix == ".zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        top = zip_path.stem
        for name in ("tuwunel.toml", "tuwunel-db", "owner-credentials.json",
                     "appservices", "state.db", "crypto",
                     HOMESERVER_UNIT_NAME, SIDECAR_UNIT_NAME):
            assert (f"{top}/{name}" in names
                    or f"{top}/{name}/" in names), name
        # Archived WAL survived inside the moved DB dir.
        assert zf.read(f"{top}/tuwunel-db/archived-wal.log") == b"wal"
        # Unit files snapshotted into the archive.
        assert zf.read(f"{top}/{HOMESERVER_UNIT_NAME}") == b"[unit hs]\n"
        assert zf.read(f"{top}/{SIDECAR_UNIT_NAME}") == b"[unit sidecar]\n"
    for name in ("tuwunel.toml", "tuwunel-db", "owner-credentials.json",
                 "appservices", "state.db", "crypto"):
        assert not (paths.root / name).exists(), name
    # No loose dir left behind; live data gone so presence is False.
    assert [p for p in paths.root.glob("wiped-archive-*")
            if p.is_dir()] == []
    assert not provision_mod.observatory_data_present(home)
    assert summary["units_removed"] == [HOMESERVER_UNIT_NAME, SIDECAR_UNIT_NAME]
    # Binary + logs kept; .env stripped of owner keys only.
    assert (paths.bin_dir / "tuwunel").exists()
    assert (paths.logs_dir / "homeserver.log").exists()
    env = (home / ".env").read_text(encoding="utf-8")
    assert "MATRIX_OBS_OWNER" not in env
    assert "OTHER_KEY=keepme" in env


def test_annihilate_deletes_data_keeps_artifacts(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    units = tmp_path / "units"
    paths = _seed(home, units)

    summary = provision_mod.wipe_observatory_data(
        home, mode="annihilate", unit_dir=units)

    assert summary["mode"] == "annihilate"
    assert "archived_to" not in summary
    assert sorted(summary["deleted"]) == sorted(
        ["tuwunel.toml", "tuwunel-db", "owner-credentials.json",
         "appservices", "state.db", "crypto"])
    assert not provision_mod.observatory_data_present(home)
    assert list(units.iterdir()) == []
    assert (paths.bin_dir / "tuwunel").exists()
    assert (paths.logs_dir / "homeserver.log").exists()
    assert "MATRIX_OBS_OWNER" not in (home / ".env").read_text()


def test_unknown_mode_fails_closed(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    with pytest.raises(provision_mod.ProvisionError, match="unknown wipe mode"):
        provision_mod.wipe_observatory_data(tmp_path, mode="yeet")


def test_empty_home_wipe_is_noop(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    units = tmp_path / "units"
    units.mkdir()
    assert not provision_mod.observatory_data_present(tmp_path)
    summary = provision_mod.wipe_observatory_data(
        tmp_path, mode="archive", unit_dir=units)
    assert summary["moved"] == []
    zip_path = Path(summary["archived_to"])
    assert zip_path.suffix == ".zip"
    assert zip_path.is_file()


def test_setup_rerun_identity_change_offers_wipe_reprovision(
        monkeypatch, capsys, tmp_path):
    """Changed identity + archive choice wipes, then provisions the NEW triple."""
    import json as _json

    import mercury_cli.setup as setup_mod
    from tests.observatory.test_setup_wizard import (
        _FakeProvision,
        _run_section,
        _status,
        _write_credentials,
    )

    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([{
        **_status(),
        "provisioned": True,
        "config_exists": True,
        "binary_installed": True,
        "owner_credentials_exist": True,
        "owner_credentials_path": str(creds),
        "homeserver_reachable": True,
        "unit_active": True,
    }])
    wiped: list = []

    def fake_wipe(*, mode):
        wiped.append(mode)
        return {"mode": mode, "moved": ["tuwunel-db"], "deleted": []}

    monkeypatch.setattr(fake, "wipe_observatory_data", fake_wipe, raising=False)
    # Wipe-first order: 0 = Install/repair, 0 = Keep (up-front wipe
    # question), then the typed identity change triggers the conditional
    # wipe offer answered 1 = archive; trailing 0 keeps mirror_cli off.
    answers = [0, 0, 1, 0]

    orig_choice = setup_mod.prompt_choice

    def seq_choice(q, c, d=0, description=None):
        assert answers, f"unexpected extra prompt_choice: {q!r}"
        return answers.pop(0)

    monkeypatch.setattr(setup_mod, "prompt_choice", seq_choice)
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda q, default=True: True)
    texts = iter(["newbox.lan", "", ""])

    def fake_prompt(question, default=None, password=False):
        answer = next(texts)
        return answer if answer else (default or "")

    monkeypatch.setattr(setup_mod, "prompt", fake_prompt)
    from mercury_cli.config import load_config
    setup_mod.setup_observatory(load_config())
    out = capsys.readouterr().out
    assert wiped == ["archive"]
    assert fake.provision_kwargs == {
        "server_name": "newbox.lan",
        "owner_localpart": _json.loads(creds.read_text())["user_id"]
        .lstrip("@").split(":", 1)[0],
        "owner_password": None,
    }
    assert "re-provisioned as @" in out
