#!/usr/bin/env python3
"""Mercury bridge v3 — unified config (~/.mercury/config.yaml), one file.

Four model slots (top level): default/fallback/delegate_model/
delegate_fallback (fallbacks optional; chains validated). Default config path:
~/.mercury/config.yaml (MERCURY_CONFIG env or HERMES_OMP_CONFIG override).

--render-omp now writes INTO the unified file's omp: subtree (preserving
models:/hermes:), instead of a separate ~/.mercury/omp/agent/config.yml.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "hermes"))
MERCURY_HOME = os.environ.get("MERCURY_HOME", os.path.expanduser("~/.mercury"))
DEFAULT_CONFIG = os.path.join(MERCURY_HOME, "config.yaml")
CONFIG = os.environ.get("HERMES_OMP_CONFIG", os.environ.get("MERCURY_CONFIG", DEFAULT_CONFIG))

SLOTS = ("default", "fallback", "delegate_model", "delegate_fallback")
# Read compatibility for older configs. Setup migrates these obsolete slot
# fields to models.reasoning_overrides, keyed by provider/model identity.
THINKING_SLOTS = ("delegate_thinking_level", "delegate_fallback_thinking_level", "orchestrator_thinking_level")

# omp interactive-onboarding version (omp/src/modes/setup-version.ts).
# The Mercury wizard + omp-sync stamp this so `mercury omp` never demands
# its five-scene first-run setup on an installer-configured machine.
CURRENT_SETUP_VERSION = 2


def parse_config(path=None):
    """Read model slots with the same YAML parser used by both engines."""
    import yaml
    path = path or CONFIG
    try:
        with open(path) as source:
            config = yaml.safe_load(source) or {}
    except FileNotFoundError:
        sys.exit(f"FATAL: {path} not found")
    from mercury_cli.model_settings import shared_models
    from mercury_cli.profile_defaults import resolve_model_defaults
    from pathlib import Path
    config = resolve_model_defaults(config, Path(path))
    models = shared_models(config)
    if not isinstance(models, dict):
        raise ValueError("models must be a mapping")
    slots = {key: str(models.get(key) or "").strip() for key in (*SLOTS, *THINKING_SLOTS)}
    for key in ("fallback_chain", "delegate_fallback_chain"):
        chain = models.get(key) or []
        if not isinstance(chain, list) or any(not isinstance(item, str) for item in chain):
            raise ValueError(f"models.{key} must be a list of model names")
        slots[key] = [item for item in chain if item]
    overrides = models.get("reasoning_overrides") or {}
    if not isinstance(overrides, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in overrides.items()):
        raise ValueError("models.reasoning_overrides must map model names to reasoning levels")
    slots["reasoning_overrides"] = overrides
    windows = models.get("context_windows") or {}
    if not isinstance(windows, dict) or any(not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v <= 0 for k, v in windows.items()):
        raise ValueError("models.context_windows must map model names to positive token limits")
    slots["context_windows"] = windows
    return slots


def validate(slots, need_delegate=False):
    errors = []
    for selector, level in (slots.get("reasoning_overrides") or {}).items():
        if thinking_level_from_config(level) is None:
            errors.append(f"models.reasoning_overrides[{selector!r}] has invalid reasoning level {level!r}")
    if not slots["default"]:
        errors.append("models.default is empty — set the main session model")
    # OPTIONAL fallbacks (user directive 2026-09-05): the wizard allows
    # skipping the first-order fallbacks at both scopes. An unset fallback is
    # a legitimate "no mid-turn failover configured" choice — NOT an error.
    # The chain invariants below still fully apply when values ARE set.
    if need_delegate:
        if not slots["delegate_model"]:
            errors.append("models.delegate_model is empty — required for delegation")
    if slots["default"] and slots["fallback"] and slots["default"] == slots["fallback"]:
        errors.append("models.default == models.fallback — fallback must be a distinct model")
    if (slots["delegate_model"] and slots["delegate_fallback"]
            and slots["delegate_model"] == slots["delegate_fallback"]):
        errors.append("models.delegate_model == models.delegate_fallback — fallback must be distinct")
    fchain = slots.get("fallback_chain") or []
    if fchain:
        # An EMPTY chain is valid (no second-order fallback configured — the
        # wizard writes [] when the user skips). Invariant applies only to
        # non-empty chains: head must be the primary fallback.
        if len(set(fchain)) != len(fchain):
            errors.append("models.fallback_chain contains duplicates")
        if slots["default"] and slots["default"] in fchain:
            errors.append("models.fallback_chain must not contain the default model itself")
        if slots["fallback"] and fchain[0] != slots["fallback"]:
            errors.append("models.fallback_chain must include models.fallback as its first entry")
    for _key in ("delegate_thinking_level", "delegate_fallback_thinking_level"):
        raw_level = str(slots.get(_key) or "").strip().lower()
        if raw_level and thinking_level_from_config(raw_level) is None:
            errors.append(
                f"models.{_key} '{raw_level}' invalid — "
                "expected one of: " + ", ".join(VALID_THINKING_LEVELS))
    _orch = str(slots.get("orchestrator_thinking_level") or "").strip().lower()
    if _orch and thinking_level_from_config(_orch, allow_auto=False) is None:
        errors.append(
            f"models.orchestrator_thinking_level '{_orch}' invalid — "
            "expected one of: " + ", ".join(HERMES_THINKING_LEVELS))
    chain = slots.get("delegate_fallback_chain") or []
    if chain:
        if len(set(chain)) != len(chain):
            errors.append("models.delegate_fallback_chain contains duplicates")
        if slots["delegate_model"] and slots["delegate_model"] in chain:
            errors.append("models.delegate_fallback_chain must not contain the delegate model itself")
        if slots["delegate_fallback"] and chain[0] != slots["delegate_fallback"]:
            errors.append("models.delegate_fallback_chain must include models.delegate_fallback as its first entry")
    return errors


DEFAULT_THINKING_LEVEL = "xhigh"
# Base Effort ladder (omp Effort enum): minimal..max. The CLI flag adds off +
# auto; the wizard picker offers off/minimal/low/medium/high/xhigh/max (+auto
# omp-side only), default xhigh. There is NO ultra level — the valid set tops
# at max. There is NO 512 budget in omp source (bench-only constant); any 512
# observed is provider-side. Do not chase either.
VALID_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")
# Per-engine picker vocabularies (ultra never offered; valid set tops at max).
HERMES_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
OMP_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")


def thinking_level_from_config(raw, allow_auto=True):
    """models.*_thinking_level -> validated level (default xhigh)."""
    v = str(raw or "").strip().lower()
    if not v:
        return DEFAULT_THINKING_LEVEL
    if v in VALID_THINKING_LEVELS and (allow_auto or v != "auto"):
        return v
    return None  # invalid -> caller reports


def delegate_fallback_selectors(slots):
    """Attach each fallback's configured effort to its native OMP selector."""
    chain = [m for m in (slots.get("delegate_fallback_chain") or []) if m] or [slots["delegate_fallback"]]
    overrides = slots.get("reasoning_overrides") or {}
    result = []
    for model in chain:
        level = overrides.get(model)
        if not level and model == slots["delegate_fallback"]:
            level = slots.get("delegate_fallback_thinking_level")
        result.append(f"{model}:{level}" if model and level else model)
    return result


