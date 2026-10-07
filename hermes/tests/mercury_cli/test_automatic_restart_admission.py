"""Automatic restarts must remain gateway-owned, never administrative stops."""
from unittest.mock import Mock

import pytest

import mercury_cli.gateway as gateway


def test_background_delegate_defers_automatic_restart_with_no_parent_agent(monkeypatch, tmp_path, capsys):
    # The parent turn has ended; the gateway's live work count still includes
    # its background delegate. The consumer must trust admission, not agents.
    query = Mock(return_value={
        "restarting": True, "deferred": True, "pid": 4242,
        "active_agents": 0, "active_work": 1,
    })
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    kill = Mock(side_effect=AssertionError("automatic restart sent a signal"))
    service = Mock(side_effect=AssertionError("automatic restart forced service"))
    monkeypatch.setattr(gateway.os, "kill", kill)
    monkeypatch.setattr(gateway, "_run_systemctl", service)

    reply = gateway.request_automatic_gateway_restart(
        home=tmp_path, pid=4242, trigger="onboarding",
    )

    assert reply["restarting"] is True
    assert reply["deferred"] is True
    query.assert_called_once_with(
        tmp_path, "restart-when-idle", params={"trigger": "onboarding"}, timeout=6,
    )
    assert "deferred" in capsys.readouterr().out.lower()
    kill.assert_not_called()
    service.assert_not_called()


@pytest.mark.parametrize("response", [
    None, {}, {"restarting": True, "pid": 4242},
    {"restarting": True, "deferred": False, "pid": 9999},
])
def test_unsupported_or_wrong_gateway_fails_closed(monkeypatch, tmp_path, response):
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", Mock(return_value=response))
    monkeypatch.setattr(gateway.os, "kill", Mock(side_effect=AssertionError("sent signal")))
    monkeypatch.setattr(gateway, "_run_systemctl", Mock(side_effect=AssertionError("forced service")))
    reply = gateway.request_automatic_gateway_restart(home=tmp_path, pid=4242)
    assert reply["restarting"] is False
    assert reply["deferred"] is True


def test_idle_automatic_restart_is_admitted(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", Mock(return_value={
        "restarting": True, "deferred": False, "pid": 4242,
    }))
    reply = gateway.request_automatic_gateway_restart(home=tmp_path, pid=4242)
    assert reply == {"restarting": True, "deferred": False, "pid": 4242}
    assert "while idle" in capsys.readouterr().out


@pytest.mark.parametrize("helper,trigger", [
    ("_restart_gateway_after_webhook_enable", "webhook-enable"),
    ("_restart_gateway_after_whatsapp_onboarding", "whatsapp-onboarding"),
    ("_restart_gateway_after_telegram_onboarding", "telegram-onboarding"),
])
def test_onboarding_restart_reports_deferral_without_admin_spawn(monkeypatch, tmp_path, helper, trigger):
    import mercury_cli.web_server as web

    admission = Mock(return_value={"restarting": True, "deferred": True, "pid": 4242})
    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admission)
    monkeypatch.setattr(web, "_resolve_profile_dir", lambda _: tmp_path)
    spawn = Mock(side_effect=AssertionError("onboarding invoked administrative restart"))
    monkeypatch.setattr(web, "_spawn_gateway_restart", spawn)
    reply = getattr(web, helper)("work")
    assert reply["restart_started"] is False
    assert reply["restart_deferred"] is True
    assert reply["restart_queued"] is True
    assert reply["restart_pid"] == 4242
    admission.assert_called_once_with(home=tmp_path, trigger=trigger)
    spawn.assert_not_called()


@pytest.mark.parametrize("status", [
    {"pid": 4242, "active_agents": 0, "active_work": 1, "active_delegations": 1},
    {"pid": 4242, "active_agents": 0},
    None,
])
def test_busy_or_unsupported_gateway_aborts_update_before_mutation(monkeypatch, tmp_path, status):
    from argparse import Namespace
    from types import SimpleNamespace
    import mercury_cli.update_cmd as update

    process = SimpleNamespace(pid=4242, profile="work", path=tmp_path)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [process])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", Mock(return_value=status))
    mutation = Mock(side_effect=AssertionError("update reached mutation preparation"))
    monkeypatch.setattr(update._m(), "_capture_active_lazy_features", mutation)
    with pytest.raises(RuntimeError, match="Update deferred"):
        update._cmd_update_impl(Namespace(), gateway_mode=False)
    mutation.assert_not_called()


