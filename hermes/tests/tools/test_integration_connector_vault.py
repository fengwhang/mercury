"""Exercise imported features through Mercury's actual auth and approval boundaries."""
import json
import pytest
from contextlib import nullcontext
from types import SimpleNamespace

from mercury_constants import mercury_home_key


def test_failed_catalog_plugin_install_does_not_persist_credentials(monkeypatch, tmp_path):
    from tools.connectors import catalog
    from tools.connectors.operation import Target

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MERCURY_HOME", raising=False)
    monkeypatch.delenv("MERCURY_CONFIG", raising=False)
    monkeypatch.setattr(catalog, "target_scope", lambda profile: nullcontext())
    installer = SimpleNamespace(install_plugin=lambda *args, **kwargs:
                                {"ok": False, "error": "simulated clone failure"})
    runner = catalog._Runner(installer)
    target = Target("test-plugin", "plugin", "install")
    with pytest.raises(RuntimeError, match="clone failure"):
        runner._install(target, {"TEST_CATALOG_KEY": "synthetic-secret"})
    assert not (tmp_path / ".env").exists()


def test_connection_operations_follow_current_profile_scope(tmp_path):
    from mercury_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.connectors import live
    from tools.connectors.operation import ConnectionOperation, Target

    operations = []
    try:
        for name in ("one", "two"):
            home = tmp_path / name
            token = set_hermes_home_override(str(home))
            try:
                assert live.current("shared-session") is None
                operation = ConnectionOperation([Target("mail", "connector", "connect")], session_key="shared-session")
                live.open(operation)
                operations.append(operation)
                assert live.current("shared-session") is operation
                assert live.get_by_op_id(operation.op_id) is operation
            finally:
                reset_hermes_home_override(token)
        assert live.current("shared-session", profile_home=str(tmp_path / "one")) is operations[0]
        assert live.current("shared-session", profile_home=str(tmp_path / "two")) is operations[1]
    finally:
        for operation in operations:
            live.close(operation)


def test_connector_gate_reads_existing_account_claim(monkeypatch):
    from mercury_cli import nous_account
    from tools.connectors.gateway.config import ConnectorConfig, connectors_available

    info = SimpleNamespace(logged_in=True, managed_tools_rolled_out=True)
    monkeypatch.setattr(nous_account, "get_nous_portal_account_info", lambda: info)
    assert connectors_available(config_loader=lambda: ConnectorConfig())
    info.managed_tools_rolled_out = False
    assert not connectors_available(config_loader=lambda: ConnectorConfig())
    info.managed_tools_rolled_out = True
    assert not connectors_available(config_loader=lambda: ConnectorConfig(enabled=False))


def test_connector_client_uses_existing_gateway_auth(monkeypatch):
    from tools.connectors.gateway.client import ConnectorClient, _default_header_provider

    monkeypatch.setenv("CONNECTOR_GATEWAY_URL", "https://connector-gateway.example.test")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "synthetic-test-token")
    seen = []

    class Transport:
        def request(self, method, url, **kwargs):
            seen.append((method, url, kwargs))
            return SimpleNamespace(status_code=200, json=lambda: {"schemas": {}})

    assert ConnectorClient(transport=Transport()).schemas(["mail-send"])["schemas"] == {}
    assert seen[0][1] == "https://connector-gateway.example.test/v1/connectors/schemas"
    assert seen[0][2]["headers"]["Authorization"] == "Bearer synthetic-test-token"
    assert _default_header_provider("https://another-host.example.test/v1/connectors/schemas") == {}


def test_connection_transition_reaches_only_owning_profile(monkeypatch, tmp_path):
    from tui_gateway import methods_connectors, server
    from tools.connectors.operation import ConnectionOperation, Target
    from tools.connectors.contract import Actor, TargetState

    home = str(tmp_path / "profile")
    monkeypatch.setattr(server, "_sessions", {
        "owner": {"session_key": "same-key", "profile_home": home},
        "other": {"session_key": "same-key", "profile_home": str(tmp_path / "other")},
    })
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(ConnectionOperation, "on_change", staticmethod(methods_connectors._connection_update))
    operation = ConnectionOperation([Target("mail", "connector", "connect")],
                                    session_key="same-key", profile_key=mercury_home_key(home))
    operation.transition("mail", TargetState.initiated, Actor.backend_watcher)
    assert len(emitted) == 1
    event, owner, payload = emitted[0]
    assert (event, owner) == ("connection.update", "owner")
    assert payload["targets"][0]["state"] == "initiated"


def test_mixed_connector_batch_returns_actionable_error():
    from tools.connectors.dispatch import dispatch_connector_batch

    result = json.loads(dispatch_connector_batch(
        [{"name": "read_file", "arguments": {"path": "unused"}}],
        user_task="test", enabled_tools=set(), middleware_trace=[],
        enabled_toolsets=[], disabled_toolsets=[],
    ))
    assert "local tools separately" in result["error"]


def test_vault_uses_existing_approval_surface(monkeypatch):
    from tools import approval, browser_vault_tool
    from agent.vault_backends import unlock

    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *args, **kwargs: "once")
    assert browser_vault_tool._confirm_payment_fill("test card", "https://checkout.example.test")
    for name in ("_is_cron_approval_context", "_is_unattended_platform_approval_context",
                 "_is_single_query_approval_context"):
        monkeypatch.setattr(approval, name, lambda: False)
    monkeypatch.setattr(unlock, "get_unlock_prompt_callback", lambda: lambda *_args: "synthetic")
    assert unlock.can_prompt_here()
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
    assert not unlock.can_prompt_here()


def test_local_vault_fence_does_not_require_unshipped_desktop():
    from tools.browser_tool_session import run_fenced

    assert run_fenced({"features": {"local": True}}, lambda: {"success": True}) == {"success": True}
