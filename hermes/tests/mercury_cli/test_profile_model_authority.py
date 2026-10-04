"""Profiles resolve all four slots centrally on real engine entry paths."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mercury_cli import profiles
from mercury_cli.config import load_config, save_config
from mercury_cli.profile_defaults import ProfileModelError, resolve_model_defaults, save_profile_models
from mercury_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture(params=["mercury", "mercury-nightly"])
def installation(tmp_path, monkeypatch, request):
    root = tmp_path / (".mercury-nightly" if request.param.endswith("nightly") else ".mercury")
    (root / "hermes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key, value in {"HOME": tmp_path, "MERCURY_HOME": root, "HERMES_HOME": root / "hermes",
                       "MERCURY_CONFIG": root / "config.yaml", "MERCURY_CMD": request.param}.items():
        monkeypatch.setenv(key, str(value))
    slots = {"default": "openai-codex/chat", "fallback": "nous/vendor/chat-retry",
             "delegate_model": "openai-codex/code", "delegate_fallback": "nous/vendor/code-retry",
             "reasoning_overrides": {"openai-codex/chat": "high"},
             "context_windows": {"openai-codex/chat": 872000}}
    main = {"models": slots, "hermes": {"model_options": {"openai-codex/chat": {"base_url": "https://example.test/codex"}},
                                       "approvals": {"mode": "smart"}}, "omp": {"tools": {"approvalMode": "write"}}}
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    home = profiles.create_profile("research", no_alias=True, no_skills=True)
    token = set_hermes_home_override(home)
    try:
        yield root, home, slots
    finally:
        reset_hermes_home_override(token)


def test_fresh_profile_gateway_and_cli_inherit_all_slots_live(installation):
    root, home, slots = installation
    from gateway.run import _load_gateway_config
    assert "models" not in yaml.safe_load((home / "config.yaml").read_text())
    assert "profile_models" not in yaml.safe_load((root / "config.yaml").read_text())
    assert resolve_model_defaults({}, home / "config.yaml")["models"]["delegate_fallback"] == slots["delegate_fallback"]
    for config in (load_config(), _load_gateway_config(home / "config.yaml")):
        assert config["model"]["provider"] == "openai-codex"
        assert config["model"]["default"] == "chat"
        assert config["model"]["base_url"] == "https://example.test/codex"
        assert config["fallback_providers"] == [{"provider": "nous", "model": "vendor/chat-retry"}]
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["models"]["default"] = "nous/vendor/next-chat"
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    assert load_config()["model"]["default"] == "vendor/next-chat"
    assert _load_gateway_config(home / "config.yaml")["model"]["provider"] == "nous"
    native = load_config()
    native["display"]["skin"] = "test"
    save_config(native)
    assert "models" not in yaml.safe_load((home / "config.yaml").read_text())
    assert "profile_models" not in yaml.safe_load((root / "config.yaml").read_text())


def test_explicit_profile_is_complete_and_ignores_legacy_mirrors(installation):
    root, home, _ = installation
    save_profile_models(home, {"default": "nous/vendor/own-chat", "delegate_model": "nous/vendor/own-code"})
    local = yaml.safe_load((home / "config.yaml").read_text())
    local["models"] = {"default": "openrouter/stale", "delegate_model": "openrouter/stale-code"}
    local["hermes"]["fallback_providers"] = [{"provider": "openrouter", "model": "stale-retry"}]
    (home / "config.yaml").write_text(yaml.safe_dump(local))
    from gateway.run import _load_gateway_config
    for config in (load_config(), _load_gateway_config(home / "config.yaml")):
        assert config["model"]["provider"] == "nous"
        assert config["model"]["default"] == "vendor/own-chat"
        assert config["fallback_providers"] == []
    main = yaml.safe_load((root / "config.yaml").read_text())
    assert main["models"]["default"] == "openai-codex/chat"
    assert main["profile_models"]["research"]["delegate_model"] == "nous/vendor/own-code"


@pytest.mark.parametrize("bad", [None, {}, {"default": "openrouter/only-chat"},
                                  {"default": "wrong", "delegate_model": "nous/code"},
                                  {"default": "nous/chat", "delegate_model": "nous/code", "context_windows": {"nous/chat": -1}}])
def test_invalid_override_hard_errors_even_after_cached_success(installation, bad):
    root, home, _ = installation
    load_config()
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["profile_models"] = {"research": bad}
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    from gateway.run import _load_gateway_config
    for read in (load_config, lambda: _load_gateway_config(home / "config.yaml")):
        with pytest.raises(ProfileModelError, match="profile_models.research"):
            read()


def test_cli_inherit_removes_only_selected_override(installation):
    root, home, slots = installation
    save_profile_models(home, slots)
    sibling = profiles.create_profile("sibling", no_alias=True, no_skills=True)
    save_profile_models(sibling, {**slots, "default": "nous/vendor/sibling"})
    before = yaml.safe_load((root / "config.yaml").read_text())
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-m", "mercury_cli.main", "profile", "models", "research", "--inherit"],
                            cwd=repo, env=os.environ.copy(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    after = yaml.safe_load((root / "config.yaml").read_text())
    assert "research" not in after["profile_models"]
    assert after["models"] == before["models"]
    assert after["profile_models"]["sibling"] == before["profile_models"]["sibling"]
    assert load_config()["model"]["default"] == "chat"


def test_picker_commits_four_provider_qualified_models_atomically(installation, monkeypatch):
    root, home, _ = installation
    from mercury_cli import setup, auth, main, models, context_settings
    before = (root / "config.yaml").read_bytes()
    local_before = (home / "config.yaml").read_bytes()
    picked = iter(["gpt-example", "vendor/retry", "gpt-code", "vendor/code-retry"])
    providers = iter(["openai-codex", "nous", "openai-codex", "nous"])
    efforts = []
    def provider(labels, **kwargs):
        assert (root / "config.yaml").read_bytes() == before
        slug = next(providers)
        return [p.slug for p in models.CANONICAL_PROVIDERS].index(slug)
    monkeypatch.setattr(main, "_prompt_provider_choice", provider)
    monkeypatch.setattr(models, "provider_model_ids", lambda *a, **kw: ["gpt-example", "vendor/retry", "gpt-code", "vendor/code-retry"])
    monkeypatch.setattr(models, "get_pricing_for_provider", lambda *a, **kw: None)
    monkeypatch.setattr(auth, "_prompt_model_selection", lambda *a, **kw: next(picked))
    monkeypatch.setattr(setup, "_pick_reasoning_level", lambda *a, **kw: efforts.append(kw["model"]) or "high")
    monkeypatch.setattr(setup, "is_noninteractive", lambda: False)
    monkeypatch.setattr(setup, "is_interactive_stdin", lambda: True)
    monkeypatch.setattr(context_settings, "model_context_windows", lambda *a: {"default": 100000, "maximum": 100000})
    monkeypatch.setattr(setup, "_curses_prompt_choice", lambda *a: 0)
    main.cmd_profile(argparse.Namespace(profile_action="models", profile_name="research", inherit=False))
    saved = yaml.safe_load((root / "config.yaml").read_text())
    explicit = saved["profile_models"]["research"]
    assert [explicit[key] for key in ("default", "fallback", "delegate_model", "delegate_fallback")] == [
        "openai-codex/gpt-example", "nous/vendor/retry", "openai-codex/gpt-code", "nous/vendor/code-retry"]
    assert efforts == [explicit[key] for key in ("default", "fallback", "delegate_model", "delegate_fallback")]
    assert explicit["context_windows"][explicit["delegate_model"]] == 100000
    assert saved["models"] == yaml.safe_load(before)["models"]
    assert (home / "config.yaml").read_bytes() == local_before
    assert load_config()["model"]["default"] == "gpt-example"


def test_cancelled_picker_writes_nothing(installation, monkeypatch):
    root, home, _ = installation
    from mercury_cli import setup
    from mercury_cli.profile_defaults import configure_profile_models
    before = (root / "config.yaml").read_bytes()
    def cancel(*a, **kw):
        kw["draft"]["default"] = "nous/not-saved"
        raise setup._SetupCancelled()
    monkeypatch.setattr(setup, "_prompt_mercury_slots", cancel)
    configure_profile_models("research")
    assert (root / "config.yaml").read_bytes() == before


def test_central_overrides_follow_clone_rename_export_import_and_delete(installation, tmp_path, monkeypatch):
    root, home, slots = installation
    selected = {**slots, "default": "nous/vendor/research"}
    save_profile_models(home, selected)
    clone = profiles.create_profile("cloned", clone_from="research", clone_config=True, no_alias=True)
    assert resolve_model_defaults({}, clone / "config.yaml")["models"]["default"] == selected["default"]
    monkeypatch.setattr(profiles, "_check_gateway_running", lambda *a: False)
    monkeypatch.setattr(profiles, "create_wrapper_script", lambda *a, **kw: None)
    renamed = profiles.rename_profile("cloned", "renamed")
    main = yaml.safe_load((root / "config.yaml").read_text())
    assert "cloned" not in main["profile_models"]
    assert main["profile_models"]["renamed"]["default"] == selected["default"]
    archive = profiles.export_profile("renamed", str(tmp_path / "profile.tar.gz"))
    imported = profiles.import_profile(str(archive), name="imported")
    assert "models" not in yaml.safe_load((imported / "config.yaml").read_text())
    assert resolve_model_defaults({}, imported / "config.yaml")["models"]["default"] == selected["default"]
    monkeypatch.setattr(profiles, "_stop_profile_backends", lambda *a: None)
    profiles.delete_profile("renamed", yes=True)
    main = yaml.safe_load((root / "config.yaml").read_text())
    assert "renamed" not in main["profile_models"]
    assert main["profile_models"]["research"]["default"] == selected["default"]
    assert main["profile_models"]["imported"]["default"] == selected["default"]


def test_gateway_fallback_refresh_uses_explicit_profile_chain_and_rejects_bad_config(installation):
    root, home, slots = installation
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._fallback_model = None
    assert runner._refresh_fallback_model() == [{"provider": "nous", "model": "vendor/chat-retry"}]
    save_profile_models(home, {**slots, "fallback": "openrouter/own-retry"})
    assert runner._refresh_fallback_model() == [{"provider": "openrouter", "model": "own-retry"}]
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["profile_models"]["research"] = None
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    with pytest.raises(ProfileModelError):
        runner._refresh_fallback_model()


def test_public_bridge_uses_same_four_slots_and_fails_closed(installation):
    root, home, slots = installation
    repo = Path(__file__).resolve().parents[3]
    env = {**os.environ, "MERCURY_CONFIG": str(home / "config.yaml"), "HERMES_OMP_CONFIG": str(home / "config.yaml"), "HERMES_HOME": str(home)}
    result = subprocess.run([sys.executable, str(repo / "bridge/bridge.py"), "--delegate"],
                            env=env, cwd=repo, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nous/vendor/code-retry" in result.stdout
    assert "openai-codex/code" in result.stdout
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["profile_models"] = {"research": {"default": "nous/chat"}}
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    result = subprocess.run([sys.executable, str(repo / "bridge/bridge.py"), "--delegate"],
                            env=env, cwd=repo, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "profile_models.research" in result.stderr
    assert "openai-codex/code" not in result.stdout


def test_explicit_unknown_provider_errors_instead_of_using_main_login(installation):
    _, home, slots = installation
    from mercury_cli.runtime_provider import resolve_runtime_provider
    from mercury_cli.auth import AuthError
    save_profile_models(home, {**slots, "default": "nonexistent-profile-provider/no-model", "fallback": ""})
    with pytest.raises(AuthError, match="Unknown provider"):
        resolve_runtime_provider()
    from gateway.run import _resolve_runtime_agent_kwargs
    with pytest.raises(RuntimeError):
        _resolve_runtime_agent_kwargs()


def test_interactive_cli_loader_rejects_invalid_override(installation):
    root, home, _ = installation
    from cli import load_cli_config
    assert load_cli_config()["model"]["default"] == "chat"
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["profile_models"] = {"research": None}
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    with pytest.raises(ProfileModelError):
        load_cli_config()
