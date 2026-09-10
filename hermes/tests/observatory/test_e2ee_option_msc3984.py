"""Regression tests: e2ee setup option + MSC3984 key surface + notices.

Covers the feature-request acceptance set:

* option persists — the encrypt prompt's plaintext opt-in writes
  ``observatory.e2ee: false`` (with the tailnet warning); answering Yes
  keeps the default untouched;
* gates skip when off — crypto / encrypted-room / poison-scan report
  ``skipped`` instead of failing on plaintext rooms;
* synthetic key query/claim 200 with REAL key material — a TestClient
  POST against ``make_app`` with a live ``E2EEManager`` behind it: the
  device-key self-signature cryptographically verifies and successive
  claims mint distinct one-time keys (a stub could never do either);
* notice text — the Element X recovery wording without the old fiction.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

import mercury_cli.setup as setup_mod
from mercury_cli.config import get_config_path, load_config
from observatory import provision as provision_mod
from observatory.appservice import (
    KEY_CLAIM_PATH,
    KEY_QUERY_PATH,
    TransactionIntake,
    make_app,
)
from observatory.e2ee import (
    DECRYPT_RECOVERY_STEPS,
    E2EEManager,
    decrypt_failure_notice,
)
from observatory.state import ObservatoryState

TOKEN = "test-msc3984-token"
GW = "@merc_gw:hs"


# ---------------------------------------------------------------------------
# fakes (duck-typed transport: canned login token + key counts so
# ``load()``/``share_keys()`` never hit the network)
# ---------------------------------------------------------------------------


class FakeTransport:
    def __init__(self):
        self.calls: list = []

    async def client_api(self, method, path, *, sender=None, params=None,
                         json_body=None):
        self.calls.append(("api", method, path))
        if path.endswith("/login"):
            return {"access_token": "tok123", "device_id": "OBSVX"}
        return {"event_id": "$x"}

    async def _request(self, method, path, *, token=None, json_body=None):
        self.calls.append(("req", method, path))
        if path.endswith("/keys/upload"):
            return {"one_time_key_counts": {"signed_curve25519": 50}}
        return {}


def _manager(tmp_path: Path) -> E2EEManager:
    return E2EEManager(
        FakeTransport(), ObservatoryState(tmp_path / "state.db"),
        crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs",
        gateway_mxid=GW,
    )


# ---------------------------------------------------------------------------
# option persists
# ---------------------------------------------------------------------------


class TestEncryptPromptPersists:
    def test_opt_out_warns_and_writes_false(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda q, default=True: False)
        config: dict = {}
        setup_mod._prompt_observatory_encrypt_rooms(config)
        out = capsys.readouterr().out
        assert "PLAINTEXT OPT-IN" in out
        assert "UNENCRYPTED" in out
        assert "observatory.e2ee = false written to" in out
        assert config["observatory"]["e2ee"] is False
        reloaded = load_config()
        assert reloaded["observatory"]["e2ee"] is False

    def test_opt_in_keeps_default_untouched(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda q, default=True: True)
        config: dict = {}
        setup_mod._prompt_observatory_encrypt_rooms(config)
        out = capsys.readouterr().out
        assert "Keeping observatory.e2ee = true" in out
        assert "e2ee" not in config.get("observatory", {})
        assert not get_config_path().exists()

    def test_provision_round_trip_on_throwaway_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
        provision_mod.set_observatory_e2ee(False)
        assert provision_mod.observatory_e2ee_flag() is False
        from observatory.e2ee import e2ee_enabled

        assert e2ee_enabled(tmp_path) is False
        provision_mod.set_observatory_e2ee(True)
        assert provision_mod.observatory_e2ee_flag() is True


# ---------------------------------------------------------------------------
# gates skip when off (+ card states plaintext plainly)
# ---------------------------------------------------------------------------


class _FakeObs:
    def assert_crypto_stack(self):
        return (True, [])

    def gateway_room_encrypted(self):
        return True

    def heal_owner_admin_token(self):
        return "valid"

    def scan_poisoned_rooms(self):
        return []

    def current_bind_addresses(self):
        return []


def _run_acceptance(monkeypatch, capsys, *, e2ee: bool):
    monkeypatch.setattr(
        provision_mod, "observatory_e2ee_flag", lambda *a, **k: e2ee)
    monkeypatch.setattr(setup_mod, "_self_test_model",
                        lambda: ("acme/widget", "acme"))
    monkeypatch.setattr(setup_mod, "_inject_ping", lambda *a, **k: "passed: pong")
    monkeypatch.setattr(setup_mod, "_synthetic_transaction", lambda: "passed")
    ok = setup_mod._run_observatory_acceptance(
        _FakeObs(), {"homeserver_reachable": False}, None)
    return ok, capsys.readouterr().out


class TestAcceptanceSkipsWhenOff:
    def test_crypto_gates_skip_on_plaintext(self, monkeypatch, capsys):
        ok, out = _run_acceptance(monkeypatch, capsys, e2ee=False)
        assert ok is True
        assert "[acceptance] crypto: skipped: E2EE disabled" in out
        assert "[acceptance] encrypted-room: skipped: E2EE disabled" in out
        assert "[acceptance] poison-scan: skipped: E2EE disabled" in out

    def test_crypto_gates_run_when_on(self, monkeypatch, capsys):
        ok, out = _run_acceptance(monkeypatch, capsys, e2ee=True)
        assert ok is True
        assert "[acceptance] crypto: passed:" in out
        assert "[acceptance] encrypted-room: passed:" in out
        assert "[acceptance] poison-scan: passed:" in out

    def test_card_states_plaintext_plainly(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(
            provision_mod, "observatory_e2ee_flag", lambda *a, **k: False)
        assert setup_mod._e2ee_card_line().startswith("OFF (PLAINTEXT")
        monkeypatch.setattr(
            provision_mod, "observatory_e2ee_flag", lambda *a, **k: True)
        assert setup_mod._e2ee_card_line().startswith("ON (Megolm")


# ---------------------------------------------------------------------------
# synthetic key query/claim: 200 with REAL key material
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def served():
    """TestClient over make_app with a LIVE E2EEManager behind the routes."""
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="msc3984-"))
    mgr = _manager(home)
    intake = TransactionIntake(
        as_token=TOKEN,
        key_query_handler=mgr.key_query,
        key_claim_handler=mgr.key_claim,
    )
    app = make_app(intake)
    async with TestClient(TestServer(app)) as client:
        yield client, mgr
    # Release the crypto SQLite handles (aiosqlite worker threads block
    # interpreter shutdown while a store stays open — same law as the
    # sidecar's shutdown path).
    await mgr.stop()


def _auth(path: str) -> str:
    return f"{path}?access_token={TOKEN}"


class TestMSC3984RealMaterial:
    @pytest.mark.asyncio
    async def test_query_200_with_verifiable_device_keys(self, served):
        client, mgr = served
        resp = await client.post(
            _auth(KEY_QUERY_PATH), json={"device_keys": {GW: []}})
        assert resp.status == 200
        body = await resp.json()
        assert body["failures"] == {}
        devices = body["device_keys"][GW]
        crypto = mgr.machine_for(GW)
        assert list(devices) == [str(crypto.device_id)]
        obj = devices[str(crypto.device_id)]
        assert obj["user_id"] == GW
        assert obj["algorithms"] == [
            "m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"]
        # real material: keys match the live account, self-signature verifies
        assert obj["keys"] == {
            f"curve25519:{crypto.device_id}":
                crypto.machine.account.identity_keys["curve25519"],
            f"ed25519:{crypto.device_id}":
                crypto.machine.account.identity_keys["ed25519"],
        }
        from mautrix.crypto.signature import verify_signature_json

        assert verify_signature_json(
            dict(obj), GW, crypto.device_id,
            crypto.machine.account.signing_key) is True

    @pytest.mark.asyncio
    async def test_claim_200_with_fresh_otks(self, served):
        client, mgr = served
        crypto = mgr.machine_for(GW)
        await crypto.load()
        dev = str(crypto.device_id)
        first, second = None, None
        for slot in ("first", "second"):
            resp = await client.post(
                _auth(KEY_CLAIM_PATH),
                json={"one_time_keys": {GW: {dev: "signed_curve25519"}}})
            assert resp.status == 200
            body = await resp.json()
            assert body["failures"] == {}
            claimed = body["one_time_keys"][GW][dev]
            assert len(claimed) == 1
            key_id, val = next(iter(claimed.items()))
            assert key_id.startswith("signed_curve25519:")
            assert val["key"]
            assert val["signatures"][GW][f"ed25519:{dev}"]
            if slot == "first":
                first = key_id
            else:
                second = key_id
        # live minting, not a canned stub: successive claims differ
        assert first != second

    @pytest.mark.asyncio
    async def test_foreign_users_omitted_not_failed(self, served):
        client, _mgr = served
        resp = await client.post(
            _auth(KEY_QUERY_PATH), json={"device_keys": {"@alice:hs": []}})
        assert resp.status == 200
        assert (await resp.json())["device_keys"] == {}
        resp = await client.post(
            _auth(KEY_CLAIM_PATH),
            json={"one_time_keys": {"@alice:hs": {"DEV": "signed_curve25519"}}})
        assert resp.status == 200
        assert (await resp.json())["one_time_keys"] == {}

    @pytest.mark.asyncio
    async def test_unknown_device_claim_fails(self, served):
        client, _mgr = served
        resp = await client.post(
            _auth(KEY_CLAIM_PATH),
            json={"one_time_keys": {GW: {"NOPE": "signed_curve25519"}}})
        assert resp.status == 200
        body = await resp.json()
        assert body["one_time_keys"] == {}
        assert "NOPE" in body["failures"][GW]

    @pytest.mark.asyncio
    async def test_key_routes_require_token(self, served):
        client, _mgr = served
        for path, payload in (
            (KEY_QUERY_PATH, {"device_keys": {GW: []}}),
            (KEY_CLAIM_PATH, {"one_time_keys": {GW: {}}}),
        ):
            resp = await client.post(path, json=payload)
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_detached_handlers_404_honestly(self):
        intake = TransactionIntake(as_token=TOKEN)
        app = make_app(intake)
        async with TestClient(TestServer(app)) as client:
            for path, payload in (
                (KEY_QUERY_PATH, {"device_keys": {GW: []}}),
                (KEY_CLAIM_PATH, {"one_time_keys": {GW: {}}}),
            ):
                resp = await client.post(_auth(path), json=payload)
                assert resp.status == 404
                assert (await resp.json())["errcode"] == "M_NOT_FOUND"


# ---------------------------------------------------------------------------
# notice text (Element X)
# ---------------------------------------------------------------------------


class TestElementXNoticeText:
    def test_recovery_names_no_fiction(self):
        assert "FluffyChat" not in DECRYPT_RECOVERY_STEPS
        assert "request keys" not in DECRYPT_RECOVERY_STEPS
        assert "verify the gateway-agent device" not in DECRYPT_RECOVERY_STEPS

    def test_recovery_names_real_steps(self):
        for step in ("force-close Element X", "FRESH message", "re-converge",
                     "ROTATE the Olm identity", "UNRECOVERABLE by design"):
            assert step in DECRYPT_RECOVERY_STEPS, step

    def test_notice_carries_event_room_and_steps(self):
        text = decrypt_failure_notice("$ev9", "!room:y")
        assert "$ev9" in text and "!room:y" in text
        assert DECRYPT_RECOVERY_STEPS in text
