"""Zip-archive + true annihilate: six live targets, bootstrap toml, zips.

Archive moves the six live targets PLUS ``tuwunel-bootstrap.toml`` (a crashed
provision can strand it between write and cleanup) into a timestamped
``wiped-archive-*`` dir, then zips it to ``.zip`` and removes the loose dir —
archives accumulate as inert zips, never loose dirs, and neither mode ever
deletes a ``*.zip``. Annihilate deletes the six PLUS the bootstrap PLUS any
loose (unzipped) ``wiped-archive-*`` dirs. ``observatory_wipe_targets`` /
``observatory_data_present`` never match ``*.zip``.

Real files throughout (tmp MERCURY_HOME + fake unit dir); systemd is
disabled via _systemctl_available=False so no host unit is ever touched.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from observatory import provision as provision_mod
from observatory.config_gen import (
    HOMESERVER_UNIT_NAME,
    SIDECAR_UNIT_NAME,
    ObservatoryPaths,
)

SIX = (
    "tuwunel.toml",
    "tuwunel-db",
    "owner-credentials.json",
    "appservices",
    "state.db",
    "crypto",
)


def _seed_six(home: Path) -> ObservatoryPaths:
    paths = ObservatoryPaths(home)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.toml.write_text('server_name = "mercury.local"\n', encoding="utf-8")
    paths.db_dir.mkdir(parents=True)
    (paths.db_dir / "data.mdb").write_text("db", encoding="utf-8")
    paths.owner_credentials.write_text('{"user_id": "@o:x"}\n', encoding="utf-8")
    paths.appservices_dir.mkdir(parents=True)
    (paths.appservices_dir / "merc-observatory.yaml").write_text(
        "as_token: s\n", encoding="utf-8")
    (paths.root / "state.db").write_text("state", encoding="utf-8")
    (paths.root / "crypto").mkdir()
    (paths.root / "crypto" / "store.db").write_text("keys", encoding="utf-8")
    return paths


def _seed_loose(paths: ObservatoryPaths, *names: str) -> list[str]:
    """Pre-zip-era loose archive dirs (leftovers, never live state)."""
    for name in names:
        old = paths.root / name
        old.mkdir(parents=True)
        (old / "owner-credentials.json").write_text(
            '{"user_id": "@stale:x"}\n', encoding="utf-8")
    return list(names)


def _seed_zip(paths: ObservatoryPaths, name: str) -> str:
    """An inert zip forensic — neither mode may ever delete it."""
    zp = paths.root / name
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("tuwunel.toml", 'server_name = "old.local"\n')
    return name


def _seed_bootstrap(paths: ObservatoryPaths) -> str:
    paths.bootstrap_toml.write_text(
        'registration_token = "stranded-secret"\n', encoding="utf-8")
    return paths.bootstrap_toml.name


def _seed_units(tmp_path: Path) -> Path:
    units = tmp_path / "units"
    units.mkdir()
    (units / HOMESERVER_UNIT_NAME).write_text("[unit hs]\n", encoding="utf-8")
    (units / SIDECAR_UNIT_NAME).write_text("[unit sidecar]\n", encoding="utf-8")
    return units


def _no_systemd(monkeypatch):
    monkeypatch.setattr(provision_mod, "_systemctl_available", lambda: False)


def _loose_dirs(root: Path) -> list[str]:
    return sorted(
        p.name for p in root.glob("wiped-archive-*")
        if p.is_dir() and not p.is_symlink())

def test_archive_zips_snapshot_and_leaves_no_loose_dir(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    paths = _seed_six(home)
    bootstrap = _seed_bootstrap(paths)

    summary = provision_mod.wipe_observatory_data(
        home, mode="archive", unit_dir=_seed_units(tmp_path))

    assert summary["mode"] == "archive"
    assert sorted(summary["moved"]) == sorted([*SIX, bootstrap])
    assert summary["deleted"] == []
    zip_path = Path(summary["archived_to"])
    assert zip_path.parent == paths.root
    assert zip_path.suffix == ".zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    top = zip_path.stem
    for name in (*SIX, bootstrap, HOMESERVER_UNIT_NAME, SIDECAR_UNIT_NAME):
        assert (f"{top}/{name}" in names
                or f"{top}/{name}/" in names), name
    # Live targets gone; no loose dir left behind; zip invisible to presence.
    for name in (*SIX, bootstrap):
        assert not (paths.root / name).exists(), name
    assert _loose_dirs(paths.root) == []
    assert not provision_mod.observatory_data_present(home)


def test_archive_never_touches_prior_archives(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    paths = _seed_six(home)
    kept_zip = _seed_zip(paths, "wiped-archive-20200101-000000.zip")
    kept_loose, = _seed_loose(paths, "wiped-archive-20200202-000000")

    summary = provision_mod.wipe_observatory_data(
        home, mode="archive", unit_dir=_seed_units(tmp_path))

    new_zip = Path(summary["archived_to"])
    assert new_zip.is_file()
    assert (paths.root / kept_zip).is_file()
    assert (paths.root / kept_loose).is_dir()
    assert sorted(p.name for p in paths.root.glob("wiped-archive-*.zip")) == sorted(
        [kept_zip, new_zip.name])


def test_annihilate_kills_live_loose_and_bootstrap_keeps_zips(
        tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    paths = _seed_six(home)
    bootstrap = _seed_bootstrap(paths)
    loose = _seed_loose(
        paths, "wiped-archive-20200101-000000", "wiped-archive-20200202-000000")
    kept = _seed_zip(paths, "wiped-archive-20200303-000000.zip")
    paths.bin_dir.mkdir(parents=True)
    (paths.bin_dir / "tuwunel").write_text("binary", encoding="utf-8")
    paths.logs_dir.mkdir(parents=True)
    (paths.logs_dir / "homeserver.log").write_text("log", encoding="utf-8")

    summary = provision_mod.wipe_observatory_data(
        home, mode="annihilate", unit_dir=_seed_units(tmp_path))

    assert summary["mode"] == "annihilate"
    assert "archived_to" not in summary
    for name in (*SIX, bootstrap, *loose):
        assert not (paths.root / name).exists(), name
        assert name in summary["deleted"], name
    # The zip survives and is never listed as deleted.
    assert (paths.root / kept).is_file()
    assert kept not in summary["deleted"]
    assert _loose_dirs(paths.root) == []
    assert not provision_mod.observatory_data_present(home)
    assert (paths.bin_dir / "tuwunel").exists()
    assert (paths.logs_dir / "homeserver.log").exists()


def test_annihilate_without_extras_deletes_just_the_six(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    _seed_six(home)

    summary = provision_mod.wipe_observatory_data(
        home, mode="annihilate", unit_dir=_seed_units(tmp_path))

    assert sorted(summary["deleted"]) == sorted(SIX)


def test_zips_are_invisible_to_discovery_and_presence(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "mercury"
    home.mkdir()
    paths = ObservatoryPaths(home)
    paths.root.mkdir(parents=True)
    kept = _seed_zip(paths, "wiped-archive-20200101-000000.zip")

    assert provision_mod.observatory_wipe_targets(paths) == []
    assert not provision_mod.observatory_data_present(home)

    # Annihilate on a home holding only a zip is a noop that keeps the zip.
    summary = provision_mod.wipe_observatory_data(
        home, mode="annihilate", unit_dir=_seed_units(tmp_path))
    assert summary["deleted"] == []
    assert (paths.root / kept).is_file()
