"""Codex model discovery from API, local cache, and config."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

import os

logger = logging.getLogger(__name__)

# Account/route-scoped metadata. Keep tokens out of cache keys and do not
# borrow another account's Codex CLI cache for request capabilities.
_reasoning_catalogs: dict[tuple[str, str], tuple[float, dict[str, dict]]] = {}
_reasoning_attempts: dict[tuple[str, str], float] = {}
_context_catalogs: dict[tuple[str, str], dict[str, dict]] = {}


def _catalog_key(access_token: str, base_url: Optional[str]) -> tuple[str, str]:
    from mercury_cli.auth import DEFAULT_CODEX_BASE_URL
    return ((base_url or DEFAULT_CODEX_BASE_URL).strip().rstrip("/"),
            hashlib.sha256(access_token.encode()).hexdigest())


def parse_codex_reasoning_capabilities(item: dict) -> Optional[dict]:
    """Retain the effort vocabulary advertised by the account's /models API."""
    levels = item.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        return None
    efforts = []
    for level in levels:
        value = level.get("effort") if isinstance(level, dict) else level
        if isinstance(value, str) and value.strip():
            normalized = value.strip().lower()
            if normalized not in efforts:
                efforts.append(normalized)
    if levels and not efforts:
        return None  # malformed metadata is unknown, not an off-only model
    positive = [effort for effort in efforts if effort not in ("none", "off")]
    result = {"supports_reasoning": bool(positive), "supported_efforts": efforts,
              "supports_effort_selection": bool(positive),
              "mandatory": bool(positive) and not any(e in ("none", "off") for e in efforts)}
    default = item.get("default_reasoning_level")
    if isinstance(default, str) and default.strip().lower() in efforts:
        result["default_effort"] = default.strip().lower()
    return result


def codex_model_reasoning_capabilities(
    model: str, *, access_token: Optional[str] = None,
    base_url: Optional[str] = None, allow_fetch: bool = False,
) -> Optional[dict]:
    """Shared setup/runtime capabilities; refresh once per hour, retry failures later."""
    # Runtime callers supply the serving request's credentials. Do not read
    # saved credentials here: that could probe an unrelated account during a
    # fallback, or refresh the operator's OAuth state from an isolated test.
    if not isinstance(access_token, str) or not access_token:
        return None
    key = _catalog_key(access_token, base_url)
    now = time.monotonic()
    cached = _reasoning_catalogs.get(key)
    if allow_fetch and (cached is None or now - cached[0] >= 3600):
        last_attempt = _reasoning_attempts.get(key)
        if last_attempt is None or now - last_attempt >= 60:
            _reasoning_attempts[key] = now
            _fetch_models_from_api(access_token, base_url)
            cached = _reasoning_catalogs.get(key)
    from agent.model_metadata import strip_codex_context_variant_suffix
    return cached[1].get(strip_codex_context_variant_suffix(model)) if cached else None

def codex_model_context_windows(
    model: str, *, access_token: Optional[str] = None,
    base_url: Optional[str] = None, allow_fetch: bool = False,
) -> Optional[dict]:
    """Account-scoped default/max context metadata from the same effort probe."""
    codex_model_reasoning_capabilities(model, access_token=access_token,
                                      base_url=base_url, allow_fetch=allow_fetch)
    if not access_token:
        return None
    from agent.model_metadata import strip_codex_context_variant_suffix
    return _context_catalogs.get(_catalog_key(access_token, base_url), {}).get(
        strip_codex_context_variant_suffix(model))


