"""VM-report slice 3: gateway agent owns a subspace (gw-space parity).

VM symptom: the gateway agent had a room but no space; spawned subagents
had nowhere to nest. Spec: every agent gets room+space, the gateway agent
included, with its delegation children nested under its subspace.

``Renderer.build_plan`` must yield: root space → [gw-agent subspace (room
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
    assert [s.key for s in plan.subspaces][0] == "gw-agent"
    gw_agent = plan.subspaces[0]
    # The gateway room lives in its subspace — never directly in root.
    assert [r.key for r in gw_agent.rooms] == [GW]
    assert [r.key for r in plan.rooms] == ["directives", CRON]


def test_gateway_origin_child_nests_under_gateway_subspace(tmp_path):
    plan = _plan(tmp_path)
    gw_agent = plan.subspaces[0]
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
    assert labels.index(("CreateSpace", "gw-agent")) < labels.index(("CreateRoom", GW))
    assert ("AttachRoom", GW) in labels
    gw_attach = next(i for i in intents
                     if isinstance(i, AttachSpace) and i.child_key == "gw-agent")
    assert gw_attach.parent_key == GW
    child_attach = next(i for i in intents
                        if isinstance(i, AttachSpace) and i.child_key == GWSA)
    assert child_attach.parent_key == "gw-agent"
    assert ("CreateRoom", GWSA) in labels
