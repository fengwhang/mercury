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

#: Datagram ``kind`` values on the gateway-progress socket. The turn
#: collector sends ``{node_id, seq, event}`` with NO kind (legacy shape —
#: the sidecar defaults it to ``TURN_PROGRESS_KIND``); gateway-origin
#: omp-child frames carry an explicit kind so the sidecar can create and
#: render child nodes from wire bytes alone. The sidecar MUST NEVER
#: import the gateway's in-process ``tools.omp_delegation._live_children``
#: table (separate processes in production — that import always fails
#: there); the gateway reads its OWN table same-process and pushes bytes.
TURN_PROGRESS_KIND = "turn_progress"
#: ``{"kind": "child_lifecycle", "node_id", "lifecycle": "start"|"stop",
#: "name", "goal", "delegation_id", "task_index", "parent_session",
#: "status", "summary"}`` — the gateway-child feed watcher emits start
#: when a ``_live_children`` entry appears, stop when it disappears.
CHILD_LIFECYCLE_KIND = "child_lifecycle"
#: ``{"kind": "child_event", "node_id", "feed": {...}}`` — one forwarded
#: ``OmpFeed`` typed event (node/tool/thought/message) for a live child.
CHILD_EVENT_KIND = "child_event"
#: ``{"kind": "approval_prompt", "node_id", "request_id", "command",
#: "description", "session_key"}`` — one guard approval raised by a gateway
#: Matrix turn, forwarded so the sidecar mirrors it into the node's room.
#: The turn blocks in ``tools.approval``'s gateway queue under
#: :data:`GATEWAY_APPROVAL_SESSION_KEY`; the room /approve resolves that
#: same queue (via the sidecar bridge + ``resolve-approval`` control verb).
APPROVAL_PROMPT_KIND = "approval_prompt"

#: Canonical ``tools.approval`` session key for gateway Matrix turns
#: (``session:gateway`` — the gateway node's ``session_ref`` in state.db).
#: BOTH processes use this one value: the gateway turn sets it as the
#: ambient approval key and registers the datagram forwarder under it;
#: the sidecar registers the bridge ingest (``gateway_notify``) under it.
#: One shared constant — never recompute per side — so the key the turn
#: blocks on is always the key the room resolves.
GATEWAY_APPROVAL_SESSION_KEY = "session:gateway"

#: Gateway-child feed watcher poll cadence (seconds).
CHILD_FEED_POLL_S = 1.0

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


def _live_runner() -> Any | None:
    """The live GatewayRunner serving this process, if any (never raises)."""
    try:
        from gateway.run import _gateway_runner_ref  # type: ignore

        ref = _gateway_runner_ref
        return ref() if ref is not None else None
    except Exception:
        return None