DEFAULT_CODEX_MODELS: List[str] = [
    # Verified in current upstream Codex discovery/compatibility rules. Live
    # account discovery remains authoritative about entitlement.
    "gpt-6.1-sol",
    "gpt-6-sol",
    "gpt-6-luna",
    # GPT-5.6 series (Sol/Terra/Luna). The public API exposes "-pro"
    # variants, but the ChatGPT Codex OAuth backend rejects them with HTTP 400,
    # so the curated offline fallback must not surface those dead choices.
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    # gpt-5.3-codex-spark is in research preview and is exposed *only* via
    # the Codex CLI / OAuth backend (chatgpt.com/backend-api/codex/models)
    # for ChatGPT Pro subscribers. It is NOT available in the public OpenAI
    # API, so it intentionally stays out of the "openai" provider catalog
    # in mercury_cli/models.py — only the openai-codex (OAuth) provider
    # surfaces it. The Codex backend reports ``supported_in_api: false`` for
    # this slug; that flag describes API availability, not Codex backend
    # availability, so the fetch/cache code paths below intentionally do
    # not filter on it. PR #12994 removed this entry on the assumption it
    # was unsupported — that was wrong; restored here. Keep it in the
    # curated fallback so Pro users still see Spark in `/model` when live
    # discovery is unavailable (offline first run, transient API failure).
    "gpt-5.3-codex-spark",
    # NOTE: gpt-5.2-codex / gpt-5.1-codex-max / gpt-5.1-codex-mini were
    # previously listed here but the chatgpt.com Codex backend returns
    # HTTP 400 "The '<model>' model is not supported when using Codex with
    # a ChatGPT account." for all three on every ChatGPT Pro account we've
    # tested (verified live 2026-05-27). Keeping them in the fallback list
    # leaked dead slugs into /model when live discovery was unavailable
    # (transient API failure, first-run before refresh) and surfaced HTTP 400
    # crashes on selection. The Codex CLI public catalog still references
    # these slugs, which is why they survived previously — but those entries
    # describe the public OpenAI API, not the OAuth-backed Codex backend
    # Mercury uses. Removed here. If OpenAI re-enables them on Codex backend,
    # live discovery will pick them up automatically via _fetch_models_from_api.
]

