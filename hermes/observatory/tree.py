"""Pure tree assembly and Matrix space-hierarchy planning (spec §3).

No I/O anywhere: these functions consume plain node dicts (the shape
:meth:`observatory.state.ObservatoryState.get` returns — ``extra`` parsed;
raw ``extra_json`` rows are also accepted) and a current-matrix snapshot
dict, and produce the desired space/room layout plus a symbolic diff plan
the renderer executes against Tuwunel.

Layout laws encoded here (spec §3 rules):
- Every agent = one space (even before children) + one chat room inside.
  The gateway agent is a FULL 0-agent (parity with /spawn): its own
  subspace holds its chat room, with gateway-origin delegation children
  nested inside as subspaces.
- Root space ("Mercury — <host>") child ORDER: gateway agent subspace,
  directives room, cron rooms, orchestrator subspaces, manual runs.
- Cron rooms sit DIRECTLY in the root space (D11) — modeled as gateway
  children with ``extra.kind == "cron-job"``, one room per job.
- Manual omp runs (D14) render under a read-only "Manual runs" subspace.
- Node kinds come from ``extra["kind"]``: ``"gateway"`` (root agent),
  ``"cron-job"`` (pseudo-room, not an agent), ``"manual-run"``
  (observed-only session); default = plain agent.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from observatory.identity import sanitize_display_name

#: Pseudo-room/space keys (stable across restarts; never collide with node
#: ids, which discovery prefixes — e.g. ``hermes:``/``omp:``).
DIRECTIVES_ROOM_KEY = "directives"
MANUAL_RUNS_SPACE_KEY = "manual-runs"
#: The Mercury root space (owns directives/cron rooms plus every depth-0
#: agent subspace, gateway first). Its matrix id persists in state meta
#: (``space:root``); every agent subspace — gateway included — lives on
#: its own node row like any other agent.
ROOT_SPACE_KEY = "root"
#: Legacy pseudo-key for the gateway agent's own subspace (pre-unification:
#: the gateway row held the ROOT id while its subspace id lived in state
#: meta). The planner no longer reads it — ``migrate_legacy_gateway_space``
#: (renderer) swaps the ids into the unified shape once — but the constant
#: stays so old deployments resolve the migration source.
GATEWAY_AGENT_SPACE_KEY = "gw-agent"

KIND_GATEWAY = "gateway"
KIND_CRON_JOB = "cron-job"
KIND_MANUAL_RUN = "manual-run"


def _extra(node: dict[str, Any]) -> dict[str, Any]:
    extra = node.get("extra")
    if isinstance(extra, dict):
        return extra
    if "extra_json" in node:
        raw = node["extra_json"]
        if isinstance(raw, dict):
            # Already parsed (e.g. raw sqlite rows materialized by a caller).
            return raw
        if isinstance(raw, str):
            import json

            try:
                parsed = json.loads(raw or "{}")
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return extra or {}


def _kind(node: dict[str, Any]) -> str:
    return _extra(node).get("kind", "")


# --- forest ---------------------------------------------------------------------


@dataclass
class Forest:
    """Agent tree from ``nodes`` rows: roots + children maps + id index."""

    roots: list[dict[str, Any]] = field(default_factory=list)
    children: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    def walk(self) -> Iterator[dict[str, Any]]:
        """Depth-first, spawn order (roots, then each node's children)."""
        stack = list(reversed(self.roots))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(self.children.get(node["node_id"], [])))


def _normalize(node: dict[str, Any]) -> dict[str, Any]:
    """Accept both state.get() dicts (``extra`` parsed) and raw sqlite rows
    (``extra_json``); return a shallow copy carrying ``extra``."""
    if "extra" in node:
        return node
    out = dict(node)
    out["extra"] = _extra(node)
    return out


def build_forest(nodes: Iterable[dict[str, Any]]) -> Forest:
    """Assemble rows into a Forest. Children keep DB spawn order
    (created_epoch, node_id); orphans — parent id absent from the input —
    surface as roots so partial discovery never drops a live agent."""
    nodes = [_normalize(n) for n in nodes]
    by_id = {n["node_id"]: n for n in nodes}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for n in sorted(nodes, key=lambda n: (n.get("created_epoch", 0), n["node_id"])):
        parent = n.get("parent_node_id")
        if parent is not None and parent in by_id:
            children.setdefault(parent, []).append(n)
        else:
            roots.append(n)
    return Forest(roots=roots, children=children, by_id=by_id)


# --- desired plan ----------------------------------------------------------------


