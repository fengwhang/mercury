"""Owner auto-join (VM round 2, item 2): the owner never sees invites /
'are you sure you want to join' on their own spaces+rooms.

Mechanism (real Matrix only — no protocol invented): every creation
already invites the owner; the sidecar now also accepts that invite on
the owner's behalf with the owner's own credential — the exact
``POST /_matrix/client/v3/join`` Element/FluffyChat send on a Join tap
(``MatrixClient.join_room_as_owner``). join_rule stays ``invite``
(presets unchanged): invite-only for everyone else. A converge-time
sweep heals pre-existing rooms whose invite was never accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from observatory import e2ee as e2ee_mod
from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixError
from observatory.renderer import (
    CreateRoom,
    CreateSpace,
    IntentExecutor,
    Renderer,
)
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    next_id: int = 0
    fail_join: set = field(default_factory=set)

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

    async def room_hierarchy(self, space_id, *, sender=None, suggested_only=False):
        return {"rooms": [{"room_id": space_id, "children_state": []}]}

    async def join_room_as_owner(self, room_id: str) -> str:
        self.calls.append(("join_owner", room_id))
        if room_id in self.fail_join:
            raise MatrixError("POST", "/join", 403, {"errcode": "M_FORBIDDEN"})
        return room_id


class FakeE2EE:
    def mark_room_encrypted(self, key, room_id):
        pass


def _seed(tmp_path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")
    slug = assign_slug("gateway agent", state)
    state.add_node(GW, engine="hermes", name="gateway agent", slug=slug,
                   mxid=virtual_mxid(slug), session_ref="session:gw",
                   extra={"kind": "gateway"})
    return state


def _executor(state, fake):
    return IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)


def _joins(fake) -> list:
    return [c[1] for c in fake.calls if c[0] == "join_owner"]


@pytest.mark.asyncio
async def test_create_space_and_room_join_owner(tmp_path):
    """Every creation accepts the owner invite on the owner's behalf."""
    state, fake = _seed(tmp_path), FakeClient()
    slug = assign_slug("agent 1", state)
    state.add_node("agent-1", engine="omp", name="agent 1", slug=slug,
                   mxid=virtual_mxid(slug), session_ref="session:a1")
    ex = _executor(state, fake)
    await ex.execute([
        CreateSpace("agent-1", "agent 1", "@merc_agent-1:mercury.local"),
        CreateRoom("agent-1", "agent 1 chat", "agent-1",
                   "@merc_agent-1:mercury.local"),
    ])
    space_id = state.get("agent-1")["space_id"]
    room_id = state.get("agent-1")["room_id"]
    assert space_id and room_id and space_id != room_id
    assert _joins(fake) == [space_id, room_id]


@pytest.mark.asyncio
async def test_failed_join_leaves_invite_and_succeeds(tmp_path):
    """Best-effort: a refused join never fails the creation (the invite
    stands — a normal pending invite, not a crash)."""
    state, fake = _seed(tmp_path), FakeClient(fail_join={"!room1"})
    ex = _executor(state, fake)
    await ex.execute([
        CreateRoom(GW, "gateway chat", GW, state.get(GW)["mxid"]),
    ])
    assert state.get(GW)["room_id"] == "!room1"  # recorded despite refusal
    assert _joins(fake) == ["!room1"]  # attempted


@pytest.mark.asyncio
async def test_encrypted_create_joins_owner(tmp_path):
    """The encrypted creation path joins the owner too."""
    state, fake = _seed(tmp_path), FakeClient()
    inner = _executor(state, fake)
    ex = e2ee_mod.EncryptedIntentExecutor(
        fake, state, owner_mxid=OWNER, server_name=SERVER, e2ee=FakeE2EE())
    await ex.execute([
        CreateRoom("agent-1", "agent 1 chat", "agent-1",
                   "@merc_agent-1:mercury.local"),
    ])
    assert _joins(fake) == [inner.room_id("agent-1")]


@pytest.mark.asyncio
async def test_sweep_heals_preexisting_rooms(tmp_path):
    """Converge-time sweep: pre-existing tracked ids get owner-joined; one
    refusal never stops the rest."""
    state, fake = _seed(tmp_path), FakeClient()
    state.set_space_id(GW, "!gw-space:x")
    state.set_room_id(GW, "!gw-room:x")
    fake.fail_join.add("!gw-room:x")
    ex = _executor(state, fake)
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=ex)
    joined = await ex.ensure_owner_in_plan(renderer.build_plan(host="gatehost"))
    assert joined == 1
    assert "!gw-space:x" in _joins(fake)
    assert "!gw-room:x" in _joins(fake)  # attempted despite the refusal
