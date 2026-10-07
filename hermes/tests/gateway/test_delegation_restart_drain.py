"""Background children count as real restart work, even with no chat turn."""
import threading
import time

import pytest

from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner
from tools import async_delegation as ad


@pytest.mark.asyncio
async def test_background_child_finishes_inside_bounded_drain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    release = threading.Event()
    finished = threading.Event()
    runner, _ = make_restart_runner()
    runner._restart_requested = True
    try:
        def work():
            release.wait(2)
            finished.set()
            return {"status": "completed", "summary": "done during drain"}
        ad.dispatch_async_delegation(goal="fixture", context="saved", toolsets=None,
            role="leaf", model="fixture/model", session_key="route", runner=work)
        assert runner._active_work_count() >= 1
        timer = threading.Timer(0.05, release.set)
        timer.start()
        _, timed_out = await runner._drain_active_agents(0.5)
        timer.join()
        assert not timed_out
        assert finished.is_set()
    finally:
        release.set()
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_child_drain_deadline_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    release = threading.Event()
    runner, _ = make_restart_runner()
    try:
        ad.dispatch_async_delegation(goal="fixture", context=None, toolsets=None,
            role="leaf", model="fixture/model", session_key="route",
            runner=lambda: (release.wait(2), {"status": "completed"})[1])
        started = time.monotonic()
        _, timed_out = await runner._drain_active_agents(0.05)
        assert timed_out
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        time.sleep(0.02)
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_restart_request_checkpoints_before_after_turn_wait(tmp_path, monkeypatch):
    import asyncio
    import json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    release = threading.Event()
    runner, _ = make_restart_runner()
    try:
        handle = ad.dispatch_async_delegation(goal="fixture", context="saved original goal",
            toolsets=None, role="leaf", model="fixture/model", session_key="route",
            runner=lambda: (release.wait(2), {"status": "completed"})[1])
        assert runner.request_restart(after_turn_timeout=1)
        with ad._transaction() as db:
            task = json.loads(db.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                                        (handle["delegation_id"],)).fetchone()[0])
        assert task["restart_checkpoint"]["reason"] == "planned gateway restart"
    finally:
        task = getattr(runner, "_restart_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        release.set()
        time.sleep(0.02)
        ad._reset_for_tests()
