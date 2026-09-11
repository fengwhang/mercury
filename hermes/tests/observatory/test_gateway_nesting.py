"""VM-report slice 3: gateway agent owns a subspace (unified planner).

VM symptom: the gateway agent had a room but no space; spawned subagents
had nowhere to nest. Spec: every agent gets room+space, the gateway agent
included, with its delegation children nested under its subspace.

``Renderer.build_plan`` must yield: root space → [gateway subspace (room
gw + one subspace per gateway-origin child), directives, cron rooms,
orchestrator subspaces]. This pins the exact VM shape (room present but
space absent) at the build_plan level — tree-level parity alone would not
catch a renderer regression that dropped the subspace.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import AttachRoom, AttachSpace, CreateRoom, CreateSpace, Renderer
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
GWSA = "sa-direct"  # gateway-origin delegation child (spawned subagent)
CRON = "cron:nightly"
ORCH = "orch"


def _seed(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, parent, extra=None):
        slug = assign_slug(name, state)
        return state.add_node(
            node_id, engine="hermes", name=name, slug=slug,
            mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
            parent_node_id=parent, extra=extra,
        )

    add(GW, "gateway agent", parent=None, extra={"kind": "gateway"})
    add(GWSA, "patch-audit", parent=GW)
    add(CRON, "nightly", parent=GW, extra={"kind": "cron-job"})
    add(ORCH, "auth-refactor", parent=None)
    return state


def _plan(tmp_path: Path):
    state = _seed(tmp_path)
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    return renderer.build_plan(host="gatehost")


def test_gateway_agent_has_own_subspace_with_room(tmp_path):
    plan = _plan(tmp_path)
    assert plan.key == "root"
    assert [s.key for s in plan.subspaces][0] == GW
    gw_agent = plan.subspaces[0]
    # The gateway room lives in its subspace — never directly in root.
    assert [r.key for r in gw_agent.rooms] == [GW]
    assert [r.key for r in plan.rooms] == ["directives", CRON]


def test_gateway_origin_child_nests_under_gateway_subspace(tmp_path):
    plan = _plan(tmp_path)
    gw_agent = plan.subspaces[0]
    assert gw_agent.key == GW
    assert [s.key for s in gw_agent.subspaces] == [GWSA]
    assert [r.key for r in gw_agent.subspaces[0].rooms] == [GWSA]
    # Cron pseudo-rooms never nest under the agent subspace (D11).
    assert CRON not in [s.key for s in gw_agent.subspaces]


def test_provision_intents_create_subspace_before_room(tmp_path):
    state = _seed(tmp_path)
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    plan = renderer.build_plan(host="gatehost")
    intents = renderer.plan_provision({"spaces": {}, "rooms": {}}, plan)
    labels = [
        (type(i).__name__,
         getattr(i, "key", None) or getattr(i, "child_key", None)
         or getattr(i, "room_key", None))
        for i in intents
    ]
    # Subspace exists before its room; the gateway child nests inside it.
    # Unified planner: the gateway subspace key is the gateway node id.
    assert labels.index(("CreateSpace", "root")) < labels.index(("CreateSpace", GW))
    assert labels.index(("CreateSpace", GW)) < labels.index(("CreateRoom", GW))
    assert ("AttachRoom", GW) in labels
    gw_attach = next(i for i in intents
                     if isinstance(i, AttachSpace) and i.child_key == GW)
    assert gw_attach.parent_key == "root"
    child_attach = next(i for i in intents
                        if isinstance(i, AttachSpace) and i.child_key == GWSA)
    assert child_attach.parent_key == GW
    assert ("CreateRoom", GWSA) in labels

ORCH_CHILD = "del-1"  # delegation child of a spawned orchestrator


def _seed_orch_child(tmp_path: Path) -> ObservatoryState:
    """Gateway + spawned orch + delegation child parented to the orch."""
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, parent, extra=None):
        slug = assign_slug(name, state)
        return state.add_node(
            node_id, engine="hermes", name=name, slug=slug,
            mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
            parent_node_id=parent, extra=extra,
        )

    add(GW, "gateway agent", parent=None, extra={"kind": "gateway"})
    add(ORCH, "carlos", parent=None)
    add(ORCH_CHILD, "test-sweep", parent=ORCH)
    return state


def test_orchestrator_child_nests_under_parent_space(tmp_path):
    """Delegation children of a spawned orchestrator nest INSIDE the
    parent's space — never as 0-level root spaces (gateway parity)."""
    state = _seed_orch_child(tmp_path)
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    plan = renderer.build_plan(host="gatehost")
    orch = next(s for s in plan.subspaces if s.key == ORCH)
    assert [r.key for r in orch.rooms] == [ORCH]
    assert [s.key for s in orch.subspaces] == [ORCH_CHILD]
    assert [r.key for r in orch.subspaces[0].rooms] == [ORCH_CHILD]
    # Still exactly the root children: gateway + one orch subspace.
    assert [s.key for s in plan.subspaces] == [GW, ORCH]


