"""LIVE E2EE gate for the Observatory sidecar — marker ``MERCURY-E2EE-OK``.

Proves D4 with REAL cryptography against a REAL homeserver (the render_live
testhome harness: ``provision`` + our own tuwunel process):

1. **Boot** — provision the throwaway home, boot tuwunel, seed the gateway
   virtual user, start :class:`~observatory.e2ee.E2EEManager` with the
   persistent SQLite stores and the :class:`EncryptedIntentExecutor`.
2. **Owner device O1** — a SECOND OlmMachine authenticated with the owner's
   real access token (a genuine independent Matrix client, not the
   appservice masquerade). One scripted MANUAL VERIFY step: fetch the
   gateway device's key from the server and compare it to the sidecar's
   locally displayed fingerprint — equality is what a human eyeballs in
   Element; the script asserts it, then marks the device VERIFIED both
   ways (owner→gateway manual, gateway→owner TOFU per spec O3).
3. **Encrypted room via the executor** — ``CreateSpace`` + ``CreateRoom``
   through the encrypted executor: ``m.room.encryption`` state lands on
   the room (checked on the wire).
4. **Encrypt → wire → second device** — ``SendMessage`` through the
   executor; the SERVER copy is fetched with the admin API and asserted
   to be ``m.room.encrypted`` (megolm ciphertext on the wire, plaintext
   absent); O1 receives the room key via its ``/sync`` to-device queue,
   decrypts the event, and the plaintext must match byte-for-byte.
5. **Encrypted edit round-trip** — ``EditMessage`` (``m.replace`` INSIDE
   the megolm ciphertext); O1 decrypts and the ``m.new_content`` +
   ``m.relates_to`` assertions hold.
6. **Restart persistence** — a BRAND NEW ``E2EEManager`` over the same
   crypto dir (fresh objects = sidecar restart simulation) rehydrates the
   gateway identity from SQLite and decrypts the ORIGINAL first message
   again (D8 transcript grace).

Run:  ``.venv/bin/python observatory/scripts/e2ee_live_gate.py [--fresh]``
Log:  ``~/.mercury/observatory-build/gate-e2ee.log`` — refuses to run
against the real ``~/.mercury/observatory``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from observatory import e2ee as e2ee_mod
from observatory import provision
from observatory.config_gen import ObservatoryPaths
from observatory.e2ee import (
    EncryptedIntentExecutor,
    E2EEManager,
    crypto_dir_for,
    e2ee_available,
    wire_encrypted_event,
)
from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixClient
from observatory.render_live import Gate, load_runtime
from observatory.renderer import CreateRoom, CreateSpace, EditMessage, SendMessage
from observatory.state import ObservatoryState
from observatory.appservice import as_token_from_registration

GATE_MARKER = "MERCURY-E2EE-OK"
GATE_FAIL_MARKER = "MERCURY-E2EE-FAIL"
DEFAULT_HOME = Path.home() / ".mercury" / "observatory-build" / "testhome"
CLIENT_V3 = "/_matrix/client/v3"

log = logging.getLogger("e2ee-gate")

GW = "gw-e2ee"  # one gateway-shaped virtual user is enough for this gate
OWNER_DEVICE = "E2EEGATEO1"


# ---------------------------------------------------------------------------
# Owner device O1 — a second, independent Matrix client
# ---------------------------------------------------------------------------

class _OwnerDeviceClient:
    """The mautrix client surface for the owner's device: same contract as
    ``e2ee._SidecarCryptoClient`` but authenticated with the OWNER's own
    device token (password login with a dedicated device id — tuwunel
    requires the upload's device id to match the token's device) — a real
    second client, not the appservice masquerade."""

    def add_event_handler(self, event_type, handler, **_) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def dispatch_event(self, event, source=None) -> None:  # pragma: no cover
        pass

    def __init__(self, mxid: str, device_id: str, client: MatrixClient,
                 password: str) -> None:
        self.mxid = mxid
        self.device_id = device_id
        self._client = client
        self._token: str | None = None
        self._password = password
        self._handlers: dict = {}

    async def login(self) -> None:
        out = await self._client.client_api("POST", f"{CLIENT_V3}/login", json_body={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": self.mxid},
            "password": self._password,
            "device_id": self.device_id,
        })
        self._token = str((out or {}).get("access_token") or "")
        if not self._token:
            raise RuntimeError("owner device login returned no token")

    async def _api(self, method: str, path: str, json_body=None, params=None):
        from observatory.matrix_client import MatrixError

        assert self._token is not None, "login() first"
        try:
            return await self._client._request(  # noqa: SLF001 — same-package transport
                method, path, token=self._token, params=params, json_body=json_body)
        except MatrixError as exc:
            if exc.status != 401:
                raise
            self._token = None
            await self.login()
            return await self._client._request(  # noqa: SLF001
                method, path, token=self._token, params=params, json_body=json_body)

    @property
    def token(self) -> str:
        assert self._token is not None, "login() first"
        return self._token

    async def upload_keys(self, one_time_keys=None, device_keys=None) -> dict:
        # Same contract as e2ee._SidecarCryptoClient.upload_keys: the
        # machine .get()s the no-arg result with an EncryptionKeyAlgorithm
        # member, so keys MUST be enum members, not strings.
        from mautrix.types import EncryptionKeyAlgorithm

        body: dict = {}
        if device_keys is not None:
            body["device_keys"] = (device_keys.serialize()
                                   if hasattr(device_keys, "serialize") else device_keys)
        if one_time_keys is not None:
            body["one_time_keys"] = dict(one_time_keys)
        out = await self._api("POST", f"{CLIENT_V3}/keys/upload", json_body=body)
        counts = (out or {}).get("one_time_key_counts", {})
        keyed: dict = {}
        for alg, count in (counts or {}).items():
            try:
                keyed[EncryptionKeyAlgorithm.deserialize(alg)] = count
            except Exception:  # noqa: BLE001 — unknown algorithm: keep raw key
                keyed[alg] = count
        return keyed

    async def query_keys(self, users, token=None):
        from mautrix.types import QueryKeysResponse

        if isinstance(users, dict):
            device_keys = {str(u): [str(d) for d in (devs or [])]
                           for u, devs in users.items()}
        else:
            device_keys = {str(u): [] for u in users}
        body: dict = {"device_keys": device_keys, "timeout": 0}
        if token:
            body["token"] = str(token)
        out = await self._api("POST", f"{CLIENT_V3}/keys/query", json_body=body)
        return QueryKeysResponse.deserialize(out)

    async def claim_keys(self, request):
        from mautrix.types import ClaimKeysResponse

        out = await self._api("POST", f"{CLIENT_V3}/keys/claim", json_body={
            "one_time_keys": {
                str(user): {str(dev): (alg.serialize() if hasattr(alg, "serialize")
                                       else str(alg))
                            for dev, alg in (devs or {}).items()}
                for user, devs in (request or {}).items()},
            "timeout": 0,
        })
        return ClaimKeysResponse.deserialize(out)

    async def get_state_event(self, room_id, event_type):
        from mautrix.errors import MForbidden, MNotFound
        from mautrix.types import RoomEncryptionStateEventContent
        from observatory.matrix_client import MatrixError

        et = event_type.serialize() if hasattr(event_type, "serialize") else event_type
        try:
            out = await self._api("GET", f"{CLIENT_V3}/rooms/{room_id}/state/{et}")
        except MatrixError as exc:
            # Same law as e2ee._SidecarCryptoClient.get_state_event:
            # mautrix only catches its own MNotFound/MForbidden.
            if exc.status == 404:
                raise MNotFound(404, f"room state not found: {room_id}") from exc
            if exc.status == 403:
                raise MForbidden(403, f"room state forbidden: {room_id}") from exc
            raise
        if not isinstance(out, dict):
            return None
        return RoomEncryptionStateEventContent.deserialize(out)

    async def upload_one_signature(self, *a, **k):  # pragma: no cover
        raise NotImplementedError("SSSS not driven by the gate")

    async def upload_cross_signing_keys(self, *a, **k):  # pragma: no cover
        raise NotImplementedError("SSSS not driven by the gate")


class _OwnerStateStore:
    """Room-encryption StateStore for O1 (owner-token reads)."""

    def __init__(self, client: MatrixClient, reader_mxid: str) -> None:
        self._client = client
        self._reader = reader_mxid

    async def is_encrypted(self, room_id) -> bool:
        return await self.get_encryption_info(room_id) is not None

    async def get_encryption_info(self, room_id):
        from mautrix.types import RoomEncryptionStateEventContent

        try:
            out = await self._client.admin_api(
                "GET", f"{CLIENT_V3}/rooms/{room_id}/state/m.room.encryption/")
        except Exception:  # noqa: BLE001 — 404 == not encrypted
            return None
        if not isinstance(out, dict):
            return None
        return RoomEncryptionStateEventContent.deserialize(out)

    async def find_shared_rooms(self, user_id) -> list:
        return []


async def owner_receive_to_device(client: MatrixClient, owner_mxid: str,
                                  owner_device: str, machine, *, token: str) -> int:
    """Pull O1's to-device queue with one real /sync and feed every event
    into its machine (exactly what the appservice intake will do via
    ``E2EEManager.handle_as_transaction``). The /sync MUST run as O1's
    own device token: to-device queues are per-device, so syncing as the
    owner's original login token would read the WRONG queue (empty)."""
    from mautrix.types import ASToDeviceEvent

    out = await client._request(  # noqa: SLF001 — same-package transport
        "GET", f"{CLIENT_V3}/sync", token=token,
        params={"timeout": "0", "filter": '{"room":{"timeline":{"limit":1}}}'})
    events = ((out or {}).get("to_device") or {}).get("events") or []
    delivered = 0
    for raw in events:
        try:
            evt = ASToDeviceEvent.deserialize({
                **raw, "to_user_id": owner_mxid, "to_device_id": owner_device})
            await machine.handle_as_to_device_event(evt)
            delivered += 1
        except Exception:  # noqa: BLE001 — one bad event never kills delivery
            log.warning("owner to-device delivery failed: %s", raw, exc_info=True)
    return delivered