def _dispatch_slash_command(
    text: str,
    *,
    node_id: str | None = None,
    room_id: str | None = None,
) -> Optional[str]:
    """Try the gateway slash dispatch for ``text``; None = unknown verb.

    Generic pass-through (no per-command code): after the
    ``is_gateway_known_command`` gate (unknown verbs return None so the
    caller falls back to a turn), synthesize a Matrix ``MessageEvent``
    and call the LIVE runner's full ``_handle_message`` — the same
    pipeline every other surface uses. Every present and future
    command/skill/plugin dispatches for free. /spawn + /spawnomp ride
    this path via COMMAND_REGISTRY; /exit rides it by raw-verb routing
    in the runner (it owns no registry entry — CLI /quit keeps the
    `exit` alias there).

    Session override: the event carries
    ``metadata["gateway_session_id"] = "gateway"`` (the runner's
    explicit-session seam, honored by ``_handle_message_with_agent``
    for turn fall-throughs such as rewritten blueprint seeds) plus
    ``gateway_session_key`` derived from the same source through the
    live runner (arms the route-recovery guard), and ``internal=True``
    (skips pairing auth — the Matrix ghost is not a paired user —
    startup-restore queueing, and activity stamping; command-scoped
    ``command:`` hooks still fire). Observability scope travels alongside
    as ``metadata["observatory_node_id"]`` / ``["observatory_room_id"]``
    (the sidecar inject params) so /spawn + /spawnomp + /exit handlers
    can enforce D13 room scope without a second dispatch path. Handlers that keep per-session
    state (``/model`` overrides, destructive confirms) resolve it under
    the stable Matrix DM key via ``_session_key_for_source`` — the same
    key on every Matrix call.

    Degraded forms are whatever each handler already supports without
    an adapter: no Matrix adapter is registered, so
    ``_adapter_for_source`` returns None — ``/model`` with no args
    renders its text list instead of the Telegram/Discord picker, and
    destructive confirms use their text fallback. D13 scope
    (gateway-lifecycle verbs gateway-room-only) is enforced in the
    sidecar control router before inject, not here.

    Requires the live runner (None without one → caller falls back to a
    turn). The sync socket-thread context drives the coroutine with
    ``asyncio.run``; a running loop falls back to a turn rather than
    deadlocking on nested run. Never raises otherwise.
    """
    try:
        verb, _args = _parse_slash_verb(text)
        if not verb:
            return None
        from mercury_cli.commands import is_gateway_known_command, resolve_command

        cmd_def = resolve_command(verb)
        canonical = cmd_def.name if cmd_def is not None else verb
        if not is_gateway_known_command(canonical) and verb != "exit" and canonical != "exit":
            return None
        runner = _live_runner()
        if runner is None:
            return None
        try:
            from gateway.config import Platform
            from gateway.platforms.base import MessageEvent
            from gateway.session import SessionSource
        except Exception:
            return None
        clean = (text or "").strip()
        if not clean:
            return None
        source = SessionSource(
            platform=Platform.MATRIX,
            chat_id="gateway",
            chat_type="dm",
        )
        try:
            session_key = runner._session_key_for_source(source)
        except Exception:
            session_key = ""
        try:
            meta: dict[str, object] = {
                "gateway_session_id": GATEWAY_SESSION_ID,
                "gateway_session_key": session_key,
            }
            if node_id:
                meta["observatory_node_id"] = str(node_id)
            if room_id:
                meta["observatory_room_id"] = str(room_id)
            event = MessageEvent(
                text=clean,
                source=source,
                metadata=meta,
                internal=True,
            )
        except Exception:
            logger.debug("gateway_session: matrix event synth failed", exc_info=True)
            return None
        try:
            coro = runner._handle_message(event)
        except Exception:
            logger.debug("gateway_session: runner dispatch failed for /%s", verb, exc_info=True)
            return None
        try:
            if asyncio.iscoroutine(coro):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    # Async caller hitting this path falls back to a turn
                    # rather than deadlocking on a nested run.
                    try:
                        coro.close()
                    except Exception:
                        pass
                    return None
                result = asyncio.run(coro)
            else:
                result = coro
        except Exception:
            logger.debug("gateway_session: runner command failed for /%s", verb, exc_info=True)
            return None
        if result is None:
            return ""
        # EphemeralReply and friends stringify to their text.
        return str(result)
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


def _strip_thinking_markup(text: Any) -> str:
    """Blockquote-aware compare form (FOLLOW-UP A): Element X renders
    thinking as a grey blockquote, so a thinking event carrying ``> foo``
    (markdown) or ``<blockquote>…foo…</blockquote>`` (HTML) must still match
    a final reply of ``foo``. Strips HTML tags, unescapes entities, drops
    leading ``>`` markers per line, then normalizes whitespace."""
    import html as _html
    import re as _re

    s = str(text or "")
    s = _re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    lines = [_re.sub(r"^\s*(>\s*)+", "", ln) for ln in s.splitlines()]
    return " ".join(" ".join(lines).split())


def _push_progress(node_id: str, seq: int, event: dict[str, Any], *, internal: bool = False) -> None:
    """Fire-and-forget one live-progress datagram; never raises.

    Payload is ``{node_id, seq, event}`` where ``event`` is the existing
    shape (no ``seq`` inside — it rides beside it). No listener, missing
    socket dir, or any send error = drop silently. ``internal=True`` marks
    follow-up turns (quiet gates only the final reply — the live stream
    renders like a normal turn; seqs still feed the replay dedupe).
    """
    try:
        payload_obj: dict[str, Any] = {"node_id": node_id, "seq": seq, "event": event}
        if internal:
            payload_obj["internal"] = True
        payload = json.dumps(payload_obj).encode("utf-8")
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