def render(slots, delegation=False):
    if delegation:
        print(f"OMP_MODEL={slots['delegate_model']}")
        chain = delegate_fallback_selectors(slots)
        print(f"OMP_FALLBACK_CHAIN={','.join(chain)}")
        # Use the selected model's effort; old slot fields are read-only
        # compatibility for configs that have not yet run omp-sync.
        level = thinking_level_from_config(
            (slots.get("reasoning_overrides") or {}).get(slots["delegate_model"])
            or slots.get("delegate_thinking_level"))
        print(f"OMP_THINKING_LEVEL={level or DEFAULT_THINKING_LEVEL}")
        _fb_raw = str((slots.get("reasoning_overrides") or {}).get(slots["delegate_fallback"])
                      or slots.get("delegate_fallback_thinking_level") or "").strip().lower()
        _fb = thinking_level_from_config(_fb_raw) if _fb_raw else ""
        if _fb:
            print(f"OMP_FALLBACK_THINKING_LEVEL={_fb}")
    else:
        for s in SLOTS:
            print(f"{s.upper()}={slots[s] or '<unset>'}")
        fchain = [m for m in (slots.get("fallback_chain") or []) if m]
        if fchain:
            print(f"FALLBACK_CHAIN={','.join(fchain)}")


def _yaml_sq(s: str) -> str:
    """Single-quoted YAML scalar (' escaped by doubling)."""
    return "'" + s.replace("'", "''") + "'"


