"""Native Wayland is an explicit display-scoped opt-in, never telemetry consent."""
from unittest.mock import patch

import pytest

from tools.computer_use import cua_backend

_VAR = "CUA_DRIVER_RS_ENABLE_WAYLAND"


@pytest.mark.parametrize("module,helper", [
    ("tools.computer_use.cua_backend", "cua_driver_child_env"),
    ("tools.computer_use.doctor", "_cua_child_env"),
    ("mercury_cli.tools_config", "_cua_driver_env"),
])
def test_native_wayland_reaches_shared_child_policy(module, helper, monkeypatch):
    import importlib
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-fixture")
    monkeypatch.setenv("CUA_DRIVER_RS_TELEMETRY_ENABLED", "1")
    monkeypatch.delenv(_VAR, raising=False)
    with patch("mercury_cli.config.load_config", return_value={
        "computer_use": {"native_wayland": True, "cua_telemetry": True},
    }), patch.object(cua_backend.sys, "platform", "linux"):
        env = getattr(importlib.import_module(module), helper)()
    assert env[_VAR] == "1"
    assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"


@pytest.mark.parametrize("platform,base", [
    ("linux", {"DISPLAY": ":42"}),
    ("linux", {"XDG_SESSION_TYPE": "wayland"}),
    ("darwin", {"WAYLAND_DISPLAY": "wayland-fixture"}),
])
def test_opt_in_requires_linux_and_actual_wayland_display(platform, base):
    with patch("mercury_cli.config.load_config", return_value={
        "computer_use": {"native_wayland": True},
    }), patch.object(cua_backend.sys, "platform", platform):
        env = cua_backend.cua_driver_child_env(base)
    assert _VAR not in env


def test_default_preserves_manual_driver_opt_in():
    base = {"WAYLAND_DISPLAY": "wayland-fixture", _VAR: "1"}
    with patch("mercury_cli.config.load_config", return_value={"computer_use": {}}):
        env = cua_backend.cua_driver_child_env(base)
    assert env[_VAR] == "1"
    assert base == {"WAYLAND_DISPLAY": "wayland-fixture", _VAR: "1"}


def test_native_wayland_is_disabled_in_product_defaults():
    from mercury_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["computer_use"]["native_wayland"] is False


def test_native_wayland_is_recognized_by_config_cli(tmp_path, monkeypatch, capsys):
    from mercury_cli import config

    monkeypatch.setattr(config, "is_managed", lambda: False)
    monkeypatch.setattr(config, "get_config_path", lambda: tmp_path / "config.yaml")
    assert config._validate_config_key("computer_use.native_wayland") == (True, None)
    config.set_config_value("computer_use.native_wayland", "true")
    import yaml
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["computer_use"]["native_wayland"] is True
    assert "unknown" not in capsys.readouterr().out.lower()


def test_native_wayland_has_boolean_public_schema():
    from mercury_cli.web_server import CONFIG_SCHEMA
    assert CONFIG_SCHEMA["computer_use.native_wayland"]["type"] == "boolean"
