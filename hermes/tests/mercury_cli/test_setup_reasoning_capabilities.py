"""Provider reasoning choices and their real persistence/runtime boundaries."""

from contextlib import redirect_stdout
from argparse import Namespace
from io import StringIO
from pathlib import Path
import importlib.util

import pytest
import yaml

from mercury_cli import models, setup
from mercury_cli.omp_sync import _write_slots, _current_reasoning_overrides


@pytest.fixture
def interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MERCURY_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("HERMES_OMP_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "omp"))
    monkeypatch.setenv("MERCURY_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(setup, "is_noninteractive", lambda: False)
    monkeypatch.setattr(setup, "is_interactive_stdin", lambda: True)
    return tmp_path / "config.yaml"


def _seed_catalog(monkeypatch, entries, provider="openrouter"):
    parsed = {entry["id"]: models.parse_openrouter_reasoning_capabilities(entry) for entry in entries}
    monkeypatch.setattr(models, f"_{provider}_reasoning_caps_cache", parsed)


def _entry(model, efforts, *, mandatory=False, default=None):
    reasoning = {"supported_efforts": efforts, "mandatory": mandatory}
    if default is not None:
        reasoning["default_effort"] = default
    return {"id": model, "supported_parameters": ["tools", "reasoning"], "reasoning": reasoning}


@pytest.mark.parametrize("efforts,mandatory,expected", [
    (["high", "low"], True, ["low", "high"]),
    (["high", "low"], False, ["off", "low", "high"]),
    (["none", "high"], False, ["off", "high"]),
    (["none", "high"], True, ["high"]),
    (None, True, ["minimal", "low", "medium", "high", "xhigh", "max"]),
    (None, False, ["off", "minimal", "low", "medium", "high", "xhigh", "max"]),
])
def test_api_efforts_and_mandatory_controls_define_menu(interactive, monkeypatch, efforts, mandatory, expected):
    _seed_catalog(monkeypatch, [_entry("vendor/model", efforts, mandatory=mandatory)])
    seen = {}

    def choose(title, choices, default):
        seen.update(choices=choices, default=default)
        return default

    monkeypatch.setattr(setup, "_curses_prompt_choice", choose)
    value = setup._pick_reasoning_level("Reasoning", model="openrouter/vendor/model", allow_auto=True)
    assert seen["choices"] == expected
    assert value in expected
    assert "auto" not in expected


@pytest.mark.parametrize("current,api_default,expected", [
    ("low", "medium", "low"),
    ("xhigh", "medium", "medium"),
    ("", "medium", "medium"),
    ("", None, "high"),
    ("", "none", "off"),
])
def test_default_is_current_or_api_default_or_supported_level(interactive, monkeypatch, current, api_default, expected):
    _seed_catalog(monkeypatch, [_entry("model", ["low", "medium", "high"], default=api_default)])
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: default)
    assert setup._pick_reasoning_level("Reasoning", current, model="openrouter/model") == expected


@pytest.mark.parametrize("metadata", [
    {"id": "model", "supported_parameters": ["tools"]},
    {"id": "model", "supported_parameters": ["tools", "reasoning"], "reasoning": {}},
    {"id": "model", "supported_parameters": ["tools", "reasoning"]},
    _entry("model", []),
])
def test_no_selector_does_not_invent_efforts(interactive, monkeypatch, metadata):
    _seed_catalog(monkeypatch, [metadata])

    def unexpected_menu(*args, **kwargs):
        pytest.fail("model without an API effort selector received a menu")

    monkeypatch.setattr(setup, "_curses_prompt_choice", unexpected_menu)
    assert setup._pick_reasoning_level("Reasoning", "high", model="openrouter/model") is None


@pytest.mark.parametrize("result", [None, RuntimeError("catalog unavailable")])
def test_unknown_metadata_keeps_current_setting(interactive, monkeypatch, result):
    def capabilities(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(models, "model_reasoning_capabilities", capabilities)
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *a, **kw: pytest.fail("guessed menu"))
    assert setup._pick_reasoning_level("Reasoning", "high", model="private/model") is None


def test_aggregators_use_serving_provider_metadata(interactive, monkeypatch):
    _seed_catalog(monkeypatch, [_entry("same/model", ["low"], mandatory=True)])
    _seed_catalog(monkeypatch, [_entry("same/model", ["max"], mandatory=True)], "nous")
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: default)
    assert setup._pick_reasoning_level("Reasoning", model="openrouter/same/model") == "low"
    assert setup._pick_reasoning_level("Reasoning", model="nous/same/model") == "max"


