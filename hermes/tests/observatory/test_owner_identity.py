"""Owner identity: setup-chosen server-name / localpart / password.

Laws:
- validation accepts sane values (normalizing case/whitespace) and
  rejects bad grammar with a reason (ValueError, never silent);
- the chosen identity persists into owner-credentials.json + the .env
  mirror on fresh installs;
- idempotent re-runs keep stored credentials (agreeing explicit values
  are a no-op; differing ones fail hard with a remedy — never a silent
  fork);
- password rotation hits the live homeserver first and rewrites local
  state only on success (a failed rotation never diverges).

No real homeserver or binary: the bootstrap spawn + HTTP are faked at
the provision seams (subprocess.Popen, _wait_for_homeserver, _http_json).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("mercury.local", "mercury.local"),
    (" Mercury.Local ", "mercury.local"),
    ("localhost", "localhost"),
    ("my-box.lan", "my-box.lan"),
    ("mercury.local:18008", "mercury.local:18008"),
    ("a", "a"),
])
def test_validate_server_name_accepts(raw, expected):
    assert provision_mod.validate_server_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "has space.local",
    "under_score.local",
    "-leading.local",
    "trailing-.local",
    ".leading-dot.local",
    "trailing-dot.local.",
    "empty..label",
    "bad!char.local",
    "x" * 256,
    "host:port Nad",
    "host:999999",
])
def test_validate_server_name_rejects(raw):
    with pytest.raises(ValueError):
        provision_mod.validate_server_name(raw)


@pytest.mark.parametrize("raw,expected", [
    ("owner", "owner"),
    ("merc-owner", "merc-owner"),
    (" Alice.Bob_1 ", "alice.bob_1"),
    ("a=b-c+d/e", "a=b-c+d/e"),
    ("x", "x"),
])
def test_validate_owner_localpart_accepts(raw, expected):
    assert provision_mod.validate_owner_localpart(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "has space",
    "UPPER!",  # case normalizes, but ! is outside the grammar
    "@mention",
    "merc_bot",  # appservice's exclusive namespace
    "merc_anything",
    "_leading",
    "x" * 256,
])
def test_validate_owner_localpart_rejects(raw):
    with pytest.raises(ValueError):
        provision_mod.validate_owner_localpart(raw)


def test_validate_owner_password_floor():
    assert provision_mod.validate_owner_password("twelve-chars!") == "twelve-chars!"
    with pytest.raises(ValueError):
        provision_mod.validate_owner_password("short")
    with pytest.raises(ValueError):
        provision_mod.validate_owner_password("   ")
    with pytest.raises(ValueError):
        provision_mod.validate_owner_password("ownername12345", localpart="OwnerName12345")


def test_generate_owner_password_shape():
    first = provision_mod.generate_owner_password()
    second = provision_mod.generate_owner_password()
    assert len(first) >= 32
    assert first != second


# ---------------------------------------------------------------------------
# credential reader
# ---------------------------------------------------------------------------


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


def test_read_owner_credentials_absent_is_none(tmp_path: Path):
    assert provision_mod.read_owner_credentials(_paths(tmp_path)) is None


def test_read_owner_credentials_roundtrip(tmp_path: Path):
    paths = _paths(tmp_path)
    doc = _write_creds(paths)
    assert provision_mod.read_owner_credentials(paths) == doc


def test_read_owner_credentials_corrupt_fails_hard(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.owner_credentials.write_text("{not json", encoding="utf-8")
    with pytest.raises(provision_mod.ProvisionError, match="re-provision"):
        provision_mod.read_owner_credentials(paths)


def test_read_owner_credentials_missing_user_id_fails_hard(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.owner_credentials.write_text('{"password": "x"}', encoding="utf-8")
    with pytest.raises(provision_mod.ProvisionError, match="no user_id"):
        provision_mod.read_owner_credentials(paths)


# ---------------------------------------------------------------------------
# ensure_config server-name
# ---------------------------------------------------------------------------


def _toml_server(paths: ObservatoryPaths) -> str:
    import tomllib

    return tomllib.loads(paths.toml.read_text(encoding="utf-8"))["global"]["server_name"]


def test_ensure_config_fresh_uses_default_server(tmp_path: Path):
    paths = _paths(tmp_path)
    assert provision_mod.ensure_config(paths, "tok") == "created"
    assert _toml_server(paths) == "mercury.local"


def test_ensure_config_fresh_uses_chosen_server(tmp_path: Path):
    paths = _paths(tmp_path)
    assert provision_mod.ensure_config(paths, "tok", server_name="My-Box.lan") == "created"
    assert _toml_server(paths) == "my-box.lan"


def test_ensure_config_rejects_bad_server_upfront(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid server name"):
        provision_mod.ensure_config(_paths(tmp_path), "tok", server_name="bad name!")


def test_ensure_config_existing_agreeing_name_keeps(tmp_path: Path):
    paths = _paths(tmp_path)
    provision_mod.ensure_config(paths, "tok", server_name="my-box.lan")
    assert provision_mod.ensure_config(paths, "tok", server_name="my-box.lan") == "kept"


def test_ensure_config_existing_differing_name_fails_hard(tmp_path: Path):
    paths = _paths(tmp_path)
    provision_mod.ensure_config(paths, "tok", server_name="my-box.lan")
    with pytest.raises(provision_mod.ProvisionError, match="already pins"):
        provision_mod.ensure_config(paths, "tok", server_name="other.lan")
    assert _toml_server(paths) == "my-box.lan"  # untouched


def test_ensure_config_existing_no_name_claimed_keeps(tmp_path: Path):
    paths = _paths(tmp_path)
    provision_mod.ensure_config(paths, "tok", server_name="my-box.lan")
    assert provision_mod.ensure_config(paths, "tok") == "kept"


# ---------------------------------------------------------------------------
# ensure_owner_account identity (faked bootstrap)
# ---------------------------------------------------------------------------


@pytest.fixture()
def bootstrap(monkeypatch):
    """Fake the owner-bootstrap spawn + homeserver HTTP."""
    calls: dict = {}
    monkeypatch.setattr(provision_mod, "_systemctl_available", lambda: False)
    monkeypatch.setattr(provision_mod, "_wait_for_homeserver", lambda url: None)

    def fake_http(method, url, payload=None, token=None):
        calls["request"] = {
            "method": method, "url": url,
            "payload": payload, "token": token,
        }
        return 200, {
            "user_id": f"@{payload['username']}:mercury.local",
            "access_token": "fresh-admin-tok",
            "device_id": "FAKEDEV",
        }

    monkeypatch.setattr(provision_mod, "_http_json", fake_http)
    monkeypatch.setattr(
        provision_mod.subprocess, "Popen",
        lambda *a, **k: SimpleNamespace(
            terminate=lambda: None, wait=lambda timeout=None: 0),
    )
    return calls


def _provisioned_home(tmp_path: Path, monkeypatch, localpart="merc-owner",
                      password="generated-secret-password-AAAA") -> ObservatoryPaths:
    """A home whose toml + binary exist (ready for owner bootstrap)."""
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    paths = _paths(tmp_path)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.binary.touch()
    provision_mod.ensure_config(paths, "tok")
    return paths


def test_owner_bootstrap_persists_chosen_identity(tmp_path: Path, monkeypatch, bootstrap):
    paths = _provisioned_home(tmp_path, monkeypatch)
    assert provision_mod.ensure_owner_account(
        paths, "Custom.User", "my-very-strong-password") == "created"
    req = bootstrap["request"]
    assert req["payload"]["username"] == "custom.user"
    assert req["payload"]["password"] == "my-very-strong-password"
    stored = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    assert stored["user_id"] == "@custom.user:mercury.local"
    assert stored["password"] == "my-very-strong-password"
    assert oct(paths.owner_credentials.stat().st_mode & 0o777) == "0o600"
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MATRIX_OBS_OWNER_USER_ID=@custom.user:mercury.local" in env
    assert "MATRIX_OBS_OWNER_PASSWORD=my-very-strong-password" in env


def test_owner_bootstrap_defaults_when_unprompted(tmp_path: Path, monkeypatch, bootstrap):
    paths = _provisioned_home(tmp_path, monkeypatch)
    assert provision_mod.ensure_owner_account(paths) == "created"
    req = bootstrap["request"]
    assert req["payload"]["username"] == "merc-owner"
    assert len(req["payload"]["password"]) >= 32


def test_owner_exists_keeps_credentials(tmp_path: Path, monkeypatch, bootstrap):
    paths = _provisioned_home(tmp_path, monkeypatch)
    provision_mod.ensure_owner_account(paths, "keep.me", "keep-me-password-123")
    # idempotent re-run: same values, defaults, all keep without re-registering
    calls_before = len(bootstrap)
    assert provision_mod.ensure_owner_account(paths, "keep.me", "keep-me-password-123") == "exists"
    assert provision_mod.ensure_owner_account(paths) == "exists"
    assert len(bootstrap) == calls_before
    stored = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    assert stored["user_id"] == "@keep.me:mercury.local"
    assert stored["password"] == "keep-me-password-123"


def test_owner_exists_rejects_rename(tmp_path: Path, monkeypatch, bootstrap):
    paths = _provisioned_home(tmp_path, monkeypatch)
    provision_mod.ensure_owner_account(paths, "keep.me", "keep-me-password-123")
    with pytest.raises(provision_mod.ProvisionError, match="already provisioned as"):
        provision_mod.ensure_owner_account(paths, "someone.else")
    with pytest.raises(provision_mod.ProvisionError, match="rotate"):
        provision_mod.ensure_owner_account(paths, "keep.me", "different-password-1")


def test_owner_bootstrap_rejects_weak_password_without_touching_server(
        tmp_path: Path, monkeypatch, bootstrap):
    paths = _provisioned_home(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="at least"):
        provision_mod.ensure_owner_account(paths, "new.user", "short")
    assert bootstrap == {}
    assert not paths.owner_credentials.exists()


# ---------------------------------------------------------------------------
# provision() upfront identity validation
# ---------------------------------------------------------------------------


def test_provision_rejects_bad_identity_before_binary_step(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="invalid server name"):
        provision_mod.provision(tmp_path, server_name="bad name!")
    with pytest.raises(ValueError, match="at least"):
        provision_mod.provision(tmp_path, owner_password="short")


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------


def _creds_with_env(tmp_path: Path, monkeypatch) -> ObservatoryPaths:
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    paths = _paths(tmp_path)
    _write_creds(paths)
    (tmp_path / ".env").write_text(
        "MATRIX_OBS_OWNER_USER_ID=@owner:mercury.local\n"
        "MATRIX_OBS_OWNER_PASSWORD=old-password-12345\n",
        encoding="utf-8",
    )
    return paths


def test_rotate_success_rewrites_both_mirrors(tmp_path: Path, monkeypatch):
    paths = _creds_with_env(tmp_path, monkeypatch)
    calls: dict = {}

    def fake_http(method, url, payload=None, token=None):
        calls.update(method=method, url=url, payload=payload, token=token)
        return 200, {}

    assert provision_mod.rotate_owner_password(
        "brand-new-password-1", paths, http=fake_http) == "rotated"
    assert calls["method"] == "PUT"
    assert calls["url"] == (
        "http://127.0.0.1:18008/_synapse/admin/v2/users/%40owner%3Amercury.local")
    assert calls["payload"] == {"password": "brand-new-password-1",
                                "logout_devices": False}
    assert calls["token"] == "admin-tok"
    stored = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    assert stored["password"] == "brand-new-password-1"
    assert stored["user_id"] == "@owner:mercury.local"
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MATRIX_OBS_OWNER_PASSWORD=brand-new-password-1" in env
    assert "old-password-12345" not in env


def test_rotate_unprovisioned_fails_hard(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))

    def boom(*a, **k):
        raise AssertionError("no server call without credentials")

    with pytest.raises(provision_mod.ProvisionError, match="not provisioned"):
        provision_mod.rotate_owner_password(
            "brand-new-password-1", _paths(tmp_path), http=boom)


def test_rotate_weak_password_never_calls_server(tmp_path: Path, monkeypatch):
    paths = _creds_with_env(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise AssertionError("weak password must not reach the server")

    with pytest.raises(ValueError, match="at least"):
        provision_mod.rotate_owner_password("short", paths, http=boom)
    stored = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    assert stored["password"] == "old-password-12345"


def test_rotate_server_rejection_keeps_local_state(tmp_path: Path, monkeypatch):
    paths = _creds_with_env(tmp_path, monkeypatch)
    before_creds = paths.owner_credentials.read_text(encoding="utf-8")
    before_env = (tmp_path / ".env").read_text(encoding="utf-8")

    def deny(method, url, payload=None, token=None):
        return 403, {"errcode": "M_FORBIDDEN", "error": "not admin"}

    with pytest.raises(provision_mod.ProvisionError, match="HTTP 403"):
        provision_mod.rotate_owner_password(
            "brand-new-password-1", paths, http=deny)
    assert paths.owner_credentials.read_text(encoding="utf-8") == before_creds
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before_env


def test_rotate_connection_failure_keeps_local_state(tmp_path: Path, monkeypatch):
    paths = _creds_with_env(tmp_path, monkeypatch)

    def down(*a, **k):
        raise provision_mod.ProvisionError("PUT http://127.0.0.1:18008 failed: refused")

    with pytest.raises(provision_mod.ProvisionError, match="refused"):
        provision_mod.rotate_owner_password(
            "brand-new-password-1", paths, http=down)
    stored = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    assert stored["password"] == "old-password-12345"


def test_rotate_missing_admin_token_fails_hard(tmp_path: Path, monkeypatch):
    paths = _creds_with_env(tmp_path, monkeypatch)
    doc = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    del doc["access_token"]
    paths.owner_credentials.write_text(json.dumps(doc), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("no token, no call")

    with pytest.raises(provision_mod.ProvisionError, match="no admin access token"):
        provision_mod.rotate_owner_password(
            "brand-new-password-1", paths, http=boom)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_rejects_bad_server_name(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    rc = provision_mod.main(["--mercury-home", str(tmp_path),
                             "--server-name", "bad name!",
                             "--no-systemd"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "invalid server name" in out
