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
collapses — every injection starts a turn, and ``kind`` selects the entry
path: ``prompt``/``steer`` run the agent turn directly, while ``command``
first tries the gateway slash dispatch (the same table
``GatewayRunner._handle_message`` uses) and falls back to a turn for
unknown verbs.

The gateway-session agent is built with ``platform="matrix"`` so the
system prompt picks the Matrix formatting hint; spawned orchestrators
(``observatory.spawn``) keep the ``cli`` default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: hermes session id behind the observatory gateway node
#: (``session_ref="session:gateway"`` in state.db).
GATEWAY_SESSION_ID = "gateway"

#: Kinds the sidecar may send. ``prompt``/``steer`` both start a turn;
#: ``command`` tries slash dispatch first. Anything else is a caller bug.
INJECT_KINDS = frozenset({"prompt", "steer", "command"})

#: Live-progress datagram socket name under ``$MERCURY_HOME/observatory``.
#: The turn collector fire-and-forget sends one ``SOCK_DGRAM`` datagram per
#: captured event; the sidecar ingests them for live room streaming (§5).
#: Best-effort: no listener or send error = drop silently.
GATEWAY_PROGRESS_SOCK_NAME = "gateway-progress.sock"

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

    The gateway session renders into a Matrix room, so it is built with
    ``platform="matrix"`` (Matrix markdown hint). Spawned orchestrators
    keep the ``cli`` default in ``build_hermes_agent``.
    """
    from observatory.spawn import build_hermes_agent

    return build_hermes_agent(session_id=session_id, platform="matrix")


def drop_cached_agent(session_id: str = GATEWAY_SESSION_ID) -> None:
    """Forget the cached agent (tests + wedge recovery)."""
    with _locks_guard:
        agent = _session_agents.pop(session_id, None)
    if agent is not None:
        try:
            agent.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            logger.exception("gateway_session: cached agent close failed")


def _parse_slash_verb(text: str) -> tuple[str, str]:
    """Split ``/verb args`` (or ``!verb``) into (verb, args)."""
    stripped = (text or "").strip()
    if stripped[:1] in ("/", "!"):
        stripped = stripped[1:]
    parts = stripped.split(None, 1)
    verb = (parts[0] or "").lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    # Strip @bot suffix (gateway parity with MessageEvent.get_command).
    if "@" in verb:
        verb = verb.split("@", 1)[0]
    return verb, args


def _dispatch_slash_command(text: str) -> Optional[str]:
    """Try the gateway slash dispatch for ``text``; None = unknown verb.

    Uses the same table ``GatewayRunner._handle_message`` uses:

    1. ``mercury_cli.commands.resolve_command`` (same resolver, aliases
       included) + ``is_gateway_known_command`` (same gate). Unknown →
       None so the caller falls back to ``run_conversation``.
    2. Registry-owned pure executors via ``mercury_cli.slash_exec``
       (``/version``, ``/help``, ``/commands``, ``/profile`` … — no
       session mutation, safe headless).
    3. The runner's plain-command table
       (``GatewayRunner._gateway_plain_command_handlers`` — ``/status``,
       ``/restart`` via ``slash_commands.py`` etc.) against the live
       runner when one is serving this process; best-effort, any
       failure falls through to None (caller falls back to a turn).

    Never raises: unexpected failures log and return None.
    """
    try:
        verb, args = _parse_slash_verb(text)
        if not verb:
            return None
        from mercury_cli.commands import is_gateway_known_command, resolve_command

        cmd_def = resolve_command(verb)
        canonical = cmd_def.name if cmd_def is not None else verb
        if not is_gateway_known_command(canonical):
            return None
        # 2. Pure executors first (no runner needed).
        try:
            from mercury_cli.slash_exec import CommandContext, run_execute

            reply = run_execute(
                cmd_def, CommandContext(surface="gateway", args=args)
            )
            if reply is not None:
                return reply.text
        except Exception:
            logger.debug("gateway_session: slash executor failed for /%s", verb, exc_info=True)
        # 3. Plain-command table on the live runner (stateful commands).
        try:
            from gateway.run import _gateway_runner_ref  # type: ignore

            ref = _gateway_runner_ref
            runner = ref() if ref is not None else None
        except Exception:
            runner = None
        if runner is None:
            return None
        try:
            handlers = runner._gateway_plain_command_handlers()
        except Exception:
            return None
        handler = handlers.get(canonical)
        if handler is None:
            return None
        try:
            from gateway.config import Platform
            from gateway.platforms.base import MessageEvent
            from gateway.session import SessionSource
        except Exception:
            return None
        try:
            event = MessageEvent(
                text=(text or "").strip(),
                source=SessionSource(
                    platform=Platform.MATRIX,
                    chat_id="gateway",
                    chat_type="dm",
                ),
            )
            result = handler(event)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    # Sync socket-thread context never has a running loop;
                    # an async caller hitting this path falls back to a
                    # turn rather than deadlocking on nested run.
                    return None
                result = asyncio.run(result)
            if result is None:
                return ""
            # EphemeralReply and friends stringify to their text.
            return str(result)
        except Exception:
            logger.debug("gateway_session: plain handler failed for /%s", verb, exc_info=True)
            return None
    except Exception:
        logger.debug("gateway_session: slash dispatch failed", exc_info=True)
        return None


def _progress_socket_path() -> Path:
    """``$MERCURY_HOME/observatory/gateway-progress.sock`` (never raises)."""
    try:
        from observatory.provision import _mercury_home

        return Path(_mercury_home()) / "observatory" / GATEWAY_PROGRESS_SOCK_NAME
    except Exception:
        env = os.environ.get("MERCURY_HOME", "").strip()
        home = Path(env).expanduser() if env else Path.home() / ".mercury"
        return home / "observatory" / GATEWAY_PROGRESS_SOCK_NAME


def _normalize_text(text: Any) -> str:
    """Whitespace-collapsed compare form (reply-echo + double-capture dedupe)."""
    return " ".join(str(text).split())


def _push_progress(node_id: str, seq: int, event: dict[str, Any]) -> None:
    """Fire-and-forget one live-progress datagram; never raises.

    Payload is ``{node_id, seq, event}`` where ``event`` is the existing
    shape (no ``seq`` inside — it rides beside it). No listener, missing
    socket dir, or any send error = drop silently.
    """
    try:
        payload = json.dumps({"node_id": node_id, "seq": seq, "event": event}).encode("utf-8")
    except Exception:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(payload, str(_progress_socket_path()))
        finally:
            try:
                sock.close()
            except Exception:
                pass
    except Exception:
        pass


class _TurnEventCollector:
    """Accumulate batched tool/thinking events during one turn, live-pushed.

    Rides the agent's existing display callbacks — ``tool_progress_callback``
    (``tool.started`` carries the tool name + args dict; ``_thinking`` /
    ``reasoning.available`` carry assistant scratch text) plus the
    ``thinking_callback``/``reasoning_callback`` string sinks. Holds no
    agent-loop state; installed around the turn and removed after.

    Each captured event takes the next per-turn ``seq`` starting at 0
    (capture order) and is immediately pushed as a best-effort datagram;
    the same ``seq`` rides the final ``events()`` list so live and final
    correlate. Thinking double-capture (``_thinking`` tool_progress vs the
    thinking/reasoning string sinks firing for the same text) is deduped
    on normalized text — first capture wins, the drop consumes no seq.
    """

    def __init__(self, node_id: str = "gw") -> None:
        self._node_id = node_id or "gw"
        self._next_seq = 0
        self._records: list[dict[str, Any]] = []
        self._seen_thinking: set[str] = set()

    # -- capture ---------------------------------------------------------
    def _record(self, event: dict[str, Any]) -> None:
        seq = self._next_seq
        self._next_seq += 1
        stored = dict(event)
        stored["seq"] = seq
        self._records.append(stored)
        _push_progress(self._node_id, seq, event)

    def _add_thinking(self, text: str) -> None:
        norm = _normalize_text(text)
        if not norm or norm in self._seen_thinking:
            return
        self._seen_thinking.add(norm)
        self._record({"type": "thinking", "text": text.strip()})

    # -- callback shapes -------------------------------------------------
    def tool_progress(self, event_type: str, name: str | None = None, preview=None, args=None, **kwargs) -> None:
        try:
            if event_type == "_thinking" or name == "_thinking":
                text = preview if name == "_thinking" else (name or "")
                if isinstance(text, str) and text.strip():
                    self._add_thinking(text)
                return
            if event_type == "tool.started" and name:
                if str(name).startswith("_"):
                    return
                if isinstance(args, dict):
                    self._record({"type": "tool_call", "tool": str(name), "args": args})
                elif args is None:
                    self._record({"type": "tool_call", "tool": str(name), "args": {}})
                else:
                    self._record({"type": "tool_call", "tool": str(name), "args": {"_raw": str(args)}})
        except Exception:
            pass

    def thinking(self, text: str) -> None:
        try:
            if isinstance(text, str) and text.strip():
                self._add_thinking(text)
        except Exception:
            pass

    def events(self) -> list[dict[str, Any]]:
        return [dict(rec) for rec in self._records]


def _install_collector(agent: Any, collector: _TurnEventCollector) -> Callable[[], None]:
    """Attach the collector to the agent's display callbacks; return restore."""
    prev_tp = getattr(agent, "tool_progress_callback", None)
    prev_th = getattr(agent, "thinking_callback", None)
    prev_re = getattr(agent, "reasoning_callback", None)

    def _tp(event_type: str, name: str | None = None, preview=None, args=None, **kwargs) -> None:
        if callable(prev_tp):
            try:
                prev_tp(event_type, name, preview, args, **kwargs)
            except Exception:
                pass
        collector.tool_progress(event_type, name, preview, args, **kwargs)

    def _th(text: str) -> None:
        if callable(prev_th):
            try:
                prev_th(text)
            except Exception:
                pass
        collector.thinking(text)

    def _re(text: str) -> None:
        if callable(prev_re):
            try:
                prev_re(text)
            except Exception:
                pass
        collector.thinking(text)

    try:
        agent.tool_progress_callback = _tp
    except Exception:
        pass
    try:
        agent.thinking_callback = _th
    except Exception:
        pass
    try:
        agent.reasoning_callback = _re
    except Exception:
        pass

    def _restore() -> None:
        for attr, prev in (
            ("tool_progress_callback", prev_tp),
            ("thinking_callback", prev_th),
            ("reasoning_callback", prev_re),
        ):
            try:
                setattr(agent, attr, prev)
            except Exception:
                pass

    return _restore


