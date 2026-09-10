"""Backfill rule (VM round 2, item 1): children of unmirrored parents stay
unmirrored — no orphan rooms.

After a wipe+reprovision the server is new but state.db still holds
pre-wipe ids. The old snapshot() augmented the server hierarchy with every
state-known id, so a phantom parent counted as known: its subtree was never
recreated while brand-new children got fresh rooms — orphan child rooms
beside a roomless parent. Now snapshot() confirms state-known-but-unlisted
ids with the owner-token admin probe and drops phantoms, so the next diff
recreates the parent WITH its subtree in one consistent plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import CreateRoom, CreateSpace, IntentExecutor, Renderer
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
PARENT = "cli-agent"
CHILD = "cli-agent/sub"
PHANTOM_SPACE = "!phantom-space:mercury.local"
PHANTOM_ROOM = "!phantom-room:mercury.local"
GW_SPACE = "!gw-space:mercury.local"


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    next_id: int = 0
    rooms: dict = field(default_factory=dict)
    probe_map: dict = field(default_factory=dict)
    probed: list = field(default_factory=list)
    probe_error: object = None

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def create_room(self, *, name, sender, preset, invite, space=False,
                          topic=None, initial_state=None):
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space))
        rid = self._id("!room")
        self.rooms[rid] = {"name": name, "space": space}
        return rid

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))

    async def room_hierarchy(self, space_id, *, sender=None, suggested_only=False):
        children = {}
        for c in self.calls:
            if c[0] == "child" and not c[6]:
                children.setdefault(c[1], []).append(c[2])
        out = [{"room_id": GW_SPACE, "room_type": "m.space",
                "children_state": [
                    {"type": "m.space.child", "state_key": ch}
                    for ch in children.get(GW_SPACE, [])]}]
        return {"rooms": out}

    async def admin_room_alive(self, room_id: str) -> bool:
        self.probed.append(room_id)
        if self.probe_error is not None:
            raise self.probe_error
        return bool(self.probe_map.get(room_id, False))


def _seed(tmp_path, *, parent_space=None, parent_room=None):
    state = ObservatoryState(tmp_path / "state.db")
    slug = assign_slug("gateway agent", state)
    state.add_node(GW, engine="hermes", name="gateway agent", slug=slug,
                   mxid=virtual_mxid(slug), session_ref="session:gw",
                   extra={"kind": "gateway"})
    state.set_space_id(GW, GW_SPACE)
    pslug = assign_slug("cli agent", state)
    state.add_node(PARENT, engine="omp", name="cli agent", slug=pslug,
                   mxid=virtual_mxid(pslug), session_ref="session:cli")
    if parent_space:
        state.set_space_id(PARENT, parent_space)
    if parent_room:
        state.set_room_id(PARENT, parent_room)
    cslug = assign_slug("cli subagent", state)
    state.add_node(CHILD, engine="omp", name="cli subagent", slug=cslug,
                   mxid=virtual_mxid(cslug), session_ref="session:sub",
                   parent_node_id=PARENT)
    return state


def _renderer(state, fake):
    ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)
    return Renderer(state, gateway_node_id=GW, server_name=SERVER,
                    owner_mxid=OWNER, executor=ex)


@pytest.mark.asyncio
async def test_phantom_parent_recreated_with_subtree(tmp_path):
    """VM repro: phantom parent ids are dropped, so the diff recreates the
    parent WITH its child in one plan — no orphan child room."""
    fake = FakeClient()
    state = _seed(tmp_path, parent_space=PHANTOM_SPACE, parent_room=PHANTOM_ROOM)
    renderer = _renderer(state, fake)
    plan = renderer.build_plan(host="gatehost")
    snap = await renderer.snapshot(plan)
    assert PHANTOM_SPACE not in snap["spaces"]
    assert PHANTOM_ROOM not in snap["rooms"]
    assert set(fake.probed) == {PHANTOM_SPACE, PHANTOM_ROOM}

    applied = await renderer.apply_plan(plan)
    kinds = [(type(op).__name__, getattr(op, "key", None)) for op in applied]
    parent_space_idx = kinds.index(("CreateSpace", PARENT))
    child_space_idx = kinds.index(("CreateSpace", CHILD))
    child_room_idx = kinds.index(("CreateRoom", CHILD))
    assert parent_space_idx < child_space_idx < child_room_idx
    assert PHANTOM_SPACE not in str(applied)  # no attach references the phantom
    assert PHANTOM_ROOM not in str(applied)
    fresh = state.get(PARENT)
    assert fresh["space_id"] not in ("", PHANTOM_SPACE)
    assert fresh["room_id"] not in ("", PHANTOM_ROOM)


@pytest.mark.asyncio
async def test_confirmed_parent_not_duplicated(tmp_path):
    """A state-known parent the server confirms (created-but-unattached)
    counts as known: attach-only, never a duplicate space."""
    fake = FakeClient(probe_map={PHANTOM_SPACE: True, PHANTOM_ROOM: True})
    state = _seed(tmp_path, parent_space=PHANTOM_SPACE, parent_room=PHANTOM_ROOM)
    renderer = _renderer(state, fake)
    applied = await renderer.apply_plan(renderer.build_plan(host="gatehost"))
    creates = [op for op in applied
               if isinstance(op, (CreateSpace, CreateRoom)) and op.key == PARENT]
    assert creates == []
    assert state.get(PARENT)["space_id"] == PHANTOM_SPACE


@pytest.mark.asyncio
async def test_probe_error_keeps_attach_only(tmp_path):
    """A failing probe never invents a duplicate: errors degrade to the old
    attach-only behavior."""
    fake = FakeClient(probe_error=RuntimeError("admin down"))
    fake.probe_map = {}
    state = _seed(tmp_path, parent_space=PHANTOM_SPACE, parent_room=PHANTOM_ROOM)
    renderer = _renderer(state, fake)
    applied = await renderer.apply_plan(renderer.build_plan(host="gatehost"))
    creates = [op for op in applied
               if isinstance(op, (CreateSpace, CreateRoom)) and op.key == PARENT]
    assert creates == []
