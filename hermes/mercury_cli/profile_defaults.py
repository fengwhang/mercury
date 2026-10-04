"""Installation model authority and provider logins for Mercury named profiles.

Only inference settings are inherited. Prompts, channels, permissions and
runtime state remain local. Stable and nightly installations never overlap.
"""
from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path


def main_profile_root(home: Path) -> Path | None:
    root = os.environ.get("MERCURY_HOME", "").strip()
    if not root:
        return None
    root = Path(root).resolve()
    home = Path(home).resolve()
    if home.parent == root / "hermes" / "profiles":
        return root
    return None


def _document(path: Path) -> dict:
    from utils import fast_safe_load
    try:
        with path.open(encoding="utf-8") as source:
            value = fast_safe_load(source) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a configuration mapping: {path}")
    return value


def inherits(home: Path, kind: str, document: dict | None = None) -> bool:
    if main_profile_root(home) is None:
        return False
    if kind == "models":
        return True
    document = _document(Path(home) / "config.yaml") if document is None else document
    return (document.get("profile") or {}).get(f"inherit_{kind}", True) is not False


def credential_home(home: Path) -> Path:
    """Resolve the login owner, including the owner of refreshed OAuth grants."""
    root = main_profile_root(home)
    return root / "hermes" if root and inherits(home, "credentials") else Path(home)


def model_parent_signature(config_path: Path) -> tuple:
    root = main_profile_root(Path(config_path).parent)
    if root is None:
        return ()
    path = root / "config.yaml"
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return str(path), 0, 0


class ProfileModelError(ValueError):
    """A named profile cannot resolve its authoritative inference settings."""


MODEL_SLOTS = ("default", "fallback", "delegate_model", "delegate_fallback")


def validate_profile_models(value: object, location: str) -> dict:
    """Validate a complete override without borrowing any main-profile slots."""
    def fail(message: str) -> None:
        raise ProfileModelError(f"{location}: {message}; fix this profile's models or remove its profile_models entry to inherit")

    if not isinstance(value, dict):
        fail("expected a model mapping")
    models = deepcopy(value)
    for key in MODEL_SLOTS:
        selector = models.get(key, "")
        if not isinstance(selector, str):
            fail(f"{key} must be a provider/model string")
        if key in ("default", "delegate_model") and not selector:
            fail(f"{key} is required")
        if selector and ("/" not in selector or not all(selector.partition("/")[::2]) or any(c.isspace() for c in selector) or selector.partition("/")[0] == "auto"):
            fail(f"{key} must include provider/model identity")
        models[key] = selector
    for primary, fallback, chain_key in (("default", "fallback", "fallback_chain"),
                                          ("delegate_model", "delegate_fallback", "delegate_fallback_chain")):
        chain = models.setdefault(chain_key, [])
        if not isinstance(chain, list) or any(not isinstance(item, str) or not item or "/" not in item or not all(item.partition("/")[::2]) or any(c.isspace() for c in item) for item in chain):
            fail(f"{chain_key} must contain provider/model identities")
        if models[fallback] and models[fallback] == models[primary]:
            fail(f"{fallback} must differ from {primary}")
        if chain and (len(set(chain)) != len(chain) or models[primary] in chain or chain[0] != models[fallback]):
            fail(f"{chain_key} must start with {fallback}, without duplicates or {primary}")
    efforts = models.setdefault("reasoning_overrides", {})
    if not isinstance(efforts, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in efforts.items()):
        fail("reasoning_overrides must map model identities to effort strings")
    windows = models.setdefault("context_windows", {})
    if not isinstance(windows, dict) or any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v <= 0 for k, v in windows.items()):
        fail("context_windows must map model identities to positive token limits")
    return models


def profile_models_entry(main: dict, name: str, config_path: Path) -> dict | None:
    if "profile_models" not in main:
        return None
    entries = main["profile_models"]
    if not isinstance(entries, dict):
        raise ProfileModelError(f"{config_path}: profile_models must be a mapping")
    if name not in entries:
        return None
    return validate_profile_models(entries[name], f"{config_path}: profile_models.{name}")


