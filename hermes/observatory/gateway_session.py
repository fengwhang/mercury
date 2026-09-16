"""Gateway-process side of IRC observatory prompt delivery.

The gateway agent's hermes session lives in (and is resumed by) the
gateway process itself — boot resync deliberately never builds a
handle for it (SKIP_RESPAWN_KINDS). A gateway-room prompt may also
arrive over the gateway control socket (``gateway.control_socket``
``inject`` verb); the gateway answers it with :func:`run_gateway_prompt`
here.

Execution is a headless AIAgent turn on a STABLE session id
(``GATEWAY_SESSION_ID``) — the same machinery ``mercury -z`` uses
(``mercury_cli.oneshot``: config-resolved runtime + ``run_conversation``),
except the agent is cached per session so consecutive IRC messages share
one transcript. Turns are serialized per session: a headless session is
always idle between turns, so steer-vs-prompt (a busy-session distinction)
collapses — every injection starts a turn, and ``kind`` selects the entry
path: ``prompt``/``steer`` run the agent turn directly, while ``command``
first tries the gateway slash dispatch (the same table
``GatewayRunner._handle_message`` uses) and falls back to a turn for
unknown verbs.

The gateway-session agent is built with ``platform="irc"`` so the
system prompt picks the plain-text hint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

#: Queue ``kind`` values for gateway→rooms frames. The room pump reads
#: its OWN process queue (same process — no socket hop).
TURN_PROGRESS_KIND = "turn_progress"
#: ``{"kind": "child_lifecycle", "node_id", "lifecycle": "start"|"stop",

#: Canonical ``tools.approval`` session key for gateway turns
#: (``session:gateway`` — the gateway node's ``session_ref`` in state.db).
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

    Same builder boot resync uses for spawned hermes orchestrators
    (dedicated SessionDB handle on the home's hermes state.db — never a
    borrowed live object). The turn prologue resolves the
    compression-lineage tip and loads prior history, so a stored
    session resumes and a missing one starts fresh.

    The gateway session renders into an IRC channel, so it is built with
    ``platform="irc"`` (plain-text hint, no markdown).
    """
    from observatory.spawn import build_hermes_agent

    return build_hermes_agent(session_id=session_id, platform="irc")


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
            logger.debug("gateway_session: event synth failed", exc_info=True)
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
    """Fire-and-forget one live-progress frame into the rooms queue.

    Maps collector shapes to feed frames (gateway_session is the gateway
    room's trace source). Never raises; no listener = drop silently.
    """
    try:
        kind = str((event or {}).get("type") or "")
        if kind == "thinking":
            feed = {"feed": "thought", "subagent_id": "",
                    "text": str(event.get("text") or "")}
        elif kind == "tool_call":
            feed = {"feed": "tool", "subagent_id": "",
                    "tool": str(event.get("tool") or "tool"),
                    "args": event.get("args") or {}}
        else:
            return
        from observatory.rooms import (
            channel_for_node_id, format_frame, say_nowait,
        )

        channel = channel_for_node_id(node_id)
        if channel:
            line = format_frame(feed)
            if line:
                say_nowait(channel, line)
    except Exception:
        pass
def gateway_approval_notify(node_id: str):
    """``tools.approval.register_gateway_notify`` callback for gateway
    turns: mirrors the guard prompt into the room. Runs on the
    blocked agent thread; the send is fire-and-forget (never block)."""
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


def push_approval_prompt(
    node_id: str,
    *,
    request_id: str,
    command: str,
    description: str = "",
    session_key: str = GATEWAY_APPROVAL_SESSION_KEY,
) -> None:
    """Fire-and-forget one approval-prompt line; never raises.

    Direct to the room (no queue): resolves the channel from state and
    sends via the loop-hop. Runs on the blocked agent thread.
    """
    try:
        from observatory.rooms import channel_for_node_id, say_nowait

        channel = channel_for_node_id(node_id)
        if channel:
            say_nowait(
                channel,
                f"approval requested: `{command}`"
                f" — {description} "
                f"(reply /approve or /deny in the parent room)",
            )
    except Exception:
        pass


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