_FORWARD_COMPAT_TEMPLATE_MODELS: List[tuple[str, tuple[str, ...]]] = [
    ("gpt-5.6-sol", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-terra", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.6-luna", ("gpt-5.5", "gpt-5.4")),
    ("gpt-5.5", ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex")),
    ("gpt-5.4-mini", ("gpt-5.3-codex",)),
    ("gpt-5.4", ("gpt-5.3-codex",)),
    # Surface Spark whenever any compatible Codex template is present so
    # accounts hitting the live endpoint with an older lineup still see
    # Spark in the picker. Backend gates real availability by ChatGPT Pro
    # entitlement; Mercury does not.
    ("gpt-5.3-codex-spark", ("gpt-5.3-codex",)),
]


def _add_forward_compat_models(model_ids: List[str]) -> List[str]:
    """Add Clawdbot-style synthetic forward-compat Codex models.

    If a newer Codex slug isn't returned by live discovery, surface it when an
    older compatible template model is present. This mirrors Clawdbot's
    synthetic catalog / forward-compat behavior for GPT-5 Codex variants.
    """
    ordered: List[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id not in seen:
            ordered.append(model_id)
            seen.add(model_id)

    for synthetic_model, template_models in _FORWARD_COMPAT_TEMPLATE_MODELS:
        if synthetic_model in seen:
            continue
        if any(template in seen for template in template_models):
            ordered.append(synthetic_model)
            seen.add(synthetic_model)

    return ordered


def _add_context_variants(model_ids: List[str]) -> List[str]:
    """Insert ``-900k`` large-context picker variants after eligible base slugs.

    The ChatGPT Codex backend advertises 272K for the gpt-5.4 / gpt-5.6
    families but accepts ~911K (live-verified Aug 2026). The base slugs keep
    the cheaper advertised 272K limit by default; each verified slug gets an
    explicit ``<slug>-900k`` picker entry that opts into the large window.
    The suffix is Mercury-side only — it is stripped before the model id hits
    the wire (agent/transports/codex.py, agent/auxiliary_client.py).
    """
    from agent.model_metadata import (
        CODEX_CONTEXT_VARIANT_SUFFIX,
        has_codex_context_variant,
    )

    out: List[str] = []
    present = set(model_ids)
    for model_id in model_ids:
        out.append(model_id)
        variant = model_id + CODEX_CONTEXT_VARIANT_SUFFIX
        if variant in present or variant in out:
            continue
        if has_codex_context_variant(model_id):
            out.append(variant)
    return out


def _finalize_codex_models(model_ids: List[str]) -> List[str]:
    """Forward-compat synthesis + large-context variant synthesis."""
    return _add_context_variants(_add_forward_compat_models(model_ids))


def _extract_chatgpt_account_id(access_token: str) -> Optional[str]:
    """Best-effort extraction of ``chatgpt_account_id`` from the OAuth JWT.

    The Codex backend requires the ``ChatGPT-Account-Id`` header for the
    per-account catalog. Without it, ``GET /backend-api/codex/models``
    returns ``{"models":[]}`` (HTTP 200) — which masquerades as "no
    models available" and silently degrades the picker to the curated
    fallback list. The request-side path in ``auxiliary_client.py``
    already extracts the same claim; this mirrors that logic here so the
    probe sees the same catalog the request path will actually use.

    Returns ``None`` on any parse error — the probe then degrades
    gracefully to the unauthenticated fallback list instead of crashing.
    """
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = (
            claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
            if isinstance(claims, dict)
            else None
        )
        return acct_id if isinstance(acct_id, str) and acct_id else None
    except Exception:
        return None


def _fetch_models_from_api(access_token: str, base_url: Optional[str] = None) -> List[str]:
    """Fetch available models from the Codex API. Returns visible models sorted by priority."""
    try:
        import httpx
        headers = {"Authorization": f"Bearer {access_token}"}
        acct_id = _extract_chatgpt_account_id(access_token)
        if acct_id:
            headers["ChatGPT-Account-Id"] = acct_id
        from mercury_cli.auth import DEFAULT_CODEX_BASE_URL
        base = (base_url or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")
        entries = []
        # Hermes upstream bddd22be uses the newest-version probe and the old
        # ungated sentinel fallback, rather than a stale client roster. Fetch
        # from the credential's own route, never an unrelated hardcoded host.
        for version in ("99.0.0", "0.0.0"):
            resp = httpx.get(f"{base}/models?client_version={version}", headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            entries = data.get("models", []) if isinstance(data, dict) else []
            if entries:
                break
    except Exception as exc:
        logger.debug("Failed to fetch Codex models from API: %s", exc)
        return []

    sortable = []
    capabilities = {}
    contexts = {}
    from mercury_cli.context_settings import parse_context_windows
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        # Codex CLI's catalog uses ``supported_in_api`` for the public OpenAI
        # API, not for the OAuth-backed Codex backend that this provider uses.
        # Some valid Codex CLI models (for example gpt-5.3-codex-spark) are
        # marked false here but are still accepted by the Codex route.
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug))
        context = parse_context_windows(item)
        if context is not None:
            contexts[slug] = context
        caps = parse_codex_reasoning_capabilities(item)
        if caps is not None:
            capabilities[slug] = caps

    if entries:
        _context_catalogs[_catalog_key(access_token, base_url)] = contexts
        _reasoning_catalogs[_catalog_key(access_token, base_url)] = (time.monotonic(), capabilities)

    sortable.sort(key=lambda x: (x[0], x[1]))
    return _finalize_codex_models([slug for _, slug in sortable])


def _read_default_model(codex_home: Path) -> Optional[str]:
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib
    except Exception:
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _read_cache_models(codex_home: Path) -> List[str]:
    cache_path = codex_home / "models_cache.json"
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = raw.get("models") if isinstance(raw, dict) else None
    sortable = []
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            # Do not filter on ``supported_in_api`` here.  It describes the
            # public OpenAI API, while Mercury openai-codex talks to the same
            # OAuth-backed Codex backend as Codex CLI.
            visibility = item.get("visibility")
            if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
                continue
            priority = item.get("priority")
            rank = int(priority) if isinstance(priority, (int, float)) else 10_000
            sortable.append((rank, slug))

    sortable.sort(key=lambda item: (item[0], item[1]))
    deduped: List[str] = []
    for _, slug in sortable:
        if slug not in deduped:
            deduped.append(slug)
    return deduped


def get_codex_model_ids(access_token: Optional[str] = None, base_url: Optional[str] = None) -> List[str]:
    """Return available Codex model IDs, trying API first, then local sources.
    
    Resolution order: API (live, if token provided) > config.toml default >
    local cache > hardcoded defaults.
    """
    codex_home_str = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    codex_home = Path(codex_home_str).expanduser()
    ordered: List[str] = []

    # Try live API if we have a token
    if access_token:
        api_models = _fetch_models_from_api(access_token, base_url=base_url)
        if api_models:
            return _finalize_codex_models(api_models)

    # Fall back to local sources
    default_model = _read_default_model(codex_home)
    if default_model:
        ordered.append(default_model)

    for model_id in _read_cache_models(codex_home):
        if model_id not in ordered:
            ordered.append(model_id)

    for model_id in DEFAULT_CODEX_MODELS:
        if model_id not in ordered:
            ordered.append(model_id)

    return _finalize_codex_models(ordered)
