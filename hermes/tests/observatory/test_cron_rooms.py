"""Contract tests for cron job rooms (M5b, D11).

Fake cron stores under tmp_path (jobs.json + executions.db written by the
test in the REAL scheduler formats — cron/jobs.py save_jobs shape and
cron/executions.py schema). One LIVE-ish test reads the machine's real
cron store strictly read-only. Laws under test: one node/room per job
with D11 ordering; rooms never purged on pause/completion; room dies only
when the job is deleted (D8 depth-1 purge shape); fire notices + terminal
result summaries appended as messages; no replay across polls/restarts;
the store is never written or created.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from observatory import tree
from observatory.cron_rooms import (
    CRON_EXEC_META_PREFIX,
    CronRooms,
    CronStore,
    fired_message,
    result_message,
)
from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import IntentExecutor, PurgeRoom, Renderer, SendMessage
from observatory.state import ObservatoryState, StateError

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch"

EXECUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
     id TEXT PRIMARY KEY,
     job_id TEXT NOT NULL,
     source TEXT NOT NULL,
     process_id TEXT NOT NULL,
     pid INTEGER NOT NULL,
     process_started_at INTEGER,
     status TEXT NOT NULL,
     claimed_at TEXT NOT NULL,
     started_at TEXT,
     finished_at TEXT,
     error TEXT
)
"""


def write_jobs(cron_dir: Path, jobs: list[dict]) -> None:
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8"
    )


def write_execution(
    cron_dir: Path,
    exec_id: str,
    job_id: str,
    status: str,
    claimed_at: str,
    *,
    error: str | None = None,
) -> None:
    cron_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cron_dir / "executions.db")
    try:
        conn.execute(EXECUTIONS_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO executions (id, job_id, source, process_id,"
            " pid, status, claimed_at, started_at, finished_at, error)"
            " VALUES (?, ?, 'builtin', 'p', 1, ?, ?, ?, ?, ?)",
            (exec_id, job_id, status, claimed_at, claimed_at, claimed_at, error),
        )
        conn.commit()
    finally:
        conn.close()


def seed_state(tmp_path: Path) -> ObservatoryState:
    """Idempotent gateway+orchestrator seed (restart tests reopen the db)."""
    state = ObservatoryState(tmp_path / "state.db")

    def ensure(node_id: str, name: str, extra: dict | None = None) -> None:
        try:
            state.get(node_id)
        except StateError:
            slug = assign_slug(name, state)
            state.add_node(
                node_id,
                engine="hermes",
                name=name,
                slug=slug,
                mxid=virtual_mxid(slug),
                session_ref=f"session:{node_id}",
                extra=extra,
            )

    ensure(GW, "gateway agent", {"kind": "gateway"})
    ensure(ORCH, "auth-refactor")
    return state


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    _n: int = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def create_room(self, *, name, sender, preset=None, invite=(), space=False):
        self.calls.append(("create_room", name, sender, space))
        return self._id("!room")

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users)))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender))
        return self._id("$ev")

    async def edit_message(self, room_id, event_id, body, *, sender, formatted_body=None):
        self.calls.append(("edit", room_id, event_id, body))
        return self._id("$ev")

    async def delete_room(self, room_id, *, block=False, purge=True):
        self.calls.append(("delete", room_id))

    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        return {"rooms": [{"room_id": room_id, "children_state": []}]}


def make_rooms(tmp_path: Path, cron_dir: Path, *, executor: bool = False):
    state = seed_state(tmp_path)
    client = FakeClient()
    ex = (
        IntentExecutor(client, state, owner_mxid=OWNER, server_name=SERVER)
        if executor
        else None
    )
    renderer = Renderer(
        state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER, executor=ex
    )
    return CronRooms(renderer, store=CronStore(cron_dir)), state, client


def job(job_id: str, name: str, **over: object) -> dict:
    base: dict = {
        "id": job_id,
        "name": name,
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 60},
        "schedule_display": "every 60m",
        "prompt": "do the thing",
    }
    base.update(over)
    return base


