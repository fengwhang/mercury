"""Standalone skill helpers must preserve the selected Mercury config."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_prompt_helper_preserves_both_engine_configs(tmp_path, monkeypatch):
    root = tmp_path / ".mercury-nightly"
    home = root / "hermes" / "profiles" / "helper"
    home.mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("MERCURY_CONFIG", str(root / "config.yaml"))
    cfg = {"models": {"default": "test/model"}, "hermes": {"model": {"default": "model"}},
           "omp": {"tools": {"approvalMode": "write"}}, "approvals": {"mode": "smart"}}
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    helper = load_script(ROOT / "optional-skills/security/godmode/scripts/auto_jailbreak.py", "mercury_prompt_config_helper")
    assert helper.CONFIG_PATH == path
    assert helper._get_current_model()[0] == "model"
    # Test configuration writes only, with a neutral persona. No client calls,
    # canaries, model requests, or changes to the live installation.
    helper._write_config(system_prompt="A temporary test persona.")
    actual = yaml.safe_load(path.read_text())
    assert actual["hermes"]["agent"]["system_prompt"] == "A temporary test persona."
    assert actual["models"] == cfg["models"]
    assert actual["omp"] == cfg["omp"]
    assert actual["approvals"] == cfg["approvals"]
    helper.undo_jailbreak(verbose=False)
    actual = yaml.safe_load(path.read_text())
    assert "system_prompt" not in actual["hermes"]["agent"]
    assert actual["omp"] == cfg["omp"]
    assert not (root / "config.yaml").exists()


def test_telephony_uses_profile_settings_and_shared_secret_file(tmp_path, monkeypatch):
    root = tmp_path / ".mercury-nightly"
    home = root / "hermes" / "profiles" / "calls"
    home.mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("MERCURY_CONFIG", str(root / "config.yaml"))
    (home / "config.yaml").write_text(yaml.safe_dump({"hermes": {"telephony": {"provider": "test"}}, "omp": {}}))
    helper = load_script(ROOT / "optional-skills/productivity/telephony/scripts/telephony.py", "mercury_phone_config_helper")
    assert helper._config_path() == home / "config.yaml"
    assert helper._env_path() == root / ".env"
    assert helper._config_lookup(("telephony", "provider")) == "test"


def test_flashcards_use_shared_library_across_profiles(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".mercury-nightly"
    monkeypatch.setenv("MERCURY_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(root / "hermes/profiles/first"))
    script = ROOT / "optional-skills/productivity/memento-flashcards/scripts/memento_cards.py"
    first = load_script(script, "mercury_cards_first_profile")
    first.cmd_add(argparse.Namespace(question="Two plus two?", answer="4", collection="Math"))
    assert json.loads(capsys.readouterr().out)["ok"] is True
    monkeypatch.setenv("HERMES_HOME", str(root / "hermes/profiles/second"))
    second = load_script(script, "mercury_cards_second_profile")
    assert second._load()["cards"][0]["answer"] == "4"
    assert second.CARDS_FILE == root / "skills/productivity/memento-flashcards/data/cards.json"
    assert not (root / "hermes/profiles").exists()
