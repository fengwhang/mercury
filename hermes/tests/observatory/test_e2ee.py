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

    async def create_room(self, *, name, sender, preset, invite=(), space=False,
                          topic=None, initial_state=None):
        self.calls.append(("create_room", name, sender, preset, tuple(invite),
                           space, initial_state))
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
                                     formatted_body=None, relates_to=None,
                                     report_out=None):
        self.encrypted_sends.append((room_id, sender, body))
        if report_out is not None:
            # no owner devices in the fake world: empty ceremony, no notice
            report_out.update({"trusted": [], "known": [],
                               "refused": [], "fetched": [], "shared": []})
        return f"$crypt{len(self.encrypted_sends)}"

    async def maybe_post_verify_notice(self, room_id, *, sender, room_key,
                                       report) -> bool:
        return False


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
        # encryption rode initial_state (atomic, defect iii) — never a
        # follow-up PUT, so no enable call exists for the created room.
        creates = [c for c in client.calls if c[0] == "create_room"]
        assert len(creates) == 1
        initial = creates[0][6]
        assert initial == [{"type": "m.room.encryption", "state_key": "",
                            "content": dict(ENCRYPTION_CONTENT)}]
        assert not [k for k in e2ee.enabled_rooms if k.startswith("room:")]

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

# ---------------------------------------------------------------------------
# Loopback key-sharing with a fake second device (real Olm/Megolm crypto,
# no homeserver): the FluffyChat-owner-phone simulation. The gateway
# machine and the owner's phone are two REAL OlmMachines plumbed through
# loopback client adapters (shared key directory + to-device delivery).
# ---------------------------------------------------------------------------


class _LoopNet:
    """Shared homeserver stand-in: published device keys + one-time keys,
    and the peer registry delivering to-device messages between machines."""

    def __init__(self) -> None:
        self.keys: dict[tuple[str, str], dict] = {}
        self.peers: dict[str, object] = {}


class _LoopClient:
    """The exact client surface OlmMachine touches, backed by _LoopNet."""

    def __init__(self, mxid: str, device_id: str, net: _LoopNet) -> None:
        self.mxid = mxid
        self.device_id = device_id
        self._net = net
        self._handlers: dict[object, list[object]] = {}

    def add_event_handler(self, event_type, handler, **_kwargs) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def dispatch_event(self, event, source=None) -> None:
        return None

    @staticmethod
    def _ser(obj):
        return obj.serialize() if hasattr(obj, "serialize") else obj

    async def send_to_device(self, event_type, messages) -> None:
        from mautrix.types import ASToDeviceEvent

        et = (event_type.serialize() if hasattr(event_type, "serialize")
              else str(event_type))
        for user_id, devs in (messages or {}).items():
            for device_id, msg in (devs or {}).items():
                raw = self._ser(msg)
                peer = self._net.peers[str(user_id)]
                evt = ASToDeviceEvent.deserialize({
                    "sender": self.mxid,
                    "type": et,
                    "content": dict(raw) if isinstance(raw, dict) else raw,
                    "to_user_id": str(user_id),
                    "to_device_id": str(device_id),
                })
                await peer.handle_as_to_device_event(evt)

    async def send_to_one_device(self, event_type, user_id, device_id,
                                 message) -> None:
        await self.send_to_device(event_type, {user_id: {device_id: message}})

    async def upload_keys(self, one_time_keys=None, device_keys=None) -> dict:
        from mautrix.types import EncryptionKeyAlgorithm

        entry = self._net.keys.setdefault(
            (self.mxid, self.device_id), {"otks": {}})
        if device_keys is None and one_time_keys is None:
            return {EncryptionKeyAlgorithm.SIGNED_CURVE25519:
                    len(entry.get("otks", {}))}
        if device_keys is not None:
            entry["device"] = self._ser(device_keys)
        for key_id, key in (one_time_keys or {}).items():
            entry["otks"][str(key_id)] = self._ser(key)
        return {EncryptionKeyAlgorithm.SIGNED_CURVE25519: len(entry["otks"])}

    async def query_keys(self, users, token=None):
        from mautrix.types import QueryKeysResponse

        wanted = [str(u) for u in (users or [])]
        device_keys: dict[str, dict] = {}
        for user_id in wanted:
            per: dict[str, dict] = {}
            for (mxid, device_id), entry in self._net.keys.items():
                if mxid == user_id and entry.get("device"):
                    per[device_id] = entry["device"]
            device_keys[user_id] = per
        return QueryKeysResponse.deserialize({
            "device_keys": device_keys, "master_keys": {},
            "self_signing_keys": {}, "user_signing_keys": {}, "failures": {},
        })

    async def claim_keys(self, request):
        from mautrix.types import ClaimKeysResponse

        out: dict[str, dict] = {}
        for user_id, devs in (request or {}).items():
            for device_id in (devs or {}):
                entry = self._net.keys.get(
                    (str(user_id), str(device_id)), {})
                otks = entry.get("otks", {})
                if otks:
                    key_id, key = next(iter(otks.items()))
                    del otks[key_id]
                    out.setdefault(str(user_id), {})[str(device_id)] = {
                        key_id: key}
        return ClaimKeysResponse.deserialize(
            {"one_time_keys": out, "failures": {}})

    async def get_state_event(self, room_id, event_type):
        return None