def test_headless_does_not_fetch_metadata(interactive, monkeypatch):
    monkeypatch.setattr(setup, "is_noninteractive", lambda: True)
    monkeypatch.setattr(models, "model_reasoning_capabilities", lambda *a: pytest.fail("headless API request"))
    assert setup._pick_reasoning_level("Reasoning", model="openrouter/model") is None


def test_cancel_keeps_reasoning_file_and_config_unchanged(interactive, monkeypatch):
    _seed_catalog(monkeypatch, [_entry("model", ["high"], mandatory=True)])
    _write_slots({"default": "openrouter/model", "reasoning_overrides": {"openrouter/model": "high"}})
    before = interactive.read_bytes()
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *a: -1)
    config = {"agent": {"reasoning_overrides": {"openrouter/model": "high"}}}
    setup._prompt_slot_reasoning(config, "openrouter/model", "", "", "")
    assert interactive.read_bytes() == before
    assert config == {"agent": {"reasoning_overrides": {"openrouter/model": "high"}}}


def _bridge():
    path = Path(__file__).resolve().parents[3] / "bridge" / "bridge.py"
    spec = importlib.util.spec_from_file_location("setup_reasoning_test_bridge", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


@pytest.mark.parametrize("configured_delegates", [False, True])
def test_four_models_are_followed_by_reasoning_and_reach_runtime_chains(interactive, monkeypatch, configured_delegates):
    from mercury_cli.config import load_config, save_config

    names = ["main", "fallback", "delegate", "delegate-fallback"]
    levels = ["xhigh", "high", "max", "off"]
    _seed_catalog(monkeypatch, [_entry(n, ["none" if level == "off" else level], mandatory=level != "off")
                                for n, level in zip(names, levels)])
    selected = ["openrouter/" + n for n in names]
    _write_slots({"default": selected[0], **({
        "delegate_model": "openrouter/old-delegate",
        "delegate_fallback": "openrouter/old-delegate-fallback",
    } if configured_delegates else {})})
    monkeypatch.setattr(setup, "_ask_reconfigure", lambda *a: pytest.fail("separate delegate gate hid fallback choices"))
    menus = []
    events = []
    remaining = iter(selected[1:])

    def select_main():
        events.append(("model", selected[0]))

    def select_slot(catalog, **kwargs):
        model = next(remaining)
        events.append(("model", model))
        return model

    def select_provider(choices, default=0, title=""):
        events.append(("provider", title))
        return default

    def choose(title, choices, default):
        menus.append((title, choices))
        events.append(("reasoning", title.split(" — ", 1)[0]))
        return default

    monkeypatch.setattr("mercury_cli.main.select_provider_and_model", select_main)
    monkeypatch.setattr("mercury_cli.main._prompt_provider_choice", select_provider)
    monkeypatch.setattr("mercury_cli.auth._prompt_model_selection", select_slot)
    monkeypatch.setattr(models, "provider_model_ids", lambda *args, **kwargs: names)
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda *args, **kwargs: {})
    monkeypatch.setattr(setup, "_skip_configured_section", lambda *args: False)
    monkeypatch.setattr(setup, "_curses_prompt_choice", choose)
    config = load_config()
    setup.setup_model_provider(config, quick=True)
    model_events = [(i, model) for i, (kind, model) in enumerate(events) if kind == "model"]
    assert [model for _, model in model_events] == selected
    for index, model in model_events:
        assert events[index + 1] == ("reasoning", model), events
    save_config(config)
    expected = dict(zip(selected, levels))
    assert len(menus) == 4
    assert [menu[1] for menu in menus] == [[level] for level in levels]
    saved = yaml.safe_load(interactive.read_text())
    assert saved["models"]["reasoning_overrides"] == expected
    assert _current_reasoning_overrides() == expected
    assert load_config()["agent"]["reasoning_overrides"] == {
        model: "none" if level == "off" else level for model, level in expected.items()
    }
    bridge = _bridge()
    slots = bridge.parse_config(str(interactive))
    assert bridge.validate(slots, need_delegate=True) == []
    bridge.render_omp_subtree(slots, target=str(interactive))
    runtime = yaml.safe_load(interactive.read_text())["omp"]
    assert runtime["defaultThinkingLevel"] == "max"
    assert runtime["retry"]["fallbackChains"] == {
        selected[2]: [selected[3] + ":off"],
    }
    env_output = StringIO()
    with redirect_stdout(env_output):
        bridge.render(slots, delegation=True)
    assert "OMP_FALLBACK_CHAIN=openrouter/delegate-fallback:off" in env_output.getvalue()
    assert "OMP_THINKING_LEVEL=max" in env_output.getvalue()