#: Live-forward registries for the batched turn replay. The watcher records
#: each child's forwarding OmpFeed (for a synchronous listener detach at
#: replay time — no NEW frames queue after the turn) and a TurnFrameDedupe
#: (live-vs-replay multiset: the replay pushes only the occurrences the
#: live path missed). Guarded by ``_child_feed_lock`` (watcher loop vs
#: delegation worker threads). Entries die with the forward task.
_child_feed_lock = threading.Lock()
_child_live_feeds: dict[str, Any] = {}
_child_dedupe: dict[str, Any] = {}


def replay_child_turn_frames(child_id: str, frames: Any) -> int:
    """Push the batched turn's SELF frames the live path missed.

    ``frames`` are the transport's JSON-safe ``turn_frames`` dicts. Only
    tool/thought/message frames for the child itself replay (grandchildren
    rely on the live path — server-gated from task start by the synchronous
    subscribe in the child_started hook). The multiset skips occurrences the
    live forwarder already pushed, in turn order; a frame live-forwarded
    AFTER this runs is likewise skipped. Returns pushed count. Never raises.
    """
    try:
        cid = str(child_id or "")
        wanted = [
            f for f in (frames or [])
            if isinstance(f, dict) and f.get("feed") in ("tool", "thought", "message")
            and not f.get("subagent_id")
        ]
        if not cid or not wanted:
            return 0
        from observatory.omp_feed import TurnFrameDedupe, child_frame_key

        with _child_feed_lock:
            dd = _child_dedupe.get(cid)
            if dd is None:
                dd = TurnFrameDedupe()
                _child_dedupe[cid] = dd
            feed = _child_live_feeds.get(cid)
            if feed is not None:
                for attr in ("_dispose_listener", "_dispose_agent_listener"):
                    try:
                        dispose = getattr(feed, attr, None)
                        if callable(dispose):
                            dispose()
                    except Exception:
                        pass
                    try:
                        setattr(feed, attr, None)
                    except Exception:
                        pass
            keys = [child_frame_key(f) for f in wanted]
            surplus = dd.replay_indexes(keys)
            try:
                from observatory.rooms import (
                    channel_for_node_id, format_frame, say_nowait,
                )

                channel = channel_for_node_id(cid)
            except Exception:
                channel = ""
            for i in surplus:
                try:
                    if not channel:
                        continue
                    line = format_frame(wanted[i])
                    if line:
                        say_nowait(channel, line)
                except Exception:
                    continue
            return len(surplus)
    except Exception:
        logger.debug("child turn replay failed for %s", child_id, exc_info=True)
        return 0


def _grandchild_name(feed: dict[str, Any], sid: str) -> str:
    name = (str(feed.get("agent") or "").strip()
            or str(feed.get("task") or "").strip())
    return name or f"sub-{sid[:8]}"


def _publish_live_payload(
    child_id: str, payload: dict[str, Any], grands: dict[str, str] | None,
) -> None:
    """One live feed frame into its room (watcher thread)."""
    from observatory.rooms import format_frame, say_nowait

    try:
        if not isinstance(payload, dict):
            return
        sub = str(payload.get("subagent_id") or "")
        manager = _watcher_manager()
        if manager is None:
            return
        if str(payload.get("feed") or "") == "node":
            if sub:
                _route_grandchild_frame(
                    manager, child_id, payload, grands or {})
            return
        if sub:
            _route_grandchild_frame(
                manager, child_id, payload, grands or {})
            return
        try:
            channel = manager.channel_for_node(child_id)
        except Exception:
            channel = ""
        if not channel:
            return
        line = format_frame(payload)
        if line:
            say_nowait(channel, line)
    except Exception:
        pass


