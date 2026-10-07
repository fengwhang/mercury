"""Death-time room reaping for the Observatory agent tree.

Why this exists
---------------
A dead subagent must vanish from BOTH MIRC and mLounge the moment its
lifetime ends, while its transcript/history survives. D8 timing:

- depth 1  -> gone at task completion
- depth >= 2 -> gone when its parent dies (cascades, deepest first)
- depth 0  -> never auto-gone; only ``/exit``

The old path held that cleanup hostage to ``OPER DESTROY``: ``finish_exit``
deleted the node rows only after the channel destroy converged. Any destroy
failure (no bot sink registered, ``oper/auth refused``, daemon down) skipped
``finish_exit``, so the row and the mLounge sidebar entry both survived
forever — the "zombie room" that eats screen space as an empty, useless
channel.

This module makes the VISIBLE cleanup independent of the channel destroy:

- :func:`prune_mlounge_channels` drops the channel from every mLounge user's
  saved channel list for this MIRC connection. This prevents stale rejoins;
  connected clients receive a self-PART when daemon destruction succeeds.
- :func:`reap_orphan_rooms` reconciles state on boot and on demand: every
  non-``live`` node row is purged, and every channel in ``closed-rooms`` with
  no live row is reported for destruction. Idempotent, and never touches
  history (logs / session transcripts are untouched).

The MIRC channel destroy is still attempted and retried, but a failed destroy
now costs a background retry rather than a permanent zombie.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _norm(channel: Any) -> str:
    return str(channel or "").strip().lower()


def mlounge_users_dir(mercury_home: str | Path | None = None) -> Path:
    """``$MERCURY_HOME/observatory/lounge/home/users`` via the ONE home
    resolution used by provisioning — never a second derivation."""
    from observatory.mlounge import MLoungePaths
    from observatory.provision import _mercury_home

    return MLoungePaths(_mercury_home(mercury_home)).home / "users"


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Replace ``path`` atomically, never leaving a half-written file.

    mLounge reads these user files live; a torn write would corrupt a user's
    whole network list. Explicit ``encoding='utf-8'`` (PLW1514): the system
    locale encoding on Windows silently corrupts non-ASCII network names.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def prune_mlounge_channels(
    channels: Iterable[str], mercury_home: str | Path | None = None
) -> dict[str, int]:
    """Remove ``channels`` from every mLounge user's saved channel lists.

    This removes stale reconnect entries: mLounge persists a user's channels in
    ``users/<name>.json`` and only drops one when it sees a self-PART for a
    channel it tries to JOIN. A room destroyed while the client is already
    joined therefore lingers in the sidebar forever. Editing the persisted
    list prevents rejoining on the next frontend restart. A connected browser
    is removed by the daemon's self-PART when destruction converges.

    Returns ``{"users": <files changed>, "removed": <entries dropped>}``.
    Never raises: a pruning failure must not block a room teardown.
    """
    doomed = {_norm(c) for c in channels if _norm(c).startswith("#")}
    if not doomed:
        return {"users": 0, "removed": 0}

    from observatory.provision import read_config, server_key

    cfg = read_config(mercury_home)
    if not cfg:
        return {"users": 0, "removed": 0}
    hosts = {"localhost", "127.0.0.1", "::1", str(server_key(cfg, "server_host", "")).lower()}
    hosts -= {"", "0.0.0.0", "::"}
    ports = {int(server_key(cfg, "server_port", 6670)), int(cfg.get("tls_port") or 6697)}
    users_dir = mlounge_users_dir(mercury_home)
    changed_users = 0
    removed = 0
    try:
        paths = sorted(users_dir.glob("*.json"))
    except Exception:
        logger.debug("room-reaper: mLounge users dir unreadable: %s", users_dir, exc_info=True)
        return {"users": 0, "removed": 0}

    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("room-reaper: skipping unreadable user file %s", path, exc_info=True)
            continue
        if not isinstance(data, dict):
            continue
        networks = data.get("networks")
        if not isinstance(networks, list):
            continue

        touched = False
        for net in networks:
            if not isinstance(net, dict):
                continue
            # Channel names are only unique within a network. Never remove a
            # same-named room on another machine in the user's tailnet.
            try:
                local = str(net.get("host") or "").lower() in hosts and int(net.get("port") or 6667) in ports
            except (ValueError, TypeError):
                local = False
            if not local:
                continue
            saved = net.get("channels")
            if not isinstance(saved, list):
                continue
            keep = [
                entry
                for entry in saved
                if not (isinstance(entry, dict) and _norm(entry.get("name")) in doomed)
            ]
            if len(keep) != len(saved):
                removed += len(saved) - len(keep)
                net["channels"] = keep
                touched = True

        if touched:
            try:
                _atomic_write_json(path, data)
                changed_users += 1
            except Exception:
                logger.warning("room-reaper: failed to prune %s", path, exc_info=True)

    if removed:
        logger.info(
            "room-reaper: pruned %d stale channel(s) from %d mLounge user(s)",
            removed,
            changed_users,
        )
    return {"users": changed_users, "removed": removed}


def closed_rooms(state: Any) -> set[str]:
    """Channels recorded as closed (the durable 'never come back' set)."""
    from observatory.state import CLOSED_ROOMS_META_KEY, StateError

    try:
        raw = state.get_meta(CLOSED_ROOMS_META_KEY)
    except StateError:
        return set()
    try:
        entries = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(entries, list):
        return set()
    return {_norm(e) for e in entries if _norm(e).startswith("#")}


def reap_orphan_rooms(
    state: Any,
    *,
    mercury_home: str | Path | None = None,
    live_channels: Iterable[str] = (),
) -> dict[str, Any]:
    """Reconcile dead weight out of the agent tree and the mLounge sidebar.

    Two independent leaks, both healed here:

    1. **Tombstoned families.** A node whose ``status`` is not ``live`` has
       already ended its lifetime. Its descendants expire with it, even if
       an older partial teardown left them marked live. Reap the complete
       subtree deepest-first so parent foreign keys cannot strand either
       generation. Transcripts live in the mLounge log store and Hermes
       session DB, neither touched here.
       A depth-1 task whose completion persisted before an interrupted exit
       also ended its lifetime; completed roots and deeper retained agents
       do not expire on their own.
    2. **Orphan channels.** Every channel in ``closed-rooms`` with no live
       node row must not exist. Returned for destruction — destroying a gone
       channel is success, so the caller's retry is idempotent.

    ``live_channels`` lets a caller pin channels that must survive even with
    no row (e.g. the gateway room). Never raises.

    Returns ``{"rows_purged": [...], "channels_queued": [...], "pruned": {...}}``.
    """
    result: dict[str, Any] = {
        "rows_purged": [],
        "channels_queued": [],
        "pruned": {"users": 0, "removed": 0},
    }

    pinned = {_norm(c) for c in live_channels if _norm(c).startswith("#")}
    from observatory.provision import read_config
    from observatory.rooms import gateway_channel

    cfg = read_config(mercury_home) or {}
    pinned.add(gateway_channel(str(cfg.get("server_name") or "mercury")).lower())

    expired: dict[str, dict[str, Any]] = {}
    # A live node row is not task liveness. Only the delegation ledger's
    # exact child identity and recorded terminal outcome justify expiry.
    terminal_tasks: dict[str, str] = {}
    try:
        from tools.async_delegation import terminal_child_outcomes
        terminal_tasks = terminal_child_outcomes()
    except Exception:
        logger.debug("room-reaper: terminal task evidence unavailable", exc_info=True)
    try:
        from observatory.state import purge_on_death

        with state.locked() as db:
            live_rows = list(state.get_live())
            expired_roots = list(db.execute(
                "SELECT node_id, room_id FROM nodes WHERE status != 'live' ORDER BY depth DESC"
            ).fetchall())
            expired_roots.extend(
                row for row in live_rows
                if purge_on_death(int(row["depth"]))
                and (row.get("extra", {}).get("task_state") == "completed"
                     or row["node_id"] in terminal_tasks)
            )
            for raw in expired_roots:
                if raw["node_id"] in expired or _norm(raw["room_id"]) in pinned:
                    continue
                for row in state.get_subtree(raw["node_id"]):
                    if _norm(row.get("room_id")) not in pinned:
                        expired[row["node_id"]] = row
            # Hold admission closed through the snapshot and deletion. Live
            # descendants of an expired parent share that parent's lifetime;
            # leaving them behind also prevents its deletion via the FK.
            with db:
                for row in sorted(expired.values(), key=lambda row: row["depth"], reverse=True):
                    db.execute("DELETE FROM nodes WHERE node_id = ?", (row["node_id"],))
    except Exception:
        logger.warning("room-reaper: family purge failed", exc_info=True)
        return result

    result["rows_purged"] = list(expired)
    live_channels_now = {
        _norm(r.get("room_id"))
        for r in live_rows
        if r["node_id"] not in expired and _norm(r.get("room_id")).startswith("#")
    } | pinned
    doomed = {
        _norm(row.get("room_id"))
        for row in expired.values()
        if _norm(row.get("room_id")).startswith("#")
    }

    try:
        for channel in closed_rooms(state):
            if channel not in live_channels_now:
                doomed.add(channel)
    except Exception:
        logger.debug("room-reaper: closed-room scan failed", exc_info=True)

    doomed -= live_channels_now
    if doomed:
        result["channels_queued"] = sorted(doomed)
        # Record them closed first: if the destroy never converges (or the
        # process dies mid-reap) the daemon still refuses to re-create them,
        # so a reaped room can never come back as a zombie.
        try:
            from observatory.state import CLOSED_ROOMS_META_KEY

            with state.locked() as db:
                prior = db.execute(
                    "SELECT value FROM meta WHERE key = ?", (CLOSED_ROOMS_META_KEY,)
                ).fetchone()
                closed = set(json.loads(prior[0])) if prior else set()
                closed |= doomed
                closed -= live_channels_now
                with db:
                    db.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (CLOSED_ROOMS_META_KEY, json.dumps(sorted(closed))),
                    )
                    from observatory.spawn import ExitRecord, PURGE_JOURNAL_KEY, read_purge_journal

                    journal = read_purge_journal(state)
                    queued = {_norm(c) for entry in journal for c in entry.get("channels", [])}
                    missing = sorted(doomed - queued)
                    if missing:
                        record = ExitRecord(
                            journal_id=f"pj-{uuid.uuid4().hex[:8]}", node_id="room-reaper",
                            status="completed", summary=None, created_epoch=time.time(),
                            rows=[], channels=missing,
                        )
                        journal.append(record.to_entry())
                        db.execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (PURGE_JOURNAL_KEY, json.dumps(journal)),
                        )
        except Exception:
            logger.debug("room-reaper: closed-room update failed", exc_info=True)
        try:
            result["pruned"] = prune_mlounge_channels(doomed, mercury_home)
        except Exception:
            logger.warning("room-reaper: mLounge prune failed", exc_info=True)

    if result["rows_purged"] or result["channels_queued"]:
        logger.info(
            "room-reaper: purged %d dead row(s), %d orphan channel(s), mLounge %s",
            len(result["rows_purged"]),
            len(result["channels_queued"]),
            result["pruned"],
        )
    return result
