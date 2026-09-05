#!/usr/bin/env python3
"""Mercury bridge v3 — unified config (~/.mercury/config.yaml), one file.

Four model slots (top level): default/fallback/delegate_model/
delegate_fallback. Fail-hard semantics unchanged. New default config path:
~/.mercury/config.yaml (MERCURY_CONFIG env or HERMES_OMP_CONFIG override).

--render-omp now writes INTO the unified file's omp: subtree (preserving
models:/hermes:), instead of a separate ~/.omp/agent/config.yml.
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MERCURY_HOME = os.environ.get("MERCURY_HOME", os.path.expanduser("~/.mercury"))
DEFAULT_CONFIG = os.path.join(MERCURY_HOME, "config.yaml")
CONFIG = os.environ.get("HERMES_OMP_CONFIG", os.environ.get("MERCURY_CONFIG", DEFAULT_CONFIG))

SLOTS = ("default", "fallback", "delegate_model", "delegate_fallback")


def parse_config(path=None):
    """Return {slot: value} from the models: block of the unified file."""
    path = path or CONFIG
    slots = {s: "" for s in SLOTS}
    in_models = False
    try:
        raw = open(path).read()
    except FileNotFoundError:
        sys.exit(f"FATAL: {path} not found")
    chain: list[str] | None = None  # None until the key appears (block-seq guard)
    fchain: list[str] = []
    for line in raw.splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        if s == "models:":
            in_models = True
            continue
        if in_models:
            if ":" in s and not s.startswith("-"):
                k, _, v = s.partition(":")
                k = k.strip()
                if k in slots:
                    slots[k] = v.strip().strip("'\"")
                elif k == "fallback_chain":
                    raw_list = v.strip()
                    fchain = []
                    if raw_list.startswith("["):
                        inner = raw_list[1:-1] if raw_list.endswith("]") else raw_list[1:]
                        for item in inner.split(","):
                            item = item.strip().strip("'\"")
                            if item:
                                fchain.append(item)
                elif k == "delegate_fallback_chain":
                    # ordered fallback list — JSON array or YAML flow
                    # sequence (single OR double quoted items both legal)
                    raw_list = v.strip()
                    chain = []
                    if raw_list.startswith("["):
                        inner = raw_list[1:-1] if raw_list.endswith("]") else raw_list[1:]
                        for item in inner.split(","):
                            item = item.strip().strip("'\"")
                            if item:
                                chain.append(item)
            elif s.startswith("-") and chain is not None:
                # block-sequence continuation
                item = s[1:].strip().strip("'\"")
                if item:
                    chain.append(item)
            elif not s.startswith((" ", "\t")):
                in_models = False
    slots["delegate_fallback_chain"] = [m for m in (chain or []) if m]
    slots["fallback_chain"] = fchain
    return slots


def validate(slots, need_delegate=False):
    errors = []
    if not slots["default"]:
        errors.append("models.default is empty — set the main session model")
    if not slots["fallback"]:
        errors.append("models.fallback is empty — REQUIRED; refusing to run without an explicit fallback")
    if need_delegate:
        if not slots["delegate_model"]:
            errors.append("models.delegate_model is empty — required for delegation")
        if not slots["delegate_fallback"]:
            errors.append("models.delegate_fallback is empty — REQUIRED for delegation")
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
    chain = slots.get("delegate_fallback_chain") or []
    if len(set(chain)) != len(chain):
        errors.append("models.delegate_fallback_chain contains duplicates")
    if slots["delegate_model"] and slots["delegate_model"] in chain:
        errors.append("models.delegate_fallback_chain must not contain the delegate model itself")
    return errors


def render(slots, delegation=False):
    if delegation:
        print(f"OMP_MODEL={slots['delegate_model']}")
        chain = [m for m in (slots.get("delegate_fallback_chain") or [])
                 if m] or [slots["delegate_fallback"]]
        print(f"OMP_FALLBACK_CHAIN={','.join(chain)}")
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
    Known delta (documented): omp pattern matching is case-sensitive;
    hermes lowercases both sides. omp's own CRITICAL_BASH_PATTERNS are
    case-insensitive, so the catastrophic floor stays covered either way.
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


def _unified_approvals_mode(text: str) -> str:
    """HERMES-OMP PATCH (unified approvals): the ONE mode knob.

    Read top-level ``approvals:`` → ``mode:`` from the unified file (the
    same block shape hermes reads). Falls back to the hermes: subtree's
    approvals.mode, then to "smart" (safe default — read+write approved,
    exec prompts). Values: manual | smart | off (off = yolo).
    """
    lines = [l.split("#", 1)[0].rstrip() for l in text.splitlines()]

    def _mode_under(header: str) -> str | None:
        # header may BE the approvals: block (top-level knob) or a parent
        # (hermes:) containing an approvals: child. Handle both: track the
        # indent of the approvals: key itself, then read mode: one level in.
        try:
            h = next(i for i, l in enumerate(lines) if l.strip() == header)
        except StopIteration:
            return None
        base_indent = len(lines[h]) - len(lines[h].lstrip())
        ap_indent = base_indent if lines[h].strip() == "approvals:" else None
        for l in lines[h + 1:]:
            if l.strip() and (len(l) - len(l.lstrip())) <= base_indent:
                break
            if not l.strip():
                continue
            stripped = l.strip()
            indent = len(l) - len(l.lstrip())
            if ap_indent is None:
                if stripped == "approvals:":
                    ap_indent = indent
                    continue
                continue
            if indent == ap_indent + 2 and stripped.startswith("mode:"):
                v = stripped.split(":", 1)[1].strip().strip("'\"")
                if v == "False":
                    return "off"
                if v in ("manual", "smart", "off"):
                    return v
        return None

    return _mode_under("approvals:") or _mode_under("hermes:") or "smart"


def _hermes_deny_globs(text: str) -> list:
    """HERMES-OMP PATCH (C2): approvals.deny globs from the hermes: subtree.

    The unified file's hermes: subtree IS hermes' live config (via
    MERCURY_CONFIG), so this reads the same source of truth hermes'
    approval gate enforces — no second policy list can drift. Allowlist
    is deliberately NOT translated: an omp-side allow can only loosen.
    """
    lines = [l.split("#", 1)[0].rstrip() for l in text.splitlines()]
    try:
        h = next(i for i, l in enumerate(lines) if l == "hermes:")
    except StopIteration:
        return []
    globs = []
    ap_indent = None
    deny_indent = None
    for l in lines[h + 1:]:
        if l.strip() and not l[0].isspace():
            break  # left the hermes: block
        if not l.strip():
            continue
        stripped = l.strip()
        indent = len(l) - len(l.lstrip())
        if deny_indent is not None:
            if stripped.startswith("- ") and indent > deny_indent:
                item = stripped[2:].strip().strip("'\"")
                if item:
                    globs.append(item)
                continue
            deny_indent = None  # list ended; fall through to key scan
        if stripped == "approvals:":
            ap_indent = indent
        elif stripped.endswith(":") and ap_indent is not None and indent <= ap_indent:
            ap_indent = None  # sibling key: approvals: block closed
            if stripped == "deny:":
                pass  # not under approvals; ignore
        elif stripped == "deny:" and ap_indent is not None and indent > ap_indent:
            deny_indent = indent
    return globs


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
    # HERMES-OMP PATCH (unified approvals): ONE mode knob for both engines.
    # Unified approvals.mode (top level, alongside models:) maps to omp's
    # tools.approvalMode: manual->always-ask, smart->write, off->yolo.
    # Safe default: write (read+workspace-write auto-approved, exec prompts).
    mode_map = {"manual": "always-ask", "smart": "write", "off": "yolo"}
    unified_mode = _unified_approvals_mode(text)
    omp_mode = mode_map.get(unified_mode, "write")
    omp_block = (
        "omp:\n"
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
        f'    fallbackChains: {{"{slots["delegate_model"]}": {json.dumps([m for m in (slots.get("delegate_fallback_chain") or []) if m] or [slots["delegate_fallback"]])}}}\n'
    )
    if deny_globs:
        omp_block += "  bash:\n    patterns:\n"
        for g in deny_globs:
            omp_block += f"    - {{match: {_yaml_sq(g)}, approval: deny}}\n"
    if re.search(r"^omp:", text, re.M):
        text = re.sub(r"^omp:(.*?)(?=^\S|\Z)", omp_block, text, count=1, flags=re.M | re.S)
    else:
        text = text.rstrip("\n") + "\n\n" + omp_block
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(text)
    return target


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
        return
    if not check_only:
        render(slots, delegation=delegation)


if __name__ == "__main__":
    main()