def test_orchestrator_child_provision_attaches_under_parent(tmp_path):
    """Provision intents attach the orch child's space to the orch space."""
    state = _seed_orch_child(tmp_path)
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    plan = renderer.build_plan(host="gatehost")
    intents = renderer.plan_provision({"spaces": {}, "rooms": {}}, plan)
    child_attach = next(i for i in intents
                        if isinstance(i, AttachSpace) and i.child_key == ORCH_CHILD)
    assert child_attach.parent_key == ORCH
    assert ("CreateRoom", ORCH_CHILD) in [
        (type(i).__name__, getattr(i, "key", None)) for i in intents
    ]


def test_orchestrator_child_death_clears_space(tmp_path):
    """Depth-1 orch child death purges its own space+room, detaches from
    the orch space, and lands the summary in the ORCH room."""
    from observatory.renderer import DetachChild, PurgeRoom, SendMessage

    state = _seed_orch_child(tmp_path)
    state.set_space_id(GW, "!gw-space:x")
    state.set_space_id(ORCH, "!sp-orch:x")
    state.set_room_id(ORCH, "!room-orch:x")
    state.set_space_id(ORCH_CHILD, "!sp-del:x")
    state.set_room_id(ORCH_CHILD, "!room-del:x")
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    intents = renderer.plan_death(ORCH_CHILD, status="failed", summary="boom")
    purged = {i.room_id for i in intents if isinstance(i, PurgeRoom)}
    assert purged == {"!sp-del:x", "!room-del:x"}
    detach = next(i for i in intents if isinstance(i, DetachChild))
    assert detach.space_id == "!sp-orch:x" and detach.child_id == "!sp-del:x"
    summaries = [i for i in intents if isinstance(i, SendMessage)]
    assert len(summaries) == 1 and summaries[0].room_key == ORCH


def test_depth2_grandchild_settles_without_purge(tmp_path):
    """Depth>=2 settles: marker in its own room, summary to the parent —
    artifacts survive until the parent dies (D8)."""
    from observatory.renderer import PurgeRoom, SendMessage

    state = _seed_orch_child(tmp_path)
    state.add_node(
        "del-1/0", engine="hermes", name="lint", slug="lint-x",
        mxid="@merc_lint:x", session_ref="session:del-1/0",
        parent_node_id=ORCH_CHILD,
    )
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    intents = renderer.plan_death("del-1/0", status="completed", summary="ok")
    assert not [i for i in intents if isinstance(i, PurgeRoom)]
    rooms = {i.room_key for i in intents if isinstance(i, SendMessage)}
    assert rooms == {"del-1/0", ORCH_CHILD}


def test_gateway_child_death_purges_like_orch_child(tmp_path):
    """Unified planner regression: a gateway-origin depth-1 child dies
    exactly like a spawned-orchestrator child — instant purge of its own
    space+room, detach from the GATEWAY space (not the root), summary in
    the gateway room, leave-then-delete before the purge."""
    from observatory.renderer import DetachChild, LeaveRoom, PurgeRoom, SendMessage

    state = _seed(tmp_path)
    state.set_space_id(GW, "!gw-sub:x")
    state.set_room_id(GW, "!gw-room:x")
    state.set_space_id(GWSA, "!sp-gwsa:x")
    state.set_room_id(GWSA, "!room-gwsa:x")
    state.set_meta("space:root", "!root:x")
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    intents = renderer.plan_death(GWSA, status="completed", summary="audit done")
    purged = {i.room_id for i in intents if isinstance(i, PurgeRoom)}
    assert purged == {"!sp-gwsa:x", "!room-gwsa:x"}
    detach = next(i for i in intents if isinstance(i, DetachChild))
    assert detach.space_id == "!gw-sub:x" and detach.child_id == "!sp-gwsa:x"
    summaries = [i for i in intents if isinstance(i, SendMessage)]
    assert len(summaries) == 1 and summaries[0].room_key == GW
    leaves = [i for i in intents if isinstance(i, LeaveRoom)]
    assert leaves, "leave-then-delete must precede the purge"
    assert {i.room_id for i in leaves} == purged
    first_purge = next(n for n, i in enumerate(intents) if isinstance(i, PurgeRoom))
    assert all(n < first_purge for n, i in enumerate(intents) if isinstance(i, LeaveRoom))


def test_legacy_gateway_space_migrates_to_unified(tmp_path):
    """Pre-unification rows (gateway row holds ROOT, meta holds subspace)
    migrate once: gateway row takes its own subspace, root moves to meta."""
    from observatory.renderer import migrate_legacy_gateway_space

    state = _seed(tmp_path)
    state.set_space_id(GW, "!root:x")
    state.set_meta("space:gw-agent", "!gw-sub:x")
    assert migrate_legacy_gateway_space(state, GW) is True
    assert state.get(GW)["space_id"] == "!gw-sub:x"
    assert state.get_meta("space:root") == "!root:x"
    # Idempotent: second run is a no-op.
    assert migrate_legacy_gateway_space(state, GW) is False
    # The unified plan keys the gateway subspace by its node id.
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    plan = renderer.build_plan(host="gatehost")
    assert plan.key == "root"
    assert plan.matrix_id == "!root:x"
    gw_agent = plan.subspaces[0]
    assert gw_agent.key == GW and gw_agent.matrix_id == "!gw-sub:x"
    assert [s.key for s in gw_agent.subspaces] == [GWSA]
