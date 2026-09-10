#!/usr/bin/env python3
"""Delegate Tool -- omp dispatch + live control plane.

Model-facing ``delegate_task`` spawns run on ONE engine: the patched omp
build (``tools/omp_delegation.dispatch_omp_delegation``), reached live via
``run_agent._dispatch_delegate_task`` and via the registry fallback below.
This module owns:

  - the tool schema + registry registration (spawn shape + the
    list/steer/stop control actions),
  - the synchronous control plane (``_handle_control_action``) over the
    live registries (the hermes-side registry, empty now that nothing
    spawns here, merged with the omp engine's live children),
  - the operator pause kill-switch (``set_spawn_paused`` /
    ``is_spawn_paused``),
  - spawn-input validation (``_recover_tasks_from_json_string``,
    ``_validate_batch_tasks``, ``normalize_delegation_names``,
    output_schema coercion),
  - shared config readers (``_load_config``,
    ``_get_max_concurrent_children``, ``_get_max_async_children``,
    ``_get_worktree_isolation``, ``_resolve_workspace_hint``),
  - failure-line / attribution helpers consumed by the gateway and the
    process registry.

The hermes-side child-agent engine (``_build_child_agent``,
``_run_single_child`` and friends, depth/role/orchestrator machinery) was
removed: DEAD DEPTH. Removed names survive below as thin shims that raise,
so stale imports fail loudly instead of silently forking a second engine.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from agent.interrupt_compat import request_hard_interrupt



DELEGATE_BLOCKED_TOOLS = frozenset(
    [
        "delegate_task",  # no recursive delegation
        "clarify",  # no user interaction
        "memory",  # no writes to shared MEMORY.md
        "send_message",  # no cross-platform side effects
        "cronjob",  # no scheduling more work in the parent's name
    ]
)


_DEFAULT_MAX_CONCURRENT_CHILDREN = 10
# One-shot guard: the high-concurrency cost advisory is emitted at most once
# per process. _get_max_concurrent_children() runs on every get_definitions()
# schema rebuild (via _build_top_level_description / _build_tasks_param_description),
# so without this flag a config of max_concurrent_children>10 spams the log on
# every turn / agent spawn even when delegate_task is never called.
_HIGH_CONCURRENCY_WARNED = False


# ---------------------------------------------------------------------------
# Runtime state: pause flag + active subagent registry
#
# Consumed by the TUI observability layer (overlay/control surface) and the
# gateway RPCs `delegation.pause`, `delegation.status`, `subagent.interrupt`.
# Kept module-level so they span every delegate_task invocation in the
# process, including nested orchestrator -> worker chains.
# ---------------------------------------------------------------------------

_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False

_active_subagents_lock = threading.Lock()
# subagent_id -> mutable record tracking the live child agent.  Stays only
# for the lifetime of the run; _run_single_child is the owner.
_active_subagents: Dict[str, Dict[str, Any]] = {}

# subagent_id -> {goal, delegation_id, parent_session_id} retained AFTER the
# child finishes (bounded FIFO). Child-started background processes routinely
# outlive the child itself (its npm ci with notify_on_complete=true finishes
# after the child's summary was delivered); their completion notifications
# reach the parent conversation via the shared completion_queue and need
# delegation attribution even though the live registry entry is gone.
_RECENT_SUBAGENTS_CAP = 200
_recent_subagents: Dict[str, Dict[str, Any]] = {}


# Terminal child statuses that mean "the subagent did NOT deliver a usable
# result". Shared by the CLI spinner echo, the gateway failure notice, and
# the parent-facing failure summary so every surface agrees on what counts
# as a failure.
SUBAGENT_FAILURE_STATUSES = frozenset({"failed", "error", "timeout"})


def _clean_error_text(error: Any, max_chars: int = 200) -> str:
    """Reduce an arbitrary error payload to one clean human-readable line.

    Provider/SDK errors routinely arrive as multi-line tracebacks or JSON
    walls. For a chat-facing notice we want the single most informative
    line: the exception message (last line of a traceback) or the first
    non-empty line otherwise, hard-capped in length.
    """
    text = str(error or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    # A traceback's last line is the actual exception message.
    line = lines[-1] if lines[0].startswith("Traceback") else lines[0]
    if len(line) > max_chars:
        line = line[: max_chars - 3] + "..."
    return line


def format_subagent_failure_line(
    goal: Optional[str],
    status: Optional[str],
    error: Any = None,
    duration_seconds: Any = None,
) -> str:
    """One clean, human-readable line describing a failed subagent.

    Rendered directly to the user (CLI spinner echo, gateway platform
    notice) — no JSON, no traceback, no internal field names. Example:

        ⚠️ Subagent failed — "research competitor pricing": Error code: 404 —
        model not found (after 12s)
    """
    goal_label = (goal or "").strip().replace("\n", " ")
    if len(goal_label) > 60:
        goal_label = goal_label[:57] + "..."
    verb = "timed out" if status == "timeout" else "failed"
    line = f"⚠️ Subagent {verb}"
    if goal_label:
        line += f' — "{goal_label}"'
    err = _clean_error_text(error)
    if err:
        line += f": {err}"
    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        line += f" (after {round(duration_seconds)}s)"
    return line


def get_subagent_attribution(task_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve a process task_id to its originating delegation, if any.

    Children run their terminal sessions under ``task_id == subagent_id``
    (see _run_single_child's child_task_id), so a background process spawned
    by a subagent carries that id in ``ProcessSession.task_id``. Returns
    ``{subagent_id, goal, delegation_id}`` for live AND recently-finished
    children, or None when the task_id is not a known subagent.
    """
    if not task_id or not isinstance(task_id, str):
        return None
    with _active_subagents_lock:
        record = _active_subagents.get(task_id)
        if record is not None:
            return {
                "subagent_id": task_id,
                "goal": record.get("goal"),
                "delegation_id": record.get("delegation_id"),
            }
        retained = _recent_subagents.get(task_id)
        if retained is not None:
            return {
                "subagent_id": task_id,
                "goal": retained.get("goal"),
                "delegation_id": retained.get("delegation_id"),
            }
    return None


