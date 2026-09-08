"""Contract tests for the Observatory agent-tree store (spec §2 discovery,
D8 depth semantics, D17 live-only collisions).

Filesystem is tmp_path-only; no live matrix, no real $MERCURY_HOME.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from observatory import state as state_mod
from observatory.state import (
    SCHEMA_VERSION,
    ObservatoryState,
    StateError,
    default_state_db_path,
    purge_on_death,
)


@pytest.fixture()
def store(tmp_path: Path) -> ObservatoryState:
    with ObservatoryState(tmp_path / "state.db") as s:
        yield s


def _node(node_id: str, **over: object) -> dict:
    kw = dict(
        engine="hermes",
        name=node_id,
        slug=node_id,
        mxid=f"@merc_{node_id}:mercury.local",
        session_ref=f"session:{node_id}",
    )
    kw.update(over)
    return kw  # type: ignore[return-value]


# --- schema & meta --------------------------------------------------------------


class TestSchema:
    def test_wal_mode_on_open(self, store: ObservatoryState):
        mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_schema_version_seeded_and_readable(self, store: ObservatoryState):
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)

    def test_reopen_is_idempotent_and_keeps_version(self, tmp_path: Path):
        db = tmp_path / "state.db"
        with ObservatoryState(db) as s:
            s.set_meta("marker", "kept")
            s.add_node("a", **_node("a"))
        with ObservatoryState(db) as s2:
            assert s2.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert s2.get_meta("marker") == "kept"
            assert s2.get("a")["status"] == "live"

    def test_future_schema_version_fails_hard(self, tmp_path: Path):
        db = tmp_path / "state.db"
        with ObservatoryState(db) as s:
            s.set_meta("schema_version", str(SCHEMA_VERSION + 99))
        with pytest.raises(StateError, match="newer"):
            ObservatoryState(db)

    def test_engine_check_constraint(self, store: ObservatoryState):
        with pytest.raises(sqlite3.IntegrityError):
            store.add_node("x", **_node("x", engine="telegram"))

    def test_default_path_lives_under_mercury_home(self, tmp_path: Path):
        # explicit home > env > engine home — same law as provision.
        assert default_state_db_path(tmp_path).name == "state.db"
        assert default_state_db_path(tmp_path).parent.name == "observatory"


# --- add / depth (D8: stored at insert) ------------------------------------------


class TestAddNode:
    def test_root_depth_zero_child_one_deeper(self, store: ObservatoryState):
        store.add_node("g", **_node("g"))
        store.add_node("k", parent_node_id="g", **_node("k"))
        assert store.get("g")["depth"] == 0
        assert store.get("k")["depth"] == 1

    def test_depth_computed_at_insert_not_recomputed_after(self, store: ObservatoryState):
        # A child's depth is frozen at spawn: later structural facts must
        # not rewrite it (deletion-timing semantics were fixed at birth).
        store.add_node("g", **_node("g"))
        store.add_node("k", parent_node_id="g", **_node("k"))
        before = store.get("k")["depth"]
        store.mark_dead("g")
        assert store.get("k")["depth"] == before

    def test_explicit_depth_frozen_verbatim(self, store: ObservatoryState):
        store.add_node("g", **_node("g"))
        store.add_node("k", parent_node_id="g", depth=7, **_node("k"))
        assert store.get("k")["depth"] == 7

    def test_missing_parent_fails_hard(self, store: ObservatoryState):
        with pytest.raises(StateError):
            store.add_node("orphan", parent_node_id="ghost", **_node("orphan"))

    def test_extra_json_roundtrip(self, store: ObservatoryState):
        store.add_node("g", extra={"kind": "gateway", "n": 3}, **_node("g"))
        assert store.get("g")["extra"] == {"kind": "gateway", "n": 3}

    def test_next_depth_helper(self, store: ObservatoryState):
        assert store.next_depth(None) == 0
        store.add_node("g", **_node("g"))
        assert store.next_depth("g") == 1


# --- death & purge (D8) -----------------------------------------------------------


class TestDeathSemantics:
    def test_purge_on_death_by_depth(self):
        # 1-agents die→purge instantly; ≥2 grace until parent dies;
        # 0-agents never auto-delete (restart is not death, D18).
        assert purge_on_death(0) is False
        assert purge_on_death(1) is True
        assert purge_on_death(2) is False
        assert purge_on_death(9) is False
        with pytest.raises(ValueError):
            purge_on_death(-1)

    def test_mark_dead_tombstones_but_never_deletes(self, store: ObservatoryState):
        store.add_node("g", **_node("g"))
        store.add_node("k", parent_node_id="g", **_node("k"))
        dead = store.mark_dead("k", died_epoch=123.0)
        assert dead["status"] == "dead"
        assert dead["died_epoch"] == 123.0
        assert [n["node_id"] for n in store.get_live()] == ["g"]
        # row survives (reading grace / transcript):
        assert store.get("k")["status"] == "dead"

    def test_zero_agent_mark_dead_keeps_row(self, store: ObservatoryState):
        # D8/D18: gateway + spawned orchestrators keep history until /exit.
        store.add_node("g", **_node("g"))
        store.mark_dead("g")
        assert store.get("g")["status"] == "dead"  # still there

    def test_mark_dead_unknown_node(self, store: ObservatoryState):
        with pytest.raises(StateError):
            store.mark_dead("ghost")

    def test_mark_deleted_and_purge_returns_row_and_removes_it(
        self, store: ObservatoryState
    ):
        store.add_node("g", **_node("g"))
        store.add_node("k", parent_node_id="g", **_node("k"))
        store.set_space_id("k", "!space-k")
        store.set_room_id("k", "!room-k")
        row = store.mark_deleted_and_purge("k")
        # caller gets everything needed to purge Tuwunel artifacts:
        assert row["space_id"] == "!space-k"
        assert row["room_id"] == "!room-k"
        assert row["mxid"] == "@merc_k:mercury.local"
        # ...and the row itself is gone (D17: no tombstone leaks state):
        with pytest.raises(StateError):
            store.get("k")
        assert [n["node_id"] for n in store.get_live()] == ["g"]


# --- queries -----------------------------------------------------------------------


class TestQueries:
    def _seed_tree(self, store: ObservatoryState) -> None:
        store.add_node("g", created_epoch=1.0, **_node("g"))
        store.add_node("a", parent_node_id="g", created_epoch=2.0, **_node("a"))
        store.add_node("b", parent_node_id="g", created_epoch=3.0, **_node("b"))
        store.add_node("a1", parent_node_id="a", created_epoch=4.0, **_node("a1"))
        store.add_node("a1x", parent_node_id="a1", created_epoch=5.0, **_node("a1x"))
        store.add_node("orch", created_epoch=6.0, **_node("orch"))  # second root

    def test_children_of_spawn_order(self, store: ObservatoryState):
        self._seed_tree(store)
        assert [n["node_id"] for n in store.children_of("g")] == ["a", "b"]

    def test_get_subtree_breadth_first_all_statuses(self, store: ObservatoryState):
        self._seed_tree(store)
        store.mark_dead("a1")
        assert [n["node_id"] for n in store.get_subtree("a")] == ["a", "a1", "a1x"]

    def test_find_live_by_slug_ignores_dead(self, store: ObservatoryState):
        self._seed_tree(store)
        store.mark_dead("a")
        assert [n["node_id"] for n in store.find_live_by_slug("a")] == []
        assert [n["node_id"] for n in store.find_live_by_slug("b")] == ["b"]

    def test_get_live_ordered_by_depth_then_spawn(self, store: ObservatoryState):
        self._seed_tree(store)
        assert [n["node_id"] for n in store.get_live()] == [
            "g",
            "orch",
            "a",
            "b",
            "a1",
            "a1x",
        ]


# --- matrix id upsert ----------------------------------------------------------------


class TestMatrixIdUpsert:
    def test_set_and_overwrite_ids(self, store: ObservatoryState):
        store.add_node("g", **_node("g"))
        store.set_space_id("g", "!s1")
        store.set_room_id("g", "!r1")
        assert (store.get("g")["space_id"], store.get("g")["room_id"]) == ("!s1", "!r1")
        store.set_space_id("g", "!s2")  # D18 respawn re-ensures idempotently
        assert store.get("g")["space_id"] == "!s2"

    def test_set_id_unknown_node_fails_hard(self, store: ObservatoryState):
        with pytest.raises(StateError):
            store.set_room_id("ghost", "!r")
