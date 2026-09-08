"""M3b discovery engine tests: async_delegations poll + lifecycle hooks.

Hermetic: a temp SQLite db with the REAL async_delegations schema, and
synthetic hook payloads using the exact field names the hermes lifecycle
hooks fire with (delegate_tool.py subagent_start / subagent_stop). Every
test drives the engine's loop directly (asyncio.run) and asserts on the
NodeStream — including the ordering races the engine must survive.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from observatory.discovery import DiscoveryEngine, NodeEvent

# The durable ledger's schema (tools/async_delegation.py
# _initialize_schema — columns the poll reads, plus the durable-delivery
# tail so the table shape matches production).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS async_delegations (
    delegation_id TEXT PRIMARY KEY,
    origin_session TEXT NOT NULL,
    origin_ui_session_id TEXT NOT NULL DEFAULT '',
    parent_session_id TEXT,
    state TEXT NOT NULL,
    dispatched_at REAL NOT NULL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    event_json TEXT,
    result_json TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    task_json TEXT,
    delivery_claim TEXT,
    delivery_claimed_at REAL,
    origin_session_id TEXT NOT NULL DEFAULT ''
)
"""


class Db:
    """The sandbox state.db."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "state.db"
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    def _conn(self):
        return sqlite3.connect(self.path, timeout=5.0)

    def insert_running(
        self,
        delegation_id,
        *,
        parent_session_id="sess-parent",
        origin_session="",
        task=None,
        state="running",
        result_json=None,
    ):
        task = task if task is not None else {"goal": "one goal"}
        conn = self._conn()
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, parent_session_id, state,
                dispatched_at, updated_at, task_json, result_json)
               VALUES (?, ?, ?, ?, 1.0, 1.0, ?, ?)""",
            (
                delegation_id,
                origin_session,
                parent_session_id,
                state,
                json.dumps(task),
                json.dumps(result_json) if result_json is not None else None,
            ),
        )
        conn.commit()
        conn.close()

    def set_state(self, delegation_id, state, result_json=None):
        conn = self._conn()
        if result_json is not None:
            conn.execute(
                "UPDATE async_delegations SET state=?, result_json=?, updated_at=2.0 "
                "WHERE delegation_id=?",
                (state, json.dumps(result_json), delegation_id),
            )
        else:
            conn.execute(
                "UPDATE async_delegations SET state=?, updated_at=2.0 "
                "WHERE delegation_id=?",
                (state, delegation_id),
            )
        conn.commit()
        conn.close()

    def delete(self, delegation_id):
        conn = self._conn()
        conn.execute(
            "DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,)
        )
        conn.commit()
        conn.close()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def drain(engine, expect=None, timeout=2.0):
    """Pull events off the stream; all of them up to `expect` if given."""
    out = []
    loop = asyncio.get_running_loop()
    # Give call_soon_threadsafe callbacks already scheduled a chance.
    await asyncio.sleep(0)
    if expect is None:
        return out
    while len(out) < expect:
        try:
            item = await asyncio.wait_for(engine.stream().__anext__(), timeout)
        except asyncio.TimeoutError:
            pytest.fail(f"only {len(out)}/{expect} events arrived: {out}")
        out.append(item)
    return out


def start_payload(**over):
    payload = {
        "parent_session_id": "sess-parent",
        "parent_turn_id": "turn-1",
        "parent_subagent_id": None,
        "child_session_id": "sess-child-1",
        "child_subagent_id": "sa-0-11111111",
        "child_role": "worker",
        "child_goal": "hook goal",
    }
    payload.update(over)
    return payload


def stop_payload(**over):
    payload = {
        "parent_session_id": "sess-parent",
        "parent_turn_id": "turn-1",
        "child_session_id": "sess-child-1",
        "child_role": "worker",
        "child_summary": "all done",
        "child_status": "completed",
        "tool_call_history": [],
        "duration_ms": 1234,
    }
    payload.update(over)
    return payload


def fresh_engine(db, clock, *, poll_interval=50.0, claim_grace=50.0):
    # Huge intervals + direct _tick() calls keep tests deterministic;
    # grace is also huge so expiry only happens via clock.advance().
    return DiscoveryEngine(
        db.path, poll_interval=poll_interval, claim_grace=claim_grace, clock=clock
    )


# ---------------------------------------------------------------------------
# Poll source
# ---------------------------------------------------------------------------