def set_spawn_paused(paused: bool) -> bool:
    """Globally block/unblock new delegate_task spawns.

    Active children keep running; only NEW calls to delegate_task fail fast
    with a "spawning paused" error until unblocked.  Returns the new state.
    """
    global _spawn_paused
    with _spawn_pause_lock:
        _spawn_paused = bool(paused)
        return _spawn_paused


def is_spawn_paused() -> bool:
    with _spawn_pause_lock:
        return _spawn_paused


def _register_subagent(record: Dict[str, Any]) -> None:
    sid = record.get("subagent_id")
    if not sid:
        return
    record.setdefault("accepting_steer", True)
    with _active_subagents_lock:
        _active_subagents[sid] = record


def _retain_recent_subagent(record: Dict[str, Any]) -> None:
    """Keep a bounded attribution stub after a child finishes (lock held)."""
    sid = record.get("subagent_id")
    if not sid:
        return
    _recent_subagents[sid] = {
        "goal": record.get("goal"),
        "delegation_id": record.get("delegation_id"),
        "owner_agent_session_id": record.get("owner_agent_session_id"),
    }
    while len(_recent_subagents) > _RECENT_SUBAGENTS_CAP:
        _recent_subagents.pop(next(iter(_recent_subagents)), None)


def _unregister_subagent(subagent_id: str, *, agent: Any = None) -> None:
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if record is not None and (agent is None or record.get("agent") is agent):
            _active_subagents.pop(subagent_id, None)
            _retain_recent_subagent(record)


def _close_subagent_steering(subagent_id: str, agent: Any) -> Optional[str]:
    """Atomically close steer acceptance and drain its final durable artifact.

    ``steer_subagent`` holds the same registry lock through ``agent.steer``.
    Therefore either acceptance wins and this drain sees its exact text, or
    closure wins and the caller is rejected. Exact agent identity prevents a
    finishing child with a recycled public id from closing its replacement.
    """
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if record is None or record.get("agent") is not agent:
            return None
        record["accepting_steer"] = False
        drain = getattr(agent, "_drain_pending_steer", None)
        if not callable(drain):
            return None
        try:
            pending = drain()
        except Exception as exc:
            logger.debug("final steer drain for %s failed: %s", subagent_id, exc)
            return None
        return pending if isinstance(pending, str) and pending.strip() else None


def interrupt_subagent(subagent_id: str) -> bool:
    """Request that a single running subagent stop at its next iteration boundary.

    Does not hard-kill the worker thread (Python can't); sets the child's
    interrupt flag which propagates to in-flight tools and recurses into
    grandchildren via AIAgent.interrupt().  Returns True if a matching
    subagent was found.
    """
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
    if not record:
        return False
    agent = record.get("agent")
    if agent is None:
        return False
    try:
        if not request_hard_interrupt(agent, f"Interrupted via TUI ({subagent_id})"):
            return False
    except Exception as exc:
        logger.debug("interrupt_subagent(%s) failed: %s", subagent_id, exc)
        return False
    return True


def steer_subagent(
    subagent_id: str,
    text: str,
    *,
    owner_session_id: Optional[str] = None,
    owner_transport: Any = None,
    owner_session_record: Any = None,
) -> bool:
    """Queue steering text into a single running subagent without stopping it.

    The redirection-side mirror of interrupt_subagent(): resolves the live
    child in the registry and calls AIAgent.steer(), which appends the text
    to the child's last tool result at its next iteration boundary — the
    current tool call is never cut. Returns True if a matching subagent
    QUEUED the text while the child was still accepting work; False for an
    unknown/closed id, an ownership mismatch, a record with no live agent, or
    empty text. ``owner_session_id=None`` deliberately preserves the internal
    in-process helper contract; gateway callers must pass exact authority.

    Acceptance and completion are linearized by the registry lock. If
    acceptance wins but no delivery boundary remains, ``_run_single_child``
    drains the exact text into the completion entry as ``missed_steer``.
    """
    if not text or not text.strip():
        return False
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if not record or not record.get("accepting_steer", False):
            return False
        if owner_session_id is not None:
            if (
                record.get("owner_session_id") != owner_session_id
                or owner_transport is None
                or record.get("owner_transport") is not owner_transport
                or owner_session_record is None
                or record.get("owner_session_record") is not owner_session_record
            ):
                return False
        agent = record.get("agent")
        if agent is None:
            return False
        try:
            return bool(agent.steer(text))
        except Exception as exc:
            logger.debug("steer_subagent(%s) failed: %s", subagent_id, exc)
            return False


def _capture_gateway_steer_authority(
    owner_session_id: Optional[str],
) -> tuple[Any, Any]:
    """Capture exact request transport + live session generation, if any.

    This is intentionally an in-process bridge, not a serializable capability.
    Non-gateway hosts (including the CLI helper path) receive ``(None, None)``.
    """
    if not owner_session_id:
        return None, None
    try:
        from tui_gateway.server import _current_session_steer_authority

        return _current_session_steer_authority(owner_session_id)
    except Exception:
        return None, None


def list_active_subagents() -> List[Dict[str, Any]]:
    """Snapshot of the currently running subagent tree.

    Each record: {subagent_id, parent_id, depth, goal, model, started_at,
    tool_count, status}.  Safe to call from any thread — returns a copy.
    """
    with _active_subagents_lock:
        return [
            {
                k: v
                for k, v in r.items()
                if k
                not in {
                    "agent",
                    "owner_session_id",
                    "owner_transport",
                    "owner_session_record",
                    "accepting_steer",
                }
            }
            for r in _active_subagents.values()
        ]


def _is_descendant_of(child_agent: Any, parent_agent: Any, max_hops: int = 8) -> bool:
    """True when *child_agent* sits below *parent_agent* in the spawn tree.

    Walks the ``_delegate_parent_ref`` weakref chain stamped at build time.
    Identity comparison only — a parent may steer/stop its own children and
    grandchildren, never a sibling tree owned by another conversation.
    """
    if child_agent is None or parent_agent is None:
        return False
    cur = child_agent
    for _ in range(max_hops):
        ref = getattr(cur, "_delegate_parent_ref", None)
        ancestor = ref() if callable(ref) else None
        if ancestor is None:
            return False
        if ancestor is parent_agent:
            return True
        cur = ancestor
    return False


