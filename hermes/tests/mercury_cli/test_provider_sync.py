"""Provider logins must interoperate without replaying or cross-scoping grants."""
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mercury_cli import auth
from mercury_cli.provider_sync import exchange


def jwt(account="workspace-a", expiry=None, email="person@example.test"):
    payload = {"exp": expiry if expiry is not None else time.time() + 3600,
               "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": "pro"},
               "https://api.openai.com/profile": {"email": email}}
    return "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path / "mercury"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "mercury/hermes"))


def test_hermes_chatgpt_login_becomes_omp_oauth_with_workspace_metadata():
    token = jwt()
    auth._save_codex_tokens({"access_token": token, "refresh_token": "fake-refresh"})
    record = exchange({"operation": "snapshot"})["records"][0]
    credential = record["credential"]
    assert record["provider"] == "openai-codex"
    assert credential["type"] == "oauth"  # Never mislabel a subscription as an API key.
    assert credential["accountId"] == credential["orgId"] == "workspace-a"
    assert credential["orgName"] == "pro"
    assert credential["expires"] > time.time() * 1000
    assert credential["access"] == token


def test_omp_api_key_login_is_usable_by_hermes_pool():
    exchange({"operation": "upsert", "provider": "openai", "credentials": [{"type": "api_key", "key": "fake-key"}]})
    from agent.credential_pool import load_pool
    selected = load_pool("openai-api").select()
    assert selected is not None
    assert selected.access_token == "fake-key"
    assert selected.auth_type == "api_key"


def test_omp_login_updates_existing_singleton_and_keeps_other_workspaces():
    auth._save_codex_tokens({"access_token": jwt(), "refresh_token": "old-refresh"})
    exchange({"operation": "snapshot"})
    for workspace in ("workspace-a", "workspace-b"):
        exchange({"operation": "upsert", "provider": "openai-codex", "credentials": [{
            "type": "oauth", "access": jwt(workspace), "refresh": f"new-{workspace}",
            "expires": (time.time() + 3600) * 1000, "accountId": workspace, "orgId": workspace,
            "email": "person@example.test", "orgName": "Team"}]})
    rows = exchange({"operation": "snapshot"})["records"]
    assert {row["credential"]["orgId"] for row in rows} == {"workspace-a", "workspace-b"}
    assert {row["credential"]["refresh"] for row in rows} == {"new-workspace-a", "new-workspace-b"}
    assert auth._read_codex_tokens()["tokens"]["refresh_token"] == "new-workspace-a"


def test_stale_omp_migration_cannot_replace_fresher_hermes_login():
    auth._save_codex_tokens({"access_token": jwt(), "refresh_token": "fresh"})
    result = exchange({"operation": "adopt", "provider": "openai-codex", "credentials": [{
        "type": "oauth", "access": jwt(expiry=time.time() - 60), "refresh": "spent",
        "expires": 0, "accountId": "workspace-a", "orgId": "workspace-a", "email": "person@example.test"}]})
    assert result["records"][0]["credential"]["refresh"] == "fresh"


def test_shared_workspace_does_not_collapse_distinct_members():
    for email in ("one@example.test", "two@example.test"):
        exchange({"operation": "upsert", "provider": "openai-codex", "credentials": [{
            "type": "oauth", "access": jwt(email=email), "refresh": "fake-" + email,
            "expires": (time.time() + 3600) * 1000, "accountId": "workspace-a", "orgId": "workspace-a", "email": email}]})
    assert {row["credential"]["email"] for row in exchange({"operation": "snapshot"})["records"]} == {
        "one@example.test", "two@example.test"}


def test_removed_singleton_does_not_resurrect_at_next_snapshot():
    auth._save_codex_tokens({"access_token": jwt(), "refresh_token": "fake"})
    row = exchange({"operation": "snapshot"})["records"][0]
    exchange({"operation": "remove", "provider": "openai-codex", "id": row["id"]})
    assert exchange({"operation": "snapshot"})["records"] == []
    assert not (auth._load_auth_store().get("providers") or {}).get("openai-codex")


