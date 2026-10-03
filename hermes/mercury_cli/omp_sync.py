"""MERCURY-OMP PATCH: post-setup omp sync.

`mercury setup` (the full hermes wizard — provider OAuth, Nous Portal,
API keys, model pickers) writes hermes-native config keys. Mercury's
unified file feeds BOTH engines from the shared four-slot ``models:``
block, so after the wizard runs, this module:

1. extracts the hermes-side choices (model.default + provider,
   fallback chain) back into the shared ``models:`` slots,
2. re-renders the ``omp:`` subtree via the config bridge so the omp
   engine inherits shared models/deny rules while preserving its native approval mode,
3. verifies both engines resolve a model afterwards.

Invoked by the installer right after `mercury setup`, and by
`mercury setup` itself (setup.py tail hook) so ANY later re-run keeps
both engines in sync.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _mercury_home() -> Path:
    return Path(os.environ.get("MERCURY_HOME") or Path.home() / ".mercury")


def _unified_path() -> Path:
    from mercury_constants import get_config_path
    return get_config_path()


def _repo_root() -> Path | None:
    env = os.environ.get("MERCURY_REPO")
    if env and Path(env).is_dir():
        return Path(env)
    # self-locate: .../hermes/mercury_cli/omp_sync.py
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "bridge" / "bridge.py").exists() and (parent / "bin" / "mercury").exists():
            return parent
    return None


def qualify_omp_model(model_id: str, provider: str, *, provider_relative: bool = False) -> str:
    """Qualify a catalog ID without guessing its serving provider from slashes.

    Catalog callers pass provider_relative=True; vendor/model IDs are opaque
    to that provider. Legacy shared selectors already containing a slash keep
    their own provider, including cross-provider fallback entries.
    """
    mid = (model_id or "").strip()
    prov = (provider or "").strip().rstrip("/")
    if not mid or not prov:
        return mid
    if mid == prov or mid.startswith(prov + "/"):
        return mid
    if provider_relative or "/" not in mid:
        return f"{prov}/{mid}"
    return mid


def derive_slot_provider(default_slot: str, hermes_provider: str = "") -> str:
    """Resolve the provider a shared models: default slot belongs to."""
    prov = (hermes_provider or "").strip()
    if prov:
        return prov
    slot = (default_slot or "").strip()
    if "/" in slot:
        return slot.split("/", 1)[0].strip()
    return ""


def _read_model_default() -> tuple[str, str] | None:
    """Resolve the EFFECTIVE default model from the hermes config view.

    Returns (provider, model_id) or None. Uses the same load path the
    CLI uses (MERCURY_CONFIG-aware) rather than parsing YAML by hand.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from mercury_cli.config import load_config  # noqa: WPS433
        cfg = load_config()
        model = cfg.get("model") or {}
        default = str(model.get("default") or "").strip()
        provider = str(model.get("provider") or "").strip()
        if default and provider:
            return provider, default
        if default:
            return "", default
    except Exception:
        pass
    # fallback: parse the unified file's hermes subtree directly
    try:
        import yaml
        whole = yaml.safe_load(_unified_path().read_text()) or {}
        sub = whole.get("hermes") or {}
        model = sub.get("model") or {}
        default = str(model.get("default") or "").strip()
        provider = str(model.get("provider") or "").strip()
        if default:
            return provider, default
    except Exception:
        pass
    return None