# Model-facing control actions accepted by delegate_task(action=...).
# "spawn" (or omitted) keeps the historical spawn semantics.
_CONTROL_ACTIONS = frozenset({"list", "steer", "stop"})


def _resolve_session_lineage(session_id: Optional[str], parent_agent: Any) -> str:
    """Resolve a session id to the tip of its compression lineage.

    Best-effort: uses the parent's live SessionDB handle when present so a
    delegation dispatched before a compression rotation still matches the
    rotated parent. Returns the input unchanged when resolution fails.
    """
    sid = str(session_id or "")
    if not sid:
        return ""
    db = getattr(parent_agent, "_session_db", None)
    if db is None:
        return sid
    try:
        resolved = db.resolve_resume_session_id(sid)
        return str(resolved) if resolved else sid
    except Exception:
        return sid


def _owns_subagent_record(record: Dict[str, Any], parent_agent: Any) -> bool:
    """True when *parent_agent*'s conversation owns this live-child record.

    Two-tier check:

    1. Object identity — the ``_delegate_parent_ref`` weakref chain stamped
       at build time reaches *parent_agent*. Fast path for the common case
       where the parent AIAgent object survives the whole run.
    2. Durable conversation lineage — the child was registered with the
       owning conversation's durable session id
       (``owner_agent_session_id``); match it against the calling parent's
       ``session_id``, resolving compression-rotation lineage on both sides.

    Tier 2 exists because the identity chain is BRITTLE across parent-agent
    rebuilds: the CLI sets ``self.agent = None`` mid-session (route-signature
    change, credential refresh, /model, MoA one-shots) and constructs a NEW
    AIAgent for the next turn while the child keeps running with a weakref to
    the old object. The delivery path always survived this (it routes by
    durable session id); the control path must use the same durable spine or
    running children go invisible/unsteerable (observed live: deleg_88454b70
    / sa-0-dc0100f4, 2026-08-17).
    """
    agent = record.get("agent")
    if _is_descendant_of(agent, parent_agent):
        return True
    owner_sid = str(record.get("owner_agent_session_id") or "")
    if not owner_sid:
        return False
    parent_sid = str(getattr(parent_agent, "session_id", "") or "")
    if not parent_sid:
        return False
    if owner_sid == parent_sid:
        return True
    # Compression rotation on either side: compare lineage tips.
    return _resolve_session_lineage(owner_sid, parent_agent) in {
        parent_sid,
        _resolve_session_lineage(parent_sid, parent_agent),
    }


def _handle_control_action(
    action: str,
    subagent_id: Optional[str],
    message: Optional[str],
    parent_agent: Any,
) -> str:
    """Synchronous control plane for delegate_task: list/steer/stop.

    Runs in-turn (never backgrounded) and only over subagents descended from
    *parent_agent* — the same registry the TUI overlay drives, but scoped so
    a conversation can only control its own spawn tree.
    """
    if action == "list":
        with _active_subagents_lock:
            records = list(_active_subagents.values())
        entries = []
        for r in records:
            agent = r.get("agent")
            if not _owns_subagent_record(r, parent_agent):
                continue
            started = r.get("started_at")
            entries.append(
                {
                    "subagent_id": r.get("subagent_id"),
                    "parent_id": r.get("parent_id"),
                    "goal": r.get("goal"),
                    "model": r.get("model"),
                    "status": r.get("status"),
                    "running_seconds": (
                        round(time.time() - started, 1)
                        if isinstance(started, (int, float))
                        else None
                    ),
                    "accepting_steer": bool(r.get("accepting_steer", False)),
                    "live_transcript": getattr(agent, "_live_transcript_path", None),
                }
            )
        # M0A (matrix observatory §8.1 item 2): Mercury's children are omp
        # children — merge the omp engine's live registry (owned only) so
        # one action='list' sees the whole spawn tree.
        try:
            from tools.omp_delegation import _live_children, _live_children_lock, _owns_live_child

            with _live_children_lock:
                omp_records = list(_live_children.values())
            for r in omp_records:
                if not _owns_live_child(r, parent_agent):
                    continue
                entries.append(
                    {
                        "subagent_id": r.get("child_id"),
                        "engine": "omp",
                        "name": r.get("name"),
                        "delegation_id": r.get("delegation_id"),
                        "goal": r.get("goal"),
                        "model": r.get("model"),
                        "transport": r.get("transport_kind"),
                        "running_seconds": round(
                            time.time() - r.get("started_at", time.time()), 1),
                        "accepting_steer": bool(r.get("steerable")),
                    }
                )
        except Exception:
            logger.debug("control list: omp registry unavailable", exc_info=True)
        payload: Dict[str, Any] = {
            "action": "list",
            "count": len(entries),
            "subagents": entries,
        }
        if not entries:
            payload["note"] = (
                "No live subagents right now. Children that already finished "
                "have delivered (or will deliver) their results as normal "
                "completion messages — there is nothing to steer or stop."
            )
        return json.dumps(payload, ensure_ascii=False)

    # steer / stop need a resolvable, owned target.
    sid = (subagent_id or "").strip()
    if not sid:
        return tool_error(
            f"action='{action}' requires subagent_id (from the spawn dispatch "
            "response or action='list')."
        )
    # M0A (§8.1 item 2): steer/stop over live omp children. The hermes-side
    # registry is empty in Mercury (children are omp processes) — fall
    # through to the omp engine's control plane on a miss.
    with _active_subagents_lock:
        record = _active_subagents.get(sid)
    if record is None:
        from tools.omp_delegation import handle_omp_control_action

        return handle_omp_control_action(action, sid, message, parent_agent)
    if not _owns_subagent_record(record, parent_agent):
        return tool_error(
            f"No live subagent '{sid}' in this conversation's spawn tree. It "
            "may have already finished (its result arrives as a normal "
            "completion message). Use action='list' to see live children."
        )

    if action == "stop":
        if interrupt_subagent(sid):
            return json.dumps(
                {
                    "action": "stop",
                    "subagent_id": sid,
                    "status": "interrupt_requested",
                    "note": (
                        "The subagent stops at its next iteration boundary "
                        "(in-flight tool calls are asked to cancel). Its "
                        "partial result still re-enters the conversation as a "
                        "completion message — do not wait or poll."
                    ),
                },
                ensure_ascii=False,
            )
        return tool_error(
            f"Could not interrupt '{sid}' — it likely finished in the last "
            "moment. Its result arrives as a normal completion message."
        )

    if action == "steer":
        text = (message or "").strip()
        if not text:
            return tool_error(
                "action='steer' requires a non-empty 'message' describing the "
                "course correction."
            )
        if steer_subagent(sid, text):
            return json.dumps(
                {
                    "action": "steer",
                    "subagent_id": sid,
                    "status": "queued",
                    "note": (
                        "Steering text queued. The subagent sees it appended "
                        "to its next tool result — the current tool call is "
                        "never cut. If the child finishes before a delivery "
                        "boundary remains, the text is reported back as "
                        "missed_steer in its completion entry."
                    ),
                },
                ensure_ascii=False,
            )
        return tool_error(
            f"Subagent '{sid}' is no longer accepting steering (finishing or "
            "already finished). Its result arrives as a normal completion "
            "message; re-delegate a follow-up task if more work is needed."
        )

    return tool_error(f"Unknown action '{action}'. Use spawn, list, steer, or stop.")


