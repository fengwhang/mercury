"""Engine-specific Mercury approval modes and shared explicit deny rules."""

OMP_APPROVAL_MODES = ("always-ask", "write", "yolo")


def omp_approval_mode(config: dict) -> str:
    """Read OMP's native tier mode independently of Hermes's risk reviewer."""
    omp = config.get("omp", {})
    if not isinstance(omp, dict):
        raise ValueError("omp settings must be a mapping")
    tools = omp.get("tools", {})
    if not isinstance(tools, dict):
        raise ValueError("omp.tools must be a mapping")
    mode = tools.get("approvalMode", "yolo")
    if mode not in OMP_APPROVAL_MODES:
        raise ValueError("omp.tools.approvalMode must be always-ask, write, or yolo")
    return mode


def normalize_approval_mode(mode) -> str:
    """Normalize Hermes's public safe/smart/yolo names and legacy values."""
    if isinstance(mode, bool):
        return "off" if mode is False else "manual"
    if isinstance(mode, str):
        value = mode.strip().lower()
        value = {"safe": "manual", "yolo": "off"}.get(value, value)
        if value in ("manual", "smart", "off"):
            return value
    return "manual"


def shared_approval_config(config: dict) -> dict:
    """Resolve Hermes policy; top-level values win and deny rules are retained."""
    subtree = config.get("hermes") or {}
    legacy = (subtree.get("approvals") or {}) if isinstance(subtree, dict) else {}
    shared = config.get("approvals") or {}
    if not isinstance(legacy, dict) or not isinstance(shared, dict):
        raise ValueError("approvals must be a mapping")
    result = {**legacy, **shared}
    deny = []
    for policy in (legacy, shared):
        rules = policy.get("deny") or []
        if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
            raise ValueError("approvals.deny must be a list of command patterns")
        for rule in rules:
            if rule and rule not in deny:
                deny.append(rule)
    if deny:
        result["deny"] = deny
    return result