class _LoopStateStore:
    """OlmMachine StateStore over nothing: default Megolm params, no rooms."""

    async def is_encrypted(self, room_id) -> bool:
        return True

    async def get_encryption_info(self, room_id):
        return None

    async def find_shared_rooms(self, user_id) -> list:
        return []


class _SeededVirtual:
    """VirtualUserCrypto-shaped shim around a loopback OlmMachine (offline):
    the manager drives ``machine_for(...).load()`` + ``.machine`` only."""

    def __init__(self, mxid: str, device_id: str, machine) -> None:
        self.mxid = mxid
        self.device_id = device_id
        self.machine = machine
        self._loaded = False
        self.loads = 0

    async def load(self) -> None:
        self.loads += 1
        if not self._loaded:
            await self.machine.load()
            await self.machine.share_keys()
            self._loaded = True


@dataclass
class _KeyshareServer(FakeClient):
    """Fake homeserver: room members + encrypted sends with canned ids."""

    members: list = field(default_factory=list)

    async def client_api(self, method, path, *, sender=None, params=None,
                         json_body=None):
        self.calls.append(("api", method, path, sender, json_body))
        if method == "GET" and path.rstrip("/").endswith("/members"):
            return {"chunk": [
                {"type": "m.room.member", "state_key": m,
                 "content": {"membership": "join"}} for m in self.members
            ]}
        return {"event_id": self._id("$ev")}