def _send_child_datagram(payload: dict[str, Any]) -> None:
    """Fire-and-forget one child-feed datagram; never raises."""
    try:
        raw = json.dumps(payload).encode("utf-8")
    except Exception:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(raw, str(_progress_socket_path()))
        finally:
            try:
                sock.close()
            except Exception:
                pass
    except Exception:
        pass


def push_approval_prompt(
    node_id: str,
    *,
    request_id: str,
    command: str,
    description: str = "",
    session_key: str = GATEWAY_APPROVAL_SESSION_KEY,
) -> None:
    """Fire-and-forget one approval-prompt datagram; never raises.

    Same best-effort law as the other gateway→sidecar datagrams: no
    listener or send error = drop silently (the turn still blocks in the
    gateway queue until timeout — fail-closed deny — it just never
    surfaces in the room)."""
    _send_child_datagram({
        "kind": APPROVAL_PROMPT_KIND,
        "node_id": node_id,
        "request_id": request_id,
        "command": command,
        "description": description,
        "session_key": session_key,
    })


def gateway_approval_notify(node_id: str):
    """``tools.approval.register_gateway_notify`` callback for gateway
    Matrix turns: mirrors the guard prompt to the sidecar over the
    progress socket. Runs on the blocked agent thread; the send is
    fire-and-forget datagram I/O (the socket pattern the
    register_gateway_notify docstring prescribes — never block here)."""
    def _notify(approval_data) -> None:
        try:
            data = dict(approval_data or {})
        except Exception:
            return
        push_approval_prompt(
            node_id,
            request_id=str(data.get("request_id") or ""),
            command=str(data.get("command") or ""),
            description=str(data.get("description") or ""),
            session_key=str(data.get("session_key") or GATEWAY_APPROVAL_SESSION_KEY),
        )
    return _notify


def push_child_lifecycle(
    node_id: str,
    lifecycle: str,
    *,
    name: str | None = None,
    goal: str | None = None,
    delegation_id: str | None = None,
    task_index: int | None = None,
    parent_session: str | None = None,
    status: str | None = None,
    summary: str | None = None,
) -> None:
    """Push one child-lifecycle datagram; never raises.

    ``lifecycle`` is ``"start"`` (entry appeared in the gateway's own
    ``_live_children`` table) or ``"stop"`` (entry disappeared — the
    watcher cannot know the terminal status, so stop carries
    ``status="unknown"`` and no summary; the post-delegate follow-up
    still verifies the result through the gateway turn).
    """
    payload: dict[str, Any] = {
        "kind": CHILD_LIFECYCLE_KIND,
        "node_id": node_id,
        "lifecycle": lifecycle,
    }
    if name is not None:
        payload["name"] = name
    if goal is not None:
        payload["goal"] = goal
    if delegation_id is not None:
        payload["delegation_id"] = delegation_id
    if task_index is not None:
        payload["task_index"] = task_index
    if parent_session is not None:
        payload["parent_session"] = parent_session
    if status is not None:
        payload["status"] = status
    if summary is not None:
        payload["summary"] = summary
    _send_child_datagram(payload)


def push_child_feed_event(node_id: str, feed_event: dict[str, Any]) -> None:
    """Push one forwarded child feed frame; never raises."""
    try:
        feed = dict(feed_event)
    except Exception:
        return
    _send_child_datagram(
        {"kind": CHILD_EVENT_KIND, "node_id": node_id, "feed": feed}
    )


