"""Gateway-process side of Matrix observatory prompt delivery (M4a/M5c).

The observatory sidecar owns all Matrix I/O, but the gateway agent's hermes
session lives in (and is resumed by) the gateway process itself — the D18
respawn pass deliberately never builds a handle for it
(``observatory.respawn`` SKIP_RESPAWN_KINDS). So a gateway-room prompt must
cross into the gateway process: the sidecar sends the ``inject`` verb over
the gateway control socket (``gateway.control_socket``) and the gateway
answers it with :func:`run_gateway_prompt` here.

Execution is a headless AIAgent turn on a STABLE session id
(``GATEWAY_SESSION_ID``) — the same machinery ``mercury -z`` uses
(``mercury_cli.oneshot``: config-resolved runtime + ``run_conversation``),
except the agent is cached per session so consecutive Matrix messages share
one transcript. Turns are serialized per session: a headless session is
always idle between turns, so steer-vs-prompt (a busy-session distinction)
collapses — every injection starts a turn, and ``kind`` is carried only so
the wire shape stays stable if that ever changes.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: hermes session id behind the observatory gateway node
#: (``session_ref="session:gateway"`` in state.db).
GATEWAY_SESSION_ID = "gateway"

#: Kinds the sidecar may send. All start a turn (see module docstring);
#: anything else is a caller bug, rejected loudly.
INJECT_KINDS = frozenset({"prompt", "steer", "command"})

_locks_guard = threading.Lock()
_session_locks: dict[str, threading.Lock] = {}
_session_agents: dict[str, Any] = {}


def _session_lock(session_id: str) -> threading.Lock:
    """Per-session turn lock (created once, held for build + turn)."""
    with _locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def _default_agent(session_id: str) -> Any:
    """Fresh-or-resumed AIAgent on the session id.

    Same builder the D18 respawn pass uses for spawned hermes
    orchestrators (dedicated SessionDB handle on the home's hermes
    state.db — never a borrowed live object). The turn prologue resolves
    the compression-lineage tip and loads prior history, so a stored
    session resumes and a missing one starts fresh.
    """
    from observatory.spawn import build_hermes_agent

    return build_hermes_agent(session_id=session_id)


def drop_cached_agent(session_id: str = GATEWAY_SESSION_ID) -> None:
    """Forget the cached agent (tests + wedge recovery)."""
    with _locks_guard:
        agent = _session_agents.pop(session_id, None)
    if agent is not None:
        try:
            agent.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.exception("gateway_session: cached agent close failed")


def run_gateway_prompt(
    text: str,
    *,
    kind: str = "prompt",
    session_id: str = GATEWAY_SESSION_ID,
    agent_factory: Optional[Callable[[str], Any]] = None,
    turn: Optional[Callable[[Any, str], Any]] = None,
) -> str:
    """Run one headless turn on the gateway session; return its reply text.

    ``agent_factory(session_id)`` replaces the real builder and
    ``turn(agent, text)`` replaces ``agent.run_conversation`` (tests inject
    doubles; both default to the live engine). Raises on empty text,
    unknown kind, or engine failure — the control-socket envelope reports
    the failure and the sidecar tells the room honestly.
    """
    clean = (text or "").strip()
    if not clean:
        raise ValueError("gateway_session: refusing empty prompt text")
    if kind not in INJECT_KINDS:
        raise ValueError(f"gateway_session: unknown inject kind {kind!r}")
    if not session_id:
        raise ValueError("gateway_session: session_id is required")

    with _session_lock(session_id):
        if agent_factory is not None:
            agent = agent_factory(session_id)
        else:
            with _locks_guard:
                agent = _session_agents.get(session_id)
            if agent is None:
                agent = _default_agent(session_id)
                with _locks_guard:
                    _session_agents[session_id] = agent
        try:
            result = turn(agent, clean) if turn is not None else agent.run_conversation(clean)
        except Exception:
            # A failed turn may leave the cached agent wedged (broken
            # stream, poisoned cache) — drop it so the next prompt rebuilds.
            if agent_factory is None:
                with _locks_guard:
                    if _session_agents.get(session_id) is agent:
                        _session_agents.pop(session_id, None)
            raise
    if not isinstance(result, dict):
        raise RuntimeError(
            f"gateway_session: turn returned {type(result).__name__}, not a result dict"
        )
    reply = result.get("final_response") or ""
    return str(reply)