class TestPoll:
    def test_running_row_expands_to_one_node_per_task(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running(
            "deleg_aaaa1111",
            task={"goals": ["goal A", "goal B"], "names": ["alpha", "beta"]},
        )
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            events = await drain(engine, expect=2)
            await engine.stop()
            return events

        events = asyncio.run(main())
        adds = [e for e in events if e.kind == "add"]
        assert [(a.delegation_id, a.task_index) for a in adds] == [
            ("deleg_aaaa1111", 0),
            ("deleg_aaaa1111", 1),
        ]
        assert [a.name for a in adds] == ["alpha", "beta"]
        assert [a.goal for a in adds] == ["goal A", "goal B"]
        assert all(a.status == "running" and a.source == "poll" for a in adds)
        assert all(a.parent_session == "sess-parent" for a in adds)
        assert [e.seq for e in events] == sorted(e.seq for e in events)

    def test_name_fallback_task_n_and_single_goal(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_bbbb2222", task={"goal": "solo"})
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            events = await drain(engine, expect=1)
            await engine.stop()
            return events

        (event,) = asyncio.run(main())
        assert event.name == "task-0"
        assert event.goal == "solo"

    def test_terminal_row_kills_each_task_with_per_task_status(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running(
            "deleg_cccc3333",
            task={"goals": ["g1", "g2"], "names": ["n1", "n2"]},
        )
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            adds = await drain(engine, expect=2)
            db.set_state(
                "deleg_cccc3333",
                "completed",
                result_json={
                    "results": [
                        {"summary": "S1", "status": "completed"},
                        {"summary": None, "status": "failed"},
                    ]
                },
            )
            engine._tick()
            rest = await drain(engine, expect=2)
            await engine.stop()
            return adds, rest

        adds, deaths = asyncio.run(main())
        assert all(e.kind == "add" for e in adds)
        by_index = {e.task_index: e for e in deaths}
        assert set(by_index) == {0, 1}
        assert by_index[0].kind == "death"
        assert by_index[0].status == "completed"
        assert by_index[0].summary == "S1"
        assert by_index[1].status == "failed"
        assert by_index[1].summary is None
        assert engine.snapshot() == {}

    def test_vanished_running_row_is_reaped_unknown(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_dddd4444")
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            await drain(engine, expect=1)
            db.delete("deleg_dddd4444")
            engine._tick()
            events = await drain(engine, expect=1)
            await engine.stop()
            return events

        (death,) = asyncio.run(main())
        assert death.kind == "death"
        assert death.status == "unknown"
        assert death.delegation_id == "deleg_dddd4444"

    def test_missing_db_and_locked_reads_are_inert(self, tmp_path):
        # No state.db at all: start/stop cleanly, zero events.
        engine = DiscoveryEngine(tmp_path / "nowhere.db", poll_interval=0.05)
        engine._db_path = str(tmp_path / "nowhere.db")

        async def main():
            await engine.start()
            await asyncio.sleep(0.15)
            await engine.stop()

        asyncio.run(main())
        assert engine.snapshot() == {}


# ---------------------------------------------------------------------------
# Hook source + poll-vs-hook dedupe (by delegation id)
# ---------------------------------------------------------------------------


class TestHookDedupe:
    def test_hook_with_explicit_delegation_id_then_poll_no_duplicate(self, tmp_path):
        db = Db(tmp_path)
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine.on_subagent_start(
                start_payload(delegation_id="deleg_eeee5555", task_index=0)
            )
            first = await drain(engine, expect=1)
            db.insert_running(
                "deleg_eeee5555",
                task={"goals": ["row goal"], "names": ["row-name"]},
            )
            engine._tick()
            await asyncio.sleep(0.05)
            await engine.stop()
            return first

        (event,) = asyncio.run(main())
        assert event.kind == "add"
        assert event.source == "hook"
        assert event.delegation_id == "deleg_eeee5555"
        assert event.child_session_id == "sess-child-1"
        assert event.goal == "hook goal"
        # Enriched, not duplicated: one node, poll saw the same key.
        snap = engine.snapshot()
        assert list(snap) == ["deleg_eeee5555/0"]
        assert snap["deleg_eeee5555/0"]["from_table"] is True

    def test_omp_style_child_id_maps_onto_delegation_key(self, tmp_path):
        db = Db(tmp_path)
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine.on_subagent_start(
                start_payload(child_subagent_id="deleg_ffff6666/1", delegation_id=None)
            )
            events = await drain(engine, expect=1)
            await engine.stop()
            return events

        (event,) = asyncio.run(main())
        assert event.delegation_id == "deleg_ffff6666"
        assert event.task_index == 1

    def test_hook_claims_already_polled_node_no_second_add(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_aaaa7777", task={"goal": "row goal"})
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            await drain(engine, expect=1)
            engine.on_subagent_start(start_payload())  # sa-0-… → index 0
            await asyncio.sleep(0.05)
            await engine.stop()

        asyncio.run(main())
        snap = engine.snapshot()
        assert list(snap) == ["deleg_aaaa7777/0"]
        assert snap["deleg_aaaa7777/0"]["child_session_id"] == "sess-child-1"

    def test_hook_before_row_is_claimed_under_real_key(self, tmp_path):
        """The core ordering race: push arrives, table row lands later."""
        db = Db(tmp_path)
        clock = FakeClock()
        engine = fresh_engine(db, clock)

        async def main():
            await engine.start()
            engine.on_subagent_start(start_payload())
            await asyncio.sleep(0.05)  # parks; nothing emitted yet
            db.insert_running(
                "deleg_bbbb8888", task={"goal": "late row", "names": ["late"]}
            )
            engine._tick()  # row claims the parked start
            events = await drain(engine, expect=1)
            await engine.stop()
            return events

        (event,) = asyncio.run(main())
        assert event.kind == "add"
        assert event.delegation_id == "deleg_bbbb8888"
        assert event.source == "hook"
        assert event.child_session_id == "sess-child-1"
        snap = engine.snapshot()
        assert snap["deleg_bbbb8888/0"]["from_table"] is True

    def test_unclaimed_hook_expires_to_synthetic_then_rekeys(self, tmp_path):
        db = Db(tmp_path)
        clock = FakeClock()
        engine = fresh_engine(db, clock)

        async def main():
            await engine.start()
            engine.on_subagent_start(
                start_payload(child_session_id="sess-late", child_goal="orphan")
            )
            await asyncio.sleep(0.05)  # let the hook land before ticking
            engine._tick()  # parked, not expired yet
            clock.advance(1000.0)  # past the grace
            engine._tick()  # expires → synthetic add
            synthetic = await drain(engine, expect=1)
            # The row finally shows up: synthetic re-keys to the real id.
            db.insert_running("deleg_cccc9999", task={"goal": "very late"})
            engine._tick()
            rest = await drain(engine, expect=2)
            await engine.stop()
            return synthetic, rest

        synthetic, rest = asyncio.run(main())
        assert synthetic[0].delegation_id == "hook:sess-late"
        death, add = rest
        assert death.kind == "death"
        assert death.delegation_id == "hook:sess-late"
        assert death.status == "reattributed"
        assert add.kind == "add"
        assert add.delegation_id == "deleg_cccc9999"
        assert list(engine.snapshot()) == ["deleg_cccc9999/0"]

    def test_ambiguous_parent_index_never_cross_claims(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_1a1a1a1a", task={"goal": "one"})
        db.insert_running("deleg_2b2b2b2b", task={"goal": "two"})
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            await drain(engine, expect=2)
            engine.on_subagent_start(start_payload())
            await asyncio.sleep(0.05)
            await engine.stop()

        asyncio.run(main())
        # Two live (parent, 0) table nodes: the hook must not guess.
        assert engine.snapshot()["deleg_1a1a1a1a/0"]["child_session_id"] is None
        assert engine.snapshot()["deleg_2b2b2b2b/0"]["child_session_id"] is None


# ---------------------------------------------------------------------------
# Death ordering races
# ---------------------------------------------------------------------------


    def test_stop_before_start_buffers_then_adds_and_kills(self, tmp_path):
        db = Db(tmp_path)
        clock = FakeClock()
        engine = fresh_engine(db, clock)

        async def main():
            await engine.start()
            engine.on_subagent_stop(stop_payload())  # child not seen yet
            await asyncio.sleep(0.05)
            engine.on_subagent_start(start_payload())
            await asyncio.sleep(0.05)  # start parks; nothing emitted yet
            clock.advance(1000.0)  # past the grace
            engine._tick()  # expires → synthetic add, buffered stop kills it
            events = await drain(engine, expect=2)
            await engine.stop()
            return events

        events = asyncio.run(main())
        add, death = events
        assert add.kind == "add"
        assert add.delegation_id == "hook:sess-child-1"
        assert death.kind == "death"
        assert death.status == "completed"
        assert death.summary == "all done"
        assert death.delegation_id == add.delegation_id
        assert death.seq > add.seq  # add precedes its buffered death

    def test_poll_death_then_hook_stop_single_death(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_abab1212", task={"goal": "g"})
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            await drain(engine, expect=1)
            db.set_state("deleg_abab1212", "failed")
            engine._tick()
            deaths = await drain(engine, expect=1)
            engine.on_subagent_stop(stop_payload(child_session_id=None))
            await asyncio.sleep(0.05)
            await engine.stop()
            return deaths

        (death,) = asyncio.run(main())
        assert death.kind == "death"
        assert death.status == "failed"
        assert death.source == "poll"
        assert engine.snapshot() == {}

    def test_hook_stop_then_poll_terminal_single_death(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_cdcd3434", task={"goal": "g"})
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            await drain(engine, expect=1)
            engine.on_subagent_start(start_payload())
            await asyncio.sleep(0.05)
            engine.on_subagent_stop(
                stop_payload(child_status="failed", child_summary="boom")
            )
            deaths = await drain(engine, expect=1)
            db.set_state("deleg_cdcd3434", "completed")
            engine._tick()
            await asyncio.sleep(0.05)
            await engine.stop()
            return deaths

        (death,) = asyncio.run(main())
        assert death.status == "failed"
        assert death.summary == "boom"
        assert death.source == "hook"
        assert engine.snapshot() == {}


# ---------------------------------------------------------------------------
# Engine lifecycle
# ---------------------------------------------------------------------------


class TestEngineLifecycle:
    def test_hooks_before_start_are_replayed_in_order(self, tmp_path):
        db = Db(tmp_path)
        engine = fresh_engine(db, FakeClock())
        engine.on_subagent_start(
            start_payload(delegation_id="deleg_efef5656", child_goal="early")
        )

        async def main():
            await engine.start()
            events = await drain(engine, expect=1)
            await engine.stop()
            return events

        (event,) = asyncio.run(main())
        assert event.delegation_id == "deleg_efef5656"
        assert event.goal == "early"

    def test_real_poll_loop_discovers_and_reaps(self, tmp_path):
        db = Db(tmp_path)
        db.insert_running("deleg_98987878", task={"goal": "loop"})
        engine = DiscoveryEngine(db.path, poll_interval=0.05, claim_grace=0.5)

        async def main():
            await engine.start()
            got = []
            async for event in engine.stream():
                got.append(event)
                if event.kind == "death":
                    break
            return got

        async def driver():
            await asyncio.sleep(0.2)
            db.set_state(
                "deleg_98987878",
                "completed",
                result_json={"summary": "done", "status": "completed"},
            )
            return None

        async def runner():
            results = await asyncio.gather(main(), driver())
            await engine.stop()
            return results[0]

        events = asyncio.run(runner())
        kinds = [(e.kind, e.status, e.summary) for e in events]
        assert kinds == [("add", "running", None), ("death", "completed", "done")]

    def test_synthetic_nodes_are_not_reaped_by_row_vanish(self, tmp_path):
        db = Db(tmp_path)
        clock = FakeClock()
        engine = fresh_engine(db, clock)

        async def main():
            await engine.start()
            engine.on_subagent_start(
                start_payload(child_session_id="sess-sync", delegation_id=None)
            )
            await asyncio.sleep(0.05)  # let the hook park before ticking
            clock.advance(1000.0)
            engine._tick()  # expire → synthetic add
            await drain(engine, expect=1)
            engine._tick()  # empty table sweep must not reap it
            await asyncio.sleep(0.05)
            await engine.stop()

        asyncio.run(main())
        assert list(engine.snapshot()) == ["hook:sess-sync/0"]


class TestPayloadParsing:
    def test_sa_id_and_missing_ids_default_index_zero(self):
        from observatory.discovery import _parse_task_index

        assert _parse_task_index({"child_subagent_id": "sa-3-abcdef12"}) == 3
        assert _parse_task_index({"child_subagent_id": "weird"}) == 0
        assert _parse_task_index({"task_index": "7"}) == 7
        assert _parse_task_index({}) == 0

    def test_bad_task_json_and_result_json_are_tolerated(self, tmp_path):
        db = Db(tmp_path)
        conn = sqlite3.connect(db.path)
        conn.execute(
            """INSERT INTO async_delegations
               (delegation_id, origin_session, parent_session_id, state,
                dispatched_at, updated_at, task_json, result_json)
               VALUES ('deleg_77770000', '', NULL, 'running', 1.0, 1.0,
                       'not-json', '{"broken"')"""
        )
        conn.commit()
        conn.close()
        engine = fresh_engine(db, FakeClock())

        async def main():
            await engine.start()
            engine._tick()
            events = await drain(engine, expect=1)
            db.set_state("deleg_77770000", "completed", result_json="also-not-json")
            engine._tick()
            events += await drain(engine, expect=1)
            await engine.stop()
            return events

        add, death = asyncio.run(main())
        assert add.name == "task-0"
        assert add.goal == ""
        assert death.status == "completed"
