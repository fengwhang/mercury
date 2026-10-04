"""Named profiles share inference defaults, without sharing private prompts."""
import json
from pathlib import Path

import pytest
import yaml

from mercury_cli import auth, profiles, provider_sync
from mercury_cli.config import load_config, save_config
from mercury_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.fixture(params=[".mercury", ".mercury-nightly"])
def installation(tmp_path, monkeypatch, request):
    root = tmp_path / request.param
    (root / "hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("MERCURY_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(root / "hermes"))
    monkeypatch.setenv("MERCURY_CONFIG", str(root / "config.yaml"))
    (root / "config.yaml").write_text(yaml.safe_dump({
        "models": {"default": "openai-codex/chat", "fallback": "nous/vendor/fallback",
                   "delegate_model": "openai-codex/code", "delegate_fallback": "nous/vendor/fallback",
                   "reasoning_overrides": {"openai-codex/chat": "high"},
                   "context_windows": {"openai-codex/chat": 872000}},
        "hermes": {"approvals": {"mode": "smart"}},
        "omp": {"tools": {"approvalMode": "write"}},
    }))
    (root / "hermes" / "auth.json").write_text(json.dumps({
        "version": 1, "providers": {"openai-codex": {"tokens": {
            "access_token": "synthetic-main-access", "refresh_token": "synthetic-main-refresh"}}}}))
    home = profiles.create_profile("research", no_alias=True, no_skills=True)
    token = set_hermes_home_override(str(home))
    try:
        yield root, home
    finally:
        reset_hermes_home_override(token)


def test_existing_profile_uses_main_login_and_refresh_owner(installation):
    root, home = installation
    # Existing profiles need no recreation or credentials copied into their folder.
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg.pop("profile")
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    assert auth._auth_file_path() == root / "hermes" / "auth.json"
    assert auth._read_codex_tokens()["tokens"]["access_token"] == "synthetic-main-access"
    with auth._provider_state_transaction("openai-codex") as (store, state, owner):
        assert owner == root / "hermes" / "auth.json"
        state["tokens"]["access_token"] = "synthetic-rotated-access"
        store["providers"]["openai-codex"] = state
        auth._save_auth_store(store, target_path=owner)
    assert auth._read_codex_tokens()["tokens"]["access_token"] == "synthetic-rotated-access"
    assert not (home / "auth.json").exists()


def test_live_models_central_overrides_and_native_save(installation):
    root, home = installation
    cfg = load_config()
    assert cfg["model"]["default"] == "chat"
    assert cfg["model"]["provider"] == "openai-codex"
    assert cfg["model_overrides"]["openai-codex"]["chat"]["context_window"] == 872000
    assert cfg["fallback_providers"][0]["provider"] == "nous"
    assert cfg["approvals"]["mode"] == "smart"
    cfg["display"]["skin"] = "test-skin"
    save_config(cfg)
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "models" not in raw
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["models"]["default"] = "openrouter/next-chat"
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    assert load_config()["model"]["default"] == "next-chat"
    from mercury_cli.profile_defaults import save_profile_models
    save_profile_models(home, {**main["models"], "default": "openrouter/local-chat"})
    assert load_config()["model"]["default"] == "local-chat"
    from mercury_cli.profile_defaults import resolve_model_defaults
    effective = resolve_model_defaults(raw, home / "config.yaml")
    assert effective["models"]["delegate_model"] == "openai-codex/code"
    assert effective["omp"]["tools"]["approvalMode"] == "write"


def test_api_keys_inherit_but_channel_credentials_do_not(installation):
    root, home = installation
    from agent.secret_scope import build_profile_secret_scope, get_secret, set_secret_scope, reset_secret_scope
    (root / ".env").write_text("OPENROUTER_API_KEY=synthetic-inherited\nOPENAI_API_KEY=synthetic-main\nTELEGRAM_BOT_TOKEN=private-channel\n")
    (home / ".env").write_text("OPENAI_API_KEY=synthetic-local\n")
    scope = build_profile_secret_scope(home)
    assert scope["OPENROUTER_API_KEY"] == "synthetic-inherited"
    assert scope["OPENAI_API_KEY"] == "synthetic-local"
    assert "TELEGRAM_BOT_TOKEN" not in scope
    token = set_secret_scope(scope)
    try:
        assert get_secret("OPENROUTER_API_KEY") == "synthetic-inherited"
    finally:
        reset_secret_scope(token)
    from agent.credential_pool import get_env_prefer_dotenv
    assert get_env_prefer_dotenv("OPENAI_API_KEY") == "synthetic-local"