def _widen_fnmatch_glob(pattern: str) -> str:
    """HERMES-OMP PATCH (C2): fnmatch glob → omp '*' wildcard grammar.

    omp bash.patterns support ONLY '*' wildcards (bash.ts
    bashApprovalPatternToRegExp). fnmatch '?' (single char) and '[seq]'
    character classes have no omp equivalent; both widen to '*', which
    matches MORE commands — omp can never end up LOOSER than hermes.
    Deny matching is case-insensitive in both engines.
    """
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c in "*?":
            out.append("*")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j != -1:
                out.append("*")
                i = j + 1
            else:  # unterminated class: fnmatch treats '[' literally
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    # collapse runs of '*' — 'mkfs?[abc]' widens to 'mkfs**' ≡ 'mkfs*'
    return re.sub(r"\*{2,}", "*", "".join(out))


def _approval_config_from_yaml(text: str) -> dict:
    import yaml
    from mercury_cli.approval_policy import shared_approval_config

    config = yaml.safe_load(text) or {}
    if not isinstance(config, dict):
        raise ValueError("Mercury config must be a mapping")
    return shared_approval_config(config)


def _omp_approvals_mode(text: str) -> str:
    import yaml
    from mercury_cli.approval_policy import omp_approval_mode

    return omp_approval_mode(yaml.safe_load(text) or {})


# HERMES-OMP PATCH (tool-call inheritance, user directive): map hermes'
# configured web backend ids to omp's provider ids so omp uses the SAME
# search/scrape as the hermes harness (not whichever keyed provider it
# stumbles on first — the zai-defaulting bug).
_WEB_BACKEND_TO_OMP = {
    "exa": "exa", "firecrawl": "firecrawl", "searxng": "searxng",
    "brave": "brave", "brave_free": "brave", "ddgs": "duckduckgo",
    "keenable": "parallel", "parallel": "parallel",
    "omp": "zai",  # hermes' omp-bridge provider == omp's own zai search
    # Nous-managed gateway: omp has no native 'nous' provider, but the
    # gateway speaks the Firecrawl API shape. The delegation/passthrough
    # env bridge exports FIRECRAWL_API_URL=<gateway> + FIRECRAWL_API_KEY=
    # <nous token>, so omp's NATIVE firecrawl provider hits the gateway —
    # order pin is firecrawl.
    "nous": "firecrawl",
}


def _hermes_web_omp_provider(text: str) -> str:
    """Read hermes' effective web backend from the unified config and map it
    to omp's provider id ('' when unset/unknown). Priority mirrors the
    hermes side: web.search_backend > web.backend."""
    lines = [l.split("#", 1)[0].rstrip() for l in text.splitlines()]
    backend = search_backend = ""
    in_web = in_hermes = False
    for l in lines:
        if not l.strip():
            continue
        indented = l[0].isspace()
        s = l.strip()
        if not indented:
            in_web = s == "web:"
            in_hermes = s == "hermes:"
            continue
        if in_hermes and s == "web:":
            in_web = True
            continue
        if in_web and ":" in s:
            k, _, v = s.partition(":")
            k = k.strip()
            v = v.strip().strip("'\"")
            if k == "backend":
                backend = v
            elif k == "search_backend":
                search_backend = v
            elif k == "provider":
                # Nous-managed selection stores web.provider = nous
                if v == "nous":
                    backend = "nous"
    return _WEB_BACKEND_TO_OMP.get(search_backend or backend or "", "")


def _hermes_deny_globs(text: str) -> list:
    """Read the same effective deny list as the Hermes approval gate."""
    return _approval_config_from_yaml(text).get("deny", [])


