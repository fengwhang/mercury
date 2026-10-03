"""Shared selections survive native writes and produce profile-local views."""
from copy import deepcopy

import pytest
import yaml

from mercury_cli.config import load_config, save_config, set_config_value, unset_config_value
from mercury_cli.model_settings import canonical_model_document, hermes_model_view, save_hermes_model_view


PRIMARY = "openai-codex/gpt-6.1-sol"
FALLBACK = "nous/xiaomi/mimo-v2.6-pro"


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "profile/config.yaml"
    path.parent.mkdir()
    monkeypatch.setenv("MERCURY_CONFIG", str(path))
    monkeypatch.setenv("MERCURY_HOME", str(path.parent))
    monkeypatch.setenv("HERMES_HOME", str(path.parent / "hermes"))
    path.write_text(yaml.safe_dump({
        "models": {"default": PRIMARY, "fallback": FALLBACK, "delegate_model": PRIMARY,
                   "delegate_fallback": FALLBACK, "reasoning_overrides": {PRIMARY: "high", FALLBACK: "medium"},
                   "context_windows": {PRIMARY: 872000}},
        "hermes": {"model": {"default": "old", "provider": "openrouter", "base_url": "https://old.example/v1"},
                   "agent": {"max_turns": 150, "reasoning_overrides": {PRIMARY: "xhigh"}},
                   "fallback_providers": [{"provider": "openrouter", "model": "stale"}],
                   "approvals": {"mode": "smart"}},
        "omp": {"tools": {"approvalMode": "write"}},
    }))
    return path


def test_both_native_and_raw_readers_obey_shared_choices(config_file):
    from mercury_cli.config import read_raw_config
    for native in (load_config(), read_raw_config()):
        assert native["model"]["default"] == "gpt-6.1-sol"
        assert native["model"]["provider"] == "openai-codex"
        assert "base_url" not in native["model"]
        assert native["fallback_providers"] == [{"provider": "nous", "model": "xiaomi/mimo-v2.6-pro"}]
        assert native["agent"]["reasoning_overrides"][PRIMARY] == "high"


def test_native_round_trip_removes_model_mirrors_and_keeps_permissions(config_file):
    view = load_config()
    view["display"]["skin"] = "test-skin"
    save_config(view)
    whole = yaml.safe_load(config_file.read_text())
    assert "model" not in whole["hermes"]
    assert "fallback_providers" not in whole["hermes"]
    assert "reasoning_overrides" not in whole["hermes"]["agent"]
    assert whole["models"]["default"] == PRIMARY
    assert whole["models"]["context_windows"][PRIMARY] == 872000
    assert whole["hermes"]["approvals"]["mode"] == "smart"
    assert whole["omp"]["tools"]["approvalMode"] == "write"
    assert load_config()["model"]["provider"] == "openai-codex"


@pytest.mark.parametrize("key,value,expected", [
    ("models.default", "nous/anthropic/claude-test", "nous/anthropic/claude-test"),
    ("model.default", "gpt-next", "openai-codex/gpt-next"),
    ("hermes.model.default", "gpt-next", "openai-codex/gpt-next"),
])
def test_config_set_updates_the_authority_instead_of_a_dead_mirror(config_file, key, value, expected):
    set_config_value(key, value)
    whole = yaml.safe_load(config_file.read_text())
    assert whole["models"]["default"] == expected
    assert "model" not in whole["hermes"]
    assert load_config()["model"]["default"] == expected.partition("/")[2]


def test_stale_native_settings_save_does_not_undo_a_shared_model_change(config_file):
    view = load_config()
    whole = yaml.safe_load(config_file.read_text())
    whole["models"]["default"] = FALLBACK
    config_file.write_text(yaml.safe_dump(whole))
    view["display"]["skin"] = "changed"
    save_config(view)
    assert yaml.safe_load(config_file.read_text())["models"]["default"] == FALLBACK
    assert load_config()["model"]["provider"] == "nous"