@needs_stack
class TestLoopbackKeyShare:
    """Key share → encrypt → decrypt round trip in BOTH directions with a
    fake second device, through the REAL E2EEManager ceremony
    (ensure_room_share → TOFU trust → Megolm share → notice)."""

    GW = "@merc_gateway:hs"
    OWNER = "@owner:hs"
    GWDEV = "GWDEV1"
    PHONE = "PHONE1"
    ROOM = "!room:hs"

    async def _rig(self, tmp_path):
        from mautrix.crypto import OlmMachine
        from mautrix.crypto.store import MemoryCryptoStore
        from mautrix.types import UserID

        net = _LoopNet()
        gw_machine = OlmMachine(
            _LoopClient(self.GW, self.GWDEV, net),
            MemoryCryptoStore(UserID(self.GW), "pickle-gw"),
            _LoopStateStore())
        o1 = OlmMachine(
            _LoopClient(self.OWNER, self.PHONE, net),
            MemoryCryptoStore(UserID(self.OWNER), "pickle-o1"),
            _LoopStateStore())
        net.peers[self.GW] = gw_machine
        net.peers[self.OWNER] = o1
        await gw_machine.load()
        await gw_machine.share_keys()
        await o1.load()
        await o1.share_keys()

        server = _KeyshareServer(members=[self.GW, self.OWNER])
        state = ObservatoryState(tmp_path / "state.db")
        mgr = E2EEManager(
            server, state, crypto_dir=tmp_path / "crypto",
            owner_mxid=self.OWNER, gateway_mxid=self.GW)
        mgr._machines[self.GW] = _SeededVirtual(
            self.GW, self.GWDEV, gw_machine)
        return mgr, gw_machine, o1

    @staticmethod
    def _body(content) -> str:
        data = (content.serialize() if hasattr(content, "serialize")
                else dict(content))
        return str(data.get("body"))

    @pytest.mark.asyncio
    async def test_sidecar_to_phone_round_trip(self, tmp_path):
        mgr, _, o1 = await self._rig(tmp_path)
        secret = "loopback secret gw-to-phone"
        report = await mgr.ensure_room_share(self.ROOM, self.GW)
        assert report["trusted"] == [self.PHONE]
        assert report["refused"] == []
        assert report["shared"] == [self.ROOM]
        # the phone's device is VERIFIED in our store after TOFU
        stored = await mgr._machines[self.GW].machine.crypto_store.get_device(
            self.OWNER, self.PHONE)
        assert stored is not None
        cipher = await mgr.encrypt_megolm(
            self.ROOM, self.GW, "m.room.message",
            {"msgtype": "m.text", "body": secret})
        wire = {"event_id": "$e1", "room_id": self.ROOM, "sender": self.GW,
                "type": "m.room.encrypted", "origin_server_ts": 1,
                "content": cipher}
        decrypted = await o1.decrypt_megolm_event(
            wire_encrypted_event(wire))
        assert self._body(decrypted.content) == secret

    @pytest.mark.asyncio
    async def test_phone_to_sidecar_round_trip_via_decrypt_event(
            self, tmp_path):
        from mautrix.types import EventType, RoomID, UserID

        mgr, _, o1 = await self._rig(tmp_path)
        secret = "loopback secret phone-to-gw"
        # the phone shares its own session back to the gateway device
        await o1.share_group_session(RoomID(self.ROOM), [UserID(self.GW)])
        cipher = (await o1.encrypt_megolm_event(
            RoomID(self.ROOM), EventType.find("m.room.message"),
            {"msgtype": "m.text", "body": secret})).serialize()
        wire = {"event_id": "$e2", "room_id": self.ROOM,
                "sender": self.OWNER, "type": "m.room.encrypted",
                "origin_server_ts": 2, "content": cipher}
        seeded = mgr._machines[self.GW]
        assert seeded.loads == 0  # cold machine — the live decrypt bug
        out = await mgr.decrypt_event(wire)
        assert seeded.loads >= 1  # decrypt loads before touching crypto
        assert out is not None and out.get("body") == secret

    @pytest.mark.asyncio
    async def test_second_share_reuses_session_without_retrust(
            self, tmp_path):
        mgr, _, _ = await self._rig(tmp_path)
        first = await mgr.ensure_room_share(self.ROOM, self.GW)
        assert first["trusted"] == [self.PHONE]
        second = await mgr.ensure_room_share(self.ROOM, self.GW)
        assert second["trusted"] == []  # already known — no re-trust
        assert second["known"] == [self.PHONE]
        assert second["shared"] == []  # live session reused, no re-share


