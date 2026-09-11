"""Defect (iii): rooms encrypted from the first event, poison detected.

- Creation rides ``initial_state`` (no plaintext window); the follow-up
  PUT path was the poison source.
- ``detect_poisoned_rooms`` finds pre-fix rooms (unencrypted, or
  encrypted later than the tolerance) for the setup converge offer.
- Every decrypt failure surfaces with recovery steps (never a bare
  "unable to decrypt"); the sidecar posts one notice per room.

No homeserver: fake clients throughout.
"""
from __future__ import annotations

import asyncio

import pytest

from observatory import e2ee as e2ee_mod
from observatory.matrix_client import MatrixError
from observatory.renderer import CreateRoom
from observatory.state import ObservatoryState


class _FakeClient:
    def __init__(self):
        self.created: list[dict] = []
        self.powers: list[dict] = []

    async def create_room(self, *, name=None, sender=None, preset=None,
                          invite=(), space=False, topic=None,
                          initial_state=None):
        self.created.append({"name": name, "sender": sender,
                             "initial_state": initial_state})
        return "!room:x"

    async def set_power_levels(self, room_id, users, *, sender):
        self.powers.append({"room_id": room_id, "users": users})
        return ""


class _FakeE2EE:
    def __init__(self):
        self.marked: list[tuple[str, str]] = []
        self.enabled_calls = 0

    def mark_room_encrypted(self, key, room_id):
        self.marked.append((key, room_id))

    async def enable_room_encryption(self, room_id, *, sender):
        self.enabled_calls += 1
        return ""


def _executor(tmp_path, client, e2ee):
    from observatory.renderer import IntentExecutor, Renderer
    state = ObservatoryState(tmp_path / "state.db")
    inner = IntentExecutor(client, state, owner_mxid="@o:x",
                           server_name="x")
    return e2ee_mod.EncryptedIntentExecutor(
        client, state, owner_mxid="@o:x", server_name="x", e2ee=e2ee)


def test_create_is_encrypted_from_first_event(tmp_path):
    client, e2ee = _FakeClient(), _FakeE2EE()
    ex = _executor(tmp_path, client, e2ee)
    op = CreateRoom(key="gw", name="g", space_key="gw-agent",
                    sender="@gw:x")
    rid = asyncio.run(ex.execute([op]))
    assert rid[0]["room_id"] == "!room:x"
    (created,) = client.created
    states = created["initial_state"]
    assert states == [{"type": "m.room.encryption", "state_key": "",
                       "content": dict(e2ee_mod.ENCRYPTION_CONTENT)}]
    assert e2ee_mod.ENCRYPTION_CONTENT["algorithm"] == "m.megolm.v1.aes-sha2"
    # No follow-up PUT path (the poison window): enable never called.
    assert e2ee.enabled_calls == 0
    assert e2ee.marked == [("gw", "!room:x")]
    assert client.powers[0]["users"] == {"@o:x": 100}


class _StateClient:
    """get_room_state from canned (create_ts, encryption_ts|None)."""

    def __init__(self, rooms: dict[str, tuple[float | None, float | None]]):
        self.rooms = rooms

    async def get_room_state(self, room_id, event_type, state_key="", *,
                             sender=None):
        created_ts, encrypted_ts = self.rooms[room_id]
        if event_type == "m.room.create":
            if created_ts is None:
                raise MatrixError("GET", "state", 404, {})
            return {"origin_server_ts": int(created_ts * 1000)}
        assert event_type == "m.room.encryption"
        if encrypted_ts is None:
            raise MatrixError("GET", "state", 404,
                              {"errcode": "M_NOT_FOUND"})
        return {"origin_server_ts": int(encrypted_ts * 1000)}


def test_detect_flags_unencrypted_and_late_rooms():
    client = _StateClient({
        "!atomic:x": (1000.0, 1000.5),      # clean
        "!late:x": (1000.0, 4600.0),        # poisoned (1h window)
        "!plain:x": (1000.0, None),         # unencrypted
    })
    found = asyncio.run(e2ee_mod.detect_poisoned_rooms(
        client, [("a", "!atomic:x"), ("b", "!late:x"), ("c", "!plain:x")],
        sender="@gw:x"))
    by_key = {p["key"]: p for p in found}
    assert set(by_key) == {"b", "c"}
    assert by_key["b"]["status"] == "poisoned"
    assert by_key["b"]["gap_seconds"] == pytest.approx(3600.0)
    assert by_key["c"]["status"] == "unencrypted"


def test_detect_never_raises_on_broken_rooms():
    class _Boom:
        async def get_room_state(self, *a, **k):
            raise RuntimeError("hs down")

    assert asyncio.run(e2ee_mod.detect_poisoned_rooms(
        _Boom(), [("a", "!x:y")], sender="@gw:x")) == []


def test_decrypt_failure_notice_carries_recovery():
    text = e2ee_mod.decrypt_failure_notice("$ev1", "!room:x")
    assert "$ev1" in text and "!room:x" in text
    # Element X reality: force-close/rejoin + fresh message, re-converge
    # offer, and the reinstall-rotation caveat — never the old fiction.
    for step in ("force-close Element X", "FRESH message", "re-converge",
                 "ROTATE the Olm identity", "UNRECOVERABLE by design",
                 "mercury setup observatory"):
        assert step in text, step
    for fiction in ("verify the gateway-agent device", "request keys",
                    "FluffyChat"):
        assert fiction not in text, fiction


def test_sidecar_notifies_once_per_room():
    from observatory.sidecar_main import SidecarDaemon
    from types import SimpleNamespace

    sent: list[dict] = []

    class _C:
        async def send_message(self, room_id, body, *, sender):
            sent.append({"room_id": room_id, "body": body, "sender": sender})
            return "$n"

    async def _members(room_id: str) -> list[str]:
        return ["@gw:x"]

    self = SimpleNamespace(_decrypt_notified=set(), client=_C(),
                           gateway_mxid="@gw:x", _room_members=_members)
    event = {"type": "m.room.encrypted", "room_id": "!r:x",
             "event_id": "$e1", "content": {}}
    asyncio.run(SidecarDaemon._notice_decrypt_failure(self, event))
    asyncio.run(SidecarDaemon._notice_decrypt_failure(self, event))
    assert len(sent) == 1
    assert "force-close Element X" in sent[0]["body"]
    assert "request keys" not in sent[0]["body"]
    assert sent[0]["sender"] == "@gw:x"