# Shared mnemosyne/mnemopi bank (Mercury default memory system).
# ONE SQLite file shared by hermes + omp: ~/.mercury/memories/mnemopi.db
# (MERCURY_HOME-aware so tests stay hermetic). Omp pins it via explicit
# mnemopi.dbPath + scoping:global (no per-project sibling DBs split the
# bank). FTS-only default (noEmbeddings:true) keeps fresh installs light;
# embeddings are opt-in later via mnemopi.noEmbeddings=false.
_VALID_OMP_MEMORY_BACKENDS = {"off", "local", "hindsight", "mnemopi", "sharpshooter", "mnemosyne"}
_VALID_MNEMOPI_SCOPINGS = {"global", "per-project", "per-project-tagged"}


def _shared_mnemopi_db_path() -> str:
    home = os.environ.get("MERCURY_HOME", os.path.expanduser("~/.mercury"))
    return os.path.join(home, "memories", "mnemopi.db")


def _existing_omp_memory_backend(text: str):
    """Return the explicit omp.memory.backend value, or None when unset."""
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


def _parse_yaml_bool_or_none(value):
    if value is None:
        return None
    s = str(value).strip().strip("'\"").lower()
    if s in ("true", "yes", "on", "1"):
        return True
    if s in ("false", "no", "off", "0"):
        return False
    return None


def _existing_omp_mnemopi_values(text: str) -> dict:
    """Return explicitly set mnemopi keys from the existing omp: subtree."""
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