def _get_max_concurrent_children() -> int:
    """Read delegation.max_concurrent_children from config, falling back to
    DELEGATION_MAX_CONCURRENT_CHILDREN env var, then the default (10).

    Users can raise this as high as they want; only the floor (1) is enforced.

    Uses the same ``_load_config()`` path that the rest of ``delegate_task``
    uses, keeping config priority consistent (config.yaml > env > default).
    """
    cfg = _load_config()
    val = cfg.get("max_concurrent_children")
    if val is not None:
        try:
            result = max(1, int(val))
            if result > 10:
                global _HIGH_CONCURRENCY_WARNED
                if not _HIGH_CONCURRENCY_WARNED:
                    _HIGH_CONCURRENCY_WARNED = True
                    logger.warning(
                        "delegation.max_concurrent_children=%d: each child consumes API tokens "
                        "independently. High values multiply cost linearly.",
                        result,
                    )
            return result
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_concurrent_children=%r is not a valid integer; "
                "using default %d",
                val,
                _DEFAULT_MAX_CONCURRENT_CHILDREN,
            )
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    env_val = os.getenv("DELEGATION_MAX_CONCURRENT_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    return _DEFAULT_MAX_CONCURRENT_CHILDREN


def _get_worktree_isolation() -> bool:
    """Read delegation.worktree_isolation from config (bool, default False).

    Inspired by Muse Code's ``--subagent-worktree-isolation`` (Meta, Aug
    2026): when enabled, each delegated child gets its own git worktree
    checked out from the parent's current commit so parallel children never
    contend for the same working copy. Opt-in and git-only — in a non-git
    workspace or on a non-local terminal backend the flag is ignored without
    an error and children share the parent's workspace as before.
    """
    cfg = _load_config()
    return bool(cfg.get("worktree_isolation", False))


_LEGACY_MAX_ASYNC_WARNED = False


def _get_max_async_children() -> int:
    """Concurrency cap for background (``background=true``) delegations.

    DEPRECATED KNOB: ``delegation.max_async_children`` has been unified into
    ``delegation.max_concurrent_children`` — one cap governs both a single
    synchronous batch's parallelism and how many background delegation units
    may run at once. When at capacity, a new async dispatch is REJECTED (not
    queued) so a runaway model can't pile up unbounded background work; the
    caller falls back to running the work synchronously.

    A leftover ``max_async_children`` in config.yaml is ignored (the config
    migration removes it, folding a raised value into
    ``max_concurrent_children``); we log a one-time deprecation warning if
    one is still present.
    """
    global _LEGACY_MAX_ASYNC_WARNED
    cfg = _load_config()
    if cfg.get("max_async_children") is not None and not _LEGACY_MAX_ASYNC_WARNED:
        _LEGACY_MAX_ASYNC_WARNED = True
        logger.warning(
            "delegation.max_async_children is deprecated and ignored; "
            "delegation.max_concurrent_children now caps background "
            "delegations too. Remove the stale key from config.yaml."
        )
    return _get_max_concurrent_children()


DEFAULT_MAX_ITERATIONS = 250


# Heartbeat staleness thresholds of the retired hermes-child monitor, kept
# as pinned constants (a test pins their values; the omp engine owns child
# liveness now).
_HEARTBEAT_INTERVAL = 30
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 15 * 30s = 450s idle between turns -> stale
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 40 * 30s = 1200s stuck on same tool -> stale


def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """Best-effort local workspace hint for child prompts.

    We only inject a path when we have a concrete absolute directory. This avoids
    teaching subagents a fake container path while still helping them avoid
    guessing `/workspace/...` for local repo tasks.
    """
    candidates = [
        os.getenv("TERMINAL_CWD"),
        getattr(
            getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None
        ),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _normalized_runtime_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _inherit_parent_capabilities(
    parent_agent, override_provider, override_base_url
) -> Optional[dict]:
    """Return the parent's endpoint-trust capability map for a child, or None.

    The trusted-proxy capability map (``agent.capabilities``, e.g.
    ``openai_native_compaction`` from a custom_providers entry) is a trust
    decision scoped to one provider+endpoint. A child inherits it ONLY when
    it runs against the parent's exact route — any delegation override that
    changes provider or base_url stays DEFAULT-DENY, matching the /model
    switch posture (#94036/#97292).
    """
    if override_provider or override_base_url:
        return None
    parent_caps = getattr(parent_agent, "capabilities", None)
    if not isinstance(parent_caps, dict):
        return None
    return {
        key: value
        for key, value in parent_caps.items()
        if isinstance(key, str) and isinstance(value, bool)
    }


# ---------------------------------------------------------------------------
# Removed hermes-side child-agent engine -- compatibility shims.
#
# DEAD DEPTH: delegate_task routes exclusively through
# run_agent._dispatch_delegate_task -> tools/omp_delegation.
# The builders/runners below raise so stale callers (e.g. the plugin
# subagent-lifecycle service) fail loudly instead of silently forking a
# second delegation engine. The two config readers after them stay
# value-compatible (legacy keys, omp-side recursion is unguarded here).
# ---------------------------------------------------------------------------

_DEAD_ENGINE_MESSAGE = (
    "the hermes-side child-agent engine was removed (DEAD DEPTH); "
    "delegate_task routes exclusively through the omp engine "
    "(run_agent._dispatch_delegate_task -> "
    "tools/omp_delegation.dispatch_omp_delegation)"
)


def _build_child_agent(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(f"_build_child_agent removed: {_DEAD_ENGINE_MESSAGE}")


def _build_child_system_prompt(*args: Any, **kwargs: Any) -> str:
    raise RuntimeError(f"_build_child_system_prompt removed: {_DEAD_ENGINE_MESSAGE}")


def _build_child_progress_callback(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(f"_build_child_progress_callback removed: {_DEAD_ENGINE_MESSAGE}")


def _build_child_preserving_parent_tools(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(
        f"_build_child_preserving_parent_tools removed: {_DEAD_ENGINE_MESSAGE}"
    )


def _run_single_child(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    raise RuntimeError(f"_run_single_child removed: {_DEAD_ENGINE_MESSAGE}")


def _run_child_lifecycle(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    raise RuntimeError(f"_run_child_lifecycle removed: {_DEAD_ENGINE_MESSAGE}")


def _finalize_child_results(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError(f"_finalize_child_results removed: {_DEAD_ENGINE_MESSAGE}")


def _strip_blocked_tools(*args: Any, **kwargs: Any) -> List[str]:
    raise RuntimeError(f"_strip_blocked_tools removed: {_DEAD_ENGINE_MESSAGE}")


def _blocked_toolsets_for_role(*args: Any, **kwargs: Any) -> List[str]:
    raise RuntimeError(f"_blocked_toolsets_for_role removed: {_DEAD_ENGINE_MESSAGE}")


def _resolve_delegation_credentials(*args: Any, **kwargs: Any) -> dict:
    raise RuntimeError(
        f"_resolve_delegation_credentials removed: {_DEAD_ENGINE_MESSAGE}"
    )


def _resolve_child_credential_pool(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(
        f"_resolve_child_credential_pool removed: {_DEAD_ENGINE_MESSAGE}"
    )


def _merge_request_overrides(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(f"_merge_request_overrides removed: {_DEAD_ENGINE_MESSAGE}")


def _get_max_spawn_depth() -> int:
    """Legacy compat: hermes-side depth cap no longer enforced.

    Recursion is omp-side now, so nothing reads this guard. The
    ``delegation.max_spawn_depth`` config key is still accepted (floored at
    1, default 1) so old config files and status consumers keep working.
    """
    try:
        cfg = _load_config()
        val = cfg.get("max_spawn_depth")
        if val is None:
            return 1
        return max(1, int(val))
    except (TypeError, ValueError):
        return 1


def _get_orchestrator_enabled() -> bool:
    """Legacy compat: the orchestrator role no longer exists hermes-side."""
    try:
        cfg = _load_config()
        val = cfg.get("orchestrator_enabled", True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in {"true", "1", "yes", "on"}
        return True
    except Exception:
        return True


def _recover_tasks_from_json_string(
    tasks: Any,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


# Placeholder shapes for batch goal validation: bare 'TODO', bare 'task N'
# labels, or goals still carrying unexpanded template markers.
#
# The marker regex is deliberately NARROW: it only fires on snake_case /
# space-separated placeholder identifiers (`<feature_name>`, `{file path}`,
# `<FEATURE-NAME>`) — the shape LLM templates actually leave behind. Bare
# single-word brackets are left alone because legitimate coding goals are
# full of them: generics (`Vec<T>`, `Result<String>`), HTML tags (`<div>`),
# JSON/dict snippets (`{"key": 1}`), glob braces (`{a,b}`), and f-string
# style (`{i}`) must never be rejected (post-merge audit of #81141).
_PLACEHOLDER_GOAL_RE = re.compile(r"^(todo|task\s*\d+)$", re.IGNORECASE)
_TEMPLATE_MARKER_RE = re.compile(
    r"<[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)+>"
    r"|\{[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)+\}"
)
_MIN_BATCH_GOAL_LEN = 10


def _validate_batch_tasks(task_list: List[Dict[str, Any]]) -> Optional[str]:
    """Validate a tasks=[...] batch beyond per-task goal presence.

    Returns an actionable error string, or None when the batch is valid.

    A one-entry array is the canonical single-task shape (the advertised
    interface is tasks-only; legacy top-level `goal` is wrapped into a
    one-entry batch), so no minimum count is enforced. The placeholder/
    template checks below still run on every entry.

    Duplicate goals are deliberately NOT rejected: identical-goal fan-outs
    are a legitimate pattern (best-of-N / ensemble sampling), and blocking
    them broke real workflows (post-merge audit of #81141).
    """

    for i, task in enumerate(task_list):
        goal = str(task.get("goal", "")).strip()
        normalized = " ".join(goal.lower().split())

        if _PLACEHOLDER_GOAL_RE.match(normalized):
            return (
                f"Task {i} has a placeholder goal ({goal!r}). Replace it "
                "with a specific, self-contained description of what the "
                "subagent should accomplish."
            )
        marker = _TEMPLATE_MARKER_RE.search(goal)
        if marker:
            return (
                f"Task {i} goal contains an unexpanded template marker "
                f"({marker.group(0)!r}). Substitute the real value before "
                "calling delegate_task — subagents cannot resolve "
                "placeholders."
            )
        if len(goal) < _MIN_BATCH_GOAL_LEN and len(task_list) >= 2:
            # Multi-task fan-outs with terse goals are usually unexpanded
            # templates; a SINGLE task legitimately uses short goals
            # ("Fix the tests"), so one-entry arrays keep the historical
            # single-`goal` exemption.
            return (
                f"Task {i} goal is too short ({goal!r}). Write a specific, "
                "self-contained goal of at least "
                f"{_MIN_BATCH_GOAL_LEN} characters so the subagent knows "
                "exactly what to do."
            )
    return None


def normalize_delegation_names(task_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stamp a per-task ``name`` on every task (M0A, matrix observatory §8.1).

    Rule (D6): the name is the child's identity in delegation listings,
    steer/stop targeting, and the observatory UI — model-facing schema
    hard-requires it. Here, at the handler seam, a missing/blank name gets
    the derived fallback ``task-<n>`` (1-based) so legacy single-goal
    callers, cron paths, and direct python callers keep working unchanged.
    Whitespace is collapsed (display hygiene); the text is otherwise taken
    verbatim (unicode OK).
    """
    for i, task in enumerate(task_list):
        raw = task.get("name")
        name = " ".join(str(raw or "").split()) if raw is not None else ""
        task["name"] = name or f"task-{i + 1}"
    return task_list


def delegate_task(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    role: Optional[str] = None,
    background: Optional[bool] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    action: Optional[str] = None,
    subagent_id: Optional[str] = None,
    message: Optional[str] = None,
    parent_agent=None,
    credentials_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Spawn omp subagents to handle delegated tasks, or control live ones.

    Spawn modes (action='spawn' or omitted):
      - Single: provide goal (+ optional context)
      - Batch:  provide tasks array [{goal, context, name}, ...]

    Control modes (synchronous, never backgrounded):
      - action='list'  -> live children of this conversation's spawn tree
      - action='steer' -> queue course-correction text into a running child
                          (subagent_id + message)
      - action='stop'  -> interrupt a running child early (subagent_id)

    Spawning runs on the omp engine (dispatch_omp_delegation): top-level
    calls run in the background and re-enter as one consolidated message;
    orchestrator-child calls run synchronously within their turn.

    Legacy hermes-side knobs (role, max_iterations, credentials_cfg,
    background) are accepted for wire compat and ignored -- the omp engine
    owns routing, models, and nesting.

    Returns JSON with results array, one entry per task.
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    # -- Control plane: list/steer/stop run synchronously and return here.
    # They never spawn, so they bypass the pause gate and dispatch entirely.
    normalized_action = (action or "").strip().lower()
    if normalized_action in _CONTROL_ACTIONS:
        return _handle_control_action(
            normalized_action, subagent_id, message, parent_agent
        )
    if normalized_action and normalized_action != "spawn":
        return tool_error(
            f"Unknown action '{action}'. Use spawn (default), list, steer, or stop."
        )

    # Operator-controlled kill switch -- freezes new fan-out without
    # interrupting already-running children. Cleared via `delegation.pause`.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    # Normalize to task list (same validation the hermes path enforced).
    max_children = _get_max_concurrent_children()
    recovered_tasks, tasks_error = _recover_tasks_from_json_string(tasks)
    if tasks_error:
        return tool_error(tasks_error)
    if recovered_tasks is not None:
        tasks = recovered_tasks

    # Small models frequently emit an empty tasks array ([]) alongside a
    # single goal. Treat that as "no batch" instead of letting the batch
    # quality gate below reject the goal-derived single task.
    if isinstance(tasks, list) and not tasks:
        tasks = None

    if tasks and isinstance(tasks, list):
        if len(tasks) > max_children:
            return tool_error(
                f"Too many tasks: {len(tasks)} provided, but "
                f"max_concurrent_children is {max_children}. "
                f"Either reduce the task count, split into multiple "
                f"delegate_task calls, or increase "
                f"delegation.max_concurrent_children in config.yaml."
            )
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        single_task: Dict[str, Any] = {"goal": goal, "context": context}
        if output_schema is not None:
            single_task["output_schema"] = output_schema
        task_list = [single_task]
    else:
        return tool_error(
            "No tasks provided. Pass tasks=[{goal: '...', context: '...'}, "
            "...] -- one entry per subagent (a single task is a one-entry "
            "array)."
        )

    if not task_list:
        return tool_error("No tasks provided.")

    # Validate each task has a goal.
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(
                f"Task {i} must be an object, got {type(task).__name__}."
            )
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    # Batch-only quality gate: placeholder goals, unexpanded template
    # markers, terse multi-task goals. The single-`goal` form is exempt for
    # short goals; duplicates are allowed (best-of-N).
    if tasks is not None and isinstance(tasks, list):
        batch_error = _validate_batch_tasks(task_list)
        if batch_error:
            return tool_error(batch_error)

    # Every task gets a name (provided or task-<n>) for listings/steering.
    normalize_delegation_names(task_list)

    # Coerce/validate optional per-task output_schema up front so a malformed
    # schema fails loudly instead of dispatching children that can never
    # satisfy their contract.
    from tools.delegation_output_schema import coerce_output_schema

    for i, task in enumerate(task_list):
        raw_schema = task.get("output_schema")
        if raw_schema is None and len(task_list) == 1 and output_schema is not None:
            raw_schema = output_schema
        _, schema_err = coerce_output_schema(raw_schema)
        if schema_err:
            return tool_error(f"Task {i} output_schema invalid: {schema_err}")

    cleaned = _strip_model_hidden_task_fields(task_list)
    if not isinstance(cleaned, list):
        cleaned = task_list

    from tools.omp_delegation import dispatch_omp_delegation

    return dispatch_omp_delegation(
        parent_agent,
        {"goal": goal, "context": context, "tasks": cleaned},
    )


def _load_config() -> dict:
    """Load delegation config from the active Mercury config.

    Prefer the shared persistent loader because it follows the active
    HERMES_HOME/profile. ``cli.CLI_CONFIG`` is a legacy fallback for entry
    points that cannot import the shared loader; importing it first can return
    an old default ``delegation`` block and hide user-set keys such as
    ``max_concurrent_children``.

    Uses ``load_config_readonly()``: every consumer of this dict is read-only
    (``.get()`` lookups), and this runs on each ``get_definitions()`` schema
    rebuild via ``_get_max_concurrent_children``, so skipping the defensive
    deepcopy matters. Do NOT mutate the returned dict.

    ``HERMES_IGNORE_USER_CONFIG=1`` (``mercury chat --ignore-user-config``) is
    only honored by the legacy ``cli`` loader, not the shared one, so when the
    flag is set we keep ``cli.CLI_CONFIG`` authoritative to preserve the
    flag's contract of suppressing user config.yaml settings.
    """
    prefer_legacy = os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1"
    if not prefer_legacy:
        try:
            from mercury_cli.config import load_config_readonly

            full = load_config_readonly()
            cfg = full.get("delegation") or {}
            if isinstance(cfg, dict):
                return cfg
        except Exception:
            pass
    try:
        from cli import CLI_CONFIG

        cfg = CLI_CONFIG.get("delegation") or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OpenAI Function-Calling Schema
# ---------------------------------------------------------------------------


def _build_top_level_description() -> str:
    """Compose the delegate_task tool description.

    Deliberately carries ONLY guidance that exists nowhere else in the
    schema. Batch/concurrency limits live in the 'tasks' parameter
    description and the nesting clause lives in the 'role' parameter
    description (both rebuilt per get_definitions() call with the user's
    actual delegation.max_concurrent_children / max_spawn_depth), so the
    top-level text stays static and duplication-free. If you add text
    here, check it is not already stated in a parameter description.
    """
    # HERMES-OMP PATCH (docs strip): in Mercury, children run as omp
    # one-shots — recursion is omp-native, and the legacy hermes-side
    # depth-guard text described machinery that no longer applies. The
    # only child restriction that survives is the tool allowlist rule.
    restrictions_rule = (
        "- Children cannot call clarify, memory, or cronjob.\n"
    )

    return (
        "Spawn subagents in isolated contexts; each gets its own conversation, "
        "terminal session, and toolset, and only its final summary returns to "
        "you. Subagents run on the omp engine, which is Mercury's coding "
        "specialist — they can write, edit, build, and test code, and can "
        "spawn subagents of their own.\n\n"
        "CODING TASKS ALWAYS DELEGATE: any task that produces or modifies "
        "code, scripts, or config goes to at least one subagent (you "
        "orchestrate; omp executes). Write code yourself only when "
        "delegation is impossible (subagent engine down mid-task), and say "
        "so when you do.\n\n"
        "Runs in the background: dispatch returns immediately with live "
        "transcript paths, and the consolidated result re-enters the "
        "conversation on its own. Do NOT wait or poll; continue other "
        "work. While children run, `action` (list/steer/stop) controls "
        "them live — steer when a transcript shows a child drifting.\n\n"
        "USE FOR: ALL coding work (see above), reasoning-heavy subtasks, "
        "work that would flood your context with intermediate data, or "
        "independent parallel workstreams.\n"
        "DO NOT USE FOR (use these instead):\n"
        "- Pure data transformation with NO code artifact (parse this JSON, "
        "compute these totals) -> execute_code\n"
        "- A single tool call -> call the tool directly\n"
        "- Tasks needing user interaction -> subagents cannot ask questions\n"
        "- Durable work that must survive this session -> cronjob or "
        "terminal(background=True, notify=True); /stop, /new, or "
        "process exit discards running subagents.\n\n"
        "RULES:\n"
        "- Children know nothing of this conversation: pass everything needed "
        "via 'context', including any required output language, tone, or "
        "style (e.g. \"respond in Chinese\").\n"
        "- Child summaries are SELF-REPORTS, not verified facts: a child "
        "claiming \"uploaded successfully\" or \"file written\" may be wrong. "
        "For external side effects (uploads, remote writes, publishing), "
        "require a verifiable handle (URL, ID, absolute path) and verify it "
        "yourself before telling the user the operation succeeded.\n"
        + restrictions_rule +
        "- Every task also carries a short `name` (2-4 words) — the "
        "child's identity in listings, steering, and the observatory.\n"
        "- Children run on the delegate slots unless pinned otherwise in "
        "config."
    )


def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"The task(s), up to {max_children} in parallel for this user (set "
        "via delegation.max_concurrent_children). Each entry spawns one "
        "subagent with isolated context and terminal session; a single task "
        "is a one-entry array. Required when spawning. EVERY entry needs "
        "both `goal` and a short task-relevant `name` (2-4 words, unique in "
        "the batch) — the name becomes that child's identity in delegation "
        "listings, steering, and the observatory UI."
    )




def _build_role_param_description() -> str:
    """Legacy helper -- the `role` param is no longer advertised.

    Delegation runs on the omp engine, which nests subagents natively;
    there is no caller-declared role. Kept because external callers import
    this symbol; returns a static legacy note.
    """
    return (
        "Legacy parameter, ignored: delegation runs on the omp engine, "
        "which spawns and nests subagents natively -- no caller-declared "
        "role is needed."
    )


def _build_dynamic_schema_overrides() -> dict:
    """Return per-call schema overrides reflecting current config.

    Plugged into ToolEntry.dynamic_schema_overrides so every
    get_definitions() pass rewrites the description fields to the user's
    actual limits.
    """
    overrides_params = {
        **DELEGATE_TASK_SCHEMA["parameters"],
    }
    # Deep-copy properties so we don't mutate the static schema dict.
    overrides_params["properties"] = {
        k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()
    }
    # M0A (matrix observatory §8.1 item 1 / D6): mirror the static schema —
    # `name` is hard-required in tasks.items model-facing; the handler
    # derives task-<n> fallbacks for non-model callers (legacy single-goal
    # shape, cron, direct python).
    _tasks_override = dict(overrides_params["properties"]["tasks"])
    _tasks_override["items"] = {
        **_tasks_override["items"],
        "required": ["goal", "name"],
    }
    _tasks_override["description"] = _build_tasks_param_description()
    overrides_params["properties"]["tasks"] = _tasks_override

    return {
        "description": _build_top_level_description(),
        "parameters": overrides_params,
    }


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # NOTE: description / tasks.description / role.description are placeholder
    # values. The real text is generated per get_definitions() call by
    # _build_dynamic_schema_overrides() (registered via
    # dynamic_schema_overrides below) so the model sees the user's actual
    # delegation.max_concurrent_children / max_spawn_depth, not the framework
    # defaults. Building these lazily (instead of at module import) also
    # avoids forcing cli.CLI_CONFIG to load before the test conftest can
    # redirect HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect "
        "the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # NOTE: the handler also accepts the legacy single-goal shape —
            # top-level `goal` (string), `context` (string), `output_schema`
            # (object) — wrapped into a one-entry batch at dispatch. Legacy,
            # unadvertised (old transcripts/callers only); tasks=[...] is the
            # only advertised shape. Do not re-add these to the schema.
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": (
                                "What this subagent should accomplish. Be "
                                "specific and self-contained — it knows "
                                "nothing about your conversation history."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "SHORT task-relevant name for this subagent "
                                "(2-4 words, unique within the batch). It is "
                                "the child's identity in delegation listings, "
                                "steering targets, and the observatory UI — "
                                "not shown to the child. Unicode allowed; "
                                "lowercase-kebab recommended (e.g. "
                                "'auth-refactor', '修复测试')."
                            ),
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "Background THIS child needs: file paths, "
                                "error messages, constraints. Each child "
                                "sees only its own context — repeat shared "
                                "background in every task that needs it."
                            ),
                        },
                        "output_schema": {
                            "type": "object",
                            "description": (
                                "Optional JSON Schema this child's final "
                                "answer must validate against (told to the "
                                "child up front; parent validates with one "
                                "bounded correction retry; result gains "
                                "schema_valid, plus schema_errors on "
                                "failure). Keep it forgiving — require only "
                                "fields you will read."
                            ),
                        },
                    },
                    # M0A (matrix observatory §8.1 item 1 / D6): `name` is
                    # hard-required in the MODEL-FACING schema. The handler
                    # still derives fallback names for non-model callers
                    # (legacy single-goal shape, cron, direct python) so
                    # nothing breaks — the schema is the mandate, the
                    # fallback is the compat seam.
                    "required": ["goal", "name"],
                },
                # No maxItems — the runtime limit is configurable via
                # delegation.max_concurrent_children (default 3) and
                # enforced with a clear error in delegate_task().
                # NOTE: the handler also accepts a per-task `role` — legacy,
                # ignored: delegation capability is depth-derived, not
                # caller-declared. Unadvertised on purpose; do not re-add.
                "description": "(rebuilt at get_definitions() time)",
            },
            # NOTE: the handler also accepts `background` (bool) — DEPRECATED,
            # ignored: top-level delegations always run in the background.
            # Deliberately unadvertised (old transcripts/callers only); do not
            # re-add to the schema.
            "action": {
                "type": "string",
                "enum": ["spawn", "list", "steer", "stop"],
                "description": (
                    "Default 'spawn'. Live control of running children: "
                    "'list' = ids/goals/status/transcripts; 'steer' = queue "
                    "course-correction text into one child (subagent_id + "
                    "message) without stopping it; 'stop' = end one child "
                    "early (subagent_id; partial result still returns). "
                    "Control actions return immediately; goal/tasks are "
                    "ignored unless spawning."
                ),
            },
            "subagent_id": {
                "type": "string",
                "description": (
                    "Target for action='steer'/'stop' (ids from the spawn "
                    "response or action='list')."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "For action='steer': the course correction, appended to "
                    "the child's next tool result mid-run. Be directive and "
                    "specific."
                ),
            },
        },
        "required": [],
    },
}


def _model_background_value(args: dict, parent_agent=None) -> bool:
    """Background flag for the MODEL-facing dispatch path (registry fallback).

    Delegations from the top-level agent always run in the background — the
    model does not choose. This applies to both a single task and a fan-out
    batch (the whole batch is one async unit that joins on all children and
    returns one consolidated result). The one
    exception is a delegation from an orchestrator subagent (depth > 0), which
    needs its workers' results within its own turn. The live path is
    ``run_agent._dispatch_delegate_task``; this lambda mirrors it for the rare
    case the intercept is bypassed. Direct Python callers of ``delegate_task``
    keep the historical synchronous default.
    """
    is_subagent = getattr(parent_agent, "_delegate_depth", 0) > 0
    return not is_subagent


_MODEL_HIDDEN_TASK_FIELDS = {"acp_command", "acp_args"}


def _strip_model_hidden_task_fields(tasks: Any) -> Any:
    if not isinstance(tasks, list):
        return tasks
    stripped_tasks = []
    changed = False
    for task in tasks:
        if not isinstance(task, dict):
            stripped_tasks.append(task)
            continue
        stripped = {
            key: value
            for key, value in task.items()
            if key not in _MODEL_HIDDEN_TASK_FIELDS
        }
        changed = changed or len(stripped) != len(task)
        stripped_tasks.append(stripped)
    return stripped_tasks if changed else tasks


# --- Registry ---
from tools.registry import registry, tool_error

# MERCURY-OMP PATCH (B1): registry fallback → omp engine (see registration
# below). Same dispatch semantics as the live path; direct Python callers of
# ``delegate_task`` (tests, internal helpers) keep the historical synchronous
# behavior via the original function, which remains importable and untouched.
def _omp_registry_fallback(args: dict, kw: dict) -> str:
    try:
        from tools.omp_delegation import dispatch_omp_delegation
        return dispatch_omp_delegation(kw.get("parent_agent"), args)
    except Exception:
        # Fail-hard with the error visible — never a silent fallback to
        # Mercury child agents (one delegation engine in this distribution).
        import traceback
        detail = traceback.format_exc(limit=2)
        return json.dumps({
            "status": "failed",
            "error": f"omp delegation engine unavailable: {detail}",
        }, ensure_ascii=False)


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: _omp_registry_fallback(args, kw),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)