def test_opt_out_keeps_own_login_and_model(installation):
    _, home = installation
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg["profile"] = {"inherit_models": False, "inherit_credentials": False}
    from mercury_cli.profile_defaults import save_profile_models
    save_profile_models(home, {"default": "openrouter/private-model", "delegate_model": "openrouter/private-code"})
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    assert auth._auth_file_path() == home / "auth.json"
    assert auth._global_auth_file_path() is None
    with pytest.raises(auth.AuthError):
        auth._read_codex_tokens()
    assert load_config()["model"]["default"] == "private-model"


def test_omp_credential_exchange_uses_same_main_owner(installation):
    root, home = installation
    main_before = yaml.safe_load((root / "config.yaml").read_text())
    exchange = provider_sync.exchange({"operation": "snapshot"})
    record = next(item for item in exchange["records"] if item["provider"] == "openai-codex")
    assert record["credential"]["access"] == "synthetic-main-access"
    assert not (home / "auth.json").exists()
    assert yaml.safe_load((root / "config.yaml").read_text()) == main_before


def test_anthropic_singleton_shares_the_refresh_owner(installation):
    root, _ = installation
    from agent.anthropic_credentials import _get_hermes_oauth_file
    assert _get_hermes_oauth_file() == root / "hermes" / ".anthropic_oauth.json"


def test_post_setup_and_spawn_bridge_preserve_inheritance(installation, monkeypatch):
    root, home = installation
    monkeypatch.setenv("MERCURY_CONFIG", str(home / "config.yaml"))
    monkeypatch.setenv("HERMES_OMP_CONFIG", str(home / "config.yaml"))
    from mercury_cli.omp_sync import sync_omp_from_setup
    before = (root / "config.yaml").read_bytes()
    assert sync_omp_from_setup(quiet=True)
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "models" not in raw
    assert "delegateModel" not in raw["omp"]
    assert (root / "config.yaml").read_bytes() == before


def test_gateway_turn_inherits_provider_keys_without_channel_or_prompt_leaks(installation):
    root, home = installation
    from gateway.run import _profile_runtime_scope
    from agent.secret_scope import get_secret, set_multiplex_active
    from mercury_constants import get_config_dir
    (root / ".env").write_text("OPENROUTER_API_KEY=synthetic-main-key\nTELEGRAM_BOT_TOKEN=main-bot\n")
    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(home):
            assert load_config()["model"]["provider"] == "openai-codex"
            assert auth._read_codex_tokens()["tokens"]["access_token"] == "synthetic-main-access"
            assert get_secret("OPENROUTER_API_KEY") == "synthetic-main-key"
            assert get_secret("TELEGRAM_BOT_TOKEN") is None
            assert get_config_dir() == home / "config"
    finally:
        set_multiplex_active(False)


def test_native_model_and_reasoning_edits_become_central_profile_overrides(installation):
    root, home = installation
    from mercury_cli.config import set_config_value
    before = yaml.safe_load((root / "config.yaml").read_text())
    set_config_value("hermes.agent.reasoning_effort", "xhigh")
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert yaml.safe_load((root / "config.yaml").read_text())["profile_models"]["research"]["reasoning_overrides"]["openai-codex/chat"] == "xhigh"
    assert "models" not in raw
    set_config_value("hermes.model.default", "custom-chat")
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert yaml.safe_load((root / "config.yaml").read_text())["profile_models"]["research"]["default"] == "openai-codex/custom-chat"
    assert yaml.safe_load((root / "config.yaml").read_text())["models"] == before["models"]


def test_provider_endpoints_also_remain_live_after_unrelated_saves(installation):
    root, home = installation
    main = yaml.safe_load((root / "config.yaml").read_text())
    main["hermes"]["model_options"] = {"openai-codex/chat": {"base_url": "https://old.example.test"}}
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    native = load_config()
    assert native["model"]["base_url"] == "https://old.example.test"
    save_config(native)
    raw = yaml.safe_load((home / "config.yaml").read_text())
    assert "openai-codex/chat" not in (raw["hermes"].get("model_options") or {})
    main["hermes"]["model_options"]["openai-codex/chat"]["base_url"] = "https://new.example.test"
    (root / "config.yaml").write_text(yaml.safe_dump(main))
    assert load_config()["model"]["base_url"] == "https://new.example.test"


def test_profile_created_from_an_inheriting_profile_also_inherits_main(installation):
    _, _ = installation
    sibling = profiles.create_profile("sibling", no_alias=True, no_skills=True)
    assert (sibling / "config.yaml").is_file()
    token = set_hermes_home_override(str(sibling))
    try:
        assert load_config()["model"]["default"] == "chat"
        assert auth._read_codex_tokens()["tokens"]["access_token"] == "synthetic-main-access"
    finally:
        reset_hermes_home_override(token)
