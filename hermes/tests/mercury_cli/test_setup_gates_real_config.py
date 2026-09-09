"""Reconfigure gates against REAL config files (VM-gate regression).

The mock-based gate suite (test_setup_reconfigure_gates.py) passed while
the gates misfired on the live VM: every detection predicate was exercised
only with fabricated inputs, never with what ``save_config``/``load_config``
actually round-trip on disk. These tests write a real ``config.yaml`` (+ a
real ``.env`` where relevant) into an isolated home and assert the gate
summaries on the loaded result:

- model gate fires for OAuth-style installs (Nous: model named in config,
  no env keys, no active_provider) — previously read as unconfigured and
  setup fell straight into full model prompts;
- tools gate fires for config-file tools state (platform_toolsets,
  mcp_servers) without any of the three env keys — previously forced a
  full tool reconfiguration with no gate;
- fresh defaults stay silent (no gate, section runs);
- ``_read_model_slots`` finds the ``models:`` block nested under
  ``hermes:`` as ``save_config`` writes it (the delegate gate's probe was
  blind on real files).

Ambient credential leakage is neutralized narrowly (env reads + active
provider) so the assertions pin the *config-file* signal; the config
under test is always a real on-disk round trip, never a hand-built dict.
"""

from __future__ import annotations

import pytest

import mercury_cli.setup as setup_mod
from mercury_cli.config import DEFAULT_CONFIG, load_config, save_config


@pytest.fixture()
def real_home(tmp_path, monkeypatch):
    """An isolated Mercury home with config + env paths pinned to it."""
    home = tmp_path / "mhome"
    home.mkdir()
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("MERCURY_CONFIG", str(home / "config.yaml"))
    monkeypatch.delenv("HERMES_NONINTERACTIVE", raising=False)
    return home


@pytest.fixture()
def no_ambient_credentials(monkeypatch):
    """Neutralize ambient shell keys / auth store; the config file decides."""
    monkeypatch.setattr(setup_mod, "get_env_value", lambda key: None)
    monkeypatch.setattr(
        "mercury_cli.auth.get_active_provider", lambda: None
    )


def _save_and_load(config: dict) -> dict:
    save_config(config)
    return load_config()


def _configured_model_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    config["model"] = {"provider": "nous", "default": "nous/hermes-test"}
    config["models"] = {
        "default": "nous/hermes-test",
        "fallback": "",
        "delegate_model": "nous/hermes-test",
        "delegate_fallback": "",
    }
    return config


# ---------------------------------------------------------------------------
# model gate: OAuth-style installs (no env keys, no active provider)
# ---------------------------------------------------------------------------


def test_model_gate_fires_for_oauth_style_real_config(
    real_home, no_ambient_credentials
):
    loaded = _save_and_load(_configured_model_config())
    assert loaded["model"]["provider"] == "nous"  # real round trip kept it
    assert setup_mod._model_section_is_configured(loaded) is True
    assert (
        setup_mod._get_section_config_summary(loaded, "model")
        == "nous/hermes-test"
    )


def test_model_gate_fires_for_models_block_only_real_config(
    real_home, no_ambient_credentials
):
    config = dict(DEFAULT_CONFIG)
    config["model"] = ""
    config["models"] = {"default": "openrouter/test-model"}
    loaded = _save_and_load(config)
    assert setup_mod._model_section_is_configured(loaded) is True
    assert (
        setup_mod._get_section_config_summary(loaded, "model")
        == "openrouter/test-model"
    )


def test_model_gate_silent_for_fresh_default_real_config(
    real_home, no_ambient_credentials
):
    loaded = _save_and_load(dict(DEFAULT_CONFIG))
    assert setup_mod._model_section_is_configured(loaded) is False
    assert setup_mod._get_section_config_summary(loaded, "model") is None


