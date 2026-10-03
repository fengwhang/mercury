"""Mercury's model authority and transient Hermes view.

The shared models block owns selections, effort and context budgets. Native
transport options remain profile-local and keyed by full model identity.
Legacy mirrors are imported only when a shared value has not been set.
"""
from __future__ import annotations

from copy import deepcopy

LEGACY_THINKING_SLOTS = {
    "orchestrator_thinking_level": "default",
    "delegate_thinking_level": "delegate_model",
    "delegate_fallback_thinking_level": "delegate_fallback",
}


def qualify_model(model: str, provider: str) -> str:
    model, provider = str(model or "").strip(), str(provider or "").strip().rstrip("/")
    return f"{provider}/{model}" if model and provider and not model.startswith(provider + "/") else model


def per_model_reasoning(models: dict, native_overrides: dict | None = None) -> dict[str, str]:
    levels = {}
    for key, slot in LEGACY_THINKING_SLOTS.items():
        selector, level = models.get(slot), models.get(key)
        if selector and isinstance(level, str) and level:
            levels.setdefault(selector, "off" if level == "none" else level)
    if isinstance(native_overrides, dict):
        levels.update({key: "off" if value == "none" else value
                       for key, value in native_overrides.items() if isinstance(value, str)})
    levels.update(models.get("reasoning_overrides") or {})
    return levels


def shared_models(whole: dict) -> dict:
    """Resolve old documents without mutating them or overriding shared choices."""
    models = deepcopy(whole.get("models") or {})
    migrated_efforts = {}

    def native_selector(value):
        selector, sep, level = value.rpartition(":")
        if sep and level in ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"):
            migrated_efforts[selector] = level
            return selector
        return value
    native = whole.get("hermes") or {}
    model = native.get("model") or {}
    if isinstance(model, str):
        model = {"default": model}
    if "default" not in models and model.get("default"):
        models["default"] = qualify_model(model["default"], model.get("provider", ""))
    if "delegate_model" not in models:
        task = (whole.get("omp") or {}).get("delegateModel") or ((whole.get("omp") or {}).get("modelRoles") or {}).get("task")
        if task:
            models["delegate_model"] = native_selector(task)
    omp = whole.get("omp") or {}
    delegate = models.get("delegate_model")
    if delegate and "delegate_fallback" not in models and "delegate_fallback_chain" not in models:
        chain = [native_selector(item) for item in (((omp.get("retry") or {}).get("fallbackChains") or {}).get(delegate) or ([omp["delegateFallback"]] if omp.get("delegateFallback") else []))]
        if chain:
            models["delegate_fallback"], models["delegate_fallback_chain"] = chain[0], chain if len(chain) > 1 else []
    if "fallback" not in models and "fallback_chain" not in models:
        from mercury_cli.fallback_config import get_fallback_chain
        chain = [qualify_model(item["model"], item["provider"]) for item in get_fallback_chain(native)]
        if chain:
            models["fallback"], models["fallback_chain"] = chain[0], chain if len(chain) > 1 else []
    agent = native.get("agent") or {}
    efforts = deepcopy(models.get("reasoning_overrides") or {}) if "reasoning_overrides" in models else per_model_reasoning(models, agent.get("reasoning_overrides"))
    if "reasoning_overrides" not in models:
        for selector, effort in (omp.get("modelReasoningOverrides") or {}).items():
            efforts.setdefault(selector, effort)
        for selector, effort in migrated_efforts.items():
            efforts.setdefault(selector, effort)
    if "reasoning_overrides" not in models and delegate and omp.get("defaultThinkingLevel"):
        efforts.setdefault(delegate, omp["defaultThinkingLevel"])
    if "reasoning_overrides" not in models and models.get("default") and agent.get("reasoning_effort"):
        level = str(agent["reasoning_effort"])
        efforts.setdefault(models["default"], "off" if level == "none" else level)
    if efforts:
        models["reasoning_overrides"] = efforts
    windows = deepcopy(models.get("context_windows") or {})
    if "context_windows" not in models:
        windows.update(omp.get("modelContextWindows") or {})
        for provider, overrides in (native.get("model_overrides") or {}).items():
            for model_id, metadata in overrides.items():
                if isinstance(metadata, dict) and metadata.get("context_window"):
                    windows.setdefault(qualify_model(model_id, provider), metadata["context_window"])
    if "context_windows" not in models and model.get("context_length") and models.get("default"):
        windows.setdefault(models["default"], model["context_length"])
    if windows or "context_windows" in models:
        models["context_windows"] = windows
    for key in LEGACY_THINKING_SLOTS:
        models.pop(key, None)
    return models