async def room_wire_messages(client: MatrixClient, room_id: str,
                             *, sender: str, limit: int = 10) -> list:
    """Server-stored timeline read AS A JOINED MEMBER (the gateway) via
    plain client ``/messages``. The Synapse-style admin endpoint needs no
    membership but is not a tuwunel-core path; this asserts the same
    server-persisted events through the membership-honest channel."""
    from urllib.parse import quote

    out = await client.client_api(
        "GET", f"{CLIENT_V3}/rooms/{quote(room_id, safe='')}/messages",
        sender=sender, params={"dir": "b", "limit": str(limit)})
    chunk = (out or {}).get("chunk", []) if isinstance(out, dict) else []
    return list(chunk)


_OPEN_MANAGERS: list = []
"""Every E2EEManager the scenario constructs. ``main``'s finally stops
them all: each open SQLiteCryptoStore owns a NON-DAEMON aiosqlite worker
thread, so any FATAL exit that skips the explicit ``stop()`` calls would
otherwise hang the gate process forever AFTER printing the verdict."""


async def _stop_open_managers() -> None:
    while _OPEN_MANAGERS:
        manager = _OPEN_MANAGERS.pop()
        try:
            await manager.stop()
        except Exception:  # noqa: BLE001 — teardown must not raise
            log.warning("manager stop failed", exc_info=True)



# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

async def scenario(gate: Gate, paths: ObservatoryPaths, base_url: str, cfg: dict) -> None:
    from mautrix.crypto import OlmMachine
    from mautrix.crypto.store import MemoryCryptoStore
    from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID

    server_name = str(cfg.get("server_name", "mercury.local"))
    as_token = as_token_from_registration(paths.appservice_registration)
    owner = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    owner_mxid = owner["user_id"]

    state = ObservatoryState(paths.root / "state.db")
    from observatory.state import StateError as _StateError

    try:
        state.get(GW)
        have_gw = True
    except _StateError:
        have_gw = False
    if not have_gw:
        slug = assign_slug("e2ee-gate-gateway", state)
        state.add_node(GW, engine="hermes", name="e2ee gate gateway", slug=slug,
                       mxid=virtual_mxid(slug, server_name=server_name), session_ref=f"session:{GW}",
                       parent_node_id=None, extra={"kind": "gateway"})
    gw_row = state.get(GW)
    gw_mxid = gw_row["mxid"]
    gw_localpart = gw_mxid.lstrip("@").split(":", 1)[0]
    gate.check("observatory.e2ee flag reads true for the gate home",
               e2ee_mod.e2ee_enabled(paths.root.parent), f"home={paths.root.parent}")

    async with MatrixClient(
        base_url, as_token, server_name=server_name, admin_token=owner["access_token"]
    ) as client:
        try:
            await client.register_virtual_user(gw_localpart)
        except Exception as exc:  # noqa: BLE001 — ghost may already exist
            log.info("register %s: %s (continuing)", gw_localpart, exc)

        # --- sidecar side --------------------------------------------------
        e2ee = E2EEManager(
            client, state, crypto_dir=crypto_dir_for(paths.root.parent),
            owner_mxid=owner_mxid, gateway_mxid=gw_mxid,
        )
        _OPEN_MANAGERS.append(e2ee)
        await e2ee.start(enabled=True)
        gw_crypto = e2ee.machine_for(gw_mxid)
        await gw_crypto.load()
        gw_fingerprint = gw_crypto.machine.account.fingerprint
        gate.line(f"gateway device {gw_crypto.device_id} fingerprint: {gw_fingerprint}")

        # --- owner device O1 (second machine) --------------------------------
        o1_client = _OwnerDeviceClient(owner_mxid, OWNER_DEVICE, client,
                                       password=owner["password"])
        await o1_client.login()
        o1_state = _OwnerStateStore(client, reader_mxid=owner_mxid)
        o1 = OlmMachine(o1_client, MemoryCryptoStore(
            UserID(owner_mxid), f"e2ee-gate-pickle:{owner_mxid}:{OWNER_DEVICE}"), o1_state)
        await o1.load()
        await o1.share_keys()
        gate.line(f"owner device {OWNER_DEVICE} up "
                  f"(fingerprint {o1.account.fingerprint})")
        # --- the ONE manual verify step, scripted ----------------------------
        # What a human does in Element: compare the device key the SERVER
        # publishes against the key the sidecar displays locally.
        # mautrix 0.21.1 has no QueryKeysRequest — the gate client's
        # query_keys(users) posts the device_keys map itself.
        published = await o1_client.query_keys([gw_mxid])
        server_keys = ((published.device_keys or {}).get(gw_mxid) or {})
        gate.check("gateway device key published on the server",
                   gw_crypto.device_id in server_keys,
                   f"devices={list(server_keys)}")
        published_ed = ""
        if gw_crypto.device_id in server_keys:
            dev_keys = server_keys[gw_crypto.device_id]
            if isinstance(dev_keys, dict):  # raw shape — string key ids
                published_ed = str((dev_keys.get("keys") or {}).get(
                    f"ed25519:{gw_crypto.device_id}", "") or "")
            else:  # DeviceKeys attrs: .keys is KeyID-keyed — use .ed25519
                published_ed = str(getattr(dev_keys, "ed25519", "") or "")
        gate.check("manual verify: server key == sidecar-displayed fingerprint",
                   bool(published_ed) and published_ed == gw_fingerprint.replace(" ", ""),
                   f"server={published_ed[:24]}… local={gw_fingerprint.replace(' ', '')[:24]}…")
        # owner→gateway: manual verify; gateway→owner: TOFU (both VERIFIED)
        gw_identity = DeviceIdentity(
            user_id=UserID(owner_mxid), device_id=DeviceID(OWNER_DEVICE),
            identity_key=o1.account.identity_key, signing_key=o1.account.signing_key,
            trust=TrustState.VERIFIED, deleted=False, name="owner gate device",
        )
        await e2ee.trust_device_tofu(gw_mxid, owner_mxid, gw_identity)
        o1_gw_device = DeviceIdentity(
            user_id=UserID(gw_mxid), device_id=DeviceID(gw_crypto.device_id),
            identity_key=gw_crypto.machine.account.identity_key,
            signing_key=gw_crypto.machine.account.signing_key,
            trust=TrustState.VERIFIED, deleted=False, name="gateway",
        )
        # MemoryCryptoStore has no put_device (singular) — replace-set API only.
        await o1.crypto_store.put_devices(
            UserID(gw_mxid), {DeviceID(gw_crypto.device_id): o1_gw_device})

        # --- encrypted room via the executor ----------------------------------
        executor = EncryptedIntentExecutor(
            client, state, owner_mxid=owner_mxid, server_name=server_name, e2ee=e2ee
        )
        records = await executor.execute([
            CreateSpace(key=GW, name="E2EE Gate", sender=gw_mxid),
            CreateRoom(key=f"{GW}:room", name="e2ee-gate-room",
                       space_key=GW, sender=gw_mxid, kind="chat"),
        ])
        room_id = executor.room_id(f"{GW}:room")
        gate.check("encrypted executor created the room",
                   bool(room_id) and records[-1].get("encrypted") is True,
                   f"room={room_id} records={records[-1]}")
        # Read state AS THE GATEWAY (a joined member): the owner is only
        # INVITED at creation, and tuwunel correctly 404s non-member reads.
        enc_state = await client.client_api(
            "GET", f"{CLIENT_V3}/rooms/{room_id}/state/m.room.encryption/",
            sender=gw_mxid)
        gate.check("m.room.encryption state on the wire (megolm v1)",
                   (enc_state or {}).get("algorithm") == "m.megolm.v1.aes-sha2",
                   f"state={enc_state}")

        # --- encrypt -> wire -> second device -----------------------------------
        secret = f"MERCURY-E2EE-OK secret {time.time_ns()}"
        send_records = await executor.execute([
            SendMessage(room_key=f"{GW}:room", sender=gw_mxid, body=secret,
                       tag="e2ee:gate:first"),
        ])
        event_id = send_records[0]["event_id"]
        gate.check("executor sent the message as m.room.encrypted",
                   send_records[0].get("encrypted") is True and bool(event_id),
                   f"event={event_id}")

        wire = await room_wire_messages(client, room_id, sender=gw_mxid)
        enc_events = [e for e in wire if e.get("type") == "m.room.encrypted"]
        ours = next((e for e in enc_events
                     if e.get("event_id") == event_id or e.get("content", {}).get(
                         "algorithm") == "m.megolm.v1.aes-sha2"), None)
        gate.check("server copy IS megolm ciphertext (plaintext never on the wire)",
                   ours is not None and bool((ours or {}).get("content", {}).get("ciphertext"))
                   and secret not in json.dumps(wire),
                   f"content_keys={sorted((ours or {}).get('content', {}).keys())}")
        wire_event = ours

        # O1 receives the room key (real /sync queue) and decrypts
        delivered = await owner_receive_to_device(
            client, owner_mxid, OWNER_DEVICE, o1, token=o1_client.token)
        gate.check("owner device received to-device key material via /sync",
                   delivered >= 1, f"to_device events={delivered}")

        decrypted = None
        try:
            decrypted = await o1.decrypt_megolm_event(
                wire_encrypted_event(dict(wire_event)))
        except Exception as exc:  # noqa: BLE001 — report, not crash
            gate.line(f"[INFO] O1 decrypt failed: {type(exc).__name__}: {exc}")
        o1_body = (decrypted.content.body if decrypted is not None else None)
        gate.check("SECOND machine decrypts: plaintext matches byte-for-byte",
                   o1_body == secret, f"body={o1_body!r}")

        # --- encrypted edit round-trip (m.replace inside megolm) -----------------
        edited = f"MERCURY-E2EE-OK edited {time.time_ns()}"
        edit_records = await executor.execute([
            EditMessage(room_key=f"{GW}:room", sender=gw_mxid,
                        event_id=event_id, body=edited),
        ])
        gate.check("edit sent encrypted", edit_records[0].get("encrypted") is True,
                   f"event={edit_records[0]['event_id']}")

        wire2 = await room_wire_messages(client, room_id, sender=gw_mxid)
        edit_wire = next((e for e in wire2 if e.get("event_id")
                          == edit_records[0]["event_id"]), None)
        gate.check("edit on the wire is m.room.encrypted too",
                   edit_wire is not None and edit_wire.get("type") == "m.room.encrypted",
                   f"type={(edit_wire or {}).get('type')}")
        dec_edit = await o1.decrypt_megolm_event(
            wire_encrypted_event(dict(edit_wire)))
        c = dec_edit.content
        rel = getattr(c, "relates_to", None)
        rel_type = getattr(rel, "rel_type", None)
        # RelationType is an ExtensibleEnum (hash/eq UNEQUAL to its
        # string, live-gate proven) — coerce before comparing.
        rel_type_s = (rel_type.serialize() if hasattr(rel_type, "serialize")
                      else str(rel_type) if rel_type is not None else None)
        gate.check("decrypted edit is m.replace of the original",
                   rel_type_s == "m.replace"
                   and getattr(rel, "event_id", None) == event_id,
                   f"rel={rel!r}")
        # mautrix promotes m.new_content INTO the content on decrypt
        # (MessageEvent.deserialize_content) — there is no .new_content
        # wrapper post-decrypt; the replacement body IS c.body.
        gate.check("decrypted m.new_content body matches",
                   getattr(c, "body", None) == f"* {edited}",
                   f"body={getattr(c, 'body', None)!r}")

        # --- restart persistence (fresh manager over the same crypto dir) --------
        await e2ee.stop()
        e2ee2 = E2EEManager(
            client, state, crypto_dir=crypto_dir_for(paths.root.parent),
            owner_mxid=owner_mxid, gateway_mxid=gw_mxid,
        )
        _OPEN_MANAGERS.append(e2ee2)
        await e2ee2.start(enabled=True)
        gw2 = e2ee2.machine_for(gw_mxid)
        await gw2.load()
        gate.check("restart reattaches the SAME device identity",
                   gw2.device_id == gw_crypto.device_id
                   and gw2.machine.account.fingerprint == gw_fingerprint,
                   f"device={gw2.device_id} fp={gw2.machine.account.fingerprint[:24]}…")
        again = await e2ee2.decrypt_event(dict(wire_event))
        gate.check("post-restart decrypt of the ORIGINAL message (D8)",
                   again is not None and again.get("body") == secret,
                   f"body={(again or {}).get('body')!r}")
        await e2ee2.stop()

    state.close()


