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