def render_omp_subtree(slots, target=None):
    """Write omp settings into the unified file's omp: subtree, preserving
    models: and hermes: subtrees. No modelRoles (dead feature).

    HERMES-OMP PATCH (C2): hermes approvals.deny globs are translated to
    omp bash.patterns deny rules — privilege inheritance at spawn time.
    omp's schema default tools.approvalMode is yolo, so WITHOUT this a
    delegated child would run with none of the user's deny rules. A
    bash.patterns deny is a tool-declared override that resolveApproval
    honors BEFORE mode logic — absolute even under yolo, per shell
    segment (mirrors hermes: user deny fires before the yolo bypass).
    """
    target = target or CONFIG
    # read existing whole-file structure (minimal: split top-level blocks)
    text = open(target).read() if os.path.exists(target) else ""
    deny_globs = sorted({_widen_fnmatch_glob(g) for g in _hermes_deny_globs(text)})
    # Preserve the native OMP mode. Hermes smart review is a separate option.
    omp_mode = _omp_approvals_mode(text)
    omp_block = (
        "omp:\n"
        f"  # setupVersion {CURRENT_SETUP_VERSION}: stamped by the Mercury wizard/omp-sync —\n"
        "  # omp's interactive onboarding never fires for installer-configured\n"
        "  # installs (user directive: the installer did the setup).\n"
        f"  setupVersion: {CURRENT_SETUP_VERSION}\n"
        "  tools:\n"
        f'    approvalMode: "{omp_mode}"\n'
        "  retry:\n"
        "    modelFallback: true\n"
        # omp's schema key is retry.fallbackChains (plural): a RECORD mapping
        # a model selector ("provider/model-id") to an ordered fallback list.
        # "default" would be a role key — roles are dead in Mercury, so key by
        # the delegate model's full selector: the chain applies whenever THAT
        # model is active (exactly delegation time).
        # Single-line flow mapping: omp's YAML parser is strict YAML 1.2 and
        # REJECTS trailing commas in block-style flow maps — verified live
        # (Settings config is invalid ... YAML Parse error: Unexpected token).
        # HERMES-OMP PATCH (ordered delegate fallback): the user-configured
        # chain (models.delegate_fallback_chain) is rendered in ORDER; the
        # single legacy slot remains the default when no chain is set.
        f'    fallbackChains: {{"{slots["delegate_model"]}": {json.dumps(delegate_fallback_selectors(slots))}}}\n'
    )
    # Project the selected model's effort into OMP's native startup setting.
    # "off" is passed explicitly by the launcher, not stored in this enum.
    _think = thinking_level_from_config(
        (slots.get("reasoning_overrides") or {}).get(slots["delegate_model"])
        or slots.get("delegate_thinking_level"))
    if _think and _think != "off":
        omp_block += f"  defaultThinkingLevel: {_think}\n"
    import yaml
    whole = yaml.safe_load(text) or {}
    compression = (whole.get("hermes") or {}).get("compression") or {}
    threshold = compression.get("threshold", 0.50)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold < 1:
        raise ValueError("hermes.compression.threshold must be between 0 and 1")
    enabled = compression.get("enabled", True)
    compaction = dict((whole.get("omp") or {}).get("compaction") or {})
    compaction.update(enabled=bool(enabled), thresholdPercent=threshold * 100,
                      thresholdTokens=compression.get("threshold_tokens") or -1)
    omp_block += f"  compaction: {json.dumps(compaction)}\n"
    omp_block += f"  modelContextWindows: {json.dumps(slots.get('context_windows') or {})}\n"
    omp_provider = _hermes_web_omp_provider(text)
    if omp_provider:
        # PIN the inherited provider first; omp appends its remaining chain.
        omp_block += (
            "  providers:\n"
            f'    webSearchOrder: ["{omp_provider}"]\n'
        )
    # Shared mnemosyne/mnemopi bank: default-on for fresh installs, never
    # clobber an explicit user-set memory.backend. Fresh installs get
    # memory.backend=mnemopi + mnemopi {dbPath bank scoping:global
    # autoRecall/autoRetain/noEmbeddings} (FTS-only, no embedding deps).
    # Scoping global keeps ONE bank file (no per-project sibling DBs).
    _old_backend = _existing_omp_memory_backend(text)
    _new_backend = _old_backend if _old_backend is not None else "mnemopi"
    if _new_backend == "mnemosyne":
        _new_backend = "mnemopi"
    if _new_backend == "mnemopi":
        _old_mn = _existing_omp_mnemopi_values(text)
        _db_path = (_old_mn.get("dbPath") or "").strip().strip("'\"") or _shared_mnemopi_db_path()
        _bank = (_old_mn.get("bank") or "").strip().strip("'\"") or "default"
        _scoping = (_old_mn.get("scoping") or "").strip().strip("'\"")
        if _scoping not in _VALID_MNEMOPI_SCOPINGS:
            _scoping = "global"
        _auto_recall = _parse_yaml_bool_or_none(_old_mn.get("autoRecall"))
        if _auto_recall is None:
            _auto_recall = True
        _auto_retain = _parse_yaml_bool_or_none(_old_mn.get("autoRetain"))
        if _auto_retain is None:
            _auto_retain = True
        _no_emb = _parse_yaml_bool_or_none(_old_mn.get("noEmbeddings"))
        if _no_emb is None:
            _no_emb = True
        omp_block += (
            "  memory:\n"
            "    backend: mnemopi\n"
            "  mnemopi:\n"
            f"    dbPath: {_yaml_sq(_db_path)}\n"
            f"    bank: {_yaml_sq(_bank)}\n"
            f"    scoping: {_scoping}\n"
            f"    autoRecall: {str(_auto_recall).lower()}\n"
            f"    autoRetain: {str(_auto_retain).lower()}\n"
            f"    noEmbeddings: {str(_no_emb).lower()}\n"
        )
    elif _old_backend in _VALID_OMP_MEMORY_BACKENDS:
        omp_block += f"  memory:\n    backend: {_old_backend}\n"
    if deny_globs:
        omp_block += "  bash:\n    patterns:\n"
        for g in deny_globs:
            omp_block += f"    - {{match: {_yaml_sq(g)}, approval: deny}}\n"
    # Preserve independent OMP settings instead of replacing the entire subtree.
    import yaml
    whole_config = yaml.safe_load(text) or {}
    original = whole_config.get("omp") or {}
    if text.lstrip().startswith("{"):
        text = yaml.safe_dump({key: value for key, value in whole_config.items() if key != "omp"}, sort_keys=False)
    generated = yaml.safe_load(omp_block)["omp"]
    if (original.get("memory") or {}).get("backend") is False:
        original["memory"]["backend"] = "off"
    if (generated.get("memory") or {}).get("backend") is False:
        generated["memory"]["backend"] = "off"

    def merge_settings(old, new):
        merged = dict(old)
        for key, value in new.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = merge_settings(merged[key], value)
            else:
                merged[key] = value
        return merged

    if not isinstance(original, dict):
        raise ValueError("omp settings must be a mapping")
    existing_patterns = (original.get("bash") or {}).get("patterns") or []
    inherited_marker = re.search(r"^  # Mercury inherited deny patterns: (.*)$", text, re.M)
    inherited_before = json.loads(inherited_marker.group(1)) if inherited_marker else []
    user_patterns = [pattern for pattern in existing_patterns if pattern not in inherited_before]
    inherited_now = [{"match": glob, "approval": "deny"} for glob in deny_globs]
    merged = merge_settings(original, generated)
    # Retry chains are a projection of shared models, never an accumulating cache.
    merged["retry"]["fallbackChains"] = generated["retry"]["fallbackChains"]
    if "modelFallback" in (original.get("retry") or {}):
        merged["retry"]["modelFallback"] = original["retry"]["modelFallback"]
    if user_patterns or inherited_now or existing_patterns:
        merged.setdefault("bash", {})["patterns"] = user_patterns + inherited_now
    omp_block = "omp:\n" + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
    omp_block = "omp:\n" + "".join("  " + line + "\n" for line in omp_block.splitlines()[1:])
    omp_block = omp_block.replace("omp:\n", "omp:\n  # Mercury inherited deny patterns: " + json.dumps(inherited_now) + "\n", 1)
    if re.search(r"^omp:", text, re.M):
        text = re.sub(r"^omp:(.*?)(?=^\S|\Z)", omp_block, text, count=1, flags=re.M | re.S)
    else:
        text = text.rstrip("\n") + "\n\n" + omp_block
    from mercury_cli.model_settings import canonical_model_document
    from utils import atomic_write_text
    document = canonical_model_document(yaml.safe_load(text) or {})
    from pathlib import Path
    from mercury_cli.profile_defaults import inherits
    if inherits(Path(target).parent, "models", whole_config):
        # Generated OMP projections must not turn live inherited defaults
        # into a permanent local model selection when an agent is spawned.
        document["models"] = canonical_model_document(whole_config)["models"]
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    text = text.replace("omp:\n", "omp:\n  # Mercury inherited deny patterns: " + json.dumps(inherited_now) + "\n", 1)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    atomic_write_text(target, text, preserve_mode=True)
    return target


