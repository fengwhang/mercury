"""Gateway service barriers consume registered native execution ownership."""
import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from gateway import restart_owners as owners
from gateway.status import get_process_start_time
from tests.gateway.restart_test_helpers import make_restart_runner


def register_fixture(tmp_path, activity, detach):
    path = tmp_path / 'native-checkpoint.json'

    def checkpoint(reason):
        path.write_text(json.dumps({'goal': 'original peer goal', 'reason': reason,
                                    'active_work': activity['work'], 'accepted': ['receipt-one']}))
        path.chmod(0o600)
        return path

    return owners.register_owner('fixture-native-scope', profile_home=tmp_path,
        pid=os.getpid(), started_at=get_process_start_time(os.getpid()),
        active_work=lambda: activity['work'], checkpoint=checkpoint, detach=detach)


@pytest.mark.asyncio
async def test_automatic_restart_waits_for_native_peer_work_without_hermes_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner, _ = make_restart_runner()
    runner.stop = AsyncMock()
    runner._restart_after_turn_timeout = 0
    activity = {'work': 1}
    token = register_fixture(tmp_path, activity, lambda: None)
    try:
        assert runner._running_agent_count() == 0
        assert runner._active_work_count() == 1
        assert runner.request_restart(automatic=True, via_service=True,
                                      trigger='control:restart-when-idle')
        receipts = owners.pending_checkpoints(tmp_path)
        assert receipts[0]['owner_id'] == 'fixture-native-scope'
        await asyncio.sleep(0.15)
        runner.stop.assert_not_awaited()
        assert not runner._draining
        activity['work'] = 0
        await asyncio.wait_for(runner._restart_task, 2)
        runner.stop.assert_awaited_once()
    finally:
        task = getattr(runner, '_restart_task', None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        owners.unregister_owner(token)
        owners.discard_checkpoint('fixture-native-scope', profile_home=tmp_path)


@pytest.mark.asyncio
async def test_admin_shutdown_checkpoints_before_native_detach_and_exit_signal(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.run._hermes_home', tmp_path)
    runner, _ = make_restart_runner()
    detached = []
    activity = {'work': 0}

    def detach():
        assert not runner._shutdown_event.is_set()
        receipt = owners.pending_checkpoints(tmp_path)[0]
        assert receipt['gateway_pid'] == os.getpid()
        detached.append(receipt['sha256'])

    token = register_fixture(tmp_path, activity, detach)
    try:
        await runner.stop(restart=True, service_restart=True)
        assert len(detached) == 1
        assert runner._shutdown_event.is_set()
        assert (tmp_path / '.clean_shutdown').exists()
        final = owners.pending_checkpoints(tmp_path)[0]
        with open(final["checkpoint_path"]) as handle:
            restored = json.load(handle)
        assert restored["goal"] == "original peer goal"
        assert restored["accepted"] == ["receipt-one"]
    finally:
        owners.unregister_owner(token)
        owners.discard_checkpoint('fixture-native-scope', profile_home=tmp_path)


@pytest.mark.asyncio
async def test_native_checkpoint_failure_refuses_restart_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner, _ = make_restart_runner()

    def checkpoint(reason):
        raise OSError('fixture native durability barrier failed')

    token = owners.register_owner('broken-native-scope', profile_home=tmp_path,
        pid=os.getpid(), started_at=get_process_start_time(os.getpid()),
        active_work=lambda: 0, checkpoint=checkpoint, detach=lambda: None)
    try:
        assert runner.request_restart(trigger='admin:restart') is False
        assert not runner._restart_requested
        assert not runner._restart_task_started
        assert not runner._draining
    finally:
        task = getattr(runner, '_restart_task', None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        owners.unregister_owner(token)


@pytest.mark.asyncio
async def test_late_stop_admission_failure_keeps_gateway_feed_and_guards_alive(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, _ = make_restart_runner()
    runner._restart_after_turn_timeout = 0
    runner._stop_loop_liveness_guards = Mock()
    calls = []
    detached = []
    path = tmp_path / "late-checkpoint.json"

    def checkpoint(reason):
        calls.append(reason)
        if len(calls) > 1:
            raise OSError("fixture late native checkpoint refusal")
        path.write_text('{"goal":"original goal remains usable"}')
        path.chmod(0o600)
        return path

    token = owners.register_owner("late-native-scope", profile_home=tmp_path,
        pid=os.getpid(), started_at=get_process_start_time(os.getpid()),
        active_work=lambda: 0, checkpoint=checkpoint, detach=lambda: detached.append(True))
    try:
        assert runner.request_restart(automatic=True, via_service=True, trigger="control:restart-when-idle")
        await asyncio.wait_for(runner._restart_task, 2)
        assert runner._running
        assert not runner._shutdown_event.is_set()
        assert not runner._draining
        assert not runner._restart_requested
        assert not runner._restart_task_started
        runner._stop_loop_liveness_guards.assert_not_called()
        assert detached == []
        assert runner.adapters
    finally:
        owners.unregister_owner(token)
        owners.discard_checkpoint("late-native-scope", profile_home=tmp_path)
