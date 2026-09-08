"""Contract tests for pure tree assembly + space planning + diff (spec §3).

Everything here is pure: node dicts in, plans/diffs out. No I/O.
"""
from __future__ import annotations

from typing import Any

import pytest

from observatory.tree import (
    DIRECTIVES_ROOM_KEY,
    MANUAL_RUNS_SPACE_KEY,
    AddRoom,
    AttachChild,
    CreateRoom,
    CreateSpace,
    DetachChild,
    build_forest,
    desired_plan,
    diff_plan,
    plan_index,
)


def _node(
    node_id: str,
    *,
    parent: str | None = None,
    depth: int = 0,
    name: str | None = None,
    epoch: float = 0.0,
    extra: dict[str, Any] | None = None,
    room_id: str | None = None,
    space_id: str | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "parent_node_id": parent,
        "engine": "hermes",
        "depth": depth,
        "name": name or node_id,
        "slug": node_id,
        "mxid": f"@merc_{node_id}:mercury.local",
        "session_ref": "s",
        "delegation_id": None,
        "status": "live",
        "created_epoch": epoch,
        "died_epoch": None,
        "room_id": room_id,
        "space_id": space_id,
        "extra": extra or {},
    }


def _gateway_tree() -> list[dict[str, Any]]:
    """The §3 reference shape: gateway + cron job + orchestrator with a
    3-deep chain under it + a manual omp run."""
    return [
        _node("gw", name="gateway", epoch=1.0, extra={"kind": "gateway"}),
        _node("cron-backup", parent="gw", depth=1, name="cron:backup",
              epoch=2.0, extra={"kind": "cron-job"}),
        _node("orch", name="auth-refactor", epoch=3.0),
        _node("sub", parent="orch", depth=1, name="docs sweep", epoch=4.0),
        _node("gc", parent="sub", depth=2, name="deep dive", epoch=5.0),
        _node("manual1", name="terminal omp", epoch=6.0,
              extra={"kind": "manual-run"}),
    ]


# --- build_forest -----------------------------------------------------------------


class TestBuildForest:
    def test_roots_and_children_maps(self):
        f = build_forest(_gateway_tree())
        assert [n["node_id"] for n in f.roots] == ["gw", "orch", "manual1"]
        assert [n["node_id"] for n in f.children["orch"]] == ["sub"]
        assert set(f.by_id) == {"gw", "cron-backup", "orch", "sub", "gc", "manual1"}

    def test_children_ordered_by_spawn_epoch(self):
        nodes = [
            _node("p", epoch=0.0),
            _node("late", parent="p", depth=1, epoch=9.0),
            _node("early", parent="p", depth=1, epoch=2.0),
        ]
        assert [n["node_id"] for n in build_forest(nodes).children["p"]] == [
            "early",
            "late",
        ]

    def test_orphan_promoted_to_root(self):
        # Partial discovery must never drop a live agent.
        nodes = [_node("a"), _node("b", parent="ghost", depth=1)]
        f = build_forest(nodes)
        assert [n["node_id"] for n in f.roots] == ["a", "b"]

    def test_walk_is_depth_first_complete(self):
        f = build_forest(_gateway_tree())
        seen = [n["node_id"] for n in f.walk()]
        assert seen.index("sub") < seen.index("gc")
        assert set(seen) == set(f.by_id)

    def test_accepts_raw_extra_json_rows(self):
        nodes = [_node("gw", extra={"kind": "gateway"})]
        import json

        nodes[0]["extra_json"] = nodes[0].pop("extra")
        f = build_forest(nodes)
        assert f.by_id["gw"]["extra"] == {"kind": "gateway"}


# --- desired_plan -------------------------------------------------------------------


