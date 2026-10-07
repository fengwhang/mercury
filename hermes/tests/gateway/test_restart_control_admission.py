"""Live control admission counts background work, not stale active_agents."""
import asyncio
import os
from unittest.mock import Mock

import pytest

from gateway.control_socket import GatewayControlServer, query_gateway_control


@pytest.mark.asyncio
async def test_live_socket_defers_update_and_queues_only_automatic_restart(tmp_path, monkeypatch):
    from gateway.control_socket import restart_control_handlers

    active = {"work": 1}
    runner = Mock()
    runner._active_work_count.side_effect = lambda: active["work"]
    runner._active_delegation_count.return_value = 1
    runner._running_agent_count.return_value = 0
    runner._restart_drain_timeout = 1.0
    runner.request_restart.return_value = True
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda: True)
    handlers = restart_control_handlers(runner, asyncio.get_running_loop())
    server = GatewayControlServer(home=tmp_path, verb_handlers=handlers)
    server.register_handler("restart-when-idle", handlers["restart-when-idle"], takes_params=True)
    assert await server.start()
    try:
        async def query(verb, **params):
            return await asyncio.to_thread(query_gateway_control, tmp_path, verb, params=params)

        status = await query("status")
        assert status["active_work"] == 1
        assert status["active_delegations"] == 1
        assert status["running_agents"] == 0
        pause = await query("pause-for-update")
        assert pause["deferred"] is True
        assert pause["pausing"] is False
        assert pause["already_stopping"] is False
        runner.request_restart.assert_not_called()
        queued = await query("restart-when-idle", trigger="webhook-refresh")
        assert queued["restarting"] is True and queued["deferred"] is True
        assert queued["pid"] == os.getpid()
        runner.request_restart.assert_called_once_with(
            detached=False, via_service=True, automatic=True,
            trigger="control:restart-when-idle:webhook-refresh")
        runner.request_restart.reset_mock()
        active["work"] = 0
        pause = await query("pause-for-update")
        assert pause["pausing"] is True and pause["deferred"] is False
        runner.request_restart.assert_called_once_with(
            detached=False, via_service=True, trigger="control:pause-for-update")
    finally:
        await server.stop()