# --- CronStore (read-only adapter) ---------------------------------------------------


class TestCronStore:
    def test_reads_jobs_dict_and_bare_list(self, tmp_path):
        write_jobs(tmp_path, [job("abc123", "backup")])
        store = CronStore(tmp_path)
        assert [j["id"] for j in store.read_jobs()] == ["abc123"]
        (tmp_path / "jobs.json").write_text(json.dumps([job("zzz", "x")]), encoding="utf-8")
        assert [j["id"] for j in store.read_jobs()] == ["zzz"]

    def test_missing_or_corrupt_store_degrades_to_empty(self, tmp_path):
        store = CronStore(tmp_path / "nope")
        assert store.read_jobs() == []
        assert store.read_executions() == []
        (tmp_path / "jobs.json").write_text("{not json", encoding="utf-8")
        assert CronStore(tmp_path).read_jobs() == []

    def test_read_jobs_never_writes(self, tmp_path):
        cron_dir = tmp_path / "cron"
        write_jobs(cron_dir, [job("abc123", "backup")])
        before = (cron_dir / "jobs.json").stat()
        store = CronStore(cron_dir)
        store.read_jobs()
        store.read_jobs()
        after = (cron_dir / "jobs.json").stat()
        assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)
        assert [p.name for p in cron_dir.iterdir()] == ["jobs.json"]
    def test_junk_records_skipped(self, tmp_path):
        write_jobs(tmp_path, [job("ok1", "real"), "a string", {"no_id": True}, {"id": 42}])
        assert [j["id"] for j in CronStore(tmp_path).read_jobs()] == ["ok1"]

    def test_executions_ordered_oldest_first(self, tmp_path):
        write_execution(tmp_path, "e2", "j1", "completed", "2026-09-08T02:00:00+00:00")
        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        assert [r["id"] for r in CronStore(tmp_path).read_executions()] == ["e1", "e2"]

    def test_executions_readonly_never_creates(self, tmp_path):
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        store = CronStore(cron_dir)  # dir exists, db does not
        assert store.read_executions() == []
        assert not (cron_dir / "executions.db").exists()
    def test_latest_output_excerpt(self, tmp_path):
        out = tmp_path / "output" / "j1"
        out.mkdir(parents=True)
        (out / "old.md").write_text("old", encoding="utf-8")
        (out / "new.md").write_text("fresh result " + "x" * 400, encoding="utf-8")
        excerpt = CronStore(tmp_path).latest_output_excerpt("j1")
        assert excerpt is not None and excerpt.startswith("fresh result")
        assert len(excerpt) <= 200
        assert CronStore(tmp_path).latest_output_excerpt("../escape") is None


# --- registry sync ----------------------------------------------------------------------