class TestDesiredPlan:
    def _plan(self, **kw: Any):
        tree = _gateway_tree()
        plan = desired_plan(build_forest(tree), gateway_node_id="gw", host="box", **kw)
        return plan

    def test_gateway_space_order_is_spec_order(self):
        # §3 rule: gateway room, directives room, cron rooms, orchestrator
        # subspaces (manual runs last per layout).
        root = self._plan()
        assert [r.key for r in root.rooms] == ["gw", "directives", "cron-backup"]
        assert [s.key for s in root.subspaces] == ["orch", MANUAL_RUNS_SPACE_KEY]

    def test_root_space_name_carries_host(self):
        assert self._plan().name == "Mercury — box"

    def test_every_agent_is_space_plus_room_nested_by_parent(self):
        root = self._plan()
        orch = root.subspaces[0]
        assert (orch.key, orch.rooms[0].key, orch.rooms[0].name) == (
            "orch",
            "orch",
            "auth-refactor",
        )
        sub = orch.subspaces[0]
        assert sub.key == "sub" and sub.rooms[0].key == "sub"
        assert sub.subspaces[0].key == "gc"  # grandchild nests one level deeper

    def test_cron_jobs_are_rooms_not_spaces(self):
        root = self._plan()
        cron = root.rooms[2]
        assert cron.kind == "cron"
        assert all(s.key != "cron-backup" for s in root.subspaces)

    def test_manual_runs_subspace_only_when_runs_exist(self):
        root = self._plan()
        manual = root.subspaces[-1]
        assert manual.kind == "manual-runs"
        assert manual.rooms[0].key == "manual1" and manual.rooms[0].kind == "manual-run"
        # Without manual nodes the subspace disappears entirely:
        tree = [n for n in _gateway_tree() if n["node_id"] != "manual1"]
        plan = desired_plan(build_forest(tree), gateway_node_id="gw", host="box")
        assert [s.key for s in plan.subspaces] == ["orch"]

    def test_fixed_ids_flow_into_pseudo_specs(self):
        root = self._plan(
            fixed_room_ids={DIRECTIVES_ROOM_KEY: "!rdir"},
            fixed_space_ids={MANUAL_RUNS_SPACE_KEY: "!smanual"},
        )
        assert root.rooms[1].matrix_id == "!rdir"
        assert root.subspaces[-1].matrix_id == "!smanual"

    def test_node_matrix_ids_flow_into_specs(self):
        tree = _gateway_tree()
        tree[0]["room_id"] = "!rgw"
        tree[0]["space_id"] = "!sgw"
        tree[2]["space_id"] = "!sorch"
        root = desired_plan(build_forest(tree), gateway_node_id="gw", host="box")
        assert root.matrix_id == "!sgw" and root.rooms[0].matrix_id == "!rgw"
        assert root.subspaces[0].matrix_id == "!sorch"

    def test_unknown_gateway_fails_hard(self):
        with pytest.raises(KeyError):
            desired_plan(build_forest(_gateway_tree()), gateway_node_id="nope")

    def test_unicode_names_preserved_in_spec_names(self):
        tree = [_node("gw", extra={"kind": "gateway"}),
                _node("orch", name="Борис 資料整理", epoch=1.0)]
        root = desired_plan(build_forest(tree), gateway_node_id="gw", host="b")
        assert root.subspaces[0].name == "Борис 資料整理"

    def test_plan_index_flattens(self):
        spaces, rooms = plan_index(self._plan())
        assert set(spaces) == {"gw", "orch", "sub", "gc", "manual-runs"}
        assert {"gw", "directives", "cron-backup", "orch", "sub", "gc", "manual1"} <= set(rooms)


# --- diff_plan ------------------------------------------------------------------------


