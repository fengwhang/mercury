"""Runtime maintenance is local-only; explicit provisioning is separate."""
from unittest.mock import Mock


def test_banner_never_queries_update_server(monkeypatch):
    from mercury_cli import banner
    query = Mock(return_value=2)
    monkeypatch.setenv("HERMES_REVISION", "fixture-revision")
    monkeypatch.setattr(banner, "_check_via_rev", query)
    assert banner.check_for_updates() is None
    query.assert_not_called()


def test_missing_dependencies_never_install_even_with_durable_target(monkeypatch, tmp_path):
    from tools import lazy_deps
    import pytest
    installer = Mock()
    monkeypatch.setenv("HERMES_LAZY_INSTALL_TARGET", str(tmp_path))
    monkeypatch.setattr(lazy_deps, "feature_missing", lambda _: ("mcp>=1.2.0",))
    monkeypatch.setattr(lazy_deps, "_venv_pip_install", installer)
    with pytest.raises(lazy_deps.FeatureUnavailable):
        lazy_deps.ensure("tool.computer_use", prompt=False)
    installer.assert_not_called()


def test_cua_runtime_never_repairs_or_checks_updates(monkeypatch):
    from tools.computer_use import cua_backend
    from mercury_cli import tools_config
    installer = Mock(return_value=False)
    updater = Mock(return_value="new release")
    threads = Mock()
    monkeypatch.setattr(tools_config, "install_cua_driver", installer)
    monkeypatch.setattr(cua_backend, "cua_driver_update_nudge", updater)
    monkeypatch.setattr(cua_backend.threading, "Thread", threads)
    monkeypatch.setattr(cua_backend, "_contract_repair_attempted", False)
    monkeypatch.setattr(cua_backend, "_update_checked", False)
    monkeypatch.delenv("HERMES_CUA_DRIVER_CMD", raising=False)
    contract = {"ready": False, "binary": "/fixture/cua-driver", "reason": "incompatible"}
    assert cua_backend._maybe_repair_runtime_contract(contract) == contract
    cua_backend._maybe_nudge_update()
    installer.assert_not_called()
    updater.assert_not_called()
    threads.assert_not_called()


def test_cold_model_metadata_uses_only_local_inventory(monkeypatch):
    from agent import model_metadata
    query = Mock(side_effect=RuntimeError("fixture remote boundary"))
    monkeypatch.setattr(model_metadata, "_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_load_model_metadata_disk_cache", lambda: {})
    monkeypatch.setattr(model_metadata, "_model_metadata_disk_cache_age_seconds", lambda: None)
    monkeypatch.setattr(model_metadata, "_ensure_requests", query)
    assert model_metadata.fetch_model_metadata() == {}
    query.assert_not_called()


def test_native_cua_seals_credentials_after_terminal_passthrough(monkeypatch):
    from tools.computer_use import cua_backend
    from tools.environments import local
    secrets = {
        "OPENAI_API_KEY": "fixture",
        "AWS_ACCESS_KEY_ID": "fixture",
        "AWS_SECRET_ACCESS_KEY": "fixture",
        "CLAUDE_CODE_OAUTH_TOKEN": "fixture",
        "GOOGLE_APPLICATION_CREDENTIALS": "/fixture/service-account",
    }
    monkeypatch.setattr(local, "_sanitize_subprocess_env", lambda env: {**env, **secrets})
    for env in (cua_backend.cua_driver_child_env(secrets),
                cua_backend.sanitized_cua_driver_env({"WAYLAND_DISPLAY": "fixture"})):
        assert not (secrets.keys() & env.keys())
        assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
