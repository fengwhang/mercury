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
(3.2.16 ships cp310–cp312 only), so the observatory carries a
reproducible podman wheel build:
``observatory/scripts/build_python_olm_wheel.sh`` (python:3.13-slim +
g++/make/cmake; the sdist bundles the full libolm C++ sources). Log and
wheel land under ``observatory/scripts/{logs,dist}/``.

**Key bootstrap — the manual verify step (documented, D4 bridge pattern):**

1. The owner logs in on a second device (device O1 — FluffyChat is the
   tested client) and — once — verifies the
   gateway agent's device in the gateway room ("Verify manually" /
   emoji/SAS or key-pin): display the gateway machine's ed25519 key from
   ``observatory crypto-key @merc_gateway:<server>`` output. This pins
   the root of the observatory's trust. The store makes this a ONE-TIME
   step: the gateway's Olm identity survives sidecar restarts, so the
   owner's verification stays valid.
2. Virtual users (one machine each) apply **trust-on-first-use (TOFU)**
   to the owner's devices — the O3-sanctioned fallback: the first device
   key seen for ``@owner:<server>`` is marked ``TrustState.VERIFIED``
   (:meth:`E2EEManager.trust_device_tofu`); a CHANGED key later fails
   decryption (DecryptionError), never silently re-trusts. TOFU is
   spec-sanctioned because the homeserver is single-owner,
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
   sidecar intake must call it with each raw transaction body (the
   room-event half of that pipeline is already wired via
   ``decrypt_event`` in sidecar_main).
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

#: Exact remedy surfaced by :class:`E2EEError` (single source, tested).
E2EE_REMEDY = (
    "E2EE is enabled (observatory.e2ee: true) but the crypto stack is "
    "missing. Build + install it (reproducible, no host compiler "
    "needed), then restart the sidecar:  "
    "observatory/scripts/build_python_olm_wheel.sh <venv-dir>  "
    "(podman build of python-olm for cp313 — mautrix 0.21.1 requires "
    "python-olm; upstream ships no cp313 wheel). "
    "Or set observatory.e2ee: false for the plaintext path."
)

