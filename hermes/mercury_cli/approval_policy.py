"""Shared Mercury approval configuration for both execution engines."""


def normalize_approval_mode(mode) -> str:
    """Return the Hermes mode for either engine's public permission names."""
    if isinstance(mode, bool):
        return "off" if mode is False else "manual"
    if isinstance(mode, str):
        value = mode.strip().lower()
        value = {"safe": "manual", "yolo": "off"}.get(value, value)
        if value in ("manual", "smart", "off"):
            return value
    return "manual"


def shared_approval_config(config: dict) -> dict:
    """Top-level policy wins; explicit deny rules from either location are retained."""
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
