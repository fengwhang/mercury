"""Restart contracts exercised only against temporary runtime homes."""
import json
import os
import queue
import sys
import time

import pytest

from tools import async_delegation as ad
from gateway.status import get_process_start_time


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def dispatch_row(delegation_id="durable", **extra):
    record = {"delegation_id": delegation_id, "goal": "finish verified artifact",
              "context": "preserve existing edits", "session_key": "parent-route",
              "parent_session_id": "parent-transcript", "origin_session_id": "api-parent",
              "dispatched_at": time.time(), **extra}
    ad._persist_dispatch(record)
    return record


def dead_owner(delegation_id="durable"):
    with ad._transaction() as db:
        db.execute("UPDATE async_delegations SET owner_pid=?, owner_started_at=? WHERE delegation_id=?",
                   (os.getpid(), -1, delegation_id))


def test_real_child_records_result_before_return(tmp_path, monkeypatch):
    from tools import omp_delegation as od
    child = tmp_path / "fixture-omp"
    child.write_text(f"#!{sys.executable}\nprint('verified fixture result')\n")
    child.chmod(0o700)
    monkeypatch.setenv("HERMES_OMP_TRANSPORT", "oneshot")
    monkeypatch.setattr(od, "_resolve_omp_binary", lambda: str(child))
    dispatch_row()
    entry = od._run_omp_task(0, "fixture goal", "fixture/model", str(tmp_path), 5, None,
                             delegation_id="durable", name="Fixture", goal="fixture goal")
    assert entry["status"] == "completed"
    children = ad.list_delegation_children("durable")
    assert len(children) == 1
    assert children[0]["status"] == "completed"
    assert children[0]["summary"] == "verified fixture result"
    assert children[0]["child_started_at"] is not None


def test_recovery_preserves_completed_child_and_parent_route():
    dispatch_row()
    ad.record_child_spawn("durable/0", "durable", name="Fixture", goal="verify")
    ad.record_child_terminal("durable/0", "completed", summary="real terminal result")
    dead_owner()
    pending = queue.Queue()
    assert ad.restore_undelivered_completions(pending) == 1
    event = pending.get_nowait()
    assert event["status"] == "completed"
    assert "real terminal result" in event["summary"]
    assert event["parent_session_id"] == "parent-transcript"
    assert event["session_key"] == "parent-route"
    assert event["origin_session_id"] == "api-parent"
    claim = ad.claim_event_delivery(event, "fixture")
    assert claim
    ad.complete_event_delivery(event, claim)
    assert ad.restore_undelivered_completions(pending) == 0
    assert pending.empty()


def test_dispose_marker_is_not_task_success(tmp_path):
    transcript = tmp_path / "child.jsonl"
    transcript.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "partial edits only"}]}}) +
        "\n" + json.dumps({"type": "custom", "customType": "session_exit"}) + "\n")
    dispatch_row()
    ad.record_child_spawn("durable/0", "durable", name="Fixture", goal="finish task",
                          child_pid=os.getpid(), child_started_at=-1, session_file=str(transcript))
    dead_owner()
    pending = queue.Queue()
    ad.restore_undelivered_completions(pending)
    event = pending.get_nowait()
    assert event["status"] == "interrupted"
    result = event["results"][0]
    assert result["summary"] is None
    assert result["recovery"]["session_file"] == str(transcript)
    assert result["recovery"]["goal"] == "finish task"
    assert transcript.read_text().endswith('"session_exit"}\n')


def test_missing_start_identity_never_proves_liveness():
    assert ad.child_process_alive(os.getpid(), None) is False


def test_partial_batch_is_not_completed():
    dispatch_row(goals=["one", "two"], names=["One", "Two"], is_batch=True)
    for i in range(2):
        ad.record_child_spawn(f"durable/{i}", "durable", i, goal=str(i))
    ad.record_child_terminal("durable/0", "completed", summary="one done")
    ad.record_child_terminal("durable/1", "failed", error="real failure")
    dead_owner()
    pending = queue.Queue()
    ad.restore_undelivered_completions(pending)
    assert pending.get_nowait()["status"] == "failed"


def test_checkpoint_preserves_prompt_workdir_and_terminal_result(tmp_path, monkeypatch):
    from tools import omp_delegation as od
    child = tmp_path / "fixture-omp"
    child.write_text(f"#!{sys.executable}\nprint('terminal fixture')\n")
    child.chmod(0o700)
    monkeypatch.setenv("HERMES_OMP_TRANSPORT", "oneshot")
    monkeypatch.setattr(od, "_resolve_omp_binary", lambda: str(child))
    dispatch_row()
    od._run_omp_task(0, "full frozen specification", "fixture/model", str(tmp_path), 5, None,
                     delegation_id="durable", name="Fixture", goal="finish task")
    saved = ad.list_delegation_children("durable")[0]
    assert saved["checkpoint"]["prompt"] == "full frozen specification"
    assert saved["checkpoint"]["workdir"] == str(tmp_path)
    assert saved["checkpoint"]["model"] == "fixture/model"
    ad.checkpoint_active_delegations("planned restart")
    assert ad.list_delegation_children("durable")[0]["status"] == "completed"