def _refresh_omp_skills_union():
    """MERCURY-OMP PATCH (skills bridge union): refresh omp's engine-root
    symlink view of the shared library AND the hermes engine tree before
    this spawn.

    --render-omp runs before EVERY omp spawn (delegate RPC children via
    omp_delegation._render_omp_config_once, the /omp command, cron
    omp_direct, post-setup omp_sync), so hanging the skills union off it
    makes a long-running gateway converge without a launcher reboot —
    skills installed or removed mid-flight reach the next child. The
    launcher hook (bin/mercury) and install.sh stay as boot/install-time
    belts. Best-effort by contract: a config render must NEVER fail here;
    outside a mercury tree (neither bridged skills dir exists) it is a no-op.
    """
    try:
        sys.path.insert(0, os.path.join(REPO, "hermes"))
        from tools.omp_skills_bridge import (
            reconcile_omp_skills,
            resolve_hermes_engine_skills_dir,
            resolve_mercury_skills_dir,
        )
        if not (resolve_mercury_skills_dir().is_dir()
                or resolve_hermes_engine_skills_dir().is_dir()):
            return
        summary = reconcile_omp_skills()
        failed = summary.get("failed") or []
        if failed:
            print(
                f"omp skills bridge: {len(failed)} failure(s) — run 'mercury omp-sync-skills'",
                file=sys.stderr,
            )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"omp skills bridge skipped: {exc}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    delegation = "--delegate" in args
    check_only = "--check" in args
    render_omp = "--render-omp" in args
    slots = parse_config()
    errors = validate(slots, need_delegate=(delegation or render_omp))
    if errors:
        for e in errors:
            print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    if render_omp:
        target = render_omp_subtree(slots)
        print(f"rendered omp: subtree in {target}")
        _refresh_omp_skills_union()
        return
    if not check_only:
        render(slots, delegation=delegation)


if __name__ == "__main__":
    main()
