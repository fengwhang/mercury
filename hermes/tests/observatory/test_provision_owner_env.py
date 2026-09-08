"""Owner .env mirror: ``ensure_owner_account`` writes the owner credentials to
``owner-credentials.json`` (0600) AND mirrors the user id + password into
``$MERCURY_HOME/.env`` as ``MATRIX_OBS_OWNER_*`` (0600, replace in place).

Laws: create-if-missing, replace-in-place with neighbors kept, heal-only
(``only_missing``) on the idempotent 'exists' path, 0600, and never logged.
"""

from __future__ import annotations

import json

from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths


MXID = "@owner:mercury.local"
PASSWORD = "s3cr3t-observatory-owner-password"


def test_mirror_owner_env_creates_env_0600(tmp_path):
    home = tmp_path / "mhome"
    provision_mod.mirror_owner_env(home, "@owner:mercury.local", "pw-abc-123")
    env = home / ".env"
    text = env.read_text(encoding="utf-8")
    assert "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local" in text
    assert "MATRIX_OBS_OWNER_PASSWORD=pw-abc-123" in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_mirror_owner_env_replaces_in_place_keeps_neighbors(tmp_path):
    home = tmp_path / "mhome"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "OPENAI_API_KEY=sk-old\n"
        "MATRIX_OBS_OWNER_USER_ID=@stale:mercury.local\n"
        "MATRIX_OBS_OWNER_PASSWORD=stale-pw\n"
        "OTHER=1\n",
        encoding="utf-8",
    )
    provision_mod.mirror_owner_env(home, "@owner:mercury.local", "fresh-pw")
    lines = (home / ".env").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "OPENAI_API_KEY=sk-old"
    assert lines[1] == "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local"
    assert lines[2] == "MATRIX_OBS_OWNER_PASSWORD=fresh-pw"
    assert lines[3] == "OTHER=1"
    assert sum(1 for ln in lines if "MATRIX_OBS_OWNER_PASSWORD" in ln) == 1


def test_mirror_owner_env_only_missing_never_overwrites(tmp_path):
    home = tmp_path / "mhome"
    home.mkdir(parents=True)
    (home / ".env").write_text("MATRIX_OBS_OWNER_PASSWORD=keepme\n", encoding="utf-8")
    provision_mod.mirror_owner_env(
        home, "@owner:mercury.local", "new-pw", only_missing=True
    )
    text = (home / ".env").read_text(encoding="utf-8")
    assert "MATRIX_OBS_OWNER_PASSWORD=keepme" in text
    assert "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local" in text


def test_mirror_owner_env_never_logs_values(tmp_path, capsys, caplog):
    home = tmp_path / "mhome"
    secret = "zz-never-log-9f8-secret"
    with caplog.at_level("DEBUG"):
        provision_mod.mirror_owner_env(home, "@owner:mercury.local", secret)
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    assert secret not in caplog.text


def test_ensure_owner_account_exists_heals_missing_env_keys(tmp_path):
    """Pre-mirror installs gain .env keys on re-provision; values kept."""
    home = tmp_path / "mhome"
    obs_dir = home / "observatory"
    obs_dir.mkdir(parents=True)
    (obs_dir / "owner-credentials.json").write_text(
        json.dumps({"user_id": MXID, "password": PASSWORD}), encoding="utf-8"
    )
    (home / ".env").write_text("MATRIX_OBS_OWNER_PASSWORD=keepme\n", encoding="utf-8")
    assert provision_mod.ensure_owner_account(ObservatoryPaths(home)) == "exists"
    text = (home / ".env").read_text(encoding="utf-8")
    assert "MATRIX_OBS_OWNER_PASSWORD=keepme" in text
    assert f"MATRIX_OBS_OWNER_USER_ID={MXID}" in text
