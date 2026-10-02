"""Context choices survive setup and control both engines under the active profile."""

import importlib.util
from pathlib import Path

import pytest
import yaml

from mercury_cli import setup
from mercury_cli.context_settings import parse_context_windows
from mercury_cli.omp_sync import _write_slots


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile" / "config.yaml"
    path.parent.mkdir()
    monkeypatch.setenv("MERCURY_HOME", str(path.parent))
    monkeypatch.setenv("MERCURY_CONFIG", str(path))
    monkeypatch.setenv("HERMES_HOME", str(path.parent / "hermes"))
    monkeypatch.setenv("HERMES_OMP_CONFIG", str(path))
    monkeypatch.setattr(setup, "is_noninteractive", lambda: False)
    monkeypatch.setattr(setup, "is_interactive_stdin", lambda: True)
    path.write_text("hermes:\n  model:\n    default: main\n    provider: openai-codex\nmodels:\n  default: openai-codex/main\n")
    return path


def bridge():
    path = Path(__file__).resolve().parents[3] / "bridge" / "bridge.py"
    spec = importlib.util.spec_from_file_location("context_test_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("payload,expected", [
    ({"context_window": 272000, "max_context_window": 872000}, {"default": 272000, "maximum": 872000}),
    ({"context_length": 64000}, {"default": 64000, "maximum": 64000}),
    ({"max_context_window": 128000}, {"default": 128000, "maximum": 128000}),
    ({"context_window": 64000, "max_context_window": 32000}, {"default": 64000, "maximum": 64000}),
    ({"context_window": True, "max_context_window": -1}, None),
])
def test_advertised_default_maximum_and_malformed_metadata(payload, expected):
    assert parse_context_windows(payload) == expected


@pytest.mark.parametrize("metadata,choice,custom,expected", [
    ({"default": 272000, "maximum": 872000}, 1, None, 872000),
    ({"default": 64000, "maximum": 64000}, 1, "32000", 32000),
    (None, 1, "96000", 96000),
])
def test_window_selection_reaches_hermes_fallback_and_omp_configuration(profile, monkeypatch, metadata, choice, custom, expected):
    from mercury_cli.config import load_config, save_config
    from agent.model_metadata import get_model_context_length

    monkeypatch.setattr("mercury_cli.context_settings.model_context_windows", lambda *args: metadata)
    menus = []
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: menus.append(choices) or choice)
    monkeypatch.setattr(setup, "prompt", lambda *args: custom)
    config = load_config()
    setup._prompt_model_context(config, "openai-codex/fallback")
    save_config(config)
    saved = yaml.safe_load(profile.read_text())
    assert saved["models"]["context_windows"] == {"openai-codex/fallback": expected}
    assert get_model_context_length("fallback", provider="openai-codex") == expected
    if metadata and metadata["default"] == metadata["maximum"]:
        assert len(menus[0]) == 2  # a single advertised window gets no duplicate Maximum
    module = bridge()
    module.render_omp_subtree(module.parse_config(str(profile)), target=str(profile))
    assert yaml.safe_load(profile.read_text())["omp"]["modelContextWindows"] == saved["models"]["context_windows"]


def test_invalid_custom_window_reprompts_instead_of_exceeding_provider_max(profile, monkeypatch):
    from mercury_cli.config import load_config, save_config
    monkeypatch.setattr("mercury_cli.context_settings.model_context_windows", lambda *args: {"default": 64000, "maximum": 64000})
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *args: 1)
    answers = iter(["64001", "-1", "32k", "48000"])
    monkeypatch.setattr(setup, "prompt", lambda *args: next(answers))
    config = load_config()
    setup._prompt_model_context(config, "openai-codex/main")
    save_config(config)
    assert yaml.safe_load(profile.read_text())["models"]["context_windows"]["openai-codex/main"] == 48000


def test_cancel_preserves_window_and_unrelated_models(profile, monkeypatch):
    from mercury_cli.config import load_config
    _write_slots({"context_windows": {"openai-codex/main": 872000, "openrouter/other": 100000}})
    before = profile.read_bytes()
    monkeypatch.setattr("mercury_cli.context_settings.model_context_windows", lambda *args: {"default": 272000, "maximum": 872000})
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *args: None)
    setup._prompt_model_context(load_config(), "openai-codex/main")
    assert profile.read_bytes() == before


