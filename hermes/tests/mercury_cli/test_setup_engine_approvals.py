"""Setup persists each engine's distinct policy without cross-engine rewrites."""
import argparse

import pytest
import yaml

from mercury_cli import config, setup
from mercury_cli.subcommands.setup import build_setup_parser
from tools import approval


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "approvals": {"mode": "manual", "smart_policy": "Keep this policy", "deny": ["*git push*"]},
        "hermes": {"approvals": {"mode": "off"}},
        "omp": {"tools": {"approvalMode": "yolo", "approval": {"write": "deny"}}},
    }))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_CONFIG", str(path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return path


def test_setup_keeps_engine_choices_and_policy_details_through_final_save(profile, monkeypatch):
    prompts = []

    def choose(question, choices, default):
        prompts.append((question, choices, default))
        return 1  # Hermes smart, then OMP write.

    monkeypatch.setattr(setup, "prompt_choice", choose)
    loaded = config.load_config()
    setup.setup_approvals(loaded)
    config.save_config(loaded)
    saved = yaml.safe_load(profile.read_text())
    assert approval._get_approval_mode() == "smart"
    assert saved["omp"]["tools"] == {"approvalMode": "write", "approval": {"write": "deny"}}
    assert saved["approvals"]["smart_policy"] == "Keep this policy"
    assert saved["approvals"]["deny"] == ["*git push*"]
    assert len(prompts) == 2
    # Exposes the actual choice boundary: OMP write is not offered as Hermes smart.
    assert prompts[1][1][1].startswith("write")
    assert prompts[0][1][1].startswith("smart")


def test_omp_setup_does_not_change_hermes_policy(profile, monkeypatch, capsys):
    before = yaml.safe_load(profile.read_text())
    monkeypatch.setattr(setup, "prompt_choice", lambda *_args: 0)
    setup.setup_omp_approvals(config.load_config())
    after = yaml.safe_load(profile.read_text())
    assert after["approvals"] == before["approvals"]
    assert after["hermes"] == before["hermes"]
    assert after["omp"]["tools"]["approvalMode"] == "always-ask"
    capsys.readouterr()
    config.get_config_value("omp.tools.approvalMode")
    assert capsys.readouterr().out.strip() == "always-ask"


def test_invalid_omp_mode_refuses_write(profile):
    before = profile.read_bytes()
    with pytest.raises(SystemExit):
        config.set_config_value("omp.tools.approvalMode", "smart")
    assert profile.read_bytes() == before


def test_setup_sections_are_reachable_from_cli():
    parser = argparse.ArgumentParser()
    build_setup_parser(parser.add_subparsers(), cmd_setup=lambda _args: None)
    for section in ("approvals", "hermes-approvals", "omp-approvals"):
        assert parser.parse_args(["setup", section]).section == section
