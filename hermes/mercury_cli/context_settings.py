"""Provider context metadata shared by setup and the model catalogues."""

from __future__ import annotations


def parse_context_windows(item: dict) -> dict | None:
    """Keep advertised defaults and maxima distinct; a lone window is both."""
    def positive(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    limits = item.get("limit") or {}
    if not isinstance(limits, dict):
        limits = {}
    default = positive(item.get("context_window")) or positive(item.get("context_length")) or positive(limits.get("context")) or positive(item.get("max_input_tokens")) or positive(item.get("inputTokenLimit"))
    maximum = positive(item.get("max_context_window")) or positive(item.get("max_context_length"))
    if default is None and maximum is None:
        return None
    default = default or maximum
    maximum = max(default, maximum or default)
    return {"default": default, "maximum": maximum}


def model_context_windows(provider: str, model: str) -> dict | None:
    """Read the serving provider's catalogue, never another provider's limit."""
    from mercury_cli.models import normalize_provider
    provider = normalize_provider(provider)
    if provider == "openai-codex":
        from mercury_cli.auth import resolve_codex_runtime_credentials
        from mercury_cli.codex_models import codex_model_context_windows
        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        return codex_model_context_windows(
            model, access_token=credentials.get("api_key"),
            base_url=credentials.get("base_url"), allow_fetch=True,
        )
    if provider == "openrouter":
        from agent.model_metadata import fetch_model_metadata
        return parse_context_windows(fetch_model_metadata().get(model, {}))
    from mercury_cli.runtime_provider import resolve_runtime_provider
    from agent.model_metadata import fetch_endpoint_model_metadata
    runtime = resolve_runtime_provider(requested=provider, target_model=model)
    if provider == "anthropic":
        from agent.model_metadata import _query_anthropic_context_length
        window = _query_anthropic_context_length(model, runtime.get("base_url") or "", runtime.get("api_key") or "")
        return parse_context_windows({"context_window": window})
    if provider == "copilot":
        from mercury_cli.models import fetch_github_model_catalog
        catalog = fetch_github_model_catalog(api_key=runtime.get("api_key")) or []
        entry = next((item for item in catalog if item.get("id") == model), {})
        capabilities = entry.get("capabilities") or {}
        limits = capabilities.get("limits") or {}
        return parse_context_windows({"context_window": limits.get("max_prompt_tokens")})
    catalog = fetch_endpoint_model_metadata(runtime.get("base_url") or "", api_key=runtime.get("api_key") or "")
    return parse_context_windows(catalog.get(model, {}))
