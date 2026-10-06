"""Mercury must never allow a cua-driver child to opt into reporting."""

from unittest.mock import patch

import pytest

from tools.computer_use import cua_backend


_VAR = "CUA_DRIVER_RS_TELEMETRY_ENABLED"


def test_legacy_opt_in_cannot_enable_driver_reporting():
    base = {"PATH": "/usr/bin", _VAR: "1"}
    with patch("mercury_cli.config.load_config", return_value={
        "computer_use": {"cua_telemetry": True},
    }):
        env = cua_backend.cua_driver_child_env(base)
    assert env[_VAR] == "0"
    assert env["PATH"] == base["PATH"]
    assert base[_VAR] == "1"


@pytest.mark.parametrize("module,helper", [
    ("tools.computer_use.doctor", "_cua_child_env"),
    ("tools.computer_use.permissions", "_child_env"),
    ("mercury_cli.tools_config", "_cua_driver_env"),
])
def test_failed_policy_helper_still_disables_reporting(module, helper, monkeypatch):
    import importlib

    caller = importlib.import_module(module)
    monkeypatch.setenv(_VAR, "1")
    with patch.object(cua_backend, "cua_driver_child_env", side_effect=RuntimeError("unavailable")):
        env = getattr(caller, helper)()
    assert env[_VAR] == "0"
