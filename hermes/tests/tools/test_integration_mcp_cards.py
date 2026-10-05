"""Connection cards must work through the real Mercury MCP/config facade."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from mercury_constants import set_hermes_home_override, reset_hermes_home_override
from mercury_cli import mcp_catalog
from tools.connectors.mcp import _CatalogBackend


@pytest.fixture
def local_catalog(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("MERCURY_CONFIG", raising=False)
    monkeypatch.delenv("MERCURY_HOME", raising=False)
    server = tmp_path / "server.py"
    server.write_text('''import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        if "--fail" in sys.argv:
            reply = {"error": {"code": -32603, "message": "rejected test credentials"}}
        else:
            reply = {"result": {"protocolVersion": request["params"]["protocolVersion"],
                     "capabilities": {"tools": {}}, "serverInfo": {"name": "local-test", "version": "1"}}}
    elif method == "tools/list":
        reply = {"result": {"tools": [{"name": "hello", "description": "local greeting",
                 "inputSchema": {"type": "object", "properties": {}}}]}}
    elif method == "ping":
        reply = {"result": {}}
    else:
        reply = {"error": {"code": -32601, "message": "unknown method"}}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], **reply}), flush=True)
''')
    entry = mcp_catalog.CatalogEntry(
        name="local-test", description="Local test server", source="test",
        auth=mcp_catalog.AuthSpec(type="none", env=[
            mcp_catalog.EnvVarSpec("MCP_LOCAL_KEY", "Key"),
            mcp_catalog.EnvVarSpec("MCP_WORKSPACE", "Workspace", secret=False)]),
        transport=mcp_catalog.TransportSpec(type="stdio", command=sys.executable,
            args=[str(server), "${MCP_WORKSPACE}"], env={"MCP_LOCAL_KEY": "${MCP_LOCAL_KEY}"}),
    )
    monkeypatch.setattr(mcp_catalog, "get_entry", lambda name: entry if name == entry.name else None)
    return home, entry


def test_install_probes_real_stdio_then_persists_declared_values(local_catalog):
    home, entry = local_catalog
    assert _CatalogBackend().install(entry.name, {"MCP_LOCAL_KEY": "synthetic-key", "MCP_WORKSPACE": "work"}) == ["hello"]
    config = yaml.safe_load((home / "config.yaml").read_text())
    server = config["mcp_servers"][entry.name]
    assert server["args"][-1] == "work"
    assert server["env"]["MCP_LOCAL_KEY"] == "${MCP_LOCAL_KEY}"
    assert "synthetic-key" not in (home / "config.yaml").read_text()
    assert "MCP_LOCAL_KEY=synthetic-key" in (home / ".env").read_text()
    assert "MCP_WORKSPACE" not in (home / ".env").read_text()


def test_failed_reinstall_keeps_config_and_credentials(local_catalog):
    home, entry = local_catalog
    _CatalogBackend().install(entry.name, {"MCP_LOCAL_KEY": "old-key", "MCP_WORKSPACE": "work"})
    before = {name: (home / name).read_bytes() for name in ("config.yaml", ".env")}
    entry.transport.args.append("--fail")
    with pytest.raises(Exception, match="rejected test credentials"):
        _CatalogBackend().install(entry.name, {"MCP_LOCAL_KEY": "new-key"})
    assert before == {name: (home / name).read_bytes() for name in before}


@pytest.mark.parametrize("existing", [False, True])
def test_credential_write_failure_rolls_back_entire_install(local_catalog, monkeypatch, existing):
    from mercury_cli import config

    home, entry = local_catalog
    if existing:
        _CatalogBackend().install(entry.name, {"MCP_LOCAL_KEY": "old-key"})
    before = {name: (home / name).read_bytes() if (home / name).exists() else None
              for name in ("config.yaml", ".env")}
    old_env = os.environ.get("MCP_LOCAL_KEY")
    original = config.save_env_value

    def fail_after_write(key, value):
        original(key, value)
        raise OSError("simulated credential disk failure")

    monkeypatch.setattr(config, "save_env_value", fail_after_write)
    with pytest.raises(OSError, match="credential disk failure"):
        _CatalogBackend().install(entry.name, {"MCP_LOCAL_KEY": "new-key"})
    assert before == {name: (home / name).read_bytes() if (home / name).exists() else None
                      for name in before}
    assert os.environ.get("MCP_LOCAL_KEY") == old_env


def test_oauth_commit_failure_restores_previous_configuration(local_catalog):
    from tools.connectors import mcp_oauth
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    home, entry = local_catalog
    _CatalogBackend().install(entry.name, {})
    before = (home / "config.yaml").read_bytes()
    flow = DashboardOAuthFlow("test-disk-failure", entry.name, None, str(home), "")

    def fail():
        raise OSError("simulated commit failure")

    with pytest.raises(OSError, match="commit failure"):
        mcp_oauth._commit(entry.name, {"url": "https://example.test/new"}, fail, flow)
    assert (home / "config.yaml").read_bytes() == before
    assert not getattr(flow, "committed", False)


def test_managed_credentials_are_rejected_without_persisting_server(local_catalog, monkeypatch):
    from mercury_cli import managed_scope

    home, entry = local_catalog
    monkeypatch.setattr(managed_scope, "is_env_managed", lambda key: key == "MCP_LOCAL_KEY")
    with pytest.raises(PermissionError, match="managed by your administrator"):
        _CatalogBackend().install(entry.name, {"MCP_LOCAL_KEY": "synthetic-key"})
    assert not (home / "config.yaml").exists()
    assert not (home / ".env").exists()


def test_install_and_enable_use_bound_profile(local_catalog):
    home, entry = local_catalog
    profile = home / "profiles" / "work"
    profile.mkdir(parents=True)
    token = set_hermes_home_override(str(profile))
    try:
        _CatalogBackend().install(entry.name, {})
        config = yaml.safe_load((profile / "config.yaml").read_text())
        config["mcp_servers"][entry.name]["enabled"] = False
        (profile / "config.yaml").write_text(yaml.safe_dump(config))
        _CatalogBackend().enable(entry.name)
        assert yaml.safe_load((profile / "config.yaml").read_text())["mcp_servers"][entry.name]["enabled"]
        assert not (home / "config.yaml").exists()
    finally:
        reset_hermes_home_override(token)


def test_oauth_cancel_rejects_late_commit_without_changing_config(local_catalog):
    from tools.connectors import mcp_oauth
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    home, entry = local_catalog
    flow = DashboardOAuthFlow("test-cancel", entry.name, None, str(home), "http://127.0.0.1:1234/callback")
    assert not mcp_oauth.cancel_attempt(flow)
    with pytest.raises(mcp_oauth.AttemptCanceled):
        mcp_oauth._commit(entry.name, {"url": "https://example.test/mcp"}, None, flow)
    assert not (home / "config.yaml").exists()


def test_failed_oauth_probe_restores_real_token_snapshot(local_catalog):
    from tools.connectors import mcp_oauth
    from tools.mcp_oauth import HermesTokenStorage

    home, entry = local_catalog
    storage = HermesTokenStorage(entry.name, mercury_home=home)
    storage._tokens_path().parent.mkdir(parents=True, exist_ok=True)
    storage._tokens_path().write_bytes(b'{"old-token": "synthetic"}')
    before = storage.snapshot()
    entry.transport.args.append("--fail")
    with pytest.raises(Exception, match="rejected test credentials"):
        mcp_oauth.probe_with_rollback(entry.name, mcp_catalog.card_install_config(entry), str(home), None, False)
    assert storage.snapshot() == before
    assert not (home / "config.yaml").exists()


def test_oauth_callback_receiver_and_registry_use_existing_flow(local_catalog):
    import urllib.request
    from tools.connectors import mcp_oauth
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow
    from tui_gateway import mcp_oauth_sessions

    home, entry = local_catalog
    flow = DashboardOAuthFlow("test-callback", entry.name, None, str(home), "")
    asyncio.run(flow.publish_authorization_url("https://example.test/authorize?state=test-state"))
    listener = mcp_oauth.choose_callback_receiver(flow, {})
    try:
        mcp_oauth_sessions.register_flow(flow, httpd=listener)
        with urllib.request.urlopen(flow.redirect_uri + "?code=test-code&state=test-state", timeout=2) as response:
            assert response.status == 200
        assert asyncio.run(flow.wait_for_callback(timeout=1)) == ("test-code", "test-state")
        flow.mark_worker_done()
    finally:
        mcp_oauth_sessions.finish_flow(flow.flow_id)
        with mcp_oauth_sessions._sessions_lock:
            mcp_oauth_sessions._sessions.pop(flow.flow_id, None)
