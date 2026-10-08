"""Tests for explicit ownership by a wrapped external gateway supervisor."""

from types import SimpleNamespace


import mercury_cli.gateway as gateway


def _clear_native_supervisor_markers(monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.setenv("XPC_SERVICE_NAME", "0")


def test_external_marker_identifies_supervisor_process(monkeypatch):
    _clear_native_supervisor_markers(monkeypatch)
    monkeypatch.setenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, "1")

    assert gateway._running_under_gateway_supervisor() is True


def test_gateway_run_external_supervisor_flag_marks_process(monkeypatch):
    monkeypatch.delenv(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV, raising=False)
    monkeypatch.setattr(
        gateway, "_maybe_redirect_run_to_s6_supervision", lambda _args: False
    )
    observed = []
    monkeypatch.setattr(
        gateway,
        "run_gateway",
        lambda *_args, **_kwargs: observed.append(
            gateway.os.environ.get(gateway.EXTERNAL_GATEWAY_SUPERVISOR_ENV)
        ),
    )

    gateway._gateway_command_inner(
        SimpleNamespace(gateway_command="run", external_supervisor=True)
    )

    assert observed == ["1"]






def test_wrapper_upgrades_stale_plist_argv_to_external_supervisor():
    from mercury_cli.stderr_timestamp import _prepare_child_command

    stale = ["/usr/bin/python3", "-m", "mercury_cli.main", "gateway", "run", "--replace"]
    upgraded = _prepare_child_command(stale, {"XPC_SERVICE_NAME": "ai.mercury.gateway-work"})
    assert upgraded[-1] == "--external-supervisor"