@dataclass(frozen=True)
class RoomPlan:
    """A room that must exist inside exactly one space of the plan."""

    key: str
    name: str
    matrix_id: str | None = None  # known id from state; None = create
    kind: str = "chat"  # chat | directives | cron | manual-run


@dataclass(frozen=True)
class SpacePlan:
    """A space with its ordered rooms and nested subspaces."""

    key: str
    name: str
    matrix_id: str | None = None
    rooms: tuple[RoomPlan, ...] = ()
    subspaces: tuple["SpacePlan", ...] = ()
    kind: str = "agent"  # root | agent | manual-runs


def _room(node: dict[str, Any], *, kind: str = "chat") -> RoomPlan:
    return RoomPlan(
        key=node["node_id"],
        name=sanitize_display_name(node["name"]),
        matrix_id=node.get("room_id"),
        kind=kind,
    )


def _agent_space(node: dict[str, Any], children: dict[str, list[dict[str, Any]]]) -> SpacePlan:
    """Every agent = one space + its chat room, children spaces nested.

    THE single nesting rule (gateway included): the gateway agent is a
    normal depth-0 node — its key/matrix id come from its own row exactly
    like any spawned orchestrator, and its delegation children nest inside
    identically. The gateway's only differences are command scoping
    (accepts /spawn /spawnomp /restart, refuses /exit), enforced at the
    command-routing layer (control + gateway slash handlers), never here.
    Cron pseudo-rooms never nest — they sit directly in the root (D11).
    """
    return SpacePlan(
        key=node["node_id"],
        name=sanitize_display_name(node["name"]),
        matrix_id=node.get("space_id"),
        rooms=(_room(node),),
        subspaces=tuple(
            _agent_space(child, children)
            for child in children.get(node["node_id"], [])
            if _kind(child) != KIND_CRON_JOB  # cron pseudo-rooms never nest
        ),
    )


def desired_plan(
    forest: Forest,
    *,
    gateway_node_id: str,
    host: str | None = None,
    fixed_room_ids: dict[str, str] | None = None,
    fixed_space_ids: dict[str, str] | None = None,
) -> SpacePlan:
    """Forest → desired Mercury-rooted space hierarchy (spec §3).

    The gateway agent is a normal depth-0 agent subspace (same
    :func:`_agent_space` rule as every spawned orchestrator); the ROOT space
    owns directives/cron rooms plus every depth-0 agent subspace, gateway
    first. ``fixed_room_ids`` / ``fixed_space_ids`` carry matrix ids for
    pseudo targets (``directives``, ``manual-runs``, ``root``) that have no
    node row — the sidecar persists them in state meta; nodes carry their
    own ids.
    """
    if gateway_node_id not in forest.by_id:
        raise KeyError(f"gateway node {gateway_node_id!r} not in forest")
    fixed_room_ids = fixed_room_ids or {}
    fixed_space_ids = fixed_space_ids or {}
    gateway = forest.by_id[gateway_node_id]

    gateway_agent = _agent_space(gateway, forest.children)
    cron_rooms = tuple(
        _room(child, kind="cron")
        for child in forest.children.get(gateway_node_id, [])
        if _kind(child) == KIND_CRON_JOB
    )
    orchestrators = tuple(
        _agent_space(root, forest.children)
        for root in forest.roots
        if root["node_id"] != gateway_node_id and _kind(root) == ""
    )
    manual_rooms = tuple(
        _room(root, kind="manual-run")
        for root in forest.roots
        if _kind(root) == KIND_MANUAL_RUN
    )
    manual_runs = (
        SpacePlan(
            key=MANUAL_RUNS_SPACE_KEY,
            name="Manual runs",
            matrix_id=fixed_space_ids.get(MANUAL_RUNS_SPACE_KEY),
            rooms=manual_rooms,
            kind="manual-runs",
        )
        if manual_rooms
        else None
    )

    return SpacePlan(
        key=ROOT_SPACE_KEY,
        name=f"Mercury — {host if host is not None else socket.gethostname()}",
        matrix_id=fixed_space_ids.get(ROOT_SPACE_KEY),
        rooms=(
            RoomPlan(
                key=DIRECTIVES_ROOM_KEY,
                name="Directives",
                matrix_id=fixed_room_ids.get(DIRECTIVES_ROOM_KEY),
                kind="directives",
            ),
            *cron_rooms,
        ),
        subspaces=(
            gateway_agent,
            *orchestrators,
            *((manual_runs,) if manual_runs else ()),
        ),
        kind="root",
    )


