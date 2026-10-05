"""Tests for the Speech-to-Text category in `mercury tools` (tools_config).

Covers the STT provider picker rows, config writes (stt.provider /
use_gateway), the model picker catalog, config-only checklist exclusion,
and the faster_whisper post-setup readiness hook.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mercury_cli.tools_config import (  # noqa: E402
    _CONFIG_ONLY_TOOLSETS,
    CONFIGURABLE_TOOLSETS,
    STT_MODEL_CATALOG,
    TOOL_CATEGORIES,
    _checklist_toolset_keys,
    _configure_stt_model,
    _is_provider_active,
    _write_provider_config,
    apply_provider_selection,
)


def _stt_cat():
    return TOOL_CATEGORIES["stt"]


def _stt_provider_named(name):
    return next(p for p in _stt_cat()["providers"] if p["name"] == name)


class TestSttCategory:
    def test_stt_category_exists(self):
        cat = _stt_cat()
        assert cat["name"] == "Speech-to-Text"
        assert len(cat["providers"]) >= 5




    def test_managed_row_shares_tts_coverage_category(self):
        from mercury_cli.nous_subscription import MANAGED_FEATURE_COVERAGE_CATEGORY

        managed = [p for p in _stt_cat()["providers"] if p.get("managed_nous_feature")]
        assert managed, "expected a Nous Subscription row"
        for p in managed:
            assert p["managed_nous_feature"] == "stt"
        assert MANAGED_FEATURE_COVERAGE_CATEGORY["stt"] == "openai-audio"


class TestConfigWrites:
    def test_write_provider_config_sets_stt_provider(self):
        config = {"stt": {"use_gateway": True}}
        prov = _stt_provider_named("Groq")
        _write_provider_config(prov, config, managed_feature=None)
        assert config["stt"]["provider"] == "groq"
        # Legacy key is popped so the read-time shim can't override the pick.
        assert "use_gateway" not in config["stt"]


    def test_apply_provider_selection_stt(self):
        config = {}
        with patch(
            "mercury_cli.tools_config.get_nous_subscription_features"
        ) as feats:
            feats.return_value = MagicMock(
                nous_auth_present=False, account_info=None
            )
            apply_provider_selection("stt", "OpenAI", config)
        assert config["stt"]["provider"] == "openai"


class TestActiveDetection:
    def test_active_matches_config(self):
        config = {"stt": {"provider": "groq"}}
        assert _is_provider_active(_stt_provider_named("Groq"), config)
        assert not _is_provider_active(_stt_provider_named("OpenAI"), config)

    def test_unset_provider_defaults_to_local(self):
        assert _is_provider_active(_stt_provider_named("Local Whisper"), {})


class TestModelPicker:

    def test_catalog_matches_runtime_model_sets(self):
        from tools.transcription_tools import GROQ_MODELS, OPENAI_MODELS

        assert set(STT_MODEL_CATALOG["openai"]) == OPENAI_MODELS
        assert set(STT_MODEL_CATALOG["groq"]) == GROQ_MODELS




    def test_configure_stt_model_defaults_to_current(self):
        config = {"stt": {"openai": {"model": "gpt-transcribe"}}}
        with patch(
            "mercury_cli.tools_config._prompt_choice", return_value=0
        ) as pc:
            _configure_stt_model("openai", config)
        # default index should point at the currently configured model
        args = pc.call_args[0]
        assert args[2] == STT_MODEL_CATALOG["openai"].index("gpt-transcribe")


class TestConfigOnlyExclusion:
    def test_stt_is_config_only(self):
        assert "stt" in _CONFIG_ONLY_TOOLSETS

    def test_stt_excluded_from_checklist_universe(self):
        assert "stt" not in _checklist_toolset_keys("cli")
        # sanity: tts (a real toolset) stays in
        assert "tts" in _checklist_toolset_keys("cli")


class TestPostSetup:
    def test_faster_whisper_in_post_setup_ready(self):
        from mercury_cli.tools_config import _POST_SETUP_READY

        assert "faster_whisper" in _POST_SETUP_READY


def test_cli_checklist_opens_stt_provider_picker_and_saves_without_tool_schema(monkeypatch):
    from mercury_cli import tools_config as tc

    config = {"stt": {"provider": "local"}, "platform_toolsets": {"cli": ["terminal"]}}
    local = _stt_provider_named("Local Whisper")
    groq = _stt_provider_named("Groq")
    saved = []
    monkeypatch.setattr(tc, "_estimate_tool_tokens", lambda: {})
    monkeypatch.setattr(tc, "_get_effective_configurable_toolsets", lambda: [
        ("terminal", "Terminal", "shell commands"), ("stt", "Speech-to-Text", "voice")])
    monkeypatch.setattr(tc, "_toolset_has_keys", lambda *args, **kwargs: True)
    monkeypatch.setattr(tc, "_visible_providers", lambda *args, **kwargs: [local, groq])
    monkeypatch.setattr(tc, "_hidden_nous_gateway_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(tc, "_detect_active_provider_index", lambda *args, **kwargs: 0)

    def select_provider(title, choices, default):
        assert any("Groq" in choice for choice in choices)
        return next(i for i, choice in enumerate(choices) if "Groq" in choice)

    monkeypatch.setattr(tc, "_prompt_choice", select_provider)
    monkeypatch.setattr(tc, "_reconfigure_provider", lambda provider, config, **kwargs:
                        tc._write_provider_config(provider, config, managed_feature=None))
    monkeypatch.setattr(tc, "save_config", lambda cfg: saved.append(cfg["stt"]["provider"]))

    def choose_stt(title, labels, selected, **kwargs):
        assert "CLI" in title
        return selected | {next(i for i, label in enumerate(labels) if "Speech-to-Text provider" in label)}

    monkeypatch.setattr("mercury_cli.curses_ui.curses_checklist", choose_stt)
    selected = tc._prompt_toolset_checklist("🖥 CLI", {"terminal"}, config=config, force_fresh=False)
    assert selected == {"terminal"}
    assert config["stt"]["provider"] == "groq"
    assert saved == ["groq"]
    assert config["platform_toolsets"]["cli"] == ["terminal"]


def test_cancel_cli_checklist_preserves_stt_and_platform_tools(monkeypatch):
    from mercury_cli import tools_config as tc

    config = {"stt": {"provider": "groq"}}
    monkeypatch.setattr(tc, "_estimate_tool_tokens", lambda: {})
    monkeypatch.setattr(tc, "_get_effective_configurable_toolsets", lambda: [("terminal", "Terminal", "shell commands")])
    monkeypatch.setattr(tc, "_toolset_has_keys", lambda *args, **kwargs: True)
    monkeypatch.setattr("mercury_cli.curses_ui.curses_checklist", lambda title, labels, selected, **kwargs: kwargs["cancel_returns"])
    monkeypatch.setattr(tc, "save_config", lambda cfg: pytest.fail("cancel saved a configuration"))
    selected = tc._prompt_toolset_checklist("🖥 CLI", {"terminal"}, config=config, force_fresh=False)
    assert selected == {"terminal"}
    assert config == {"stt": {"provider": "groq"}}
