"""Child-room converge (gateway-origin delegation): tolerant batch + ghost membership.

ROOM NOT SPACE — regression pin: a gateway-origin delegation child must get
a visible space (attach converges) and content (lifecycle sends resolve),
even when one attach 403s mid-batch.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixError
from observatory.renderer import (
    AttachSpace,
    CreateRoom,
    CreateSpace,
    IntentExecutor,
)
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch"
CHILD = "sa-direct"


def _seed(tmp_path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, parent, extra=None):
        slug = assign_slug(name, state)
        return state.add_node(
            node_id, engine="hermes", name=name, slug=slug,
            mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
            parent_node_id=parent, extra=extra,
        )

    add(GW, "gateway agent", parent=None, extra={"kind": "gateway"})
    add(ORCH, "auth-refactor", parent=None)
    add(CHILD, "patch-audit", parent=ORCH)
    return state


def _seed_gateway_child(tmp_path) -> ObservatoryState:
    """Gateway-origin delegation child (parent is the gateway itself)."""
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, parent, extra=None):
        slug = assign_slug(name, state)
        return state.add_node(
            node_id, engine="hermes", name=name, slug=slug,
            mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
            parent_node_id=parent, extra=extra,
        )

    add(GW, "gateway agent", parent=None, extra={"kind": "gateway"})
    add(CHILD, "patch-audit", parent=GW)
    return state


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    next_id: int = 0
    fail_attach: set = field(default_factory=set)

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def create_room(self, *, name, sender, preset, invite, space=False,
                          topic=None, initial_state=None):
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space))
        return self._id("!room")

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))
        if (space_id, child_id) in self.fail_attach:
            raise MatrixError("PUT", "/_matrix/client/v3/rooms/x/state/m.space.child/y",
                              403, {"errcode": "M_FORBIDDEN"})
        return "$ev"

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender))
        return self._id("$ev")

    async def join_room_as_owner(self, room_id: str) -> str:
        self.calls.append(("join_owner", room_id))
        return room_id

    async def join_room(self, room_id: str, *, sender: str) -> str:
        self.calls.append(("join", room_id, sender))
        return room_id


def _denied() -> MatrixError:
    return MatrixError("PUT", "/_matrix/client/v3/rooms/!x/state/m.space.child/!y",
                       403, {"errcode": "M_FORBIDDEN"})


@pytest.mark.asyncio
async def test_batch_continues_past_403_attach(tmp_path):
    """(a) One failed attach never orphans the rest of the batch."""
    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    state.set_space_id(GW, "!s-gw:x")
    state.set_space_id(CHILD, "!s-child:x")
    fake = FakeClient(fail_attach={("!s-gw:x", "!s-child:x")})
    # The child ghost is not a member of the parent space on the first try;
    # the owner fallback is not joined in this double either — every sender
    # 403s, so the attach records skipped. The batch must still continue.
    orig_child = FakeClient.set_space_child

    async def _always_denied(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))
        raise _denied()

    fake.set_space_child = _always_denied.__get__(fake, FakeClient)  # type: ignore[method-assign]
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    records = await ex.execute([
        AttachSpace(GW, CHILD, gw_mxid),
        CreateRoom(CHILD, "patch audit chat", CHILD, child_mxid, kind="chat"),
    ])
    assert records[0]["op"] == "skipped" and records[0]["reason"] == "not-member"
    assert records[1]["op"] == "create_room" and records[1]["room_id"]
    # The created room id is lifecycle-able (a later SendMessage resolves it).
    assert ex.room_id(CHILD) == records[1]["room_id"]


@pytest.mark.asyncio
async def test_batch_continues_past_403_attach_with_owner_fallback(tmp_path):
    """(a2) Attach falls back from a non-member ghost to a member sender."""
    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    state.set_space_id(GW, "!s-gw:x")
    state.set_space_id(CHILD, "!s-child:x")

    seen: list = []

    class FallbackClient(FakeClient):
        async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
            seen.append(sender)
            self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))
            if sender == gw_mxid:
                raise _denied()
            return "$ev"

    fake = FallbackClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    records = await ex.execute([
        AttachSpace(GW, CHILD, gw_mxid),
        CreateRoom(CHILD, "patch audit chat", CHILD, child_mxid, kind="chat"),
    ])
    # First sender 403s, the fallback (child ghost, then owner) succeeds.
    assert records[0]["op"] == "attach_space"
    assert seen[0] == gw_mxid and len(seen) >= 2
    assert records[1]["op"] == "create_room"


@pytest.mark.asyncio
async def test_creation_invites_gateway_ghost_and_parent_voice(tmp_path):
    """(b) Child creation invites the owner + parent voice — never the gateway ghost."""
    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    orch_mxid = state.get(ORCH)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    fake = FakeClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    await ex.execute([
        CreateSpace(CHILD, "patch audit", child_mxid),
        CreateRoom(CHILD, "patch audit chat", CHILD, child_mxid, kind="chat"),
    ])
    creates = [c for c in fake.calls if c[0] == "create_room"]
    assert len(creates) == 2
    for _, _, sender, _, invite, _ in creates:
        assert OWNER in invite
        assert gw_mxid not in invite
        assert orch_mxid in invite
        assert sender not in invite
    joins = [(c[1], c[2]) for c in fake.calls if c[0] == "join"]
    joined_mxids = {mxid for _, mxid in joins}
    assert gw_mxid not in joined_mxids
    assert orch_mxid in joined_mxids


@pytest.mark.asyncio
async def test_decrypt_failure_notice_never_raises_on_403(tmp_path):
    """(c) The decrypt-failure notice skips silently when the gateway is out."""
    import observatory.sidecar_main as sm

    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]

    @dataclass
    class NoticeClient:
        calls: list = field(default_factory=list)

        async def send_message(self, room_id, body, *, sender, formatted_body=None):
            self.calls.append(("send", room_id, sender))
            raise MatrixError("PUT", "/_matrix/client/v3/rooms/!r/send/m.room.message/txn",
                              403, {"errcode": "M_FORBIDDEN"})

    daemon = sm.SidecarDaemon.__new__(sm.SidecarDaemon)
    daemon.client = NoticeClient()  # type: ignore[assignment]
    daemon.state = state  # type: ignore[assignment]
    daemon.gateway_mxid = gw_mxid
    daemon._decrypt_notified = set()  # type: ignore[attr-defined]

    async def _no_members(room_id: str) -> list[str]:
        return ["@owner:mercury.local"]

    daemon._room_members = _no_members  # type: ignore[method-assign]
    # Not a member -> skip silently, never raises, never sends.
    await daemon._notice_decrypt_failure({"room_id": "!child:x", "event_id": "$e1"})
    assert daemon.client.calls == []

    # Members unreadable (the members read itself 403s) -> also not-member.
    async def _denied_members(room_id: str) -> list[str]:
        raise _denied()

    daemon._decrypt_notified.clear()
    daemon._room_members = _denied_members  # type: ignore[method-assign]
    await daemon._notice_decrypt_failure({"room_id": "!child:x", "event_id": "$e2"})
    assert daemon.client.calls == []

    # Member but the send 403s -> swallowed, never raises.
    async def _member(room_id: str) -> list[str]:
        return [gw_mxid]

    daemon._decrypt_notified.clear()
    daemon._room_members = _member  # type: ignore[method-assign]
    await daemon._notice_decrypt_failure({"room_id": "!child:x", "event_id": "$e3"})
    assert len(daemon.client.calls) == 1


def test_sqlite_cli_check_warns_when_absent(monkeypatch, capsys):
    """(d) The observatory path warns (never raises) without the sqlite3 CLI."""
    import mercury_cli.setup as setup_mod

    monkeypatch.setattr(setup_mod.shutil, "which", lambda _name: None)
    result = setup_mod._ensure_sqlite3_cli()
    assert result == "missing"
    out = capsys.readouterr().out
    assert "sqlite3" in out
    assert "sudo" in out


def test_sqlite_cli_check_present_is_quiet(monkeypatch, capsys):
    import mercury_cli.setup as setup_mod

    def _which(name: str):
        return "/usr/bin/sqlite3" if name == "sqlite3" else None

    monkeypatch.setattr(setup_mod.shutil, "which", _which)
    assert setup_mod._ensure_sqlite3_cli() == "present"