def _feed_event_to_dict(event: Any) -> dict[str, Any] | None:
    """One ``OmpFeed`` typed event → datagram ``feed`` dict; None to skip.

    Message frames forward as ``feed="message"`` (the sidecar renders
    non-blank text into the child/grandchild room). Self frames
    (``subagent_id == ""`` — the child's OWN main-session tools/thoughts)
    forward unchanged; the sidecar maps the empty id to the child's own
    room. Unknown shapes are skipped; never raises.
    """
    try:
        import dataclasses

        if dataclasses.is_dataclass(event) and not isinstance(event, type):
            data = dataclasses.asdict(event)
            shape = type(event).__name__
        elif isinstance(event, dict):
            data = dict(event)
            shape = str(data.get("feed") or "")
        else:
            return None
    except Exception:
        return None
    try:
        # Message frames (role-bearing) forward as feed="message": they
        # share subagent_id/text keys with thought frames, so probe them
        # first. Blank text is filtered consumer-side.
        if shape == "MessageEvent" or "role" in data:
            data["feed"] = "message"
            return data
        if shape == "NodeEvent" or (
            "subagent_id" in data and "status" in data and "tool" not in data
            and "text" not in data
        ):
            data["feed"] = "node"
            return data
        if shape == "ToolEvent" or ("subagent_id" in data and "tool" in data):
            data["feed"] = "tool"
            return data
        if shape == "ThoughtEvent" or (
            "subagent_id" in data and "text" in data and "tool" not in data
        ):
            data["feed"] = "thought"
            return data
    except Exception:
        return None
    return None


def _snapshot_live_children() -> dict[str, dict[str, Any]]:
    """Copy the gateway's OWN live-child table (same-process read).

    Never raises — missing module/table reads as empty (gateways without
    omp delegation simply have no children to forward).
    """
    try:
        import tools.omp_delegation as _od
    except Exception:
        return {}
    try:
        table = getattr(_od, "_live_children", None)
        if not isinstance(table, dict):
            return {}
        lock = getattr(_od, "_live_children_lock", None)
        if lock is not None:
            with lock:
                items = list(table.items())
        else:
            items = list(table.items())
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for child_id, rec in items:
        if isinstance(rec, dict):
            out[str(child_id)] = rec
    return out


def _child_transport_feedable(transport: Any) -> bool:
    """True when an OmpFeed can subscribe to this child transport."""
    if transport is None:
        return False
    try:
        from observatory.omp_feed import OmpFeed

        OmpFeed._frame_source(transport)
        return True
    except Exception:
        return False


async def _forward_child_feed(
    child_id: str, transport: Any, feeds: dict[str, Any]
) -> None:
    """Subscribe one OmpFeed and push its frames as datagrams until cancelled."""
    try:
        from observatory.omp_feed import OmpFeed
    except Exception:
        logger.debug("child feed forwarder: no OmpFeed surface", exc_info=True)
        return
    feed = OmpFeed(transport)
    feeds[child_id] = feed
    try:
        try:
            await feed.start()
        except Exception:
            logger.debug("child feed subscribe failed for %s", child_id, exc_info=True)
            return
        try:
            async for typed in feed.events():
                try:
                    payload = _feed_event_to_dict(typed)
                except Exception:
                    continue
                if payload is None:
                    continue
                try:
                    push_child_feed_event(child_id, payload)
                except Exception:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("child feed consume failed for %s", child_id, exc_info=True)
    finally:
        try:
            await feed.stop()
        except Exception:
            pass
        feeds.pop(child_id, None)


