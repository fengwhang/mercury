"""Private credential interchange for Mercury's Hermes and OMP engines.

Hermes' profile-local auth store is the authority. OMP keeps a usage/selection
mirror, but refreshes shared OAuth grants through this module and the SAME
cross-process auth lock as Hermes. Never copy grants to another installation,
profile, Codex CLI, or Claude CLI, and never put credentials in argv or logs.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any

from mercury_cli import auth

# Only alias identical credential contracts. Subscription OAuth is distinct
# from the public API-key provider (notably openai-codex vs openai-api).
OMP_TO_HERMES = {"openai": "openai-api", "google": "gemini", "github-copilot": "copilot", "kimi-code": "kimi-coding"}
HERMES_TO_OMP = {value: key for key, value in OMP_TO_HERMES.items()}
SHARED_OAUTH = frozenset({"openai-codex", "anthropic", "xai-oauth"})
IDENTITY_FIELDS = ("accountId", "email", "orgId", "orgName", "projectId", "enterpriseUrl", "authorizedAt")


def _provider(provider: str) -> str:
    return OMP_TO_HERMES.get(provider, provider)


def _identity(credential: dict[str, Any]) -> str:
    if credential.get("type") == "oauth":
        account = credential.get("accountId") or credential.get("email")
        if account:
            return json.dumps([str(account).lower(), str(credential.get("email") or "").lower(),
                               str(credential.get("orgId") or "")])
        value = credential.get("refresh") or credential.get("access") or ""
    else:
        value = credential.get("key") or ""
    return hashlib.sha256(str(value).encode()).hexdigest()


def _credential(entry: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("auth_type") != "oauth":
        key = entry.get("access_token") or entry.get("api_key")
        return {"type": "api_key", "key": key, "source": "login"} if isinstance(key, str) and key else None
    token = entry.get("access_token")
    refresh = entry.get("refresh_token")
    if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
        return None
    claims = auth._decode_jwt_claims(token)
    account = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    expiry = (float(claims["exp"]) * 1000 if isinstance(claims.get("exp"), (int, float))
              else entry.get("expires_at_ms"))
    if not isinstance(expiry, (int, float)):
        expiry = 0
    out = {"type": "oauth", "access": token, "refresh": refresh, "expires": expiry,
           **{key: metadata[key] for key in IDENTITY_FIELDS if key in metadata}}
    if isinstance(account, dict) and account.get("chatgpt_account_id"):
        out.setdefault("accountId", account["chatgpt_account_id"])
        out.setdefault("orgId", account["chatgpt_account_id"])
        if account.get("chatgpt_plan_type"):
            out.setdefault("orgName", account["chatgpt_plan_type"])
    if isinstance(profile, dict) and profile.get("email"):
        out.setdefault("email", profile["email"])
    if entry.get("provider") == "xai-oauth" and claims.get("sub"):
        out.setdefault("accountId", claims["sub"])
    if claims.get("email"):
        out.setdefault("email", claims["email"])
    return out


def _entries(store: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    pools = store.setdefault("credential_pool", {})
    if not isinstance(pools, dict):
        raise ValueError("Invalid credential pool")
    # Seed only this profile's owned logins, without discovering credentials
    # from other profiles or external CLIs. Setup can save these before a
    # runtime has ever loaded its credential pool.
    from agent.anthropic_credentials import read_hermes_oauth_credentials
    from agent.credential_pool import get_env_prefer_dotenv

    owned = []
    for provider in ("openai-codex", "xai-oauth"):
        state = (store.get("providers") or {}).get(provider) or {}
        tokens = state.get("tokens") or {}
        owned.append((provider, "device_code", state.get("label") or provider,
                      tokens.get("access_token"), tokens.get("refresh_token"), None))
    api_key_selected = bool(get_env_prefer_dotenv("ANTHROPIC_API_KEY")
                            and not (get_env_prefer_dotenv("ANTHROPIC_TOKEN")
                                     or get_env_prefer_dotenv("CLAUDE_CODE_OAUTH_TOKEN")))
    # Match the native pool's explicit API-key choice. An old discovered
    # subscription must not silently become a fallback for that choice.
    if api_key_selected and "anthropic" in pools:
        pools["anthropic"] = [entry for entry in pools["anthropic"]
                              if entry.get("source") not in {"mercury_pkce", "claude_code"}]
    anthropic = {} if api_key_selected else (read_hermes_oauth_credentials() or {})
    owned.append(("anthropic", "mercury_pkce", "Claude", anthropic.get("accessToken"),
                  anthropic.get("refreshToken"), anthropic.get("expiresAt")))
    suppressed = store.get("suppressed_sources") or {}
    for provider, source, label, access, refresh, expiry in owned:
        if not access or not refresh or source in suppressed.get(provider, []):
            continue
        entries = pools.setdefault(provider, [])
        aliases = [entry for entry in entries if isinstance(entry, dict) and entry.get("source") == source]
        if aliases:
            for entry in aliases:
                if entry.get("access_token") != access or entry.get("refresh_token") != refresh:
                    entry.update(access_token=access, refresh_token=refresh, last_status=None,
                                 last_error_reset_at=None, expires_at_ms=expiry)
        elif not any(isinstance(entry, dict) and entry.get("refresh_token") == refresh for entry in entries):
            entries.append({"id": uuid.uuid4().hex, "provider": provider, "auth_type": "oauth",
                            "source": source, "label": label, "access_token": access,
                            "refresh_token": refresh, "expires_at_ms": expiry, "priority": len(entries)})
    return pools


def _snapshot(store: dict[str, Any]) -> dict[str, Any]:
    entries = _entries(store)
    metadata = store.get("mercury_omp_identity") or {}
    records = []
    supported = set(SHARED_OAUTH)
    supported.update(key for key, value in auth.PROVIDER_REGISTRY.items() if value.auth_type == "api_key")
    for provider, pool in entries.items():
        if provider not in supported or not isinstance(pool, list):
            continue
        for entry in pool:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            if entry.get("source") in (store.get("suppressed_sources") or {}).get(provider, []):
                continue
            if entry.get("last_status") in {"auth_failed", "dead"}:
                continue
            if entry.get("auth_type") == "oauth" and provider not in SHARED_OAUTH:
                continue
            if entry.get("auth_type") != "oauth" and provider in SHARED_OAUTH and provider not in {
                key for key, value in auth.PROVIDER_REGISTRY.items() if value.auth_type == "api_key"
            }:
                continue  # A custom gateway key must not be sent to ChatGPT.
            credential = _credential(entry, metadata.get(entry["id"], {}))
            if credential:
                records.append({"id": entry["id"], "provider": HERMES_TO_OMP.get(provider, provider), "credential": credential})
    return {"records": records, "oauthProviders": sorted(SHARED_OAUTH),
            "apiKeyProviders": sorted(HERMES_TO_OMP.get(key, key) for key, value in auth.PROVIDER_REGISTRY.items() if value.auth_type == "api_key")}


def _remove_singleton_alias(store: dict[str, Any], provider: str, removed: list[dict[str, Any]]) -> None:
    if provider == "anthropic":
        if any(entry.get("source") == "mercury_pkce" for entry in removed):
            suppressed = store.setdefault("suppressed_sources", {}).setdefault(provider, [])
            if "mercury_pkce" not in suppressed:
                suppressed.append("mercury_pkce")
        return
    if provider not in {"openai-codex", "xai-oauth"}:
        return
    state = (store.get("providers") or {}).get(provider) or {}
    tokens = state.get("tokens") or {}
    if any(entry.get("access_token") == tokens.get("access_token") for entry in removed):
        (store.get("providers") or {}).pop(provider, None)
        if store.get("active_provider") == provider:
            store.pop("active_provider", None)


def exchange(request: dict[str, Any]) -> dict[str, Any]:
    """Internal structured request; callers must never display the response."""
    operation = request.get("operation")
    with auth._auth_store_lock(timeout_seconds=60):
        store = auth._load_auth_store()
        before = json.dumps(store, sort_keys=True)
        pools = _entries(store)
        if operation == "snapshot":
            result = _snapshot(store)
        else:
            omp_provider = str(request.get("provider") or "")
            provider = _provider(omp_provider)
            allowed_keys = {key for key, value in auth.PROVIDER_REGISTRY.items() if value.auth_type == "api_key"}
            if provider not in SHARED_OAUTH and provider not in allowed_keys:
                raise ValueError("Provider has no shared credential contract")
            pool = pools.setdefault(provider, [])
            if operation in ("upsert", "replace", "adopt"):
                supplied = request.get("credentials") or []
                if not isinstance(supplied, list) or not supplied:
                    raise ValueError("Missing credential")
                if operation == "replace":
                    _remove_singleton_alias(store, provider, pool)
                    pool = pools[provider] = []
                for credential in supplied:
                    if not isinstance(credential, dict):
                        raise ValueError("Invalid credential")
                    kind = credential.get("type")
                    if kind == "oauth" and provider not in SHARED_OAUTH:
                        raise ValueError("Incompatible OAuth flow")
                    if kind not in ("oauth", "api_key"):
                        raise ValueError("Invalid credential type")
                    if kind == "oauth" and not all(isinstance(credential.get(key), str) and credential[key] for key in ("access", "refresh")):
                        raise ValueError("Missing OAuth grant")
                    if kind == "api_key" and (provider not in allowed_keys or not credential.get("key")):
                        raise ValueError("Provider has no shared API-key contract")
                    metadata = store.setdefault("mercury_omp_identity", {})
                    entry = next((row for row in pool if _identity(_credential(row, metadata.get(row["id"], {})) or {}) == _identity(credential)), None)
                    if entry is not None and operation == "adopt":
                        continue  # A pre-bridge OMP mirror must not roll back a newer grant.
                    if entry is None:
                        source = "manual:mercury_pkce" if provider == "anthropic" and kind == "oauth" else "manual:mercury_omp"
                        entry = {"id": uuid.uuid4().hex, "provider": provider, "source": source,
                                 "label": credential.get("email") or "Mercury OMP", "priority": len(pool)}
                        pool.append(entry)
                    entry.update(auth_type="oauth" if kind == "oauth" else "api_key", last_status=None)
                    if kind == "oauth":
                        entry.update(access_token=credential["access"], refresh_token=credential["refresh"], expires_at_ms=credential.get("expires", 0))
                        if provider in {"openai-codex", "xai-oauth"} and entry.get("source") == "device_code":
                            state = store.setdefault("providers", {}).setdefault(provider, {})
                            state.setdefault("tokens", {}).update(access_token=credential["access"], refresh_token=credential["refresh"])
                        if provider == "anthropic" and entry.get("source") == "mercury_pkce":
                            from agent.anthropic_credentials import _write_hermes_oauth_credentials
                            _write_hermes_oauth_credentials(credential["access"], credential["refresh"], credential.get("expires", 0))
                    else:
                        entry["access_token"] = credential["key"]
                    metadata[entry["id"]] = {key: credential[key] for key in IDENTITY_FIELDS if key in credential}
                result = _snapshot(store)
            elif operation == "remove":
                row_id = request.get("id")
                removed = [entry for entry in pool if row_id is None or entry.get("id") == row_id]
                pools[provider] = [entry for entry in pool if entry not in removed]
                for entry in removed:
                    (store.get("mercury_omp_identity") or {}).pop(entry.get("id"), None)
                _remove_singleton_alias(store, provider, removed)
                result = _snapshot(store)
            elif operation == "refresh":
                if provider not in SHARED_OAUTH:
                    raise ValueError("Provider does not share OAuth refresh")
                row = next((entry for entry in pool if entry.get("id") == request.get("id")), None)
                if row is None:
                    raise ValueError("Credential removed")
                current = _credential(row, (store.get("mercury_omp_identity") or {}).get(row["id"], {}))
                if current is None:
                    raise ValueError("Credential unavailable")
                # Commit any singleton seeding BEFORE the native pool refresh
                # rereads the canonical file. A waiter adopts a peer's rotated
                # grant rather than posting the single-use refresh token twice.
                if json.dumps(store, sort_keys=True) != before:
                    auth._save_auth_store(store)
                if (current["refresh"] == request.get("observedRefresh")
                        or current.get("expires", 0) <= time.time() * 1000):
                    from agent.credential_pool import CredentialPool, PooledCredential
                    native = CredentialPool(provider, [PooledCredential.from_dict(provider, entry) for entry in pool])
                    entry = next(entry for entry in native._entries if entry.id == row["id"])
                    updated = native._refresh_entry(entry, force=True)
                    if updated is None:
                        raise ValueError("OAuth refresh failed; reauthenticate")
                # Refresh helpers already persist through the same reentrant lock.
                latest = auth._load_auth_store()
                result = _snapshot(latest)
                return result
            else:
                raise ValueError("Invalid interchange operation")
        if json.dumps(store, sort_keys=True) != before:
            auth._save_auth_store(store)
        return result


def main() -> int:
    # Engine-only IPC. An ordinary CLI invocation never dumps credentials.
    if os.environ.get("MERCURY_AUTH_IPC") != "1" or sys.stdin.isatty() or sys.stdout.isatty():
        print("This module is an internal engine credential channel.", file=sys.stderr)
        return 2
    try:
        request = json.loads(sys.stdin.buffer.read(1024 * 1024))
        result = exchange(request)
        sys.stdout.write(json.dumps(result))
        return 0
    except Exception:
        # Exceptions/provider response bodies can contain secret material.
        print("Credential interchange failed; inspect login status in Mercury.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
