"""M4c: end-to-end encryption for the Observatory sidecar (spec D4).

**D4 law**: E2EE on every *chat* room the sidecar creates — gateway room,
directives room, cron rooms, orchestrator rooms, every subagent room.
Spaces are structural containers (``m.space.child`` graph, zero message
content) and stay unencrypted, matching mautrix bridge behavior.

**Architecture (per D4 + D3)**: one mautrix :class:`~mautrix.crypto.OlmMachine`
per virtual user, bridge pattern:

* the sidecar owns ALL Matrix I/O (D3), so each machine talks through a
  tiny client adapter (:class:`_SidecarCryptoClient`) that masquerades
  its virtual user via the appservice ``?user_id=`` param on the shared
  :class:`~observatory.matrix_client.MatrixClient`;
* one crypto store **directory** under the observatory home
  (``$MERCURY_HOME/observatory/crypto/``) — one persistent
  :class:`SQLiteCryptoStore` per virtual user (``crypto/<localpart>.db``):
  upstream 0.21.1 ships asyncpg + memory only, so this module carries a
  small aiosqlite-backed adapter over the same abstract ``CryptoStore``
  interface. Restarts reattach the SAME Olm identity (device ids are
  deterministic per MXID) and old history stays decryptable (D8);
* inbound: appservice transactions carry ``m.room.encrypted`` events and
  ``to_device`` messages; :meth:`E2EEManager.handle_as_transaction`
  routes the transaction's crypto fields (``to_device``,
  ``device_lists``, ``device_one_time_keys_count`` — the ruma/tuwunel
  appservice transaction extension) into the per-user machines'
  ``handle_as_*`` entry points (built into mautrix 0.21.1 for exactly
  this appservice shape); room events decrypt via
  ``decrypt_megolm_event`` (``E2EEManager.decrypt_event``).

**Backend**: mautrix 0.21.1 hard-imports the ``olm`` extension (python-olm)
in six modules — there is no vodozemac-bindings path in this version, so
python-olm is required. Python 3.13 has no upstream python-olm wheel
(3.2.16 ships cp310–cp312 only), so the repo vendors per-arch cp313
wheels under ``observatory/wheels/`` (SHA256SUMS-pinned; installed
automatically by ``mercury setup observatory`` — no compiler, no
container runtime). ``observatory/scripts/build_python_olm_wheel.sh``
remains as the documented MANUAL rebuild path only, never auto-invoked.

**Key bootstrap — the manual verify step (documented, D4 bridge pattern):**

1. At boot the sidecar publishes the gateway device's keys
   (:meth:`E2EEManager.warmup` — device + one-time keys), so the owner's
   clients see a live device immediately instead of "the other party is
   currently not logged in".
2. The owner logs in on a second device and — once — verifies the
   gateway agent's device in the gateway room by comparing the
   fingerprint: the fingerprint to compare is posted in the gateway
   room's verify-howto notice
   (:meth:`E2EEManager.verify_notice_text`, via
   :meth:`E2EEManager.gateway_fingerprint`) the first time a share
   reports a refused or pending device. This pins
   the root of the observatory's trust. The store makes this a ONE-TIME
   step: the gateway's Olm identity survives sidecar restarts, so the
   owner's verification stays valid.
3. Virtual users (one machine each) apply **trust-on-first-use (TOFU)**
   to the owner's devices — the O3-sanctioned fallback, applied by
   :meth:`E2EEManager.ensure_owner_trust` before every fresh Megolm
   share: the first device key seen for ``@owner:<server>`` is marked
   ``TrustState.VERIFIED`` (:meth:`E2EEManager.trust_device_tofu`); a
   CHANGED key later is refused (fail closed), never silently re-trusted.
   TOFU is spec-sanctioned because the homeserver is single-owner,
   localhost-bound (D2): the operator who can MITM the homeserver owns
   the machine anyway.

**Honest capability gate (O3 — do not fake crypto):**

``observatory.e2ee: true|false`` in ``$MERCURY_HOME/config.yaml``
(**default true** since 2026-09-08 — the live gate
``MERCURY-E2EE-OK`` passed on this box: encrypt → wire → second-device
decrypt + encrypted m.replace edit round-trip; see
``observatory/scripts/e2ee_live_gate.py``). When true,
:meth:`E2EEManager.start` probes for ``olm`` + ``aiosqlite`` +
``mautrix.crypto`` and FAILS HARD with the exact remedy when the crypto
stack is missing — it never degrades to silent plaintext. Operators who
cannot run the compiled stack set ``observatory.e2ee: false`` explicitly.

**Remaining honest gaps:**

1. Cross-signing/SSSS key backup for owner device rotation stays out of
   scope until the owner actually rotates devices (the sidecar never
   drives SSSS; those adapter methods fail loudly).
2. ``E2EEManager.handle_as_transaction`` is the routing contract; the
   sidecar intake calls it via the intake's ``crypto_handler``
   (``to_device`` / ``device_lists`` / ``device_one_time_keys_count``
   ride alongside the room events — see appservice ``TransactionIntake``).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

from observatory.config_gen import ObservatoryPaths
from observatory.state import ObservatoryState, StateError

try:  # module must import on hosts without the compiled olm stack
    from mautrix.crypto.store import MemoryCryptoStore as _MemoryCryptoStore
except Exception:  # noqa: BLE001 — any import shape failure = store base missing
    _MemoryCryptoStore = object  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

#: Megolm algorithm pinned at room creation (D4).
MEGOLM_V1 = "m.megolm.v1.aes-sha2"

#: ``m.room.encryption`` content for every chat room the sidecar creates.
ENCRYPTION_CONTENT = {
    "algorithm": MEGOLM_V1,
    "rotation_period_ms": 7 * 24 * 60 * 60 * 1000,  # one week, bridge default
    "rotation_period_msgs": 100,
}

#: Crypto store directory name under ``$MERCURY_HOME/observatory/``.
CRYPTO_DIR_NAME = "crypto"

#: State-meta prefix marking a room key as encryption-enabled
#: (``crypt:<room_key> -> room_id``); also the restart-time registry of
#: rooms the executor must keep encrypting.
CRYPT_ROOM_META_PREFIX = "crypt:"

#: State-meta prefix recording that a triggering room key already got its
#: verify-howto notice in the GATEWAY room
#: (``e2ee-notice:<room_key> -> json`` of the device picture);
#: reposted only when the picture changes (key refusal or pending rotation).
NOTICE_META_PREFIX = "e2ee-notice:"
#: State-meta prefix for pending device-rotation trust records
#: (``pending-trust:<user_id>/<device_id> -> json`` with old/new key
#: fingerprints + first-seen timestamp). Written by
#: :meth:`E2EEManager.ensure_owner_trust` when a known device ID presents
#: changed keys (Element/Element X identity reset): the rotation stays
#: REFUSED (fail closed, never auto-trusted) but becomes actionable — the
#: operator approves it explicitly via
#: ``mercury observatory trust-device --device <id>``.
PENDING_TRUST_META_PREFIX = "pending-trust:"

#: State-meta prefix for operator-approved rotation records
#: (``approved-trust:<user_id>/<device_id> -> json``). Written by the
#: trust-device approval path; consumed by the NEXT
#: :meth:`E2EEManager.ensure_owner_trust`, which drops the old record,
#: marks the approved new keys VERIFIED, and forces Megolm rotation.
#: An approval only applies to the exact approved key pair — keys that
#: changed AGAIN since approval fail closed with a fresh pending record.
APPROVED_TRUST_META_PREFIX = "approved-trust:"

#: The standalone command that approves a pending device rotation
#: (single source: room-side surfaces never reword it).
TRUST_DEVICE_CMD = "mercury observatory trust-device"


def trust_device_command(device_id: str) -> str:
    """Exact operator command approving one pending device rotation."""
    return f"{TRUST_DEVICE_CMD} --device {device_id}"


def _trust_meta_key(prefix: str, user_id: str, device_id: str) -> str:
    return f"{prefix}{user_id}/{device_id}"


def list_pending_trusts(state: ObservatoryState) -> list[dict[str, Any]]:
    """Every pending device-rotation record, oldest first. Never raises —
    CLI/setup surfaces degrade to "none" when state is unreadable."""
    try:
        with state.locked() as db:  # noqa: SLF001 — same-package state scan
            rows = db.execute(
                "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
                (PENDING_TRUST_META_PREFIX + "%",),
            ).fetchall()
    except Exception:  # noqa: BLE001 — unreadable state reads as no pendings
        return []
    out: list[dict[str, Any]] = []
    for row in rows or []:
        try:
            import json as _json

            rec = _json.loads(row["value"])
        except Exception:  # noqa: BLE001 — one corrupt record never hides rest
            continue
        if isinstance(rec, dict) and rec.get("device_id"):
            out.append(rec)
    out.sort(key=lambda rec: str(rec.get("first_seen") or ""))
    return out


def list_approved_trusts(state: ObservatoryState) -> list[dict[str, Any]]:
    """Every unconsumed rotation approval. Same never-raises law as
    :func:`list_pending_trusts`."""
    try:
        with state.locked() as db:  # noqa: SLF001 — same-package state scan
            rows = db.execute(
                "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
                (APPROVED_TRUST_META_PREFIX + "%",),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows or []:
        try:
            import json as _json

            rec = _json.loads(row["value"])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(rec, dict) and rec.get("device_id"):
            out.append(rec)
    return out


def record_pending_trust(
    state: ObservatoryState,
    *,
    user_id: str,
    device_id: str,
    old_identity_key: str,
    old_signing_key: str,
    new_identity_key: str,
    new_signing_key: str,
    reporter: str = "",
) -> dict[str, Any]:
    """Persist (or refresh) the pending record for one rotated device.
    ``first_seen`` is set once and preserved across re-detections; the
    advertised new keys + reporter list refresh. Returns the record."""
    import json as _json
    from datetime import datetime, timezone

    key = _trust_meta_key(PENDING_TRUST_META_PREFIX, user_id, device_id)
    try:
        rec = _json.loads(state.get_meta(key))
        first_seen = str((rec or {}).get("first_seen") or "")
        reporters = list((rec or {}).get("reporters") or [])
    except StateError:
        first_seen, reporters = "", []
    if not first_seen:
        first_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if reporter and reporter not in reporters:
        reporters.append(reporter)
    rec = {
        "user_id": str(user_id), "device_id": str(device_id),
        "old_identity_key": str(old_identity_key),
        "old_signing_key": str(old_signing_key),
        "new_identity_key": str(new_identity_key),
        "new_signing_key": str(new_signing_key),
        "first_seen": first_seen, "reporters": reporters,
    }
    state.set_meta(key, _json.dumps(rec, sort_keys=True))
    return rec


def approve_pending_trust(
    state: ObservatoryState, *, user_id: str, device_id: str,
) -> dict[str, Any]:
    """Approve one pending rotation: drops the pending record and writes
    the approval marker the next share consumes (old record dropped, new
    keys VERIFIED, Megolm rotation forced). Raises :class:`E2EEError`
    when nothing is pending for that device."""
    import json as _json
    from datetime import datetime, timezone

    pending_key = _trust_meta_key(PENDING_TRUST_META_PREFIX, user_id, device_id)
    try:
        rec = _json.loads(state.get_meta(pending_key))
    except StateError:
        raise E2EEError(
            f"no pending device rotation for {user_id}/{device_id} — "
            f"run `{TRUST_DEVICE_CMD}` to list pendings") from None
    if not isinstance(rec, dict) or not rec.get("new_identity_key"):
        raise E2EEError(
            f"pending record for {user_id}/{device_id} is corrupt — "
            "re-run the share to re-record it, then approve again")
    approval = {
        "user_id": str(user_id), "device_id": str(device_id),
        "new_identity_key": str(rec["new_identity_key"]),
        "new_signing_key": str(rec.get("new_signing_key") or ""),
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state.set_meta(
        _trust_meta_key(APPROVED_TRUST_META_PREFIX, user_id, device_id),
        _json.dumps(approval, sort_keys=True))
    state.delete_meta(pending_key)
    return approval

#: Exact remedy surfaced by :class:`E2EEError` (single source, tested).
E2EE_REMEDY = (
    "E2EE is enabled (observatory.e2ee: true) but the crypto stack is "
    "missing. Install it from the vendored wheels (no build needed), "
    "then restart the sidecar:  "
    "uv pip install --python <venv>/bin/python --find-links "
    "hermes/observatory/wheels 'python-olm==3.2.16' "
    "'mautrix[encryption]==0.21.1' 'aiosqlite==0.22.1'  "
    "(or re-run `mercury setup observatory`, which installs it "
    "automatically). "
    "Or set observatory.e2ee: false for the plaintext path."
)

CLIENT_V3 = "/_matrix/client/v3"


def _is_virtual_user_id(user_id: str) -> bool:
    """True when a user id falls in our exclusive ghost namespace
    (``@merc_*`` — same predicate as the intake's ``is_ours`` and the
    to-device router; the MSC3984 handlers only serve those)."""
    return str(user_id or "").lstrip("@").startswith("merc_")


class E2EEError(RuntimeError):
    """Fail-hard crypto error (same law as provision.ProvisionError)."""


# ============================================================================
# Config gate + capability probe
# ============================================================================

def e2ee_enabled(mercury_home: str | Path | None = None) -> bool:
    """``observatory.e2ee`` in ``config.yaml`` — DEFAULT TRUE (D4: crypto
    ships hot since the MERCURY-E2EE-OK gate passed 2026-09-08; the
    explicit ``e2ee: false`` opt-out remains for hosts that cannot run
    the compiled olm stack). Mirrors the config-read pattern of
    ``provision.observatory_enabled``."""
    try:
        import yaml

        from observatory.provision import _mercury_home

        cfg_path = _mercury_home(mercury_home) / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, ValueError, ImportError):
        return True
    if not isinstance(doc, dict):
        return True
    obs = doc.get("observatory")
    if not isinstance(obs, dict):
        return True
    return bool(obs.get("e2ee", True))


def e2ee_available() -> bool:
    """True when the compiled Olm stack + SQLite adapter deps import
    cleanly (python-olm provides ``olm`` and its ``_libolm`` cffi
    backend; ``aiosqlite`` backs the persistent store)."""
    try:
        import aiosqlite  # noqa: F401
        import olm  # noqa: F401

        from mautrix.crypto import OlmMachine  # noqa: F401
    except Exception:  # noqa: BLE001 — any import shape failure = unavailable
        return False
    return True



def crypto_dir_for(mercury_home: str | Path | None = None) -> Path:
    """``$MERCURY_HOME/observatory/crypto`` (one dir shared by every
    per-virtual-user store — D4's 'one crypto store dir')."""
    from observatory.provision import _mercury_home

    return ObservatoryPaths(_mercury_home(mercury_home)).root / CRYPTO_DIR_NAME


# ============================================================================
# mautrix-side plumbing (all mautrix imports stay INSIDE these classes —
# the module must import cleanly on hosts without the compiled olm stack)
# ============================================================================

class _SidecarCryptoClient:
    """The exact client surface ``OlmMachine`` touches (call sites
    verified against mautrix 0.21.1 ``crypto/*.py``: mxid, device_id,
    add_event_handler, dispatch_event, send_to_device, send_to_one_device,
    upload_keys, query_keys, claim_keys, get_state_event, and the
    cross-signing trio reached only via SSSS — which the sidecar never
    drives).

    AUTH LAW (verified live against tuwunel 1.9.0, 2026-09-08): the
    ``/keys/*`` endpoints REJECT appservice ``?user_id=`` masquerade
    ("user must be authenticated and device identified") — crypto calls
    need a REAL per-user token. Each virtual user therefore logs in via
    the spec's ``m.login.application_service`` type (AS token + user_id,
    no password) with its deterministic device id; the minted token is
    persisted in the crypto store and re-validated lazily (401 → one
    re-login). Room-event and state calls keep the plain masquerade.
    """

    def __init__(self, mxid: str, device_id: str, client: Any) -> None:
        self.mxid = mxid
        self.device_id = device_id
        self._client = client  # observatory.matrix_client.MatrixClient
        self._handlers: dict[Any, list[Any]] = {}
        self._user_token: str | None = None
        #: set by VirtualUserCrypto — async cb(token) persisting the token
        self.on_token: Any = None

    # -- event plumbing (machines register; E2EEManager drives) -------------

    def add_event_handler(self, event_type, handler, **_kwargs) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def dispatch_event(self, event, source=None) -> None:
        evt_type = getattr(event, "type", None) or getattr(
            getattr(event, "content", None), "type", None
        )
        for handlers in self._handlers.get(evt_type, []):
            try:
                await handlers(event)
            except Exception:  # noqa: BLE001 — handler bugs must not kill I/O
                log.exception("crypto event handler failed (%s)", evt_type)

    # -- per-user token (see class docstring AUTH LAW) -------------------------

    def seed_token(self, token: str | None) -> None:
        self._user_token = token or None

    async def ensure_token(self) -> str:
        if self._user_token:
            return self._user_token
        out = await self._client.client_api(
            "POST", f"{CLIENT_V3}/login",
            json_body={
                "type": "m.login.application_service",
                # spec: identifier with the FULL mxid of a namespace user
                "identifier": {"type": "m.id.user", "user": str(self.mxid)},
                "device_id": str(self.device_id),
            },
        )
        token = str((out or {}).get("access_token") or "")
        if not token:
            raise E2EEError(f"AS login returned no token for {self.mxid}")
        self._user_token = token
        if self.on_token is not None:
            try:
                await self.on_token(token)
            except Exception:  # noqa: BLE001 — persistence is best-effort
                log.warning("crypto token persist failed", exc_info=True)
        return token

    async def _token_api(self, method: str, path: str, json_body=None) -> Any:
        """Client-API call authenticated as THIS virtual user's device."""
        from observatory.matrix_client import MatrixError

        token = await self.ensure_token()
        try:
            return await self._client._request(  # noqa: SLF001 — same-package transport
                method, path, token=token, json_body=json_body
            )
        except MatrixError as exc:
            if exc.status != 401:
                raise
            self._user_token = None  # expired — re-login exactly once
            token = await self.ensure_token()
            return await self._client._request(  # noqa: SLF001
                method, path, token=token, json_body=json_body
            )

    async def send_to_device(self, event_type, messages) -> None:
        txn = uuid.uuid4().hex
        await self._client.client_api(
            "PUT",
            f"{CLIENT_V3}/sendToDevice/{event_type.serialize() if hasattr(event_type, 'serialize') else event_type}/{txn}",
            sender=self.mxid,
            json_body={"messages": {
                user: {dev: msg.serialize() if hasattr(msg, "serialize") else msg
                       for dev, msg in devs.items()}
                for user, devs in (messages or {}).items()
            }},
        )

    async def send_to_one_device(self, event_type, user_id, device_id, message) -> None:
        """mautrix canonical order (client/api/modules/crypto.py): event
        type FIRST — ``encrypt_olm``/``key_share`` call positionally."""
        await self.send_to_device(event_type, {user_id: {device_id: message}})

    async def upload_keys(self, one_time_keys=None, device_keys=None) -> dict:
        """mautrix 0.21.1 contract (crypto/machine.py:310/:327): the
        no-arg call returns an algorithm→count mapping the machine
        ``.get()``s with an ``EncryptionKeyAlgorithm`` member directly;
        the upload call's response object is never read by the machine.
        Keys are enum members (canonical client shape) — ExtensibleEnum
        members hash UNEQUAL to their strings, so string keys would make
        every lookup miss and re-upload keys each cycle."""
        from mautrix.types import EncryptionKeyAlgorithm

        body: dict[str, Any] = {}
        if device_keys is not None:
            body["device_keys"] = (
                device_keys.serialize() if hasattr(device_keys, "serialize") else device_keys
            )
        if one_time_keys is not None:
            body["one_time_keys"] = dict(one_time_keys)
        out = await self._token_api(
            "POST", f"{CLIENT_V3}/keys/upload", json_body=body
        )
        counts = (out or {}).get("one_time_key_counts", {})
        if counts and all(isinstance(v, dict) for v in counts.values()):
            # server answered user-keyed counts — flatten to algorithm→count
            merged: dict[str, int] = {}
            for per_user in counts.values():
                merged.update(per_user)
            counts = merged
        keyed: dict[Any, int] = {}
        for alg, count in (counts or {}).items():
            try:
                keyed[EncryptionKeyAlgorithm.deserialize(alg)] = count
            except Exception:  # noqa: BLE001 — unknown algorithm: keep raw key
                keyed[alg] = count
        return keyed
    async def query_keys(self, users, token=None) -> Any:
        """mautrix canonical call (device_lists.py:53, :242):
        ``query_keys(users, token=since)`` — a user SET, or a
        ``{user: [devices]}`` map (``_get_full_device_keys``) — never a
        request object. Dict input passes device lists through."""
        from mautrix.types import QueryKeysResponse

        if isinstance(users, dict):
            device_keys = {str(u): [str(d) for d in (devs or [])]
                           for u, devs in users.items()}
        else:
            device_keys = {str(u): [] for u in users}
        body: dict[str, Any] = {"device_keys": device_keys, "timeout": 0}
        if token:
            body["token"] = str(token)
        out = await self._token_api("POST", f"{CLIENT_V3}/keys/query", json_body=body)
        return QueryKeysResponse.deserialize(out)

    async def claim_keys(self, request) -> Any:
        """mautrix canonical call (encrypt_olm.py:78): plain
        ``{user: {device: EncryptionKeyAlgorithm}}`` dict; algorithms
        serialize like the canonical client (``alg.serialize()``)."""
        from mautrix.types import ClaimKeysResponse

        out = await self._token_api(
            "POST", f"{CLIENT_V3}/keys/claim",
            json_body={
                "one_time_keys": {
                    str(user): {
                        str(dev): (alg.serialize() if hasattr(alg, "serialize")
                                   else str(alg))
                        for dev, alg in (devs or {}).items()
                    }
                    for user, devs in (request or {}).items()
                },
                "timeout": 0,
            },
        )
        return ClaimKeysResponse.deserialize(out)

    async def get_state_event(self, room_id, event_type) -> Any:
        from mautrix.errors import MForbidden, MNotFound
        from mautrix.types import RoomEncryptionStateEventContent
        from observatory.matrix_client import MatrixError

        et = event_type.serialize() if hasattr(event_type, "serialize") else event_type
        path = f"{CLIENT_V3}/rooms/{room_id}/state/{et}"
        try:
            out = await self._token_api("GET", path)
        except MatrixError as exc:
            # mautrix's room-key intake (base.py ``_fill_encryption_info``)
            # catches its OWN MNotFound/MForbidden for the defaults
            # fallback — a foreign error type would drop the room key.
            if exc.status == 404:
                raise MNotFound(404, f"room state not found: {path}") from exc
            if exc.status == 403:
                raise MForbidden(403, f"room state forbidden: {path}") from exc
            raise
        if not isinstance(out, dict):
            return None
        return RoomEncryptionStateEventContent.deserialize(out)


    # -- cross-signing paths (SSSS bootstrap) — the sidecar never drives
    #    these; honest loud failures instead of silent stubs.

    async def upload_one_signature(self, *args, **kwargs):  # pragma: no cover
        raise E2EEError("cross-signing upload is not wired (see e2ee REMAINING WORK #4)")

    async def upload_cross_signing_keys(self, *args, **kwargs):  # pragma: no cover
        raise E2EEError("cross-signing upload is not wired (see e2ee REMAINING WORK #4)")


class _EncryptionStateStore:
    """mautrix ``crypto.store.StateStore`` over sidecar bookkeeping: room
    encryption state comes from the homeserver (authoritative; read as the
    room's member ghost — the gateway stays out of child rooms), shared
    rooms from the ``crypt:`` registry in ``observatory/state.db``."""

    def __init__(self, client: Any, state: ObservatoryState, *, reader_mxid: str) -> None:
        self._client = client
        self._state = state
        self._reader_mxid = reader_mxid

    def _member_reader_for(self, room_id: str) -> str:
        """Member ghost for ``room_id`` (child voice in child rooms)."""
        try:
            for row in self._state.get_live():
                try:
                    if row.get("room_id") == room_id or row.get("space_id") == room_id:
                        mxid = str(row.get("mxid") or "")
                        if mxid:
                            return mxid
                except Exception:
                    continue
        except Exception:
            pass
        return self._reader_mxid

    async def is_encrypted(self, room_id) -> bool:
        info = await self.get_encryption_info(room_id)
        return info is not None

    async def get_encryption_info(self, room_id):
        from mautrix.types import RoomEncryptionStateEventContent

        path = f"{CLIENT_V3}/rooms/{room_id}/state/m.room.encryption/"
        readers: list[str] = []
        try:
            member = self._member_reader_for(str(room_id))
        except Exception:
            member = ""
        for cand in (member, self._reader_mxid):
            if cand and cand not in readers:
                readers.append(cand)
        for reader in readers or [self._reader_mxid]:
            try:
                out = await self._client.client_api("GET", path, sender=reader)
            except Exception:  # noqa: BLE001 — try next reader; 404 == not encrypted
                continue
            if not isinstance(out, dict):
                return None
            return RoomEncryptionStateEventContent.deserialize(out)
        return None
    async def find_shared_rooms(self, user_id) -> list:
        """Encrypted rooms ``user_id`` could share keys in — the safe
        direction for ``remove_outbound_group_sessions`` is a superset,
        and every crypt-registry room has owner + virtual users as
        members, so all of them qualify.
        """
        with self._state.locked() as db:  # noqa: SLF001 — registry scan, no scan API
            rows = db.execute(
                "SELECT value FROM meta WHERE key LIKE ?", (CRYPT_ROOM_META_PREFIX + "%",)
            ).fetchall()
        return [value for (value,) in rows]


class SQLiteCryptoStore(_MemoryCryptoStore):
    """Persistent crypto store for one virtual user — the REMAINING WORK
    #1 closer. Upstream mautrix 0.21.1 ships asyncpg + memory only, so
    this is a small aiosqlite adapter over the SAME abstract
    ``CryptoStore`` interface (via ``MemoryCryptoStore`` subclassing):

    * ``open()`` hydrates the whole per-user state (account pickle, Olm
      sessions, Megolm in/outbound sessions, message indices, devices +
      trust, cross-signing keys/signatures) into the in-memory working
      set, so every READ goes through the battle-tested memory
      implementation;
    * every MUTATING call writes through to ``crypto/<localpart>.db``
      (WAL, single connection — the sidecar is the only writer), so a
      sidecar restart reattaches the same Olm identity and D8 transcript
      grace keeps decrypting old messages.

    Pickle/serialization mirrors upstream's asyncpg store exactly
    (``OlmAccount.from_pickle(..., shared=...)``, session pickles under
    ``pickle_key``, ISO datetimes, ms durations) — no invented formats.
    """

    #: Pickle passphrase for every olm object (per-user db file; rotation
    #: out of scope — deleting the file resets the device identity).
    PICKLE_KEY = "mercury-observatory"

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        shared INTEGER NOT NULL,
        pickle BLOB NOT NULL);
    CREATE TABLE IF NOT EXISTS olm_sessions (
        sender_key TEXT NOT NULL, session_id TEXT NOT NULL,
        pickle BLOB NOT NULL,
        created_at TEXT NOT NULL, last_encrypted TEXT NOT NULL,
        last_decrypted TEXT NOT NULL,
        PRIMARY KEY (sender_key, session_id));
    CREATE TABLE IF NOT EXISTS megolm_inbound_sessions (
        room_id TEXT NOT NULL, session_id TEXT NOT NULL,
        sender_key TEXT NOT NULL, signing_key TEXT NOT NULL,
        pickle BLOB, withheld_code TEXT, withheld_reason TEXT,
        forwarding_chains TEXT, ratchet_safety TEXT,
        received_at TEXT, max_age_ms INTEGER, max_messages INTEGER,
        is_scheduled INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (room_id, session_id));
    CREATE TABLE IF NOT EXISTS megolm_outbound_sessions (
        room_id TEXT PRIMARY KEY, pickle BLOB NOT NULL,
        shared INTEGER NOT NULL, max_age_ms INTEGER NOT NULL,
        max_messages INTEGER NOT NULL, message_count INTEGER NOT NULL,
        created_at TEXT NOT NULL, last_used TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS message_indices (
        sender_key TEXT NOT NULL, session_id TEXT NOT NULL,
        msg_index INTEGER NOT NULL, event_id TEXT NOT NULL,
        timestamp INTEGER NOT NULL,
        PRIMARY KEY (sender_key, session_id, msg_index));
    CREATE TABLE IF NOT EXISTS tracked_users (user_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS devices (
        user_id TEXT NOT NULL, device_id TEXT NOT NULL,
        identity_key TEXT NOT NULL, signing_key TEXT NOT NULL,
        trust INTEGER NOT NULL, deleted INTEGER NOT NULL,
        name TEXT NOT NULL,
        PRIMARY KEY (user_id, device_id));
    CREATE TABLE IF NOT EXISTS cross_signing_keys (
        user_id TEXT NOT NULL, usage TEXT NOT NULL,
        key TEXT NOT NULL, first_seen_key TEXT NOT NULL,
        PRIMARY KEY (user_id, usage));
    CREATE TABLE IF NOT EXISTS signatures (
        signer_user_id TEXT NOT NULL, signer_key TEXT NOT NULL,
        target_user_id TEXT NOT NULL, target_key TEXT NOT NULL,
        signature TEXT NOT NULL,
        PRIMARY KEY (signer_user_id, signer_key, target_user_id, target_key));
    """

    def __init__(self, path: str | Path, account_id: str) -> None:
        if _MemoryCryptoStore is object:
            raise E2EEError(
                "crypto stack missing — SQLiteCryptoStore needs mautrix.crypto.store"
            )
        super().__init__(account_id, self.PICKLE_KEY)
        self._path = Path(path)
        self._db: Any = None

    async def _write(self, sql: str, params: tuple = ()) -> None:
        await self._db.execute(sql, params)
        await self._db.commit()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list:
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchall()

    async def open(self) -> None:
        import aiosqlite

        from mautrix.types import (
            CrossSigningUsage,
            DeviceID,
            DeviceIdentity,
            IdentityKey,
            RoomID,
            SessionID,
            SigningKey,
            TOFUSigningKey,
            TrustState,
            UserID,
        )
        from mautrix.crypto.sessions import (
            InboundGroupSession,
            OutboundGroupSession,
            RatchetSafety,
            Session,
        )
        from mautrix.crypto.account import OlmAccount
        from datetime import timedelta

        if self._db is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(self.SCHEMA)
        await self._db.commit()

        # --- hydrate the memory working set from disk -------------------------
        for row in await self._fetchall("SELECT key, value FROM kv"):
            if row["key"] == "device_id":
                self._device_id = DeviceID(row["value"])
            elif row["key"] == "sync_token":
                self._sync_token = row["value"] or None
        for row in await self._fetchall("SELECT shared, pickle FROM account"):
            self._account = OlmAccount.from_pickle(
                row["pickle"], passphrase=self.pickle_key, shared=bool(row["shared"])
            )
        for row in await self._fetchall(
            "SELECT sender_key, session_id, pickle, created_at, last_encrypted,"
            " last_decrypted FROM olm_sessions ORDER BY last_decrypted"
        ):
            sess = Session.from_pickle(
                row["pickle"],
                passphrase=self.pickle_key,
                creation_time=_dt(row["created_at"]),
                last_encrypted=_dt(row["last_encrypted"]),
                last_decrypted=_dt(row["last_decrypted"]),
            )
            self._olm_sessions.setdefault(IdentityKey(row["sender_key"]), []).append(sess)
        for row in await self._fetchall(
            "SELECT * FROM megolm_inbound_sessions WHERE pickle IS NOT NULL"
        ):
            chain = [k for k in (row["forwarding_chains"] or "").split(",") if k]
            self._inbound_sessions[(RoomID(row["room_id"]), SessionID(row["session_id"]))] = (
                InboundGroupSession.from_pickle(
                    row["pickle"],
                    passphrase=self.pickle_key,
                    signing_key=SigningKey(row["signing_key"]),
                    sender_key=IdentityKey(row["sender_key"]),
                    room_id=RoomID(row["room_id"]),
                    forwarding_chain=chain,
                    ratchet_safety=RatchetSafety.parse_json(row["ratchet_safety"] or "{}"),
                    received_at=_dt(row["received_at"]) if row["received_at"] else None,
                    max_age=(timedelta(milliseconds=row["max_age_ms"])
                             if row["max_age_ms"] is not None else None),
                    max_messages=row["max_messages"],
                    is_scheduled=bool(row["is_scheduled"]),
                )
            )
        for row in await self._fetchall("SELECT * FROM megolm_outbound_sessions"):
            self._outbound_sessions[RoomID(row["room_id"])] = OutboundGroupSession.from_pickle(
                row["pickle"],
                passphrase=self.pickle_key,
                room_id=RoomID(row["room_id"]),
                shared=bool(row["shared"]),
                max_messages=row["max_messages"],
                message_count=row["message_count"],
                max_age=timedelta(milliseconds=row["max_age_ms"]),
                use_time=_dt(row["last_used"]),
                creation_time=_dt(row["created_at"]),
            )
        for row in await self._fetchall("SELECT * FROM message_indices"):
            self._message_indices[
                (row["sender_key"], SessionID(row["session_id"]), row["msg_index"])
            ] = (row["event_id"], row["timestamp"])
        for row in await self._fetchall("SELECT user_id FROM tracked_users"):
            self._devices[UserID(row["user_id"])] = {}

        for row in await self._fetchall("SELECT * FROM devices"):
            self._devices.setdefault(UserID(row["user_id"]), {})[DeviceID(row["device_id"])] = (
                DeviceIdentity(
                    user_id=UserID(row["user_id"]),
                    device_id=DeviceID(row["device_id"]),
                    identity_key=IdentityKey(row["identity_key"]),
                    signing_key=SigningKey(row["signing_key"]),
                    trust=TrustState(row["trust"]),
                    deleted=bool(row["deleted"]),
                    name=row["name"],
                )
            )
        for row in await self._fetchall("SELECT * FROM cross_signing_keys"):
            self._cross_signing_keys.setdefault(UserID(row["user_id"]), {})[
                CrossSigningUsage(row["usage"])
            ] = TOFUSigningKey(key=SigningKey(row["key"]), first=SigningKey(row["first_seen_key"]))
        for row in await self._fetchall("SELECT * FROM signatures"):
            self._signatures.setdefault(
                (UserID(row["signer_user_id"]), SigningKey(row["signer_key"])), {}
            )[(UserID(row["target_user_id"]), SigningKey(row["target_key"]))] = row["signature"]


    async def put_kv(self, key: str, value: str) -> None:
        """Generic kv write (crypto-store internals: the per-user login
        token, so restarts reuse the device's token)."""
        await self._write("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    async def get_kv(self, key: str) -> str | None:
        rows = await self._fetchall("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None
    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def delete(self) -> None:
        await super().delete()
        if self._db is not None:
            for table in ("kv", "account", "olm_sessions", "megolm_inbound_sessions",
                          "megolm_outbound_sessions", "message_indices", "tracked_users",
                          "devices", "cross_signing_keys", "signatures"):
                await self._db.execute(f"DELETE FROM {table}")
            await self._db.commit()

    # -- identity / sync token ----------------------------------------------------

    async def put_device_id(self, device_id) -> None:
        await super().put_device_id(device_id)
        if self._db is not None:
            await self._write("INSERT OR REPLACE INTO kv VALUES ('device_id', ?)",
                              (str(device_id),))

    async def put_next_batch(self, next_batch) -> None:
        await super().put_next_batch(next_batch)
        if self._db is not None:
            await self._write("INSERT OR REPLACE INTO kv VALUES ('sync_token', ?)",
                              (str(next_batch),))

    # -- account -------------------------------------------------------------------

    async def put_account(self, account) -> None:
        await super().put_account(account)
        await self._write(
            "INSERT INTO account (id, shared, pickle) VALUES (1, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET shared=excluded.shared,"
            " pickle=excluded.pickle",
            (account.shared, account.pickle(self.pickle_key)),
        )

    # -- olm sessions ----------------------------------------------------------------

    async def add_session(self, key, session) -> None:
        await super().add_session(key, session)
        await self._write(
            "INSERT OR REPLACE INTO olm_sessions VALUES (?, ?, ?, ?, ?, ?)",
            (str(key), str(session.id), session.pickle(self.pickle_key),
             session.creation_time.isoformat(), session.last_encrypted.isoformat(),
             session.last_decrypted.isoformat()),
        )

    async def update_session(self, key, session) -> None:
        await self._write(
            "UPDATE olm_sessions SET pickle=?, last_encrypted=?, last_decrypted=?"
            " WHERE sender_key=? AND session_id=?",
            (session.pickle(self.pickle_key), session.last_encrypted.isoformat(),
             session.last_decrypted.isoformat(), str(key), str(session.id)),
        )

    # -- megolm inbound ----------------------------------------------------------------

    async def put_group_session(self, room_id, sender_key, session_id, session) -> None:
        await super().put_group_session(room_id, sender_key, session_id, session)
        from datetime import timedelta

        await self._write(
            "INSERT INTO megolm_inbound_sessions (room_id, session_id, sender_key,"
            " signing_key, pickle, forwarding_chains, ratchet_safety, received_at,"
            " max_age_ms, max_messages, is_scheduled)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (room_id, session_id) DO UPDATE SET withheld_code=NULL,"
            " withheld_reason=NULL, sender_key=excluded.sender_key,"
            " signing_key=excluded.signing_key, pickle=excluded.pickle,"
            " forwarding_chains=excluded.forwarding_chains,"
            " ratchet_safety=excluded.ratchet_safety, received_at=excluded.received_at,"
            " max_age_ms=excluded.max_age_ms, max_messages=excluded.max_messages,"
            " is_scheduled=excluded.is_scheduled",
            (str(room_id), str(session_id), str(sender_key), str(session.signing_key),
             session.pickle(self.pickle_key), ",".join(session.forwarding_chain),
             session.ratchet_safety.json(),
             session.received_at.isoformat() if session.received_at else None,
             int(session.max_age.total_seconds() * 1000) if session.max_age else None,
             session.max_messages, int(session.is_scheduled)),
        )

    async def redact_group_session(self, room_id, session_id, reason: str) -> None:
        await super().redact_group_session(room_id, session_id, reason)
        await self._write(
            "UPDATE megolm_inbound_sessions SET withheld_code='m.beeper.redacted',"
            " withheld_reason=?, pickle=NULL, forwarding_chains=NULL"
            " WHERE room_id=? AND session_id=? AND pickle IS NOT NULL",
            (f"Session redacted: {reason}", str(room_id), str(session_id)),
        )

    async def redact_group_sessions(self, room_id, sender_key, reason: str) -> list:
        deleted = await super().redact_group_sessions(room_id, sender_key, reason)
        await self._write(
            "UPDATE megolm_inbound_sessions SET withheld_code='m.beeper.redacted',"
            " withheld_reason=?, pickle=NULL, forwarding_chains=NULL"
            " WHERE (room_id=? OR ?='') AND (sender_key=? OR ?='') AND pickle IS NOT NULL",
            (f"Session redacted: {reason}", str(room_id) if room_id else "",
             str(room_id) if room_id else "", str(sender_key) if sender_key else "",
             str(sender_key) if sender_key else ""),
        )
        return deleted

    async def redact_expired_group_sessions(self) -> list:
        rows = await self._fetchall(
            "UPDATE megolm_inbound_sessions SET withheld_code='m.beeper.redacted',"
            " withheld_reason='Session redacted: expired', pickle=NULL,"
            " forwarding_chains=NULL WHERE pickle IS NOT NULL AND is_scheduled=0"
            " AND received_at IS NOT NULL AND max_age_ms IS NOT NULL"
            " AND unixepoch(received_at) + (2 * max_age_ms / 1000) < unixepoch('now')"
            " RETURNING session_id"
        )
        await self._db.commit()
        return [row["session_id"] for row in rows]

    async def redact_outdated_group_sessions(self) -> list:
        rows = await self._fetchall(
            "UPDATE megolm_inbound_sessions SET withheld_code='m.beeper.redacted',"
            " withheld_reason='Session redacted: outdated', pickle=NULL,"
            " forwarding_chains=NULL WHERE pickle IS NOT NULL AND received_at IS NULL"
            " RETURNING session_id"
        )
        await self._db.commit()
        return [row["session_id"] for row in rows]

    # -- megolm outbound ------------------------------------------------------------------

    async def add_outbound_group_session(self, session) -> None:
        await super().add_outbound_group_session(session)
        await self._write(
            "INSERT INTO megolm_outbound_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (room_id) DO UPDATE SET pickle=excluded.pickle,"
            " shared=excluded.shared, max_age_ms=excluded.max_age_ms,"
            " max_messages=excluded.max_messages, message_count=excluded.message_count,"
            " created_at=excluded.created_at, last_used=excluded.last_used",
            (str(session.room_id), session.pickle(self.pickle_key), int(session.shared),
             int(session.max_age.total_seconds() * 1000), session.max_messages,
             session.message_count, session.creation_time.isoformat(),
             session.use_time.isoformat()),
        )

    async def update_outbound_group_session(self, session) -> None:
        await self._write(
            "UPDATE megolm_outbound_sessions SET pickle=?, message_count=?, last_used=?"
            " WHERE room_id=?",
            (session.pickle(self.pickle_key), session.message_count,
             session.use_time.isoformat(), str(session.room_id)),
        )

    async def remove_outbound_group_session(self, room_id) -> None:
        await super().remove_outbound_group_session(room_id)
        await self._write("DELETE FROM megolm_outbound_sessions WHERE room_id=?",
                          (str(room_id),))

    async def remove_outbound_group_sessions(self, rooms) -> None:
        await super().remove_outbound_group_sessions(rooms)
        for room_id in rooms:
            await self._write("DELETE FROM megolm_outbound_sessions WHERE room_id=?",
                              (str(room_id),))

    # -- replay protection -------------------------------------------------------------------

    async def validate_message_index(self, sender_key, session_id, event_id,
                                     index, timestamp) -> bool:
        ok = await super().validate_message_index(sender_key, session_id, event_id,
                                                  index, timestamp)
        if ok:
            await self._write(
                "INSERT OR IGNORE INTO message_indices VALUES (?, ?, ?, ?, ?)",
                (str(sender_key), str(session_id), int(index), str(event_id),
                 int(timestamp)),
            )
        return ok

    # -- devices ---------------------------------------------------------------------------------

    async def put_devices(self, user_id, devices) -> None:
        # Atomic under concurrency: the old DELETE + multi-row INSERT
        # interleaved across concurrent ensure_room_share calls (the
        # inherited mautrix transaction() is a NO-OP), so the second
        # writer's INSERT hit the (user_id, device_id) PRIMARY KEY and
        # every Matrix send died UNIQUE-constraint. Single-statement
        # upserts are idempotent — no interleave can conflict — so no
        # lock/transaction is needed. Tradeoff vs the old full-replace:
        # server-deleted devices linger in the mirror until the set goes
        # empty (fetches only ever upsert live keys, so ghosts are never
        # trusted for a send). Empty set still DELETEs (conflict-free) so
        # tracked-empty stays exact across restarts.
        await super().put_devices(user_id, devices)
        seen: dict[tuple[str, str], tuple] = {}
        for did, dev in (devices or {}).items():
            seen[(str(user_id), str(did))] = (
                str(user_id), str(did), str(dev.identity_key), str(dev.signing_key),
                int(dev.trust), int(dev.deleted), dev.name or "")
        await self._db.execute("INSERT OR REPLACE INTO tracked_users VALUES (?)",
                               (str(user_id),))
        if seen:
            await self._db.executemany(
                "INSERT OR REPLACE INTO devices VALUES (?, ?, ?, ?, ?, ?, ?)",
                list(seen.values()))
        else:
            await self._db.execute("DELETE FROM devices WHERE user_id=?", (str(user_id),))
        await self._db.commit()

    async def put_device(self, user_id, device) -> None:
        """Upsert ONE device (used by ``E2EEManager.trust_device_tofu`` —
        upstream stores only the replace-set API)."""
        from mautrix.types import DeviceID

        self._devices.setdefault(user_id, {})[DeviceID(str(device.device_id))] = device
        await self._db.execute("INSERT OR REPLACE INTO tracked_users VALUES (?)",
                               (str(user_id),))
        await self._db.execute(
            "INSERT OR REPLACE INTO devices VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(user_id), str(device.device_id), str(device.identity_key),
             str(device.signing_key), int(device.trust), int(device.deleted),
             device.name or ""))
        await self._db.commit()

    async def get_devices(self, user_id) -> dict | None:
        """SQLite-backed read: the mirror is the restart source of truth,
        so trust decisions survive memory/SQLite disagreement (the
        put_devices race guarantees it happens). Falls back to memory
        before open() or for never-tracked users (None, not {})."""
        from mautrix.types import (
            DeviceID,
            DeviceIdentity,
            IdentityKey,
            SigningKey,
            TrustState,
            UserID,
        )

        if self._db is None:
            return await super().get_devices(user_id)
        rows = await self._fetchall(
            "SELECT * FROM devices WHERE user_id=?", (str(user_id),))
        if rows:
            return {
                DeviceID(row["device_id"]): DeviceIdentity(
                    user_id=UserID(row["user_id"]),
                    device_id=DeviceID(row["device_id"]),
                    identity_key=IdentityKey(row["identity_key"]),
                    signing_key=SigningKey(row["signing_key"]),
                    trust=TrustState(row["trust"]),
                    deleted=bool(row["deleted"]),
                    name=row["name"],
                )
                for row in rows
            }
        tracked = await self._fetchall(
            "SELECT user_id FROM tracked_users WHERE user_id=?", (str(user_id),))
        if tracked:
            return {}
        return await super().get_devices(user_id)

    async def get_device(self, user_id, device_id):
        """Single-device SQLite-backed read (same durability law)."""
        from mautrix.types import (
            DeviceID,
            DeviceIdentity,
            IdentityKey,
            SigningKey,
            TrustState,
            UserID,
        )

        if self._db is None:
            return await super().get_device(user_id, device_id)
        rows = await self._fetchall(
            "SELECT * FROM devices WHERE user_id=? AND device_id=?",
            (str(user_id), str(device_id)))
        if rows:
            row = rows[0]
            return DeviceIdentity(
                user_id=UserID(row["user_id"]),
                device_id=DeviceID(row["device_id"]),
                identity_key=IdentityKey(row["identity_key"]),
                signing_key=SigningKey(row["signing_key"]),
                trust=TrustState(row["trust"]),
                deleted=bool(row["deleted"]),
                name=row["name"],
            )
        return await super().get_device(user_id, device_id)

    # -- cross-signing (persisted for interface completeness; SSSS stays unwired) ---

    async def put_cross_signing_key(self, user_id, usage, key) -> None:
        # Crash-proof override (VM round 2): upstream mautrix 0.21.1
        # MemoryCryptoStore.put_cross_signing_key mutates the immutable
        # TOFUSigningKey NamedTuple on repeat store (``current.key = key``
        # → AttributeError), which escapes share_group_session, kills the
        # appservice transaction route, and renders no reply. mautrix is
        # site-packages (not vendored — never edited), so this override is
        # the fix.
        #
        # Write-through FIRST (SQLite is the restart source of truth), then
        # best-effort memory update: a repeat store that trips the upstream
        # bug is repaired with the immutable-safe update. Version-agnostic:
        # a fixed upstream never raises and takes the plain path, while a
        # first store (or any unrelated AttributeError) re-raises.
        await self._write(
            "INSERT INTO cross_signing_keys VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user_id, usage) DO UPDATE SET key=excluded.key",
            (str(user_id), usage.value, str(key), str(key)),
        )
        try:
            await super().put_cross_signing_key(user_id, usage, key)
        except AttributeError:
            store = getattr(self, "_cross_signing_keys", None)
            bucket = store.get(user_id, {}) if isinstance(store, dict) else {}
            current = bucket.get(usage)
            replace = getattr(current, "_replace", None)
            if current is None or replace is None:
                raise
            bucket[usage] = replace(key=key)

    async def put_signature(self, target, signer, signature: str) -> None:
        await super().put_signature(target, signer, signature)
        await self._write(
            "INSERT OR REPLACE INTO signatures VALUES (?, ?, ?, ?, ?)",
            (str(signer[0]), str(signer[1]), str(target[0]), str(target[1]), signature),
        )

    async def drop_signatures_by_key(self, signer) -> int:
        count = await super().drop_signatures_by_key(signer)
        await self._db.execute(
            "DELETE FROM signatures WHERE signer_user_id=? AND signer_key=?",
            (str(signer[0]), str(signer[1])))
        await self._db.commit()
        return count


def _dt(iso: str):
    from datetime import datetime

    return datetime.fromisoformat(str(iso))


class VirtualUserCrypto:
    """One OlmMachine + its client adapter + persistent
    :class:`SQLiteCryptoStore` for one ``@merc_*`` virtual user. The
    device id is deterministic per MXID and the store is durable
    (``crypto/<localpart>.db``), so restarts reattach the SAME device
    identity — the owner's one-time verify survives restarts."""

    DEVICE_PREFIX = "OBSV"

    def __init__(self, mxid: str, client: Any, state: ObservatoryState,
                 state_store: _EncryptionStateStore, *,
                 crypto_dir: str | Path | None = None) -> None:
        from mautrix.crypto import OlmMachine
        from mautrix.types import DeviceID, UserID

        digest = hashlib.sha256(mxid.encode()).hexdigest()[:8].upper()
        self.mxid = UserID(mxid)
        self.device_id = DeviceID(f"{self.DEVICE_PREFIX}{digest}")
        self.adapter = _SidecarCryptoClient(mxid, self.device_id, client)
        store: Any
        if crypto_dir is not None:
            localpart = str(mxid).lstrip("@").split(":", 1)[0]
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in localpart)
            store = SQLiteCryptoStore(Path(crypto_dir) / f"{safe}.db", str(mxid))
        else:  # tests / in-memory mode
            from mautrix.crypto.store import MemoryCryptoStore

            store = MemoryCryptoStore(UserID(mxid), self.device_id)
        self.store = store
        self.machine = OlmMachine(self.adapter, store, state_store)
        self._loaded = False

    async def load(self) -> None:
        if not self._loaded:
            if hasattr(self.store, "open"):
                await self.store.open()
                # reuse the persisted per-user device token across restarts
                self.adapter.seed_token(await self.store.get_kv("login_token"))
                from functools import partial

                self.adapter.on_token = partial(self.store.put_kv, "login_token")
            await self.machine.load()
            await self.machine.share_keys()
            self._loaded = True


def wire_encrypted_event(event: dict[str, Any]) -> Any:
    """Build a mautrix ``EncryptedEvent`` from a raw wire dict (a
    ``/messages`` chunk entry or appservice transaction event).
    mautrix 0.21.1 ``deserialize`` needs the JSON names — ``type``
    (``m.room.encrypted``) and ``origin_server_ts``; the attr name
    ``timestamp`` does NOT map and omitting ``type`` raises
    ``SerializerError`` (live-gate proven)."""
    from mautrix.types import EncryptedEvent

    return EncryptedEvent.deserialize(
        {
            "event_id": event.get("event_id"),
            "room_id": event.get("room_id"),
            "sender": event.get("sender"),
            "type": event.get("type") or "m.room.encrypted",
            "origin_server_ts": event.get("origin_server_ts", 0),
            "content": event.get("content") or {},
        }
    )


def message_content(
    body: str,
    formatted_body: str | None = None,
    relates_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plain ``m.room.message`` content dict for the encrypted send path.
    Spec ``m.replace`` law (live-gate proven: O1's mautrix parses the
    replacement from ``m.new_content``, not ``body``): an edit carries
    ``m.relates_to`` AND a mirrored ``m.new_content``; ``body`` stays
    the ``* ...`` fallback. Plain messages carry neither key."""
    content: dict[str, Any] = {"msgtype": "m.text", "body": body}
    if formatted_body is not None:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = formatted_body
    if relates_to is not None:
        content["m.relates_to"] = relates_to
        if relates_to.get("rel_type") == "m.replace":
            new_content: dict[str, Any] = {"msgtype": "m.text", "body": body}
            if formatted_body is not None:
                new_content["format"] = "org.matrix.custom.html"
                new_content["formatted_body"] = formatted_body
            content["m.new_content"] = new_content
    return content


class E2EEManager:
    """Sidecar-wide E2EE facade (D4). Owns one :class:`VirtualUserCrypto`
    per virtual user, the encrypted-room registry, and the plaintext
    boundary: constructing it is harmless; :meth:`start` fails hard when
    the crypto stack is absent and the flag is on."""

    def __init__(
        self,
        client: Any,  # MatrixClient (duck-typed in tests)
        state: ObservatoryState,
        *,
        crypto_dir: str | Path,
        owner_mxid: str = "",
        gateway_mxid: str = "",
    ) -> None:
        self.client = client
        self.state = state
        self.crypto_dir = Path(crypto_dir)
        self.owner_mxid = owner_mxid
        self.gateway_mxid = gateway_mxid
        self._machines: dict[str, VirtualUserCrypto] = {}
        self._state_store = _EncryptionStateStore(
            client, state, reader_mxid=gateway_mxid or owner_mxid
        )

    # -- lifecycle -----------------------------------------------------------

    async def start(self, *, enabled: bool = True) -> None:
        """Probe-and-fail-hard gate. ``enabled`` usually comes from
        :func:`e2ee_enabled`; the caller decides policy, this enforces
        honesty: enabled + missing stack = :class:`E2EEError`, never
        silent plaintext. Disabled is a no-op (plaintext path needs no
        crypto store, so it never touches the disk)."""
        if not enabled:
            return
        if not e2ee_available():
            raise E2EEError(E2EE_REMEDY)
        self.crypto_dir.mkdir(parents=True, exist_ok=True)

    # -- machines --------------------------------------------------------------

    def machine_for(self, mxid: str) -> VirtualUserCrypto:
        machine = self._machines.get(mxid)
        if machine is None:
            machine = VirtualUserCrypto(
                mxid, self.client, self.state, self._state_store,
                crypto_dir=self.crypto_dir,
            )
            self._machines[mxid] = machine
        return machine

    async def stop(self) -> None:
        """Close every persistent store (each write already committed —
        this only releases the SQLite handles)."""
        for crypto in self._machines.values():
            if hasattr(crypto.store, "close") and crypto.store is not None:
                try:
                    await crypto.store.close()
                except Exception:  # noqa: BLE001 — teardown must not raise
                    log.warning("crypto store close failed for %s", crypto.mxid, exc_info=True)

    # -- to-device routing (appservice transaction pipeline) ----------------------

    async def handle_as_transaction(self, txn: dict[str, Any]) -> dict[str, int]:
        """Route one RAW appservice transaction body into the per-user
        machines — the intake-side half of the crypto pipeline (REMAINING
        WORK #2 closer). Tuwunel/ruma transaction shape (bare keys; tuwunel
        1.9.0 emits the MSC-prefixed aliases — both accepted, first
        present wins):

        * ``to_device``: ``{user_id: {device_id: {sender, type, content}}}``
          OR the flattened LIST of ``AsToDeviceEvent``
          ``[{sender, type, content, to_user_id, to_device_id}]`` (MSC2409
          wire shape — flattened to the map form internally)
          → each event deserialized to ``ASToDeviceEvent`` and handed to
          the target machine's ``handle_as_to_device_event`` (Olm pre-key
          messages, room keys, key requests);
        * ``device_lists``: ``{changed: [...], left: [...]}`` → every
          machine's ``handle_as_device_lists`` (re-query changed users'
          device keys; drops rotated outbound sessions);
        * ``device_one_time_keys_count``:
          ``{user_id: {device_id: {signed_curve25519: N}}}`` → the owning
          machine's ``handle_as_otk_counts`` (tops up one-time keys).

        Machines are created lazily for targeted ``@merc_*`` virtual users
        (a to-device message for a never-loaded virtual user would else
        be dropped — its Olm session would wedge). Returns counters for
        the intake log. Unknown targets are logged and skipped, never
        raised: intake must survive crypto noise.
        """
        from mautrix.types import ASToDeviceEvent, DeviceLists, DeviceOTKCount

        routed: dict[str, int] = {"to_device": 0, "device_lists": 0, "otk_counts": 0}
        # MSC aliases: the intake normalizes these to bare first, but raw
        # bodies carry them — accept both, first present wins, never double.
        to_device = (txn.get("to_device")
                     or txn.get("de.sorunome.msc2409.to_device") or {})
        if isinstance(to_device, list):
            nested: dict[str, dict[str, Any]] = {}
            for entry in to_device:
                if not isinstance(entry, dict):
                    continue
                user_id = entry.get("to_user_id")
                device_id = entry.get("to_device_id")
                if not user_id or not device_id:
                    continue
                raw = {k: v for k, v in entry.items()
                       if k not in ("to_user_id", "to_device_id")}
                nested.setdefault(str(user_id), {})[str(device_id)] = raw
            to_device = nested
        if not isinstance(to_device, dict):
            to_device = {}
        for user_id, devices in to_device.items():
            if not str(user_id).lstrip("@").startswith("merc_"):
                log.debug("to-device for non-virtual user %s — skipped", user_id)
                continue
            machine = self.machine_for(str(user_id))
            await machine.load()
            for device_id, raw in (devices or {}).items():
                try:
                    evt = ASToDeviceEvent.deserialize({
                        **(raw or {}),
                        "to_user_id": user_id,
                        "to_device_id": device_id,
                    })
                    await machine.machine.handle_as_to_device_event(evt)
                    routed["to_device"] += 1
                except Exception:  # noqa: BLE001 — one bad message must not
                    # kill the routing of the remaining ones
                    log.warning("to-device route failed for %s/%s",
                                user_id, device_id, exc_info=True)
        raw_lists = (txn.get("device_lists")
                     or txn.get("org.matrix.msc3202.device_lists"))
        if raw_lists:
            try:
                lists = DeviceLists.deserialize(raw_lists)
                for machine in self._machines.values():
                    if machine._loaded:
                        await machine.machine.handle_as_device_lists(lists)
                routed["device_lists"] = 1
            except Exception:  # noqa: BLE001
                log.warning("device_lists route failed", exc_info=True)
        counts = (txn.get("device_one_time_keys_count")
                  or txn.get("device_one_time_keys_counts")
                  or txn.get("org.matrix.msc3202.device_one_time_keys_count")
                  or {})
        if isinstance(counts, dict):
            for user_id, devices in counts.items():
                machine = self._machines.get(str(user_id))
                if machine is None or not machine._loaded:
                    continue
                try:
                    nested = {
                        user_id: {
                            dev: DeviceOTKCount.deserialize(c)
                            for dev, c in (devices or {}).items()
                        }
                    }
                    await machine.machine.handle_as_otk_counts(nested)
                    routed["otk_counts"] += 1
                except Exception:  # noqa: BLE001
                    log.warning("otk-count route failed for %s", user_id, exc_info=True)
        return routed

    # -- MSC3984 appservice key queries (HS fans @merc_* queries here) -----------

    async def key_query(self, body: dict[str, Any]) -> dict[str, Any]:
        """Serve MSC3984 ``keys/query`` from live Olm account state.

        The homeserver fans device-key queries for our namespace here
        instead of serving them itself; without this surface every query
        404s and clients encrypt blind. Returns REAL device keys (identity
        + signing keys with a self-signature straight from the owning
        ``OlmAccount``) for virtual users we own; foreign users are
        omitted (the HS serves those itself) and per-user failures ride
        ``failures``. Never raises — intake must survive crypto noise.
        """
        from mautrix.types import UserID

        requested = (body or {}).get("device_keys") or {}
        device_keys: dict[str, Any] = {}
        failures: dict[str, Any] = {}
        if not isinstance(requested, dict):
            return {"device_keys": {}, "master_keys": {},
                    "self_signing_keys": {}, "failures": failures}
        for raw_user, devices in requested.items():
            user_id = str(raw_user)
            if not _is_virtual_user_id(user_id):
                continue  # not ours — the HS serves those itself
            try:
                crypto = self.machine_for(user_id)
                await crypto.load()
                if devices and str(crypto.device_id) not in {
                        str(d) for d in devices}:
                    continue  # asked for other devices only — nothing to serve
                dk = crypto.machine.account.get_device_keys(
                    UserID(user_id), crypto.device_id)
                device_keys.setdefault(user_id, {})[str(crypto.device_id)] = (
                    dk.serialize())
            except Exception as exc:  # noqa: BLE001 — one bad user never kills
                log.warning("msc3984 key query failed for %s: %s",
                            user_id, exc)
                failures[user_id] = {"errcode": "M_UNKNOWN",
                                     "error": "key lookup failed"}
        return {"device_keys": device_keys, "master_keys": {},
                "self_signing_keys": {}, "failures": failures}

    async def key_claim(self, body: dict[str, Any]) -> dict[str, Any]:
        """Serve MSC3984 ``keys/claim`` with a FRESH signed one-time key.

        Mints one ``signed_curve25519`` OTK from the owning account per
        (user, device) pair and signs it exactly like the upload path
        (``OlmAccount.get_one_time_keys`` shape). The private half stays
        in the account until the inbound session consumes it, so the
        claimed key establishes a working Olm session via the already-wired
        to-device pipeline. Unknown devices / algorithms land in
        ``failures``. Never raises.
        """
        from mautrix.crypto.signature import sign_olm

        requested = (body or {}).get("one_time_keys") or {}
        one_time_keys: dict[str, Any] = {}
        failures: dict[str, Any] = {}
        if not isinstance(requested, dict):
            return {"one_time_keys": {}, "failures": failures}
        for raw_user, devices in requested.items():
            user_id = str(raw_user)
            if not _is_virtual_user_id(user_id):
                continue  # not ours — the HS serves those itself
            if not isinstance(devices, dict):
                continue
            for raw_device, algorithm in devices.items():
                device_id = str(raw_device)
                try:
                    crypto = self.machine_for(user_id)
                    await crypto.load()
                    if device_id != str(crypto.device_id):
                        failures.setdefault(user_id, {})[device_id] = {
                            "errcode": "M_NOT_FOUND",
                            "error": f"unknown device {device_id}"}
                        continue
                    if str(algorithm) != "signed_curve25519":
                        failures.setdefault(user_id, {})[device_id] = {
                            "errcode": "M_UNRECOGNIZED",
                            "error": f"unsupported algorithm {algorithm}"}
                        continue
                    account = crypto.machine.account
                    account.generate_one_time_keys(1)
                    unpublished = dict(
                        account.one_time_keys.get("curve25519", {}))
                    if not unpublished:
                        failures.setdefault(user_id, {})[device_id] = {
                            "errcode": "M_UNKNOWN",
                            "error": "no one-time keys available"}
                        continue
                    key_id = sorted(unpublished)[0]
                    pub = unpublished[key_id]
                    sig = sign_olm({"key": pub}, account)
                    one_time_keys.setdefault(user_id, {}).setdefault(
                        device_id, {})[f"signed_curve25519:{key_id}"] = {
                        "key": pub,
                        "signatures": {
                            user_id: {f"ed25519:{device_id}": str(sig)},
                        },
                    }
                    # Real-server semantics (mirrors share_keys: generate →
                    # publish → mark): a claimed key must never be handed out
                    # twice, so mark it published — the next claim mints a
                    # fresh one and the machine re-tops-up on its own cycle.
                    account.mark_keys_as_published()
                except Exception as exc:  # noqa: BLE001 — one bad claim never
                    log.warning("msc3984 key claim failed for %s/%s: %s",
                                user_id, device_id, exc)
                    failures.setdefault(user_id, {}).setdefault(device_id, {
                        "errcode": "M_UNKNOWN", "error": "key claim failed"})
        return {"one_time_keys": one_time_keys, "failures": failures}

    # -- room registry (survives restarts; the executor consults it) -----------

    def mark_room_encrypted(self, key: str, room_id: str) -> None:
        self.state.set_meta(CRYPT_ROOM_META_PREFIX + key, room_id)

    def room_is_encrypted(self, key: str) -> bool:
        try:
            return bool(self.state.get_meta(CRYPT_ROOM_META_PREFIX + key))
        except StateError:
            return False

    # -- room enablement (D4: at creation, every chat room) ---------------------

    async def enable_room_encryption(self, room_id: str, *, sender: str) -> str:
        """PUT ``m.room.encryption`` (idempotent: re-PUT of identical
        content is a no-op state-wise). The creator (sender) owns the
        state event; owner PL 100 already permits it."""
        from urllib.parse import quote

        path = f"{CLIENT_V3}/rooms/{quote(room_id, safe='')}/state/m.room.encryption/"
        out = await self.client.client_api(
            "PUT", path, sender=sender, json_body=dict(ENCRYPTION_CONTENT)
        )
        return str((out or {}).get("event_id") or "")

    async def trust_device_tofu(self, mxid: str, target_user: str, device: Any,
                                *, first_sight: bool = False) -> bool:
        """Mark a first-seen device of ``target_user`` as verified for the
        given virtual user's machine. TOFU: only first sight; a device
        that CHANGES key later fails closed — never re-trusted.

        ``first_sight`` covers the fetch-then-trust order: mautrix key
        fetches store every device as UNVERIFIED, so a device the last
        fetch just introduced looks "known". The caller passes
        ``first_sight=True`` only for device ids absent from its
        pre-fetch snapshot; same-keys-required, so a concurrent rotation
        still refuses instead of trusting. A known device resighted under
        identical keys is (re-)verified and persisted — the last fetch
        clobbered its VERIFIED trust back to UNVERIFIED in both memory
        and SQLite, and only this write restores it.

        Returns True when the device is trusted (first sight, or already
        known under identical keys) and False when a key change was
        refused: the caller must surface that refusal, never encrypt to
        the changed device on TOFU authority."""
        from mautrix.types import TrustState

        crypto = self.machine_for(mxid)
        store = crypto.machine.crypto_store
        existing = await store.get_device(target_user, device.device_id)
        if existing is not None:
            if (str(existing.identity_key) != str(device.identity_key)
                    or str(existing.signing_key) != str(device.signing_key)):
                log.warning(
                    "TOFU REFUSED for %s/%s: keys changed since first sight"
                    " — manual re-verify required, never silent re-trust",
                    target_user, device.device_id,
                )
                return False
            if existing.trust == TrustState.VERIFIED and not first_sight:
                return True
            # else: first sight of a device the last fetch just stored as
            # UNVERIFIED, or a known device whose VERIFIED trust that same
            # fetch clobbered — fall through to (re-)VERIFY + persist below.
        device.trust = TrustState.VERIFIED
        if hasattr(store, "put_device"):  # SQLiteCryptoStore — single upsert
            await store.put_device(target_user, device)
        else:  # upstream memory store: replace-set API only
            devices = await store.get_devices(target_user) or {}
            devices[device.device_id] = device
            await store.put_devices(target_user, devices)
        return True

    # -- startup warmup ---------------------------------------------------------

    async def warmup(self) -> dict[str, str]:
        """Load the gateway agent's machine — device + one-time key upload
        via ``share_keys`` — so owner clients see a live device the moment
        the sidecar is up. The live "other party is currently not logged
        in" dead-end was machines that only loaded lazily on first send:
        until then no device keys were published and FluffyChat could not
        open an Olm session to us. Idempotent; returns ``{mxid: device_id}``.

        Gateway ONLY — never the owner MXID: loading a machine for the
        owner would publish a rogue sidecar device under the owner's user
        id. Without a gateway identity there is nothing safe to warm."""
        if not self.gateway_mxid:
            return {}
        crypto = self.machine_for(self.gateway_mxid)
        await crypto.load()
        log.info("e2ee warmup: %s device %s keys published",
                 self.gateway_mxid, crypto.device_id)
        return {self.gateway_mxid: str(crypto.device_id)}

    async def drop_outbound_sessions(self, rooms, senders=()) -> dict[str, int]:
        """Drop cached outbound Megolm sessions for ``rooms`` on every
        relevant sender machine. First-login cold start: the first E2EE
        share + power snapshot are built BEFORE the owner has ever
        joined (owner auto-join heal runs at boot when the owner has no
        session), so a pre-join outbound session never encrypted to the
        owner — dropping forces the next send to re-share fresh via
        :meth:`ensure_room_share` (fresh TOFU trust + fresh OTK verify).
        ``senders`` are loaded on demand (a pre-join session from a
        PREVIOUS boot persists in that sender's crypto file while this
        process never loaded its machine); already-loaded machines are
        always covered; machines are never created for anyone else.
        Removal of a missing session is a no-op. Returns
        ``{sender_mxid: actually_dropped_count}``."""
        from mautrix.types import RoomID

        targets = set(self._machines) | set(senders or ())
        if self.gateway_mxid:
            targets.add(self.gateway_mxid)
        owner = (self.owner_mxid or "").strip()
        targets = {t for t in targets if t and t != owner}
        dropped: dict[str, int] = {}
        for sender in sorted(targets):
            try:
                crypto = self.machine_for(sender)
                await crypto.load()
                store = crypto.machine.crypto_store
            except Exception:  # noqa: BLE001 — one sick store never blocks rest
                log.warning("post-join rotation: store load failed for %s",
                            sender, exc_info=True)
                continue
            n = 0
            for room_id in rooms:
                try:
                    session = await store.get_outbound_group_session(
                        RoomID(str(room_id)))
                    if session is None:
                        continue
                    await store.remove_outbound_group_session(RoomID(str(room_id)))
                    n += 1
                except Exception:  # noqa: BLE001 — per-room best effort
                    log.warning("post-join rotation failed for %s in %s",
                                sender, room_id, exc_info=True)
            if n:
                dropped[str(sender)] = n
                log.info("post-join rotation: dropped %d outbound session(s) "
                         "for %s", n, sender)
        return dropped

    async def post_wipe_rotation(self, rooms, senders=()) -> dict[str, Any]:
        """One-shot first boot after an annihilate wipe: force-drop ALL
        outbound Megolm sessions for ``rooms`` (the crypto dir may have
        survived a partial wipe with pre-wipe sessions) + re-run
        :meth:`ensure_owner_trust` fresh per sender with NO snapshot to
        compare against — stored owner devices are cleared first, so
        whatever the fresh server returns is first sight. Never raises:
        every sender/room is best-effort; the report carries counts."""
        from mautrix.types import UserID

        report: dict[str, Any] = {"dropped": {}, "trust": {}, "errors": []}
        dropped = await self.drop_outbound_sessions(rooms, senders=senders)
        report["dropped"] = dropped
        owner = (self.owner_mxid or "").strip()
        if not owner:
            return report
        targets = set(self._machines) | set(senders or ())
        if self.gateway_mxid:
            targets.add(self.gateway_mxid)
        targets = {t for t in targets if t and t != owner}
        for sender in sorted(targets):
            try:
                crypto = self.machine_for(sender)
                await crypto.load()
                try:
                    await crypto.machine.crypto_store.put_devices(UserID(owner), {})
                except Exception:  # noqa: BLE001 — store without device API
                    pass
                report["trust"][str(sender)] = await self.ensure_owner_trust(sender)
            except Exception as exc:  # noqa: BLE001 — per-sender best effort
                report["errors"].append(f"{sender}: {exc}")
                log.warning("post-wipe trust refresh failed for %s: %s",
                            sender, exc)
        log.info("post-wipe rotation: dropped=%s trust_senders=%s",
                 dropped, sorted(report["trust"]))
        return report


    async def gateway_fingerprint(self, mxid: str) -> str:
        """This device's ed25519 fingerprint as clients display it — the
        string the owner compares in the manual-verify step (also carried
        by every verify-howto room notice, so no separate key-display
        command is needed)."""
        crypto = self.machine_for(mxid)
        await crypto.load()
        return str(crypto.machine.account.fingerprint)

    async def _handle_same_id_change(
        self, *, sender_mxid: str, owner: str, name: str,
        old_identity_key: str, old_signing_key: str, device: Any,
        snapshot: dict[str, tuple[str, str]],
        approvals: dict[tuple[str, str], dict[str, Any]],
        report: dict[str, list[str]], approval_store: Any,
    ) -> None:
        """Classify one same-device-ID key change (identity-reset rotation
        candidate). Fail closed in every branch — the ONLY trust path is a
        matching operator approval (``trust-device``); otherwise the device
        lands in ``refused`` plus an actionable ``pending`` record, unless
        the new keys collide with a DIFFERENT known device (genuine
        ambiguity: refused with no pending record)."""
        new_identity_key = str(device.identity_key)
        new_signing_key = str(device.signing_key)
        for other_id, (other_ik, other_sk) in snapshot.items():
            if other_id != name and (new_identity_key == other_ik
                                     or new_signing_key == other_sk):
                log.warning(
                    "TOFU REFUSED for %s/%s: new keys collide with a DIFFERENT "
                    "known device %s — genuine ambiguity, manual investigation "
                    "required, never auto-trusted",
                    owner, name, other_id)
                report["refused"].append(name)
                return
        approval = approvals.get((owner, name))
        if (approval is not None
                and str(approval.get("new_identity_key") or "") == new_identity_key
                and str(approval.get("new_signing_key") or "") == new_signing_key):
            # Operator approval is an explicit override: VERIFY + upsert
            # directly (trust_device_tofu's same-keys gate would refuse —
            # the store may still hold the OLD record when the fetch did
            # not replace it). Same persistence tail as trust_device_tofu.
            from mautrix.types import TrustState as _TrustState

            device.trust = _TrustState.VERIFIED
            ok = False
            try:
                if hasattr(approval_store, "put_device"):
                    await approval_store.put_device(owner, device)
                else:
                    devices = await approval_store.get_devices(owner) or {}
                    devices[device.device_id] = device
                    await approval_store.put_devices(owner, devices)
                ok = True
            except Exception:  # noqa: BLE001 — persist failure refuses
                log.exception("e2ee approved-rotation persist failed for %s/%s",
                              owner, name)
            try:
                self.state.delete_meta(
                    _trust_meta_key(APPROVED_TRUST_META_PREFIX, owner, name))
            except Exception:  # noqa: BLE001 — approval already consumed
                pass
            if ok:
                log.warning(
                    "TOFU APPROVED-ROTATION for %s/%s: operator approval consumed, "
                    "new keys VERIFIED, Megolm rotation forced",
                    owner, name)
                report["trusted"].append(name)
                report["rotated"].append(name)
            else:
                report["refused"].append(name)
            return
        if approval is not None:
            log.warning(
                "TOFU REFUSED for %s/%s: keys changed AGAIN since the approval — "
                "stale approval ignored, fail closed with a fresh pending record",
                owner, name)
        rec = record_pending_trust(
            self.state, user_id=owner, device_id=name,
            old_identity_key=old_identity_key, old_signing_key=old_signing_key,
            new_identity_key=new_identity_key, new_signing_key=new_signing_key,
            reporter=sender_mxid)
        log.warning(
            "TOFU REFUSED for %s/%s: same device ID with changed keys "
            "(Element/Element X identity reset) — manual re-verify required, "
            "never silent re-trust. Old identity=%s signing=%s, new identity=%s "
            "signing=%s (first seen %s). To approve: %s",
            owner, name, old_identity_key, old_signing_key,
            new_identity_key, new_signing_key, rec.get("first_seen"),
            trust_device_command(name))
        report["refused"].append(name)
        report["pending"].append(name)

    async def ensure_owner_trust(self, sender_mxid: str) -> dict[str, list[str]]:
        """Fetch the owner's current device keys and TOFU-trust every
        first-seen device as VERIFIED for ``sender``'s machine; a device
        whose keys CHANGED since first sight lands in ``refused`` (fail
        closed — the caller surfaces it, never encrypts on TOFU authority).
        A same-ID change with fresh keys is additionally recorded as
        ``pending`` (actionable via ``trust-device``); the only path back
        to trusted is a matching operator approval, which lands in
        ``trusted`` + ``rotated``.

        Runs BEFORE ``share_group_session`` on purpose: mautrix 0.21.1
        resets every refetched device to UNVERIFIED (``_validate_device``),
        so trust must be applied after the last fetch. The share's own
        internal fetch then finds the devices already stored and keeps
        this trust. Returns ``trusted`` (new), ``known``, ``refused``,
        ``pending``, ``rotated`` and ``fetched`` device-id lists."""
        report: dict[str, list[str]] = {"trusted": [], "known": [],
                                        "refused": [], "fetched": [],
                                        "pending": [], "rotated": []}
        owner = (self.owner_mxid or "").strip()
        if not owner:
            return report
        from mautrix.types import UserID

        crypto = self.machine_for(sender_mxid)
        await crypto.load()
        machine = crypto.machine
        store = machine.crypto_store
        # Pre-fetch snapshot: the fetch below stores everything as
        # UNVERIFIED, so "first sight" is decided against THIS set — and
        # same-ID key changes (identity reset) are detected against it.
        # A signing-key change never survives mautrix validation (the
        # device is dropped from the fetch), so the snapshot also feeds
        # the raw-query supplement below.
        try:
            snapshot = {str(did): (str(dev.identity_key), str(dev.signing_key))
                        for did, dev in ((await store.get_devices(owner)) or {}).items()}
        except Exception:  # noqa: BLE001 — stub stores without device listing
            snapshot = {}
        approvals = {(str(rec.get("user_id") or ""), str(rec.get("device_id") or "")): rec
                     for rec in list_approved_trusts(self.state)}
        # Pinned mautrix 0.21.1 has no public fetch-untracked entry point;
        # _share_group_session uses this same call, so the contract is stable.
        fetched = await machine._fetch_keys(  # noqa: SLF001 — see above
            [UserID(owner)], include_untracked=True)
        devices = fetched.get(UserID(owner), {}) or {}
        for device_id, device in devices.items():
            name = str(device_id)
            report["fetched"].append(name)
            new_keys = (str(device.identity_key), str(device.signing_key))
            if name in snapshot and snapshot[name] != new_keys:
                await self._handle_same_id_change(
                    sender_mxid=sender_mxid, owner=owner, name=name,
                    old_identity_key=snapshot[name][0],
                    old_signing_key=snapshot[name][1], device=device,
                    snapshot=snapshot, approvals=approvals, report=report,
                    approval_store=store)
                continue
            sighted = name not in snapshot
            if sighted and any(new_keys[0] == ik or new_keys[1] == sk
                               for ik, sk in snapshot.values()):
                log.warning(
                    "TOFU REFUSED for %s/%s: brand-new device ID presents keys "
                    "already seen under a DIFFERENT device — genuine ambiguity, "
                    "manual investigation required, never auto-trusted",
                    owner, name)
                report["refused"].append(name)
                continue
            ok = await self.trust_device_tofu(sender_mxid, owner, device,
                                              first_sight=sighted)
            if not ok:
                report["refused"].append(name)
            elif sighted:
                report["trusted"].append(name)
            else:
                report["known"].append(name)
        # Supplement: known IDs the fetch dropped (mautrix validation
        # rejects a same-ID signing-key change, so an identity reset
        # vanishes here). Re-read the RAW advertised keys and classify
        # them the same way — this is the live rotation path.
        missing = [did for did in snapshot
                   if did not in {str(d) for d in devices}]
        if missing:
            await self._supplement_missing_devices(
                sender_mxid=sender_mxid, owner=owner, missing=missing,
                snapshot=snapshot, approvals=approvals, report=report,
                machine=machine)
        if not devices and not missing:
            log.warning("owner %s published no device keys — encrypted rooms stay "
                        "unreadable on their clients until they log in", owner)
        return report

    async def _supplement_missing_devices(
        self, *, sender_mxid: str, owner: str, missing: list[str],
        snapshot: dict[str, tuple[str, str]],
        approvals: dict[tuple[str, str], dict[str, Any]],
        report: dict[str, list[str]], machine: Any,
    ) -> None:
        """Classify known device IDs absent from the validated fetch via a
        raw key query (bypasses mautrix's drop-on-signing-change). Devices
        the server no longer advertises are genuinely deleted (left alone);
        advertised ones go through the same rotation/approval classification
        as fetched devices. Fail closed on any query/validation error."""
        from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID
        from mautrix.crypto.signature import verify_signature_json

        try:
            raw = await machine.client.query_keys([UserID(owner)])
        except Exception as exc:  # noqa: BLE001 — no raw keys, no healing
            log.warning("e2ee rotation supplement skipped for %s: key query failed: %s",
                        owner, exc)
            return
        try:
            advertised = (raw.device_keys.get(UserID(owner), {}) or {})
            by_name = {str(did): rk for did, rk in advertised.items()}
        except Exception:  # noqa: BLE001 — malformed response reads as empty
            by_name = {}
        for name in missing:
            raw_keys = by_name.get(name)
            if raw_keys is None:
                continue  # genuinely deleted server-side — stays gone
            try:
                signing_key = raw_keys.ed25519
                identity_key = raw_keys.curve25519
                name_attr = (getattr(getattr(raw_keys, "unsigned", None),
                                     "device_display_name", None) or name)
            except Exception:  # noqa: BLE001 — malformed device entry
                continue
            try:
                self_signed = bool(verify_signature_json(
                    raw_keys.serialize(), UserID(owner), DeviceID(name),
                    signing_key))
            except Exception:  # noqa: BLE001 — unverifiable advertisement
                self_signed = False
            if not self_signed:
                log.warning(
                    "TOFU REFUSED for %s/%s: re-advertised keys carry no valid "
                    "self-signature — fail closed, never re-trusted",
                    owner, name)
                report["refused"].append(name)
                continue
            new_keys = (str(identity_key), str(signing_key))
            device = DeviceIdentity(
                user_id=UserID(owner), device_id=DeviceID(name),
                identity_key=identity_key, signing_key=signing_key,
                trust=TrustState.UNVERIFIED, deleted=False, name=str(name_attr))
            report["fetched"].append(name)
            if snapshot.get(name) == new_keys:
                # Defensive: same keys the fetch dropped anyway — re-list as
                # known without touching trust.
                report["known"].append(name)
                continue
            await self._handle_same_id_change(
                sender_mxid=sender_mxid, owner=owner, name=name,
                old_identity_key=snapshot[name][0],
                old_signing_key=snapshot[name][1], device=device,
                snapshot=snapshot, approvals=approvals, report=report,
                approval_store=machine.crypto_store)

    async def verify_recipient_otks(
        self, machine: Any, members: list[str],
    ) -> dict[str, list[str]]:
        """CLAIM-VERIFY GUARD (D2 stale-pool defense): before sharing a
        Megolm session, claim one OTK per recipient device lacking an Olm
        session and verify its signature against the CURRENT advertised
        signing key (mautrix ``verify_signature_json``). The homeserver
        keeps serving OLD-signed OTKs after an Element/Element X identity
        reset; mautrix drops those sessions yet logs success with zero
        recipients — this guard names them instead.

        Returns ``share_users`` (members safe to pass to
        ``share_group_session`` — users whose every device failed are
        dropped), ``verified`` and ``stale`` device-id lists. Devices with
        a live Olm session skip the claim (reachable without an OTK) and
        count as verified. A failed claim RPC degrades to the legacy
        unfiltered share (fail-open: that error is not a stale pool)."""
        from mautrix.crypto.signature import verify_signature_json
        from mautrix.types import DeviceID, EncryptionKeyAlgorithm, UserID

        store = getattr(machine, "crypto_store", None)
        try:
            per_user: dict[str, dict[str, Any]] = {}
            if store is not None:
                for user in members:
                    try:
                        devs = await store.get_devices(UserID(user)) or {}
                    except Exception:  # noqa: BLE001 — per-user degrade
                        devs = {}
                    if devs:
                        per_user[str(user)] = {str(did): dev
                                              for did, dev in devs.items()}
        except Exception:  # noqa: BLE001 — stub stores without device listing
            return {"share_users": list(members), "verified": [],
                    "stale": [], "stale_pairs": []}
        need: dict[str, dict[str, Any]] = {}
        verified: list[str] = []
        for user, devs in per_user.items():
            for did, dev in devs.items():
                try:
                    has = bool(await store.has_session(dev.identity_key))
                except Exception:  # noqa: BLE001 — unknown reads as no session
                    has = False
                if has:
                    verified.append(did)
                else:
                    need.setdefault(user, {})[did] = dev
        stale_pairs: list[tuple[str, str]] = []
        if need:
            request = {UserID(user): {DeviceID(did): EncryptionKeyAlgorithm.SIGNED_CURVE25519
                                      for did in devs}
                       for user, devs in need.items()}
            try:
                resp = await machine.client.claim_keys(request)
            except Exception as exc:  # noqa: BLE001 — not a stale pool
                log.warning("e2ee claim-verify skipped: key claim failed (%s) — "
                            "proceeding to legacy share", exc)
                return {"share_users": list(members),
                        "verified": sorted(set(verified)
                                           | {d for devs in need.values()
                                              for d in devs}),
                        "stale": [], "stale_pairs": []}
            try:
                one_time_keys = getattr(resp, "one_time_keys", None) or {}
            except Exception:  # noqa: BLE001 — malformed response
                one_time_keys = {}
            for user, devs in need.items():
                got = (one_time_keys.get(UserID(user), {}) or {}
                       or one_time_keys.get(str(user), {}) or {})
                for did, dev in devs.items():
                    entries = (got.get(DeviceID(did), {}) or {}
                               or got.get(str(did), {}) or {})
                    if not entries:
                        stale_pairs.append((user, did))
                        continue
                    try:
                        _key_id, otk = next(iter(entries.items()))
                    except StopIteration:
                        stale_pairs.append((user, did))
                        continue
                    try:
                        data = (otk.serialize() if hasattr(otk, "serialize")
                                else dict(otk))
                        ok = bool(verify_signature_json(
                            data, UserID(user), DeviceID(did),
                            dev.signing_key))
                    except Exception:  # noqa: BLE001 — unverifiable reads stale
                        ok = False
                    if ok:
                        verified.append(did)
                    else:
                        stale_pairs.append((user, did))
        # Per-(user, device) staleness drives exclusion (a bare device id
        # can repeat across users); reports stay bare-device-id lists to
        # match the trusted/known/refused convention.
        stale_pair_set = set(stale_pairs)
        stale = sorted({did for _user, did in stale_pair_set})
        share_users = [u for u in members
                       if not (u in need and need[u]
                               and all((u, d) in stale_pair_set
                                       for d in need[u]))]
        return {"share_users": share_users,
                "verified": sorted(set(verified)), "stale": stale,
                "stale_pairs": sorted(stale_pair_set)}

    async def ensure_room_share(self, room_id: str, sender: str) -> dict[str, list[str]]:
        """TOFU-trust the owner's devices, then create/share the Megolm
        outbound session when none is live (missing, expired, or never
        shared). Before sharing, the claim-verify guard drops stale-pool
        devices; refused/pending (untrusted) devices are excluded too —
        without approval nothing encrypts to rotated keys. When ZERO
        devices verify, nothing is shared and the report carries
        ``failed-stale-pool`` (never a false success). Returns the trust
        report plus ``shared`` ([room_id] when a fresh share happened,
        else []), ``stale_excluded``, and ``pending``/``rotated``."""
        from mautrix.types import RoomID, UserID

        crypto = self.machine_for(sender)
        await crypto.load()
        machine = crypto.machine
        store = machine.crypto_store
        report = await self.ensure_owner_trust(sender)
        for bucket in ("pending", "rotated", "stale_excluded"):
            report.setdefault(bucket, [])
        session = await store.get_outbound_group_session(RoomID(room_id))
        # BUG1 (VM round 2): a tracked user that gained a first-seen device
        # id (``trusted``) or failed validation with changed keys
        # (``refused``) since the outbound session was created means the
        # live session never encrypted to that device. Rotate BEFORE the
        # shared/expired check so the share below is fresh.
        needs_rotate = bool(report.get("trusted") or report.get("refused"))
        if needs_rotate and session is not None:
            old_id = str(getattr(session, "id", getattr(session, "session_id", "?")))
            old_created = str(getattr(session, "creation_time", "?"))
            try:
                await store.remove_outbound_group_session(RoomID(room_id))
            except Exception:
                log.exception("e2ee reshare rotation failed for %s", room_id)
            else:
                log.info("e2ee reshare rotation for %s: dropped outbound %s (created %s) new %s refused %s", room_id, old_id, old_created, report.get("trusted"), report.get("refused"))
            session = None
            old_session_id = old_id
        else:
            old_session_id = None
        if (session is None or getattr(session, "expired", False)
                or not getattr(session, "shared", True)):
            members = await self._room_members(room_id, fallback_sender=sender)
            guard = await self.verify_recipient_otks(machine, list(members))
            if guard["stale"]:
                report["stale_excluded"] = list(guard["stale"])
            if guard["stale"] and not guard["verified"]:
                report["failed-stale-pool"] = list(guard["stale"])
                report["shared"] = []
                log.warning(
                    "E2EE STALE OTK POOL for %s: %d/%d recipient device(s) "
                    "presented one-time keys that FAIL signature verification "
                    "against their advertised signing keys (%s) — share SKIPPED, "
                    "no Megolm session created, refusing to log success with "
                    "zero recipients. This is the Element/Element X "
                    "identity-reset shape: resetting the identity replaces the "
                    "device key under the SAME device ID while the homeserver "
                    "keeps serving OTKs signed by the OLD key. REMEDY: bring "
                    "the owner client online so it publishes freshly-signed "
                    "OTKs (force-close + reopen Element, or sign out/in), then "
                    "send a fresh message to trigger a new share",
                    room_id, len(guard["stale"]),
                    len(guard["stale"]) + len(guard["verified"]),
                    ", ".join(guard["stale"]))
                return report
            # Exclude stale-pool AND untrusted (refused/pending) devices from
            # this share: without approval nothing encrypts to rotated keys.
            # The share API is per-user, so untrusted devices are
            # temporarily dropped from the store and restored afterwards —
            # the share's internal fetch only refetches users with NO stored
            # devices, so trust state survives.
            # refused/pending are owner devices (ensure_owner_trust only
            # classifies the owner); stale pairs already carry their user.
            owner = (self.owner_mxid or "").strip()
            excluded_pairs = (set(guard.get("stale_pairs", []))
                              | {(owner, d) for d in (report.get("refused") or [])}
                              | {(owner, d) for d in (report.get("pending") or [])})
            dropped: dict[str, dict[Any, Any]] = {}
            if excluded_pairs:
                for user in members:
                    try:
                        devs = await store.get_devices(UserID(user)) or {}
                    except Exception:  # noqa: BLE001 — stub/legacy stores
                        continue
                    if any((str(user), str(did)) in excluded_pairs
                           for did in devs):
                        dropped[str(user)] = dict(devs)
                        try:
                            await store.put_devices(
                                UserID(user),
                                {did: dev for did, dev in devs.items()
                                 if (str(user), str(did)) not in excluded_pairs})
                        except Exception:  # noqa: BLE001 — keep legacy share
                            dropped.pop(str(user), None)
            try:
                await machine.share_group_session(
                    RoomID(room_id), list(guard["share_users"]))
            finally:
                for user, devs in dropped.items():
                    try:
                        await store.put_devices(UserID(user), devs)
                    except Exception:  # noqa: BLE001 — teardown must not raise
                        log.warning("e2ee share device restore failed for %s",
                                    user, exc_info=True)
            report["shared"] = [room_id]
            if old_session_id is not None:
                try:
                    fresh = await store.get_outbound_group_session(RoomID(room_id))
                    new_id = str(getattr(fresh, "id", getattr(fresh, "session_id", "?"))) if fresh else "?"
                except Exception:
                    new_id = "?"
                log.info("e2ee reshare rotation for %s: %s -> %s", room_id, old_session_id, new_id)
        else:
            report["shared"] = []
        return report

    # -- outbound encryption ------------------------------------------------------

    async def encrypt_megolm(
        self, room_id: str, sender: str, event_type: str, content: dict[str, Any],
        *, report_out: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Encrypt one room event as ``sender``. Shares the group session
        with the room members on first use (bridge pattern: we KNOW the
        member set — owner + virtual users — the sidecar created the
        room), TOFU-trusting the owner's devices first. Returns
        ``m.room.encrypted`` content ready to PUT. The share ceremony
        report lands in ``report_out`` when provided (verify-notice path)."""
        from mautrix.types import EventType, RoomID

        report = await self.ensure_room_share(room_id, sender)
        if report_out is not None:
            report_out.update(report)
        crypto = self.machine_for(sender)
        machine = crypto.machine
        return (
            await machine.encrypt_megolm_event(
                # mautrix 0.21.1 EventType takes (type, t_class) — .find()
                # is the string lookup (direct construction TypeErrors).
                RoomID(room_id), EventType.find(event_type), content
            )
        ).serialize()
    async def send_encrypted_message(
        self,
        room_id: str,
        *,
        sender: str,
        body: str,
        formatted_body: str | None = None,
        relates_to: dict[str, Any] | None = None,
        report_out: dict[str, list[str]] | None = None,
    ) -> str:
        """Encrypt an ``m.room.message`` and PUT it (txnId form — the
        ruma/tuwunel-compatible send path, same law as
        ``matrix_client.MatrixClient._send_event``). The share ceremony
        report lands in ``report_out`` when provided (verify-notice path)."""
        from urllib.parse import quote

        content = message_content(body, formatted_body, relates_to)
        encrypted = await self.encrypt_megolm(
            room_id, sender, "m.room.message", content, report_out=report_out)
        txn = uuid.uuid4().hex
        path = f"{CLIENT_V3}/rooms/{quote(room_id, safe='')}/send/m.room.encrypted/{txn}"
        out = await self.client.client_api("PUT", path, sender=sender, json_body=encrypted)
        event_id = str((out or {}).get("event_id") or "")
        if not event_id:
            raise E2EEError(f"homeserver accepted no event id for encrypted send in {room_id}")
        return event_id

    # -- verify-howto notice (never silent darkness) -------------------------------

    def verify_notice_text(self, *, gateway_mxid: str, device_id: str,
                           fingerprint: str, trusted: list[str],
                           refused: list[str],
                           pending: list[str] | None = None) -> str:
        """The user-facing notice posted to the gateway room when a share
        reports refused or pending devices: what happened, the fingerprint
        to compare against the phone's device details screen, and the
        exact trust-device command for refused devices. Client-agnostic —
        no per-client steps. Single source so the room text and any
        operator docs never drift."""
        pending = list(pending or [])
        lines = [
            "Encrypted chat is on, but one verification step remains — until then",
            "your phone may show these messages as unverified or refuse to send its own.",
            "",
            f"This room is served by {gateway_mxid}, device {device_id}.",
            "Compare this fingerprint against the fingerprint shown on your phone's",
            "device details screen for that device:",
            f"   {fingerprint}",
            "",
        ]
        if trusted:
            lines += [
                f"New owner device(s) trusted on first sight: "
                f"{', '.join(trusted)}.",
                "If a device key ever changes, messages fail closed — never silently re-trusted.",
                "",
            ]
        if refused:
            lines += [
                "WARNING: these owner device(s) presented CHANGED keys and were NOT trusted:",
                f"   {', '.join(refused)}.",
                "Messages to those devices stay blocked until you confirm the new device",
                "in person and approve it with:",
            ]
            for did in refused:
                lines.append(f"   {trust_device_command(did)}")
            lines.append("")
        extra_pending = sorted(set(pending) - set(refused))
        if extra_pending:
            lines += [
                "These device(s) await approval:",
                f"   {', '.join(extra_pending)}.",
            ]
            for did in extra_pending:
                lines.append(f"   {trust_device_command(did)}")
            lines.append("")
        lines += ["After verifying, new messages arrive without warnings."]
        return "\n".join(lines)

    def _gateway_room_id(self) -> str:
        """Gateway room id from state (the ``gw`` node row). Empty when the
        gateway has not converged yet — callers skip instead of posting
        elsewhere (never directives, never agent rooms)."""
        try:
            gw = self.state.get("gw")
        except StateError:
            return ""
        return str((gw or {}).get("room_id") or "")

    async def maybe_post_verify_notice(self, room_id: str, *, sender: str,
                                       room_key: str, report: dict[str, Any]) -> bool:
        """Post the verify-howto notice to the GATEWAY room only, and only
        when the share report shows refused or pending devices. Routine
        first-sight TOFU trust never posts. Deduped per ``room_key`` via
        state meta: reposts only when the device picture (fingerprint,
        device, trusted, refused, pending) changes. The ``room_id``
        argument names the triggering room (dedupe scope only) — the
        notice itself always targets the gateway room resolved via state
        and is skipped when that room is unknown. Returns True when a
        notice was posted."""
        import json

        trusted = [str(d) for d in (report.get("trusted") or [])]
        refused = [str(d) for d in (report.get("refused") or [])]
        pending = [str(d) for d in (report.get("pending") or [])]
        if not refused and not pending:
            return False
        gw_room = self._gateway_room_id()
        if not gw_room:
            return False
        gateway_sender = self.gateway_mxid or sender
        crypto = self.machine_for(gateway_sender)
        await crypto.load()
        fingerprint = str(crypto.machine.account.fingerprint)
        device_id = str(crypto.device_id)
        picture = {"fp": fingerprint, "dev": device_id,
                   "trusted": sorted(trusted), "refused": sorted(refused),
                   "pending": sorted(pending)}
        meta_key = NOTICE_META_PREFIX + room_key
        try:
            seen_raw = self.state.get_meta(meta_key)
        except StateError:
            seen_raw = ""
        if seen_raw == json.dumps(picture, sort_keys=True):
            return False
        body = self.verify_notice_text(
            gateway_mxid=gateway_sender, device_id=device_id, fingerprint=fingerprint,
            trusted=trusted, refused=refused, pending=pending)
        await self.send_encrypted_message(gw_room, sender=gateway_sender, body=body)
        self.state.set_meta(meta_key, json.dumps(picture, sort_keys=True))
        return True

    async def _room_members(self, room_id: str, *, fallback_sender: str = "") -> list[str]:
        from urllib.parse import quote

        path = f"{CLIENT_V3}/rooms/{quote(room_id, safe='')}/members"
        # The gateway stays OUT of child rooms, so a gateway-masqueraded
        # members read 403s there (the owner is NOT masqueradeable).
        # Try the sender (always a member on the share path) first, then
        # the gateway. Never raises for membership alone — callers treat
        # failure as empty.
        readers: list[str | None] = []
        for cand in (fallback_sender or "", self.gateway_mxid or "", self.owner_mxid or "", None):
            if cand not in readers:
                # None (appservice sender, no masquerade) goes last.
                readers.append(cand)
        last_exc: Exception | None = None
        for reader in readers:
            try:
                out = await self.client.client_api("GET", path, sender=reader)
                chunk = (out or {}).get("chunk", []) if isinstance(out, dict) else []
                return [
                    e["state_key"]
                    for e in chunk
                    if isinstance(e, dict) and e.get("type") == "m.room.member"
                    and (e.get("content") or {}).get("membership") in ("join", "invite")
                    and e.get("state_key")
                ]
            except Exception as exc:  # noqa: BLE001 — try next reader
                last_exc = exc
                continue
        if last_exc is not None:
            # Every reader failed (ghost-not-member everywhere): unreadable
            # is empty, never a raise — the share path treats failure as
            # empty and fails closed downstream, and a members failure must
            # never veto the child turn's render. Loud at debug.
            log.debug("room members unreadable for %s: %s", room_id, last_exc)
        return []

    # -- inbound decryption (appservice transaction pipeline) ----------------------
    async def decrypt_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Decrypt one ``m.room.encrypted`` room event via the gateway
        agent's machine (any member machine decrypts — we hold every
        key). Returns the decrypted content dict, or None when the event
        is not ours to decrypt (foreign algorithm / missing fields)."""
        try:
            from mautrix.types import EncryptedEvent, EventType

            if event.get("type") != EventType.ROOM_ENCRYPTED.serialize():
                return None
            evt = wire_encrypted_event(event)
            crypto = self.machine_for(self.gateway_mxid or self._first_machine_mxid())
            await crypto.load()  # never decrypt on a cold machine (no account = always fail)
            decrypted = await crypto.machine.decrypt_megolm_event(evt)
            content = decrypted.content
            return content.serialize() if hasattr(content, "serialize") else dict(content)
        except Exception as exc:  # noqa: BLE001 — inbound decryption failure must
            # never kill the intake; the room shows a decryption-failure notice.
            log.warning("megolm decrypt failed for %s: %s", event.get("event_id"), exc)
            return None

    def _first_machine_mxid(self) -> str:
        if self._machines:
            return next(iter(self._machines))
        raise E2EEError(
            "no crypto machine loaded — decrypt before any machine_for() is a wiring bug"
        )


# ============================================================================
# Renderer intent hook — the encrypt flag flows through the EXECUTOR
# (spec M4c: coordinate via subclass; renderer.py is NOT edited)
# ============================================================================

#: Plaintext-window tolerance (seconds) between room creation and
#: ``m.room.encryption``: atomic creation lands both in the same instant;
#: anything wider means plaintext history exists (poisoned, defect iii).
POISON_GAP_SECONDS = 60.0

#: Recovery steps surfaced with every decrypt failure: written for clients
#: with no per-device verify screen and no key-request gesture, so the
#: notice names neither. Recovery is force-close/rejoin + a FRESH message,
#: with a re-converge offer for the reinstall-rotation case. Never a bare
#: "unable to decrypt".
DECRYPT_RECOVERY_STEPS = (
    "Recovery, in order: "
    "1) force-close Element X completely, reopen it, rejoin this room, then "
    "ask the sender to post a FRESH message (a new message, not a resend of "
    "the undecryptable one) — fresh messages use the current Megolm session "
    "and usually decrypt; "
    "2) if this message was sent BEFORE the sidecar was last reinstalled, it "
    "is UNRECOVERABLE by design — reinstalls ROTATE the Olm identity, so no "
    "keys exist anywhere that can decrypt pre-reinstall messages; ask the "
    "sender for a fresh message instead; "
    "3) if even fresh messages fail, purge + re-converge this room encrypted "
    "from the start (`mercury setup observatory` offers this automatically), "
    "then ask the sender to post again. "
    "Element X has no per-device verify screen and no key-request gesture — "
    "do not look for them."
)


def decrypt_failure_notice(event_id: str | None, room_id: str) -> str:
    """Human notice for one undecryptable event + recovery steps."""
    return (
        f"⚠️ Could not decrypt event {event_id or '(unknown)'} in {room_id}. "
        + DECRYPT_RECOVERY_STEPS
    )


async def detect_poisoned_rooms(
    client: Any,
    rooms: list[tuple[str, str]],
    *,
    sender: str,
    gap_seconds: float = POISON_GAP_SECONDS,
) -> list[dict[str, Any]]:
    """Find rooms with undecryptable plaintext history (defect iii).

    A room is poisoned when it is unencrypted (encryption state 404 — a
    pre-fix plaintext room) or was encrypted LATER than ``gap_seconds``
    after creation (plaintext window). Atomically created rooms land both
    state events in the same instant and never appear here. Returns one
    dict per problem room (``key``, ``room_id``, ``status``,
    ``gap_seconds``); clean rooms are omitted. Never raises — per-room
    failures degrade to omission (detection must not block setup).
    """
    problems: list[dict[str, Any]] = []

    def _ts(state_event: Any) -> float | None:
        try:
            ts = (state_event or {}).get("origin_server_ts")
            return float(ts) / 1000.0 if ts is not None else None
        except Exception:  # noqa: BLE001 — malformed state degrades to unknown
            return None

    for key, room_id in rooms:
        try:
            try:
                created = await client.get_room_state(
                    room_id, "m.room.create", "", sender=sender)
            except Exception:  # noqa: BLE001 — unreadable room: skip it
                continue
            try:
                encrypted = await client.get_room_state(
                    room_id, "m.room.encryption", "", sender=sender)
            except Exception:  # noqa: BLE001 — 404 = never encrypted
                problems.append({"key": key, "room_id": room_id,
                                 "status": "unencrypted", "gap_seconds": None})
                continue
            created_ts, encrypted_ts = _ts(created), _ts(encrypted)
            if created_ts is None or encrypted_ts is None:
                continue
            gap = encrypted_ts - created_ts
            if gap > gap_seconds:
                problems.append({"key": key, "room_id": room_id,
                                 "status": "poisoned", "gap_seconds": gap})
        except Exception:  # noqa: BLE001 — one bad room never kills the scan
            continue
    return problems


class EncryptedIntentExecutor:
    """Compose-in wrapper around ``renderer.IntentExecutor``: identical
    intent surface, but every chat room it creates gets
    ``m.room.encryption`` at creation (D4) and every SendMessage /
    EditMessage into a ``crypt:``-registered room is sent as
    ``m.room.encrypted``. When the E2EE flag is off the sidecar simply
    builds the plain ``IntentExecutor`` — this class is never
    constructed.

    Implemented by composition (same ``client``/``state``/presets
    surface, delegates every non-message intent to the wrapped
    executor) so it tracks renderer.py's evolution without inheriting
    its private branches.
    """

    def __init__(
        self,
        client: Any,
        state: ObservatoryState,
        *,
        owner_mxid: str,
        server_name: str,
        e2ee: "E2EEManager | Any",
        space_preset: str = "private_chat",
        room_preset: str = "trusted_private_chat",
        gateway_mxid: str = "",
    ) -> None:
        from observatory.renderer import IntentExecutor

        self._inner = IntentExecutor(
            client,
            state,
            owner_mxid=owner_mxid,
            server_name=server_name,
            space_preset=space_preset,
            room_preset=room_preset,
            gateway_mxid=gateway_mxid,
        )
        self.client = client
        self.state = state
        self.owner_mxid = owner_mxid
        self.server_name = server_name
        self.gateway_mxid = gateway_mxid
        self.e2ee = e2ee

    # --- IntentExecutor API (duck-typed — the renderer only calls these) ----

    def room_id(self, key: str) -> str:
        return self._inner.room_id(key)

    def space_id(self, key: str) -> str:
        return self._inner.space_id(key)

    @property
    def room_preset(self) -> str:
        return self._inner.room_preset

    async def execute(self, intents) -> list[dict[str, Any]]:
        from observatory.renderer import CreateRoom, EditMessage, SendMessage

        records: list[dict[str, Any]] = []
        for op in intents:
            if isinstance(op, CreateRoom):
                rid = await self._create_encrypted_room(op)
                records.append({"op": "create_room", "key": op.key, "room_id": rid,
                                "encrypted": True})
            elif isinstance(op, SendMessage) and self.e2ee.room_is_encrypted(op.room_key):
                rid = self.room_id(op.room_key)
                report: dict[str, Any] = {}
                event_id = await self.e2ee.send_encrypted_message(
                    rid,
                    sender=op.sender,
                    body=op.body,
                    formatted_body=op.formatted_body,
                    report_out=report,
                )
                await self.e2ee.maybe_post_verify_notice(
                    rid, sender=op.sender, room_key=op.room_key, report=report)
                if op.tag:
                    self.state.set_meta(op.tag, event_id)
                records.append({"op": "send", "room": rid, "event_id": event_id,
                                "encrypted": True, "tag": op.tag})
            elif isinstance(op, EditMessage) and self.e2ee.room_is_encrypted(op.room_key):
                rid = self.room_id(op.room_key)
                report: dict[str, Any] = {}
                event_id = await self.e2ee.send_encrypted_message(
                    rid,
                    sender=op.sender,
                    body=f"* {op.body}",
                    formatted_body=op.formatted_body,
                    relates_to={"rel_type": "m.replace", "event_id": op.event_id},
                    report_out=report,
                )
                await self.e2ee.maybe_post_verify_notice(
                    rid, sender=op.sender, room_key=op.room_key, report=report)
                records.append({"op": "edit", "room": rid, "replaces": op.event_id,
                                "event_id": event_id, "encrypted": True})
            else:
                records.extend(await self._inner.execute([op]))
        return records

    async def _create_encrypted_room(self, op: "CreateRoom") -> str:
        """Encrypted from the FIRST event (defect iii): ``m.room.encryption``
        rides ``initial_state`` in the creation call itself — never a
        follow-up PUT (a plaintext window poisons history: pre-encryption
        events stay undecryptable forever). Owner PL + registry entry
        follow the base executor's law."""
        inner = self._inner
        await inner._ensure_sender_registered(op.sender)
        room_id = await inner.client.create_room(
            name=op.name,
            sender=op.sender,
            preset=inner.room_preset,
            invite=inner._create_invites(op, space=False),
            initial_state=[{
                "type": "m.room.encryption",
                "state_key": "",
                "content": dict(ENCRYPTION_CONTENT),
            }],
        )
        try:
            await inner.client.set_power_levels(
                room_id, {inner.owner_mxid: 100}, sender=op.sender)
        except Exception as exc:  # noqa: BLE001 — power failure never orphans the id
            if inner._is_not_member_error(exc):
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "create power skipped for %s (not-member): %s", room_id, exc)
            else:
                raise
        # Owner auto-join like the base executor (VM round 2): the creation
        # invite alone leaves a pending invite the owner must tap.
        await inner.ensure_owner_in_room(room_id)
        # Parent-voice membership at creation (parity with the base
        # executor): the gateway ghost stays OUT of child rooms.
        parent_voice = inner._parent_voice_for_create(op, space=False)
        if parent_voice:
            await inner.ensure_ghost_in_room(room_id, parent_voice)
        inner._record_room(op.key, room_id)
        self.e2ee.mark_room_encrypted(op.key, room_id)
        return room_id

    async def ensure_owner_in_plan(self, plan) -> int:
        """Converge-time owner-membership heal (delegates to the wrapped
        executor — same surface the renderer calls on the plain one)."""
        return await self._inner.ensure_owner_in_plan(plan)

    async def ensure_gateway_leaves_plan(self, plan) -> int:
        """Converge-time gateway-leave heal (delegates to the wrapped
        executor — removes the gateway ghost from child rooms/spaces)."""
        return await self._inner.ensure_gateway_leaves_plan(plan)

    async def ensure_owner_in_room(self, room_id: str) -> bool:
        return await self._inner.ensure_owner_in_room(room_id)

    async def ensure_ghost_in_room(self, room_id: str, mxid: str) -> bool:
        return await self._inner.ensure_ghost_in_room(room_id, mxid)