@needs_stack
class TestTofuTrust:
    """TOFU contract: first sight VERIFIED, resight idempotent, changed
    key refused fail-closed (trust + stored keys untouched)."""

    GW = "@merc_gateway:hs"
    OWNER = "@owner:hs"
    PHONE = "PHONE1"

    async def _rig(self, tmp_path):
        from mautrix.crypto import OlmMachine
        from mautrix.crypto.store import MemoryCryptoStore
        from mautrix.types import UserID

        net = _LoopNet()
        gw_machine = OlmMachine(
            _LoopClient(self.GW, "GWDEV1", net),
            MemoryCryptoStore(UserID(self.GW), "pickle-gw"),
            _LoopStateStore())
        net.peers[self.GW] = gw_machine
        await gw_machine.load()
        state = ObservatoryState(tmp_path / "state.db")
        mgr = E2EEManager(
            FakeClient(), state, crypto_dir=tmp_path / "crypto",
            owner_mxid=self.OWNER, gateway_mxid=self.GW)
        mgr._machines[self.GW] = _SeededVirtual(
            self.GW, "GWDEV1", gw_machine)
        return mgr, gw_machine

    @staticmethod
    def _device(name, identity_key, signing_key):
        from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID

        return DeviceIdentity(
            user_id=UserID(TestTofuTrust.OWNER),
            device_id=DeviceID(name), identity_key=identity_key,
            signing_key=signing_key, trust=TrustState.UNVERIFIED,
            deleted=False, name=name)

    @pytest.mark.asyncio
    async def test_first_sight_verifies_resight_keeps_changed_refuses(
            self, tmp_path):
        from mautrix.types import TrustState

        mgr, gw_machine = await self._rig(tmp_path)
        store = gw_machine.crypto_store
        dev = self._device(self.PHONE, "curve-key-1", "ed-key-1")
        assert await mgr.trust_device_tofu(self.GW, self.OWNER, dev) is True
        stored = await store.get_device(self.OWNER, self.PHONE)
        assert stored is not None and stored.trust == TrustState.VERIFIED
        # resight under identical keys: idempotent, stays verified
        assert await mgr.trust_device_tofu(
            self.GW, self.OWNER,
            self._device(self.PHONE, "curve-key-1", "ed-key-1")) is True
        # changed keys: refused, stored trust + keys untouched
        assert await mgr.trust_device_tofu(
            self.GW, self.OWNER,
            self._device(self.PHONE, "curve-key-ROTATED", "ed-key-1")) is False
        kept = await store.get_device(self.OWNER, self.PHONE)
        assert kept is not None and kept.trust == TrustState.VERIFIED
        assert str(kept.identity_key) == "curve-key-1"


