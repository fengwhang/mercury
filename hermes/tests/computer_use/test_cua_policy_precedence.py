"""Profile/skill passthrough cannot override Mercury's final CUA no-reporting policy."""
import asyncio
import importlib
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent import secret_scope
from tools import env_passthrough
from tools.computer_use import cua_backend, cua_backend_driver

_VAR = "CUA_DRIVER_RS_TELEMETRY_ENABLED"


@pytest.fixture(params=["1", None], ids=["scoped-opt-in", "scoped-missing"])
def scoped_opt_in(monkeypatch, request):
    passthrough_token = env_passthrough._allowed_env_vars_var.set(set())
    monkeypatch.setattr(env_passthrough, "_config_passthrough", frozenset())
    env_passthrough.register_env_passthrough([_VAR])
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    secret_token = secret_scope.set_secret_scope({_VAR: request.param} if request.param is not None else {})
    monkeypatch.setenv(_VAR, "1")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-provider-key")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-fixture")
    try:
        yield
    finally:
        secret_scope.reset_secret_scope(secret_token)
        env_passthrough._allowed_env_vars_var.reset(passthrough_token)


def _assert_policy(env):
    assert env.get(_VAR) == "0"
    assert "OPENAI_API_KEY" not in env
    assert env["WAYLAND_DISPLAY"] == "wayland-fixture"


@pytest.mark.parametrize("module,helper", [
    ("mercury_cli.tools_config", "_cua_driver_env"),
    ("tools.computer_use.doctor", "_sanitized_cua_env"),
    ("tools.computer_use.permissions", "_child_env"),
])
@pytest.mark.parametrize("policy_unavailable", [False, True])
def test_cli_helpers_apply_reporting_policy_after_scoped_passthrough(scoped_opt_in, module, helper, policy_unavailable):
    caller = importlib.import_module(module)
    if policy_unavailable:
        with patch.object(cua_backend, "cua_driver_child_env", side_effect=RuntimeError("unavailable")):
            env = getattr(caller, helper)()
    else:
        env = getattr(caller, helper)()
    _assert_policy(env)


def test_configured_daemon_probe_applies_final_reporting_policy(scoped_opt_in):
    with patch.object(cua_backend_driver.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
        assert cua_backend_driver.cua_daemon_listening("/fixture/cua-driver", "/tmp/fixture.sock") is True
    _assert_policy(run.call_args.kwargs["env"])


def test_active_manifest_probe_applies_final_reporting_policy(scoped_opt_in, monkeypatch):
    monkeypatch.setattr(cua_backend, "_cua_no_overlay", lambda: False)
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
        cua_backend._resolve_mcp_invocation("/fixture/cua-driver")
    _assert_policy(run.call_args.kwargs["env"])


def test_active_cli_fallback_applies_final_reporting_policy(scoped_opt_in, monkeypatch):
    monkeypatch.setattr(cua_backend, "resolve_cua_driver_cmd", lambda: "/fixture/cua-driver")
    session = object.__new__(cua_backend._CuaDriverSession)
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"tree_markdown": "root"}), "")) as run:
        session._call_tool_via_cli("list_windows", {}, timeout=5.0)
    _assert_policy(run.call_args.kwargs["env"])


def test_active_mcp_spawn_applies_final_reporting_policy(scoped_opt_in, monkeypatch):
    import mcp
    monkeypatch.setattr(cua_backend, "resolve_cua_driver_cmd", lambda: "/fixture/cua-driver")
    monkeypatch.setattr(cua_backend, "_resolve_mcp_invocation", lambda _cmd: ("/fixture/cua-driver", ["mcp"]))
    session = cua_backend._CuaDriverSession(MagicMock())
    # Stop at the spawn boundary: no MCP process or desktop interaction occurs.
    with patch.object(mcp, "StdioServerParameters", side_effect=RuntimeError("fixture stop")) as params:
        with pytest.raises(RuntimeError, match="fixture stop"):
            asyncio.run(session._lifecycle_coro())
    _assert_policy(params.call_args.kwargs["env"])


def test_active_embedded_daemon_spawn_applies_final_reporting_policy(scoped_opt_in, monkeypatch):
    monkeypatch.setattr(cua_backend, "_resolve_mcp_invocation", lambda _cmd: ("/fixture/cua-driver", ["mcp"]))
    monkeypatch.setattr(cua_backend, "_mcp_args_with_overlay_flag", lambda args, **_kw: args)
    daemon = cua_backend._EmbeddedCuaDaemon("/fixture/cua-driver", "unrestricted")
    # Stop before a daemon can be created, not after it starts.
    with patch("subprocess.Popen", side_effect=RuntimeError("fixture stop")) as popen:
        with pytest.raises(RuntimeError, match="fixture stop"):
            daemon.start()
    _assert_policy(popen.call_args.kwargs["env"])
