"""Usage anchoring (parity port of stock ``agent/usage_anchor.py``).

An anchor = provider usage at capture + a snapshot of the transcript position it priced.
Minimal closure for the ported ``turn_*`` modules (``turn_usage``, ``turn_request_assembly``,
``image_token_cost``): capture/set/persist plus the fingerprint-validated anchor math. The
fingerprint (not ``id()``) is the identity: the gateway re-reads the transcript from the DB
and a live dict's ``id()`` is not stable across that boundary. Mercury's consolidated
``agent.model_metadata`` keeps its own id-based anchor pair for its in-tree flow.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

USAGE_ANCHOR_MODEL_CONFIG_KEY = "_usage_anchor"

_FINGERPRINT_KEYS = ("role", "content", "api_content", "tool_call_id", "tool_calls")


def message_fingerprint(msg: Any) -> Optional[str]:
    """Stable digest of one transcript message over its provider-visible, persisted fields."""
    if not isinstance(msg, dict):
        return None
    payload = {k: msg.get(k) for k in _FINGERPRINT_KEYS if msg.get(k) is not None}
    try:
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = repr(sorted(payload.items()))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _priced_prefix_fingerprint(messages: List[Dict[str, Any]], base_count: int) -> Optional[str]:
    """Stable digest of the whole provider-priced prefix.

    The last priced message fingerprint proves only that one row survived at
    ``base_count - 1``. A compaction can preserve that row while rewriting the
    earlier prefix, so the anchor must also bind to the full priced prefix it
    represents.
    """
    if base_count <= 0 or len(messages) < base_count:
        return None
    fps = []
    for msg in messages[:base_count]:
        fp = message_fingerprint(msg)
        if not fp:
            return None
        role = msg.get("role") if isinstance(msg, dict) else None
        fps.append((role, fp))
    raw = json.dumps(fps, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def capture_usage_anchor(prompt_tokens: Any, completion_tokens: Any, messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build a usage anchor from provider-reported usage, or None when usage is unusable."""
    try:
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
    except (TypeError, ValueError):
        return None
    if pt <= 0 or not isinstance(messages, list) or not messages:
        return None  # some endpoints omit usage — caller keeps its anchor
    last = messages[-1]
    return {
        "prompt_tokens": pt,
        "completion_tokens": max(0, ct),
        "base_count": len(messages),
        "base_last_role": last.get("role") if isinstance(last, dict) else None,
        "base_last_fp": message_fingerprint(last),
        "base_prefix_fp": _priced_prefix_fingerprint(messages, len(messages)),
    }


def _anchor_matches(messages: List[Dict[str, Any]], anchor: Dict[str, Any]) -> bool:
    try:
        base_count = int(anchor.get("base_count") or 0)
    except (TypeError, ValueError):
        return False
    if base_count <= 0 or len(messages) < base_count:
        return False
    base_msg = messages[base_count - 1]
    if not isinstance(base_msg, dict) or base_msg.get("role") != anchor.get("base_last_role"):
        return False
    fp = anchor.get("base_last_fp")
    if not isinstance(fp, str) or not fp or message_fingerprint(base_msg) != fp:
        return False
    prefix_fp = anchor.get("base_prefix_fp")
    return (
        isinstance(prefix_fp, str)
        and bool(prefix_fp)
        and _priced_prefix_fingerprint(messages, base_count) == prefix_fp
    )


def anchored_context_tokens(messages: List[Dict[str, Any]], anchor: Optional[Dict[str, Any]], *, charge_stale_thinking: bool = True) -> Optional[int]:
    """Anchored prompt+completion tokens plus a rough estimate of ONLY the messages appended since;
    None when the anchor is missing or stale. The anchored response's own reply is skipped (already
    in completion_tokens). ``charge_stale_thinking`` is forwarded to the delta estimate."""
    if not isinstance(anchor, dict) or not isinstance(messages, list) or not _anchor_matches(messages, anchor):
        return None
    from agent.model_metadata import estimate_messages_tokens_rough

    total = int(anchor["prompt_tokens"]) + int(anchor.get("completion_tokens") or 0)
    delta = messages[int(anchor["base_count"]):]
    if delta and isinstance(delta[0], dict) and delta[0].get("role") == "assistant":
        delta = delta[1:]
    if delta:
        total += estimate_messages_tokens_rough(delta, charge_stale_thinking=charge_stale_thinking)
    return total


def _serialize(anchor: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(anchor, dict):
        return None
    try:
        pt, ct, base_count = (int(anchor.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "base_count"))
    except (TypeError, ValueError):
        return None
    fp, role = anchor.get("base_last_fp"), anchor.get("base_last_role")
    prefix_fp = anchor.get("base_prefix_fp")
    if (
        pt <= 0
        or base_count <= 0
        or not isinstance(fp, str)
        or not fp
        or not isinstance(prefix_fp, str)
        or not prefix_fp
    ):
        return None
    return {"prompt_tokens": pt, "completion_tokens": max(0, ct), "base_count": base_count,
            "base_last_role": role if isinstance(role, str) else None, "base_last_fp": fp,
            "base_prefix_fp": prefix_fp}


def persist_usage_anchor(agent: Any, anchor: Optional[Dict[str, Any]]) -> None:
    """Write (or clear, ``None``) the session row's anchor blob. Best-effort: the row may not exist yet."""
    if getattr(agent, "_persist_disabled", False):
        return
    session_id = getattr(agent, "session_id", None)
    patcher = getattr(getattr(agent, "_session_db", None), "patch_session_model_config", None)
    if not session_id or not callable(patcher):
        return
    try:
        patcher(session_id, {USAGE_ANCHOR_MODEL_CONFIG_KEY: _serialize(anchor)})
    except Exception:
        logger.debug("usage anchor persist failed", exc_info=True)


def set_usage_anchor(agent: Any, anchor: Optional[Dict[str, Any]], *, turn_base: bool = False) -> None:
    """Install ``anchor`` on the agent (``None`` clears) and mirror it to the session row."""
    agent._usage_anchor = anchor
    if turn_base or anchor is None:
        agent._turn_base_usage_anchor = anchor
    persist_usage_anchor(agent, anchor)
