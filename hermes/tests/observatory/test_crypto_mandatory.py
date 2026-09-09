"""Defect (ii): crypto is MANDATORY, never best-effort.

- ``assert_crypto_stack`` probes all four sidecar-boot imports
  (olm + mautrix.crypto + aiosqlite + aiohttp); ``ensure_crypto_stack``
  is "ready" only when all four land.
- The setup converge refuses plaintext rooms when E2EE is wanted but
  the stack is missing (that silent downgrade is defect iii's poisoned
  rooms): it defers, and the sidecar retries encrypted on boot.

Real files (tmp home); homeserver + Matrix client faked at the seams.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths


def test_assert_crypto_stack_shape():
    ok, missing = provision_mod.assert_crypto_stack()
    assert set(provision_mod.crypto_import_probe()) == {
        "olm", "mautrix.crypto", "aiosqlite", "aiohttp"}
    assert ok == (missing == [])


def _seed_home(home: Path) -> ObservatoryPaths:
    paths = ObservatoryPaths(home)
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.toml).write_text(
        '[global]\nserver_name = "mercury.local"\n'
        'address = "127.0.0.1"\nport = 18008\n'
        'registration_token = "tok"\n',
        encoding="utf-8",
    )
    (paths.owner_credentials).write_text(json.dumps({
        "homeserver_url": "http://127.0.0.1:18008",
        "user_id": "@owner:mercury.local",
        "password": "old-password-12345",
        "access_token": "admin-tok",
        "device_id": "DEV",
    }), encoding="utf-8")
    paths.appservices_dir.mkdir(parents=True, exist_ok=True)
    (paths.appservice_registration).write_text(
        'id: merc-observatory\nas_token: "as-tok"\nhs_token: "hs-tok"\n',
        encoding="utf-8",
    )
    return paths


class _FakeClient:
    def __init__(self, *a, **k):
        self.registered: list[str] = []

    async def register_virtual_user(self, localpart: str) -> str:
        self.registered.append(localpart)
        return ""

    async def get_profile(self, mxid: str) -> dict:
        return {"displayname": "gateway agent"}


def test_converge_defers_never_plaintext_without_stack(tmp_path, monkeypatch):
    """E2EE wanted + stack missing → deferred, zero rooms created."""
    home = tmp_path / "mhome"
    _seed_home(home)
    monkeypatch.setattr(provision_mod, "_homeserver_reachable", lambda url: True)
    monkeypatch.setattr(
        provision_mod, "assert_crypto_stack", lambda: (False, ["olm"]))
    import observatory.matrix_client as client_mod
    monkeypatch.setattr(client_mod, "MatrixClient", _FakeClient)

    result = provision_mod.verify_and_converge_gateway(home)

    assert result.startswith("deferred: crypto stack missing")
    assert "refusing plaintext rooms" in result
    from observatory.state import ObservatoryState
    state = ObservatoryState(home / "observatory" / "state.db")
    try:
        gw = state.get("gw")
        assert not gw.get("room_id") and not gw.get("space_id")
        try:
            state.get_meta("space:gw-agent")
            raise AssertionError("gw-agent space must not exist after deferral")
        except Exception as exc:
            assert "no meta" in str(exc).lower() or "StateError" in type(exc).__name__
    finally:
        state.close()