def test_model_gate_end_to_end_skip_on_real_config(
    real_home, no_ambient_credentials, monkeypatch
):
    loaded = _save_and_load(_configured_model_config())
    monkeypatch.setattr(setup_mod, "is_interactive_stdin", lambda: True)
    monkeypatch.delenv("HERMES_NONINTERACTIVE", raising=False)
    asked = []
    monkeypatch.setattr(
        setup_mod,
        "prompt_yes_no",
        lambda question, default=True: asked.append((question, default))
        or False,
    )
    assert (
        setup_mod._skip_configured_section(loaded, "model", "Model & Provider")
        is True
    )
    assert [q for q, _ in asked if "Reconfigure" in q] == [
        "  Reconfigure model & provider?"
    ]
    assert asked[0][1] is False  # default NO is load-bearing


# ---------------------------------------------------------------------------
# tools gate: config-file tools state, no env keys
# ---------------------------------------------------------------------------


def test_tools_gate_fires_for_platform_toolsets_real_config(
    real_home, no_ambient_credentials
):
    config = dict(DEFAULT_CONFIG)
    config["platform_toolsets"] = {
        "cli": ["web_search", "browser", "tts"],
        "telegram": ["web_search"],
    }
    loaded = _save_and_load(config)
    summary = setup_mod._get_section_config_summary(loaded, "tools")
    assert summary is not None and "toolsets" in summary


def test_tools_gate_fires_for_mcp_servers_real_config(
    real_home, no_ambient_credentials
):
    config = dict(DEFAULT_CONFIG)
    config["mcp_servers"] = {"gh": {"command": "mcp-gh"}}
    loaded = _save_and_load(config)
    summary = setup_mod._get_section_config_summary(loaded, "tools")
    assert summary is not None and "MCP" in summary


def test_tools_gate_fires_for_real_env_key(real_home, monkeypatch):
    from mercury_cli.config import save_env_value

    for var in ("ELEVENLABS_API_KEY", "BROWSERBASE_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "mercury_cli.auth.get_active_provider", lambda: None
    )
    save_env_value("ELEVENLABS_API_KEY", "test-elevenlabs-key")
    loaded = _save_and_load(dict(DEFAULT_CONFIG))
    summary = setup_mod._get_section_config_summary(loaded, "tools")
    assert summary is not None and "TTS/ElevenLabs" in summary


def test_tools_gate_silent_for_fresh_default_real_config(
    real_home, no_ambient_credentials
):
    loaded = _save_and_load(dict(DEFAULT_CONFIG))
    assert setup_mod._get_section_config_summary(loaded, "tools") is None


def test_tools_gate_end_to_end_skip_on_real_config(
    real_home, no_ambient_credentials, monkeypatch
):
    config = dict(DEFAULT_CONFIG)
    config["platform_toolsets"] = {"cli": ["web_search"]}
    loaded = _save_and_load(config)
    monkeypatch.setattr(setup_mod, "is_interactive_stdin", lambda: True)
    monkeypatch.delenv("HERMES_NONINTERACTIVE", raising=False)
    asked = []
    monkeypatch.setattr(
        setup_mod,
        "prompt_yes_no",
        lambda question, default=True: asked.append((question, default))
        or False,
    )
    assert (
        setup_mod._skip_configured_section(loaded, "tools", "Tools") is True
    )
    assert [q for q, _ in asked if "Reconfigure" in q] == [
        "  Reconfigure tools?"
    ]


# ---------------------------------------------------------------------------
# delegate probe: nested models block on real files
# ---------------------------------------------------------------------------


def test_read_model_slots_finds_nested_block_on_real_file(real_home):
    loaded = _save_and_load(_configured_model_config())
    assert loaded["models"]["default"] == "nous/hermes-test"
    slots = setup_mod._read_model_slots()
    assert slots["default"] == "nous/hermes-test"
    assert slots["delegate_model"] == "nous/hermes-test"


def test_read_model_slots_empty_for_fresh_real_file(real_home):
    _save_and_load(dict(DEFAULT_CONFIG))
    assert setup_mod._read_model_slots() == {
        "default": "",
        "fallback": "",
        "delegate_model": "",
        "delegate_fallback": "",
    }
