"""VM-report slice 2: identity rotation applies on Tuwunel (real-file repro).

The VM path: provisioned data exists (owner-credentials.json + .env mirror),
the user enters a new password, and NOTHING changes (.env byte-identical).
Root cause: rotation spoke ONLY the Synapse admin ``PUT
/_synapse/admin/v2/users/`` surface, which Tuwunel does not implement (404 /
M_UNRECOGNIZED) — the write died before the atomic dual-write, so both
mirrors kept the old secret. A stale admin token (401 / M_UNKNOWN_TOKEN)
killed it the same way.

Fix: admin PUT first; 401 → re-login with the stored password + one retry;
endpoint-absent (404/405/M_UNRECOGNIZED) → client ``/account/password``
UIAA change with the stored password (incl. UIAA session retry). Mirrors
still move only after the server accepts AND the new password logs in.

Real files throughout (tmp MERCURY_HOME); only HTTP is faked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths

BASE = "http://127.0.0.1:18008"
USER = "@owner:mercury.local"
OLD = "old-password-12345"
NEW = "brand-new-password-1"


def _paths(tmp_path: Path) -> ObservatoryPaths:
    return ObservatoryPaths(tmp_path)


def _provisioned(tmp_path: Path, monkeypatch, **over) -> ObservatoryPaths:
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    paths = _paths(tmp_path)
    doc = {
        "homeserver_url": BASE,
        "user_id": USER,
        "password": OLD,
        "access_token": "admin-tok",
        "device_id": "DEV",
    }
    doc.update(over)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.owner_credentials.write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"MATRIX_OBS_OWNER_USER_ID={USER}\nMATRIX_OBS_OWNER_PASSWORD={OLD}\n",
        encoding="utf-8",
    )
    return paths


def _stored(paths: ObservatoryPaths) -> dict:
    return json.loads(paths.owner_credentials.read_text(encoding="utf-8"))


def test_vm_path_admin404_rotates_via_client_uiaa(tmp_path, monkeypatch):
    """EXACT VM repro: Tuwunel 404s the admin PUT → rotation still applies."""
    paths = _provisioned(tmp_path, monkeypatch)
    calls: list = []

    def fake_http(method, url, payload=None, token=None):
        calls.append((method, url))
        if method == "PUT":
            return 404, {"errcode": "M_UNRECOGNIZED", "error": "unknown endpoint"}
        assert url == f"{BASE}/_matrix/client/v3/account/password"
        assert token == "admin-tok"
        auth = (payload or {}).get("auth") or {}
        assert (payload or {}).get("new_password") == NEW
        assert auth.get("password") == OLD
        assert auth.get("identifier") == {"type": "m.id.user", "user": USER}
        return 200, {}

    orig_login = provision_mod.verify_owner_login

    def fake_login(base_url, user_id, password, *, http=None):
        assert password == NEW
        return {"access_token": "login-tok", "user_id": USER}

    monkeypatch.setattr(provision_mod, "verify_owner_login", fake_login)
    assert provision_mod.rotate_owner_password(NEW, paths, http=fake_http) == "rotated"

    stored = _stored(paths)
    assert stored["password"] == NEW
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"MATRIX_OBS_OWNER_PASSWORD={NEW}" in env
    assert OLD not in env
    assert ("POST", f"{BASE}/_matrix/client/v3/account/password") in calls


def test_admin_happy_path_never_touches_client_api(tmp_path, monkeypatch):
    paths = _provisioned(tmp_path, monkeypatch)
    calls: list = []

    def fake_http(method, url, payload=None, token=None):
        calls.append((method, url))
        if method == "PUT":
            assert url.endswith("/_synapse/admin/v2/users/%40owner%3Amercury.local")
            assert token == "admin-tok"
            return 200, {}
        raise AssertionError(f"unexpected call {method} {url}")

    def fake_login(base_url, user_id, password, *, http=None):
        assert password == NEW
        return {"access_token": "t", "user_id": USER}

    monkeypatch.setattr(provision_mod, "verify_owner_login", fake_login)
    assert provision_mod.rotate_owner_password(NEW, paths, http=fake_http) == "rotated"
    assert all("account/password" not in u for _m, u in calls)
    assert _stored(paths)["password"] == NEW


def test_stale_admin_token_refreshes_then_rotates(tmp_path, monkeypatch):
    """401/M_UNKNOWN_TOKEN → re-login with stored password, retry, persist."""
    paths = _provisioned(tmp_path, monkeypatch)
    puts: list = []

    def fake_http(method, url, payload=None, token=None):
        if method == "PUT":
            puts.append(token)
            if token == "admin-tok":
                return 401, {"errcode": "M_UNKNOWN_TOKEN", "error": "stale"}
            assert token == "fresh-tok"
            return 200, {}
        assert method == "POST" and url.endswith("/login")
        if (payload or {}).get("password") == OLD:
            return 200, {"access_token": "fresh-tok", "user_id": USER,
                         "device_id": "NEWDEV"}
        assert (payload or {}).get("password") == NEW
        return 200, {"access_token": "login-tok", "user_id": USER}

    assert provision_mod.rotate_owner_password(NEW, paths, http=fake_http) == "rotated"
    assert puts == ["admin-tok", "fresh-tok"]
    stored = _stored(paths)
    assert stored["password"] == NEW
    assert stored["access_token"] == "fresh-tok"
    assert stored["device_id"] == "NEWDEV"


def test_uiaa_session_challenge_retried(tmp_path, monkeypatch):
    paths = _provisioned(tmp_path, monkeypatch)
    posts: list = []

    def fake_http(method, url, payload=None, token=None):
        if method == "PUT":
            return 404, {"errcode": "M_UNRECOGNIZED"}
        if url.endswith("/account/password"):
            posts.append(dict((payload or {}).get("auth") or {}))
            if len(posts) == 1:
                return 401, {"errcode": "M_UNAUTHORIZED", "session": "sess-1",
                             "flows": [{"stages": ["m.login.password"]}], }
            assert posts[1].get("session") == "sess-1"
            return 200, {}
        raise AssertionError(f"unexpected {method} {url}")

    def fake_login(base_url, user_id, password, *, http=None):
        return {"access_token": "t", "user_id": USER}

    monkeypatch.setattr(provision_mod, "verify_owner_login", fake_login)
    assert provision_mod.rotate_owner_password(NEW, paths, http=fake_http) == "rotated"
    assert len(posts) == 2
    assert _stored(paths)["password"] == NEW


def test_hard_failure_leaves_both_mirrors_identical(tmp_path, monkeypatch):
    """A truly failed rotation stays loud AND byte-identical (never silent)."""
    paths = _provisioned(tmp_path, monkeypatch)
    before_creds = paths.owner_credentials.read_bytes()
    before_env = (tmp_path / ".env").read_bytes()

    def fake_http(method, url, payload=None, token=None):
        if method == "PUT":
            return 404, {"errcode": "M_UNRECOGNIZED"}
        return 403, {"errcode": "M_FORBIDDEN", "error": "bad password"}

    with pytest.raises(provision_mod.ProvisionError, match="password rotation failed"):
        provision_mod.rotate_owner_password(NEW, paths, http=fake_http)
    assert paths.owner_credentials.read_bytes() == before_creds
    assert (tmp_path / ".env").read_bytes() == before_env


def test_heal_valid_and_refreshed(tmp_path, monkeypatch):
    paths = _provisioned(tmp_path, monkeypatch)

    def ok_http(method, url, payload=None, token=None):
        assert url.endswith("/account/whoami")
        return 200, {"user_id": USER}

    assert provision_mod.heal_owner_admin_token(paths, http=ok_http) == "valid"

    def stale_http(method, url, payload=None, token=None):
        if url.endswith("/account/whoami"):
            return 401, {"errcode": "M_UNKNOWN_TOKEN"}
        assert (payload or {}).get("password") == OLD
        return 200, {"access_token": "healed-tok", "user_id": USER}

    assert provision_mod.heal_owner_admin_token(paths, http=stale_http) == "refreshed"
    assert _stored(paths)["access_token"] == "healed-tok"