def plan_index(plan: SpacePlan) -> tuple[dict[str, SpacePlan], dict[str, RoomPlan]]:
    """Flat key → spec maps (spaces, rooms) for the executor and tests."""
    spaces: dict[str, SpacePlan] = {}
    rooms: dict[str, RoomPlan] = {}

    def walk(space: SpacePlan) -> None:
        spaces[space.key] = space
        for room in space.rooms:
            rooms[room.key] = room
        for sub in space.subspaces:
            walk(sub)

    walk(plan)
    return spaces, rooms


def space_child_order(space: SpacePlan) -> tuple[RoomPlan | SpacePlan, ...]:
    """Desired child order of one space (op order = ``m.space.child``
    send order). The ROOT space leads with the gateway agent's subspace
    (gw-space parity: the gateway agent comes first, before the
    directives room); every other space lists its rooms first, then
    nested subspaces (an agent's own room before its children)."""
    if space.kind == "root" and space.subspaces:
        first, *rest = space.subspaces
        return (first, *space.rooms, *rest)
    return (*space.rooms, *space.subspaces)


# --- diff plan --------------------------------------------------------------------

@dataclass(frozen=True)
class CreateSpace:
    """Provision a new space (name is full unicode)."""

    key: str
    name: str


@dataclass(frozen=True)
class CreateRoom:
    """Provision a new room."""

    key: str
    name: str


@dataclass(frozen=True)
class AttachChild:
    """Nest a child space under its parent space (``m.space.child`` add).
    Symbolic keys — the executor resolves ids for freshly created targets."""

    parent_key: str
    child_key: str


@dataclass(frozen=True)
class AddRoom:
    """Place a room in a space (room-add; ``m.space.child`` with a room id)."""

    space_key: str
    room_key: str


@dataclass(frozen=True)
class DetachChild:
    """Remove a stale child (room or space id) from a space. Concrete ids —
    these come FROM the snapshot, so they are already resolved."""

    parent_id: str
    child_id: str


DiffOp = CreateSpace | CreateRoom | AttachChild | AddRoom | DetachChild


def diff_plan(snapshot: dict[str, Any], plan: SpacePlan) -> tuple[DiffOp, ...]:
    """Diff a current-matrix snapshot against the desired plan.

    Snapshot shape (renderer's view of Tuwunel state):

    ``{"spaces": {sid: {"name": str, "children": [ids...]}},
        "rooms": {rid: {"name": str}}}``

    Semantics:
    - desired space/room with no known id (or id absent from the snapshot)
      → ``CreateSpace``/``CreateRoom`` + symbolic attach/add op;
    - desired child with a known id NOT currently attached to its desired
      parent → attach/add op;
    - snapshot child of a known space that no desired child claims →
      ``DetachChild`` (dead agents purged per D8 land here).
    - ordering inside a space is carried by op order (``m.space.child``
      events are sent in sequence); reorder-only differences produce no ops.
    """
    snap_spaces: dict[str, dict[str, Any]] = snapshot.get("spaces", {})
    snap_rooms: dict[str, dict[str, Any]] = snapshot.get("rooms", {})
    ops: list[DiffOp] = []

    def space_known(space: SpacePlan) -> bool:
        return space.matrix_id is not None and space.matrix_id in snap_spaces

    def room_known(room: RoomPlan) -> bool:
        return room.matrix_id is not None and room.matrix_id in snap_rooms

    def current_children(space: SpacePlan) -> set[str]:
        if not space_known(space):
            return set()
        return set(snap_spaces[space.matrix_id].get("children", []))

    def walk(space: SpacePlan, parent: SpacePlan | None) -> None:
        if not space_known(space):
            ops.append(CreateSpace(key=space.key, name=space.name))
        if parent is not None and (
            space.matrix_id is None or space.matrix_id not in current_children(parent)
        ):
            ops.append(AttachChild(parent_key=parent.key, child_key=space.key))

        desired_ids = {
            child.matrix_id
            for child in (*space.rooms, *space.subspaces)
            if child.matrix_id is not None
        }
        for stale in sorted(current_children(space) - desired_ids):
            assert space.matrix_id is not None  # current_children() gate
            ops.append(DetachChild(parent_id=space.matrix_id, child_id=stale))

        attached = current_children(space)
        for child in space_child_order(space):
            if isinstance(child, RoomPlan):
                if not room_known(child):
                    ops.append(CreateRoom(key=child.key, name=child.name))
                if child.matrix_id not in attached:
                    ops.append(AddRoom(space_key=space.key, room_key=child.key))
            else:
                walk(child, parent=space)

    walk(plan, None)
    return tuple(ops)