@pytest.mark.parametrize("choice,custom,expected", [(0, None, 0.5), (1, None, 0.75), (2, "62.5%", 0.625)])
def test_shared_percentage_removes_overriding_caps_and_changes_real_compressor(profile, monkeypatch, choice, custom, expected):
    from mercury_cli.config import load_config, save_config
    from agent.context_compressor import ContextCompressor
    config = load_config()
    config["compression"].update(threshold_tokens=1000, model_thresholds={"main": 0.1}, enabled=False)
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *args: choice)
    monkeypatch.setattr(setup, "prompt", lambda *args: custom)
    setup._prompt_compaction(config)
    save_config(config)
    effective = load_config()["compression"]
    assert effective["enabled"] is True
    assert effective.get("threshold_tokens") is None
    assert not effective.get("model_thresholds")
    compressor = ContextCompressor("main", provider="openai-codex", config_context_length=200000,
                                   threshold_percent=effective["threshold"], max_tokens=0,
                                   respect_threshold_percent=effective["respect_threshold_percent"])
    assert compressor.threshold_tokens == int(200000 * expected)
    module = bridge()
    module.render_omp_subtree(module.parse_config(str(profile)), target=str(profile))
    omp = yaml.safe_load(profile.read_text())["omp"]["compaction"]
    assert omp["thresholdPercent"] == expected * 100
    assert omp["thresholdTokens"] == -1
    assert omp["enabled"] is True


def test_clearing_automatic_choice_removes_old_window_after_wizard_save(profile, monkeypatch):
    from mercury_cli.config import load_config, save_config
    _write_slots({"context_windows": {"openai-codex/main": 872000}})
    monkeypatch.setattr("mercury_cli.context_settings.model_context_windows", lambda *args: None)
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *args: 0)
    config = load_config()
    setup._prompt_model_context(config, "openai-codex/main")
    save_config(config)
    assert yaml.safe_load(profile.read_text())["models"]["context_windows"] == {}
    assert "context_window" not in load_config()["model_overrides"]["openai-codex"]["main"]


def test_real_agent_uses_selected_window_and_percentage_without_codex_autoraise(profile, monkeypatch):
    from mercury_cli.config import load_config, save_config
    from mercury_state import SessionDB
    from run_agent import AIAgent

    _write_slots({"default": "openai-codex/gpt-5.5", "context_windows": {"openai-codex/gpt-5.5": 872000}})
    config = load_config()
    config["model"]["default"] = "gpt-5.5"
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *args: 0)
    setup._prompt_compaction(config)
    save_config(config)
    agent = AIAgent(model="gpt-5.5", provider="openai-codex", api_key="fixture-token",
                    base_url="https://chatgpt.com/backend-api/codex", enabled_toolsets=[],
                    skip_memory=True, skip_context_files=True, quiet_mode=True,
                    session_db=SessionDB(db_path=profile.parent / "state.db"), session_id="context-choice")
    assert agent.context_compressor.context_length == 872000
    assert agent.context_compressor.threshold_tokens == 436000
    assert agent._compression_threshold_autoraised is None
    agent.context_compressor.update_model("small", 64000, max_tokens=0)
    assert agent.context_compressor.threshold_tokens == 32000


def test_codex_context_catalog_does_not_borrow_another_account_maximum(profile, monkeypatch):
    from mercury_cli import codex_models
    import httpx

    def catalog(url, headers, **kwargs):
        maximum = 872000 if headers["Authorization"] == "Bearer account-a" else 400000
        return httpx.Response(200, json={"models": [{"slug": "gpt-6.1-sol", "context_window": 272000,
                                                    "max_context_window": maximum}]})
    monkeypatch.setattr(httpx, "get", catalog)
    for token, maximum in [("account-a", 872000), ("account-b", 400000)]:
        windows = codex_models.codex_model_context_windows("gpt-6.1-sol", access_token=token, allow_fetch=True)
        assert windows == {"default": 272000, "maximum": maximum}