def _route_grandchild_frame(
    manager, owner_id: str, feed: dict[str, Any],
    cache: dict[str, str],
) -> None:
    """One N>1 frame into its own room (watcher thread)."""
    from observatory.rooms import format_frame, say_nowait

    try:
        sid = str(feed.get("subagent_id") or "")
        if not sid:
            return
        kind = str(feed.get("kind") or "")
        if kind in ("add", "death"):
            if kind == "add":
                channel = cache.get(sid)
                if not channel:
                    node_id = f"{owner_id}/sub-{sid}"
                    channel = _hop(manager._ensure_child_room_for(node_id, {
                        "name": _grandchild_name(feed, sid),
                        "parent_name": owner_id,
                        "engine": "omp",
                        "subagent_id": sid,
                        "session_ref": str(feed.get("session_file") or node_id),
                    })) or ""
                    if channel:
                        cache[sid] = channel
                if channel:
                    flat = dict(feed)
                    flat["subagent_id"] = ""
                    line = format_frame(flat)
                    if line:
                        say_nowait(channel, line)
            else:
                node_id = f"{owner_id}/sub-{sid}"
                try:
                    row_channel = manager.channel_for_node(node_id)
                except Exception:
                    row_channel = ""
                if row_channel:
                    flat = dict(feed)
                    flat["subagent_id"] = ""
                    line = format_frame(flat)
                    if line:
                        say_nowait(row_channel, line)
                cache.pop(sid, None)
                _hop(manager._retire_child_room(node_id))
            return
        channel = cache.get(sid)
        if not channel:
            node_id = f"{owner_id}/sub-{sid}"
            try:
                row_channel = manager.channel_for_node(node_id)
            except Exception:
                row_channel = ""
            if row_channel:
                channel = row_channel
                cache[sid] = channel
            else:
                channel = _hop(manager._ensure_child_room_for(node_id, {
                    "name": _grandchild_name(feed, sid),
                    "parent_name": owner_id,
                    "engine": "omp",
                    "subagent_id": sid,
                    "session_ref": node_id,
                })) or ""
                if channel:
                    cache[sid] = channel
        if channel:
            flat = dict(feed)
            flat["subagent_id"] = ""
            line = format_frame(flat)
            if line:
                say_nowait(channel, line)
    except Exception:
        pass


async def _forward_child_feed(
    child_id: str, transport: Any, feeds: dict[str, Any],
    grands: dict[str, str] | None = None,
) -> None:
    """Subscribe one OmpFeed and publish its frames direct until cancelled.

    SELF frames go to the child's own room; grandchild (N>1) frames get
    their own chained rooms. SELF frames consult the turn multiset (a
    frame the batched replay already covered is skipped). Direct sends
    only — no queue. Runs on the watcher loop; sends hop threads.
    """
    try:
        from observatory.omp_feed import OmpFeed
    except Exception:
        logger.debug("child feed forwarder: no OmpFeed surface", exc_info=True)
        return
    feed = OmpFeed(transport)
    feeds[child_id] = feed
    try:
        from observatory.omp_feed import TurnFrameDedupe
    except Exception:
        TurnFrameDedupe = None  # type: ignore[assignment]
    try:
        with _child_feed_lock:
            dd = _child_dedupe.get(child_id)
            if dd is None and TurnFrameDedupe is not None:
                dd = TurnFrameDedupe()
                _child_dedupe[child_id] = dd
            _child_live_feeds[child_id] = feed
    except Exception:
        dd = None
        logger.debug("child feed registry failed for %s", child_id, exc_info=True)
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
                    if (
                        dd is not None
                        and payload.get("feed") in ("tool", "thought", "message")
                        and not payload.get("subagent_id")
                    ):
                        from observatory.omp_feed import child_frame_key

                        skip = False
                        try:
                            with _child_feed_lock:
                                skip = bool(dd.live_hit(child_frame_key(payload)))
                        except Exception:
                            skip = False
                        if skip:
                            continue
                    _publish_live_payload(child_id, payload, grands)
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
        try:
            with _child_feed_lock:
                if _child_live_feeds.get(child_id) is feed:
                    _child_live_feeds.pop(child_id, None)
                _child_dedupe.pop(child_id, None)
        except Exception:
            pass


def _ensure_watcher_room(child_id: str, meta: dict[str, Any]) -> str:
    """Create the delegate room inline (watcher thread).

    Row + join + subscribe + invite + start line, via the gateway loop
    where transport is involved. Returns the channel ("" when unavailable).
    Never raises.
    """
    try:
        from observatory.rooms import format_lifecycle, say_nowait

        manager = _watcher_manager()
        if manager is None:
            return ""
        item = {
            "name": str(meta.get("name") or child_id),
            "parent_name": str(meta.get("owner_session_id") or ""),
            "engine": "omp",
            "session_ref": child_id,
        }
        channel = _hop(manager._ensure_child_room_for(child_id, item)) or ""
        if channel:
            logger.info("observatory: room ensured %s for %s", channel, child_id)
            say_nowait(channel, format_lifecycle(
                "start", name=str(meta.get("name") or child_id)))
        return channel
    except Exception:
        return ""