@pytest.mark.parametrize("ack", [
    {"pid": 4242, "pausing": False, "already_stopping": False, "deferred": True, "active_work": 1},
    {"pid": 4242, "pausing": True, "already_stopping": False, "drain_timeout": 10},
    None,
])
def test_update_pause_decline_never_writes_stop_marker_or_force_kills(monkeypatch, tmp_path, ack):
    from types import SimpleNamespace
    import mercury_cli.update_cmd as update

    process = SimpleNamespace(pid=4242, profile="work", path=tmp_path)
    monkeypatch.setattr(update._m(), "_is_windows", lambda: True)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [process])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    monkeypatch.setattr(gateway, "find_windows_gateway_services", lambda **_: [])
    monkeypatch.setattr("gateway.control_socket.pause_gateway_for_update", lambda *_: ack)
    marker = Mock()
    kill = Mock()
    launcher = Mock(side_effect=RuntimeError("unsafe fallback reached"))
    monkeypatch.setattr(update, "_write_update_planned_stop_marker", marker)
    monkeypatch.setattr("gateway.status.terminate_pid", kill)
    monkeypatch.setattr(update._m(), "_venv_launcher_ancestors", launcher)
    with pytest.raises(RuntimeError, match="Update deferred"):
        update._pause_windows_gateways_for_update()
    marker.assert_not_called()
    kill.assert_not_called()
    launcher.assert_not_called()


def test_setup_final_convenience_restart_queues_instead_of_admin_command(monkeypatch):
    import mercury_cli.setup as setup
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *_, **__: True)
    admission = Mock(return_value={"restarting": True, "deferred": True, "pid": 4242})
    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admission)
    spawn = Mock(side_effect=AssertionError("setup invoked admin command"))
    monkeypatch.setattr("subprocess.run", spawn)
    assert setup._restart_gateway("observatory configuration") is False
    admission.assert_called_once_with(trigger="setup-observatory")
    spawn.assert_not_called()


def test_pending_update_restart_only_queues_background_child_busy_gateway(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import mercury_cli.update_cmd as update

    process = SimpleNamespace(pid=4242, profile="work", path=tmp_path)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [process])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    monkeypatch.setattr(update._m(), "_purge_stale_hermes_modules", lambda: None)
    admission = Mock(return_value={"restarting": True, "deferred": True, "pid": 4242})
    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admission)
    forced = Mock(side_effect=AssertionError("catchup tried forced restart"))
    monkeypatch.setattr("subprocess.run", forced)
    monkeypatch.setattr(gateway, "kill_gateway_processes", forced)
    assert update._run_pending_fleet_restart() is False
    admission.assert_called_once_with(home=tmp_path, pid=4242, trigger="pending-update")
    forced.assert_not_called()


def test_fresh_abort_recovery_queues_without_spawning_admin_restart(monkeypatch, tmp_path):
    import mercury_cli.update_restart_recovery as recovery

    monkeypatch.setattr("mercury_cli.profiles.get_profile_dir", lambda _: tmp_path)
    admission = Mock(return_value={"restarting": True, "deferred": True, "pid": 4242})
    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admission)
    forced = Mock(side_effect=AssertionError("fresh recovery forced restart"))
    monkeypatch.setattr("subprocess.run", forced)
    reply = recovery.restart_profiles(["work"], supervisors={"work": "systemd"})
    assert reply["verified"] == []
    assert reply["relaunch_attempted"] == ["work"]
    assert reply["failed"] == []
    admission.assert_called_once_with(home=tmp_path, trigger="update-recovery")
    forced.assert_not_called()


def test_live_launchd_definition_refresh_never_bootouts_busy_gateway(monkeypatch, tmp_path):
    plist = tmp_path / "gateway.plist"
    plist.write_text("old")
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway, "launchd_plist_is_current", lambda: False)
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda: "new")
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.mercury.gateway")
    monkeypatch.setattr(gateway, "_launchd_domain", lambda: "gui/1000")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 4242)
    admission = Mock(return_value={"restarting": True, "deferred": True, "pid": 4242})
    monkeypatch.setattr(gateway, "request_automatic_gateway_restart", admission)
    force = Mock(side_effect=AssertionError("refresh tried service teardown"))
    monkeypatch.setattr(gateway.subprocess, "run", force)
    monkeypatch.setattr(gateway.subprocess, "Popen", force)
    assert gateway.refresh_launchd_plist_if_needed() is True
    admission.assert_called_once_with(pid=4242, trigger="launchd-definition-refresh")
    assert plist.read_text() == "new"
    force.assert_not_called()


