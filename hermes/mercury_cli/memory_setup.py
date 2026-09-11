"""mercury memory setup|status — configure memory provider plugins.

Auto-detects installed memory providers via the plugin system.
Interactive curses-based UI for provider selection, then walks through
the provider's config schema. Writes config to config.yaml + .env.
"""

from __future__ import annotations

import os
import re
import sys
import shlex
from pathlib import Path

from mercury_constants import get_hermes_home
from mercury_cli.secret_prompt import masked_secret_prompt

_CANCELLED = -1


MNEMOSYNE_PROVIDER = "mnemosyne"
LOCAL_MNEMOSYNE_LABEL = "local mnemosyne"

# memory.provider values that mean "no external backend chosen" (fresh
# install or built-in-only). ensure_mnemosyne_default() treats these as
# "apply the local mnemosyne default"; any other value is an explicit user
# backend and is never clobbered (silent keep).
_UNSET_MEMORY_PROVIDERS = frozenset({"", "built-in", "builtin", "default", "none"})

# omp memory backends that count as an explicit user choice (never clobbered
# by the fresh-install default). Absent/empty means "bridge has not pinned
# yet" and defaults to mnemopi. Mirrors bridge/bridge.py so the ONE
# ensure_mnemosyne_default() pass stamps BOTH sides even when the bridge
# render has not run yet (setup-tail ordering, installed-layout fallback).
_VALID_OMP_MEMORY_BACKENDS = {"off", "local", "hindsight", "mnemopi", "sharpshooter", "mnemosyne"}
_VALID_MNEMOPI_SCOPINGS = {"global", "per-project", "per-project-tagged"}


