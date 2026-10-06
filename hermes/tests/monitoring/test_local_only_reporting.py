"""Outbound exporters cannot be restored by legacy opt-in configuration."""
from importlib.util import find_spec
from types import SimpleNamespace

import pytest
import yaml


def test_legacy_langfuse_opt_in_is_not_discovered(tmp_path, monkeypatch):
    from mercury_cli import plugins

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["observability/langfuse", "langfuse"]},
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk-lf-test-public")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk-lf-test-secret")
    monkeypatch.setattr(plugins, "discover_entrypoint_manifests", lambda: [])
    manager = plugins.PluginManager()
    try:
        manager.discover_and_load()
        assert "observability/langfuse" not in manager._plugins
    finally:
        manager.unload()


def test_langfuse_post_setup_is_rejected_without_installing(monkeypatch):
    from mercury_cli import tools_config

    calls = []
    monkeypatch.setattr(tools_config, "_run_post_setup", calls.append)
    assert tools_config.run_post_setup_command(
        SimpleNamespace(post_setup_key="langfuse")
    ) == 2
    assert calls == []


@pytest.mark.parametrize("module", [
    "agent.monitoring.gateway_health_export",
    "agent.monitoring.otlp_exporter",
])
def test_removed_exporters_are_not_importable(module):
    assert find_spec(module) is None


def test_monitoring_reports_local_health_with_legacy_export_config(tmp_path, monkeypatch, capsys):
    from mercury_cli import config, main
    from gateway.status import write_runtime_status

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config, "load_config", lambda: {"monitoring": {
        "gateway_health_export": {"enabled": True},
        "export": {"otlp": {"enabled": True, "endpoint": "https://report.invalid/v1/traces"}},
    }})
    write_runtime_status(
        gateway_state="running", active_agents=2,
        platform="mirc", platform_state="fatal", error_code="auth_failed",
    )
    main.cmd_monitoring(SimpleNamespace(monitoring_action="status"))
    text = capsys.readouterr().out
    assert "local only" in text
    assert "mercury.gateway.active_agents: 2" in text
    assert "mercury.platform.degraded: 1" in text
    assert "mercury.cron.jobs.enabled:" in text
    assert "report.invalid" not in text