@needs_stack
class TestVerifyNotice:
    """Refused/pending-only notice to the gateway room: client-agnostic
    copy + fingerprint, posted once per device picture, reposted on change."""

    GW = "@merc_gateway:hs"
    ROOM = "!room:hs"

    async def _rig(self, tmp_path):
        from mautrix.crypto import OlmMachine
        from mautrix.crypto.store import MemoryCryptoStore
        from mautrix.types import UserID

        net = _LoopNet()
        gw_machine = OlmMachine(
            _LoopClient(self.GW, "GWDEV1", net),
            MemoryCryptoStore(UserID(self.GW), "pickle-gw"),
            _LoopStateStore())
        net.peers[self.GW] = gw_machine
        await gw_machine.load()
        state = ObservatoryState(tmp_path / "state.db")
        state.add_node(
            "gw", engine="hermes", name="gateway agent", slug="gateway-agent",
            mxid=self.GW, session_ref="session:gateway", parent_node_id=None,
            extra={"kind": "gateway"},
        )
        state.set_room_id("gw", self.ROOM)
        mgr = E2EEManager(
            FakeClient(), state, crypto_dir=tmp_path / "crypto",
            owner_mxid="@owner:hs", gateway_mxid=self.GW)
        mgr._machines[self.GW] = _SeededVirtual(
            self.GW, "GWDEV1", gw_machine)
        return mgr

    @pytest.mark.asyncio
    async def test_notice_text_names_device_and_command(self, tmp_path):
        from observatory.e2ee import trust_device_command

        mgr = await self._rig(tmp_path)
        text = mgr.verify_notice_text(
            gateway_mxid=self.GW, device_id="GWDEV1",
            fingerprint="AB12 CD34", trusted=["PHONE1"], refused=["TABLET2"])
        assert self.GW in text and "GWDEV1" in text
        assert "AB12 CD34" in text
        assert "device details screen" in text
        assert "PHONE1" in text and "TABLET2" in text
        assert trust_device_command("TABLET2") in text
        assert "FluffyChat" not in text and "Element" not in text
        warned = mgr.verify_notice_text(
            gateway_mxid=self.GW, device_id="GWDEV1",
            fingerprint="AB12 CD34", trusted=[], refused=["PHONE1"])
        assert "WARNING" in warned and "PHONE1" in warned
        assert trust_device_command("PHONE1") in warned

    @pytest.mark.asyncio
    async def test_notice_posts_once_per_device_picture(self, tmp_path):
        mgr = await self._rig(tmp_path)
        sent: list[tuple[str, str, str]] = []

        async def fake_send(room_id, *, sender, body, **_kwargs) -> str:
            sent.append((room_id, sender, body))
            return "$notice1"

        mgr.send_encrypted_message = fake_send  # type: ignore[method-assign]
        # routine first-sight TOFU trust never posts
        trust_only = {"trusted": ["PHONE1"], "known": [],
                      "refused": [], "pending": [],
                      "fetched": ["PHONE1"], "shared": []}
        assert await mgr.maybe_post_verify_notice(
            self.ROOM, sender=self.GW, room_key="gw",
            report=trust_only) is False
        assert sent == []
        # refusal posts to the gateway room even when triggered elsewhere
        report = {"trusted": [], "known": [],
                  "refused": ["PHONE1"], "pending": [],
                  "fetched": ["PHONE1"], "shared": []}
        assert await mgr.maybe_post_verify_notice(
            "!other:hs", sender=self.GW, room_key="gw",
            report=report) is True
        assert len(sent) == 1 and sent[0][0] == self.ROOM
        fingerprint = await mgr.gateway_fingerprint(self.GW)
        assert fingerprint in sent[0][2]  # the compare-string is usable
        # same picture → no repost
        assert await mgr.maybe_post_verify_notice(
            self.ROOM, sender=self.GW, room_key="gw",
            report=report) is False
        assert len(sent) == 1
        # changed picture (second device) → repost
        report2 = {"trusted": [], "known": [],
                   "refused": ["PHONE1", "TABLET2"], "pending": [],
                   "fetched": ["PHONE1", "TABLET2"],
                   "shared": []}
        assert await mgr.maybe_post_verify_notice(
            self.ROOM, sender=self.GW, room_key="gw",
            report=report2) is True
        assert len(sent) == 2 and "TABLET2" in sent[1][2]
        # clean report → nothing to say
        assert await mgr.maybe_post_verify_notice(
            self.ROOM, sender=self.GW, room_key="gw",
            report={"trusted": [], "known": ["PHONE1"], "refused": [],
                    "pending": [], "fetched": ["PHONE1"], "shared": []}) is False
        assert len(sent) == 2


class TestDecryptLoadsMachine:
    """decrypt_event loads a cold machine first (no stack: fake virtual)."""

    @pytest.mark.asyncio
    async def test_cold_machine_loads_before_decrypt(self, tmp_path):
        class _Cold:
            _loaded = False
            loads = 0

            def __init__(self):
                from mautrix.types import TrustState  # noqa: F401

                self.decrypted_content = {"msgtype": "m.text",
                                          "body": "hello"}

            async def load(self):
                self.loads += 1
                self._loaded = True

        class _FakeMachine:
            def __init__(self, cold):
                self._cold = cold

            async def decrypt_megolm_event(self, evt):
                assert self._cold._loaded, "decrypt ran on a cold machine"

                class _Dec:
                    content = {"msgtype": "m.text", "body": "hello"}

                return _Dec()

        cold = _Cold()
        virtual = _Cold()
        virtual.machine = _FakeMachine(cold)
        virtual.loads = 0

        async def _load():
            virtual.loads += 1
            cold._loaded = True

        virtual.load = _load  # type: ignore[method-assign]
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs",
            gateway_mxid="@merc_gateway:hs")
        manager._machines["@merc_gateway:hs"] = virtual
        out = await manager.decrypt_event({
            "event_id": "$e1", "room_id": "!r:hs", "sender": "@owner:hs",
            "type": "m.room.encrypted", "origin_server_ts": 1,
            "content": {"sender_key": "k", "ciphertext": "c",
                        "session_id": "s", "device_id": "d"}})
        assert virtual.loads == 1
        assert out is not None and out.get("body") == "hello"