def test_explicit_admin_restart_uses_bounded_authenticated_control_before_signal(monkeypatch):
    query = Mock(return_value={"restarting": True, "deferred": False, "pid": 4242})
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    signal = Mock(side_effect=AssertionError("admin control should precede compatibility signal"))
    monkeypatch.setattr(gateway.os, "kill", signal)
    wait = Mock(return_value=True)
    monkeypatch.setattr(gateway, "_wait_for_pid_exit", wait)
    assert gateway._graceful_restart_via_sigusr1(4242, 12) is True
    assert query.call_args.args[1] == "restart-admin"
    wait.assert_called_once_with(4242, 12)
    signal.assert_not_called()


def test_admin_admission_denial_never_uses_legacy_signal_fallback(monkeypatch):
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", Mock(return_value={
        "restarting": False, "deferred": True, "denied": True, "pid": 4242,
    }))
    kill = Mock(side_effect=AssertionError("denied request must not escalate"))
    monkeypatch.setattr(gateway.os, "kill", kill)
    with pytest.raises(RuntimeError, match="declined"):
        gateway._graceful_restart_via_sigusr1(4242, 12)
    kill.assert_not_called()


def test_quick_admin_explicitly_requests_checkpoint_resume(monkeypatch):
    query = Mock(return_value={"restarting": True, "deferred": False, "pid": 4242})
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    assert gateway._request_gateway_admin_restart(4242, checkpoint_resume=True) is True
    assert query.call_args.kwargs["params"] == {
        "trigger": "cli-admin-restart", "checkpoint_resume": True,
    }


def test_dashboard_refresh_does_not_stop_owner_of_new_active_delegate(monkeypatch):
    import mercury_cli.update_cmd as update
    monkeypatch.setattr(update, "_require_idle_gateways_for_update",
                        Mock(side_effect=RuntimeError("Update deferred: active delegate")))
    stop = Mock(side_effect=AssertionError("serve restart can kill its gateway child"))
    monkeypatch.setattr(update._m(), "_kill_stale_dashboard_processes", stop)
    with pytest.raises(RuntimeError, match="Update deferred"):
        update._finish_dashboard_update_cleanup([])
    stop.assert_not_called()


def test_default_profile_admission_maps_canonical_engine_home_not_metadata_root(monkeypatch, tmp_path):
    from types import SimpleNamespace

    engine_home = tmp_path / "hermes"
    monkeypatch.setattr("mercury_cli.profiles.list_profiles", lambda: [
        SimpleNamespace(name="default", path=tmp_path),
    ])
    monkeypatch.setattr("mercury_cli.profiles._get_default_hermes_dir", lambda: engine_home)
    monkeypatch.setattr("gateway.status.get_running_pid_identity_strict",
                        lambda path: (4242, 1.0) if path == engine_home / "gateway.pid" else None)
    processes = gateway.find_profile_gateway_processes(strict=True)
    assert len(processes) == 1
    assert processes[0].pid == 4242
    assert processes[0].path == engine_home


def test_windows_service_gateway_pause_decline_has_no_scm_stop_fallback(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import mercury_cli.update_cmd as update

    process = SimpleNamespace(pid=4242, profile="work", path=tmp_path)
    monkeypatch.setattr(update._m(), "_is_windows", lambda: True)
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda **_: [process])
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda **_: [4242])
    monkeypatch.setattr(gateway, "find_windows_gateway_services", lambda **_: [
        SimpleNamespace(gateway_pid=4242, name="MercuryGateway"),
    ])
    pause = Mock(return_value={"pid": 4242, "pausing": False, "deferred": True, "active_work": 1})
    monkeypatch.setattr("gateway.control_socket.pause_gateway_for_update", pause)
    stop = Mock(side_effect=AssertionError("busy service must not stop"))
    monkeypatch.setattr(update, "_stop_windows_gateway_service", stop)
    monkeypatch.setattr(update._m(), "_venv_launcher_ancestors",
                        Mock(side_effect=RuntimeError("unsafe service fallback reached")))
    with pytest.raises(RuntimeError, match="Update deferred"):
        update._pause_windows_gateways_for_update()
    pause.assert_called_once_with(tmp_path)
    stop.assert_not_called()


def test_resume_watcher_cannot_take_over_new_gateway_generation(monkeypatch):
    watcher = Mock(return_value=True)
    monkeypatch.setattr(gateway, "_spawn_gateway_restart_watcher", watcher)
    assert gateway.launch_detached_profile_gateway_restart("work", 4242) is True
    assert "--replace" not in watcher.call_args.args[1]


