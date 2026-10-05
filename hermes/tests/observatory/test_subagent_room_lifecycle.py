"""Subagent room lifecycle (D8) — dead subagents must not leave zombie rooms.

The bug this pins: a finished 1-level subagent left its MIRC channel and its
mLounge sidebar entry behind forever, because row deletion was gated on
``OPER DESTROY`` converging. These tests assert the contract directly:

- depth 1 dies with its task, depth >= 2 dies with its parent, depth 0 never
- the visible cleanup (node rows + mLounge sidebar) happens at death time
  regardless of whether the channel destroy succeeded
- a channel destroy that cannot converge costs a retry, never a permanent row
- boot self-heal fixes zombies that already exist on an upgraded install
- deleting a room never deletes its history

Filesystem is tmp_path-only; no live matrix, no real ``$MERCURY_HOME``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from observatory.room_reaper import prune_mlounge_channels, reap_orphan_rooms
from observatory.spawn import ExitRecord, finish_exit, read_purge_journal
from observatory.state import (
    CLOSED_ROOMS_META_KEY,
    ObservatoryState,
    purge_on_death,
)


# ---------------------------------------------------------------------------
# the D8 predicate is the single source of truth for deletion timing
# ---------------------------------------------------------------------------


def test_purge_on_death_encodes_the_d8_law() -> None:
    """depth 1 purges at death; deeper waits for its parent; 0 never."""
    assert purge_on_death(0) is False
    assert purge_on_death(1) is True
    for depth in (2, 3, 9, 64):
        assert purge_on_death(depth) is False


def test_purge_on_death_rejects_negative_depth() -> None:
    with pytest.raises(ValueError):
        purge_on_death(-1)


# ---------------------------------------------------------------------------
# row deletion is never hostage to channel-destroy convergence
# ---------------------------------------------------------------------------


def _seed(state: ObservatoryState, node_id: str, *, depth: int, room: str,
          parent: str | None, status: str = "live") -> None:
    state.add_node(
        node_id,
        engine="hermes",
        name=node_id.split("/")[-1],
        slug=node_id.replace("/", "-").lower(),
        mxid=node_id.replace("/", "_"),
        session_ref=node_id,
        parent_node_id=parent,
        depth=depth,
    )
    state.set_room_id(node_id, room)
    if status != "live":
        state.mark_dead(node_id)


def test_finish_exit_deletes_rows_when_destroy_did_not_converge(tmp_path) -> None:
    """The zombie fix: a refused OPER must not keep the row alive."""
    state = ObservatoryState(tmp_path / "state.db")
    _seed(state, "orch", depth=0, room="#srv_root", parent=None)
    _seed(state, "deleg_1", depth=1, room="#srv_root-child", parent="orch")

    record = ExitRecord(
        journal_id="pj-test",
        node_id="deleg_1",
        status="completed",
        summary="",
        created_epoch=0.0,
        rows=[{"node_id": "deleg_1", "parent_node_id": "orch", "depth": 1}],
        channels=["#srv_root-child"],
    )
    state.set_meta("purge-journal", json.dumps([record.to_entry()]))

    # Destroy did NOT converge -> only the channel stays queued.
    finish_exit(state, record, pending_channels=["#srv_root-child"])

    # Row is gone immediately...
    assert [r["node_id"] for r in state.get_live()] == ["orch"]
    with state.locked() as db:
        left = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert left == 1, "dead row must not survive a failed channel destroy"

    # ...and the channel destroy is still queued, with no rows to block on.
    entries = read_purge_journal(state)
    assert len(entries) == 1
    assert entries[0]["channels"] == ["#srv_root-child"]
    assert entries[0]["rows"] == []


def test_finish_exit_drops_entry_when_nothing_pends(tmp_path) -> None:
    state = ObservatoryState(tmp_path / "state.db")
    _seed(state, "orch", depth=0, room="#srv_root", parent=None)
    _seed(state, "deleg_1", depth=1, room="#srv_root-child", parent="orch")
    record = ExitRecord(
        journal_id="pj-clean",
        node_id="deleg_1",
        status="completed",
        summary="",
        created_epoch=0.0,
        rows=[{"node_id": "deleg_1", "parent_node_id": "orch", "depth": 1}],
        channels=["#srv_root-child"],
    )
    state.set_meta("purge-journal", json.dumps([record.to_entry()]))

    finish_exit(state, record)

    assert read_purge_journal(state) == []
    assert [r["node_id"] for r in state.get_live()] == ["orch"]


# ---------------------------------------------------------------------------
# mLounge sidebar pruning — the part the user actually sees
# ---------------------------------------------------------------------------


def _write_user(home: Path, name: str, networks: list) -> Path:
    users = home / "observatory" / "lounge" / "home" / "users"
    users.mkdir(parents=True, exist_ok=True)
    path = users / f"{name}.json"
    path.write_text(json.dumps({"password": "x", "networks": networks}), encoding="utf-8")
    return path


def test_prune_mlounge_channels_removes_only_the_named_rooms(tmp_path) -> None:
    _write_user(
        tmp_path,
        "owner",
        [
            {
                "name": "nixpad",
                "channels": [
                    {"name": "#srv_root", "muted": False, "key": ""},
                    {"name": "#srv_root-child", "muted": False, "key": ""},
                    {"name": "#srv_root-zombie", "muted": False, "key": ""},
                ],
            },
            {
                "name": "other-net",
                "channels": [{"name": "#elsewhere", "muted": False, "key": ""}],
            },
        ],
    )

    result = prune_mlounge_channels(
        ["#srv_root-child", "#srv_root-zombie"], mercury_home=tmp_path
    )

    assert result == {"users": 1, "removed": 2}
    data = json.loads(
        (tmp_path / "observatory/lounge/home/users/owner.json").read_text(encoding="utf-8")
    )
    kept = {c["name"] for n in data["networks"] for c in n["channels"]}
    assert kept == {"#srv_root", "#elsewhere"}, "unrelated networks/channels must survive"


def test_prune_mlounge_channels_matches_case_insensitively(tmp_path) -> None:
    _write_user(
        tmp_path,
        "owner",
        [{"name": "nixpad", "channels": [{"name": "#NixPad_Child", "muted": False, "key": ""}]}],
    )
    result = prune_mlounge_channels(["#nixpad_child"], mercury_home=tmp_path)
    assert result == {"users": 1, "removed": 1}


def test_prune_mlounge_channels_is_idempotent(tmp_path) -> None:
    _write_user(
        tmp_path,
        "owner",
        [{"name": "nixpad", "channels": [{"name": "#gone", "muted": False, "key": ""}]}],
    )
    assert prune_mlounge_channels(["#gone"], mercury_home=tmp_path)["removed"] == 1
    # A second run finds nothing to do and must not rewrite the file.
    assert prune_mlounge_channels(["#gone"], mercury_home=tmp_path) == {"users": 0, "removed": 0}


def test_prune_mlounge_channels_never_raises_on_garbage(tmp_path) -> None:
    users = tmp_path / "observatory/lounge/home/users"
    users.mkdir(parents=True)
    (users / "broken.json").write_text("not json", encoding="utf-8")
    assert prune_mlounge_channels(["#gone"], mercury_home=tmp_path) == {"users": 0, "removed": 0}


# ---------------------------------------------------------------------------
# boot self-heal: the migration that fixes the install that already broke
# ---------------------------------------------------------------------------


def test_reap_orphan_rooms_heals_preexisting_zombies(tmp_path) -> None:
    """Recreates the live install's shape and proves it is reconciled.

    3 live depth-0 agents, plus the 7 dead rows / 7 closed channels / 2 stale
    sidebar entries that the destroy-convergence bug left behind.
    """
    state = ObservatoryState(tmp_path / "state.db")
    home = tmp_path

    for node_id, room in (
        ("gw", "#nixpad_gateway"),
        ("orch-mercurator", "#nixpad_mercurator"),
        ("orch-dionysian", "#nixpad_dionysian"),
    ):
        _seed(state, node_id, depth=0, room=room, parent=None)

    dead = [
        ("deleg_int/0", 1, "#nixpad_mercurator-integrator", "orch-mercurator"),
        ("deleg_int/0/sub-A", 2, "#nixpad_mercurator-integrator-a", "deleg_int/0"),
        ("deleg_int/0/sub-B", 2, "#nixpad_mercurator-integrator-b", "deleg_int/0"),
        ("deleg_survey", 1, "#nixpad_dionysian-survey", "orch-dionysian"),
        ("deleg_survey2", 1, "#nixpad_dionysian-survey-2", "orch-dionysian"),
    ]
    for node_id, depth, room, parent in dead:
        _seed(state, node_id, depth=depth, room=room, parent=parent, status="dead")

    zombie_rooms = [room for _, _, room, _ in dead]
    state.set_meta(
        CLOSED_ROOMS_META_KEY, json.dumps([c.lower() for c in zombie_rooms])
    )
    # The stuck restart-cleanup journal entry (channels already doomed).
    state.set_meta(
        "purge-journal",
        json.dumps([
            {
                "journal_id": "pj-restart-stuck",
                "node_id": "restart-cleanup",
                "status": "restart",
                "summary": None,
                "created_epoch": 0.0,
                "rows": [{"node_id": r[0], "parent_node_id": r[3], "depth": r[1]} for r in dead[:3]],
                "channels": [r[2] for r in dead[:3]],
            }
        ]),
    )
    _write_user(
        home,
        "owner",
        [
            {
                "name": "nixpad",
                "channels": [
                    {"name": "#nixpad_dionysian", "muted": False, "key": ""},
                    {"name": "#nixpad_dionysian-survey", "muted": False, "key": ""},
                    {"name": "#nixpad_dionysian-survey-2", "muted": False, "key": ""},
                    {"name": "#nixpad_gateway", "muted": False, "key": ""},
                    {"name": "#nixpad_mercurator", "muted": False, "key": ""},
                ],
            }
        ],
    )
    # History that must survive the reap.
    log_dir = home / "observatory/lounge/home/logs/owner/net"
    log_dir.mkdir(parents=True)
    transcript = log_dir / "#nixpad_dionysian-survey.log"
    transcript.write_text("past messages", encoding="utf-8")

    result = reap_orphan_rooms(state, mercury_home=home, live_channels=["#nixpad_gateway"])

    # Every dead row is gone; every live row survives.
    assert sorted(result["rows_purged"]) == sorted(r[0] for r in dead)
    assert sorted(r["node_id"] for r in state.get_live()) == [
        "gw",
        "orch-dionysian",
        "orch-mercurator",
    ]

    # Zombie rooms are queued for destruction and the sidebar is clean.
    assert sorted(result["channels_queued"]) == sorted(c.lower() for c in zombie_rooms)
    data = json.loads(
        (home / "observatory/lounge/home/users/owner.json").read_text(encoding="utf-8")
    )
    kept = {c["name"] for n in data["networks"] for c in n["channels"]}
    assert kept == {"#nixpad_dionysian", "#nixpad_gateway", "#nixpad_mercurator"}

    # History survives: the reap never touches transcript logs.
    assert transcript.read_text(encoding="utf-8") == "past messages"


def test_reap_orphan_rooms_is_idempotent_and_keeps_live_rooms(tmp_path) -> None:
    state = ObservatoryState(tmp_path / "state.db")
    _seed(state, "orch", depth=0, room="#srv_root", parent=None)
    _seed(state, "orch2", depth=0, room="#srv_root2", parent=None)

    first = reap_orphan_rooms(state, mercury_home=tmp_path)
    second = reap_orphan_rooms(state, mercury_home=tmp_path)

    assert first["rows_purged"] == [] and second["rows_purged"] == []
    assert first["channels_queued"] == [] and second["channels_queued"] == []
    assert sorted(r["node_id"] for r in state.get_live()) == ["orch", "orch2"]


def test_reap_marks_reaped_channels_closed(tmp_path) -> None:
    """A reaped room must never come back, even if its destroy never runs."""
    state = ObservatoryState(tmp_path / "state.db")
    _seed(state, "orch", depth=0, room="#srv_root", parent=None)
    _seed(state, "deleg_1", depth=1, room="#srv_root-child", parent="orch", status="dead")

    result = reap_orphan_rooms(state, mercury_home=tmp_path)

    assert result["channels_queued"] == ["#srv_root-child"]
    closed = set(json.loads(state.get_meta(CLOSED_ROOMS_META_KEY)))
    assert "#srv_root-child" in closed
    assert "#srv_root" not in closed


def test_reap_never_touches_a_live_room_pinned_by_the_caller(tmp_path) -> None:
    state = ObservatoryState(tmp_path / "state.db")
    _seed(state, "orch", depth=0, room="#srv_root", parent=None)
    state.set_meta(CLOSED_ROOMS_META_KEY, json.dumps(["#srv_pinned"]))

    result = reap_orphan_rooms(
        state, mercury_home=tmp_path, live_channels=["#srv_pinned"]
    )

    assert "#srv_pinned" not in result["channels_queued"]
