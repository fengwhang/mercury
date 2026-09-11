"""Agent-tree SQLite store for the Matrix Observatory sidecar (spec §2
component 2, "Discovery"; D8/D17/D18 semantics).

One row per observed agent session (any depth, any engine). The sidecar is
the ONLY writer; readers are the renderer and the respawn pass (D18).

Design laws tested here:
- **Depth is stored at insert** and never recomputed (D8: the depth class
  fixes an agent's deletion timing for its whole life — a parent's row may
  later change, but a child's semantics were frozen at spawn).
- **0-agents are never auto-deleted** (D8: their history survives until
  /exit or session reset; restart is not death — D18). ``mark_dead`` only
  tombstones; deletion is always an explicit ``mark_deleted_and_purge``.
- **Slug collisions count LIVE agents only** (D17): dead/purged rows are
  invisible to ``find_live_by_slug``, so a new agent reuses an inert
  predecessor's MXID-and-nothing-else.

Schema is WAL-mode SQLite with a ``meta`` KV table carrying
``schema_version`` for forward migrations.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: Bump when the schema below changes; add a migration step in
#: :meth:`ObservatoryState._migrate`. Rows written by older sidecars carry
#: their version in ``meta.schema_version``.
SCHEMA_VERSION = 1

STATE_DB_FILENAME = "state.db"

#: Valid values for ``nodes.engine`` (spec: sidecar mirrors both engines).
ENGINES = ("hermes", "omp")

#: Valid values for ``nodes.status``.
STATUSES = ("live", "dead", "deleted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id         TEXT PRIMARY KEY,
    parent_node_id  TEXT REFERENCES nodes(node_id),
    engine          TEXT NOT NULL CHECK (engine IN ('hermes', 'omp')),
    depth           INTEGER NOT NULL,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    mxid            TEXT NOT NULL,
    session_ref     TEXT NOT NULL,
    delegation_id   TEXT,
    space_id        TEXT,
    room_id         TEXT,
    status          TEXT NOT NULL DEFAULT 'live'
                    CHECK (status IN ('live', 'dead', 'deleted')),
    created_epoch   REAL NOT NULL,
    died_epoch      REAL,
    extra_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StateError(KeyError):
    """Missing/unknown node or meta key (fail-hard, same law as provision)."""


def default_state_db_path(mercury_home: str | Path | None = None) -> Path:
    """``$MERCURY_HOME/observatory/state.db`` using the SAME home resolution
    and directory layout as provisioning (config_gen.ObservatoryPaths +
    provision._mercury_home) — never a second derivation."""
    from observatory.config_gen import ObservatoryPaths
    from observatory.provision import _mercury_home

    return ObservatoryPaths(_mercury_home(mercury_home)).root / STATE_DB_FILENAME


def purge_on_death(depth: int) -> bool:
    """D8 deletion timing, as a pure predicate on the stored depth.

    - depth == 1: room/space destroyed the instant it dies (summary lands
      in the PARENT's room) → immediate purge.
    - depth >= 2: survives until its parent dies (parent lifetime is the
      reading grace, no timer) → no purge at death time.
    - depth == 0: top-level orchestrator / gateway agent — history survives
      until /exit or session reset; NEVER auto-deleted.
    """
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")
    return depth == 1


class ObservatoryState:
    """SQLite-backed agent tree. One connection, sync I/O — thread-safe
    via an internal ``threading.RLock`` (``check_same_thread=False``).

    The ``/spawn`` + ``/spawnomp`` Matrix pass-through runs INSIDE the
    gateway event loop while ``try_boot_sidecar`` opens the same state on
    a daemon boot thread — sqlite objects are created on one thread and
    used on another, so every ``_db`` access takes the lock. Call sites
    that must not block the loop wrap calls in ``asyncio.to_thread``
    (the repo's ASYNC-lint pattern). External same-package ``state._db``
    users MUST hold ``state._lock`` (or use :meth:`locked`)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL: the renderer/respawn pass reads while discovery writes, and a
        # crashed sidecar must never leave a torn journal (D18 restart story).
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)
            self._migrate_locked()
            self._db.commit()

    # --- lifecycle ------------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Hold the state lock and yield the raw connection.

        Same-package direct ``state._db`` users (spawn/e2ee/cron_rooms)
        MUST wrap their transaction blocks in this — the connection is
        shared across the boot thread and the gateway event loop."""
        with self._lock:
            yield self._db

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "ObservatoryState":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        """Version-gate the schema. Unknown FUTURE versions fail hard (an
        older sidecar must not write into a newer store); older versions
        walk forward through explicit steps — none yet at v1."""
        with self._lock:
            self._migrate_locked()

    def _migrate_locked(self) -> None:
        """_migrate without locking (caller holds ``_lock``)."""
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        version = int(row["value"])
        if version > SCHEMA_VERSION:
            raise StateError(
                f"state.db schema v{version} is newer than this sidecar "
                f"understands (v{SCHEMA_VERSION}); upgrade mercury first"
            )
        # v1 is the floor: nothing to walk forward from yet.

    # --- meta KV --------------------------------------------------------------

    def get_meta(self, key: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise StateError(f"no meta key {key!r}")
            return row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    def delete_meta(self, key: str) -> bool:
        """Drop one meta entry (poisoned-room reconverge); True when one
        existed. Never raises for absent keys."""
        with self._lock:
            try:
                cur = self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
                self._db.commit()
                return (cur.rowcount or 0) > 0
            except Exception:  # noqa: BLE001 — best-effort delete
                return False

    # --- nodes ----------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["extra"] = json.loads(d.pop("extra_json") or "{}")
        return d

    def get(self, node_id: str) -> dict[str, Any]:
        """Full row as a dict (``extra_json`` parsed into ``extra``)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"no node {node_id!r}")
            return self._row_to_dict(row)

    def next_depth(self, parent_node_id: str | None) -> int:
        """D8 depth for a new node: roots (gateway agent, spawned
        orchestrators) are 0; every child is exactly parent.depth + 1,
        read at INSERT time and never recomputed afterwards."""
        if parent_node_id is None:
            return 0
        return self.get(parent_node_id)["depth"] + 1

    def add_node(
        self,
        node_id: str,
        *,
        engine: str,
        name: str,
        slug: str,
        mxid: str,
        session_ref: str,
        parent_node_id: str | None = None,
        delegation_id: str | None = None,
        depth: int | None = None,
        extra: dict[str, Any] | None = None,
        created_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Insert a LIVE node. ``depth`` defaults to ``next_depth(parent)``;
        passing it explicitly is allowed only for callers that already know
        the tree position (the value is still frozen verbatim at insert)."""
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
        with self._lock:
            if depth is None:
                depth = self.next_depth(parent_node_id)
            self._db.execute(
                "INSERT INTO nodes (node_id, parent_node_id, engine, depth, name,"
                " slug, mxid, session_ref, delegation_id, status, created_epoch,"
                " extra_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?)",
                (
                    node_id,
                    parent_node_id,
                    engine,
                    depth,
                    name,
                    slug,
                    mxid,
                    session_ref,
                    delegation_id,
                    created_epoch if created_epoch is not None else time.time(),
                    json.dumps(extra or {}, ensure_ascii=False),
                ),
            )
            self._db.commit()
        return self.get(node_id)

    def mark_dead(self, node_id: str, *, died_epoch: float | None = None) -> dict[str, Any]:
        """Tombstone a settled agent. NEVER deletes anything — 0-agents keep
        their history (D8) and >=2-agents keep their reading grace until the
        parent dies; matrix purge + row removal are always the caller's
        explicit ``mark_deleted_and_purge``."""
        with self._lock:
            row = self.get(node_id)
            if row["status"] == "deleted":
                raise StateError(f"node {node_id!r} is already deleted")
            self._db.execute(
                "UPDATE nodes SET status = 'dead', died_epoch = ? WHERE node_id = ?",
                (died_epoch if died_epoch is not None else time.time(), node_id),
            )
            self._db.commit()
        return self.get(node_id)

    def mark_deleted_and_purge(self, node_id: str) -> dict[str, Any]:
        """Remove a node's row after (or while) purging its matrix artifacts.
        Returns the pre-delete row so the caller can drive the Tuwunel
        delete (needs space_id/room_id) and log the annihilation. The row is
        deleted — per D17 a successor with the same name inherits the MXID
        and NOTHING else, so no tombstone may survive to leak state."""
        with self._lock:
            row = self.get(node_id)
            with self._db:
                # Transient 'deleted' status: same transaction, observable only
                # to CHECK-constraint readers; keeps the enum honest.
                self._db.execute(
                    "UPDATE nodes SET status = 'deleted' WHERE node_id = ?", (node_id,)
                )
                self._db.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
        return row

    def get_live(self) -> list[dict[str, Any]]:
        """All live nodes, deterministic order (depth, created_epoch, node_id)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM nodes WHERE status = 'live'"
                " ORDER BY depth, created_epoch, node_id"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def find_live_by_slug(self, slug: str) -> list[dict[str, Any]]:
        """D17: collision detection counts LIVE agents only — dead and
        purged predecessors are invisible here, so their MXID is inherited
        freely (with zero context inheritance)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM nodes WHERE slug = ? AND status = 'live'"
                " ORDER BY created_epoch, node_id",
                (slug,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def children_of(self, node_id: str) -> list[dict[str, Any]]:
        """Direct children (any status), spawn order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM nodes WHERE parent_node_id = ?"
                " ORDER BY created_epoch, node_id",
                (node_id,),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def get_subtree(self, node_id: str) -> list[dict[str, Any]]:
        """The node plus every descendant (any status), breadth-first in
        spawn order — the set a parent-death cascade (D8) must consider."""
        with self._lock:
            out = [self.get(node_id)]
            frontier = [node_id]
            while frontier:
                placeholders = ",".join("?" * len(frontier))
                rows = self._db.execute(
                    f"SELECT * FROM nodes WHERE parent_node_id IN ({placeholders})"
                    " ORDER BY created_epoch, node_id",
                    frontier,
                ).fetchall()
                frontier = [r["node_id"] for r in rows]
                out.extend(self._row_to_dict(r) for r in rows)
            return out

    # --- matrix id upsert -----------------------------------------------------

    def set_space_id(self, node_id: str, space_id: str) -> None:
        """Record the node's (sub)space id once the renderer has created it;
        idempotent overwrite for re-ensure passes (D18 respawn)."""
        self._set_matrix_id(node_id, "space_id", space_id)

    def set_room_id(self, node_id: str, room_id: str) -> None:
        self._set_matrix_id(node_id, "room_id", room_id)

    def _set_matrix_id(self, node_id: str, column: str, value: str) -> None:
        assert column in ("space_id", "room_id")
        with self._lock:
            self.get(node_id)  # fail hard on unknown node
            self._db.execute(
                f"UPDATE nodes SET {column} = ? WHERE node_id = ?", (value, node_id)
            )
            self._db.commit()
