"""Gateway ghost out of spawned rooms (ghost-leave).

- Spawn creates rooms/spaces with NO gateway invite/join (owner + parent
  voice where the parent is not the gateway).
- Notice/attach paths survive ghost-not-member (attach fallback, send
  skip, power skip, decrypt-notice skip).
- Heal removes the gateway ghost from child rooms/spaces but keeps its
  OWN room/space + directives/root.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from observatory import tree
from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixError
from observatory.renderer import (
    AttachRoom,
    AttachSpace,
    CreateRoom,
    CreateSpace,
    IntentExecutor,
    Renderer,
    SendMessage,
    SetUserPower,
)
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch"
CHILD = "sa-direct"
CRON = "cron:nightly"


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
    add(CRON, "nightly", parent=GW, extra={"kind": "cron-job"})
    add(ORCH, "auth-refactor", parent=None)
    add(CHILD, "patch-audit", parent=ORCH)
    return state


def _seed_gateway_child(tmp_path) -> ObservatoryState:
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
    fail_senders: set = field(default_factory=set)

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def create_room(self, *, name, sender, preset, invite, space=False,
                          topic=None, initial_state=None):
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space))
        return self._id("!room")

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))
        if sender in self.fail_senders:
            raise MatrixError("PUT", "/rooms/x/state/m.room.power_levels/", 403,
                              {"errcode": "M_FORBIDDEN"})
        return "$ev"

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))
        if sender in self.fail_senders:
            raise MatrixError("PUT", "/rooms/x/state/m.space.child/y", 403,
                              {"errcode": "M_FORBIDDEN"})
        return "$ev"

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender))
        if sender in self.fail_senders:
            raise MatrixError("PUT", "/rooms/x/send/m.room.message/txn", 403,
                              {"errcode": "M_FORBIDDEN"})
        return self._id("$ev")

    async def join_room_as_owner(self, room_id: str) -> str:
        self.calls.append(("join_owner", room_id))
        return room_id

    async def join_room(self, room_id: str, *, sender: str) -> str:
        self.calls.append(("join", room_id, sender))
        return room_id

    async def leave_room(self, room_id: str, *, sender: str) -> None:
        self.calls.append(("leave", room_id, sender))
        return None

    async def get_power_levels(self, room_id: str, *, sender=None) -> dict:
        self.calls.append(("get_pl", room_id, sender))
        if sender in self.fail_senders:
            raise MatrixError("GET", "/rooms/x/state/m.room.power_levels/", 403,
                              {"errcode": "M_FORBIDDEN"})
        return {"users": {OWNER: 100}, "events_default": 0, "users_default": 0}

    async def room_hierarchy(self, space_id, *, sender=None):
        return {"rooms": []}


def _denied() -> MatrixError:
    return MatrixError("PUT", "/_matrix/client/v3/rooms/!x/state/m.space.child/!y",
                       403, {"errcode": "M_FORBIDDEN"})


@pytest.mark.asyncio
async def test_spawn_space_has_no_gateway_invite_or_join(tmp_path):
    """Spawned child space: owner + non-gateway parent only."""
    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    orch_mxid = state.get(ORCH)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    fake = FakeClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    await ex.execute([CreateSpace(CHILD, "patch audit", child_mxid)])
    creates = [c for c in fake.calls if c[0] == "create_room"]
    assert len(creates) == 1
    _, _, sender, _, invite, is_space = creates[0]
    assert is_space is True
    assert OWNER in invite
    assert gw_mxid not in invite
    assert orch_mxid in invite
    assert sender == child_mxid
    joins = [c[2] for c in fake.calls if c[0] == "join"]
    assert gw_mxid not in joins
    assert orch_mxid in joins


@pytest.mark.asyncio
async def test_spawn_room_has_no_gateway_invite_or_join(tmp_path):
    """Spawned child room: owner + non-gateway parent only."""
    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    orch_mxid = state.get(ORCH)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    fake = FakeClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    await ex.execute([CreateRoom(CHILD, "patch audit chat", CHILD, child_mxid, kind="chat")])
    creates = [c for c in fake.calls if c[0] == "create_room"]
    assert len(creates) == 1
    _, _, sender, _, invite, is_space = creates[0]
    assert is_space is False
    assert OWNER in invite
    assert gw_mxid not in invite
    assert orch_mxid in invite
    joins = [c[2] for c in fake.calls if c[0] == "join"]
    assert gw_mxid not in joins


@pytest.mark.asyncio
async def test_gateway_origin_child_has_no_gateway_parent_voice(tmp_path):
    """Gateway-parented child: parent voice suppressed, owner only."""
    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
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
        assert sender == child_mxid
    joins = [c[2] for c in fake.calls if c[0] == "join"]
    assert gw_mxid not in joins


@pytest.mark.asyncio
async def test_root_orchestrator_space_has_no_gateway_invite(tmp_path):
    """Root-level orchestrator (no parent): owner only, no gateway."""
    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    orch_mxid = state.get(ORCH)["mxid"]
    fake = FakeClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    await ex.execute([CreateSpace(ORCH, "auth-refactor", orch_mxid)])
    creates = [c for c in fake.calls if c[0] == "create_room"]
    assert len(creates) == 1
    _, _, _, _, invite, _ = creates[0]
    assert tuple(invite) == (OWNER,)
    joins = [c for c in fake.calls if c[0] == "join"]
    assert joins == []


@pytest.mark.asyncio
async def test_attach_falls_back_past_gateway_not_member(tmp_path):
    """Attach survives gateway-not-member via child/owner fallback then skip."""
    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    state.set_space_id(GW, "!s-gw:x")
    state.set_space_id(CHILD, "!s-child:x")
    fake = FakeClient(fail_senders={gw_mxid})
    # Owner fallback is outside the appservice namespace in production;
    # the double only fails the gateway sender here so the child ghost
    # path proves the fallback without tripping the owner masquerade.
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    # Parent voice for a gateway child is suppressed at create, but attach
    # still rides the parent sender with child fallback — force the parent
    # sender to prove the fallback chain.
    records = await ex.execute([AttachSpace(GW, CHILD, gw_mxid)])
    assert records[0]["op"] == "attach_space"
    senders = [c[3] for c in fake.calls if c[0] == "child"]
    assert senders[0] == gw_mxid
    assert len(senders) >= 2


@pytest.mark.asyncio
async def test_send_and_power_skip_on_ghost_not_member(tmp_path):
    """Send/power intents skip (never raise) on ghost-not-member."""
    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    state.set_room_id(CHILD, "!r-child:x")
    fake = FakeClient(fail_senders={child_mxid, gw_mxid})
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    records = await ex.execute([
        SendMessage(CHILD, child_mxid, "hello"),
        SetUserPower(CHILD, child_mxid, 50, sender=child_mxid),
    ])
    assert all(r["op"] == "skipped" and r["reason"] == "not-member" for r in records)


@pytest.mark.asyncio
async def test_decrypt_notice_skips_when_gateway_out(tmp_path):
    """Decrypt-failure notice never raises and never sends when out."""
    import observatory.sidecar_main as sm

    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]

    @dataclass
    class NoticeClient:
        calls: list = field(default_factory=list)

        async def send_message(self, room_id, body, *, sender, formatted_body=None):
            self.calls.append(("send", room_id, sender))
            raise _denied()

    daemon = sm.SidecarDaemon.__new__(sm.SidecarDaemon)
    daemon.client = NoticeClient()  # type: ignore[assignment]
    daemon.state = state  # type: ignore[assignment]
    daemon.gateway_mxid = gw_mxid
    daemon._decrypt_notified = set()  # type: ignore[attr-defined]

    async def _no_members(room_id: str) -> list[str]:
        return [OWNER]

    daemon._room_members = _no_members  # type: ignore[method-assign]
    await daemon._notice_decrypt_failure({"room_id": "!child:x", "event_id": "$e1"})
    assert daemon.client.calls == []


@pytest.mark.asyncio
async def test_heal_leaves_children_keeps_own_and_directives(tmp_path):
    """Heal leaves child ids, keeps gateway OWN + root/directives."""
    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    # Converged ids: gateway OWN + root/directives + children.
    state.set_space_id(GW, "!s-gw:x")
    state.set_room_id(GW, "!r-gw:x")
    state.set_meta("space:root", "!s-root:x")
    state.set_meta("room:directives", "!r-dir:x")
    state.set_space_id(ORCH, "!s-orch:x")
    state.set_room_id(ORCH, "!r-orch:x")
    state.set_space_id(CHILD, "!s-child:x")
    state.set_room_id(CHILD, "!r-child:x")
    fake = FakeClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    plan = Renderer(
        state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER, executor=None,
    ).build_plan(host="gatehost")
    left = await ex.ensure_gateway_leaves_plan(plan)
    leaves = {c[1] for c in fake.calls if c[0] == "leave"}
    assert "!s-orch:x" in leaves
    assert "!r-orch:x" in leaves
    assert "!s-child:x" in leaves
    assert "!r-child:x" in leaves
    assert "!s-gw:x" not in leaves
    assert "!r-gw:x" not in leaves
    assert "!s-root:x" not in leaves
    assert "!r-dir:x" not in leaves
    assert left >= 4


@pytest.mark.asyncio
async def test_heal_never_fails_converge(tmp_path):
    """Heal swallows leave failures (never fails converge)."""

    @dataclass
    class FailLeaveClient(FakeClient):
        async def leave_room(self, room_id: str, *, sender: str) -> None:
            self.calls.append(("leave", room_id, sender))
            raise RuntimeError("boom")

    state = _seed(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    state.set_space_id(ORCH, "!s-orch:x")
    state.set_room_id(ORCH, "!r-orch:x")
    fake = FailLeaveClient()
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER,
                        gateway_mxid=gw_mxid)
    plan = Renderer(
        state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER, executor=None,
    ).build_plan(host="gatehost")
    assert await ex.ensure_gateway_leaves_plan(plan) == 0


@dataclass
class MembersClient:
    """Members-read double: per-sender 403s plus a canned member chunk."""
    calls: list = field(default_factory=list)
    fail_senders: set = field(default_factory=set)
    chunk: list = field(default_factory=list)

    async def client_api(self, method, path, *, sender=None, params=None, json_body=None):
        self.calls.append(("client_api", method, str(path), sender))
        if sender in self.fail_senders:
            raise MatrixError("GET", path, 403, {"errcode": "M_FORBIDDEN"})
        return {"chunk": list(self.chunk)}


def _member_event(user_id: str) -> dict:
    return {"type": "m.room.member", "state_key": user_id,
            "content": {"membership": "join"}}


@pytest.mark.asyncio
async def test_sidecar_room_members_falls_back_past_member_voice_raise(tmp_path):
    """Member-voice 403 (gateway left the child room) falls back to the
    gateway reader — the member voice is tried first, never skipped."""
    import observatory.sidecar_main as sm

    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    state.set_room_id(CHILD, "!r-child:x")
    fake = MembersClient(fail_senders={child_mxid},
                         chunk=[_member_event(child_mxid), _member_event(OWNER)])
    daemon = sm.SidecarDaemon.__new__(sm.SidecarDaemon)
    daemon.client = fake  # type: ignore[assignment]
    daemon.state = state  # type: ignore[assignment]
    daemon.gateway_mxid = gw_mxid
    assert await daemon._room_members("!r-child:x") == [child_mxid, OWNER]
    senders = [c[3] for c in fake.calls]
    assert senders == [child_mxid, gw_mxid], "member voice must be tried before fallback"


@pytest.mark.asyncio
async def test_sidecar_room_members_all_readers_fail_returns_empty(tmp_path):
    """Ghost-not-member everywhere reads as not-member — never raises, so a
    members failure cannot veto the child turn or kill the reconcile."""
    import observatory.sidecar_main as sm

    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    state.set_room_id(CHILD, "!r-child:x")
    fake = MembersClient(fail_senders={child_mxid, gw_mxid})
    daemon = sm.SidecarDaemon.__new__(sm.SidecarDaemon)
    daemon.client = fake  # type: ignore[assignment]
    daemon.state = state  # type: ignore[assignment]
    daemon.gateway_mxid = gw_mxid
    assert await daemon._room_members("!r-child:x") == []


@pytest.mark.asyncio
async def test_e2ee_room_members_sender_first_then_gateway(tmp_path):
    """E2EE share path: the sender (always a member there) reads first,
    then the gateway; total failure reads as empty — never raises."""
    from observatory import e2ee as e2ee_mod

    state = _seed_gateway_child(tmp_path)
    gw_mxid = state.get(GW)["mxid"]
    child_mxid = state.get(CHILD)["mxid"]
    mgr = e2ee_mod.E2EEManager.__new__(e2ee_mod.E2EEManager)
    mgr.gateway_mxid = gw_mxid
    mgr.owner_mxid = OWNER
    # Sender-first: sender 403s, gateway chunk wins.
    mgr.client = MembersClient(fail_senders={child_mxid},
                               chunk=[_member_event(child_mxid)])
    assert await mgr._room_members("!r-child:x", fallback_sender=child_mxid) == [child_mxid]
    senders = [c[3] for c in mgr.client.calls]
    assert senders[0] == child_mxid and gw_mxid in senders
    # Total outage: empty, no raise — the share path fails closed downstream.
    mgr.client = MembersClient(fail_senders={child_mxid, gw_mxid, OWNER, None})
    assert await mgr._room_members("!r-child:x", fallback_sender=child_mxid) == []