def _retire_watcher_room(child_id: str, *, name=None, summary: str = "") -> None:
    """Retire a finished delegate room inline (watcher thread)."""
    try:
        from observatory.rooms import format_lifecycle, say_nowait

        manager = _watcher_manager()
        if manager is None:
            return
        channel = ""
        try:
            channel = manager.channel_for_node(child_id)
        except Exception:
            pass
        if channel:
            say_nowait(channel, format_lifecycle(
                "stop", name=str(name or child_id), summary=summary))
        _hop(manager._retire_child_room(child_id, summary=summary))
    except Exception:
        pass


def _register_watcher_steer(child_id: str, meta: dict[str, Any]) -> None:
    """Register the room steerer for a live omp child (best-effort).

    RPC transports steer mid-run; one-shot Popen transports (steerable
    False, no ``steer`` method) get no room steering — their room stays
    a read-only trace. Never raises.
    """
    try:
        from observatory.rooms import drop_child_steer, register_child_steer
        transport = (meta or {}).get("transport")
        if not bool((meta or {}).get("steerable", False)):
            drop_child_steer(child_id)
            return
        if transport is None or not callable(getattr(transport, "steer", None)):
            drop_child_steer(child_id)
            return

        def _steer(text: str, _transport: Any = transport) -> bool:
            try:
                _transport.steer(text)
                return True
            except Exception:
                return False

        register_child_steer(child_id, _steer)
    except Exception:
        pass


def _hop(coro, timeout: float = 30.0):
    """Run a rooms coroutine on the gateway loop; return its result.

    Watcher/worker threads only — blocking on the loop's own thread
    would deadlock, so that misuse degrades loudly instead of hanging.
    Timeout/failure reads as None (best-effort). Never raises.
    """
    try:
        import asyncio as _asyncio

        from observatory.rooms import _loop_now, call_soon

        try:
            running = _asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is _loop_now():
            logger.warning("observatory: hop from the gateway loop itself")
            try:
                if coro is not None:
                    coro.close()
            except Exception:
                pass
            return None
        fut = call_soon(coro)
        if fut is None:
            return None
        return fut.result(timeout=timeout)
    except Exception:
        return None


def _watcher_manager():
    """Live room manager for the watcher thread (global, never builds)."""
    try:
        from observatory.rooms import get_room_manager

        return get_room_manager()
    except Exception:
        return None


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
    grands: dict[str, dict[str, str]] = {}
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
            logger.info("observatory: watcher child start %s", child_id)
            logger.info("observatory: watcher child start %s", child_id)
            try:
                _ensure_watcher_room(child_id, meta)
                _register_watcher_steer(child_id, meta)
            except Exception:
                logger.debug("child start failed for %s", child_id, exc_info=True)
            try:
                transport = meta.get("transport")
            except Exception:
                transport = None
            if _child_transport_feedable(transport):
                try:
                    grands.setdefault(child_id, {})
                    tasks[child_id] = asyncio.create_task(
                        _forward_child_feed(
                            child_id, transport, feeds, grands[child_id]),
                        name=f"observatory-child-feed-{child_id}",
                    )
                except Exception:
                    logger.debug("child feed task spawn failed for %s", child_id, exc_info=True)
        for child_id in list(known):
            if child_id in snapshot:
                continue
            gone_meta = known.pop(child_id, None) or {}
            try:
                from observatory.rooms import drop_child_steer
                drop_child_steer(child_id)
            except Exception:
                pass
            grands.pop(child_id, None)
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
                logger.info("observatory: watcher child stop %s", child_id)
                logger.info("observatory: watcher child stop %s", child_id)
                try:
                    _retire_watcher_room(
                        child_id, name=gone_meta.get("name"))
                except Exception:
                    logger.debug("child stop failed for %s", child_id, exc_info=True)
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
    forwarded into the rooms queue (mirrored into the node's channel). Registration is undone
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


def interrupt_gateway_agent(reason: str = "irc /stop", *, session_id: str = GATEWAY_SESSION_ID) -> dict[str, Any]:
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
