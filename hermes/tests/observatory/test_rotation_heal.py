"""Claim-verify guard + pending-trust approval flow (rotation-heal).

TRUTH (D2 single-owner closed homeserver): an Element/Element X identity
reset replaces the device ed25519 key under the SAME device ID while the
server keeps serving old-signed OTKs. Two defenses, both fail-closed:

* CLAIM-VERIFY GUARD: before ``share_group_session``, one OTK per recipient
  device is claimed and verified against the CURRENT advertised signing key.
  Bad OTKs are excluded from the share and named; zero verified devices
  means no share at all (``failed-stale-pool``, loud warning) — never a
  false success with zero recipients.
* PENDING TRUST: a same-ID key change stays REFUSED (never auto-trusted)
  but becomes actionable — a pending record (old/new fingerprints,
  first-seen) the operator approves via
  ``mercury observatory trust-device --device <id>``. Approval re-trusts
  the new keys and forces Megolm rotation on the next share. Genuinely
  ambiguous keys (two DIFFERENT device IDs colliding) refuse with no
  pending record. No room notice is ever posted for pendings (the room is
  undecryptable in exactly this state); the setup acceptance wizard carries
  the remedy instead.
"""

from __future__ import annotations

import logging
from argparse import Namespace
from types import SimpleNamespace

import pytest

from observatory.e2ee import (
    E2EEManager,
    approve_pending_trust,
    list_pending_trusts,
    record_pending_trust,
    trust_device_command,
)
from observatory.state import ObservatoryState

try:  # crypto fakes need real mautrix types + a real ed25519 verifier
    from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID

    from mautrix.crypto.signature import verify_signature_json  # noqa: F401

    HAVE_STACK = True
except Exception:  # noqa: BLE001 — stack tests skip cleanly without it
    HAVE_STACK = False

needs_stack = pytest.mark.skipif(not HAVE_STACK, reason="mautrix crypto stack missing")

GW = "@merc_gateway:hs"
OWNER = "@owner:hs"
ROOM = "!room:hs"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _device(user, did, identity_key, signing_key, trust=TrustState.UNVERIFIED):
    return DeviceIdentity(
        user_id=UserID(user), device_id=DeviceID(did),
        identity_key=identity_key, signing_key=signing_key,
        trust=trust, deleted=False, name=did)


class FakeStore:
    """Crypto-store surface the trust/share paths touch (devices belong to
    the single tracked ``user`` — like a real sender machine tracking the
    owner; any other user has no devices)."""

    def __init__(self, devices=None, session=None, sessions=(), user=None):
        self.devices = dict(devices or {})
        self._session = session
        self._sessions = set(sessions)  # identity keys with live Olm sessions
        self._user = None if user is None else str(user)
        self.removed: list = []

    def _mine(self, user_id) -> bool:
        return self._user is None or str(user_id) == self._user

    async def get_devices(self, user_id):
        return dict(self.devices) if self._mine(user_id) else {}

    async def get_device(self, user_id, device_id):
        if not self._mine(user_id):
            return None
        for did, dev in self.devices.items():
            if str(did) == str(device_id):
                return dev
        return None

    async def put_device(self, user_id, device):
        self.devices[device.device_id] = device

    async def put_devices(self, user_id, devices):
        self.devices = dict(devices)

    async def has_session(self, key):
        return str(key) in self._sessions

    async def get_outbound_group_session(self, room_id):
        return self._session

    async def remove_outbound_group_session(self, room_id):
        self.removed.append(str(room_id))
        self._session = None


class FakeSession:
    def __init__(self, sid="OLDSESSION", shared=True, expired=False):
        self.id = sid
        self.session_id = sid
        self.shared = shared
        self.expired = expired


class FakeClaimClient:
    """Key-claim/query adapter: ``otks`` maps (user, device) -> OTK dict."""

    def __init__(self, otks=None, raw_keys=None):
        self.otks = dict(otks or {})
        self.raw_keys = raw_keys or {}
        self.claimed: list = []

    async def claim_keys(self, request):
        self.claimed.append(request)
        out: dict = {}
        for user, devs in (request or {}).items():
            for did in (devs or {}):
                otk = self.otks.get((str(user), str(did)), "MISSING")
                if otk != "MISSING" and otk is not None:
                    out.setdefault(user, {}).setdefault(did, {})["k1"] = otk
        return SimpleNamespace(one_time_keys=out, failures={})

    async def query_keys(self, users, token=None):
        return SimpleNamespace(device_keys=dict(self.raw_keys), failures={})