def test_existing_delegate_does_not_hide_main_fallback(interactive, monkeypatch):
    from mercury_cli.config import load_config, save_config

    _write_slots({"default": "openrouter/main", "delegate_model": "openrouter/delegate"})
    _seed_catalog(monkeypatch, [_entry("fallback", ["high"], mandatory=True), _entry("delegate", [])])
    selected_titles = []
    picks = iter(["fallback", "delegate", None])

    def select_slot(catalog, **kwargs):
        selected_titles.append(kwargs["title"])
        return next(picks)

    monkeypatch.setattr("mercury_cli.auth._prompt_model_selection", select_slot)
    monkeypatch.setattr("mercury_cli.main._prompt_provider_choice", lambda choices, default=0, **kw: default)
    monkeypatch.setattr(models, "provider_model_ids", lambda *args, **kw: ["fallback", "delegate"])
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda *args, **kw: {})
    monkeypatch.setattr(setup, "_ask_reconfigure", lambda *a: pytest.fail("unexpected delegate gate"))
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: default)
    config = load_config()
    setup._prompt_mercury_slots(config)
    save_config(config)
    saved = yaml.safe_load(interactive.read_text())["models"]
    assert "fallback model" in selected_titles[0]
    assert "delegate model" in selected_titles[1]
    assert "delegate fallback" in selected_titles[2]
    assert len(selected_titles) == 3
    assert saved["fallback"] == "openrouter/fallback"
    assert saved["delegate_model"] == "openrouter/delegate"
    assert saved["reasoning_overrides"] == {"openrouter/fallback": "high"}


def test_rejected_duplicates_and_skipped_slots_get_no_reasoning_prompt(interactive, monkeypatch):
    from mercury_cli.config import load_config, save_config

    _write_slots({"default": "openrouter/main"})
    _seed_catalog(monkeypatch, [_entry(name, ["high"], mandatory=True) for name in ("fallback", "delegate")])
    # Retry a duplicate main fallback; skip the delegate fallback.
    picks = iter(["main", "fallback", "delegate", None])
    models_with_reasoning = []
    monkeypatch.setattr("mercury_cli.auth._prompt_model_selection", lambda *args, **kwargs: next(picks))
    monkeypatch.setattr("mercury_cli.main._prompt_provider_choice", lambda choices, default=0, **kwargs: default)
    monkeypatch.setattr(models, "provider_model_ids", lambda *args, **kwargs: [])
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda *args, **kwargs: {})

    def choose(title, choices, default):
        models_with_reasoning.append(title.split(" — ", 1)[0])
        return default

    monkeypatch.setattr(setup, "_curses_prompt_choice", choose)
    config = load_config()
    setup._prompt_mercury_slots(config)
    save_config(config)
    assert models_with_reasoning == ["openrouter/fallback", "openrouter/delegate"]
    saved = yaml.safe_load(interactive.read_text())["models"]
    assert saved["reasoning_overrides"] == dict.fromkeys(models_with_reasoning, "high")
    assert saved["fallback_chain"] == []
    assert setup._read_model_slots()["delegate_fallback"] == ""
    assert saved["delegate_fallback_chain"] == []

def test_reasoning_map_rewrite_preserves_unrelated_settings(interactive):
    interactive.write_text(yaml.safe_dump({
        "models": {"default": "openrouter/main", "reasoning_overrides": {"old/model": "high"},
                   "fallback_chain": ["old/first", "old/second"], "orchestrator_thinking_level": "high"},
        "hermes": {"agent": {"max_turns": 150}}, "omp": {"tools": {"approvalMode": "write"}},
    }))
    _write_slots({"reasoning_overrides": {"new/model": "low"}})
    saved = yaml.safe_load(interactive.read_text())
    assert saved["models"]["reasoning_overrides"] == {"new/model": "low"}
    assert saved["models"]["fallback_chain"] == ["old/first", "old/second"]
    assert saved["models"]["orchestrator_thinking_level"] == "high"
    assert saved["hermes"]["agent"]["max_turns"] == 150
    assert saved["omp"]["tools"]["approvalMode"] == "write"