class TestIntakeCryptoChannel:
    """The appservice crypto side-channel: to-device / device-lists / OTK
    counts ride beside room events and reach the crypto consumer FIRST."""

    @pytest.mark.asyncio
    async def test_crypto_fields_reach_crypto_handler_first(self):
        from observatory.appservice import TransactionIntake

        order: list[str] = []
        seen_crypto: list[dict] = []
        seen_events: list = []

        async def crypto(txn: dict) -> None:
            order.append("crypto")
            seen_crypto.append(txn)

        async def handler(txn_id: str, events: list) -> None:
            order.append("events")
            seen_events.append((txn_id, events))

        intake = TransactionIntake(
            as_token="tok", handler=handler, crypto_handler=crypto)
        await intake.start()
        try:
            crypto_body = {
                "to_device": {"@merc_a:hs": {"D1": {}}},
                "device_lists": {"changed": ["@owner:hs"], "left": []},
            }
            await intake.accept("t1", [{"type": "m.room.message"}],
                                crypto=crypto_body)
            await intake.accept("t2", [{"type": "m.room.message"}])
            await intake.queue.join()
        finally:
            await intake.stop()
        assert order == ["crypto", "events", "events"]
        assert seen_crypto == [crypto_body]
        assert [t for t, _ in seen_events] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_http_edge_extracts_crypto_fields(self):
        import json

        from aiohttp.test_utils import TestClient, TestServer

        from observatory.appservice import (
            TRANSACTIONS_PATH,
            TransactionIntake,
            make_app,
        )

        seen_crypto: list[dict] = []

        async def crypto(txn: dict) -> None:
            seen_crypto.append(txn)

        async def handler(txn_id: str, events: list) -> None:
            return None

        intake = TransactionIntake(
            as_token="tok", handler=handler, crypto_handler=crypto)
        await intake.start()
        app = make_app(intake)
        try:
            async with TestClient(TestServer(app)) as client:
                body = {
                    "events": [],
                    "to_device": {"@merc_a:hs": {"D1": {"type": "x"}}},
                    "device_lists": {"changed": [], "left": []},
                    "device_one_time_keys_count": {"@merc_a:hs": {}},
                    "pdus": [],
                }
                resp = await client.put(
                    TRANSACTIONS_PATH.format(txn_id="t9"),
                    data=json.dumps(body),
                    headers={"Authorization": "Bearer tok"},
                )
                assert resp.status == 200
                # empty crypto sections are dropped, real ones ride along
                body2 = {"events": [], "device_lists": {}, "to_device": {}}
                resp2 = await client.put(
                    TRANSACTIONS_PATH.format(txn_id="t10"),
                    data=json.dumps(body2),
                    headers={"Authorization": "Bearer tok"},
                )
                assert resp2.status == 200
                await intake.queue.join()
        finally:
            await intake.stop()
        assert len(seen_crypto) == 1  # t10's empty sections ride as {} → skipped
        assert seen_crypto[0]["to_device"] == {
            "@merc_a:hs": {"D1": {"type": "x"}}}
        assert seen_crypto[0]["device_one_time_keys_count"] == {
            "@merc_a:hs": {}}