def resolve_model_defaults(document: dict, config_path: Path) -> dict:
    """Use installation models or a complete central profile override, never a blend."""
    home = Path(config_path).parent
    root = main_profile_root(home)
    if root is None:
        return document
    from mercury_cli.model_settings import shared_models, canonical_model_document

    try:
        main = _document(root / "config.yaml")
        selected = profile_models_entry(main, home.name, root / "config.yaml")
        models = selected if selected is not None else shared_models(main)
        if not isinstance(document, dict):
            raise ValueError("expected a configuration mapping")
        out = deepcopy(document)
        # Legacy local/native mirrors are not model authorities. Explicit
        # empty fallbacks/maps also prevent shared_models from importing them.
        out["models"] = {**{key: "" for key in MODEL_SLOTS},
                         "fallback_chain": [], "delegate_fallback_chain": [],
                         "reasoning_overrides": {}, "context_windows": {}, **models}
        native = out.setdefault("hermes", {})
        for key in ("model", "fallback_providers", "fallback_model"):
            native.pop(key, None)
        main_native = canonical_model_document(main).get("hermes") or {}
        for key in ("model_options", "providers"):
            if isinstance(main_native.get(key), dict):
                native[key] = {**deepcopy(main_native[key]), **(native.get(key) or {})}
        if "custom_providers" not in native and "custom_providers" in main_native:
            native["custom_providers"] = deepcopy(main_native["custom_providers"])
        return out
    except ProfileModelError:
        raise
    except Exception as exc:
        raise ProfileModelError(f"Cannot resolve profile '{home.name}' models from {root / 'config.yaml'}: {exc}") from exc


def save_profile_models(home: Path, models: dict | None) -> None:
    """Atomically edit only this profile's entry in the installation config."""
    root = main_profile_root(home)
    if root is None:
        raise ProfileModelError("Profile models require a named Mercury installation profile")
    from mercury_cli.config import _CONFIG_LOCK, require_readable_config_before_write
    from utils import atomic_yaml_write
    selected = validate_profile_models(models, f"profile_models.{home.name}") if models is not None else None
    with _CONFIG_LOCK:
        path = root / "config.yaml"
        whole = require_readable_config_before_write(path)
        entries = whole.setdefault("profile_models", {})
        if not isinstance(entries, dict):
            raise ProfileModelError(f"{path}: profile_models must be a mapping")
        if selected is None:
            entries.pop(home.name, None)
            if not entries:
                whole.pop("profile_models")
        else:
            entries[home.name] = {key: value for key, value in selected.items()
                                  if key in MODEL_SLOTS or value}
        atomic_yaml_write(path, whole)


def persist_profile_model_edits(document: dict, original: dict, config_path: Path) -> dict:
    """Route deliberate native edits centrally; unrelated saves cannot pin inheritance."""
    home = Path(config_path).parent
    if main_profile_root(home) is None:
        return document
    from mercury_cli.model_settings import shared_models
    before, after = shared_models(original), shared_models(document)
    effective = resolve_model_defaults(original, config_path)["models"]
    changed = False
    for key, value in after.items():
        if value == before.get(key, {} if key in ("reasoning_overrides", "context_windows") else None):
            continue
        if key in ("reasoning_overrides", "context_windows"):
            merged = dict(effective.get(key) or {})
            previous = before.get(key) or {}
            for selector, item in value.items():
                if item != previous.get(selector):
                    merged[selector] = item
            effective[key] = merged
        else:
            effective[key] = value
        changed = True
    if changed:
        save_profile_models(home, effective)
    document.pop("models", None)
    return document


def keep_local_transport_settings(document: dict, original: dict, previous: dict, config_path: Path) -> dict:
    """Persist deliberate transport edits, not unchanged inherited endpoints."""
    if main_profile_root(Path(config_path).parent) is None:
        return document
    native = document.get("hermes") or {}
    local = original.get("hermes") or {}

    def edits(current: dict, before: dict, explicit: dict) -> dict:
        result = {}
        for key, value in current.items():
            if isinstance(value, dict) and isinstance(before.get(key), dict):
                changed = edits(value, before[key], explicit.get(key) or {})
                if changed or key in explicit:
                    result[key] = changed
            elif key in explicit or value != before.get(key):
                result[key] = value
        return result

    for key in ("model_options", "providers"):
        if isinstance(native.get(key), dict):
            native[key] = edits(native[key], previous.get(key) or {}, local.get(key) or {})
            if not native[key] and key not in local:
                native.pop(key)
    if "custom_providers" not in local and native.get("custom_providers") == previous.get("custom_providers"):
        native.pop("custom_providers", None)
    return document