def test_legacy_migration_preserves_endpoint_credentials_and_native_behaviour():
    whole = {"hermes": {
        "model": {"default": "vendor/model", "provider": "private", "base_url": "https://private.example/v1", "key_env": "PRIVATE_KEY"},
        "agent": {"reasoning_effort": "low", "max_turns": 23},
    }, "omp": {"modelRoles": {"task": "nous/vendor/delegate:high"}, "defaultThinkingLevel": "medium"}}
    migrated = canonical_model_document(whole)
    assert migrated["models"]["default"] == "private/vendor/model"
    assert migrated["models"]["delegate_model"] == "nous/vendor/delegate"
    assert migrated["models"]["reasoning_overrides"]["nous/vendor/delegate"] == "high"
    assert migrated["models"]["reasoning_overrides"]["private/vendor/model"] == "low"
    assert "modelRoles" not in migrated["omp"]
    assert hermes_model_view(migrated)["model"]["base_url"] == "https://private.example/v1"
    assert hermes_model_view(migrated)["model"]["key_env"] == "PRIVATE_KEY"
    assert canonical_model_document(migrated) == migrated


def test_a_deliberate_native_model_edit_updates_shared_selection_without_other_slots(config_file):
    whole = yaml.safe_load(config_file.read_text())
    view = hermes_model_view(whole)
    edited = deepcopy(view)
    edited["model"] = {"provider": "nous", "default": "xiaomi/mimo-v2.6-pro"}
    saved = save_hermes_model_view(whole, edited, previous=view)
    assert saved["models"]["default"] == FALLBACK
    assert saved["models"]["delegate_model"] == PRIMARY


def test_native_behavior_writes_do_not_resurrect_stale_selections(config_file):
    set_config_value("hermes.display.skin", "changed")
    set_config_value("omp.tools.approvalMode", "yolo")
    whole = yaml.safe_load(config_file.read_text())
    assert whole["models"]["default"] == PRIMARY
    assert whole["hermes"]["display"]["skin"] == "changed"
    assert whole["omp"]["tools"]["approvalMode"] == "yolo"
    assert whole["hermes"]["approvals"]["mode"] == "smart"


def test_native_omp_alias_writes_shared_selection_and_effort(config_file):
    set_config_value("omp.delegateModel", FALLBACK + ":low")
    whole = yaml.safe_load(config_file.read_text())
    assert whole["models"]["delegate_model"] == FALLBACK
    assert whole["models"]["reasoning_overrides"][FALLBACK] == "low"
    assert whole["models"]["default"] == PRIMARY
    assert "delegateModel" not in whole["omp"]


def test_unsetting_shared_effort_cannot_import_an_old_native_override(config_file):
    unset_config_value("models.reasoning_overrides")
    whole = yaml.safe_load(config_file.read_text())
    assert whole["models"].get("reasoning_overrides", {}) == {}
    assert PRIMARY not in load_config()["agent"].get("reasoning_overrides", {})


def test_partial_native_map_edit_preserves_other_shared_model_choices(config_file):
    set_config_value("omp.modelReasoningOverrides.nous/xiaomi/mimo-v2.6-pro", "off")
    set_config_value("omp.modelContextWindows.nous/xiaomi/mimo-v2.6-pro", "123456")
    whole = yaml.safe_load(config_file.read_text())
    assert whole["models"]["reasoning_overrides"] == {PRIMARY: "high", FALLBACK: "off"}
    assert whole["models"]["context_windows"] == {PRIMARY: 872000, FALLBACK: 123456}
    assert "modelReasoningOverrides" not in whole["omp"]
    assert "modelContextWindows" not in whole["omp"]


def test_explicit_empty_shared_reasoning_map_suppresses_legacy_pins(config_file):
    whole = yaml.safe_load(config_file.read_text())
    whole["models"]["reasoning_overrides"] = {}
    whole["models"]["orchestrator_thinking_level"] = "xhigh"
    config_file.write_text(yaml.safe_dump(whole))
    assert hermes_model_view(whole)["agent"]["reasoning_overrides"] == {}
    canonical = canonical_model_document(whole)
    assert canonical["models"]["reasoning_overrides"] == {}
    assert "orchestrator_thinking_level" not in canonical["models"]


def test_native_hermes_effort_alias_keeps_off_as_a_model_setting(config_file):
    set_config_value("hermes.agent.reasoning_overrides.nous/xiaomi/mimo-v2.6-pro", "off")
    whole = yaml.safe_load(config_file.read_text())
    assert whole["models"]["reasoning_overrides"] == {PRIMARY: "high", FALLBACK: "off"}
    assert load_config()["agent"]["reasoning_overrides"][FALLBACK] == "none"