# ---------------------------------------------------------------------------
# Orchestration (mirrors render_live.main)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="E2EE live gate (MERCURY-E2EE-OK) — two-machine encrypted round trip")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME,
                        help="throwaway observatory home (default: %(default)s)")
    parser.add_argument("--fresh", action="store_true",
                        help="wipe the throwaway home first (clean-slate run)")
    args = parser.parse_args(argv)

    if not e2ee_available():
        print("REFUSING to run: crypto stack missing "
              "(install the vendored wheels — re-run `mercury setup observatory`, "
              "or uv pip install --find-links hermes/observatory/wheels "
              "'python-olm==3.2.16')", file=sys.stderr)
        return 2

    home = args.home.expanduser()
    if home.resolve() == (Path.home() / ".mercury" / "observatory").resolve():
        print("REFUSING to run the gate against the real ~/.mercury/observatory",
              file=sys.stderr)
        return 2
    if args.fresh:
        import shutil
        import tempfile

        # The tuwunel binary is an immutable fetched artifact (~100MB),
        # not gate state: stage it outside the home so --fresh stays
        # offline-clean. Without this, offline provision would fail
        # post-wipe AND a reused server DB would invalidate O1's fresh
        # device keys (same device id, new account = signing-key
        # rotation the gateway correctly refuses to trust).
        bin_dir = home / "observatory" / "bin"
        stage = Path(tempfile.mkdtemp(prefix="e2ee-gate-bin-"))
        try:
            for name in ("tuwunel", "tuwunel.version"):
                src = bin_dir / name
                if src.is_file():
                    shutil.copy2(src, stage / name)
            shutil.rmtree(home, ignore_errors=True)
            bin_dir.mkdir(parents=True, exist_ok=True)
            for name in ("tuwunel", "tuwunel.version"):
                staged = stage / name
                if staged.is_file():
                    shutil.copy2(staged, bin_dir / name)
            if (bin_dir / "tuwunel").is_file():
                (bin_dir / "tuwunel").chmod(0o755)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    home.mkdir(parents=True, exist_ok=True)
    gate_log = home.parent / "gate-e2ee.log"

    _force_offline_config(home)  # recorded for the sidecar later
    summary = provision.provision(mercury_home=home, systemd=False, offline=True)
    gate = Gate(gate_log)
    gate.line(f"== E2EE live gate == {time.strftime('%Y-%m-%d %H:%M:%S')} home={home}")
    gate.line(f"provision: {json.dumps(summary)}")

    paths = ObservatoryPaths(home)
    cfg, base_url = load_runtime(paths)

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    server_log = (paths.logs_dir / "tuwunel-e2ee-gate.log").open("wb")
    proc = subprocess.Popen([str(paths.binary), "-c", str(paths.toml)],
                            stdout=server_log, stderr=subprocess.STDOUT)
    exit_code = 1
    fatal = False
    try:
        provision._wait_for_homeserver(base_url)
        gate.line(f"tuwunel up (pid {proc.pid}) at {base_url}")
        asyncio.run(scenario(gate, paths, base_url, cfg))
    except BaseException as exc:  # noqa: BLE001 — gate reports, never swallows
        fatal = True
        gate.line(f"[FATAL] {type(exc).__name__}: {exc}")
        import traceback

        gate.line(traceback.format_exc())
    finally:
        asyncio.run(_stop_open_managers())
        proc.terminate()
        try:
            proc.wait(timeout=15)
            gate.line(f"tuwunel stopped (pid {proc.pid})")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            gate.line(f"tuwunel killed (pid {proc.pid})")
        server_log.close()

    total = gate.passed + gate.failed
    verdict = (GATE_MARKER if gate.failed == 0 and total > 0 and not fatal
               else GATE_FAIL_MARKER)
    gate.line(f"== {verdict} {gate.passed}/{total} checks == log: {gate_log}")
    gate.close()
    return 0 if verdict == GATE_MARKER else exit_code

def _force_offline_config(home: Path) -> None:
    """The gate home boots ITS OWN tuwunel from the already-downloaded
    binary; skip the GitHub releases check (rate-limited from CI boxes)."""
    import yaml

    cfg_path = home / "config.yaml"
    doc: dict = {}
    if cfg_path.exists():
        try:
            doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except ValueError:
            doc = {}
    obs = doc.get("observatory")
    if not isinstance(obs, dict):
        obs = {}
    obs.setdefault("offline", True)
    obs.setdefault("e2ee", True)
    doc["observatory"] = obs
    cfg_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