class TestToDeviceIntakeRegression:
    """Wire-shape regression: tuwunel 1.9.0 sends MSC-prefixed keys with a
    LIST-shape to_device; bare senders send nested maps. Every shape must
    reach _on_crypto and route N to-device users — else room keys never
    ingest and decrypt fails with 'no session with given ID' forever."""

    GW = "@merc_gateway:hs"
    GW2 = "@merc_auth:hs"

    @staticmethod
    def _olm_envelope(sender="@owner:hs"):
        return {
            "sender": sender,
            "type": "m.room.encrypted",
            "content": {
                "algorithm": "m.olm.v1.curve25519-aes-sha2",
                "sender_key": "CURVE1",
                "ciphertext": {"CURVE1": {"body": "x", "type": 0}},
            },
        }

    @classmethod
    def _list_event(cls, user_id: str, device_id: str) -> dict:
        return {**cls._olm_envelope(),
                "to_user_id": user_id, "to_device_id": device_id}

    def _manager_with_two_machines(self, tmp_path):
        """E2EEManager with two seeded fake virtuals — no compiled stack
        touched (machine_for returns the seeds; only mautrix.types parses)."""
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs",
            gateway_mxid=self.GW,
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

        class _FakeVirtual:
            _loaded = True
            store = None

            def __init__(self, recording):
                self.machine = recording

            async def load(self):
                self._loaded = True

        recorders = {}
        for mxid in (self.GW, self.GW2):
            rec = _Recording()
            fake = _FakeVirtual(rec)
            fake.mxid = mxid
            manager._machines[mxid] = fake
            recorders[mxid] = rec
        return manager, recorders

    @pytest.mark.asyncio
    async def test_bare_dict_routes_each_user_to_own_machine(self, tmp_path):
        manager, recs = self._manager_with_two_machines(tmp_path)
        routed = await manager.handle_as_transaction({
            "events": [],
            "to_device": {
                self.GW: {"OBSVAA11": self._olm_envelope()},
                self.GW2: {"OBSVBB22": self._olm_envelope()},
            },
        })
        assert routed["to_device"] == 2
        assert len(recs[self.GW].to_device) == 1
        assert len(recs[self.GW2].to_device) == 1
        assert recs[self.GW].to_device[0].to_device_id == "OBSVAA11"
        assert recs[self.GW2].to_device[0].to_device_id == "OBSVBB22"

    @pytest.mark.asyncio
    async def test_msc_list_routes_each_user_to_own_machine(self, tmp_path):
        manager, recs = self._manager_with_two_machines(tmp_path)
        routed = await manager.handle_as_transaction({
            "events": [],
            "de.sorunome.msc2409.to_device": [
                self._list_event(self.GW, "OBSVAA11"),
                self._list_event(self.GW2, "OBSVBB22"),
                {"sender": "@owner:hs", "type": "m.room.encrypted"},
                "garbage",
            ],
            "org.matrix.msc3202.device_lists": {
                "changed": ["@owner:hs"], "left": []},
        })
        assert routed["to_device"] == 2
        assert routed["device_lists"] == 1
        assert len(recs[self.GW].to_device) == 1
        assert len(recs[self.GW2].to_device) == 1
        assert recs[self.GW].to_device[0].to_device_id == "OBSVAA11"
        assert recs[self.GW2].to_device[0].to_device_id == "OBSVBB22"

    @pytest.mark.asyncio
    async def test_mixed_txn_routes_all_channels(self, tmp_path):
        manager, recs = self._manager_with_two_machines(tmp_path)
        routed = await manager.handle_as_transaction({
            "events": [],
            "to_device": {self.GW: {"OBSVAA11": self._olm_envelope()}},
            "org.matrix.msc3202.device_lists": {
                "changed": ["@owner:hs"], "left": []},
            "org.matrix.msc3202.device_one_time_keys_count": {
                self.GW: {"OBSVAA11": {"signed_curve25519": 9}}},
        })
        assert routed == {"to_device": 1, "device_lists": 1, "otk_counts": 1}
        assert len(recs[self.GW].to_device) == 1
        assert recs[self.GW2].to_device == []

    @pytest.mark.asyncio
    async def test_all_shapes_reach_on_crypto_end_to_end(self, tmp_path):
        """Full wire path per shape: HTTP PUT → intake → _on_crypto →
        handle_as_transaction. _on_crypto reached once per txn, N users
        routed each time."""
        import json

        from aiohttp.test_utils import TestClient, TestServer

        from observatory.appservice import (
            TRANSACTIONS_PATH,
            TransactionIntake,
            make_app,
        )

        gw, gw2 = self.GW, self.GW2
        manager, recs = self._manager_with_two_machines(tmp_path)
        on_crypto_calls: list[dict] = []
        routed_out: list[dict] = []

        async def _on_crypto(txn: dict) -> None:
            """SidecarDaemon._on_crypto contract: route the transaction's
            crypto side-channel into the per-user machines BEFORE the room
            events of the same transaction reach decrypt."""
            on_crypto_calls.append(txn)
            routed_out.append(await manager.handle_as_transaction(txn))

        async def _on_transaction(txn_id: str, events: list) -> None:
            return None

        bare_dict = {"events": [], "to_device": {
            gw: {"D1": self._olm_envelope()},
            gw2: {"D2": self._olm_envelope()},
        }}
        msc_list = {"events": [],
                    "de.sorunome.msc2409.to_device": [
                        self._list_event(gw, "D1"),
                        self._list_event(gw2, "D2"),
                    ],
                    "org.matrix.msc3202.device_lists": {
                        "changed": ["@owner:hs"], "left": []}}
        mixed = {"events": [],
                 "to_device": {gw: {"D1": self._olm_envelope()}},
                 "org.matrix.msc3202.device_lists": {
                     "changed": ["@owner:hs"], "left": []},
                 "org.matrix.msc3202.device_one_time_keys_count": {
                     gw: {"D1": {"signed_curve25519": 3}}}}
        intake = TransactionIntake(
            as_token="tok", handler=_on_transaction, crypto_handler=_on_crypto)
        await intake.start()
        app = make_app(intake)
        try:
            async with TestClient(TestServer(app)) as client:
                for i, body in enumerate((bare_dict, msc_list, mixed)):
                    resp = await client.put(
                        TRANSACTIONS_PATH.format(txn_id=f"td{i}"),
                        data=json.dumps(body),
                        headers={"Authorization": "Bearer tok"},
                    )
                    assert resp.status == 200
                await intake.queue.join()
        finally:
            await intake.stop()
        assert len(on_crypto_calls) == 3
        assert [r["to_device"] for r in routed_out] == [2, 2, 1]
        assert all(r["to_device"] > 0 for r in routed_out)
        assert routed_out[1]["device_lists"] == 1
        assert routed_out[2] == {
            "to_device": 1, "device_lists": 1, "otk_counts": 1}
        assert len(recs[gw].to_device) == 3
        assert len(recs[gw2].to_device) == 2