class FakeMachine:
    def __init__(self, store, client, fetched=None):
        self.crypto_store = store
        self.client = client
        self._fetched = fetched or {}
        self.shared: list = []

    async def _fetch_keys(self, users, include_untracked=False):
        return self._fetched

    async def share_group_session(self, room_id, users):
        snap = {}
        for user in users:
            try:
                devs = await self.crypto_store.get_devices(user) or {}
            except Exception:  # noqa: BLE001 — legacy stores
                devs = {}
            snap[str(user)] = sorted(str(d) for d in devs)
        self.shared.append((str(room_id), list(users), snap))
        self.crypto_store._session = FakeSession(sid="NEWSESSION")


class FakeVirtual:
    def __init__(self, mxid, device_id, machine):
        self.mxid = mxid
        self.device_id = device_id
        self.machine = machine

    async def load(self):
        return None


class FakeHomeserver:
    def __init__(self, members):
        self.members = list(members)
        self.calls: list = []

    async def client_api(self, method, path, *, sender=None, params=None,
                         json_body=None):
        self.calls.append((method, path, sender, json_body))
        if method == "GET" and path.rstrip("/").endswith("/members"):
            return {"chunk": [
                {"type": "m.room.member", "state_key": m,
                 "content": {"membership": "join"}} for m in self.members
            ]}
        return {"event_id": "$ev"}


def _manager(tmp_path, *, machines, members):
    server = FakeHomeserver(members)
    state = ObservatoryState(tmp_path / "state.db")
    mgr = E2EEManager(
        server, state, crypto_dir=tmp_path / "crypto",
        owner_mxid=OWNER, gateway_mxid=GW)
    for mxid, virt in machines.items():
        mgr._machines[mxid] = virt
    return mgr


# -- real ed25519 OTKs (pycryptodome only, no Olm account needed) -------------


def _b64(raw: bytes) -> str:
    import unpaddedbase64

    return unpaddedbase64.encode_base64(raw)


def _new_key():
    from Crypto.PublicKey import ECC

    return ECC.generate(curve="ed25519")


def _pub_of(key) -> str:
    return _b64(key.public_key().export_key(format="raw"))


def _signed_otk(user, did, signing_key, *, body_key="OTK-PUB"):
    """OTK dict with a real ed25519 signature verifiable by mautrix."""
    import json as _json

    from Crypto.Signature import eddsa

    body = {"key": f"{body_key}-{did}"}
    data = _json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True)
    sig = eddsa.new(signing_key, "rfc8032").sign(data.encode("utf-8"))
    return {"key": body["key"],
            "signatures": {str(user): {f"ed25519:{did}": _b64(sig)}}}


def _rig(tmp_path, *, owner_devices, fetched=None, otks=None,
         raw_keys=None, session=None, members=(OWNER,), sender_devices=None):
    """One sender (gateway) machine tracking the owner's devices."""
    store = FakeStore(devices=dict(owner_devices), session=session, user=OWNER)
    client = FakeClaimClient(otks=otks, raw_keys=raw_keys)
    machine = FakeMachine(store, client, fetched=fetched)
    mgr = _manager(tmp_path, machines={GW: FakeVirtual(GW, "GWDEV", machine)},
                   members=list(members))
    return mgr, store, client, machine


# ---------------------------------------------------------------------------
# claim-verify guard
# ---------------------------------------------------------------------------