def hermes_model_view(whole: dict) -> dict:
    """Project authoritative selections into the unchanged Hermes runtime API."""
    native = deepcopy(whole.get("hermes") or {})
    models = shared_models(whole)
    selector = str(models.get("default") or "")
    if selector:
        provider, sep, model_id = selector.partition("/")
        old = native.get("model") or {}
        old = old if isinstance(old, dict) else {"default": old}
        options = {key: value for key, value in old.items() if key not in ("default", "provider")}
        if old.get("default") and qualify_model(old["default"], old.get("provider", "")) != selector:
            options = {}
        options.update((native.get("model_options") or {}).get(selector) or {})
        native["model"] = {**options, "provider": provider if sep else old.get("provider", ""),
                           "default": model_id if sep else selector}
    if "fallback" in models or "fallback_chain" in models:
        chain = models.get("fallback_chain") or ([models["fallback"]] if models.get("fallback") else [])
        native["fallback_providers"] = []
        for selector in chain:
            provider, _, model_id = selector.partition("/")
            options = (native.get("model_options") or {}).get(selector) or {}
            native["fallback_providers"].append({**options, "provider": provider, "model": model_id})
        native.pop("fallback_model", None)
    efforts = models.get("reasoning_overrides") or {}
    if "reasoning_overrides" in models:
        native.setdefault("agent", {}).pop("reasoning_effort", None)
        native["agent"]["reasoning_overrides"] = {
            key: "none" if value == "off" else value for key, value in efforts.items()
        }
    if "context_windows" in models:
        for overrides in (native.get("model_overrides") or {}).values():
            for metadata in overrides.values():
                if isinstance(metadata, dict):
                    metadata.pop("context_window", None)
        if isinstance(native.get("model"), dict):
            native["model"].pop("context_length", None)
    for selector, window in (models.get("context_windows") or {}).items():
        provider, _, model_id = selector.partition("/")
        native.setdefault("model_overrides", {}).setdefault(provider, {}).setdefault(model_id, {})["context_window"] = window
    return native


