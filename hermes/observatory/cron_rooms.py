"""M5b (D11): cron job rooms — one room per JOB, directly in the gateway
space, with rolling per-fire history appended as messages.

The module is a READ-ONLY adapter over the cron scheduler's own store
(discovered in ``cron/jobs.py`` / ``cron/executions.py`` — never modified):

- **Jobs registry**: ``<HERMES_HOME>/cron/jobs.json`` — ``{"jobs": [...]}``
  written by ``save_jobs()`` (a bare list from hand edits is tolerated).
  Parsed here directly; the scheduler's ``load_jobs()`` AUTO-REPAIRS and
  rewrites the file, which the observatory must never do.
- **Fire ledger**: ``<HERMES_HOME>/cron/executions.db`` — SQLite
``executions`` table (id, job_id, source, status
claimed/running/completed/failed/unknown, claimed_at, finished_at,
error), opened ``mode=ro`` so a missing database is never created.
- **Per-fire output**: ``<cron>/output/<job_id>/<timestamp>.md`` — newest
  file excerpted into result summaries, best-effort.

Room lifecycle (D11): a job gets its node (``extra.kind == "cron-job"``,
parent = the gateway agent) and the sidecar's plan/apply pass renders one
room per job AFTER the directives room and BEFORE orchestrator subspaces
(that ordering is ``tree.desired_plan``'s job — this module only maintains
the nodes). Rooms are NEVER purged on pause/completion — jobs are not
agents; a room dies only when its job is deleted from the store, via the
same D8 depth-1 purge shape the renderer already produces.

Fire tracking: ``poll_fires()`` diffs the executions ledger against per-job
state meta (``cronexec:<job_id>``, JSON ``{"attached": bool, "last": iso,
"pending": {...}}``). A job's FIRST poll is a baseline (terminal history is
swallowed — a fresh room never replays old fires); executions still
claimed/running at first sight are announced and tracked to their summary.
A newly seen non-terminal execution appends a 🔥 fired notice; a terminal
one appends a single ✅/❌ result summary (with error line and output
excerpt); executions announced while non-terminal get their summary when
they land in a terminal state — across sidecar restarts, via the persisted
pending set.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from observatory import tree
from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import (
    RenderIntent,
    Renderer,
    SendMessage,
    markdown_to_html,
    truncate,
)
from observatory.state import ObservatoryState, StateError

logger = logging.getLogger(__name__)

#: Node ids (and, per spec §3, room names) are ``cron:<job-name>``.
CRON_NODE_PREFIX = "cron:"

#: State meta key prefix for per-job fire tracking.
CRON_EXEC_META_PREFIX = "cronexec:"

#: State meta marker: a job room's first notice has been sent.
CRON_NOTICE_META_PREFIX = "cronnotice:"

#: Ledger statuses that end a fire.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "unknown"})

#: Result-summary excerpt budget (chars of the newest output file).
_OUTPUT_EXCERPT_CHARS = 200

_SAFE_JOB_ID = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


# ============================================================================
# Read-only store adapter
# ============================================================================


class CronStore:
    """Strictly read-only view of one profile's cron store.

    Every method degrades to empty on missing/corrupt input and NEVER
    writes: jobs.json is parsed by hand (not ``load_jobs()`` — that repairs
    and rewrites), and executions.db is opened with ``mode=ro``.
    """

    def __init__(self, cron_dir: str | Path):
        self.cron_dir = Path(cron_dir)
        self.jobs_file = self.cron_dir / "jobs.json"
        self.executions_db = self.cron_dir / "executions.db"
        self.output_dir = self.cron_dir / "output"

    @classmethod
    def for_hermes_home(cls, home: str | Path) -> "CronStore":
        """Store under a hermes home (``<home>/cron`` — cron/jobs.py layout)."""
        return cls(Path(home) / "cron")

    def read_jobs(self) -> list[dict[str, Any]]:
        """All job records; [] on missing/corrupt store. Job records are
        dicts carrying a string ``id`` (anything else is junk, skipped)."""
        try:
            raw = self.jobs_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("cron_rooms: cannot read %s: %s", self.jobs_file, exc)
            return []
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.error("cron_rooms: corrupt %s: %s", self.jobs_file, exc)
            return []
        if isinstance(data, Mapping):
            jobs = data.get("jobs", [])
        elif isinstance(data, list):
            jobs = data
        else:
            return []
        if not isinstance(jobs, list):
            return []
        return [j for j in jobs if isinstance(j, Mapping) and isinstance(j.get("id"), str)]

    def read_executions(self) -> list[dict[str, Any]]:
        """All execution rows, oldest-fire-first. [] when the ledger is
        missing or unreadable; the database file is never created."""
        if not self.executions_db.exists():
            return []
        uri = f"file:{self.executions_db}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            logger.warning("cron_rooms: cannot open %s: %s", self.executions_db, exc)
            return []
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY claimed_at ASC, id ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.warning("cron_rooms: cannot query %s: %s", self.executions_db, exc)
            return []
        finally:
            conn.close()

    def latest_output_excerpt(self, job_id: str, *, max_chars: int = _OUTPUT_EXCERPT_CHARS) -> Optional[str]:
        """First-line-ish excerpt of the newest ``output/<job_id>/*.md``."""
        if not job_id or set(job_id) - _SAFE_JOB_ID:
            return None  # never join unvalidated ids into a path
        job_dir = self.output_dir / job_id
        try:
            files = [p for p in job_dir.iterdir() if p.is_file() and p.suffix == ".md"]
        except OSError:
            return None
        if not files:
            return None
        newest = max(files, key=lambda p: p.stat().st_mtime)
        try:
            text = newest.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        excerpt, _ = truncate(text.replace("\n", " "), max_chars)
        return excerpt or None


# ============================================================================
# Message composition — pure
# ============================================================================


def fired_message(job_name: str, *, schedule: Optional[str] = None) -> tuple[str, str]:
    when = f" ({schedule})" if schedule else ""
    src = f"🔥 **{job_name}** fired{when}"
    return src, markdown_to_html(src)


def result_message(
    job_name: str,
    status: str,
    *,
    error: Optional[str] = None,
    excerpt: Optional[str] = None,
) -> tuple[str, str]:
    """Terminal per-fire summary: status, first error line, output excerpt."""
    icon = {"completed": "✅", "failed": "❌"}.get(status, "❓")
    src = f"{icon} **{job_name}** {status}"
    if error:
        first, _ = truncate(error.strip().replace("\n", " "), 160)
        src += f" — {first}"
    if excerpt:
        src += f"\n> {excerpt}"
    return src, markdown_to_html(src)


def job_notice_body(job_name: str, *, schedule: Optional[str] = None) -> tuple[str, str]:
    """First message in a fresh job room: what this room mirrors."""
    when = f", {schedule}" if schedule else ""
    src = f"🕒 cron job room — **{job_name}**{when}. One rolling history per fire; the room dies only when the job is deleted."
    return src, markdown_to_html(src)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================================
# Registry + fire tracking
# ============================================================================


@dataclass
class RegistrySync:
    """Outcome of one registry reconciliation pass."""

    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    #: Intents purging the REMOVED jobs' rooms (D8 depth-1 shape via the
    #: renderer): summary to the gateway room + admin DELETE. The live
    #: path executes them and drops the rows.
    removal_intents: tuple[RenderIntent, ...] = ()


class CronRooms:
    """Nodes + fire messages for D11 cron rooms. Constructed over a
    :class:`~observatory.renderer.Renderer` (pure when executor=None)."""

    def __init__(self, renderer: Renderer, *, store: CronStore):
        self.renderer = renderer
        self.state: ObservatoryState = renderer.state
        self.store = store

    # --- registry ----------------------------------------------------------------

    @property
    def gateway_node_id(self) -> str:
        return self.renderer.gateway_node_id

    def job_nodes(self) -> dict[str, dict[str, Any]]:
        """Live cron-job nodes keyed by job id."""
        out: dict[str, dict[str, Any]] = {}
        for row in self.state.children_of(self.gateway_node_id):
            if row["status"] != "live":
                continue
            if (row.get("extra") or {}).get("kind") != tree.KIND_CRON_JOB:
                continue
            job_id = (row.get("extra") or {}).get("job_id")
            if isinstance(job_id, str):
                out[job_id] = row
        return out

    def _job_display(self, job: Mapping[str, Any]) -> str:
        return str(job.get("name") or job.get("id") or "").strip() or str(job["id"])

    def sync_registry(self) -> RegistrySync:
        """Reconcile nodes with the cron store (pure: rows added here,
        removal intents planned but not executed).

        Pause/completion never removes a room (D11: rooms are not agents);
        only the job's disappearance from the store does. Removing uses the
        renderer's own depth-1 death plan — the room is attached to the
        gateway space, so the detach targets it.
        """
        jobs = self.store.read_jobs()
        desired_ids = {job["id"] for job in jobs}
        existing = self.job_nodes()

        added: list[dict[str, Any]] = []
        for job in jobs:
            if job["id"] in existing:
                continue
            display = self._job_display(job)
            slug = assign_slug(f"cron-{display}", self.state)
            added.append(
                self.state.add_node(
                    CRON_NODE_PREFIX + job["id"],
                    engine="hermes",  # the cron scheduler is hermes-side
                    name=f"cron:{display}",
                    slug=slug,
                    mxid=virtual_mxid(slug),
                    session_ref=f"cronjob:{job['id']}",
                    parent_node_id=self.gateway_node_id,
                    extra={"kind": tree.KIND_CRON_JOB, "job_id": job["id"]},
                )
            )

        removed: list[dict[str, Any]] = []
        removal_intents: list[RenderIntent] = []
        for job_id, row in sorted(existing.items()):
            if job_id in desired_ids:
                continue
            removed.append(row)
            removal_intents.extend(
                self.renderer.plan_death(row["node_id"], status="deleted", summary="cron job removed")
            )
        return RegistrySync(
            added=added,
            removed=removed,
            removal_intents=tuple(removal_intents),
        )

    def _schedule_for(self, row: Mapping[str, Any]) -> Optional[str]:
        job_id = (row.get("extra") or {}).get("job_id")
        for job in self.store.read_jobs():
            if job.get("id") == job_id:
                display = job.get("schedule_display")
                return display if isinstance(display, str) and display.strip() else None
        return None

    # --- room notices ----------------------------------------------------------------

    def plan_notices(self) -> tuple[RenderIntent, ...]:
        """First-message intents for job rooms that don't have one yet
        (idempotent via the ``cronnotice:`` meta marker, so a notice can
        wait for the sidecar's apply_plan pass to create the room)."""
        intents: list[RenderIntent] = []
        for job_id, node in sorted(self.job_nodes().items()):
            if not node.get("room_id"):
                continue  # room not provisioned yet — a later pass picks it up
            try:
                self.state.get_meta(CRON_NOTICE_META_PREFIX + job_id)
                continue
            except StateError:
                pass
            body, formatted = job_notice_body(
                node["name"], schedule=self._schedule_for(node)
            )
            intents.append(SendMessage(node["node_id"], node["mxid"], body, formatted))
        return tuple(intents)

    async def apply_registry(self) -> RegistrySync:
        """Live reconcile: plan + execute purge intents, drop rows, then
        send pending room notices (marking them sent). Room CREATION for
        added jobs is the sidecar's apply_plan pass — notices for rooms
        that don't exist yet fire on a later call."""
        result = self.sync_registry()
        executor = self.renderer.executor
        if executor is None:
            raise RuntimeError("live path requires a Renderer with an IntentExecutor")
        if result.removal_intents:
            await executor.execute(result.removal_intents)
        for row in result.removed:
            self.state.mark_dead(row["node_id"])
            self.state.mark_deleted_and_purge(row["node_id"])

        notices = self.plan_notices()
        if notices:
            await executor.execute(notices)
            for intent in notices:
                job_id = intent.room_key.removeprefix(CRON_NODE_PREFIX)
                self.state.set_meta(CRON_NOTICE_META_PREFIX + job_id, "sent")
        return result

    # --- fire tracking -------------------------------------------------------------

    def _load_tracking(self, job_id: str) -> dict[str, Any]:
        try:
            raw = self.state.get_meta(CRON_EXEC_META_PREFIX + job_id)
        except StateError:
            return {"attached": False, "last": None, "pending": {}}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"attached": False, "last": None, "pending": {}}
        if not isinstance(data, Mapping):
            return {"attached": False, "last": None, "pending": {}}
        pending = data.get("pending")
        return {
            "attached": bool(data.get("attached")),
            "last": data.get("last"),
            "pending": dict(pending) if isinstance(pending, Mapping) else {},
        }

    def _save_tracking(self, job_id: str, tracking: Mapping[str, Any]) -> None:
        self.state.set_meta(
            CRON_EXEC_META_PREFIX + job_id,
            json.dumps(
                {
                    "attached": True,  # every saved pass has attached
                    "last": tracking.get("last"),
                    "pending": tracking.get("pending") or {},
                },
                ensure_ascii=False,
            ),
        )

    def poll_fires(self) -> list[tuple[str, tuple[RenderIntent, ...]]]:
        """Diff the executions ledger since the last poll → per-job message
        intents (pure: nothing executed, markers persisted).

        Only jobs with a live node get messages; markers advance for every
        tracked job so history never replays when a room appears late.
        Returns ``(node_id, intents)`` pairs, oldest fire first.
        """
        jobs_by_id = {job["id"]: job for job in self.store.read_jobs()}
        nodes = self.job_nodes()
        tracked_jobs = set(nodes) | {
            key[len(CRON_EXEC_META_PREFIX):]
            for key in self._cronexec_meta_keys()
        }

        by_job: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.read_executions():
            job_id = row.get("job_id")
            if isinstance(job_id, str):
                by_job.setdefault(job_id, []).append(row)

        out: list[tuple[str, tuple[RenderIntent, ...]]] = []
        for job_id in sorted(tracked_jobs | set(by_job)):
            rows = by_job.get(job_id, [])
            node = nodes.get(job_id)
            tracking = self._load_tracking(job_id)
            pending: dict[str, str] = dict(tracking["pending"])
            last_ts = _parse_ts(tracking["last"])
            intents: list[RenderIntent] = []
            first_attach = not tracking["attached"]

            for row in rows:
                exec_id = str(row.get("id") or "")
                if not exec_id:
                    continue
                claimed = _parse_ts(row.get("claimed_at"))
                status = str(row.get("status") or "")
                # "new" is judged BEFORE the marker advances past this row.
                is_new = claimed is not None and (last_ts is None or claimed > last_ts)
                if claimed is None and exec_id not in pending:
                    is_new = True

                if first_attach:
                    # First sight of this job: terminal history is a
                    # BASELINE (a fresh room must not replay old fires);
                    # executions still claimed/running are genuinely in
                    # flight — announce and track them.
                    if status not in _TERMINAL_STATUSES:
                        if node is not None:
                            intents.append(self._fired_intent(node, jobs_by_id.get(job_id)))
                        pending[exec_id] = str(row.get("claimed_at") or "")
                elif exec_id in pending:
                    # Announced while in flight → summary on landing.
                    if status in _TERMINAL_STATUSES:
                        if node is not None:
                            intents.append(self._result_intent(node, row))
                        pending.pop(exec_id, None)
                elif is_new:
                    if status in _TERMINAL_STATUSES:
                        if node is not None:
                            intents.append(self._result_intent(node, row))
                    else:
                        if node is not None:
                            intents.append(self._fired_intent(node, jobs_by_id.get(job_id)))
                        pending[exec_id] = str(row.get("claimed_at") or "")

                if claimed is not None and (last_ts is None or claimed > last_ts):
                    last_ts = claimed

            tracking_out = {
                "last": last_ts.isoformat() if last_ts else tracking["last"],
                "pending": pending,
            }
            self._save_tracking(job_id, tracking_out)
            if intents and node is not None:
                out.append((node["node_id"], tuple(intents)))
        return out

    def _cronexec_meta_keys(self) -> list[str]:
        rows = self.state._db.execute(
            "SELECT key FROM meta WHERE key LIKE ?", (CRON_EXEC_META_PREFIX + "%",)
        ).fetchall()
        return [r["key"] for r in rows]

    def _fired_intent(
        self, node: Mapping[str, Any], job: Optional[Mapping[str, Any]]
    ) -> RenderIntent:
        schedule = None
        if job is not None:
            display = job.get("schedule_display")
            if isinstance(display, str) and display.strip():
                schedule = display.strip()
        body, formatted = fired_message(node["name"].removeprefix("cron:"), schedule=schedule)
        return SendMessage(node["node_id"], node["mxid"], body, formatted)

    def _result_intent(
        self,
        node: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> RenderIntent:
        job_id = (node.get("extra") or {}).get("job_id")
        excerpt = self.store.latest_output_excerpt(job_id) if job_id else None
        body, formatted = result_message(
            node["name"].removeprefix("cron:"),
            str(row.get("status") or "unknown"),
            error=row.get("error") if isinstance(row.get("error"), str) else None,
            excerpt=excerpt,
        )
        return SendMessage(node["node_id"], node["mxid"], body, formatted)

    async def render_poll(self) -> list[RenderIntent]:
        """Live fire pass: plan + execute the per-fire messages.

        Named ``render_poll`` to mirror ``ManualRunsWatcher.render_poll``
        (the sidecar runs both on its poll loops)."""
        planned = self.poll_fires()
        intents = [intent for _, per_node in planned for intent in per_node]
        if intents:
            if self.renderer.executor is None:
                raise RuntimeError("live path requires a Renderer with an IntentExecutor")
            await self.renderer.executor.execute(intents)
        return intents
