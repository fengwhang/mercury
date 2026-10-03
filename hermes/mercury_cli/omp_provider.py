"""Nous inference adapter data for OMP's private, profile-local auth channel."""
from __future__ import annotations

import hashlib
import json
import math
import ssl
import time
import urllib.request

from mercury_cli import auth


def _catalog(base_url: str, api_key: str, state: dict) -> list[dict]:
    from mercury_constants import get_hermes_home
    from utils import atomic_write_text

    url = base_url.rstrip("/")
    url = url + "/models" if url.endswith("/v1") else url + "/v1/models"
    path = get_hermes_home() / "cache" / ("omp_nous_" + hashlib.sha256((url + "\0" + str(state.get("account_id") or state.get("user_id") or state.get("obtained_at") or "")).encode()).hexdigest()[:20] + ".json")
    cached = {}
    try:
        cached = json.loads(path.read_text())
        if time.time() - cached.get("time", 0) < 900:
            return cached["models"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        from mercury_cli.urllib_security import open_credentialed_url
        verify = auth._resolve_verify(auth_state=state)
        context = ssl._create_unverified_context() if verify is False else verify if isinstance(verify, ssl.SSLContext) else None
        with open_credentialed_url(request, timeout=8, ssl_context=context) as response:
            cards = json.load(response).get("data", [])
        if not isinstance(cards, list):
            raise ValueError("Invalid model catalog")
        cards = [card for card in cards if isinstance(card, dict) and isinstance(card.get("id"), str)]
        # Never persist inference keys or authorization headers in the catalog.
        atomic_write_text(path, json.dumps({"time": time.time(), "models": cards}))
        return cards
    except Exception:
        return cached.get("models", [])


def runtime_provider(*, include_models: bool = True, force_refresh: bool = False) -> dict | None:
    # The auth store resolves the main login owner for inheriting profiles.
    state = (auth._load_auth_store().get("providers") or {}).get("nous")
    if not state:
        return None
    try:
        credentials = auth.resolve_nous_runtime_credentials(force_refresh=force_refresh)
    except auth.AuthError as exc:
        if exc.relogin_required:
            return None
        raise
    key, base = credentials.get("api_key"), credentials.get("base_url")
    if not key or not base:
        return None
    result = {"provider": "nous", "apiKey": key, "baseUrl": base, "models": []}
    if not include_models:
        return result
    from mercury_cli.context_settings import parse_context_windows
    from mercury_cli.models import parse_openrouter_reasoning_capabilities
    from mercury_cli.providers import nous_api_mode

    def price(value):
        try:
            amount = float(value or 0)
            return amount * 1_000_000 if math.isfinite(amount) and amount >= 0 else 0
        except (TypeError, ValueError):
            return 0

    for card in _catalog(base, key, state):
        model_id = card["id"]
        anthropic = nous_api_mode(model_id) == "anthropic_messages"
        limits = card.get("top_provider") if isinstance(card.get("top_provider"), dict) else {}
        window = parse_context_windows(card)
        pricing = card.get("pricing") if isinstance(card.get("pricing"), dict) else {}
        architecture = card.get("architecture") if isinstance(card.get("architecture"), dict) else {}
        caps = parse_openrouter_reasoning_capabilities(card) or {}
        spec = {
            "id": model_id, "name": card.get("name") or model_id,
            "api": "anthropic-messages" if anthropic else "openai-completions",
            "baseUrl": base.rstrip("/").removesuffix("/v1") if anthropic else base,
            "reasoning": caps.get("supports_reasoning", False),
            "input": [item for item in (architecture.get("input_modalities") if isinstance(architecture.get("input_modalities"), list) else ["text"]) if item in ("text", "image")],
            # Missing catalogue limits use OMP's existing conservative custom-
            # model defaults; limits are never borrowed from another provider.
            "contextWindow": window["default"] if window else 128000,
            "maxTokens": next((value for value in (limits.get("max_completion_tokens"), card.get("max_output_tokens")) if type(value) is int and value > 0), 16384),
            "cost": {target: price(pricing.get(source)) for target, source in (
                ("input", "prompt"), ("output", "completion"), ("cacheRead", "input_cache_read"), ("cacheWrite", "input_cache_write"))},
        }
        if window:
            spec["maxContextWindow"] = window["maximum"]
        efforts = caps.get("supported_efforts")
        if efforts is None and caps.get("supports_effort_selection"):
            from agent.reasoning_effort import OPENAI_COMPAT_WIRE_EFFORTS
            efforts = list(OPENAI_COMPAT_WIRE_EFFORTS)
        efforts = [effort for effort in (efforts or []) if effort in ("minimal", "low", "medium", "high", "xhigh", "max")]
        if efforts:
            spec["thinking"] = {"mode": "effort", "efforts": efforts, "requiresEffort": caps.get("mandatory", False)}
        if not anthropic:
            spec["compat"] = {"thinkingFormat": "openrouter", "supportsReasoningEffort": False,
                              "supportsReasoningParams": bool(caps.get("supports_reasoning"))}
        result["models"].append(spec)
    return result