def test_profile_declared_efforts_are_used_without_family_guesses(interactive, monkeypatch):
    from providers.base import ProviderProfile
    import providers

    class Profile(ProviderProfile):
        def supported_reasoning_efforts(self, model):
            return ("low", "high") if model == "reasoning-model" else ()

    monkeypatch.setattr(providers, "get_provider_profile", lambda name: Profile(name="private"))
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: default)
    assert setup._pick_reasoning_level("Reasoning", model="private/reasoning-model") == "high"
    assert setup._pick_reasoning_level("Reasoning", model="private/plain-model") is None


def test_lmstudio_uses_account_endpoint_options(interactive, monkeypatch):
    monkeypatch.setattr(models, "_get_model_config_dict", lambda: {
        "provider": "lmstudio", "base_url": "http://localhost:1234/v1",
    })
    seen = {}

    def options(model, base_url, api_key):
        seen.update(model=model, base_url=base_url)
        return ["off", "high"]

    monkeypatch.setattr(models, "lmstudio_model_reasoning_options", options)
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: default)
    assert setup._pick_reasoning_level("Reasoning", model="lmstudio/local-model") == "high"
    assert seen == {"model": "local-model", "base_url": "http://localhost:1234/v1"}


def test_copilot_uses_live_capabilities_and_keeps_unlisted_models_unknown(interactive, monkeypatch):
    monkeypatch.setattr(models, "_resolve_copilot_catalog_api_key", lambda: "fake-test-token")
    monkeypatch.setattr(models, "fetch_github_model_catalog", lambda **kwargs: [
        {"id": "gpt-reasoning", "capabilities": {"supports": {"reasoning_effort": ["low", "high"]}}},
        {"id": "plain", "capabilities": {"supports": {}}},
    ])
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda title, choices, default: default)
    assert setup._pick_reasoning_level("Reasoning", model="copilot/gpt-reasoning") == "high"
    assert setup._pick_reasoning_level("Reasoning", model="copilot/plain") is None
    assert models.model_reasoning_capabilities("copilot", "unlisted") is None


@pytest.mark.parametrize("passthrough,expected_thinking", [
    ([], ["--thinking", "off"]),
    (["--thinking", "low"], ["--thinking", "low"]),
    (["--model", "private/other"], None),
])
def test_omp_cli_honors_selected_disable_and_explicit_overrides(
    interactive, monkeypatch, capsys, passthrough, expected_thinking,
):
    import shlex
    from mercury_cli.omp_command import cmd_omp

    _write_slots({"default": "openrouter/main", "delegate_model": "openrouter/delegate",
                  "fallback": "", "delegate_fallback": "",
                  "reasoning_overrides": {"openrouter/delegate": "off"}})
    monkeypatch.setattr("mercury_cli.omp_command._resolve_omp_binary", lambda: "/fake/omp")
    monkeypatch.setattr("tools.omp_delegation._nous_search_env_overrides", lambda: {})
    assert cmd_omp(Namespace(omp_args=passthrough, print_cmd=True)) == 0
    argv = shlex.split(capsys.readouterr().out)
    if expected_thinking is None:
        assert "--thinking" not in argv
    else:
        index = argv.index("--thinking")
        assert argv[index:index + 2] == expected_thinking
        assert argv.count("--thinking") == 1


def test_omp_one_shot_passes_configured_reasoning_disable(interactive, monkeypatch):
    import subprocess
    from mercury_cli.slash_exec import CommandContext
    from mercury_cli import omp_exec

    monkeypatch.setattr(omp_exec, "_render_delegate_env", lambda: (
        {"OMP_MODEL": "openrouter/delegate", "OMP_THINKING_LEVEL": "off"}, None,
    ))
    monkeypatch.setattr(omp_exec.shutil, "which", lambda path: "/fake/omp")
    seen = []

    def execute(argv, **kwargs):
        seen.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(omp_exec.subprocess, "run", execute)
    omp_exec._exec_omp(CommandContext(args="test task"))
    assert seen[-2:] == ["--thinking", "off"]
