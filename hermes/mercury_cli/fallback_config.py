"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

from typing import Any


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def _split_model_selector(value: Any) -> tuple[str, str]:
    """``provider/model`` -> ``(provider, model)``; bare names -> ``("", name)``.

    Mirrors ``omp_sync.derive_slot_provider``: the provider is the text before
    the FIRST slash and the model is everything after it, because model ids
    themselves carry slashes (``nous/xiaomi/mimo-v2.6-pro`` is provider
    ``nous`` + model ``xiaomi/mimo-v2.6-pro``).
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    if "/" not in text:
        return "", text
    provider, model = text.split("/", 1)
    return provider.strip(), model.strip()


def _entries_from_model_slots(models: dict[str, Any]) -> list[dict[str, Any]]:
    """Mercury's flat ``models.fallback`` / ``models.fallback_chain`` slots.

    Mercury names a fallback as ONE selector string (``nous/xiaomi/mimo-v2.6-pro``)
    rather than the stock ``{provider, model}`` dict, so the dict-only chain
    builder saw an EMPTY chain and never failed over — a usage limit then
    killed the agent even though the user had configured a fallback. Bridge
    validation requires ``models.fallback_chain`` to begin with
    ``models.fallback``, so the declared order is ``fallback`` followed by the
    rest of the chain.

    A bare model name (no provider prefix) borrows the provider from
    ``models.default`` so it can still become a usable entry.
    """
    if not isinstance(models, dict):
        return []
    default_provider, _ = _split_model_selector(models.get("default"))

    ordered: list[Any] = []
    head = models.get("fallback")
    if head:
        ordered.append(head)
    chain = models.get("fallback_chain")
    if isinstance(chain, list):
        ordered.extend(item for item in chain if item)

    entries: list[dict[str, Any]] = []
    for item in ordered:
        provider, model = _split_model_selector(item)
        if not provider:
            provider = default_provider
        if not provider or not model:
            continue
        entries.append({"provider": provider, "model": model})
    return entries


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    Mercury's flat ``models.fallback`` / ``models.fallback_chain`` slots lead
    (they are the user's declared first-order fallback), then stock
    ``fallback_providers`` in its own order, then legacy ``fallback_model`` —
    each appended only when it does not target the same provider/model/base_url
    route as an earlier entry. The returned list always contains fresh dict
    copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _append(entry: dict[str, Any]) -> None:
        identity = _entry_identity(entry)
        if identity in seen:
            return
        seen.add(identity)
        chain.append(entry)

    models = config.get("models")
    for entry in _entries_from_model_slots(models if isinstance(models, dict) else {}):
        _append(entry)

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            _append(entry)

    return chain
