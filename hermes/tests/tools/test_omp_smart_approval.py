"""OMP smart assessment uses Hermes decisions and keeps human routing in OMP."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mercury_cli.omp_approval import assess_command
from tools import approval


@pytest.fixture
def smart(monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda _command: False)
    from tools import tirith_security
    monkeypatch.setattr(tirith_security, "check_command_security", lambda _command: {"action": "allow"})


def test_ordinary_shell_commands_do_not_need_a_human_or_guardian(smart, monkeypatch):
    def unexpected(*_args):
        pytest.fail("An ordinary command reached the guardian")
    monkeypatch.setattr(approval, "_smart_approve", unexpected)
    assert assess_command("true") == {"policy": "allow"}
    assert assess_command("git status --short") == {"policy": "allow"}


@pytest.mark.parametrize("verdict,policy", [("approve", "allow"), ("escalate", "prompt"), ("deny", "prompt")])
def test_risky_commands_share_smart_verdict_and_owner_override(smart, monkeypatch, verdict, policy):
    seen = []
    def guardian(command, description):
        seen.append(command)
        return verdict
    monkeypatch.setattr(approval, "_smart_approve", guardian)
    command = "rm -rf /tmp/omp-smart-never-executed"
    result = assess_command(command)
    assert result["policy"] == policy
    assert seen == [command]
    if policy == "prompt":
        assert result["reason"]


def test_hard_blocks_and_explicit_denies_never_become_owner_prompts(smart, monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_config", lambda: {"mode": "smart", "deny": ["true"]})
    for command in ("rm -rf /", "true"):
        assert assess_command(command)["policy"] == "deny"


def test_scanner_warning_escalates_even_without_a_dangerous_shell_pattern(smart, monkeypatch):
    from tools import tirith_security
    monkeypatch.setattr(tirith_security, "check_command_security", lambda _command: {
        "action": "warn", "findings": [{"rule_id": "test", "title": "Suspicious content"}],
    })
    monkeypatch.setattr(approval, "_smart_approve", lambda *_args: "escalate")
    assert assess_command("echo harmless")["policy"] == "prompt"


def test_assessment_failure_escalates_and_does_not_corrupt_json_protocol(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("approvals: {mode: smart}\n")
    env = {**os.environ, "MERCURY_CONFIG": str(config), "HERMES_HOME": str(tmp_path / "hermes")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, "-m", "mercury_cli.omp_approval"],
        input=json.dumps({"command": 1}), text=True, capture_output=True, env=env,
        timeout=20,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["policy"] == "prompt"
    result = subprocess.run(
        [sys.executable, "-m", "mercury_cli.omp_approval"],
        input=json.dumps({"command": "true"}), text=True, capture_output=True, env=env,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"policy": "allow"}