def run_gateway_prompt_with_events(
    text: str,
    *,
    kind: str = "prompt",
    session_id: str = GATEWAY_SESSION_ID,
    node_id: str = "gw",
    agent_factory: Optional[Callable[[str], Any]] = None,
    turn: Optional[Callable[[Any, str], Any]] = None,
    slash_dispatch: Optional[Callable[[str], Optional[str]]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one headless turn; return (reply text, batched display events).

    ``kind == "command"`` first tries the gateway slash dispatch (same
    table ``_handle_message`` uses); a non-None dispatch result is the
    reply (no tool events — the command never ran a turn). Unknown verbs
    (dispatch returns None) and ``prompt``/``steer`` run the agent turn
    with display callbacks attached, batching ``tool_call`` + ``thinking``
    events for the sidecar replay.

    Every batched event carries its per-turn ``seq`` (0-based capture
    order, keys otherwise stable) and was already live-pushed as
    ``{node_id, seq, event}`` during the turn. Thinking events echoing
    the final reply (normalized compare — the final message must not
    appear twice, once plain once as reasoning) are dropped here; the
    live datagrams for them already went out and keep their seqs, so
    final seqs may show gaps.
    """
    clean = (text or "").strip()
    if not clean:
        raise ValueError("gateway_session: refusing empty prompt text")
    if kind not in INJECT_KINDS:
        raise ValueError(f"gateway_session: unknown inject kind {kind!r}")
    if not session_id:
        raise ValueError("gateway_session: session_id is required")

    if kind == "command":
        dispatch = slash_dispatch if slash_dispatch is not None else _dispatch_slash_command
        try:
            out = dispatch(clean)
        except Exception:
            logger.debug("gateway_session: slash dispatch raised", exc_info=True)
            out = None
        if out is not None:
            return str(out), []

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
        collector = _TurnEventCollector(node_id=node_id)
        restore = _install_collector(agent, collector)
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
        finally:
            try:
                restore()
            except Exception:
                pass
    if not isinstance(result, dict):
        raise RuntimeError(
            f"gateway_session: turn returned {type(result).__name__}, not a result dict"
        )
    reply = str(result.get("final_response") or "")
    events = collector.events()
    norm_reply = _normalize_text(reply)
    if norm_reply:
        events = [
            e
            for e in events
            if not (
                e.get("type") == "thinking"
                and _normalize_text(e.get("text", "")) == norm_reply
            )
        ]
    return reply, events


def run_gateway_prompt(
    text: str,
    *,
    kind: str = "prompt",
    session_id: str = GATEWAY_SESSION_ID,
    node_id: str = "gw",
    agent_factory: Optional[Callable[[str], Any]] = None,
    turn: Optional[Callable[[Any, str], Any]] = None,
    slash_dispatch: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Run one headless turn on the gateway session; return its reply text.

    ``agent_factory(session_id)`` replaces the real builder and
    ``turn(agent, text)`` replaces ``agent.run_conversation`` (tests inject
    doubles; both default to the live engine). Raises on empty text,
    unknown kind, or engine failure — the control-socket envelope reports
    the failure and the sidecar tells the room honestly.
    """
    reply, _events = run_gateway_prompt_with_events(
        text,
        kind=kind,
        session_id=session_id,
        node_id=node_id,
        agent_factory=agent_factory,
        turn=turn,
        slash_dispatch=slash_dispatch,
    )
    return reply