def test_recovery_delivery_contains_saved_resume_specification():
    from tools.process_registry import format_process_notification
    dispatch_row()
    ad.record_child_spawn("durable/0", "durable", goal="remaining goal",
                          child_pid=os.getpid(), child_started_at=-1)
    ad.record_child_checkpoint("durable/0", {"prompt": "frozen full prompt", "workdir": "/fixture/worktree"})
    dead_owner()
    pending = queue.Queue()
    ad.restore_undelivered_completions(pending)
    text = format_process_notification(pending.get_nowait())
    assert "frozen full prompt" in text
    assert "/fixture/worktree" in text
    assert "Do not replay completed side effects" in text
    assert "status=interrupted" in text


def test_dead_delivery_owner_releases_claim_without_age_delay():
    dispatch_row()
    ad._persist_completion({"delegation_id": "durable", "status": "completed",
                            "completed_at": time.time()}, {"summary": "done"})
    assert ad.claim_completion_delivery("durable", "old-consumer")
    with ad._transaction() as db:
        db.execute("UPDATE async_delegations SET delivery_owner_started_at=-1 WHERE delegation_id='durable'")
    assert ad.claim_completion_delivery("durable", "new-consumer")
    assert ad.complete_completion_delivery("durable", "new-consumer")


def test_running_delivery_owner_not_stolen_by_claim_age():
    dispatch_row()
    ad._persist_completion({"delegation_id": "durable", "status": "completed",
                            "completed_at": time.time()}, {"summary": "done"})
    assert ad.claim_completion_delivery("durable", "first")
    with ad._transaction() as db:
        db.execute("UPDATE async_delegations SET delivery_claimed_at=0 WHERE delegation_id='durable'")
    assert not ad.claim_completion_delivery("durable", "second")


def test_reaper_uses_terminal_ledger_not_live_row_age(tmp_path):
    from observatory.room_reaper import reap_orphan_rooms
    from observatory.state import ObservatoryState, StateError
    from tests.observatory.test_subagent_room_lifecycle import _seed
    state = ObservatoryState(tmp_path / "observatory" / "state.db")
    try:
        _seed(state, "root", depth=0, room="#fixture-root", parent=None)
        _seed(state, "durable/0", depth=1, room="#fixture-done", parent="root")
        _seed(state, "durable/1", depth=1, room="#fixture-running", parent="root")
        _seed(state, "deep", depth=2, room="#fixture-deep", parent="durable/1")
        dispatch_row()
        ad.record_child_spawn("durable/0", "durable")
        ad.record_child_terminal("durable/0", "completed", summary="real result")
        ad.record_child_spawn("durable/1", "durable", 1, child_pid=os.getpid(),
                              child_started_at=get_process_start_time(os.getpid()))
        state.update_extra("root", task_state="completed")
        result = reap_orphan_rooms(state, mercury_home=tmp_path)
        assert result["rows_purged"] == ["durable/0"]
        assert {r["node_id"] for r in state.get_live()} == {"root", "durable/1", "deep"}
    finally:
        state.close()


def test_recovery_uses_final_stop_message_not_process_dispose(tmp_path):
    transcript = tmp_path / "terminal.jsonl"
    summary = "verified artifact\n" + "full result " * 200
    transcript.write_text(json.dumps({"type": "message", "message": {
        "role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": summary}]}}) + "\n")
    dispatch_row()
    ad.record_child_spawn("durable/0", "durable", child_pid=os.getpid(),
                          child_started_at=-1, session_file=str(transcript))
    dead_owner()
    pending = queue.Queue()
    ad.restore_undelivered_completions(pending)
    event = pending.get_nowait()
    assert event["status"] == "completed"
    assert event["results"][0]["summary"] == summary


def test_new_input_after_final_message_is_not_completed(tmp_path):
    transcript = tmp_path / "new-turn.jsonl"
    transcript.write_text(
        json.dumps({"type": "message", "message": {"role": "assistant", "stopReason": "stop",
             "content": [{"type": "text", "text": "previous turn done"}]}}) + "\n" +
        json.dumps({"type": "message", "message": {"role": "user", "content": "remaining task"}}) + "\n")
    dispatch_row()
    ad.record_child_spawn("durable/0", "durable", child_pid=os.getpid(),
                          child_started_at=-1, session_file=str(transcript))
    dead_owner()
    pending = queue.Queue()
    ad.restore_undelivered_completions(pending)
    assert pending.get_nowait()["status"] == "interrupted"


def test_terminal_first_wins_and_clears_restart_pending():
    dispatch_row()
    assert ad.checkpoint_active_delegations("planned restart") == 1
    with ad._transaction() as db:
        assert "restart_checkpoint" in json.loads(db.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id='durable'").fetchone()[0])
    ad._persist_completion({"delegation_id": "durable", "status": "completed"}, {"summary": "done"})
    ad.mark_completion_delivered("durable")
    ad._persist_completion({"delegation_id": "durable", "status": "interrupted"}, {"error": "late stop"})
    row = ad.get_durable_delegation("durable")
    assert row["state"] == "completed"
    assert row["delivery_state"] == "delivered"
    assert row["result"]["summary"] == "done"
    with ad._transaction() as db:
        assert "restart_checkpoint" not in json.loads(db.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id='durable'").fetchone()[0])
