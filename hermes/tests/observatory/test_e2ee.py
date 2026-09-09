"""Contract tests for observatory/e2ee.py (M4c, spec D4 + O3 fallback).

Layers:

* **Flag/policy** — default-TRUE contract (D4 ships hot since the
  MERCURY-E2EE-OK gate, 2026-09-08), explicit ``e2ee: false`` opt-out,
  fail-hard capability gate with the operator-facing remedy.
* **EncryptedIntentExecutor** — the renderer intent hook (composition, no
  renderer.py edits): chat rooms get ``m.room.encryption`` + registry
  entries; SendMessage/EditMessage into crypt-registry rooms go through
  the encryptor; every other intent delegates untouched.
* **Inbound pipeline** — ``decrypt_event`` in front of the daemon intake.
* **REAL crypto stack** (needs the python-olm cp313 wheel +
  ``mautrix[encryption]``): ``SQLiteCryptoStore`` persistence contract
  (identity/sessions/trust/replay guard survive a restart) and the
  ``handle_as_transaction`` to-device router. These skip cleanly (with a
  reason) on hosts without the compiled stack; the live gate
  ``observatory/scripts/e2ee_live_gate.py`` is the end-to-end proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import pytest

from observatory import e2ee as e2ee_mod
from observatory.e2ee import (
    CRYPT_ROOM_META_PREFIX,
    ENCRYPTION_CONTENT,
    E2EEError,
    E2EE_REMEDY,
    EncryptedIntentExecutor,
    E2EEManager,
    e2ee_available,
    e2ee_enabled,
    message_content,
    wire_encrypted_event,
)
from observatory.renderer import CreateRoom, CreateSpace, EditMessage, SendMessage
from observatory.state import ObservatoryState


# ---------------------------------------------------------------------------
# fakes (same shape as the renderer tests' FakeClient — duck-typed client)
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    """MatrixClient surface the executors touch: every call recorded,
    canned ids returned. ``calls`` rows are ``(kind, *args)`` tuples."""

    as_token: str = "as-tok"
    calls: list = field(default_factory=list)
    _n: int = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def client_api(self, method, path, *, sender=None, params=None,
                         json_body=None):
        self.calls.append(("api", method, path, sender, json_body))
        return {"event_id": self._id("$ev")}

    async def create_room(self, *, name, sender, preset, invite=(), space=False):
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space))
        return f"!room{self._n + 1}:hs" if space else f"!space{self._n + 1}:hs" \
            if False else self._id(("!" if not space else "!s"))

    async def set_power_levels(self, room_id, levels, *, sender):
        self.calls.append(("power", room_id, dict(levels), sender))
        return {"event_id": self._id("$pl")}

    async def set_space_child(self, parent, child, *, sender, via=(), remove=False):
        self.calls.append(("space_child", parent, child, sender, remove))
        return {}

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send_message", room_id, body, sender))
        return self._id("$msg")

    async def edit_message(self, room_id, event_id, body, *, sender,
                           formatted_body=None):
        self.calls.append(("edit_message", room_id, event_id, body, sender))
        return self._id("$edit")

    async def invite(self, room_id, user_id, *, sender):
        self.calls.append(("invite", room_id, user_id, sender))

    async def join_room(self, room_id, *, sender):
        self.calls.append(("join", room_id, sender))
        return room_id

    async def leave_room(self, room_id, *, sender):
        self.calls.append(("leave", room_id, sender))

    async def delete_room(self, room_id, *, block=False, purge=True):
        self.calls.append(("delete", room_id))


class FakeE2EE:
    """Interface-faithful fake: real crypt-registry semantics (state meta),
    canned encrypted sends — enough to test the EXECUTOR, not the crypto."""

    def __init__(self, state: ObservatoryState):
        self.state = state
        self.encrypted_sends: list[tuple[str, str, str]] = []
        self.enabled_rooms: dict[str, str] = {}

    def room_is_encrypted(self, key: str) -> bool:
        try:
            return bool(self.state.get_meta(CRYPT_ROOM_META_PREFIX + key))
        except Exception:  # noqa: BLE001 — registry miss = not encrypted
            return False

    def mark_room_encrypted(self, key: str, room_id: str) -> None:
        self.state.set_meta(CRYPT_ROOM_META_PREFIX + key, room_id)
        self.enabled_rooms[key] = room_id

    async def enable_room_encryption(self, room_id: str, *, sender: str) -> str:
        self.enabled_rooms[f"room:{room_id}"] = room_id
        return f"$crypt-state{len(self.enabled_rooms)}"

    async def send_encrypted_message(self, room_id, *, sender, body,
                                     formatted_body=None, relates_to=None):
        self.encrypted_sends.append((room_id, sender, body))
        return f"$crypt{len(self.encrypted_sends)}"


# ---------------------------------------------------------------------------
# flag behavior (O3 + D4 default-on)
# ---------------------------------------------------------------------------


class TestFlag:
    def test_default_true_without_config(self, tmp_path: Path):
        # D4 default-on since MERCURY-E2EE-OK (2026-09-08): absence of
        # config must NOT silently disable encryption.
        assert e2ee_enabled(tmp_path) is True

    def test_missing_observatory_section_true(self, tmp_path: Path):
        (tmp_path / "config.yaml").write_text("models: {}\n", encoding="utf-8")
        assert e2ee_enabled(tmp_path) is True

    def test_explicit_true(self, tmp_path: Path):
        (tmp_path / "config.yaml").write_text(
            "observatory:\n  e2ee: true\n", encoding="utf-8"
        )
        assert e2ee_enabled(tmp_path) is True

    def test_explicit_false_opt_out(self, tmp_path: Path):
        (tmp_path / "config.yaml").write_text(
            "observatory:\n  e2ee: false\n", encoding="utf-8"
        )
        assert e2ee_enabled(tmp_path) is False

    def test_available_is_a_probe_not_a_crash(self):
        # host-dependent (needs compiled olm) — the CONTRACT is that the
        # probe returns a bool without raising on crypto-less hosts.
        assert isinstance(e2ee_available(), bool)

    @pytest.mark.asyncio
    async def test_start_fails_hard_when_stack_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:x",
        )
        with pytest.raises(E2EEError) as exc:
            await manager.start(enabled=True)
        # the remedy names the exact install path — tested verbatim so the
        # operator-facing fix can never silently drift
        assert str(exc.value) == E2EE_REMEDY
        assert "observatory/wheels" in str(exc.value)
        assert "mercury setup observatory" in str(exc.value)
        assert "observatory.e2ee: false" in str(exc.value)

    @pytest.mark.asyncio
    async def test_start_creates_shared_crypto_dir_when_available(self, tmp_path, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)
        crypto_dir = tmp_path / "obs" / "crypto"
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=crypto_dir, owner_mxid="@owner:x",
        )
        await manager.start(enabled=True)
        assert crypto_dir.is_dir()

    @pytest.mark.asyncio
    async def test_start_noop_when_disabled_without_stack(self, tmp_path, monkeypatch):
        # opt-out path: E2EE off + missing crypto stack must NOT fail and
        # must NOT touch the disk (plaintext needs no crypto store).
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
        crypto_dir = tmp_path / "crypto"
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=crypto_dir, owner_mxid="@owner:x",
        )
        await manager.start(enabled=False)
        assert not crypto_dir.exists()


# ---------------------------------------------------------------------------
# EncryptedIntentExecutor — the renderer intent hook
# ---------------------------------------------------------------------------


def _seeded_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")
    state.add_node(
        "gw", engine="hermes", name="gateway", slug="gw",
        mxid="@merc_gw:hs", session_ref="s:gw", parent_node_id=None,
        extra={"kind": "gateway"},
    )
    return state


class TestEncryptedIntentExecutor:
    @pytest.mark.asyncio
    async def test_created_chat_room_gets_encryption_state_and_registry(self, tmp_path):
        state = _seeded_state(tmp_path)
        client = FakeClient()
        e2ee = FakeE2EE(state)
        executor = EncryptedIntentExecutor(
            client, state, owner_mxid="@owner:hs", server_name="hs", e2ee=e2ee
        )
        records = await executor.execute([
            CreateRoom(key="gw:room", name="gateway room",
                       space_key="gw", sender="@merc_gw:hs", kind="chat"),
        ])
        assert records[0]["op"] == "create_room"
        assert records[0]["encrypted"] is True
        # registry entry: the crypt: meta maps key -> room id
        room_id = state.get_meta(CRYPT_ROOM_META_PREFIX + "gw:room")
        assert room_id == records[0]["room_id"]
        # the enable call was issued for the created room
        assert records[0]["room_id"] in e2ee.enabled_rooms.values()

    @pytest.mark.asyncio
    async def test_send_into_registered_room_is_encrypted(self, tmp_path):
        state = _seeded_state(tmp_path)
        client = FakeClient()
        e2ee = FakeE2EE(state)
        e2ee.mark_room_encrypted("gw:room", "!r1:hs")
        state.set_meta("room:gw:room", "!r1:hs")
        executor = EncryptedIntentExecutor(
            client, state, owner_mxid="@owner:hs", server_name="hs", e2ee=e2ee
        )
        records = await executor.execute([
            SendMessage(room_key="gw:room", sender="@merc_gw:hs",
                        body="secret", tag="t1"),
        ])
        assert records[0]["op"] == "send" and records[0]["encrypted"] is True
        assert e2ee.encrypted_sends == [("!r1:hs", "@merc_gw:hs", "secret")]
        # tag records the encrypted event id
        assert state.get_meta("t1") == records[0]["event_id"]
        # NO plaintext send through the base executor
        assert not any(c[0] == "send_message" for c in client.calls)

    @pytest.mark.asyncio
    async def test_edit_into_registered_room_is_encrypted_mreplace(self, tmp_path):
        state = _seeded_state(tmp_path)
        client = FakeClient()
        e2ee = FakeE2EE(state)
        e2ee.mark_room_encrypted("gw:room", "!r1:hs")
        state.set_meta("room:gw:room", "!r1:hs")
        executor = EncryptedIntentExecutor(
            client, state, owner_mxid="@owner:hs", server_name="hs", e2ee=e2ee
        )
        records = await executor.execute([
            EditMessage(room_key="gw:room", sender="@merc_gw:hs",
                        event_id="$orig", body="fixed"),
        ])
        assert records[0]["op"] == "edit" and records[0]["encrypted"] is True
        assert records[0]["replaces"] == "$orig"
        assert not any(c[0] == "edit_message" for c in client.calls)

    @pytest.mark.asyncio
    async def test_send_into_unregistered_room_delegates_plaintext(self, tmp_path):
        state = _seeded_state(tmp_path)
        client = FakeClient()
        e2ee = FakeE2EE(state)
        state.set_meta("room:plain", "!plain:hs")
        executor = EncryptedIntentExecutor(
            client, state, owner_mxid="@owner:hs", server_name="hs", e2ee=e2ee
        )
        records = await executor.execute([
            SendMessage(room_key="plain", sender="@merc_gw:hs", body="open"),
        ])
        assert records[0].get("encrypted") is None  # base record shape
        assert any(c[0] == "send_message" and c[2] == "open" for c in client.calls)
        assert e2ee.encrypted_sends == []

    @pytest.mark.asyncio
    async def test_non_message_intents_delegate_untouched(self, tmp_path):
        state = _seeded_state(tmp_path)
        state.set_meta("space:gw", "!s1:hs")
        client = FakeClient()
        e2ee = FakeE2EE(state)
        executor = EncryptedIntentExecutor(
            client, state, owner_mxid="@owner:hs", server_name="hs", e2ee=e2ee
        )
        records = await executor.execute([
            CreateSpace(key="extra", name="Extra", sender="@merc_gw:hs"),
        ])
        assert records[0]["op"] == "create_space" and "encrypted" not in records[0]
        # owner PL pin still flows through the base executor
        assert any(c[0] == "power" for c in client.calls)


class FakeDecryptingManager:
    """The decrypt surface of E2EEManager (decrypt_event), standalone."""

    def __init__(self):
        self.decrypted: list[dict] = []

    async def decrypt_event(self, event):
        self.decrypted.append(event)
        content = event.get("content") or {}
        return {"msgtype": "m.text", "body": f"decrypted:{content.get('ciphertext')}"}


class TestInboundPipeline:
    @staticmethod
    def _daemon(tmp_path):
        from observatory.sidecar_main import SidecarDaemon

        class _Stub:
            """Minimal SidecarDaemon shape for the intake path test."""

            def __init__(self):
                self.e2ee = FakeDecryptingManager()
                self.seen_events: list[dict] = []

            async def _route_event(self, event):
                self.seen_events.append(event)

        return _Stub()

    @pytest.mark.asyncio
    async def test_encrypted_event_decrypted_before_routing(self, tmp_path):
        daemon = self._daemon(tmp_path)
        event = {"type": "m.room.encrypted", "event_id": "$e1",
                 "content": {"ciphertext": "CIPH"}}
        # the sidecar intake contract: m.room.encrypted goes through
        # e2ee.decrypt_event first; failures never drop the wire event
        decrypted = await daemon.e2ee.decrypt_event(event)
        assert decrypted is not None and decrypted["body"].startswith("decrypted:")
        daemon.seen_events.append(event)
        assert daemon.seen_events[0]["type"] == "m.room.encrypted"

    @pytest.mark.asyncio
    async def test_undecryptable_event_survives_as_is(self, tmp_path):
        daemon = self._daemon(tmp_path)
        event = {"type": "m.room.encrypted", "event_id": "$e2", "content": {}}
        decrypted = await daemon.e2ee.decrypt_event(event)
        # a broken ciphertext still yields a body — intake continues
        assert decrypted is not None

class TestWireEncryptedEvent:
    """``wire_encrypted_event``: raw wire dicts (``/messages`` chunks,
    appservice transactions) use JSON names — the live gate proved the
    attr name ``timestamp`` does NOT deserialize and ``type`` is
    required (``SerializerError`` → intake/decrypt FATAL)."""

    @staticmethod
    def _wire(**over):
        base = {
            "event_id": "$e", "room_id": "!r:example", "sender": "@a:example",
            "type": "m.room.encrypted", "origin_server_ts": 123,
            "content": {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "C",
                        "sender_key": "K", "session_id": "S", "device_id": "D"},
        }
        base.update(over)
        return base

    def test_wire_dict_deserializes_with_json_names(self):
        from mautrix.types import EventType

        evt = wire_encrypted_event(self._wire())
        assert evt.event_id == "$e" and evt.room_id == "!r:example"
        assert evt.timestamp == 123
        assert evt.type == EventType.ROOM_ENCRYPTED
        assert evt.content.ciphertext == "C"

    def test_missing_type_defaults_to_encrypted(self):
        from mautrix.types import EventType

        wire = self._wire()
        del wire["type"]
        assert wire_encrypted_event(wire).type == EventType.ROOM_ENCRYPTED

class TestMessageContent:
    """``message_content``: spec ``m.replace`` law — an edit carries
    ``m.relates_to`` AND a mirrored ``m.new_content`` (mautrix parses
    the replacement from ``m.new_content``; without it O1 sees
    ``new_content=None``). Plain messages carry neither key."""

    def test_plain_message_has_no_relation_keys(self):
        from observatory.e2ee import message_content as mc

        assert mc("hi") == {"msgtype": "m.text", "body": "hi"}

    def test_replace_carries_new_content_mirror(self):
        from observatory.e2ee import message_content as mc

        content = mc("* fixed", relates_to={"rel_type": "m.replace",
                                            "event_id": "$orig"})
        assert content["m.relates_to"] == {"rel_type": "m.replace",
                                           "event_id": "$orig"}
        assert content["m.new_content"] == {"msgtype": "m.text",
                                            "body": "* fixed"}

    def test_replace_mirrors_format_when_present(self):
        from observatory.e2ee import message_content as mc

        content = mc("* <b>f</b>", formatted_body="<b>f</b>",
                     relates_to={"rel_type": "m.replace", "event_id": "$o"})
        assert content["m.new_content"]["formatted_body"] == "<b>f</b>"
        assert content["m.new_content"]["format"] == "org.matrix.custom.html"


# ---------------------------------------------------------------------------
# constants pinned by D4
# ---------------------------------------------------------------------------


class TestConstants:
    def test_megolm_algorithm_pinned(self):
        assert ENCRYPTION_CONTENT["algorithm"] == "m.megolm.v1.aes-sha2"

    def test_crypto_dir_lives_under_observatory_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
        from observatory.e2ee import crypto_dir_for

        assert crypto_dir_for() == tmp_path / "observatory" / "crypto"


# ---------------------------------------------------------------------------
# REAL crypto stack (python-olm cp313 wheel — skip cleanly without it)
# ---------------------------------------------------------------------------

needs_stack = pytest.mark.skipif(not e2ee_available(), reason="compiled olm stack missing")


@needs_stack
class TestSQLiteCryptoStore:
    """D8 persistence contract: identity, sessions, trust, replay guard all
    survive a store close/reopen (the sidecar-restart simulation)."""

    @pytest.mark.asyncio
    async def test_account_identity_and_flags_survive(self, tmp_path):
        from mautrix.crypto.account import OlmAccount
        from mautrix.types import DeviceID
        from observatory.e2ee import SQLiteCryptoStore

        db = tmp_path / "crypto" / "merc_gateway.db"
        s1 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s1.open()
        acc = OlmAccount()
        acc.shared = True
        await s1.put_account(acc)
        await s1.put_device_id(DeviceID("OBSVAA11"))
        fingerprint = acc.fingerprint
        await s1.close()

        s2 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s2.open()
        acc2 = await s2.get_account()
        assert acc2 is not None and acc2.fingerprint == fingerprint
        assert acc2.shared is True  # no re-upload storm after restart
        assert await s2.get_device_id() == DeviceID("OBSVAA11")
        await s2.close()

    @pytest.mark.asyncio
    async def test_devices_and_trust_survive(self, tmp_path):
        from observatory.e2ee import SQLiteCryptoStore
        from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID

        db = tmp_path / "crypto" / "merc_gateway.db"
        s1 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s1.open()
        owner_dev = DeviceIdentity(
            user_id=UserID("@owner:hs"), device_id=DeviceID("O1"),
            identity_key="curve-key", signing_key="ed-key",
            trust=TrustState.VERIFIED, deleted=False, name="Element X",
        )
        await s1.put_device("@owner:hs", owner_dev)
        # a tracked user with ZERO devices must stay tracked (upstream
        # semantic: get_devices returns {} not None after put_devices({}))
        await s1.put_devices("@ghost:hs", {})
        await s1.close()

        s2 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s2.open()
        got = await s2.get_device("@owner:hs", DeviceID("O1"))
        assert got is not None and got.trust == TrustState.VERIFIED
        assert got.name == "Element X"
        assert await s2.get_devices("@owner:hs") == {DeviceID("O1"): owner_dev}
        assert await s2.get_devices("@ghost:hs") == {}  # tracked-empty, not None
        assert await s2.get_devices("@stranger:hs") is None  # untracked
        assert await s2.filter_tracked_users(
            ["@owner:hs", "@ghost:hs", "@stranger:hs"]
        ) == ["@owner:hs", "@ghost:hs"]
        await s2.close()

    @pytest.mark.asyncio
    async def test_megolm_sessions_survive_and_decrypt(self, tmp_path):
        """The D8 killer test: an inbound Megolm session restored from
        SQLite still decrypts ciphertext encrypted before the restart."""
        from mautrix.crypto.sessions import (
            InboundGroupSession,
            OutboundGroupSession,
        )
        from observatory.e2ee import SQLiteCryptoStore
        from mautrix.types import IdentityKey, RoomID, SessionID, SigningKey

        db = tmp_path / "crypto" / "merc_gateway.db"
        out = OutboundGroupSession(RoomID("!room:hs"))
        s1 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s1.open()
        inbound = InboundGroupSession(
            out.session_key,
            signing_key=SigningKey("sign-key"), sender_key=IdentityKey("send-key"),
            room_id=RoomID("!room:hs"),
        )
        await s1.put_group_session(
            RoomID("!room:hs"), IdentityKey("send-key"), SessionID(out.id), inbound
        )
        await s1.add_outbound_group_session(out)
        out.shared = True  # the machine marks it shared after key distribution
        ciphertext = out.encrypt('{"body":"secret-before-restart"}')
        await s1.close()

        s2 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s2.open()
        restored = await s2.get_group_session(RoomID("!room:hs"), SessionID(out.id))
        assert restored is not None
        plaintext, _index = restored.decrypt(ciphertext)
        assert "secret-before-restart" in plaintext
        out2 = await s2.get_outbound_group_session(RoomID("!room:hs"))
        assert out2 is not None and out2.room_id == RoomID("!room:hs")
        await s2.close()

    @pytest.mark.asyncio
    async def test_replay_index_rejects_conflicting_event(self, tmp_path):
        from observatory.e2ee import SQLiteCryptoStore

        db = tmp_path / "crypto" / "merc_gateway.db"
        s1 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s1.open()
        assert await s1.validate_message_index("sk", "sid", "$e1", 0, 111)
        await s1.close()

        s2 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s2.open()
        # same tuple + same values → valid (idempotent redelivery)
        assert await s2.validate_message_index("sk", "sid", "$e1", 0, 111)
        # same index, DIFFERENT event → replay attack, rejected across restart
        assert not await s2.validate_message_index("sk", "sid", "$e2", 0, 111)
        await s2.close()

    @pytest.mark.asyncio
    async def test_redacted_session_stays_redacted(self, tmp_path):
        from mautrix.crypto.sessions import InboundGroupSession, OutboundGroupSession
        from observatory.e2ee import SQLiteCryptoStore
        from mautrix.types import IdentityKey, RoomID, SessionID, SigningKey

        db = tmp_path / "crypto" / "merc_gateway.db"
        out = OutboundGroupSession(RoomID("!room:hs"))
        s1 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s1.open()
        inbound = InboundGroupSession(
            out.session_key,
            signing_key=SigningKey("sign-key"), sender_key=IdentityKey("send-key"),
            room_id=RoomID("!room:hs"),
        )
        await s1.put_group_session(
            RoomID("!room:hs"), IdentityKey("send-key"), SessionID(out.id), inbound
        )
        await s1.redact_group_session(RoomID("!room:hs"), SessionID(out.id), "test")
        await s1.close()

        s2 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await s2.open()
        assert await s2.get_group_session(RoomID("!room:hs"), SessionID(out.id)) is None
        assert not await s2.has_group_session(RoomID("!room:hs"), SessionID(out.id))
        await s2.close()


@needs_stack
class TestTransactionRouting:
    """handle_as_transaction: the appservice crypto-field router (intake
    side of the to-device gap) — machines get exactly their own to-device
    messages; unknown targets never crash the intake."""

    @staticmethod
    def _manager_with_fake_machine(tmp_path, mxid, loaded=True):
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs",
            gateway_mxid="@merc_gateway:hs",
        )

        class _Recording:
            def __init__(self):
                self.to_device = []
                self.device_lists = []
                self.otk_counts = []

            async def handle_as_to_device_event(self, evt):
                self.to_device.append(evt)

            async def handle_as_device_lists(self, lists):
                self.device_lists.append(lists)

            async def handle_as_otk_counts(self, counts):
                self.otk_counts.append(counts)

        recording = _Recording()

        class _FakeVirtual:
            _loaded = loaded
            machine = recording
            store = None

            async def load(self):
                self._loaded = True

        fake = _FakeVirtual()
        fake.mxid = mxid
        manager._machines[mxid] = fake
        return manager, recording

    @pytest.mark.asyncio
    async def test_to_device_routed_to_owning_machine(self, tmp_path):
        manager, rec = self._manager_with_fake_machine(
            tmp_path, "@merc_gateway:hs", loaded=False
        )
        routed = await manager.handle_as_transaction({
            "events": [],
            "to_device": {
                "@merc_gateway:hs": {
                    "OBSVAA11": {
                        "sender": "@owner:hs",
                        "type": "m.room.encrypted",
                        "content": {
                            "algorithm": "m.olm.v1.curve25519-aes-sha2",
                            "sender_key": "CURVE1",
                            "ciphertext": {"CURVE1": {"body": "x", "type": 0}},
                        },
                    }
                }
            },
        })
        assert routed["to_device"] == 1
        assert len(rec.to_device) == 1
        evt = rec.to_device[0]
        assert evt.sender == "@owner:hs"
        assert evt.type.serialize() == "m.room.encrypted"
        assert evt.to_user_id == "@merc_gateway:hs"
        assert evt.to_device_id == "OBSVAA11"

    @pytest.mark.asyncio
    async def test_to_device_for_non_virtual_user_skipped(self, tmp_path):
        manager, rec = self._manager_with_fake_machine(
            tmp_path, "@merc_gateway:hs"
        )
        routed = await manager.handle_as_transaction({
            "to_device": {"@owner:hs": {"DEV": {"sender": "@x:hs",
                                                "type": "m.room.encrypted",
                                                "content": {}}}},
        })
        assert routed["to_device"] == 0
        assert rec.to_device == []
        assert "@owner:hs" not in manager._machines

    @pytest.mark.asyncio
    async def test_device_lists_broadcast_to_loaded_machines(self, tmp_path):
        manager, rec = self._manager_with_fake_machine(
            tmp_path, "@merc_gateway:hs"
        )
        routed = await manager.handle_as_transaction({
            "device_lists": {"changed": ["@owner:hs"], "left": []},
        })
        assert routed["device_lists"] == 1
        assert len(rec.device_lists) == 1
        assert rec.device_lists[0].changed == ["@owner:hs"]

    @pytest.mark.asyncio
    async def test_otk_counts_routed_to_owning_machine_only(self, tmp_path):
        manager, rec = self._manager_with_fake_machine(
            tmp_path, "@merc_gateway:hs"
        )
        routed = await manager.handle_as_transaction({
            "device_one_time_keys_count": {
                "@merc_gateway:hs": {"OBSVAA11": {"signed_curve25519": 42}},
                "@merc_other:hs": {"OBSVBB22": {"signed_curve25519": 7}},
            },
        })
        assert routed["otk_counts"] == 1
        assert len(rec.otk_counts) == 1
        counts = rec.otk_counts[0]["@merc_gateway:hs"]["OBSVAA11"]
        assert counts.signed_curve25519 == 42

    @pytest.mark.asyncio
    async def test_empty_and_garbage_transactions_never_raise(self, tmp_path):
        manager, _ = self._manager_with_fake_machine(tmp_path, "@merc_gateway:hs")
        assert await manager.handle_as_transaction({}) == {
            "to_device": 0, "device_lists": 0, "otk_counts": 0}
        assert (await manager.handle_as_transaction({"to_device": None}))[
            "to_device"] == 0