async def _child_watcher_async(poll_interval: float = CHILD_FEED_POLL_S) -> None:
    """Poll the OWN live-child table; push lifecycle + forward feed frames."""
    try:
        interval = float(poll_interval)
    except Exception:
        interval = CHILD_FEED_POLL_S
    if interval <= 0:
        interval = CHILD_FEED_POLL_S
    known: dict[str, dict[str, Any]] = {}
    tasks: dict[str, Any] = {}
    feeds: dict[str, Any] = {}
    stops_pushed: set[str] = set()
    while True:
        try:
            snapshot = _snapshot_live_children()
        except Exception:
            snapshot = {}
        for child_id, meta in snapshot.items():
            if child_id in known:
                continue
            known[child_id] = meta
            stops_pushed.discard(child_id)
            try:
                task_index = meta.get("task_index")
                push_child_lifecycle(
                    child_id,
                    "start",
                    name=(str(meta.get("name")) if meta.get("name") is not None else None),
                    goal=(str(meta.get("goal")) if meta.get("goal") is not None else None),
                    delegation_id=(
                        str(meta.get("delegation_id"))
                        if meta.get("delegation_id") is not None else None
                    ),
                    task_index=(int(task_index) if isinstance(task_index, int) else None),
                    parent_session=(
                        str(meta.get("owner_session_id"))
                        if meta.get("owner_session_id") else None
                    ),
                )
            except Exception:
                logger.debug("child start push failed for %s", child_id, exc_info=True)
            try:
                transport = meta.get("transport")
            except Exception:
                transport = None
            if _child_transport_feedable(transport):
                try:
                    tasks[child_id] = asyncio.create_task(
                        _forward_child_feed(child_id, transport, feeds),
                        name=f"observatory-child-feed-{child_id}",
                    )
                except Exception:
                    logger.debug("child feed task spawn failed for %s", child_id, exc_info=True)
        for child_id in list(known):
            if child_id in snapshot:
                continue
            known.pop(child_id, None)
            task = tasks.pop(child_id, None)
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
                feed = feeds.pop(child_id, None)
                if feed is not None:
                    try:
                        await feed.stop()
                    except Exception:
                        pass
            if child_id not in stops_pushed:
                stops_pushed.add(child_id)
                try:
                    push_child_lifecycle(child_id, "stop", status="unknown")
                except Exception:
                    logger.debug("child stop push failed for %s", child_id, exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("child watcher sleep failed", exc_info=True)


def _child_watcher_main(poll_interval: float = CHILD_FEED_POLL_S) -> None:
    """Watcher thread body: own event loop, never raises out."""
    try:
        asyncio.run(_child_watcher_async(poll_interval))
    except Exception:
        logger.debug("child feed watcher exited", exc_info=True)


_watcher_guard = threading.Lock()
_watcher_state: dict[str, Any] = {"thread": None, "started": False}


def ensure_child_feed_watcher(
    *, poll_interval: float = CHILD_FEED_POLL_S
) -> bool:
    """Start the gateway-child feed forwarder thread (idempotent).

    Gateway boot calls this once beside the ``inject`` verb registration;
    the daemon thread owns one asyncio loop, polls the gateway's OWN
    ``_live_children`` table, and pushes lifecycle + feed datagrams the
    sidecar ingests cross-process. Best-effort: False when the thread
    could not start. Never raises.
    """
    try:
        with _watcher_guard:
            if _watcher_state.get("started"):
                thread = _watcher_state.get("thread")
                if thread is not None and thread.is_alive():
                    return True
            thread = threading.Thread(
                target=_child_watcher_main,
                args=(poll_interval,),
                name="observatory-child-feed",
                daemon=True,
            )
            _watcher_state["thread"] = thread
            _watcher_state["started"] = True
            thread.start()
            return True
    except Exception:
        logger.debug("child feed watcher did not start", exc_info=True)
        return False


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

    def __init__(self, node_id: str = "gw", *, internal: bool = False) -> None:
        self._node_id = node_id or "gw"
        self._internal = bool(internal)
        self._next_seq = 0
        self._records: list[dict[str, Any]] = []
        self._seen_thinking: set[str] = set()

    # -- capture ---------------------------------------------------------
    def _record(self, event: dict[str, Any]) -> None:
        seq = self._next_seq
        self._next_seq += 1
        stored = dict(event)
        stored["seq"] = seq
        if self._internal:
            stored["internal"] = True
        self._records.append(stored)
        _push_progress(self._node_id, seq, event, internal=self._internal)

    def _add_thinking(self, text: str) -> None:
        norm = _strip_thinking_markup(text)
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


def _run_gateway_prompt_with_events_inner(
    text: str,
    *,
    kind: str = "prompt",
    session_id: str = GATEWAY_SESSION_ID,
    node_id: str = "gw",
    room_id: str | None = None,
    agent_factory: Optional[Callable[[str], Any]] = None,
    turn: Optional[Callable[[Any, str], Any]] = None,
    slash_dispatch: Optional[Callable[..., Optional[str]]] = None,
    internal: bool = False,
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
    the final reply (blockquote-aware compare — the final message must not
    appear twice, once plain once as reasoning) are dropped here; the
    live datagrams for them already went out and keep their seqs, so
    final seqs may show gaps. ``internal=True`` marks follow-up turns:
    events carry ``internal`` (quiet gates only the final reply — live
    and replay render like a normal turn).
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
            try:
                out = dispatch(clean, node_id=node_id, room_id=room_id)
            except TypeError:
                # Test doubles with the legacy (text)->reply shape.
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
        collector = _TurnEventCollector(node_id=node_id, internal=internal)
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
    norm_reply = _strip_thinking_markup(reply)
    if norm_reply:
        events = [
            e
            for e in events
            if not (
                e.get("type") == "thinking"
                and _strip_thinking_markup(e.get("text", "")) == norm_reply
            )
        ]
    return reply, events


def run_gateway_prompt_with_events(
    text: str,
    *,
    kind: str = "prompt",
    session_id: str = GATEWAY_SESSION_ID,
    node_id: str = "gw",
    room_id: str | None = None,
    agent_factory: Optional[Callable[[str], Any]] = None,
    turn: Optional[Callable[[Any, str], Any]] = None,
    slash_dispatch: Optional[Callable[..., Optional[str]]] = None,
    internal: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one headless turn; return (reply text, batched display events).

    Thin scope around :func:`_run_gateway_prompt_with_events_inner`: the
    turn blocks in ``tools.approval`` under the canonical
    :data:`GATEWAY_APPROVAL_SESSION_KEY` with
    :func:`gateway_approval_notify` registered, so every guard prompt is
    forwarded to the sidecar (which mirrors it into the node's room and
    resolves this exact queue on /approve|/deny). Registration is undone
    when the turn ends (same register/unregister law as the gateway's own
    ``_run_agent_turn``). When ``tools.approval`` is unavailable the turn
    runs unwrapped (approvals take their existing default path)."""
    try:
        from tools.approval import (
            register_gateway_notify,
            reset_current_session_key,
            set_current_session_key,
            unregister_gateway_notify,
        )
    except Exception:
        return _run_gateway_prompt_with_events_inner(
            text, kind=kind, session_id=session_id, node_id=node_id,
            room_id=room_id, agent_factory=agent_factory, turn=turn,
            slash_dispatch=slash_dispatch, internal=internal,
        )
    token = None
    try:
        token = set_current_session_key(GATEWAY_APPROVAL_SESSION_KEY)
    except Exception:
        token = None
    registered = False
    try:
        register_gateway_notify(
            GATEWAY_APPROVAL_SESSION_KEY, gateway_approval_notify(node_id))
        registered = True
    except Exception:
        logger.debug("gateway_session: approval forward not registered", exc_info=True)
    try:
        return _run_gateway_prompt_with_events_inner(
            text, kind=kind, session_id=session_id, node_id=node_id,
            room_id=room_id, agent_factory=agent_factory, turn=turn,
            slash_dispatch=slash_dispatch, internal=internal,
        )
    finally:
        if registered:
            try:
                unregister_gateway_notify(GATEWAY_APPROVAL_SESSION_KEY)
            except Exception:
                pass
        if token is not None:
            try:
                reset_current_session_key(token)
            except Exception:
                pass


def run_gateway_prompt(
    text: str,
    *,
    kind: str = "prompt",
    session_id: str = GATEWAY_SESSION_ID,
    node_id: str = "gw",
    room_id: str | None = None,
    agent_factory: Optional[Callable[[str], Any]] = None,
    turn: Optional[Callable[[Any, str], Any]] = None,
    slash_dispatch: Optional[Callable[..., Optional[str]]] = None,
    internal: bool = False,
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
        room_id=room_id,
        agent_factory=agent_factory,
        turn=turn,
        slash_dispatch=slash_dispatch,
        internal=internal,
    )
    return reply


def _gateway_agent_turn_live(session_id: str, agent: Any) -> bool:
    """True when the cached agent has a turn that can drain a steer."""
    try:
        if _session_lock(session_id).locked():
            return True
    except Exception:
        pass
    model_active = getattr(agent, "_model_request_active", None)
    executing = getattr(agent, "_executing_tools", None)
    if model_active is None and executing is None:
        return True  # legacy agent: no liveness surface, fail open
    try:
        if model_active is not None and bool(model_active.is_set()):
            return True
    except Exception:
        pass
    try:
        if bool(executing):
            return True
    except Exception:
        pass
    try:
        if bool(getattr(agent, "_pending_redirect", None)):
            return True
    except Exception:
        pass
    return False


def steer_gateway_agent(text: str, *, session_id: str = GATEWAY_SESSION_ID) -> dict[str, Any]:
    """Steer the cached gateway-session agent mid-turn (gateway-room steer).
 
     Redirect-then-steer: tries ``agent.redirect(text)`` first so a steer
     landing mid-generation interrupts the live model request like the CLI
    ``interrupt`` path — a long generation with no tool calls would
    otherwise sit buffered until turn end. ``redirect()`` itself degrades
    to the steer buffer during tool execution, and when there is no live
    turn (or no redirect surface) this falls back to ``agent.steer(text)``
    with the same buffer semantics — but only when a turn is live enough
    to drain that buffer (turn lock held, model request active, tools
    executing, or a redirect already admitted). A cached-but-idle agent
    would absorb the text and report success while nothing drains it, so
    it reports ``steered=False`` and the caller queues a fresh turn.
    Never takes the
     per-session turn lock — a steer arriving mid-turn must reach the agent
     holding it, not queue behind it. No cached agent after a short
     build-window wait (idle, never prompted) reports ``steered=False`` so
     the caller falls back to a fresh prompt turn. Never raises:
     the control-socket envelope reports the outcome.
     """
    import time as _time

    clean = (text or "").strip()
    if not clean:
        return {"steered": False, "reason": "empty steer text"}
    agent = None
    deadline = _time.monotonic() + 2.0
    while True:
        with _locks_guard:
            agent = _session_agents.get(session_id)
        if agent is not None:
            break
        # Build-window race: the inject handler caches the agent only after
        # taking the turn lock and building it (config + runtime, seconds).
        # A steer landing in that window must wait for the agent, not
        # report idle and fall back to a second turn blocked on the lock.
        if _time.monotonic() >= deadline:
            return {"steered": False, "reason": "idle — nothing to steer"}
        _time.sleep(0.05)
    redirect = getattr(agent, "redirect", None)
    if callable(redirect):
        try:
            if bool(redirect(clean)):
                return {"steered": True, "reason": ""}
        except Exception as exc:
            logger.warning("gateway_session: redirect failed, trying steer: %s", exc)
    if not _gateway_agent_turn_live(session_id, agent):
        return {"steered": False, "reason": "no live turn — queue a fresh turn"}
    steer = getattr(agent, "steer", None)
    if not callable(steer):
        return {"steered": False, "reason": "agent has no steer surface"}
    try:
        accepted = steer(clean)
    except Exception as exc:
        logger.warning("gateway_session: steer failed: %s", exc)
        return {"steered": False, "reason": f"steer failed: {exc}"}
    if accepted is False:
        return {"steered": False, "reason": "steer not accepted"}
    return {"steered": True, "reason": ""}


def interrupt_gateway_agent(reason: str = "matrix /stop", *, session_id: str = GATEWAY_SESSION_ID) -> dict[str, Any]:
    """Interrupt the cached gateway-session agent (BUG3 /stop wiring).

    Calls ``agent.interrupt(reason, hard_cancel=True)`` on the live cached
    agent when present; no cached agent (idle, never prompted) is success
    with ``interrupted=False`` — there is nothing to stop. Never raises:
    the control-socket envelope reports the outcome dict.
    """
    with _locks_guard:
        agent = _session_agents.get(session_id)
    if agent is None:
        return {"interrupted": False, "reason": "idle — nothing to stop"}
    interrupt = getattr(agent, "interrupt", None)
    if not callable(interrupt):
        return {"interrupted": False, "reason": "agent has no interrupt surface"}
    try:
        try:
            interrupt(reason, hard_cancel=True)
        except TypeError:
            interrupt(reason)
    except Exception as exc:
        logger.warning("gateway_session: interrupt failed: %s", exc)
        return {"interrupted": False, "reason": f"interrupt failed: {exc}"}
    return {"interrupted": True, "reason": str(reason or "")}