def _read_fallback() -> str | None:
    """First entry of the hermes fallback_providers chain, provider-qualified."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from mercury_cli.config import load_config  # noqa: WPS433
        cfg = load_config()
        chain = cfg.get("fallback_providers") or []
        if chain and isinstance(chain[0], dict):
            prov = str(chain[0].get("provider") or "").strip()
            mid = str(chain[0].get("model") or "").strip()
            if mid:
                return qualify_omp_model(mid, prov, provider_relative=True)
    except Exception:
        pass
    return None


def _current_slots() -> dict[str, str]:
    """Read the shared models: block as a dict (empty strings when absent)."""
    slots: dict[str, str] = {"default": "", "fallback": "", "delegate_model": "", "delegate_fallback": "", "delegate_thinking_level": "", "delegate_fallback_thinking_level": "", "orchestrator_thinking_level": ""}
    try:
        import yaml
        whole = yaml.safe_load(_unified_path().read_text()) or {}
        from mercury_cli.profile_defaults import resolve_model_defaults
        whole = resolve_model_defaults(whole, _unified_path())
        models = whole.get("models") or {}
        for k in slots:
            v = str(models.get(k) or "").strip()
            if v:
                slots[k] = v
    except Exception:
        pass
    return slots


def _current_chains() -> dict[str, list[str]]:
    """Read the ordered models: fallback chains as lists of ids."""
    chains: dict[str, list[str]] = {"fallback_chain": [], "delegate_fallback_chain": []}
    try:
        import yaml
        whole = yaml.safe_load(_unified_path().read_text()) or {}
        from mercury_cli.profile_defaults import resolve_model_defaults
        whole = resolve_model_defaults(whole, _unified_path())
        models = whole.get("models") or {}
        for k in chains:
            v = models.get(k) or []
            if isinstance(v, (list, tuple)):
                chains[k] = [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        pass
    return chains


from mercury_cli.model_settings import LEGACY_THINKING_SLOTS, per_model_reasoning


def _current_reasoning_overrides() -> dict[str, str]:
    """Read provider-qualified model reasoning choices from the shared file."""
    import yaml

    try:
        whole = yaml.safe_load(_unified_path().read_text(encoding="utf-8")) or {}
        from mercury_cli.profile_defaults import resolve_model_defaults
        whole = resolve_model_defaults(whole, _unified_path())
        overrides = (whole.get("models") or {}).get("reasoning_overrides") or {}
        if isinstance(overrides, dict):
            return {str(k): str(v) for k, v in overrides.items() if isinstance(v, str)}
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        pass
    return {}


def _strip_hermes_fallback_mirror(lines: list[str]) -> list[str]:
    """Remove hermes.subtree fallback_providers (block-seq list of dicts).

    Line-oriented: finds 'hermes:', then within it '  fallback_providers:'
    and drops that key plus its indented '- ...' continuation lines. The
    models: block stays untouched; other hermes: keys stay.
    """
    out: list[str] = []
    in_hermes = False
    skipped_indent = None
    for line in lines:
        if re.match(r"^hermes:\s*$", line):
            in_hermes = True
            skipped_indent = None
            out.append(line)
            continue
        if in_hermes and line and not line[0].isspace():
            in_hermes = False
            skipped_indent = None
        mirror = re.match(r"^(\s+)fallback_providers:", line)
        if in_hermes and mirror:
            skipped_indent = len(mirror[1])
            continue
        if skipped_indent is not None:
            indent = len(line) - len(line.lstrip())
            if not line.strip() or indent > skipped_indent or (
                indent == skipped_indent and line.lstrip().startswith("- ")
            ):
                continue
            skipped_indent = None
        out.append(line)
    return out


def _write_slots(update: dict[str, Any]) -> bool:
    """Parse shared models once; atomically replace that block, preserving peers.

    None deletes obsolete keys. YAML handles quoted IDs, maps, lists and any
    valid indentation instead of guessing structure from individual lines.
    """
    import yaml
    from utils import atomic_write_text

    if not update:
        return False
    path = _unified_path()
    text = path.read_text() if path.exists() else ""
    whole = yaml.safe_load(text) or {}
    models = dict(whole.get("models") or {})
    for key, value in update.items():
        if value is None:
            models.pop(key, None)
        else:
            models[key] = value
    if text.lstrip().startswith("{"):
        whole["models"] = models
        text = yaml.safe_dump(whole, sort_keys=False, allow_unicode=True)
    else:
        block = yaml.safe_dump({"models": models}, sort_keys=False, allow_unicode=True)
        pattern = r"^models:[^\n]*(?:\n(?!\S)[^\n]*)*"
        if re.search(r"^models:", text, re.M):
            text = re.sub(pattern, lambda match: block.rstrip("\n"), text, count=1, flags=re.M)
        else:
            text = text.rstrip("\n") + "\n\n" + block
    if any(key in update and update[key] is not None
           for key in ("fallback", "fallback_chain", "delegate_fallback", "delegate_fallback_chain")):
        text = "\n".join(_strip_hermes_fallback_mirror(text.split("\n")))
    atomic_write_text(path, text.rstrip("\n") + "\n", preserve_mode=True)
    return True


def _render_omp() -> bool:
    """Run the config bridge --render-omp so the omp subtree inherits."""
    # Repo checkout: subprocess the bridge (preserves skills-union side effect).
    root = _repo_root()
    if root is not None:
        bridge = root / "bridge" / "bridge.py"
        if bridge.exists():
            try:
                from mercury_cli.omp_command import omp_profile_env
                r = subprocess.run(
                    [sys.executable, str(bridge), "--render-omp"],
                    capture_output=True, text=True, timeout=30, env=omp_profile_env(),
                )
                if r.returncode == 0:
                    return True
            except Exception:
                pass
    # Installed layout (no repo root): import the bridge file directly by
    # path (HERMES_OMP_BRIDGE override first, then alongside this package).
    # Falls back to the ensure helper's surgical omp pin, so memory still
    # lands unified even when the full bridge cannot run.
    candidates: list[Path] = []
    env_bridge = os.environ.get("HERMES_OMP_BRIDGE", "").strip()
    if env_bridge:
        candidates.append(Path(env_bridge))
    try:
        here = Path(__file__).resolve()
        for parent in here.parents:
            cand = parent / "bridge" / "bridge.py"
            if cand not in candidates:
                candidates.append(cand)
            repo_cand = parent / "bin" / "mercury"
            _ = repo_cand
    except Exception:
        pass
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            import importlib.util as _ilu

            spec = _ilu.spec_from_file_location("mercury_bridge_fallback", str(cand))
            if spec is None or spec.loader is None:
                continue
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            slots = mod.parse_config()
            errors = mod.validate(slots, need_delegate=True)
            if errors:
                continue
            mod.render_omp_subtree(slots)
            try:
                mod._refresh_omp_skills_union()
            except Exception:
                pass
            return True
        except Exception:
            continue
    try:
        from mercury_cli.memory_setup import _ensure_omp_mnemopi_defaults

        _ensure_omp_mnemopi_defaults()
    except Exception:
        pass
    return False


def sync_omp_from_setup(quiet: bool = False) -> bool:
    """Entry point: slots <- wizard result, then bridge render.

    Returns True when both engines now resolve models.

    Also ensures the local mnemosyne memory default (config only, no pip):
    fresh installs get memory.provider=mnemosyne; explicit user backends
    are never clobbered. The bridge render below pins the matching omp
    memory.backend=mnemopi subtree.
    """
    try:
        from mercury_cli.memory_setup import ensure_mnemosyne_default
        ensure_mnemosyne_default(install=False)
    except Exception:
        pass
    default = _read_model_default()
    if default is None:
        if not quiet:
            print("omp-sync: no hermes model configured — nothing to sync")
        return False
    provider, model_id = default
    qualified = qualify_omp_model(model_id, provider, provider_relative=True)

    update: dict[str, Any] = {}
    slots = _current_slots()
    if slots["default"] != qualified:
        update["default"] = qualified
    # Only qualify selectors without a slash. Existing shared selectors
    # retain their provider, including cross-provider fallbacks.
    if slots["fallback"]:
        _repaired_fb = qualify_omp_model(slots["fallback"], provider)
        if _repaired_fb != slots["fallback"]:
            update["fallback"] = _repaired_fb
    else:
        # If the wizard left fallback unset, keep existing slots; do NOT
        # invent a fallback (unset means the user chose none — never invent).
        fb = _read_fallback()
        if fb and slots["fallback"] != fb:
            update["fallback"] = fb

    if provider:
        _chains = _current_chains()
        _fb_chain = _chains.get("fallback_chain") or []
        if _fb_chain:
            _repaired_chain = [qualify_omp_model(x, provider) for x in _fb_chain]
            if _repaired_chain != _fb_chain:
                update["fallback_chain"] = _repaired_chain
    # SKIP EQUALS EMPTY (user directive 2026-09-05): delegate slots are
    # NEVER auto-filled from default/fallback mirrors. Silently writing a
    # model the user skipped is exactly the "stale slot resurrection" that
    # produced duplicate chains after reinstall+skip. Empty delegate_model
    # fails loudly at the bridge ("required for delegation") instead of
    # degrading to an unchosen model.

    # Migrate legacy slot-level effort into the per-model authority once.
    import yaml
    whole = yaml.safe_load(_unified_path().read_text()) or {}
    from mercury_cli.model_settings import shared_models
    models = shared_models(whole)
    levels = models.get("reasoning_overrides") or {}
    if levels != (whole.get("models") or {}).get("reasoning_overrides", {}):
        update["reasoning_overrides"] = levels
    for key in LEGACY_THINKING_SLOTS:
        if key in (whole.get("models") or {}):
            update[key] = None

    if update:
        _write_slots(update)
        if not quiet:
            print(f"omp-sync: models slots updated ({', '.join(sorted(update))})")
    else:
        if not quiet:
            print("omp-sync: slots already consistent")
    ok = _render_omp()
    if not quiet:
        print("omp-sync: omp subtree rendered" if ok else "omp-sync: bridge render FAILED")
    return ok