def save_hermes_model_view(whole: dict, native: dict, previous: dict | None = None) -> dict:
    """Save native behavior, routing deliberate model edits to the shared block.

    Comparing with the last loaded view prevents an ordinary native settings
    save from undoing a newer shared-model edit made by another UI.
    """
    whole = deepcopy(whole)
    models = shared_models(whole)
    previous = previous if previous is not None else hermes_model_view(whole)
    native = deepcopy(native)
    native.pop("models", None)
    native.pop("omp", None)
    incoming = native.get("model")
    incoming = incoming if isinstance(incoming, dict) else {"default": incoming} if incoming else {}
    before = previous.get("model") or {}
    before = before if isinstance(before, dict) else {"default": before}
    if incoming:
        selection = {key: incoming.get(key, before.get(key, "")) for key in ("default", "provider")}
        if any(key in incoming and incoming[key] != before.get(key) for key in selection):
            models["default"] = qualify_model(selection["default"], selection["provider"])
        options = {key: value for key, value in incoming.items() if key not in ("default", "provider", "context_length")}
        if options:
            option_selector = qualify_model(selection["default"], selection["provider"])
            if option_selector:
                native.setdefault("model_options", {}).setdefault(option_selector, {}).update(options)
        if incoming.get("context_length") != before.get("context_length") and incoming.get("context_length"):
            models.setdefault("context_windows", {})[qualify_model(selection["default"], selection["provider"])] = incoming["context_length"]
    native.pop("model", None)
    from mercury_cli.fallback_config import get_fallback_chain
    if any(key in native for key in ("fallback_providers", "fallback_model")):
        chain = get_fallback_chain(native)
        if chain != get_fallback_chain(previous):
            selectors = [qualify_model(item["model"], item["provider"]) for item in chain]
            models["fallback"] = selectors[0] if selectors else ""
            models["fallback_chain"] = selectors if len(selectors) > 1 else []
        for entry in chain:
            options = {key: value for key, value in entry.items() if key not in ("provider", "model")}
            if options:
                selector = qualify_model(entry["model"], entry["provider"])
                native.setdefault("model_options", {}).setdefault(selector, {}).update(options)
    native.pop("fallback_providers", None)
    native.pop("fallback_model", None)
    agent = native.get("agent") or {}
    old_agent = previous.get("agent") or {}
    efforts = models.setdefault("reasoning_overrides", {})
    for selector, level in (agent.get("reasoning_overrides") or {}).items():
        if level != (old_agent.get("reasoning_overrides") or {}).get(selector):
            efforts[selector] = "off" if level == "none" else level
    default_selector = models.get("default") or qualify_model(incoming.get("default", before.get("default", "")), incoming.get("provider", before.get("provider", "")))
    if "reasoning_effort" in agent and agent["reasoning_effort"] != old_agent.get("reasoning_effort") and default_selector:
        level = agent["reasoning_effort"]
        efforts[default_selector] = "off" if level == "none" else level
    agent.pop("reasoning_overrides", None)
    agent.pop("reasoning_effort", None)
    for provider, overrides in list((native.get("model_overrides") or {}).items()):
        for model_id, metadata in list(overrides.items()):
            if not isinstance(metadata, dict):
                continue
            window = metadata.pop("context_window", None)
            old = ((previous.get("model_overrides") or {}).get(provider) or {}).get(model_id) or {}
            if window and window != old.get("context_window"):
                models.setdefault("context_windows", {})[qualify_model(model_id, provider)] = window
            if not metadata:
                overrides.pop(model_id)
        if not overrides:
            native["model_overrides"].pop(provider)
    if not native.get("model_overrides"):
        native.pop("model_overrides", None)
    whole["models"], whole["hermes"] = models, native
    return whole


def canonical_model_document(whole: dict) -> dict:
    """Import legacy choices once and remove persisted runtime projections."""
    view = hermes_model_view(whole)
    result = save_hermes_model_view(whole, view, previous=view)
    omp = result.get("omp") or {}
    for key in ("modelRoles", "delegateModel", "delegateFallback", "defaultThinkingLevel", "modelReasoningOverrides", "modelContextWindows"):
        omp.pop(key, None)
    if isinstance(omp.get("retry"), dict):
        omp["retry"].pop("fallbackChains", None)
    return result


def save_omp_model_edit(whole: dict, key: str, value) -> dict:
    """Route supported native config aliases into the shared authority."""
    models = whole.setdefault("models", shared_models(whole))

    def selector(entry):
        bare, sep, level = str(entry or "").rpartition(":")
        if sep and level in ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"):
            models.setdefault("reasoning_overrides", {})[bare] = level
            return bare
        return str(entry or "")

    if key == "omp.delegateModel":
        models["delegate_model"] = selector(value)
    elif key == "omp.delegateFallback":
        models["delegate_fallback"] = selector(value)
        models["delegate_fallback_chain"] = []
    elif key == "omp.defaultThinkingLevel" and models.get("delegate_model"):
        models.setdefault("reasoning_overrides", {})[models["delegate_model"]] = value
    elif key == "omp.modelContextWindows" or key.startswith("omp.modelContextWindows."):
        models["context_windows"] = deepcopy((whole.get("omp") or {}).get("modelContextWindows") or {})
    elif key == "omp.modelReasoningOverrides" or key.startswith("omp.modelReasoningOverrides."):
        models["reasoning_overrides"] = deepcopy((whole.get("omp") or {}).get("modelReasoningOverrides") or {})
    elif key == "omp.retry.fallbackChains" or key.startswith("omp.retry.fallbackChains."):
        chain = (((whole.get("omp") or {}).get("retry") or {}).get("fallbackChains") or {}).get(models.get("delegate_model")) or []
        chain = [selector(entry) for entry in chain]
        models["delegate_fallback"] = chain[0] if chain else ""
        models["delegate_fallback_chain"] = chain if len(chain) > 1 else []
    return canonical_model_document(whole)