def configure_profile_models(name: str, *, inherit: bool = False) -> None:
    """Run the setup picker in memory, then commit one complete profile entry."""
    from mercury_cli.profiles import normalize_profile_name, validate_profile_name, get_profile_dir
    from mercury_constants import set_hermes_home_override, reset_hermes_home_override
    from mercury_cli.model_settings import hermes_model_view, shared_models
    from mercury_cli.setup import _prompt_mercury_slots, _SetupCancelled, _SetupGoBack
    from mercury_cli.config import is_managed

    canon = normalize_profile_name(name)
    validate_profile_name(canon)
    if canon == "default":
        raise ValueError("Use 'mercury setup model' to configure the installation's main models")
    home = get_profile_dir(canon)
    if not home.is_dir():
        raise FileNotFoundError(f"Profile '{canon}' does not exist")
    if is_managed():
        raise ValueError("This installation's configuration is managed and cannot be edited")
    root = main_profile_root(home)
    if root is None:
        raise ValueError("Profile models require a Mercury installation")
    if inherit:
        save_profile_models(home, None)
        print(f"Profile '{canon}' now inherits the installation's main models.")
        return
    local = _document(home / "config.yaml")
    try:
        effective = resolve_model_defaults(local, home / "config.yaml")
    except ProfileModelError as exc:
        print(f"Current profile models are invalid: {exc}")
        # Repair UI may start from known main choices; runtime never does.
        main = _document(root / "config.yaml")
        effective = {**local, "models": shared_models(main)}
    draft = deepcopy(effective["models"])
    config = hermes_model_view(effective)
    token = set_hermes_home_override(home)
    try:
        try:
            selected = _prompt_mercury_slots(config, draft=draft)
        except (KeyboardInterrupt, _SetupCancelled, _SetupGoBack):
            print("Profile model selection cancelled; settings unchanged.")
            return
        if selected is None:
            return
        identities = {selected.get(key) for key in MODEL_SLOTS}
        identities.update(selected.get("fallback_chain") or [])
        identities.update(selected.get("delegate_fallback_chain") or [])
        for key in ("reasoning_overrides", "context_windows"):
            selected[key] = {model: value for model, value in (selected.get(key) or {}).items() if model in identities}
        save_profile_models(home, selected)
    finally:
        reset_hermes_home_override(token)
    print(f"Profile '{canon}' models saved to {root / 'config.yaml'} (profile_models.{canon}).")

def transfer_profile_models(source: Path, destination: Path | None = None, *, copy: bool = False) -> None:
    """Keep central model ownership aligned with profile clone/rename/delete."""
    root = main_profile_root(source)
    if root is None:
        return
    from mercury_cli.config import _CONFIG_LOCK, require_readable_config_before_write
    from utils import atomic_yaml_write
    with _CONFIG_LOCK:
        path = root / "config.yaml"
        whole = require_readable_config_before_write(path)
        entries = whole.get("profile_models")
        if not isinstance(entries, dict) or source.name not in entries:
            return
        if destination is not None:
            if main_profile_root(destination) != root:
                raise ProfileModelError("Cannot transfer model overrides between Mercury installations")
            entries[destination.name] = deepcopy(entries[source.name])
        if not copy:
            entries.pop(source.name)
        if not entries:
            whole.pop("profile_models")
        atomic_yaml_write(path, whole)


def stage_profile_models(source: Path, staged: Path) -> None:
    """Export only an explicit override; inherited models remain unpinned."""
    root = main_profile_root(source)
    if root is None:
        return
    selected = profile_models_entry(_document(root / "config.yaml"), source.name, root / "config.yaml")
    from utils import atomic_yaml_write
    path = staged / "config.yaml"
    whole = _document(path)
    if selected is None:
        whole.pop("models", None)
    else:
        whole["models"] = selected
    atomic_yaml_write(path, whole)


def import_profile_models(home: Path) -> None:
    """Promote an exported explicit selection to this installation's authority."""
    if main_profile_root(home) is None:
        return
    from utils import atomic_yaml_write
    path = home / "config.yaml"
    whole = _document(path)
    if whole.get("models"):
        save_profile_models(home, whole["models"])
    if "models" in whole:
        whole.pop("models")
        atomic_yaml_write(path, whole)