@needs_stack
class TestClaimVerifyGuard:
    @pytest.mark.asyncio
    async def test_excludes_bad_otk_names_it_and_shares_rest(self, tmp_path):
        good_key, bad_key, stale_key = _new_key(), _new_key(), _new_key()
        devs = {
            DeviceID("GOOD"): _device(OWNER, "GOOD", "ik-good", _pub_of(good_key),
                                      TrustState.VERIFIED),
            DeviceID("BAD"): _device(OWNER, "BAD", "ik-bad", _pub_of(bad_key),
                                     TrustState.VERIFIED),
        }
        fetched = {UserID(OWNER): {
            DeviceID("GOOD"): _device(OWNER, "GOOD", "ik-good",
                                      _pub_of(good_key), TrustState.UNVERIFIED),
            DeviceID("BAD"): _device(OWNER, "BAD", "ik-bad",
                                     _pub_of(bad_key), TrustState.UNVERIFIED),
        }}
        otks = {
            (OWNER, "GOOD"): _signed_otk(OWNER, "GOOD", good_key),
            # stale pool: BAD's OTK signed by a key that is NOT its
            # advertised signing key (the pre-reset key).
            (OWNER, "BAD"): _signed_otk(OWNER, "BAD", stale_key),
        }
        mgr, store, client, machine = _rig(
            tmp_path, owner_devices=devs, fetched=fetched, otks=otks)
        report = await mgr.ensure_room_share(ROOM, GW)
        assert report["shared"] == [ROOM]
        assert report["stale_excluded"] == ["BAD"]
        assert "failed-stale-pool" not in report
        # the share itself only saw the verified device
        assert machine.shared and machine.shared[0][2][OWNER] == ["GOOD"]
        # exclusion is temporary — the store is restored afterwards
        assert sorted(str(d) for d in store.devices) == ["BAD", "GOOD"]
        # both devices were actually claim-checked
        claimed_devs = {str(d) for req in client.claimed
                        for devs in req.values() for d in devs}
        assert claimed_devs == {"BAD", "GOOD"}

    @pytest.mark.asyncio
    async def test_zero_verify_fails_loud_without_share(self, tmp_path, caplog):
        key, stale_key = _new_key(), _new_key()
        devs = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-1", _pub_of(key), TrustState.VERIFIED)}
        fetched = {UserID(OWNER): {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-1", _pub_of(key), TrustState.UNVERIFIED)}}
        otks = {(OWNER, "PHONE1"): _signed_otk(OWNER, "PHONE1", stale_key)}
        mgr, _store, _client, machine = _rig(
            tmp_path, owner_devices=devs, fetched=fetched, otks=otks)
        with caplog.at_level(logging.WARNING, logger="observatory.e2ee"):
            report = await mgr.ensure_room_share(ROOM, GW)
        assert report["shared"] == []
        assert report["failed-stale-pool"] == ["PHONE1"]
        assert machine.shared == []  # do NOT share, do NOT log success
        loud = " ".join(r.getMessage() for r in caplog.records)
        assert "STALE OTK POOL" in loud
        assert "PHONE1" in loud
        assert "identity" in loud and "REMEDY" in loud

    @pytest.mark.asyncio
    async def test_session_devices_skip_the_claim(self, tmp_path):
        key = _new_key()
        devs = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-1", _pub_of(key), TrustState.VERIFIED)}
        fetched = {UserID(OWNER): {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-1", _pub_of(key), TrustState.UNVERIFIED)}}
        mgr, _store, client, machine = _rig(
            tmp_path, owner_devices=devs, fetched=fetched, otks={},
            session=None)
        machine.crypto_store._sessions = {"ik-1"}  # live Olm session
        report = await mgr.ensure_room_share(ROOM, GW)
        assert report["shared"] == [ROOM]
        assert report["stale_excluded"] == []
        assert client.claimed == []  # reachable without an OTK: no claim spent


# ---------------------------------------------------------------------------
# pending trust: rotation stays refused, approval re-trusts + rotates
# ---------------------------------------------------------------------------


@needs_stack
class TestPendingRotation:
    def _keys(self):
        old_key, new_key = _new_key(), _new_key()
        return (_pub_of(old_key), _pub_of(new_key))

    @pytest.mark.asyncio
    async def test_rotation_records_pending_and_stays_refused(
            self, tmp_path):
        old_sk, new_sk = self._keys()
        seed = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-old", old_sk, TrustState.VERIFIED)}
        fetched = {UserID(OWNER): {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-new", new_sk, TrustState.UNVERIFIED)}}
        mgr, store, _client, _machine = _rig(
            tmp_path, owner_devices=seed, fetched=fetched, otks={})
        report = await mgr.ensure_owner_trust(GW)
        assert report["refused"] == ["PHONE1"]
        assert report["pending"] == ["PHONE1"]
        assert report["trusted"] == [] and report["rotated"] == []
        # fail closed: stored trust + keys untouched
        kept = await store.get_device(OWNER, "PHONE1")
        assert kept is not None and kept.trust == TrustState.VERIFIED
        assert str(kept.identity_key) == "ik-old"
        # actionable: the pending record carries old/new + first-seen
        pendings = list_pending_trusts(mgr.state)
        assert len(pendings) == 1
        rec = pendings[0]
        assert rec["user_id"] == OWNER and rec["device_id"] == "PHONE1"
        assert rec["old_identity_key"] == "ik-old"
        assert rec["old_signing_key"] == old_sk
        assert rec["new_identity_key"] == "ik-new"
        assert rec["new_signing_key"] == new_sk
        assert rec["first_seen"]
        first_seen = rec["first_seen"]
        # re-detection refreshes keys but preserves first-seen
        await mgr.ensure_owner_trust(GW)
        again = list_pending_trusts(mgr.state)[0]
        assert again["first_seen"] == first_seen

    @pytest.mark.asyncio
    async def test_rotation_via_raw_supplement_when_fetch_drops(
            self, tmp_path):
        """Live shape: mautrix validation drops the same-ID signing-key
        change from the fetch, so the supplement classifies the raw keys."""
        from mautrix.types import DeviceKeys

        old_key, new_key = _new_key(), _new_key()
        old_sk, new_sk = _pub_of(old_key), _pub_of(new_key)
        seed = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-old", old_sk, TrustState.VERIFIED)}
        raw_body = {
            "user_id": OWNER, "device_id": "PHONE1",
            "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.ed25519"],
            "keys": {f"curve25519:PHONE1": "ik-new",
                     f"ed25519:PHONE1": new_sk},
            "signatures": {},
            "unsigned": {"device_display_name": "phone"},
        }
        import json as _json

        from Crypto.Signature import eddsa

        # sign_olm signs the payload WITHOUT signatures/unsigned — mirror it.
        payload = {k: v for k, v in raw_body.items()
                   if k not in ("signatures", "unsigned")}
        data = _json.dumps(payload, ensure_ascii=False,
                           separators=(",", ":"), sort_keys=True)
        raw_body["signatures"] = {
            OWNER: {f"ed25519:PHONE1": _b64(
                eddsa.new(new_key, "rfc8032").sign(data.encode("utf-8")))}}
        raw_keys = {UserID(OWNER): {"PHONE1": DeviceKeys.deserialize(raw_body)}}
        mgr, _store, _client, _machine = _rig(
            tmp_path, owner_devices=seed, fetched={}, otks={},
            raw_keys=raw_keys)
        report = await mgr.ensure_owner_trust(GW)
        assert report["refused"] == ["PHONE1"]
        assert report["pending"] == ["PHONE1"]
        assert report["fetched"] == ["PHONE1"]
        assert len(list_pending_trusts(mgr.state)) == 1

    @pytest.mark.asyncio
    async def test_without_approval_nothing_encrypts_to_new_keys(
            self, tmp_path):
        old_key, new_key = _new_key(), _new_key()
        old_sk, new_sk = _pub_of(old_key), _pub_of(new_key)
        seed = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-old", old_sk, TrustState.VERIFIED)}
        fetched = {UserID(OWNER): {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-new", new_sk, TrustState.UNVERIFIED)}}
        # the OLD keys still verify (their pool is fine) — exclusion must
        # come from refused/pending status, not the OTK guard.
        otks = {(OWNER, "PHONE1"): _signed_otk(OWNER, "PHONE1", old_key)}
        mgr, store, _client, machine = _rig(
            tmp_path, owner_devices=seed, fetched=fetched, otks=otks,
            members=(OWNER, GW))
        report = await mgr.ensure_room_share(ROOM, GW)
        assert report["pending"] == ["PHONE1"]
        assert report["shared"] == [ROOM]  # other members still served
        assert machine.shared
        snap = machine.shared[0][2]
        assert snap.get(OWNER) == []  # rotated device got no room key
        assert "ik-new" not in "".join(
            str(getattr(d, "identity_key", "")) for d in store.devices.values())

    @pytest.mark.asyncio
    async def test_approve_path_retrusts_and_rotates(self, tmp_path):
        old_key, new_key = _new_key(), _new_key()
        old_sk, new_sk = _pub_of(old_key), _pub_of(new_key)
        seed = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-old", old_sk, TrustState.VERIFIED)}
        rotated = _device(OWNER, "PHONE1", "ik-new", new_sk,
                          TrustState.UNVERIFIED)
        fetched = {UserID(OWNER): {DeviceID("PHONE1"): rotated}}
        otks = {(OWNER, "PHONE1"): _signed_otk(OWNER, "PHONE1", new_key)}
        mgr, store, _client, machine = _rig(
            tmp_path, owner_devices=seed, fetched=fetched, otks=otks,
            session=FakeSession(sid="OLDSESSION"))
        assert (await mgr.ensure_owner_trust(GW))["pending"] == ["PHONE1"]
        approval = approve_pending_trust(
            mgr.state, user_id=OWNER, device_id="PHONE1")
        assert approval["new_identity_key"] == "ik-new"
        assert list_pending_trusts(mgr.state) == []
        report = await mgr.ensure_room_share(ROOM, GW)
        assert report["trusted"] == ["PHONE1"]
        assert report["rotated"] == ["PHONE1"]
        assert report["pending"] == [] and report["refused"] == []
        stored = await store.get_device(OWNER, "PHONE1")
        assert stored is not None and stored.trust == TrustState.VERIFIED
        assert str(stored.identity_key) == "ik-new"
        # Megolm rotation: the old outbound session was dropped, then the
        # new keys received the fresh share.
        assert store.removed == [ROOM]
        assert report["shared"] == [ROOM]
        assert machine.shared and machine.shared[0][2][OWNER] == ["PHONE1"]

    @pytest.mark.asyncio
    async def test_stale_approval_does_not_apply(self, tmp_path):
        old_sk, new_sk = self._keys()
        newer_sk = _pub_of(_new_key())
        seed = {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-old", old_sk, TrustState.VERIFIED)}
        fetched = {UserID(OWNER): {DeviceID("PHONE1"): _device(
            OWNER, "PHONE1", "ik-new", new_sk, TrustState.UNVERIFIED)}}
        mgr, _store, _client, _machine = _rig(
            tmp_path, owner_devices=seed, fetched=fetched, otks={})
        await mgr.ensure_owner_trust(GW)
        approve_pending_trust(mgr.state, user_id=OWNER, device_id="PHONE1")
        # keys change AGAIN before the next share: the approval is stale.
        fetched[UserID(OWNER)][DeviceID("PHONE1")] = _device(
            OWNER, "PHONE1", "ik-newer", newer_sk, TrustState.UNVERIFIED)
        report = await mgr.ensure_owner_trust(GW)
        assert report["pending"] == ["PHONE1"]
        assert report["trusted"] == [] and report["rotated"] == []
        rec = list_pending_trusts(mgr.state)[0]
        assert rec["new_identity_key"] == "ik-newer"

    @pytest.mark.asyncio
    async def test_conflicting_device_ids_refuse(self, tmp_path):
        key_a, key_b = _new_key(), _new_key()
        seed = {
            DeviceID("PHONE1"): _device(
                OWNER, "PHONE1", "ik-a", _pub_of(key_a), TrustState.VERIFIED),
            DeviceID("TABLET1"): _device(
                OWNER, "TABLET1", "ik-b", _pub_of(key_b), TrustState.VERIFIED),
        }
        # brand-new device ID presenting PHONE1's keys: genuine ambiguity.
        fetched = {UserID(OWNER): {
            DeviceID("PHONE1"): _device(
                OWNER, "PHONE1", "ik-a", _pub_of(key_a), TrustState.UNVERIFIED),
            DeviceID("CLONE"): _device(
                OWNER, "CLONE", "ik-a", _pub_of(key_a), TrustState.UNVERIFIED),
        }}
        mgr, store, _client, _machine = _rig(
            tmp_path, owner_devices=seed, fetched=fetched, otks={})
        report = await mgr.ensure_owner_trust(GW)
        assert report["refused"] == ["CLONE"]
        assert report["pending"] == []  # ambiguity is never one-click-approvable
        assert report["known"] == ["PHONE1"]
        assert await store.get_device(OWNER, "CLONE") is None

    @pytest.mark.asyncio
    async def test_moved_keys_between_ids_refuse_without_pending(
            self, tmp_path):
        key_a, key_b = _new_key(), _new_key()
        seed = {
            DeviceID("PHONE1"): _device(
                OWNER, "PHONE1", "ik-a", _pub_of(key_a), TrustState.VERIFIED),
            DeviceID("TABLET1"): _device(
                OWNER, "TABLET1", "ik-b", _pub_of(key_b), TrustState.VERIFIED),
        }
        # same-ID change whose new keys belong to ANOTHER known device.
        fetched = {UserID(OWNER): {
            DeviceID("PHONE1"): _device(
                OWNER, "PHONE1", "ik-b", _pub_of(key_b), TrustState.UNVERIFIED),
        }}
        mgr, _store, _client, _machine = _rig(
            tmp_path, owner_devices=seed, fetched=fetched, otks={})
        report = await mgr.ensure_owner_trust(GW)
        assert report["refused"] == ["PHONE1"]
        assert report["pending"] == []


@needs_stack
class TestNoRoomNotice:
    @pytest.mark.asyncio
    async def test_pending_report_posts_no_notice(self, tmp_path):
        mgr = _manager(tmp_path, machines={}, members=[OWNER])
        report = {"trusted": [], "known": [], "refused": ["PHONE1"],
                  "pending": ["PHONE1"], "fetched": ["PHONE1"], "shared": []}
        assert await mgr.maybe_post_verify_notice(
            ROOM, sender=GW, room_key="gw", report=report) is False
        assert mgr.client.calls == []  # nothing posted to the room


# ---------------------------------------------------------------------------
# standalone trust-device command (no crypto stack needed)
# ---------------------------------------------------------------------------


def _state_at(tmp_path):
    from observatory.state import default_state_db_path

    db = default_state_db_path(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    return ObservatoryState(db), tmp_path


def _seed_pending(state):
    return record_pending_trust(
        state, user_id=OWNER, device_id="PHONE1",
        old_identity_key="ik-old", old_signing_key="sk-old",
        new_identity_key="ik-new", new_signing_key="sk-new",
        reporter=GW)


def _trust_args(tmp_path, **kw):
    args = {"device": None, "user": None, "yes": False, "home": str(tmp_path)}
    args.update(kw)
    return Namespace(**args)


class TestTrustDeviceCommand:
    def test_lists_pendings_with_exact_command(self, tmp_path, capsys):
        from mercury_cli.subcommands.observatory import cmd_observatory

        state, _home = _state_at(tmp_path)
        _seed_pending(state)
        state.close()
        assert cmd_observatory(_trust_args(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "PHONE1" in out and "ik-old" in out and "ik-new" in out
        assert trust_device_command("PHONE1") in out

    def test_empty_list_reports_none(self, tmp_path, capsys):
        from mercury_cli.subcommands.observatory import cmd_observatory

        state, _home = _state_at(tmp_path)
        state.close()
        assert cmd_observatory(_trust_args(tmp_path)) == 0
        assert "No pending device rotations." in capsys.readouterr().out

    def test_approve_yes_moves_pending_to_approval(self, tmp_path, capsys):
        from mercury_cli.subcommands.observatory import cmd_observatory

        state, _home = _state_at(tmp_path)
        _seed_pending(state)
        state.close()
        assert cmd_observatory(_trust_args(tmp_path, device="PHONE1",
                                           yes=True)) == 0
        out = capsys.readouterr().out
        assert "Approved" in out and "PHONE1" in out
        state2, _ = _state_at(tmp_path)
        try:
            assert list_pending_trusts(state2) == []
            approvals = state2.get_meta("approved-trust:@owner:hs/PHONE1")
            assert "ik-new" in approvals
        finally:
            state2.close()

    def test_approve_unknown_device_fails(self, tmp_path, capsys):
        from mercury_cli.subcommands.observatory import cmd_observatory

        state, _home = _state_at(tmp_path)
        state.close()
        assert cmd_observatory(_trust_args(tmp_path, device="NOPE",
                                           yes=True)) == 1
        assert "No pending device rotation" in capsys.readouterr().err

    def test_interactive_confirm_approves_and_skips(
            self, tmp_path, capsys, monkeypatch):
        import mercury_cli.subcommands.observatory as obs_mod

        state, _home = _state_at(tmp_path)
        _seed_pending(state)
        state.close()
        monkeypatch.setattr(obs_mod, "_confirm", lambda _q: True)
        assert cmd_observatory_obs(tmp_path) == 0
        state2, _ = _state_at(tmp_path)
        try:
            assert list_pending_trusts(state2) == []
        finally:
            state2.close()

        state3, _ = _state_at(tmp_path)
        _seed_pending(state3)
        state3.close()
        monkeypatch.setattr(obs_mod, "_confirm", lambda _q: False)
        assert cmd_observatory_obs(tmp_path) == 2
        state4, _ = _state_at(tmp_path)
        try:
            assert len(list_pending_trusts(state4)) == 1  # skip keeps pending
        finally:
            state4.close()
        assert "Skipped" in capsys.readouterr().out

    def test_missing_state_db_errors_without_creating(
            self, tmp_path, capsys):
        from mercury_cli.subcommands.observatory import cmd_observatory

        assert cmd_observatory(_trust_args(tmp_path)) == 1
        assert "setup observatory" in capsys.readouterr().err
        assert not (tmp_path / "observatory" / "state.db").exists()


def cmd_observatory_obs(tmp_path):
    from mercury_cli.subcommands.observatory import cmd_observatory

    return cmd_observatory(_trust_args(tmp_path, device="PHONE1"))


# ---------------------------------------------------------------------------
# setup acceptance wizard (no crypto stack needed)
# ---------------------------------------------------------------------------


def _gate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))


class TestSetupWizardTrustGate:
    STANDING = ("To decrypt messages on Matrix after resetting identity in "
                "Element or Element X, run: mercury observatory trust-device")

    def test_standing_line_constant_verbatim(self):
        import mercury_cli.setup as setup_mod

        assert setup_mod._DEVICE_TRUST_STANDING_LINE.startswith(self.STANDING)
        assert "lists rotated devices" in setup_mod._DEVICE_TRUST_STANDING_LINE
        assert "fingerprint approval" in setup_mod._DEVICE_TRUST_STANDING_LINE

    def test_no_pendings_passes_without_prompts(
            self, tmp_path, capsys, monkeypatch):
        import mercury_cli.setup as setup_mod

        _gate_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            setup_mod, "prompt_yes_no",
            lambda *a, **k: pytest.fail("no prompts when nothing is pending"))
        assert setup_mod._acceptance_device_trust_gate() == \
            "passed: no pending device rotations"
        assert self.STANDING in capsys.readouterr().out

    def test_wizard_approve(self, tmp_path, capsys, monkeypatch):
        import mercury_cli.setup as setup_mod

        _gate_env(tmp_path, monkeypatch)
        state, _home = _state_at(tmp_path)
        _seed_pending(state)
        state.close()
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
        outcome = setup_mod._acceptance_device_trust_gate()
        assert outcome.startswith("passed:") and "approved" in outcome
        out = capsys.readouterr().out
        assert self.STANDING in out
        assert "PHONE1" in out and "ik-old" in out and "ik-new" in out
        assert trust_device_command("PHONE1") in out
        state2, _ = _state_at(tmp_path)
        try:
            assert list_pending_trusts(state2) == []
        finally:
            state2.close()

    def test_wizard_skip_fails_with_command(
            self, tmp_path, capsys, monkeypatch):
        import mercury_cli.setup as setup_mod

        _gate_env(tmp_path, monkeypatch)
        state, _home = _state_at(tmp_path)
        _seed_pending(state)
        state.close()
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: False)
        outcome = setup_mod._acceptance_device_trust_gate()
        assert outcome.startswith("failed:")
        assert "mercury observatory trust-device" in outcome
        out = capsys.readouterr().out
        assert trust_device_command("PHONE1") in out
        state2, _ = _state_at(tmp_path)
        try:
            assert len(list_pending_trusts(state2)) == 1  # skip keeps pending
        finally:
            state2.close()

    def test_acceptance_wires_the_gate(self):
        """Acceptance runs the device-trust gate (byte-guard: the call site
        must survive refactors of the surrounding gates)."""
        import inspect

        import mercury_cli.setup as setup_mod

        src = inspect.getsource(setup_mod._run_observatory_acceptance)
        assert "_acceptance_device_trust_gate" in src
        assert '"device-trust"' in src or "'device-trust'" in src
