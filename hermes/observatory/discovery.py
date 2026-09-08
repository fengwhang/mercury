"""M3b (matrix observatory §7): discovery — the live agent-tree feed.

Two sources, one NodeStream (an async iterator of ``NodeEvent`` add/death):

1. **Poll** — the durable ``async_delegations`` table in the hermes
   ``state.db`` (``tools/async_delegation.py``). Every row in a live state
   (``running`` / ``finalizing``) expands to one node per task, keyed
   ``(delegation_id, task_index)``; goals and names come from the row's
   ``task_json``, terminal summaries from ``result_json``. Interval is
   configurable. A node dies when its row reaches a terminal state
   (completed / failed / stalled / unknown / ...) or vanishes entirely
   (deleted while live — e.g. dispatch rollback after a crash).

2. **Hook push** — ``on_subagent_start`` / ``on_subagent_stop`` methods
   carrying the exact payload field names the hermes lifecycle hooks fire
   with (``tools/delegate_tool.py`` ``subagent_start`` / ``subagent_stop``:
   parent_session_id, parent_turn_id, parent_subagent_id,
   child_session_id, child_subagent_id, child_role, child_goal; stop adds
   child_summary, child_status, tool_call_history, duration_ms). Hooks are
   the instant channel for SYNC delegations (which never touch the table)
   and shave up to one poll interval off async ones.

Dedupe (poll-vs-hook) is BY DELEGATION ID on the node key
``(delegation_id, task_index)``:

* A hook whose payload names a delegation id (explicit ``delegation_id``
  field, or an omp-style ``child_subagent_id`` of the form
  ``<delegation_id>/<task_index>``) maps straight onto the poll's key —
  one add, one death, whichever source sees it first.
* A hook with no derivable id tries to CLAIM an already-polled live node
  with the same parent session + task index (exactly-one match only).
* A hook that arrives before its delegation's first poll tick parks for a
  short claim grace (default: one poll interval). If the row shows up in
  time, the parked start emits a single add under the REAL delegation key;
  otherwise it expires to a synthetic key ``hook:<child_session_id>`` so no
  push event is ever dropped.
* A synthetic node that is later matched by its delegation's row is
  re-keyed: the stream sees ``death(status="reattributed")`` for the
  synthetic key followed by the real-keyed add — consumers never observe
  two live nodes for one child.
* Ambiguity never guesses: when two live table nodes (or two parked
  starts) match a (parent session, task_index) pair, no claim is made and
  the hook keeps its own key.

Ordering races the engine explicitly survives (each has a test):

* hook stop BEFORE hook start for the same child — the stop is buffered
  and applied the moment the node appears (add then immediate death);
* poll death then hook stop (and vice versa) — exactly one death;
* hook add after poll add — enrich, never duplicate.

Module scope note (M3b): the ``NodeEvent`` here is the discovery-side
vocabulary keyed by delegation; the omp-side grandchild vocabulary lives
in ``observatory.omp_feed``. The M3a tree/renderer module unifies both
when it consumes them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Row states the reapers / restart-recovery treat as active work — same set
# async_delegation.restore_running_delegations keeps alive.
LIVE_STATES = frozenset({"running", "finalizing"})

# Fallback node name per M0's ``normalize_delegation_names`` convention
# (``task-<i>``, 0-based).
def _fallback_name(index: int) -> str:
    return f"task-{index}"

# ``sa-<task_index>-<hex>`` — hermes child ids (delegate_tool.py:1792).
_SA_ID_RE = re.compile(r"^sa-(\d+)-[0-9a-f]+$")
# ``<delegation_id>/<task_index>`` — omp live-child registry ids
# (omp_delegation.py:143-146); delegation ids are ``deleg_<hex>`` or
# ``local-<hex>``.
_OMP_CHILD_ID_RE = re.compile(r"^(deleg_[0-9a-f]+|local-[0-9a-f]+)/(\d+)$")

# Buffered out-of-order stops are capped so a flood of unmatched payloads
# can't grow memory unbounded (they are advisory, not authoritative).
_MAX_PENDING_STOPS = 256


@dataclass(frozen=True)
class NodeEvent:
    """One add/death transition of a discovered agent node."""

    kind: str  # "add" | "death"
    delegation_id: str
    task_index: int
    parent_session: str
    name: str
    goal: str
    status: str  # add: "running"; death: terminal status (or "reattributed")
    source: str  # "poll" | "hook"
    seq: int  # engine-wide monotonic emission order
    child_session_id: Optional[str] = None
    child_subagent_id: Optional[str] = None
    child_role: Optional[str] = None
    parent_subagent_id: Optional[str] = None
    parent_turn_id: Optional[str] = None
    # death only: the child's final summary when known.
    summary: Optional[str] = None


@dataclass
class _Node:
    """Internal live-node record (mutated under the engine lock)."""

    key: Tuple[str, int]
    parent_session: str
    name: str
    goal: str
    # True once the async_delegations table owns this node's lifecycle
    # (poll-added or row-claimed) — only table nodes are reaped when their
    # row vanishes. Hook-synthetic nodes die via hooks only.
    from_table: bool = False
    child_session_id: Optional[str] = None
    child_subagent_id: Optional[str] = None
    child_role: Optional[str] = None
    parent_subagent_id: Optional[str] = None
    parent_turn_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _parse_task_index(payload: Mapping[str, Any]) -> int:
    raw = payload.get("task_index")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    sid = str(payload.get("child_subagent_id") or "")
    m = _SA_ID_RE.match(sid)
    if m:
        return int(m.group(1))
    m = _OMP_CHILD_ID_RE.match(sid)
    if m:
        return int(m.group(2))
    return 0


def _derive_delegation_id(payload: Mapping[str, Any]) -> Optional[str]:
    """Delegation id from the payload itself, when derivable."""
    explicit = str(payload.get("delegation_id") or "").strip()
    if explicit:
        return explicit
    sid = str(payload.get("child_subagent_id") or "")
    m = _OMP_CHILD_ID_RE.match(sid)
    if m:
        return m.group(1)
    return None


class DiscoveryEngine:
    """Merge the async_delegations poll and lifecycle hooks into one stream.

    Lifecycle::

        engine = DiscoveryEngine(state_db_path, poll_interval=2.0)
        await engine.start()
        async for event in engine.stream():   # NodeStream
            ...
        await engine.stop()

    ``on_subagent_start`` / ``on_subagent_stop`` are safe to call from any
    thread (plugin hooks fire on worker threads); events are bridged onto
    the loop that ``start()`` captured.
    """

    def __init__(
        self,
        db_path: "str | Path",
        *,
        poll_interval: float = 2.0,
        claim_grace: Optional[float] = None,
        clock=time.monotonic,
    ) -> None:
        self._db_path = str(db_path)
        self._poll_interval = max(0.05, float(poll_interval))
        # How long an unattributed hook start parks hoping its delegation
        # row shows up. Default: one poll interval (a row inserted before
        # dispatch returns is visible at the next tick).
        self._claim_grace = (
            self._poll_interval if claim_grace is None else max(0.0, float(claim_grace))
        )
        self._clock = clock

        self._lock = threading.Lock()
        self._nodes: Dict[Tuple[str, int], _Node] = {}
        self._seq = 0
        self._queue: "asyncio.Queue[NodeEvent]" = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stopped = False
        # Hooks that fired before start() — replayed in order once the
        # loop exists, so early spawns are never lost.
        self._early: List[Tuple[str, Mapping[str, Any]]] = []
        # Unattributed hook starts parking for a row to claim them.
        self._pending_starts: List[Tuple[Mapping[str, Any], float]] = []
        # Stops that arrived before their node did (applied on add).
        self._pending_stops: Dict[str, Mapping[str, Any]] = {}
        self._pending_stop_order: List[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Capture the loop, replay early hooks, launch the poll task."""
        if self._poll_task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        early, self._early = self._early, []
        for kind, payload in early:
            self._handle_hook(kind, payload)
        self._poll_task = self._loop.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Cancel polling and wake the single stream consumer."""
        self._stopped = True
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._emit_barrier()

    async def _emit_barrier(self) -> None:
        """Flush queued events then enqueue the end-of-stream sentinel."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # Give call_soon_threadsafe callbacks scheduled before stop() a
        # chance to land on the loop before the sentinel.
        await asyncio.sleep(0)
        self._queue.put_nowait(None)  # type: ignore[arg-type]

    def stream(self) -> AsyncIterator[NodeEvent]:
        """The NodeStream: async iterator of add/death NodeEvents."""
        return self._stream()

    async def _stream(self) -> AsyncIterator[NodeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    # ------------------------------------------------------------------
    # Snapshot (introspection for the sidecar / tests)
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Live nodes keyed ``"<delegation_id>/<task_index>"``."""
        with self._lock:
            return {
                f"{k[0]}/{k[1]}": {
                    "parent_session": n.parent_session,
                    "name": n.name,
                    "goal": n.goal,
                    "from_table": n.from_table,
                    "child_session_id": n.child_session_id,
                    "child_subagent_id": n.child_subagent_id,
                    "child_role": n.child_role,
                    "parent_subagent_id": n.parent_subagent_id,
                    **n.extra,
                }
                for k, n in sorted(self._nodes.items())
            }

    # ------------------------------------------------------------------
    # Hook push interface (field names per delegate_tool lifecycle hooks)
    # ------------------------------------------------------------------

    def on_subagent_start(self, payload: Mapping[str, Any]) -> None:
        """``subagent_start`` hook payload (any thread)."""
        self._submit_hook("start", payload)

    def on_subagent_stop(self, payload: Mapping[str, Any]) -> None:
        """``subagent_stop`` hook payload (any thread)."""
        self._submit_hook("stop", payload)

    def _submit_hook(self, kind: str, payload: Mapping[str, Any]) -> None:
        payload = dict(payload)
        loop = self._loop
        if loop is None or loop.is_closed():
            # Before start(): buffer (order-preserving).
            with self._lock:
                self._early.append((kind, payload))
            return
        try:
            loop.call_soon_threadsafe(self._handle_hook, kind, payload)
        except RuntimeError:
            # Loop torn down concurrently with stop().
            with self._lock:
                self._early.append((kind, payload))

    # ------------------------------------------------------------------
    # Event emission (loop thread only)
    # ------------------------------------------------------------------

    def _emit(self, event: NodeEvent) -> None:
        with self._lock:
            self._seq += 1
            event = replace(event, seq=self._seq)
        self._queue.put_nowait(event)

    # ------------------------------------------------------------------
    # Hook handling
    # ------------------------------------------------------------------

    def _handle_hook(self, kind: str, payload: Mapping[str, Any]) -> None:
        try:
            if kind == "start":
                self._handle_start(payload)
            else:
                self._handle_stop(payload)
        except Exception:  # noqa: BLE001 — a bad payload must not kill the feed
            logger.exception("observatory discovery: hook %s failed", kind)

    def _handle_start(self, payload: Mapping[str, Any]) -> None:
        index = _parse_task_index(payload)
        parent = str(payload.get("parent_session_id") or "")
        delegation_id = _derive_delegation_id(payload)

        with self._lock:
            # 1. Direct key hit: enrich, never duplicate.
            if delegation_id is not None:
                key = (delegation_id, index)
                node = self._nodes.get(key)
                if node is not None:
                    self._attach_child(node, payload)
                    self._apply_pending_stop(key)
                    return
            # 2. Claim an existing table node (exactly-one match).
            if delegation_id is None:
                candidates = [
                    (k, n)
                    for k, n in self._nodes.items()
                    if n.from_table
                    and n.parent_session == parent
                    and k[1] == index
                    and n.child_session_id is None
                ]
                if len(candidates) == 1:
                    key, node = candidates[0]
                    self._attach_child(node, payload)
                    self._apply_pending_stop(key)
                    # The poll already emitted this node's add; the claim
                    # is enrichment only (child ids, goal, linkage).
                    return
                # 3. Park for a row within the claim grace.
                deadline = self._clock() + self._claim_grace
                self._pending_starts.append((payload, deadline))
                return

        # delegation_id known and node absent → fresh hook node.
        self._add_hook_node(delegation_id, index, parent, payload)

    def _handle_stop(self, payload: Mapping[str, Any]) -> None:
        child_session = str(payload.get("child_session_id") or "")
        status = str(payload.get("child_status") or "completed")
        summary = payload.get("child_summary")
        summary = str(summary) if summary is not None else None

        with self._lock:
            # Resolve by child session first (hook-created or enriched).
            if child_session:
                keys = [
                    k
                    for k, n in self._nodes.items()
                    if n.child_session_id == child_session
                ]
                if len(keys) == 1:
                    self._kill_locked(keys[0], status, summary, source="hook")
                    return
            # Fall back to the derivable key.
            delegation_id = _derive_delegation_id(payload)
            if delegation_id is not None:
                key = (delegation_id, _parse_task_index(payload))
                if key in self._nodes:
                    self._kill_locked(key, status, summary, source="hook")
                    return
            # Node not seen yet: buffer so a late start applies it
            # immediately (ordering race: stop before start).
            if child_session and child_session not in self._pending_stops:
                if len(self._pending_stop_order) >= _MAX_PENDING_STOPS:
                    evicted = self._pending_stop_order.pop(0)
                    self._pending_stops.pop(evicted, None)
                self._pending_stops[child_session] = payload
                self._pending_stop_order.append(child_session)

    # -- helpers under lock -------------------------------------------

    def _attach_child(self, node: _Node, payload: Mapping[str, Any]) -> None:
        node.child_session_id = str(
            payload.get("child_session_id") or ""
        ) or node.child_session_id
        node.child_subagent_id = str(
            payload.get("child_subagent_id") or ""
        ) or node.child_subagent_id
        node.child_role = payload.get("child_role") or node.child_role
        node.parent_subagent_id = (
            payload.get("parent_subagent_id") or node.parent_subagent_id
        )
        node.parent_turn_id = payload.get("parent_turn_id") or node.parent_turn_id
        goal = str(payload.get("child_goal") or "")
        if goal and not node.goal:
            node.goal = goal

    def _apply_pending_stop(self, key: Tuple[str, int]) -> None:
        node = self._nodes.get(key)
        session = node.child_session_id if node else None
        if not session or session not in self._pending_stops:
            return
        payload = self._pending_stops.pop(session)
        self._pending_stop_order.remove(session)
        self._kill_locked(
            key,
            str(payload.get("child_status") or "completed"),
            payload.get("child_summary"),
            source="hook",
        )

    def _kill_locked(
        self,
        key: Tuple[str, int],
        status: str,
        summary: Optional[str],
        *,
        source: str,
    ) -> None:
        node = self._nodes.pop(key, None)
        if node is None:
            return  # already dead — exactly-once death
        self._emit_locked(
            replace(
                self._node_event("death", node, source=source),
                status=status,
                summary=summary,
            )
        )

    def _node_event(self, kind: str, node: _Node, *, source: str) -> NodeEvent:
        return NodeEvent(
            kind=kind,
            delegation_id=node.key[0],
            task_index=node.key[1],
            parent_session=node.parent_session,
            name=node.name,
            goal=node.goal,
            status="running" if kind == "add" else "completed",
            source=source,
            seq=0,
            child_session_id=node.child_session_id,
            child_subagent_id=node.child_subagent_id,
            child_role=node.child_role,
            parent_subagent_id=node.parent_subagent_id,
            parent_turn_id=node.parent_turn_id,
        )

    def _emit_locked(self, event: NodeEvent) -> None:
        # seq must be assigned under the same lock ordering as _emit; the
        # queue put is loop-thread-safe by construction (all hook handling
        # runs on the loop via call_soon_threadsafe).
        self._seq += 1
        self._queue.put_nowait(replace(event, seq=self._seq))

    def _add_hook_node(
        self,
        delegation_id: Optional[str],
        index: int,
        parent: str,
        payload: Mapping[str, Any],
    ) -> None:
        synthetic = delegation_id is None
        if synthetic:
            delegation_id = "hook:{}".format(
                payload.get("child_session_id")
                or payload.get("child_subagent_id")
                or f"idx{index}"
            )
        key = (delegation_id, index)
        with self._lock:
            if key in self._nodes:
                # Lost a race with a concurrent add of the same key.
                self._attach_child(self._nodes[key], payload)
                self._apply_pending_stop(key)
                return
            node = _Node(
                key=key,
                parent_session=parent,
                name=str(payload.get("child_name") or "") or _fallback_name(index),
                goal=str(payload.get("child_goal") or ""),
                from_table=False,
            )
            self._attach_child(node, payload)
            self._nodes[key] = node
            self._emit_locked(self._node_event("add", node, source="hook"))
            self._apply_pending_stop(key)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stopped:
            try:
                self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad tick must not kill polling
                logger.exception("observatory discovery: poll tick failed")
            await asyncio.sleep(self._poll_interval)

    def _tick(self) -> None:
        rows = self._read_rows()
        with self._lock:
            # Claim first: a parked start whose row arrived in THIS tick
            # must win over its own grace expiry (matters when grace=0).
            self._claim_pending_starts(rows)
            self._expire_pending_starts()
            self._apply_rows(rows)

    # -- sqlite read ----------------------------------------------------

    def _read_rows(self) -> Dict[str, Dict[str, Any]]:
        """All durable rows keyed by delegation id (empty on any db error)."""
        try:
            uri = f"file:{self._db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error:
            return {}
        try:
            rows = conn.execute(
                """SELECT delegation_id, origin_session, parent_session_id,
                          state, task_json, result_json
                   FROM async_delegations"""
            ).fetchall()
        except sqlite3.Error:
            # Table missing (fresh home) or transiently locked — an empty
            # snapshot is safe: vanish-reaping only fires for ids that WERE
            # present, and a locked read must not look like mass death.
            return {}
        finally:
            conn.close()
        out: Dict[str, Dict[str, Any]] = {}
        for (delegation_id, origin, parent, state, task_json, result_json) in rows:
            try:
                task = json.loads(task_json) if task_json else {}
            except (TypeError, ValueError):
                task = {}
            if not isinstance(task, dict):
                task = {}
            out[str(delegation_id)] = {
                "origin_session": origin or "",
                "parent_session_id": parent or "",
                "state": state or "",
                "task": task,
                "result": _safe_json(result_json),
            }
        return out

    # -- row application (under lock) -----------------------------------

    def _apply_rows(self, rows: Dict[str, Dict[str, Any]]) -> None:
        for delegation_id, row in rows.items():
            live = row["state"] in LIVE_STATES
            tasks = _expand_tasks(row["task"])
            for index, (name, goal) in enumerate(tasks):
                key = (delegation_id, index)
                node = self._nodes.get(key)
                if live:
                    if node is None:
                        # Re-key race: exactly one live synthetic hook node
                        # for this (parent, index) adopts the real key.
                        adopt = self._find_synthetic_adoption(row, index)
                        if adopt is not None:
                            self._rekey_locked(adopt, key, name, goal)
                            continue
                        node = _Node(
                            key=key,
                            parent_session=_row_parent(row),
                            name=name,
                            goal=goal,
                            from_table=True,
                        )
                        self._nodes[key] = node
                        self._emit_locked(
                            self._node_event("add", node, source="poll")
                        )
                    else:
                        node.from_table = True
                else:
                    # Terminal row: kill if we still hold it alive.
                    if node is not None:
                        summary, status, name = _terminal_result(row, index)
                        if name and node.name == _fallback_name(index):
                            # The durable dispatch record carries goals
                            # only; the per-task NAME rides result_json.
                            node.name = name
                        self._kill_locked(
                            key, status, summary, source="poll"
                        )
        # Rows that vanished entirely while we believed them live.
        for key in list(self._nodes):
            if key[0] in rows:
                continue
            node = self._nodes.get(key)
            if node is not None and node.from_table:
                self._kill_locked(key, "unknown", None, source="poll")

    def _find_synthetic_adoption(
        self, row: Mapping[str, Any], index: int
    ) -> Optional[Tuple[str, int]]:
        parent = _row_parent(row)
        candidates = [
            k
            for k, n in self._nodes.items()
            if not n.from_table
            and k[0].startswith("hook:")
            and k[1] == index
            and n.parent_session == parent
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _rekey_locked(
        self,
        old: Tuple[str, int],
        new: Tuple[str, int],
        name: str,
        goal: str,
    ) -> None:
        """A synthetic hook node's delegation row finally appeared."""
        node = self._nodes.pop(old, None)
        if node is None:
            return
        self._emit_locked(
            replace(
                self._node_event("death", node, source="poll"),
                status="reattributed",
            )
        )
        node.key = new
        node.from_table = True
        if not node.name or node.name == _fallback_name(old[1]):
            node.name = name
        if not node.goal:
            node.goal = goal
        self._nodes[new] = node
        self._emit_locked(self._node_event("add", node, source="poll"))

    # -- pending hook starts (under lock) -------------------------------

    def _expire_pending_starts(self) -> None:
        if not self._pending_starts:
            return
        now = self._clock()
        remaining = []
        for payload, deadline in self._pending_starts:
            if now < deadline:
                remaining.append((payload, deadline))
                continue
            # Expired: surface under a synthetic key so the push event is
            # never dropped. (Runs under lock; _add_hook_node re-locks.)
            index = _parse_task_index(payload)
            parent = str(payload.get("parent_session_id") or "")
            delegation_id = _derive_delegation_id(payload)
            self._expire_one(delegation_id, index, parent, payload)
        self._pending_starts = remaining

    def _expire_one(
        self,
        delegation_id: Optional[str],
        index: int,
        parent: str,
        payload: Mapping[str, Any],
    ) -> None:
        # Caller holds the lock; _add_hook_node is written to tolerate
        # re-entrancy by using an RLock-safe pattern instead: emit inline.
        synthetic = delegation_id is None
        if synthetic:
            delegation_id = "hook:{}".format(
                payload.get("child_session_id")
                or payload.get("child_subagent_id")
                or f"idx{index}"
            )
        key = (delegation_id, index)
        if key in self._nodes:
            self._attach_child(self._nodes[key], payload)
            self._apply_pending_stop(key)
            return
        node = _Node(
            key=key,
            parent_session=parent,
            name=str(payload.get("child_name") or "") or _fallback_name(index),
            goal=str(payload.get("child_goal") or ""),
            from_table=False,
        )
        self._attach_child(node, payload)
        self._nodes[key] = node
        self._emit_locked(self._node_event("add", node, source="hook"))
        self._apply_pending_stop(key)

    def _claim_pending_starts(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        """New rows adopt parked hook starts with matching (parent, index)."""
        if not self._pending_starts:
            return
        still_pending = []
        for payload, deadline in self._pending_starts:
            index = _parse_task_index(payload)
            parent = str(payload.get("parent_session_id") or "")
            matches = [
                delegation_id
                for delegation_id, row in rows.items()
                if row["state"] in LIVE_STATES
                and _row_parent(row) == parent
                and index < len(_expand_tasks(row["task"]))
                and (delegation_id, index) not in self._nodes
            ]
            if len(matches) == 1:
                delegation_id = matches[0]
                key = (delegation_id, index)
                node = _Node(
                    key=key,
                    parent_session=parent,
                    name=str(payload.get("child_name") or "")
                    or _fallback_name(index),
                    goal=str(payload.get("child_goal") or ""),
                    from_table=True,
                )
                self._attach_child(node, payload)
                self._nodes[key] = node
                self._emit_locked(self._node_event("add", node, source="hook"))
                self._apply_pending_stop(key)
            else:
                still_pending.append((payload, deadline))
        self._pending_starts = still_pending


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def _row_parent(row: Mapping[str, Any]) -> str:
    return str(row.get("parent_session_id") or row.get("origin_session") or "")


def _expand_tasks(task: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """(name, goal) per task_index from a row's task_json payload."""
    goals = task.get("goals")
    if not isinstance(goals, list) or not goals:
        goals = [task.get("goal") if task.get("goal") is not None else ""]
    names = task.get("names")
    if not isinstance(names, list):
        names = []
    out: List[Tuple[str, str]] = []
    for i, goal in enumerate(goals):
        name = names[i] if i < len(names) and names[i] else None
        out.append(
            (str(name) if name else _fallback_name(i), str(goal or ""))
        )
    return out

def _terminal_result(
    row: Mapping[str, Any], index: int
) -> Tuple[Optional[str], str, Optional[str]]:
    """(summary, status, name) for one task of a terminal row."""
    result = row.get("result")
    status = str(row.get("state") or "completed")
    summary: Optional[str] = None
    name: Optional[str] = None
    if isinstance(result, dict):
        results = result.get("results")
        if isinstance(results, list) and 0 <= index < len(results):
            entry = results[index]
            if isinstance(entry, dict):
                summary = entry.get("summary")
                status = str(entry.get("status") or status)
                name = entry.get("name")
        elif index == 0 and result.get("summary") is not None:
            summary = result.get("summary")
            status = str(result.get("status") or status)
    return (
        str(summary) if summary is not None else None,
        status,
        str(name) if name is not None else None,
    )


def _safe_json(raw: Any) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