class TestRegistry:
    def test_one_node_per_job_with_d11_ordering(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup"), job("j2", "watch")])
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        result = rooms.sync_registry()
        assert [r["node_id"] for r in result.added] == ["cron:j1", "cron:j2"]
        row = state.get("cron:j1")
        assert row["name"] == "cron:backup"
        assert row["extra"]["kind"] == tree.KIND_CRON_JOB
        assert row["parent_node_id"] == GW and row["depth"] == 1
        # D11 ordering inside the desired plan (spec §3: gateway-agent
        # subspace FIRST, then directives, cron rooms, orchestrators).
        plan = Renderer(
            state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER
        ).build_plan(host="h")
        top = [r.key for r in plan.rooms] + [s.key for s in plan.subspaces]
        assert top == [tree.DIRECTIVES_ROOM_KEY, "cron:j1", "cron:j2",
                       tree.GATEWAY_AGENT_SPACE_KEY, ORCH]
        assert [c.key for c in tree.space_child_order(plan)] == [
            tree.GATEWAY_AGENT_SPACE_KEY, tree.DIRECTIVES_ROOM_KEY,
            "cron:j1", "cron:j2", ORCH,
        ]
    def test_sync_idempotent(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, _, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        assert rooms.sync_registry().added == []

    def test_pause_and_completion_never_remove_rooms(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, _, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        write_jobs(
            tmp_path,
            [job("j1", "backup", enabled=False, state="paused",
                 paused_at="2026-09-07T00:00:00+00:00")],
        )
        assert rooms.sync_registry().removed == []
        write_jobs(tmp_path, [job("j1", "backup", enabled=False, state="completed")])
    def test_job_deletion_purges_room_only(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        state.set_room_id("cron:j1", "!r-cron:x")
        state.set_space_id(GW, "!s-gw:x")
        write_jobs(tmp_path, [])  # job deleted from the store
        result = rooms.sync_registry()
        assert [r["node_id"] for r in result.removed] == ["cron:j1"]
        # D8 depth-1 purge shape: summary to the gateway room + admin
        # DELETE. Cron rooms are rooms (no space_id), so no DetachChild
        # is planned here — the stale m.space.child is reaped by the
        # sidecar's diff pass (tree.diff_plan).
        intents = result.removal_intents
        assert isinstance(intents[0], SendMessage) and intents[0].room_key == GW
        assert any(isinstance(i, PurgeRoom) and i.room_id == "!r-cron:x" for i in intents)
        assert not any(i.__class__.__name__ == "DetachChild" for i in intents)

    @pytest.mark.asyncio
    async def test_apply_registry_executes_and_drops_rows(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup"), job("j2", "gone")])
        rooms, state, client = make_rooms(tmp_path, tmp_path, executor=True)
        rooms.sync_registry()  # both nodes exist now
        for node in (GW, "cron:j1", "cron:j2"):
            state.set_room_id(node, f"!r-{node}:x")
        state.set_space_id(GW, "!s-gw:x")
        write_jobs(tmp_path, [job("j1", "backup")])  # j2 deleted
        result = await rooms.apply_registry()
        assert [r["node_id"] for r in result.removed] == ["cron:j2"]
        with pytest.raises(StateError):
            state.get("cron:j2")
        assert state.get("cron:j1")["node_id"] == "cron:j1"
        assert ("delete", "!r-cron:j2:x") in client.calls
        # fresh job room gets its notice message
        sends = [c for c in client.calls if c[0] == "send" and c[1] == "!r-cron:j1:x"]
        assert sends and "cron job room" in sends[0][2]


# --- fire tracking ------------------------------------------------------------------------


class TestFires:
    def test_fired_then_result_across_polls(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        state.set_room_id("cron:j1", "!r-j1:x")

        write_execution(tmp_path, "e1", "j1", "running", "2026-09-08T01:00:00+00:00")
        planned = rooms.poll_fires()
        assert len(planned) == 1
        node_id, intents = planned[0]
        assert node_id == "cron:j1" and len(intents) == 1
        assert isinstance(intents[0], SendMessage)
        assert intents[0].body.startswith("🔥") and "**backup**" in intents[0].body
        assert "(every 60m)" in intents[0].body

        assert rooms.poll_fires() == []  # unchanged ledger: no replay

        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        planned = rooms.poll_fires()
        assert len(planned) == 1
        body = planned[0][1][0].body
        assert body.startswith("✅") and "**backup**" in body and "completed" in body

    def test_instantly_terminal_fire_gets_single_result(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        state.set_room_id("cron:j1", "!r-j1:x")
        write_execution(
            tmp_path, "e1", "j1", "failed", "2026-09-08T01:00:00+00:00",
            error="RuntimeError: boom\nsecond line",
        )
        planned = rooms.poll_fires()
        assert len(planned[0][1]) == 1  # fired+result combined, not two messages
        body = planned[0][1][0].body
        assert body.startswith("❌") and "RuntimeError: boom" in body
        assert "second line" not in body

    def test_no_replay_after_restart(self, tmp_path):
        """Markers persist in state meta; a fresh manager never replays."""
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        assert rooms.poll_fires() == []
        assert state.get_meta(CRON_EXEC_META_PREFIX + "j1")

        rooms2, _, _ = make_rooms(tmp_path, tmp_path)
        assert rooms2.poll_fires() == []

    def test_pending_survives_restart_for_inflight_fire(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        state.set_room_id("cron:j1", "!r-j1:x")
        write_execution(tmp_path, "e1", "j1", "running", "2026-09-08T01:00:00+00:00")
        rooms.poll_fires()  # fired notice; pending recorded

        rooms2, state2, _ = make_rooms(tmp_path, tmp_path)  # sidecar restart
        state2.set_room_id("cron:j1", "!r-j1:x")
        assert rooms2.poll_fires() == []  # still running: nothing new
        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        planned = rooms2.poll_fires()
        assert len(planned) == 1 and planned[0][1][0].body.startswith("✅")

    def test_history_does_not_replay_into_late_room(self, tmp_path):
        """Terminal fires seen while the job had no room never replay once
        the room appears (markers advanced room-less)."""
        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        write_jobs(tmp_path, [])
        rooms, _, _ = make_rooms(tmp_path, tmp_path)
        assert rooms.poll_fires() == []  # no job, no node — but marker advances

        write_jobs(tmp_path, [job("j1", "late room")])
        rooms.sync_registry()
        assert rooms.poll_fires() == []  # history predates the marker

    def test_result_includes_output_excerpt(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        out = tmp_path / "output" / "j1"
        out.mkdir(parents=True)
        (out / "2026-09-08T01-00-00.md").write_text("report: all good", encoding="utf-8")
        rooms, state, _ = make_rooms(tmp_path, tmp_path)
        rooms.sync_registry()
        state.set_room_id("cron:j1", "!r-j1:x")
        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        assert "report: all good" in rooms.poll_fires()[0][1][0].body

    @pytest.mark.asyncio
    async def test_render_fires_sends_into_job_rooms(self, tmp_path):
        write_jobs(tmp_path, [job("j1", "backup")])
        rooms, state, client = make_rooms(tmp_path, tmp_path, executor=True)
        rooms.sync_registry()
        state.set_room_id("cron:j1", "!r-j1:x")
        write_execution(tmp_path, "e1", "j1", "completed", "2026-09-08T01:00:00+00:00")
        await rooms.render_poll()
        sends = [c for c in client.calls if c[0] == "send"]
        assert len(sends) == 1 and sends[0][1] == "!r-j1:x"
        assert sends[0][3] == state.get("cron:j1")["mxid"]


# --- composers ---------------------------------------------------------------------------


class TestComposers:
    def test_fired_message(self):
        body, formatted = fired_message("backup", schedule="every 60m")
        assert "🔥" in body and "**backup** fired (every 60m)" in body
        assert "<strong>backup</strong>" in formatted

    def test_result_message_status_icons(self):
        assert result_message("x", "completed")[0].startswith("✅")
        assert result_message("x", "failed", error="E")[0].startswith("❌")
        assert "— E" in result_message("x", "failed", error="E")[0]
        assert result_message("x", "unknown")[0].startswith("❓")


# --- LIVE-ish: the machine's real cron store, strictly read-only ----------------------------


class TestLiveCronStore:
    def test_read_real_store_readonly(self):
        from mercury_constants import get_hermes_home

        store = CronStore.for_hermes_home(get_hermes_home())
        if not store.jobs_file.exists():
            pytest.skip(f"no real cron store at {store.jobs_file}")
        before = store.jobs_file.stat()
        jobs = store.read_jobs()
        assert isinstance(jobs, list)
        assert all(isinstance(j, dict) and isinstance(j.get("id"), str) for j in jobs)
        ids = [j["id"] for j in jobs]
        assert len(ids) == len(set(ids))  # job ids unique
        after = store.jobs_file.stat()
        assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)
        if store.executions_db.exists():
            rows = store.read_executions()
            assert all(r["job_id"] and r["claimed_at"] for r in rows)
