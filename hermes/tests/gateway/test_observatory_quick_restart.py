"""Observatory restart bypasses after-turn deferral without a second hard restart."""
import asyncio
import os

import pytest

from gateway.control_socket import GatewayControlServer, query_gateway_control
from observatory.restart import quick_restart_handler
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.mark.asyncio
async def test_control_restart_expedites_an_already_deferred_turn(tmp_path, monkeypatch):
    runner, _ = make_restart_runner()
    runner._active_work_count = lambda: 1
    runner._awaitable_work_count = lambda: 1
    runner._wedged_agent_count = lambda: 0
    runner._restart_after_turn_timeout = 1800
    stopped = asyncio.Event()

    async def stop(**kwargs):
        assert kwargs == {"restart": True, "detached_restart": False, "service_restart": True}
        stopped.set()

    runner.stop = stop
    assert runner.request_restart(via_service=True)
    await asyncio.sleep(0)
    server = GatewayControlServer(home=tmp_path, verb_handlers={
        "restart-observatory": quick_restart_handler(runner, asyncio.get_running_loop()),
    })
    try:
        assert await server.start()
        answer = await asyncio.to_thread(query_gateway_control, tmp_path, "restart-observatory")
        assert answer == {"pid": os.getpid(), "restarting": False, "already_stopping": True}
        await asyncio.wait_for(stopped.wait(), 5)
        await runner._restart_task
        # A future ordinary restart still reads its own configured timeout;
        # this request has not written a persistent permission/model setting.
        assert not (tmp_path / "config.yaml").exists()
    finally:
        await server.stop()
        if not runner._restart_task.done():
            runner._restart_task.cancel()
            await asyncio.gather(runner._restart_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_systemd_quick_restart_uses_live_control_and_observes_one_replacement(tmp_path, monkeypatch):
    from mercury_cli import gateway

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, _ = make_restart_runner()
    runner.request_restart = lambda **kwargs: kwargs == {
        "detached": False, "via_service": True, "after_turn_timeout": 0.0,
    }
    server = GatewayControlServer(home=tmp_path, verb_handlers={
        "restart-observatory": quick_restart_handler(runner, asyncio.get_running_loop()),
    })
    for name in ("_preflight_user_systemd", "_require_service_installed", "refresh_systemd_unit_if_needed"):
        monkeypatch.setattr(gateway, name, lambda *a, **k: None)
    monkeypatch.setattr(gateway, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: os.getpid())
    observed = []
    monkeypatch.setattr(gateway, "_wait_for_systemd_service_restart", lambda **kw: observed.append(kw) or True)
    # Calling a service command here would be a bug; there is already one
    # restart owner, the gateway's session-aware shutdown path.
    def refuse_systemctl(*args, **kwargs):
        raise AssertionError("Quick control restart must not hard-restart the service")
    monkeypatch.setattr(gateway, "_run_systemctl", refuse_systemctl)
    try:
        assert await server.start()
        await asyncio.to_thread(gateway.systemd_restart, quick=True)
        assert observed == [{"system": False, "previous_pid": os.getpid(), "timeout": 90}]
    finally:
        await server.stop()


def test_quick_restart_rejects_control_reply_for_other_pid(monkeypatch, tmp_path):
    from mercury_cli import gateway

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", lambda *a, **k: {
        "pid": 2, "restarting": True,
    })
    assert not gateway._request_gateway_quick_restart(1)


def test_legacy_gateway_quick_restart_has_bounded_grace(monkeypatch):
    from mercury_cli import gateway

    for name in ("_preflight_user_systemd", "_require_service_installed", "refresh_systemd_unit_if_needed"):
        monkeypatch.setattr(gateway, name, lambda *a, **k: None)
    monkeypatch.setattr(gateway, "_select_systemd_scope", lambda _system: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 101)
    monkeypatch.setattr(gateway, "_request_gateway_quick_restart", lambda _pid: False)
    monkeypatch.setattr(gateway, "probe_gateway_loop_liveness", lambda _pid: "alive")
    monkeypatch.setattr(gateway, "_get_restart_exit_wait_budget", lambda: 1815)
    waits = []
    monkeypatch.setattr(gateway, "_graceful_restart_via_sigusr1", lambda pid, timeout: waits.append(timeout) or True)
    monkeypatch.setattr(gateway, "_wait_for_systemd_service_restart", lambda **kw: True)
    gateway.systemd_restart(quick=True)
    assert waits == [10]