def test_update_refuses_new_gateway_holder_after_admitted_pause(monkeypatch):
    from argparse import Namespace
    import mercury_cli.update_cmd as update

    monkeypatch.setattr(update, "_require_idle_gateways_for_update", lambda: None)
    monkeypatch.setattr(update._m(), "_is_windows", lambda: True)
    monkeypatch.setattr(update._m(), "_venv_scripts_dir", lambda: None)
    monkeypatch.setattr(update._m(), "_run_pre_update_backup", lambda _: None)
    monkeypatch.setattr(update._m(), "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(update._m(), "_detect_venv_python_processes", lambda: [(4242, "python.exe", "gateway run")])
    monkeypatch.setattr(update._m(), "_leftover_pausable_gateway_pids", lambda _: [4242])
    monkeypatch.setattr(update, "_refuse_gateway_ancestor_tree_kill", lambda *a, **k: False)
    kill = Mock(side_effect=AssertionError("replacement gateway must not be tree-killed"))
    monkeypatch.setattr("gateway.status.terminate_pid", kill)
    with pytest.raises(SystemExit) as exit_info:
        update._cmd_update_impl(Namespace(), gateway_mode=False)
    assert exit_info.value.code == 2
    kill.assert_not_called()


@pytest.mark.parametrize("automatic", [False, True])
def test_restart_helpers_forward_original_receipt_id_without_environment_actor(monkeypatch, tmp_path, automatic):
    monkeypatch.setenv("MERCURY_RESTART_REQUEST_ID", "original-receipt-123")
    monkeypatch.setenv("MERCURY_RESTART_ACTOR", "untrusted-environment-actor")
    monkeypatch.setenv("MIRC_SERVER_PASSWORD", "test-only-token-not-provenance")
    query = Mock(return_value={"restarting": True, "deferred": False, "pid": 4242})
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    if automatic:
        reply = gateway.request_automatic_gateway_restart(home=tmp_path, pid=4242, trigger="onboarding")
        assert reply["restarting"] is True
        trigger = "onboarding"
    else:
        assert gateway._request_gateway_admin_restart(4242) is True
        trigger = "cli-admin-restart"
    assert query.call_args.kwargs["params"] == {
        "trigger": trigger, "request_id": "original-receipt-123",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_original_receipt_id_crosses_real_control_socket_without_tokens(monkeypatch, tmp_path, automatic):
    import asyncio
    import os
    from gateway.control_socket import GatewayControlServer

    monkeypatch.setenv("MERCURY_RESTART_REQUEST_ID", "platform-original-receipt")
    monkeypatch.setenv("MIRC_SERVER_PASSWORD", "test-only-token-not-provenance")
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    received = []
    def handler(params):
        received.append(params)
        return {"pid": os.getpid(), "restarting": True, "deferred": False}
    server = GatewayControlServer(home=tmp_path)
    server.register_handler(
        "restart-when-idle" if automatic else "restart-admin", handler, takes_params=True,
    )
    try:
        assert await server.start()
        if automatic:
            reply = await asyncio.to_thread(
                gateway.request_automatic_gateway_restart, home=tmp_path, pid=os.getpid(),
            )
            assert reply["restarting"] is True
        else:
            assert await asyncio.to_thread(gateway._request_gateway_admin_restart, os.getpid())
        assert len(received) == 1
        assert received[0]["request_id"] == "platform-original-receipt"
        assert "test-only-token-not-provenance" not in str(received[0])
    finally:
        await server.stop()


def test_target_user_profile_argument_uses_target_canonical_home(monkeypatch, tmp_path):
    target_home = tmp_path / "alice"
    target_profile = target_home / ".mercury" / "hermes" / "profiles" / "mybot"
    monkeypatch.setattr("mercury_cli.profiles._get_profiles_root",
                        lambda: tmp_path / "root" / ".mercury" / "hermes" / "profiles")
    assert gateway._profile_arg_for_target_user(str(target_profile), str(target_home)) == "--profile mybot"


def test_root_scope_remediation_only_prints_no_legacy_service_mutation(monkeypatch):
    monkeypatch.setattr(gateway, "has_legacy_hermes_units", lambda: True)
    remove = Mock(side_effect=AssertionError("remediation must not stop legacy services"))
    monkeypatch.setattr(gateway, "remove_legacy_hermes_units", remove)
    gateway._print_system_scope_remediation("restart")
    remove.assert_not_called()
