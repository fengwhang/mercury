"""Main-profile model and provider-login defaults for Mercury named profiles.

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


def resolve_model_defaults(document: dict, config_path: Path) -> dict:
    """Build a transient view; never write inherited choices into local config."""
    home = Path(config_path).parent
    root = main_profile_root(home)
    if root is None or not inherits(home, "models", document):
        return document
    from mercury_cli.model_settings import shared_models

    main = _document(root / "config.yaml")
    if not main:
        return document
    out = deepcopy(document)
    base, local = shared_models(main), shared_models(document)
    for key in ("reasoning_overrides", "context_windows"):
        if key in local:
            local[key] = {**(base.get(key) or {}), **(local[key] or {})}
    out["models"] = {**base, **local}
    native = out.setdefault("hermes", {})
    from mercury_cli.model_settings import canonical_model_document
    main_native = canonical_model_document(main).get("hermes") or {}
    for key in ("model_options", "providers"):
        if isinstance(main_native.get(key), dict):
            native[key] = {**deepcopy(main_native[key]), **(native.get(key) or {})}
    if "custom_providers" not in native and "custom_providers" in main_native:
        native["custom_providers"] = deepcopy(main_native["custom_providers"])
    return out


def keep_local_transport_settings(document: dict, original: dict, previous: dict, config_path: Path) -> dict:
    """Persist deliberate transport edits, not unchanged inherited endpoints."""
    if not inherits(Path(config_path).parent, "models", original):
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
