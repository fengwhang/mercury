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


@pytest.mark.asyncio
async def test_automatic_restart_defers_background_child_without_draining(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    release = threading.Event()
    runner, _ = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 0
    try:
        ad.dispatch_async_delegation(goal="original goal still running", context=None,
            toolsets=None, role="leaf", model="fixture/model", session_key="route",
            runner=lambda: (release.wait(2), {"status": "completed"})[1])
        assert runner._running_agent_count() == 0
        assert runner.request_restart(via_service=True, automatic=True,
                                      trigger="control:restart-when-idle")
        await asyncio.sleep(0.15)
        runner.stop.assert_not_awaited()
        assert not runner._draining, "deferred convenience restart must keep parent/feed usable"
        assert ad.active_count() == 1
        release.set()
        await asyncio.wait_for(runner._restart_task, timeout=2)
        runner.stop.assert_awaited_once_with(
            restart=True, detached_restart=False, service_restart=True)
    finally:
        release.set()
        task = getattr(runner, "_restart_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        time.sleep(0.02)
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_explicit_admin_restart_keeps_bounded_checkpoint_drain(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    release = threading.Event()
    runner, _ = make_restart_runner()
    runner._restart_after_turn_timeout = 0
    runner.stop = AsyncMock()
    try:
        ad.dispatch_async_delegation(goal="checkpoint before admin stop", context=None,
            toolsets=None, role="leaf", model="fixture/model", session_key="route",
            runner=lambda: (release.wait(2), {"status": "completed"})[1])
        assert runner.request_restart(via_service=True, trigger="admin:restart")
        await asyncio.wait_for(runner._restart_task, timeout=1)
        runner.stop.assert_awaited_once()
        assert ad.active_count() == 1
    finally:
        release.set()
        time.sleep(0.02)
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_clean_exit_barrier_checks_unsettled_children_not_only_turn_drain(tmp_path, monkeypatch):
    import json
    from unittest.mock import AsyncMock
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    release = threading.Event()
    runner, _ = make_restart_runner()
    # Replay the reported active_agents=0 drain seam. The clean-exit barrier
    # must independently see the running background owner, not trust this.
    runner._drain_active_agents = AsyncMock(return_value=({}, False))
    try:
        handle = ad.dispatch_async_delegation(goal="saved unfinished child", context=None,
            toolsets=None, role="leaf", model="fixture/model", session_key="route",
            interrupt_fn=lambda: None,
            runner=lambda: (release.wait(5), {"status": "completed"})[1])
        await runner.stop(restart=True, service_restart=True)
        assert not (tmp_path / ".clean_shutdown").exists()
        with ad._transaction() as db:
            task = json.loads(db.execute(
                "SELECT task_json FROM async_delegations WHERE delegation_id=?",
                (handle["delegation_id"],)).fetchone()[0])
        assert task["restart_checkpoint"]["reason"] == "gateway clean-exit barrier"
        assert ad.active_count() == 1
    finally:
        release.set()
        time.sleep(0.02)
        ad._reset_for_tests()


@pytest.mark.asyncio
async def test_shutdown_signal_follows_durable_exit_barrier(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    ad._reset_for_tests()
    runner, _ = make_restart_runner()
    observed = []
    original = ad.checkpoint_active_delegations

    def checkpoint(reason):
        if reason == "gateway clean-exit barrier":
            observed.append(runner._shutdown_event.is_set())
        return original(reason)

    monkeypatch.setattr(ad, "checkpoint_active_delegations", checkpoint)
    await runner.stop(restart=True, service_restart=True)
    assert observed == [False], "run_forever must not exit/cancel teardown before durability"
    assert runner._shutdown_event.is_set()


@pytest.mark.asyncio
async def test_restart_receipt_is_durable_before_acceptance_flags(tmp_path, monkeypatch):
    import asyncio
    import json

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, _ = make_restart_runner()
    from gateway import restart_provenance as provenance
    record = provenance.record_restart_request
    observed = []

    def persist(**kwargs):
        observed.append(runner._restart_requested)
        return record(**kwargs)

    monkeypatch.setattr(provenance, "record_restart_request", persist)
    try:
        assert runner.request_restart(trigger="signal:SIGUSR1", automatic=True,
                                      actor={"authentication": "unknown", "signal": "SIGUSR1"},
                                      request_id="receipt-before-flags")
        rows = [json.loads(line) for line in
                (tmp_path / "logs" / "gateway-restart-requests.jsonl").read_text().splitlines()]
        assert observed == [False]
        assert rows[0]["source"] == "signal"
        assert rows[0]["actor"]["authentication"] == "unknown"
        assert rows[0]["request_id"] == "receipt-before-flags"
        assert rows[1]["state"] == "accepted"
    finally:
        task = getattr(runner, "_restart_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_restart_provenance_failure_refuses_acceptance(tmp_path, monkeypatch):
    from gateway import restart_provenance as provenance

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, _ = make_restart_runner()

    def unavailable(**kwargs):
        raise OSError("fixture persistence unavailable")

    monkeypatch.setattr(provenance, "record_restart_request", unavailable)
    assert runner.request_restart(trigger="admin:restart") is False
    assert not runner._restart_requested
    assert not runner._restart_task_started
    assert not runner._draining


@pytest.mark.asyncio
async def test_automatic_restart_counts_real_native_worker_without_async_parent(tmp_path, monkeypatch):
    import asyncio
    import os
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools import omp_delegation as omp

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    runner, _ = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 0
    transport = SimpleNamespace(pid=os.getpid())
    omp._register_live_child({"child_id": "local-native-guard/0"}, transport)
    try:
        assert ad.active_count() == 0
        assert runner._running_agent_count() == 0
        assert runner._active_work_count() > 0
        assert runner.request_restart(automatic=True, via_service=True, trigger="control:restart-when-idle")
        await asyncio.sleep(0.15)
        runner.stop.assert_not_awaited()
        omp._unregister_live_child("local-native-guard/0", transport)
        await asyncio.wait_for(runner._restart_task, timeout=2)
        runner.stop.assert_awaited_once()
    finally:
        omp._unregister_live_child("local-native-guard/0", transport)
        task = getattr(runner, "_restart_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
