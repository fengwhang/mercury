from __future__ import annotations


def test_install_id_persists_across_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")

    import mercury_cli.config as cfg_mod
    from agent.monitoring.policy import ensure_install_id

    first = ensure_install_id(cfg_mod.load_config())
    assert first and first != "unknown"
    second = ensure_install_id(cfg_mod.load_config())
    assert second == first
    assert first in (tmp_path / "config.yaml").read_text()


def test_local_health_snapshot_keeps_platform_diagnostics_content_free():
    from agent.monitoring.gateway_health import build_gateway_health_snapshot

    snapshot = build_gateway_health_snapshot(
        {"gateway_state": "running", "active_agents": 2, "platforms": {
            "mirc": {"state": "fatal", "error_message": "Bearer secret-token HTTP 401 for alice@example.com"},
        }},
        gateway_running=True, profile="private", install_id="private-install",
        version="test", supervision_mode="manual",
    )
    metrics = {metric.name: metric.value for metric in snapshot.metrics}
    assert metrics["mercury.gateway.up"] == 1
    assert metrics["mercury.gateway.active_agents"] == 2
    assert metrics["mercury.platform.degraded"] == 1
    diagnostics = [event.to_dict() for event in snapshot.events if event.to_dict()["event"] == "gateway_diagnostic"]
    assert diagnostics[0]["error_class"] == "auth_failed"
    assert "secret-token" not in str(diagnostics)
    assert "alice@example.com" not in str(diagnostics)


def test_local_runtime_snapshot_combines_gateway_cron_and_background_work(monkeypatch):
    from agent.monitoring import gateway_health, cron_health

    monkeypatch.setattr(gateway_health, "_read_gateway_snapshot", lambda config:
        gateway_health.build_gateway_health_snapshot({}, gateway_running=False,
            profile="default", install_id="unknown", version="test"))
    monkeypatch.setattr(gateway_health, "_read_cron_snapshot", lambda:
        cron_health.CronHealthSnapshot(
            metrics=[gateway_health.GatewayMetric("mercury.cron.jobs.enabled", 0, {})],
            events=[],
        ))
    monkeypatch.setattr(gateway_health, "_read_background_work_count", lambda: 3)
    monkeypatch.setattr(gateway_health, "_read_background_delegations_count", lambda: 1)
    snapshot = gateway_health.read_runtime_health_snapshot({})
    metrics = {metric.name: metric.value for metric in snapshot.metrics}
    assert metrics["mercury.gateway.up"] == 0
    assert metrics["mercury.gateway.background_work"] == 3
    assert metrics["mercury.gateway.background_delegations"] == 1
    assert metrics["mercury.cron.jobs.enabled"] == 0


def test_local_runtime_snapshot_keeps_gateway_when_cron_reader_fails(monkeypatch):
    from agent.monitoring import gateway_health

    def broken_cron():
        raise RuntimeError("private cron details")

    monkeypatch.setattr(gateway_health, "_read_cron_snapshot", broken_cron)
    snapshot = gateway_health.read_runtime_health_snapshot({})
    assert any(metric.name == "mercury.gateway.up" for metric in snapshot.metrics)
