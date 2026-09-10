"""Owner-password mirror atomicity + setup-time mismatch heal.

Every path that sets/changes the owner password must rewrite
owner-credentials.json AND the $MERCURY_HOME/.env mirror together (or fail
loudly); a stale MATRIX_OBS_OWNER_PASSWORD in .env authenticates nowhere
and leaves the user unable to tell which credential works. Covers:

- describe_owner_env_mismatch: consistent / stale / missing /
  unprovisioned / corrupt (never raises);
- heal_owner_env: overwrites stale values (the VM hole: the old
  missing-only heal never corrected them), creates a missing .env,
  preserves unrelated .env lines, fails loudly when unprovisioned;
- quote round-trip: exotic passwords compare consistent after mirroring;
- mirror failure raises ProvisionError loudly and leaves no temp files;
- the setup wizard warns + heals a stale mirror (and degrades loudly when
  healing itself fails) without ever prompting for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths


def _paths(home: Path) -> ObservatoryPaths:
    return ObservatoryPaths(home)


def _write_creds(paths: ObservatoryPaths, **over) -> dict:
    doc = {
        "homeserver_url": "http://127.0.0.1:18008",
        "user_id": "@owner:mercury.local",
        "password": "old-password-12345",
        "access_token": "admin-tok",
        "device_id": "DEV",
    }
    doc.update(over)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.owner_credentials.write_text(json.dumps(doc), encoding="utf-8")
    return doc


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_NONINTERACTIVE", raising=False)
    return tmp_path


def _env_text(home: Path) -> str:
    return (home / ".env").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def test_describe_consistent_when_mirror_matches(home: Path):
    paths = _paths(home)
    _write_creds(paths)
    provision_mod.mirror_owner_env(home, "@owner:mercury.local",
                                   "old-password-12345")
    assert provision_mod.describe_owner_env_mismatch(paths) == {
        "missing": [], "stale": []}


def test_describe_flags_stale_password(home: Path):
    paths = _paths(home)
    _write_creds(paths, password="correct-horse-password-1")
    (home / ".env").write_text(
        "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local\n"
        "MATRIX_OBS_OWNER_PASSWORD=stale-password-00000\n",
        encoding="utf-8",
    )
    assert provision_mod.describe_owner_env_mismatch(paths) == {
        "missing": [], "stale": ["MATRIX_OBS_OWNER_PASSWORD"]}


def test_describe_flags_stale_user_id(home: Path):
    paths = _paths(home)
    _write_creds(paths, user_id="@someone-else:mercury.local")
    (home / ".env").write_text(
        "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local\n"
        "MATRIX_OBS_OWNER_PASSWORD=old-password-12345\n",
        encoding="utf-8",
    )
    mismatch = provision_mod.describe_owner_env_mismatch(paths)
    assert mismatch["missing"] == []
    assert "MATRIX_OBS_OWNER_USER_ID" in mismatch["stale"]


def test_describe_flags_missing_env(home: Path):
    paths = _paths(home)
    _write_creds(paths)
    assert provision_mod.describe_owner_env_mismatch(paths) == {
        "missing": ["MATRIX_OBS_OWNER_USER_ID", "MATRIX_OBS_OWNER_PASSWORD"],
        "stale": [],
    }


def test_describe_unprovisioned_is_error_not_raise(home: Path):
    mismatch = provision_mod.describe_owner_env_mismatch(_paths(home))
    assert "error" in mismatch


def test_describe_corrupt_credentials_is_error_not_raise(home: Path):
    paths = _paths(home)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.owner_credentials.write_text("{not json", encoding="utf-8")
    assert "error" in provision_mod.describe_owner_env_mismatch(paths)


# ---------------------------------------------------------------------------
# heal
# ---------------------------------------------------------------------------


def test_heal_overwrites_stale_password_and_keeps_other_lines(home: Path):
    """The VM hole: a stale .env password must not survive a heal."""
    paths = _paths(home)
    _write_creds(paths, password="correct-horse-password-1")
    (home / ".env").write_text(
        "OTHER_KEY=keep-me\n"
        "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local\n"
        "MATRIX_OBS_OWNER_PASSWORD=stale-password-00000\n",
        encoding="utf-8",
    )
    healed = provision_mod.heal_owner_env(paths)
    assert healed == ["MATRIX_OBS_OWNER_PASSWORD"]
    env = _env_text(home)
    assert "OTHER_KEY=keep-me" in env
    assert "MATRIX_OBS_OWNER_PASSWORD=correct-horse-password-1" in env
    assert "stale-password-00000" not in env
    assert provision_mod.describe_owner_env_mismatch(paths) == {
        "missing": [], "stale": []}


def test_heal_creates_missing_env(home: Path):
    paths = _paths(home)
    _write_creds(paths)
    assert provision_mod.heal_owner_env(paths) == [
        "MATRIX_OBS_OWNER_USER_ID", "MATRIX_OBS_OWNER_PASSWORD"]
    assert oct((home / ".env").stat().st_mode & 0o777) == "0o600"


def test_heal_unprovisioned_fails_loudly(home: Path):
    with pytest.raises(provision_mod.ProvisionError):
        provision_mod.heal_owner_env(_paths(home))


def test_exotic_password_round_trips_consistent(home: Path):
    password = 'pa"ss\\word with spaces and #hash!'
    paths = _paths(home)
    _write_creds(paths, password=password)
    provision_mod.mirror_owner_env(home, "@owner:mercury.local", password)
    assert provision_mod.describe_owner_env_mismatch(paths) == {
        "missing": [], "stale": []}


def test_mirror_writes_0600(home: Path):
    provision_mod.mirror_owner_env(home, "@owner:mercury.local",
                                   "old-password-12345")
    assert oct((home / ".env").stat().st_mode & 0o777) == "0o600"


def test_mirror_failure_raises_loudly_without_temp_litter(home: Path):
    (home / ".env").mkdir()  # a directory where the file belongs
    with pytest.raises(provision_mod.ProvisionError):
        provision_mod.mirror_owner_env(home, "@owner:mercury.local",
                                       "old-password-12345")
    leftovers = [p for p in home.iterdir() if p.name.startswith(".env.")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# setup-time wiring (warn + heal, never prompts)
# ---------------------------------------------------------------------------


def _provisioned_status(creds) -> dict:
    from tests.observatory.test_setup_wizard import _status

    return _status(
        provisioned=True,
        config_exists=True,
        binary_installed=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
        homeserver_reachable=True,
        unit_active=True,
    )


def _run_section(monkeypatch, capsys, fake, *, yes_no, texts=("", "", "")):
    from tests.observatory.test_setup_wizard import _run_section as run

    return run(monkeypatch, capsys, fake, choice=0, yes_no=yes_no, texts=texts)


def test_setup_warns_and_heals_stale_mirror(home: Path, monkeypatch, capsys):
    from tests.observatory.test_setup_wizard import _FakeProvision

    paths = _paths(home)
    creds_path = paths.owner_credentials
    _write_creds(paths, password="correct-horse-password-1")
    (home / ".env").write_text(
        "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local\n"
        "MATRIX_OBS_OWNER_PASSWORD=stale-password-00000\n",
        encoding="utf-8",
    )
    fake = _FakeProvision([_provisioned_status(creds_path)])
    fake.describe_owner_env_mismatch = (
        lambda *a, **k: provision_mod.describe_owner_env_mismatch(paths))
    fake.heal_owner_env = lambda *a, **k: provision_mod.heal_owner_env(paths)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, yes_no=[True, True])
    assert "disagrees" in out and "MATRIX_OBS_OWNER_PASSWORD" in out
    assert "Healed the .env owner mirror" in out
    assert "correct-horse-password-1" in _env_text(home)
    assert "stale-password-00000" not in _env_text(home)
    assert "correct-horse-password-1" not in out  # values never printed
    assert "identity unchanged (kept existing data)" in out
    assert remaining == []


def test_setup_silent_when_mirror_consistent(home: Path, monkeypatch, capsys):
    from tests.observatory.test_setup_wizard import _FakeProvision

    paths = _paths(home)
    creds_path = paths.owner_credentials
    _write_creds(paths)
    provision_mod.mirror_owner_env(home, "@owner:mercury.local",
                                   "old-password-12345")
    fake = _FakeProvision([_provisioned_status(creds_path)])
    fake.describe_owner_env_mismatch = (
        lambda *a, **k: provision_mod.describe_owner_env_mismatch(paths))
    fake.heal_owner_env = lambda *a, **k: provision_mod.heal_owner_env(paths)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, yes_no=[True, True])
    assert "disagrees" not in out
    assert "Healed" not in out
    assert remaining == []


def test_setup_heal_failure_degrades_loudly(home: Path, monkeypatch, capsys):
    from tests.observatory.test_setup_wizard import _FakeProvision

    paths = _paths(home)
    creds_path = paths.owner_credentials
    _write_creds(paths, password="correct-horse-password-1")
    (home / ".env").write_text(
        "MATRIX_OBS_OWNER_PASSWORD=stale-password-00000\n",
        encoding="utf-8",
    )
    fake = _FakeProvision([_provisioned_status(creds_path)])
    fake.describe_owner_env_mismatch = (
        lambda *a, **k: provision_mod.describe_owner_env_mismatch(paths))

    def boom(*a, **k):
        raise provision_mod.ProvisionError("disk is read-only")

    fake.heal_owner_env = boom
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, yes_no=[True, True])
    assert "Could not heal the .env owner mirror" in out
    assert "mercury setup observatory" in out
    assert remaining == []
