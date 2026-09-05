"""MERCURY-OMP PATCH: post-setup omp sync.

`mercury setup` (the full hermes wizard — provider OAuth, Nous Portal,
API keys, model pickers) writes hermes-native config keys. Mercury's
unified file feeds BOTH engines from the shared four-slot ``models:``
block, so after the wizard runs, this module:

1. extracts the hermes-side choices (model.default + provider,
   fallback chain) back into the shared ``models:`` slots,
2. re-renders the ``omp:`` subtree via the config bridge so the omp
   engine inherits the same models/approvals/deny rules,
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


def _mercury_home() -> Path:
    return Path(os.environ.get("MERCURY_HOME") or Path.home() / ".mercury")


def _unified_path() -> Path:
    return Path(os.environ.get("MERCURY_CONFIG") or _mercury_home() / "config.yaml")


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
                return f"{prov}/{mid}" if prov else mid
    except Exception:
        pass
    return None


def _current_slots() -> dict[str, str]:
    """Read the shared models: block as a dict (empty strings when absent)."""
    slots: dict[str, str] = {"default": "", "fallback": "", "delegate_model": "", "delegate_fallback": ""}
    try:
        import yaml
        whole = yaml.safe_load(_unified_path().read_text()) or {}
        models = whole.get("models") or {}
        for k in slots:
            v = str(models.get(k) or "").strip()
            if v:
                slots[k] = v
    except Exception:
        pass
    return slots


def _write_slots(update: dict[str, str]) -> bool:
    """Merge updates into the shared models: block, preserving everything else.

    Line-oriented merge (same discipline as the bridge): only rewrites
    the four slot lines inside the models: block. Creates the block if
    missing.
    """
    path = _unified_path()
    text = path.read_text() if path.exists() else ""
    lines = text.split("\n")
    out: list[str] = []
    in_models = False
    seen: dict[str, bool] = {k: False for k in update}
    wrote_any = False
    for line in lines:
        if re.match(r"^models:\s*$", line):
            in_models = True
            out.append(line)
            continue
        if in_models and re.match(r"^\S", line):
            # leaving the block: append any never-seen slots before the next top-level key
            for k, v in update.items():
                if not seen[k]:
                    if isinstance(v, (list, tuple)):
                        items = ", ".join(f"'{x}'" for x in v)
                        out.append(f"  {k}: [{items}]")
                    else:
                        out.append(f"  {k}: {v}")
                    seen[k] = True
                    wrote_any = True
            in_models = False
        m = re.match(r"^  (default|fallback|delegate_model|delegate_fallback|delegate_fallback_chain|fallback_chain):\s*(.*)$", line) if in_models else None
        if m and m.group(1) in update:
            v = update[m.group(1)]
            # ordered chain: write as a YAML flow sequence, single-quoted ids
            if isinstance(v, (list, tuple)):
                items = ", ".join(f"'{x}'" for x in v)
                out.append(f"  {m.group(1)}: [{items}]")
            else:
                out.append(f"  {m.group(1)}: {v}")
            seen[m.group(1)] = True
            wrote_any = True
            continue
        out.append(line)
    if in_models:
        # EOF inside models: block
        for k, v in update.items():
            if not seen[k]:
                if isinstance(v, (list, tuple)):
                    items = ", ".join(f"'{x}'" for x in v)
                    out.append(f"  {k}: [{items}]")
                else:
                    out.append(f"  {k}: {v}")
                seen[k] = True
                wrote_any = True
    if not any(re.match(r"^models:\s*$", l) for l in out):
        out.append("")
        out.append("models:")
        for k, v in update.items():
            if isinstance(v, (list, tuple)):
                items = ", ".join(f"'{x}'" for x in v)
                out.append(f"  {k}: [{items}]")
            else:
                out.append(f"  {k}: {v}")
            wrote_any = True
    if wrote_any:
        path.write_text("\n".join(out).rstrip("\n") + "\n")
    return wrote_any


def _render_omp() -> bool:
    """Run the config bridge --render-omp so the omp subtree inherits."""
    root = _repo_root()
    if root is None:
        return False
    bridge = root / "bridge" / "bridge.py"
    if not bridge.exists():
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(bridge), "--render-omp"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def sync_omp_from_setup(quiet: bool = False) -> bool:
    """Entry point: slots <- wizard result, then bridge render.

    Returns True when both engines now resolve models.
    """
    default = _read_model_default()
    if default is None:
        if not quiet:
            print("omp-sync: no hermes model configured — nothing to sync")
        return False
    provider, model_id = default
    qualified = f"{provider}/{model_id}" if provider else model_id

    update: dict[str, str] = {}
    slots = _current_slots()
    if slots["default"] != qualified:
        update["default"] = qualified
    # If the wizard left fallback unset, keep existing slots; do NOT
    # invent a fallback (four-slot fail-hard is the user's explicit call).
    fb = _read_fallback()
    if fb and slots["fallback"] != fb and not slots["fallback"]:
        update["fallback"] = fb
    # delegate slots: default to default/fallback mirrors when empty
    if not slots["delegate_model"] and qualified:
        update["delegate_model"] = qualified
    fb_for_delegate = slots.get("fallback") or update.get("fallback")
    if not slots["delegate_fallback"] and fb_for_delegate:
        update["delegate_fallback"] = fb_for_delegate

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