CLIENT_V3 = "/_matrix/client/v3"


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
    gateway agent — a member of every room the sidecar creates), shared
    rooms from the ``crypt:`` registry in ``observatory/state.db``."""

    def __init__(self, client: Any, state: ObservatoryState, *, reader_mxid: str) -> None:
        self._client = client
        self._state = state
        self._reader_mxid = reader_mxid

    async def is_encrypted(self, room_id) -> bool:
        info = await self.get_encryption_info(room_id)
        return info is not None

    async def get_encryption_info(self, room_id):
        from mautrix.types import RoomEncryptionStateEventContent

        path = f"{CLIENT_V3}/rooms/{room_id}/state/m.room.encryption/"
        try:
            out = await self._client.client_api("GET", path, sender=self._reader_mxid)
        except Exception:  # noqa: BLE001 — 404 == not encrypted
            return None
        if not isinstance(out, dict):
            return None
        return RoomEncryptionStateEventContent.deserialize(out)

    async def find_shared_rooms(self, user_id) -> list:
        """Encrypted rooms ``user_id`` could share keys in — the safe
        direction for ``remove_outbound_group_sessions`` is a superset,
        and every crypt-registry room has owner + virtual users as
        members, so all of them qualify.
        """
        rows = self._state._db.execute(  # noqa: SLF001 — registry scan, no scan API
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
        await super().put_devices(user_id, devices)
        rows = [(str(user_id), str(did), str(dev.identity_key), str(dev.signing_key),
                 int(dev.trust), int(dev.deleted), dev.name or "")
                for did, dev in (devices or {}).items()]
        await self._db.execute("INSERT OR REPLACE INTO tracked_users VALUES (?)",
                               (str(user_id),))
        await self._db.execute("DELETE FROM devices WHERE user_id=?", (str(user_id),))
        if rows:
            await self._db.executemany(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
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

    # -- cross-signing (persisted for interface completeness; SSSS stays unwired) ---

    async def put_cross_signing_key(self, user_id, usage, key) -> None:
        await super().put_cross_signing_key(user_id, usage, key)
        await self._write(
            "INSERT INTO cross_signing_keys VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user_id, usage) DO UPDATE SET key=excluded.key",
            (str(user_id), usage.value, str(key), str(key)),
        )

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
        WORK #2 closer). Tuwunel/ruma transaction shape:

        * ``to_device``: ``{user_id: {device_id: {sender, type, content}}}``
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
        to_device = txn.get("to_device") or {}
        if isinstance(to_device, dict):
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
        raw_lists = txn.get("device_lists")
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
                  or txn.get("device_one_time_keys_counts") or {})
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

    # -- TOFU trust (O3-sanctioned fallback; manual owner step documented up top)

    async def trust_device_tofu(self, mxid: str, target_user: str, device: Any) -> None:
        """Mark a first-seen device of ``target_user`` as verified for the
        given virtual user's machine. TOFU: only first sight; a device
        that CHANGES key later fails decryption — never re-trusted."""
        from mautrix.types import TrustState

        crypto = self.machine_for(mxid)
        store = crypto.machine.crypto_store
        existing = await store.get_device(target_user, device.device_id)
        if existing is not None:
            return  # already known — TOFU only applies at first sight
        device.trust = TrustState.VERIFIED
        if hasattr(store, "put_device"):  # SQLiteCryptoStore — single upsert
            await store.put_device(target_user, device)
        else:  # upstream memory store: replace-set API only
            devices = await store.get_devices(target_user) or {}
            devices[device.device_id] = device
            await store.put_devices(target_user, devices)

    # -- outbound encryption ------------------------------------------------------

    async def encrypt_megolm(
        self, room_id: str, sender: str, event_type: str, content: dict[str, Any]
    ) -> dict[str, Any]:
        """Encrypt one room event as ``sender``. Shares the group session
        with the room members on first use (bridge pattern: we KNOW the
        member set — owner + virtual users — the sidecar created the
        room). Returns ``m.room.encrypted`` content ready to PUT."""
        from mautrix.types import EventType, RoomID

        crypto = self.machine_for(sender)
        await crypto.load()
        machine = crypto.machine
        if not await machine.crypto_store.get_outbound_group_session(RoomID(room_id)):
            members = await self._room_members(room_id)
            await machine.share_group_session(RoomID(room_id), list(members))
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
    ) -> str:
        """Encrypt an ``m.room.message`` and PUT it (txnId form — the
        ruma/tuwunel-compatible send path, same law as
        ``matrix_client.MatrixClient._send_event``)."""
        from urllib.parse import quote

        content = message_content(body, formatted_body, relates_to)
        encrypted = await self.encrypt_megolm(room_id, sender, "m.room.message", content)
        txn = uuid.uuid4().hex
        path = f"{CLIENT_V3}/rooms/{quote(room_id, safe='')}/send/m.room.encrypted/{txn}"
        out = await self.client.client_api("PUT", path, sender=sender, json_body=encrypted)
        event_id = str((out or {}).get("event_id") or "")
        if not event_id:
            raise E2EEError(f"homeserver accepted no event id for encrypted send in {room_id}")
        return event_id

    async def _room_members(self, room_id: str) -> list[str]:
        from urllib.parse import quote

        path = f"{CLIENT_V3}/rooms/{quote(room_id, safe='')}/members"
        # read as the GATEWAY (a member of every sidecar room, and inside
        # the appservice namespace — the owner is NOT masqueradeable)
        out = await self.client.client_api(
            "GET", path, sender=self.gateway_mxid or self.owner_mxid or None)
        chunk = (out or {}).get("chunk", []) if isinstance(out, dict) else []
        return [
            e["state_key"]
            for e in chunk
            if isinstance(e, dict) and e.get("type") == "m.room.member"
            and (e.get("content") or {}).get("membership") in ("join", "invite")
            and e.get("state_key")
        ]

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
    ) -> None:
        from observatory.renderer import IntentExecutor

        self._inner = IntentExecutor(
            client,
            state,
            owner_mxid=owner_mxid,
            server_name=server_name,
            space_preset=space_preset,
            room_preset=room_preset,
        )
        self.client = client
        self.state = state
        self.owner_mxid = owner_mxid
        self.server_name = server_name
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
                event_id = await self.e2ee.send_encrypted_message(
                    rid,
                    sender=op.sender,
                    body=op.body,
                    formatted_body=op.formatted_body,
                )
                if op.tag:
                    self.state.set_meta(op.tag, event_id)
                records.append({"op": "send", "room": rid, "event_id": event_id,
                                "encrypted": True, "tag": op.tag})
            elif isinstance(op, EditMessage) and self.e2ee.room_is_encrypted(op.room_key):
                rid = self.room_id(op.room_key)
                event_id = await self.e2ee.send_encrypted_message(
                    rid,
                    sender=op.sender,
                    body=f"* {op.body}",
                    formatted_body=op.formatted_body,
                    relates_to={"rel_type": "m.replace", "event_id": op.event_id},
                )
                records.append({"op": "edit", "room": rid, "replaces": op.event_id,
                                "event_id": event_id, "encrypted": True})
            else:
                records.extend(await self._inner.execute([op]))
        return records

    async def _create_encrypted_room(self, op: "CreateRoom") -> str:
        """Plain create + owner PL (the base executor's law), then D4:
        ``m.room.encryption`` on every CHAT room, registry entry after."""
        rid = await self._inner.execute([op])
        room_id = str(rid[0].get("room_id") or "")
        await self.e2ee.enable_room_encryption(room_id, sender=op.sender)
        self.e2ee.mark_room_encrypted(op.key, room_id)
        return room_id
