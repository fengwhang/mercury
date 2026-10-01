"""Exercise the real YAML bridge and engine approval resolver against one policy."""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tools import approval


@pytest.mark.parametrize("mode,expected,omp_mode", [
    ("safe", "manual", "always-ask"), ("smart", "smart", "write"),
    ("yolo", "off", "yolo"), (False, "off", "yolo"),
])
def test_both_engines_read_same_policy_and_keep_user_settings(tmp_path, monkeypatch, mode, expected, omp_mode):
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config = {
        "models": {"default": "prov/model", "delegate_model": "prov/model"},
        "approvals": {"mode": mode, "deny": ["*git push*", "echo '#keep' *"]},
        "hermes": {"approvals": {"mode": "smart", "deny": ["shutdown*"]}},
        "omp": {"theme": {"dark": "mercury"}, "tools": {"approval": {"write": "deny"}},
                "bash": {"patterns": [{"match": "custom*", "approval": "deny"}]}},
    }
    config_path.write_text(yaml.safe_dump(config, default_flow_style=True))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("MERCURY_CONFIG", str(config_path))
    assert approval._get_approval_mode() == expected
    assert set(approval._get_approval_config().get("deny", [])) == set(config["approvals"]["deny"] + ["shutdown*"])
    bridge = Path(__file__).resolve().parents[3] / "bridge" / "bridge.py"
    for _ in range(2):
        result = subprocess.run([sys.executable, str(bridge), "--render-omp"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    rendered = yaml.safe_load(config_path.read_text())["omp"]
    assert rendered["tools"]["approvalMode"] == omp_mode
    assert rendered["tools"]["approval"] == {"write": "deny"}
    assert rendered["theme"] == {"dark": "mercury"}
    assert rendered["bash"]["patterns"] == [
        {"match": "custom*", "approval": "deny"},
        *[{"match": pattern, "approval": "deny"} for pattern in sorted(config["approvals"]["deny"] + ["shutdown*"])],
    ]
    # Removing shared rules removes only bridge-owned entries.
    config["approvals"]["deny"] = []
    config["hermes"]["approvals"]["deny"] = []
    config["omp"] = rendered
    text = config_path.read_text()
    marker = next(line for line in text.splitlines() if "# Mercury inherited deny patterns:" in line)
    updated = yaml.safe_dump(config, sort_keys=False)
    config_path.write_text(updated.replace("omp:\n", "omp:\n" + marker + "\n"))
    result = subprocess.run([sys.executable, str(bridge), "--render-omp"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(config_path.read_text())["omp"]["bash"]["patterns"] == [{"match": "custom*", "approval": "deny"}]


def test_background_child_keeps_parent_prompt_route_until_completion():
    key = "test:background:approval-owner"
    prompts = []
    def notify(data):
        prompts.append(data)
        approval.resolve_gateway_approval(key, "once", request_id=data["request_id"])
    token = approval.set_current_session_key(key)
    approval.register_gateway_notify(key, notify)
    release = approval.retain_gateway_notify(key)
    try:
        approval.unregister_gateway_notify(key)  # the orchestrator's turn ended
        result = approval.request_tool_approval("write", "background grandchild", require_human=True)
        assert result["approved"] is True
        assert len(prompts) == 1
    finally:
        release()
        release()  # release is idempotent
        approval.reset_current_session_key(token)
    assert key not in approval._gateway_notify_cbs


def test_one_shot_descendant_approval_preserves_parent_context(monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    import http.client
    import json
    import socket
    from tools.omp_delegation import _ApprovalBridgeServer

    key = "test:hermes-parent:omp-grandchild"
    seen = []
    token = approval.set_current_session_key(key)
    def notify(data):
        seen.append(approval.get_current_session_key())
        approval.resolve_gateway_approval(key, "once", request_id=data["request_id"])
    approval.register_gateway_notify(key, notify)
    bridge = _ApprovalBridgeServer(None)
    try:
        socket_path = bridge.start()
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.connect(socket_path)
        connection.request("POST", "/approve", body=json.dumps({"kind": "select", "title": "[child] [grandchild] Allow tool: write\nPath: /tmp/probe"}), headers={"Content-Type": "application/json"})
        reply = json.loads(connection.getresponse().read())
        connection.close()
        assert reply["value"] == "Approve"
        assert seen == [key]
    finally:
        bridge.stop()
        approval.unregister_gateway_notify(key)
        approval.reset_current_session_key(token)



def test_omp_room_command_gate_recognizes_attached_human_without_gateway_env(tmp_path, monkeypatch):
    home = tmp_path / "room-policy"
    home.mkdir()
    (home / "config.yaml").write_text("approvals: {mode: safe}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("MERCURY_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "0")
    key = "test:omp-room:owner"
    seen = []
    def notify(data):
        seen.append(data)
        approval.resolve_gateway_approval(key, "deny", request_id=data["request_id"])
    token = approval.set_current_session_key(key)
    approval.register_gateway_notify(key, notify)
    try:
        result = approval.check_all_command_guards("rm -rf /tmp/approval-probe", env_type="container")
        assert result["approved"] is False
        assert result["user_consent"] is False
        assert len(seen) == 1
    finally:
        approval.unregister_gateway_notify(key)
        approval.reset_current_session_key(token)