def _yaml_sq_ensure(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _parse_yaml_bool_or_none_ensure(value):
    if value is None:
        return None
    s = str(value).strip().strip("'\"").lower()
    if s in ("true", "yes", "on", "1"):
        return True
    if s in ("false", "no", "off", "0"):
        return False
    return None


def _unified_config_path_for_ensure() -> str:
    explicit = os.environ.get("MERCURY_CONFIG", "").strip()
    if explicit:
        return explicit
    home = os.environ.get("MERCURY_HOME", "").strip()
    if home:
        return os.path.join(home, "config.yaml")
    try:
        return str(Path(get_hermes_home()) / "config.yaml")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mercury", "config.yaml")


def _shared_mnemopi_db_path_for_ensure() -> str:
    home = os.environ.get("MERCURY_HOME", os.path.expanduser("~/.mercury"))
    return os.path.join(home, "memories", "mnemopi.db")


def _existing_omp_memory_backend_ensure(text: str):
    m = re.search(r"^omp:(.*?)(?=^\S|\Z)", text, flags=re.M | re.S)
    if not m:
        return None
    block = m.group(1)
    mm = re.search(r"^[ \t]+memory:[ \t]*\n((?:^[ \t]+.*\n?)*)", block, flags=re.M)
    if mm:
        bm = re.search(r"backend\s*:\s*[\"']?([A-Za-z0-9_-]+)", mm.group(1))
        if bm:
            return bm.group(1)
        return None
    dm = re.search(r"memory\.backend\s*:\s*[\"']?([A-Za-z0-9_-]+)", block)
    if dm:
        return dm.group(1)
    return None


def _existing_omp_mnemopi_values_ensure(text: str) -> dict:
    m = re.search(r"^omp:(.*?)(?=^\S|\Z)", text, flags=re.M | re.S)
    if not m:
        return {}
    block = m.group(1)
    mm = re.search(r"^[ \t]+mnemopi:[ \t]*\n((?:^[ \t]+.*\n?)*)", block, flags=re.M)
    if not mm:
        return {}
    chunk = mm.group(1)
    out: dict = {}
    for key in ("dbPath", "bank", "scoping", "autoRecall", "autoRetain", "noEmbeddings"):
        km = re.search(r"(?m)^\s*" + re.escape(key) + r"\s*:\s*(.+?)\s*$", chunk)
        if km:
            out[key] = km.group(1).strip()
    return out


def _ensure_omp_mnemopi_defaults() -> bool:
    """Pin omp memory.backend=mnemopi + FTS-only mnemopi defaults when unset.

    Fresh installs land unified (mnemopi, FTS-only both sides) even before
    the bridge render runs. Explicit user backends (off/local/hindsight/…)
    are never clobbered; explicit mnemopi keys (custom dbPath/bank, valid
    scoping, explicit noEmbeddings false opt-in) are preserved. The omp
    schema defaults to backend off + embeddings ON when keys are absent, so
    the noEmbeddings:true key is always rendered explicitly on the default
    path. Returns True when the file was created/updated, False when left
    as an explicit user backend or on any I/O failure.
    """
    try:
        path = _unified_config_path_for_ensure()
        text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    except Exception:
        return False
    try:
        old_backend = _existing_omp_memory_backend_ensure(text)
        new_backend = old_backend if old_backend is not None else "mnemopi"
        if new_backend == "mnemosyne":
            new_backend = "mnemopi"
        if new_backend != "mnemopi":
            # Explicit user backend (off/local/…): never clobber, no block.
            return False
        old_mn = _existing_omp_mnemopi_values_ensure(text)
        db_path = (old_mn.get("dbPath") or "").strip().strip("'\"") or _shared_mnemopi_db_path_for_ensure()
        bank = (old_mn.get("bank") or "").strip().strip("'\"") or "default"
        scoping = (old_mn.get("scoping") or "").strip().strip("'\"")
        if scoping not in _VALID_MNEMOPI_SCOPINGS:
            scoping = "global"
        auto_recall = _parse_yaml_bool_or_none_ensure(old_mn.get("autoRecall"))
        if auto_recall is None:
            auto_recall = True
        auto_retain = _parse_yaml_bool_or_none_ensure(old_mn.get("autoRetain"))
        if auto_retain is None:
            auto_retain = True
        no_emb = _parse_yaml_bool_or_none_ensure(old_mn.get("noEmbeddings"))
        if no_emb is None:
            no_emb = True
        mem_block = (
            "  memory:\n"
            "    backend: mnemopi\n"
            "  mnemopi:\n"
            f"    dbPath: {_yaml_sq_ensure(db_path)}\n"
            f"    bank: {_yaml_sq_ensure(bank)}\n"
            f"    scoping: {scoping}\n"
            f"    autoRecall: {str(auto_recall).lower()}\n"
            f"    autoRetain: {str(auto_retain).lower()}\n"
            f"    noEmbeddings: {str(no_emb).lower()}\n"
        )
        m = re.search(r"^omp:(.*?)(?=^\S|\Z)", text, flags=re.M | re.S)
        if not m:
            # No omp block yet (truly fresh): append a minimal unified block.
            new_text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "omp:\n" + mem_block
        else:
            old_omp = m.group(0)
            if "memory:" in m.group(1) or "mnemopi:" in m.group(1):
                # Has a memory/mnemopi section already: normalize backend to
                # mnemopi and fill only missing mnemopi keys, preserving
                # every other omp key (approvalMode/retry/providers/…).
                new_omp = old_omp
                new_omp = re.sub(
                    r"(^[ \t]+backend\s*:\s*[\"']?)([A-Za-z0-9_-]+)",
                    r"\1mnemopi",
                    new_omp, count=1, flags=re.M,
                )
                # Ensure a mnemopi: block exists.
                if not re.search(r"^[ \t]+mnemopi:[ \t]*\n", new_omp, flags=re.M):
                    new_omp = new_omp.rstrip("\n") + "\n" + mem_block.split("  memory:\n    backend: mnemopi\n", 1)[1]
                else:
                    # Fill missing keys inside the existing mnemopi block.
                    mm = re.search(r"(^[ \t]+mnemopi:[ \t]*\n((?:^[ \t]+.*\n?)*))", new_omp, flags=re.M)
                    if mm:
                        chunk = mm.group(0)
                        missing_lines = ""
                        if not re.search(r"(?m)^\s*dbPath\s*:", chunk):
                            missing_lines += f"    dbPath: {_yaml_sq_ensure(db_path)}\n"
                        if not re.search(r"(?m)^\s*bank\s*:", chunk):
                            missing_lines += f"    bank: {_yaml_sq_ensure(bank)}\n"
                        if not re.search(r"(?m)^\s*scoping\s*:", chunk):
                            missing_lines += f"    scoping: {scoping}\n"
                        if not re.search(r"(?m)^\s*autoRecall\s*:", chunk):
                            missing_lines += f"    autoRecall: {str(auto_recall).lower()}\n"
                        if not re.search(r"(?m)^\s*autoRetain\s*:", chunk):
                            missing_lines += f"    autoRetain: {str(auto_retain).lower()}\n"
                        if not re.search(r"(?m)^\s*noEmbeddings\s*:", chunk):
                            # Schema defaults embeddings ON when absent:
                            # render explicitly so fresh stays FTS-only.
                            missing_lines += f"    noEmbeddings: {str(no_emb).lower()}\n"
                        if missing_lines:
                            new_omp = new_omp[:mm.end()] + missing_lines + new_omp[mm.end():]
                new_text = text[:m.start()] + new_omp + text[m.end():]
            else:
                # omp block exists without any memory section (setup wrote
                # approvals/models first): append the unified section,
                # preserving everything already there.
                new_omp = old_omp.rstrip("\n") + "\n" + mem_block
                new_text = text[:m.start()] + new_omp + text[m.end():]
        if new_text == text:
            return True
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        return True
    except Exception:
        return False


def _wizard_default_index(names: list, current: str, builtin_idx: int) -> int:
    """Picker default: current backend on re-runs, mnemosyne when fresh."""
    if current in names:
        return names.index(current)
    if MNEMOSYNE_PROVIDER in names and (not current or current in _UNSET_MEMORY_PROVIDERS):
        return names.index(MNEMOSYNE_PROVIDER)
    return builtin_idx

def ensure_mnemosyne_default(*, install: bool = True, verbose: bool = False) -> str:
    """Auto-enable the local mnemosyne provider on fresh installs.

    ONE place stamps BOTH sides so fresh installs land unified: hermes
    ``memory.provider=mnemosyne`` plus omp ``memory.backend=mnemopi`` with
    ``mnemopi{dbPath,bank,scoping:global,noEmbeddings:true,autoRecall,
    autoRetain}`` (FTS-only both sides). Silent (no output, no write) when
    another backend is already active — an explicit user backend is never
    clobbered. Returns the effective provider name ("" when the config
    could not be read/written).

    With ``install=True`` (``mercury setup`` tail), also re-verifies the
    ``mnemosyne-hermes`` package (Mercury default ``[embeddings]`` profile,
    pulled as a dependency of ``mnemosyne-hermes`` — never ``[all]``):
    a venv rebuild that stripped it is detected via the import probe and
    reinstalled warn-only. The provider itself is in-tree stdlib-only, so
    memory keeps working via FTS even when the install fails offline.
    """
    from mercury_cli.config import load_config, save_config

    try:
        config = load_config()
    except Exception:
        return ""
    if not isinstance(config, dict):
        return ""
    mem = config.get("memory")
    if not isinstance(mem, dict):
        mem = {}
        config["memory"] = mem
    current = str(mem.get("provider", "") or "").strip()
    if current not in _UNSET_MEMORY_PROVIDERS:
        # Explicit user backend (or already mnemosyne): never clobber, silent.
        # Omp convergence on reruns rides the bridge render in sync_omp (which
        # preserves explicit backends); the fresh path below stamps BOTH sides
        # so first boot lands unified.
        return current
    mem["provider"] = MNEMOSYNE_PROVIDER
    try:
        save_config(config)
    except Exception:
        return ""
    # Fresh default: pin the omp side in the same pass so the preflight
    # passes on first boot (bridge render later is idempotent).
    try:
        _ensure_omp_mnemopi_defaults()
    except Exception:
        pass
    if verbose:
        print(f"\n  Memory provider: {LOCAL_MNEMOSYNE_LABEL} (shared bank default)")
    if install:
        # Re-verify + repair the package on every setup run (venv rebuilds
        # must not silently drop it); warn-only, FTS works regardless.
        try:
            _install_dependencies(MNEMOSYNE_PROVIDER)
        except Exception:
            pass
    return MNEMOSYNE_PROVIDER

def _provider_pip_dependencies(provider_name: str, declared: list) -> list:
    """Return the pip deps a provider actually needs on THIS install.

    ``plugin.yaml`` declares the provider's baseline bridge packages, but
    some providers install mode-dependent extras at setup time that the
    manifest can't express. Hindsight's ``local_embedded`` mode installs
    ``hindsight-all`` (daemon + embedder + client) during
    ``mercury memory setup`` — if the update-time refresh only reinstalled
    the declared ``hindsight-client``, the embedded daemon would stay
    broken after a venv rebuild stripped ``hindsight-embed`` (#70636).
    """
    deps = list(declared or [])
    if provider_name == "hindsight":
        try:
            import json
            cfg_path = get_hermes_home() / "hindsight" / "config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            mode = cfg.get("mode", "")
            # "local" is a legacy alias for "local_embedded"
            if mode in {"local", "local_embedded"}:
                deps.append("hindsight-all")
        except Exception:
            pass
    return deps


# ---------------------------------------------------------------------------
# Curses-based interactive picker (same pattern as mercury tools)
# ---------------------------------------------------------------------------

def _curses_select(
    title: str,
    items: list[tuple[str, str]],
    default: int = 0,
    *,
    cancel_returns: int | None = None,
) -> int:
    """Interactive single-select with arrow keys.

    items: list of (label, description) tuples.
    Returns selected index, or cancel_returns/default on escape/quit.
    """
    from mercury_cli.curses_ui import curses_radiolist

    if cancel_returns is None:
        cancel_returns = default

    # Format (label, desc) tuples into display strings
    display_items = [
        f"{label} - {desc}" if desc else label
        for label, desc in items
    ]
    result = curses_radiolist(title, display_items, selected=default, cancel_returns=cancel_returns)
    _clear_interactive_transition()
    return result


def _print_cancelled_setup() -> None:
    print("\n  Cancelled. No changes saved.\n")


def _clear_interactive_transition() -> None:
    """Clear stale curses content before entering a follow-up setup screen."""
    if not sys.stdout.isatty():
        return
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    """Prompt for a value with optional default and secret masking."""
    suffix = f" [{default}]" if default else ""
    if secret:
        val = masked_secret_prompt(f"  {label}{suffix}: ")
    else:
        sys.stdout.write(f"  {label}{suffix}: ")
        sys.stdout.flush()
        val = sys.stdin.readline().strip()
    return val or (default or "")


# ---------------------------------------------------------------------------
# Provider discovery
# ---------------------------------------------------------------------------

def _install_dependencies(provider_name: str, *, force: bool = False) -> None:
    """Install pip dependencies declared in ``plugin.yaml``.

    When ``force`` is true, every declared dependency is handed to the
    installer even if its import currently succeeds — the resolver then
    reinstalls anything missing or version-drifted and no-ops on satisfied
    ranges. This is how ``mercury update`` heals the active memory provider
    after a venv rebuild/sync removed or downgraded its bridge packages
    (#53272, #70636).
    """
    import subprocess
    from plugins.memory import find_provider_dir

    plugin_dir = find_provider_dir(provider_name)
    if not plugin_dir:
        return
    yaml_path = plugin_dir / "plugin.yaml"
    if not yaml_path.exists():
        return

    try:
        import yaml
        with open(yaml_path, encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
    except Exception:
        return

    pip_deps = _provider_pip_dependencies(provider_name, meta.get("pip_dependencies", []))
    if not pip_deps:
        return

    # pip name → import name mapping for packages where they differ
    _IMPORT_NAMES = {
        "honcho-ai": "honcho",
        "mem0ai": "mem0",
        "hindsight-client": "hindsight_client",
        "hindsight-all": "hindsight",
    }

    # Check which packages need installation.
    missing = []
    for dep in pip_deps:
        if force:
            missing.append(dep)
            continue
        dep_name = re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*", dep)
        base = dep_name.group(0) if dep_name else dep
        import_name = _IMPORT_NAMES.get(base, base.replace("-", "_").split("[")[0])
        try:
            __import__(import_name)
        except ImportError:
            missing.append(dep)

    if not missing:
        return

    print(f"\n  Installing dependencies: {', '.join(missing)}")

    # Environment-aware install: on immutable hosted images the agent venv
    # is sealed read-only and installs must go to the durable target on the
    # data volume (HERMES_LAZY_INSTALL_TARGET). install_specs handles the
    # routing/gating; on normal installs it is venv-scoped as before (NS-605).
    from tools.lazy_deps import install_specs

    manual_cmd = f"uv pip install {' '.join(missing)}"
    try:
        outcome = install_specs(missing, timeout=120)
        if outcome.ok:
            print(f"  ✓ Installed {', '.join(missing)}")
        elif outcome.blocked:
            print(f"  ⚠ Cannot install {', '.join(missing)}: {outcome.reason}")
        else:
            print(f"  ⚠ Failed to install {', '.join(missing)}")
            stderr = (outcome.stderr or "")[:200]
            if stderr:
                print(f"    {stderr}")
            print(f"  Run manually: {manual_cmd}")
    except Exception as e:
        print(f"  ⚠ Install failed: {e}")
        print(f"  Run manually: {manual_cmd}")

    # Also show external dependencies (non-pip) if any
    ext_deps = meta.get("external_dependencies", [])
    for dep in ext_deps:
        dep_name = dep.get("name", "")
        check_cmd = dep.get("check", "")
        install_cmd = dep.get("install", "")
        if check_cmd:
            try:
                subprocess.run(
                    shlex.split(check_cmd), check=True, capture_output=True, timeout=5
                )
            except Exception:
                if install_cmd:
                    print(f"\n  ⚠ '{dep_name}' not found. Install with:")
                    print(f"    {install_cmd}")


def _get_available_providers() -> list:
    """Discover memory providers from plugins/memory/.

    Returns list of (name, description, provider_instance) tuples.
    """
    try:
        from plugins.memory import discover_memory_providers, load_memory_provider
        raw = discover_memory_providers()
    except Exception:
        raw = []

    results = []
    for name, desc, available in raw:
        try:
            provider = load_memory_provider(name)
            if not provider:
                continue
        except Exception:
            continue

        schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []
        has_secrets = any(f.get("secret") for f in schema)
        has_non_secrets = any(not f.get("secret") for f in schema)
        if has_secrets and has_non_secrets:
            setup_hint = "API key / local"
        elif has_secrets:
            setup_hint = "requires API key"
        elif not schema:
            setup_hint = "no setup needed"
        else:
            setup_hint = "local"

        results.append((name, setup_hint, provider))
    return results


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def _report_mnemosyne_preflight() -> bool:
    """Run the local mnemosyne preflight and print unified-or-loud-failure."""
    try:
        from plugins.memory.mnemosyne import format_preflight, preflight_shared_bank
    except Exception as exc:
        print(f"  local mnemosyne preflight skipped (provider not importable: {exc})")
        return False
    try:
        report = preflight_shared_bank()
    except Exception as exc:  # noqa: BLE001
        print(f"  local mnemosyne preflight failed to run: {exc}")
        return False
    for line in format_preflight(report).splitlines():
        print(f"  {line}")
    print()
    return bool(report.get("ok"))

def cmd_setup_provider(provider_name: str) -> None:
    """Run memory setup for a specific provider, skipping the picker."""
    from mercury_cli.config import load_config, save_config

    providers = _get_available_providers()
    match = None
    for name, desc, provider in providers:
        if name == provider_name:
            match = (name, desc, provider)
            break

    if not match:
        print(f"\n  Memory provider '{provider_name}' not found.")
        print("  Run 'mercury memory setup' to see available providers.\n")
        return

    name, _, provider = match

    _clear_interactive_transition()

    _install_dependencies(name)

    config = load_config()
    if not isinstance(config.get("memory"), dict):
        config["memory"] = {}

    if hasattr(provider, "post_setup"):
        mercury_home = str(get_hermes_home())
        provider.post_setup(mercury_home, config)
        return

    # Fallback: generic schema-based setup (same as cmd_setup)
    config["memory"]["provider"] = name
    save_config(config)
    display = LOCAL_MNEMOSYNE_LABEL if name == MNEMOSYNE_PROVIDER else name
    print(f"\n  Memory provider: {display}")
    print("  Activation saved to config.yaml\n")
    if name == MNEMOSYNE_PROVIDER:
        _ensure_omp_mnemopi_defaults()
        _report_mnemosyne_preflight()


def cmd_setup(args) -> None:
    """Interactive memory provider setup wizard."""
    from mercury_cli.config import load_config, save_config

    providers = _get_available_providers()

    if not providers:
        print("\n  No memory provider plugins detected.")
        print("  Install a plugin to ~/.mercury/plugins/ and try again.\n")
        return

    # Build picker items
    items = []
    for name, desc, _ in providers:
        items.append((name, f"— {desc}"))
    # Default selection: the current backend on re-runs (offer mnemosyne
    # alongside it); the local mnemosyne default on fresh installs.
    builtin_idx = len(items) - 1
    try:
        _mem = load_config().get("memory", {})
        _cur = _mem.get("provider", "") if isinstance(_mem, dict) else ""
        _cur = _cur if isinstance(_cur, str) else ""
    except Exception:
        _cur = ""
    default_idx = _wizard_default_index([n for n, _, _ in providers], _cur, builtin_idx)
    selected = _curses_select("Memory provider setup", items, default=default_idx, cancel_returns=_CANCELLED)
    if selected == _CANCELLED:
        _print_cancelled_setup()
        return

    config = load_config()
    if not isinstance(config.get("memory"), dict):
        config["memory"] = {}

    # Built-in only
    if selected >= len(providers):
        config["memory"]["provider"] = ""
        save_config(config)
        print("\n  ✓ Memory provider: built-in only")
        print("  Saved to config.yaml\n")
        return

    name, _, provider = providers[selected]

    _clear_interactive_transition()

    # Install pip dependencies if declared in plugin.yaml
    _install_dependencies(name)

    # If the provider has a post_setup hook, delegate entirely to it.
    # The hook handles its own config, connection test, and activation.
    if hasattr(provider, "post_setup"):
        mercury_home = str(get_hermes_home())
        provider.post_setup(mercury_home, config)
        return

    schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []

    provider_config = config["memory"].get(name, {})
    if not isinstance(provider_config, dict):
        provider_config = {}

    env_writes = {}

    if schema:
        print(f"\n  Configuring {name}:\n")

        for field in schema:
            key = field["key"]
            desc = field.get("description", key)
            default = field.get("default")
            # Dynamic default: look up default from another field's value
            default_from = field.get("default_from")
            if default_from and isinstance(default_from, dict):
                ref_field = default_from.get("field", "")
                ref_map = default_from.get("map", {})
                ref_value = provider_config.get(ref_field, "")
                if ref_value and ref_value in ref_map:
                    default = ref_map[ref_value]
            is_secret = field.get("secret", False)
            choices = field.get("choices")
            env_var = field.get("env_var")
            url = field.get("url")

            # Skip fields whose "when" condition doesn't match
            when = field.get("when")
            if when and isinstance(when, dict):
                if not all(provider_config.get(k) == v for k, v in when.items()):
                    continue

            if choices and not is_secret:
                # Use curses picker for choice fields
                choice_items = [(c, "") for c in choices]
                current = provider_config.get(key, default)
                current_idx = 0
                if current and current in choices:
                    current_idx = choices.index(current)
                sel = _curses_select(f"  {desc}", choice_items, default=current_idx, cancel_returns=_CANCELLED)
                if sel == _CANCELLED:
                    _print_cancelled_setup()
                    return
                provider_config[key] = choices[sel]
            elif is_secret:
                # Prompt for secret
                existing = os.environ.get(env_var, "") if env_var else ""
                if existing:
                    masked = f"...{existing[-4:]}" if len(existing) > 4 else "set"
                    val = _prompt(f"{desc} (current: {masked}, blank to keep)", secret=True)
                else:
                    hint = f"  Get yours at {url}" if url else ""
                    if hint:
                        print(hint)
                    val = _prompt(desc, secret=True)
                if val and env_var:
                    env_writes[env_var] = val
            else:
                # Regular text prompt
                current = provider_config.get(key)
                effective_default = current or default
                val = _prompt(desc, default=str(effective_default) if effective_default else None)
                if val:
                    provider_config[key] = val
                    # Also write to .env if this field has an env_var
                    if env_var and env_var not in env_writes:
                        env_writes[env_var] = val

    # Write activation key to config.yaml
    config["memory"]["provider"] = name
    save_config(config)

    # Write non-secret config to provider's native location
    mercury_home = str(get_hermes_home())
    if provider_config and hasattr(provider, "save_config"):
        try:
            provider.save_config(provider_config, mercury_home)
        except Exception as e:
            print(f"  Failed to write provider config: {e}")

    # Write secrets to .env
    if env_writes:
        _write_env_vars(env_writes)

    display = LOCAL_MNEMOSYNE_LABEL if name == MNEMOSYNE_PROVIDER else name
    print(f"\n  Memory provider: {display}")
    print("  Activation saved to config.yaml")
    if provider_config:
        print("  Provider config saved")
    if env_writes:
        print("  API keys saved to .env")
    if name == MNEMOSYNE_PROVIDER:
        print()
        _ensure_omp_mnemopi_defaults()
        _report_mnemosyne_preflight()
    print("\n  Start a new session to activate.\n")


def _write_env_vars(
    env_writes: dict,
    mercury_home: str | os.PathLike[str] | None = None,
) -> None:
    """Persist memory-provider env vars through the canonical ``.env`` writer.

    Delegates to ``mercury_cli.config.save_env_value`` so every key flows
    through the same input-validation gate as every other ``.env`` writer:
    the ``_ENV_VAR_NAME_RE`` regex (no malformed identifiers), the
    ``_ENV_VAR_NAME_DENYLIST`` (no ``LD_PRELOAD`` / ``PYTHONPATH`` /
    ``HERMES_HOME`` / etc.), CR/LF stripping on the value, and the atomic
    0o600-from-creation write (no TOCTOU permission window). This function
    previously wrote via ``Path.write_text`` directly, bypassing all of
    that: a memory-provider plugin schema declaring ``env_var: "LD_PRELOAD"``
    would land in ``.env`` verbatim and load via the ``env_loader.py``
    ``.env`` -> ``os.environ`` chain on the next Mercury startup, and the
    file existed at the default umask between the write and the later
    ``chmod`` regardless of key legitimacy.

    Validation failures (``ValueError`` from ``save_env_value`` — a
    denylisted name or an identifier rejected by ``_ENV_VAR_NAME_RE``) are
    surfaced and skipped rather than aborting the wizard, so a single bad
    key from one schema field doesn't take down the rest of the batch.
    Non-validation errors (filesystem failures, permission errors) are
    intentionally NOT caught — those indicate the wizard cannot safely
    persist any subsequent key either and should propagate.

    ``mercury_home`` may be supplied by plugin ``post_setup`` hooks that
    already received an explicit home directory (e.g. a non-default
    profile). It is applied through the context-local Mercury home override
    so ``save_env_value`` still owns the validation, sanitization, and
    atomic-write path without mutating global ``os.environ``.
    """
    from mercury_cli.config import save_env_value
    from mercury_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(mercury_home) if mercury_home is not None else None
    try:
        for key, val in env_writes.items():
            try:
                save_env_value(key, val)
            except ValueError as exc:
                print(f"  Skipping {key}: {exc}")
    finally:
        if token is not None:
            reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def cmd_status(args) -> None:
    """Show current memory provider config."""
    from mercury_cli.config import load_config

    config = load_config()
    mem_config = config.get("memory", {})
    provider_name = mem_config.get("provider", "")

    memory_enabled = mem_config.get("memory_enabled", True)
    user_profile_enabled = mem_config.get("user_profile_enabled", True)

    mem_mark = "enabled ✓" if memory_enabled else "disabled ✗"
    user_mark = "enabled ✓" if user_profile_enabled else "disabled ✗"

    # Check if the memory tool is enabled for the CLI platform via the
    # canonical resolver and respects the check_fn gate when both stores are disabled.
    from mercury_cli.tools_config import _get_platform_tools
    from tools.memory_tool import check_memory_requirements
    cli_tools = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    memory_tool_enabled = ("memory" in cli_tools) and check_memory_requirements()
    tool_mark = "enabled ✓" if memory_tool_enabled else "disabled ✗"

    print("\nMemory status\n" + "─" * 40)
    print("  Built-in (MEMORY.md / USER.md):")
    print(f"    Memory injection:   {mem_mark}")
    print(f"    User profile:       {user_mark}")
    print(f"    Memory tool:        {tool_mark}")
    print(f"  Provider:  {provider_name or '(none — built-in only)'}")

    providers = _get_available_providers()
    provider = None
    for pname, _, candidate in providers:
        if pname == provider_name:
            provider = candidate
            break

    if provider_name:
        provider_config = mem_config.get(provider_name, {})
        display_config = provider_config
        if provider and hasattr(provider, "get_status_config"):
            try:
                display_config = provider.get_status_config(provider_config)
            except Exception as e:
                display_config = dict(provider_config) if isinstance(provider_config, dict) else provider_config
                if isinstance(display_config, dict):
                    display_config["status_config_error"] = str(e)

        if display_config:
            print(f"\n  {provider_name} config:")
            for key, val in display_config.items():
                print(f"    {key}: {val}")

        if provider:
            print("\n  Plugin:    installed ✓")
            if provider.is_available():
                print("  Status:    available ✓")
            else:
                print("  Status:    not available ✗")
                schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []
                # Check all fields that have env_var (both secret and non-secret)
                required_fields = [f for f in schema if f.get("env_var")]
                if required_fields:
                    print("  Missing:")
                    for f in required_fields:
                        env_var = f.get("env_var", "")
                        url = f.get("url", "")
                        is_set = bool(os.environ.get(env_var))
                        mark = "✓" if is_set else "✗"
                        line = f"    {mark} {env_var}"
                        if url and not is_set:
                            line += f"  → {url}"
                        print(line)
                print(
                    "  Note: systemd/gateway services do not inherit ~/.mercury/.env —"
                )
                print(
                    "        set any variables above in the service environment."
                )
        else:
            print("\n  Plugin:    NOT installed ✗")
            print(f"  Install the '{provider_name}' memory plugin to ~/.mercury/plugins/")

    if providers:
        print("\n  Installed plugins:")
        for pname, desc, _ in providers:
            active = " ← active" if pname == provider_name else ""
            print(f"    • {pname}  ({desc}){active}")

    print()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def memory_command(args) -> None:
    """Route memory subcommands."""
    sub = getattr(args, "memory_command", None)
    if sub == "setup":
        provider = getattr(args, "provider", None)
        if provider:
            cmd_setup_provider(provider)
        else:
            cmd_setup(args)
    elif sub == "status":
        cmd_status(args)
    else:
        cmd_status(args)