def test_shared_refresh_spends_single_use_token_once(monkeypatch):
    auth._save_codex_tokens({"access_token": jwt(expiry=time.time() - 60), "refresh_token": "single-use"})
    row = exchange({"operation": "snapshot"})["records"][0]
    spent = []

    def refresh(access, refresh, **kwargs):
        spent.append(refresh)
        time.sleep(0.05)
        return {"access_token": jwt(), "refresh_token": "rotated", "last_refresh": "2026-09-30T00:00:00Z"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", refresh)
    request = {"operation": "refresh", "provider": "openai-codex", "id": row["id"], "observedRefresh": "single-use"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(exchange, [request, request]))
    assert spent == ["single-use"]
    assert all(result["records"][0]["credential"]["refresh"] == "rotated" for result in results)


def test_profile_snapshot_does_not_adopt_default_credentials(tmp_path, monkeypatch):
    auth._save_codex_tokens({"access_token": jwt(), "refresh_token": "default-profile-only"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "mercury/profiles/private"))
    assert exchange({"operation": "snapshot"})["records"] == []


def test_incompatible_oauth_and_gateway_key_fail_closed():
    with pytest.raises(ValueError):
        exchange({"operation": "upsert", "provider": "google-gemini-cli", "credentials": [{
            "type": "oauth", "access": "fake", "refresh": "fake", "expires": 0}]})
    with pytest.raises(ValueError):
        exchange({"operation": "upsert", "provider": "openai-codex", "credentials": [{"type": "api_key", "key": "gateway-key"}]})


def test_xai_setup_singleton_is_shared_and_logout_removes_it():
    auth._save_xai_oauth_tokens({"access_token": jwt(), "refresh_token": "xai-refresh"})
    row = exchange({"operation": "snapshot"})["records"][0]
    assert row["provider"] == "xai-oauth"
    exchange({"operation": "remove", "provider": "xai-oauth", "id": row["id"]})
    assert exchange({"operation": "snapshot"})["records"] == []


def test_profile_owned_anthropic_singleton_is_shared_and_suppression_survives_logout():
    from agent.anthropic_credentials import _write_hermes_oauth_credentials
    _write_hermes_oauth_credentials("claude-access", "claude-refresh", int((time.time() + 3600) * 1000))
    row = exchange({"operation": "snapshot"})["records"][0]
    assert row["credential"]["access"] == "claude-access"
    assert row["credential"]["expires"] > time.time() * 1000
    exchange({"operation": "remove", "provider": "anthropic", "id": row["id"]})
    assert exchange({"operation": "snapshot"})["records"] == []


def test_explicit_anthropic_api_key_choice_does_not_revive_old_subscription(monkeypatch):
    from agent.anthropic_credentials import _write_hermes_oauth_credentials
    _write_hermes_oauth_credentials("old-claude-access", "old-claude-refresh", int((time.time() + 3600) * 1000))
    exchange({"operation": "snapshot"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-console-key")
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert exchange({"operation": "snapshot"})["records"] == []


def test_suppressed_hermes_singleton_does_not_reappear_in_omp():
    auth._save_codex_tokens({"access_token": jwt(), "refresh_token": "fake"})
    exchange({"operation": "snapshot"})
    auth.suppress_credential_source("openai-codex", "device_code")
    assert exchange({"operation": "snapshot"})["records"] == []


def test_omp_anthropic_login_refresh_uses_json_and_commits_for_hermes(monkeypatch):
    from agent import anthropic_credentials
    calls = []
    def refresh(token, *, use_json=False):
        calls.append((token, use_json))
        return {"access_token": "claude-new", "refresh_token": "claude-rotated",
                "expires_at_ms": int((time.time() + 3600) * 1000)}
    monkeypatch.setattr(anthropic_credentials, "refresh_anthropic_oauth_pure", refresh)
    row = exchange({"operation": "upsert", "provider": "anthropic", "credentials": [{
        "type": "oauth", "access": "claude-old", "refresh": "claude-refresh", "expires": 0,
        "accountId": "claude-account", "orgId": "claude-org"}]})["records"][0]
    refreshed = exchange({"operation": "refresh", "provider": "anthropic", "id": row["id"],
                          "observedRefresh": "claude-refresh"})["records"][0]
    assert calls == [("claude-refresh", True)]
    assert refreshed["credential"]["refresh"] == "claude-rotated"
    assert refreshed["credential"]["orgId"] == "claude-org"


def test_native_hermes_adopts_rotated_omp_codex_grant_before_forced_refresh(monkeypatch):
    from agent.credential_pool import CredentialPool, PooledCredential
    row = exchange({"operation": "upsert", "provider": "openai-codex", "credentials": [{
        "type": "oauth", "access": jwt(expiry=time.time() - 60), "refresh": "single-use",
        "expires": 0, "accountId": "workspace-a", "orgId": "workspace-a"}]})["records"][0]
    entries = [PooledCredential.from_dict("openai-codex", entry) for entry in auth.read_credential_pool("openai-codex")]
    native = CredentialPool("openai-codex", entries)
    spent = []
    def refresh(access, refresh, **kwargs):
        spent.append(refresh)
        return {"access_token": jwt(), "refresh_token": "rotated"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", refresh)
    exchange({"operation": "refresh", "provider": "openai-codex", "id": row["id"], "observedRefresh": "single-use"})
    updated = native._refresh_entry(entries[0], force=True)
    assert updated.refresh_token == "rotated"
    assert spent == ["single-use"]