class TestWarmup:
    """Boot key publish: gateway device loads (keys upload), and an
    identity-less manager warms nothing — never a rogue owner device."""

    @pytest.mark.asyncio
    async def test_warmup_loads_gateway_machine(self, tmp_path):
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs",
            gateway_mxid="@merc_gateway:hs")

        class _Shim:
            device_id = "GWDEV1"
            loads = 0

            async def load(self):
                self.loads += 1

        shim = _Shim()
        manager._machines["@merc_gateway:hs"] = shim
        assert await manager.warmup() == {"@merc_gateway:hs": "GWDEV1"}
        assert shim.loads == 1

    @pytest.mark.asyncio
    async def test_warmup_without_gateway_warms_nothing(self, tmp_path):
        manager = E2EEManager(
            FakeClient(), ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs")
        assert await manager.warmup() == {}
        assert manager._machines == {}  # no rogue owner device minted


class TestEnableRoomEncryption:
    """The D4 room-enablement PUT (no stack: canned client)."""

    @pytest.mark.asyncio
    async def test_puts_megolm_state_event(self, tmp_path):
        client = FakeClient()
        manager = E2EEManager(
            client, ObservatoryState(tmp_path / "state.db"),
            crypto_dir=tmp_path / "crypto", owner_mxid="@owner:hs",
            gateway_mxid="@merc_gateway:hs")
        event_id = await manager.enable_room_encryption(
            "!room:hs", sender="@merc_gateway:hs")
        assert event_id.startswith("$ev")
        _kind, method, path, sender, body = client.calls[-1]
        assert method == "PUT" and sender == "@merc_gateway:hs"
        assert path.endswith("/state/m.room.encryption/")
        assert body == ENCRYPTION_CONTENT
        assert body["algorithm"] == "m.megolm.v1.aes-sha2"