class TestDiffPlan:
    def test_empty_snapshot_creates_everything_in_order(self):
        root = desired_plan(
            build_forest(_gateway_tree()), gateway_node_id="gw", host="box"
        )
        ops = diff_plan({}, root)
        kinds = [type(o).__name__ for o in ops]
        # Root first, then its rooms, then subspaces depth-first:
        assert ops[0] == CreateSpace(key="gw", name="Mercury — box")
        assert kinds[:6] == [
            "CreateSpace",  # root (no attach — top of the hierarchy)
            "CreateRoom", "AddRoom",    # gateway room
            "CreateRoom", "AddRoom",    # directives
            "CreateRoom", "AddRoom",    # cron — wait, see refined asserts below
        ]
        # (the block above is positional context; the real law:)
        assert CreateRoom(key="cron-backup", name="cron:backup") in ops
        assert AttachChild(parent_key="gw", child_key="orch") in ops
        assert AddRoom(space_key="orch", room_key="orch") in ops
        assert AttachChild(parent_key="orch", child_key="sub") in ops
        assert AttachChild(parent_key="sub", child_key="gc") in ops
        assert AddRoom(space_key="manual-runs", room_key="manual1") in ops

    def test_converged_state_diffs_to_nothing(self):
        # After the renderer provisions everything AND writes ids back to
        # state, a fresh plan must diff to the empty op set.
        tree = _gateway_tree()
        tree[0]["room_id"], tree[0]["space_id"] = "!rgw", "!sgw"
        tree[1]["room_id"] = "!rcron"
        tree[2]["room_id"], tree[2]["space_id"] = "!rorch", "!sorch"
        tree[3]["room_id"], tree[3]["space_id"] = "!rsub", "!ssub"
        tree[4]["room_id"], tree[4]["space_id"] = "!rgc", "!sgc"
        tree[5]["room_id"] = "!rmanual"
        root = desired_plan(
            build_forest(tree),
            gateway_node_id="gw",
            host="box",
            fixed_room_ids={DIRECTIVES_ROOM_KEY: "!rdir"},
            fixed_space_ids={MANUAL_RUNS_SPACE_KEY: "!smanual"},
        )
        snapshot = {
            "spaces": {
                "!sgw": {"name": "x", "children": ["!rgw", "!rdir", "!rcron", "!sorch", "!smanual"]},
                "!sorch": {"name": "x", "children": ["!rorch", "!ssub"]},
                "!ssub": {"name": "x", "children": ["!rsub", "!sgc"]},
                "!sgc": {"name": "x", "children": ["!rgc"]},
                "!smanual": {"name": "x", "children": ["!rmanual"]},
            },
            "rooms": {f"!r{k}": {"name": "x"} for k in ("gw", "dir", "cron", "orch", "sub", "gc", "manual")},
        }
        assert diff_plan(snapshot, root) == ()

    def test_stale_child_detaches(self):
        # A purged 1-agent's leftovers (D8) surface as a detach on the
        # parent space, with CONCRETE ids from the snapshot.
        tree = [_node("gw", extra={"kind": "gateway"}, room_id="!rgw", space_id="!sgw")]
        root = desired_plan(build_forest(tree), gateway_node_id="gw", host="b",
                            fixed_room_ids={DIRECTIVES_ROOM_KEY: "!rdir"})
        snapshot = {
            "spaces": {"!sgw": {"name": "x", "children": ["!rgw", "!rdir", "!sstale", "!rstale"]}},
            "rooms": {"!rgw": {"name": "x"}, "!rdir": {"name": "x"}, "!rstale": {"name": "x"}},
        }
        ops = diff_plan(snapshot, root)
        assert set(ops) == {
            DetachChild(parent_id="!sgw", child_id="!sstale"),
            DetachChild(parent_id="!sgw", child_id="!rstale"),
        }

    def test_detached_agent_space_reattaches(self):
        # D18 respawn re-ensures the tree: membership drifted (space no
        # longer a child of its parent) → symbolic attach emitted.
        tree = _gateway_tree()
        tree[0]["space_id"] = "!sgw"
        tree[2]["space_id"] = "!sorch"
        root = desired_plan(build_forest(tree), gateway_node_id="gw", host="b")
        snapshot = {
            "spaces": {
                "!sgw": {"name": "x", "children": []},
                "!sorch": {"name": "x", "children": []},
            },
            "rooms": {},
        }
        assert AttachChild(parent_key="gw", child_key="orch") in diff_plan(snapshot, root)

    def test_room_missing_from_space_gets_room_add(self):
        # Room exists globally but is not a child of its space → room-add.
        tree = [_node("gw", extra={"kind": "gateway"}, room_id="!rgw", space_id="!sgw")]
        root = desired_plan(build_forest(tree), gateway_node_id="gw", host="b")
        snapshot = {
            "spaces": {"!sgw": {"name": "x", "children": []}},
            "rooms": {"!rgw": {"name": "x"}},
        }
        assert diff_plan(snapshot, root) == (AddRoom(space_key="gw", room_key="gw"),)

    def test_known_ids_skip_creation(self):
        tree = [_node("gw", extra={"kind": "gateway"}, room_id="!rgw", space_id="!sgw")]
        root = desired_plan(build_forest(tree), gateway_node_id="gw", host="b",
                            fixed_room_ids={DIRECTIVES_ROOM_KEY: "!rdir"})
        snapshot = {
            "spaces": {"!sgw": {"name": "x", "children": ["!rgw", "!rdir"]}},
            "rooms": {"!rgw": {"name": "x"}, "!rdir": {"name": "x"}},
        }
        assert diff_plan(snapshot, root) == ()

    def test_ops_are_deterministic(self):
        root = desired_plan(
            build_forest(_gateway_tree()), gateway_node_id="gw", host="box"
        )
        assert diff_plan({}, root) == diff_plan({}, root)
