"""M3b (matrix observatory §7/§5): OmpFeed — typed events from a live omp child.

Given a live ``OmpRpcChild`` (``tools/omp_rpc_transport.py`` — or any thin
wrapper exposing the same surface), the feed:

1. subscribes to omp's subagent frames (``set_subagent_subscription``
   level ``"events"``) and consumes them off the vendored client's
   unknown-notification channel (``subagent_lifecycle`` /
   ``subagent_progress`` / ``subagent_event`` frames arrive there — the
   python client predates them, so they surface as
   ``UnknownNotification.payload`` dicts);
2. translates them into typed events on an asyncio queue:

   * ``subagent_lifecycle``  status started → ``NodeEvent(kind="add")``;
     completed/failed/aborted → ``NodeEvent(kind="death")`` — both carry
     the ``parent_tool_call_id`` linkage that nests a grandchild under the
     child's Task tool call;
   * ``subagent_progress``   current tool + args → ``ToolEvent`` (emitted
     on change — progress frames also churn status/tokens/cost, which are
     not tool transitions; completed tools additionally surface through
     the ``recentTools`` tail, which is what the live wire reliably
     carries for fast tools);
   * ``subagent_event``      ``thinking_delta`` payloads accumulate per
     content block and flush as one ``ThoughtEvent`` on ``thinking_end``
     (a ``message_end`` with no prior end flushes defensively);
     ``message_end`` → ``MessageEvent`` with the concatenated text blocks
     (thinking blocks belong to ``ThoughtEvent``, toolCall blocks to
     ``ToolEvent``);
3. ALSO subscribes to the child's MAIN session agent events (the vendored
   ``RpcClient.on_event`` channel: ``tool_execution_start``,
   ``message_update``, ``message_end``) and translates them the same way
   with ``subagent_id == ""`` — the empty id means "the child itself", so
   the delegated task's own tool calls and thinking stream into the
   CHILD's own room (§5: one message per tool call, thinking as separate
   messages) instead of only its grandchildren's rooms. Without this the
   gateway-origin child room shows lifecycle only (BUG2-SUBAGENT-TRACE):
   ``run_task`` is prompt-and-wait, so main-session activity has no other
   live source;
4. keeps per-subagent byte offsets for ``get_subagent_messages``
   catch-up: session files are learned from lifecycle/progress frames,
   ``catch_up()`` reads transcripts incrementally (``fromByte=nextByte``),
   and ``offsets()`` snapshots ``{subagent_id: {session_file, next_byte}}``
   so a NEW feed on a reconnected child can resume exactly where the old
   one stopped (``restore_offsets``). The self stream (``""``) holds no
   transcript offset and is excluded from snapshots.

The listeners run on the client's reader thread; events cross onto the
loop via ``call_soon_threadsafe``, so hooking the queue from async code
is race-free and ordered.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

SUBSCRIPTION_LEVEL = "events"

# subagent_lifecycle payload statuses that end a grandchild's life.
_TERMINAL_LIFECYCLE = frozenset({"completed", "failed", "aborted"})


# ---------------------------------------------------------------------------
# Typed events (omp-side vocabulary; discovery.NodeEvent is the
# delegation-keyed sibling — the M3a tree module unifies them).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeEvent:
    """Grandchild (omp in-process subagent) add/death."""

    kind: str  # "add" | "death"
    subagent_id: str
    parent_tool_call_id: Optional[str]
    status: str  # add: "running"; death: completed|failed|aborted
    seq: int = 0
    index: Optional[int] = None
    agent: Optional[str] = None
    task: Optional[str] = None
    session_file: Optional[str] = None


@dataclass(frozen=True)
class ToolEvent:
    """The subagent's current tool invocation (from progress frames).

    ``subagent_id == ""`` means the CHILD ITSELF (its main omp session,
    from ``tool_execution_start`` agent events) — render into the child's
    own room, not a grandchild room.
    """

    subagent_id: str
    tool: str
    args: Optional[str]
    seq: int = 0
    parent_tool_call_id: Optional[str] = None


@dataclass(frozen=True)
class ThoughtEvent:
    """One accumulated thinking block (deltas → complete text).

    ``subagent_id == ""`` means the CHILD ITSELF (its main omp session).
    """

    subagent_id: str
    text: str
    seq: int = 0


@dataclass(frozen=True)
class MessageEvent:
    """A completed assistant/user message (text blocks only).

    ``subagent_id == ""`` means the CHILD ITSELF (its main omp session).
    """

    subagent_id: str
    role: str
    text: str
    seq: int = 0


FeedEvent = object  # NodeEvent | ToolEvent | ThoughtEvent | MessageEvent


@dataclass
class _SubagentState:
    """Per-grandchild bookkeeping (reader thread + loop thread)."""

    session_file: Optional[str] = None
    parent_tool_call_id: Optional[str] = None
    next_byte: int = 0
    # (tool, args) of the last emitted ToolEvent — change detection.
    last_tool: Optional[tuple] = None
    # (tool, args, endMs) of recentTools entries already emitted.
    seen_recent: set = field(default_factory=set)
    # contentIndex -> accumulated thinking deltas for the open block.
    thinking: Dict[int, str] = field(default_factory=dict)


class OmpFeed:
    """Typed event stream for the subagents of one live omp RPC child.

    The ``child`` exposes the subagent surface directly
    (``set_subagent_subscription(level)`` /
    ``get_subagent_messages(subagent_id=..., from_byte=...)``) or carries
    the vendored ``RpcClient`` as ``_client`` — commands then go as raw
    frames with wire keys — plus a frame source: either an
    ``on_unknown_notification(listener)`` method itself or a private
    ``_client`` attribute carrying one (``OmpRpcChild``).

    Lifecycle::
        feed = OmpFeed(child)
        await feed.start()                      # subscribes + listens
        async for event in feed.events():       # typed events
            ...
        await feed.stop()
    """

    def __init__(self, child: Any, *, queue: Optional[asyncio.Queue] = None) -> None:
        self._child = child
        self._queue: asyncio.Queue = queue if queue is not None else asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dispose_listener = None
        self._dispose_agent_listener = None
        self._seq = 0
        self._states: Dict[str, _SubagentState] = {}
        # Raw wire-frame counters by type (ops/diagnostics — the LIVE
        # gate reports these to prove which frame kinds flowed).
        self.frame_counts: Dict[str, int] = {}
        self._stopped = False

    # -- child surface resolution --------------------------------------

    @staticmethod
    def _frame_source(child: Any):
        """Resolve an ``on_unknown_notification`` registrar from the child."""
        for attr in ("on_unknown_notification", "_client", "rpc_client", "client"):
            obj = getattr(child, attr, None)
            registrar = getattr(obj, "on_unknown_notification", None)
            if obj is not None and callable(registrar):
                return obj
        raise TypeError(
            "OmpFeed needs a child exposing on_unknown_notification "
            "(directly or via _client/rpc_client) — got "
            f"{type(child).__name__}"
        )

    def _child_rpc(self, command: str, direct_kwargs: Dict[str, Any],
                   wire_kwargs: Dict[str, Any]) -> Any:
        """Invoke a subagent RPC command on the child.

        A double exposing ``command`` directly (the M1 observer surface)
        takes it with ``direct_kwargs``; otherwise the command goes over
        the vendored RpcClient as a raw frame (``child._client.request_raw``)
        with wire (camelCase) keys — ``OmpRpcChild`` itself exposes no
        subagent methods (spec §7: the transport is extended separately).
        """
        direct = getattr(self._child, command, None)
        if callable(direct):
            return direct(**direct_kwargs)
        inner = getattr(self._child, "_client", None)
        request_raw = getattr(inner, "request_raw", None)
        if callable(request_raw):
            return request_raw(command, **wire_kwargs)
        method = getattr(inner, command, None)
        if callable(method):
            return method(**direct_kwargs)
        raise TypeError(
            f"OmpFeed needs a child exposing {command} "
            "(directly or via _client.request_raw) — got "
            f"{type(self._child).__name__}"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, *, level: str = SUBSCRIPTION_LEVEL) -> None:
        """Subscribe to subagent frames and begin consuming them."""
        if self._dispose_listener is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        # Listener FIRST, then subscribe: the server starts emitting the
        # moment the subscription lands, so any frames racing between the
        # subscribe response and a later listener attach would be dropped
        # by the empty listener list. With the listener already in place,
        # every post-subscription frame is captured; pre-subscription
        # frames don't exist (the level gates them server-side).
        source = self._frame_source(self._child)
        self._dispose_listener = source.on_unknown_notification(self._on_notification)
        # Main-session agent events (the child's OWN tools/thinking — the
        # prompt-and-wait run_task has no other live source). Optional:
        # thin doubles exposing only on_unknown_notification skip this.
        on_event = getattr(source, "on_event", None)
        if callable(on_event):
            try:
                self._dispose_agent_listener = on_event(self._on_agent_event)
            except Exception:
                logger.debug("observatory omp_feed: agent-event listen failed", exc_info=True)
                self._dispose_agent_listener = None
        self._child_rpc("set_subagent_subscription", {"level": level}, {"level": level})

    async def stop(self) -> None:
        """Detach the listener (subscription level left as-is)."""
        self._stopped = True
        dispose, self._dispose_listener = self._dispose_listener, None
        if dispose is not None:
            dispose()
        agent_dispose, self._dispose_agent_listener = self._dispose_agent_listener, None
        if agent_dispose is not None:
            try:
                agent_dispose()
            except Exception:
                logger.debug("observatory omp_feed: agent-event detach failed", exc_info=True)
        if self._loop is not None and not self._loop.is_closed():
            await asyncio.sleep(0)
            self._queue.put_nowait(None)  # type: ignore[arg-type]

    def events(self):
        """Async iterator over typed events; ends after ``stop()``."""
        return self._events()

    async def _events(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    # ------------------------------------------------------------------
    # Byte-offset bookkeeping (get_subagent_messages catch-up)
    # ------------------------------------------------------------------

    def _state(self, subagent_id: str) -> _SubagentState:
        state = self._states.get(subagent_id)
        if state is None:
            state = _SubagentState()
            self._states[subagent_id] = state
        return state

    def note_session(self, subagent_id: str, session_file: Optional[str]) -> None:
        """Record a subagent's transcript path when a frame names it."""
        if not session_file:
            return
        self._state(subagent_id).session_file = session_file

    def offsets(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot ``{subagent_id: {session_file, next_byte}}``."""
        return {
            sid: {"session_file": s.session_file, "next_byte": s.next_byte}
            for sid, s in self._states.items()
            if sid  # the self stream ("") holds thinking/tool state only
        }

    def restore_offsets(self, saved: Mapping[str, Mapping[str, Any]]) -> None:
        """Resume from a previous feed's ``offsets()`` after reconnect."""
        for sid, entry in (saved or {}).items():
            state = self._state(str(sid))
            state.next_byte = int(entry.get("next_byte") or 0)
            sf = entry.get("session_file")
            state.session_file = str(sf) if sf else state.session_file

    def catch_up(self, subagent_id: str) -> Dict[str, Any]:
        """One incremental transcript read for a subagent.

        Calls ``get_subagent_messages`` with the stored ``from_byte`` and
        advances it to the server-reported ``nextByte``. The response is
        the raw server result (``entries``/``messages`` for the renderer).
        """
        state = self._state(subagent_id)
        result = self._child_rpc(
            "get_subagent_messages",
            {"subagent_id": subagent_id, "session_file": state.session_file,
             "from_byte": state.next_byte},
            {"subagentId": subagent_id, "sessionFile": state.session_file,
             "fromByte": state.next_byte},
        )
        nxt = result.get("nextByte")
        if isinstance(nxt, int):
            state.next_byte = nxt
        sf = result.get("sessionFile")
        if sf:
            state.session_file = str(sf)
        if result.get("reset"):
            # Server rotated the transcript (new session file): restart
            # from the beginning next time rather than skipping bytes.
            state.next_byte = 0
        return result

    # ------------------------------------------------------------------
    # Frame consumption (client reader thread)
    # ------------------------------------------------------------------

    def _on_notification(self, notification: Any) -> None:
        """Listener body — runs on the RpcClient reader thread."""
        try:
            events = self._translate(getattr(notification, "payload", None))
        except Exception:  # noqa: BLE001 — one bad frame must not kill the feed
            logger.exception("observatory omp_feed: frame translation failed")
            return
        if not events:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._drain, events)

    def _drain(self, events) -> None:
        for event in events:
            self._seq += 1
            try:
                event = replace(event, seq=self._seq)
            except TypeError:
                pass  # non-dataclass event: forward unstamped
            self._queue.put_nowait(event)

    def _on_agent_event(self, event: Any) -> None:
        """Main-session listener body — runs on the reader thread."""
        try:
            events = self._translate_agent_event(event)
        except Exception:  # noqa: BLE001 — one bad event must not kill the feed
            logger.exception("observatory omp_feed: agent-event translation failed")
            return
        if not events:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._drain, events)

    # -- main-session translation (pure; unit-testable without a child) --

    @staticmethod
    def _agent_field(event: Any, *names: str) -> Any:
        """First present field across Mapping keys / attribute names."""
        for name in names:
            if isinstance(event, Mapping):
                if name in event:
                    return event[name]
            else:
                try:
                    value = getattr(event, name)
                except AttributeError:
                    continue
                return value() if callable(value) and name == "to_dict" else value
        return None

    @staticmethod
    def _agent_args_text(args: Any) -> Optional[str]:
        """Tool args value → renderer text (same shape as progress args)."""
        if args is None:
            return None
        if isinstance(args, str):
            return args or None
        try:
            return json.dumps(args, default=str)
        except Exception:
            try:
                return str(args)
            except Exception:
                return None

    def _translate_agent_event(self, event: Any):
        """One main-session agent event → self typed events (may be empty).

        The child's OWN tool calls and thinking arrive here as vendored
        ``RpcClient`` agent events (``tool_execution_start`` /
        ``message_update`` / ``message_end`` dataclasses, or plain Mappings
        in tests). Every emitted event carries ``subagent_id == ""`` —
        "the child itself" — so consumers render into the child's own
        room. Unknown shapes return [] (never raise).
        """
        if event is None:
            return []
        etype = self._agent_field(event, "type")
        if not isinstance(etype, str) or not etype:
            return []
        self.frame_counts[f"main:{etype}"] = self.frame_counts.get(f"main:{etype}", 0) + 1
        if etype == "tool_execution_start":
            tool = self._agent_field(event, "tool_name", "toolName", "tool")
            if not isinstance(tool, str) or not tool:
                return []
            return [
                ToolEvent(
                    subagent_id="",
                    tool=tool,
                    args=self._agent_args_text(
                        self._agent_field(event, "args")),
                )
            ]
        if etype == "message_update":
            inner = self._agent_field(
                event, "assistant_message_event", "assistantMessageEvent", "event")
            return self._translate_self_thinking(inner)
        if etype == "message_end":
            return self._translate_self_message_end(
                self._agent_field(event, "message"))
        return []

    def _translate_self_thinking(self, inner: Any):
        """Accumulate main-session thinking deltas; flush on thinking_end."""
        if inner is None:
            return []
        itype = self._agent_field(inner, "type")
        if not isinstance(itype, str):
            return []
        state = self._state("")
        if itype == "thinking_delta":
            delta = self._agent_field(inner, "delta")
            if isinstance(delta, str) and delta:
                idx = _opt_int(self._agent_field(inner, "contentIndex", "content_index")) or 0
                state.thinking[idx] = state.thinking.get(idx, "") + delta
            return []
        if itype == "thinking_end":
            idx = _opt_int(self._agent_field(inner, "contentIndex", "content_index")) or 0
            text = state.thinking.pop(idx, "")
            content = self._agent_field(inner, "content")
            if not text and isinstance(content, str):
                text = content
            if not text:
                return []
            return [ThoughtEvent(subagent_id="", text=text)]
        return []

    def _translate_self_message_end(self, message: Any):
        """Main-session message_end → defensive thought flush + MessageEvent."""
        if message is None:
            return []
        out = []
        state = self._state("")
        # Defensive flush: a stream cut between thinking_end and here can
        # leave an unflushed partial block — surface it rather than lose it.
        for idx in sorted(state.thinking):
            text = state.thinking.pop(idx)
            if text:
                out.append(ThoughtEvent(subagent_id="", text=text))
        if isinstance(message, Mapping):
            role = str(message.get("role") or "assistant")
            content = message.get("content")
        else:
            role = str(self._agent_field(message, "role") or "assistant")
            content = self._agent_field(message, "content")
        texts = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        texts.append(text)
                else:
                    text = self._agent_field(block, "text")
                    if isinstance(text, str):
                        texts.append(text)
        elif isinstance(content, str):
            texts.append(content)
        out.append(
            MessageEvent(subagent_id="", role=role, text="".join(texts))
        )
        state.thinking.clear()
        return out

    # -- translation (pure; unit-testable without a child) --------------

    def _translate(self, frame: Optional[Mapping[str, Any]]):
        """One wire frame → list of typed events (may be empty)."""
        if not isinstance(frame, Mapping):
            return []
        ftype = frame.get("type")
        if isinstance(ftype, str):
            self.frame_counts[ftype] = self.frame_counts.get(ftype, 0) + 1
        payload = frame.get("payload")
        if not isinstance(payload, Mapping):
            return []
        if ftype == "subagent_lifecycle":
            return self._translate_lifecycle(payload)
        if ftype == "subagent_progress":
            return self._translate_progress(payload)
        if ftype == "subagent_event":
            return self._translate_event(payload)
        return []

    def _translate_lifecycle(self, payload: Mapping[str, Any]):
        sid = str(payload.get("id") or "")
        if not sid:
            return []
        session_file = payload.get("sessionFile")
        self.note_session(sid, session_file if isinstance(session_file, str) else None)
        state = self._state(sid)
        state.parent_tool_call_id = payload.get("parentToolCallId") or None
        status = str(payload.get("status") or "")
        if status == "started":
            return [
                NodeEvent(
                    kind="add",
                    subagent_id=sid,
                    parent_tool_call_id=state.parent_tool_call_id,
                    status="running",
                    index=_opt_int(payload.get("index")),
                    agent=_opt_str(payload.get("agent")),
                    task=_opt_str(payload.get("description"))
                    or _opt_str(payload.get("task")),
                    session_file=state.session_file,
                )
            ]
        if status in _TERMINAL_LIFECYCLE:
            return [
                NodeEvent(
                    kind="death",
                    subagent_id=sid,
                    parent_tool_call_id=state.parent_tool_call_id,
                    status=status,
                    index=_opt_int(payload.get("index")),
                    agent=_opt_str(payload.get("agent")),
                    task=_opt_str(payload.get("description"))
                    or _opt_str(payload.get("task")),
                    session_file=state.session_file,
                )
            ]
        return []

    def _translate_progress(self, payload: Mapping[str, Any]):
        progress = payload.get("progress")
        if not isinstance(progress, Mapping):
            return []
        # Live wire (verified 2026-09-07): the RPC progress payload has
        # NO top-level id — the subagent id rides progress.id.
        sid = str(payload.get("id") or progress.get("id") or "")
        if not sid:
            return []
        session_file = payload.get("sessionFile")
        state = self._state(sid)
        if payload.get("parentToolCallId"):
            state.parent_tool_call_id = payload.get("parentToolCallId")
        out = []
        # 1. In-flight tool (present only while the tool runs).
        tool = _opt_str(progress.get("currentTool"))
        if tool:
            args = _opt_str(progress.get("currentToolArgs"))
            signature = (tool, args)
            if state.last_tool != signature:
                state.last_tool = signature
                out.append(
                    ToolEvent(
                        subagent_id=sid,
                        tool=tool,
                        args=args,
                        parent_tool_call_id=state.parent_tool_call_id,
                    )
                )
        # 2. Completed tools: recentTools (newest first) grows as tools
        # finish — emit one ToolEvent per unseen entry. Fast tools can
        # start and end between progress frames, so currentTool alone
        # under-reports on the live wire.
        recent = progress.get("recentTools")
        if isinstance(recent, list):
            fresh = []
            for entry in recent:
                if not isinstance(entry, Mapping):
                    continue
                key = (
                    entry.get("tool"),
                    entry.get("args"),
                    entry.get("endMs"),
                )
                if key in state.seen_recent:
                    continue
                state.seen_recent.add(key)
                fresh.append(entry)
            for entry in reversed(fresh):  # oldest first
                out.append(
                    ToolEvent(
                        subagent_id=sid,
                        tool=str(entry.get("tool") or ""),
                        args=_opt_str(entry.get("args")),
                        parent_tool_call_id=state.parent_tool_call_id,
                    )
                )
        return out

    def _translate_event(self, payload: Mapping[str, Any]):
        sid = str(payload.get("id") or "")
        event = payload.get("event")
        if not sid or not isinstance(event, Mapping):
            return []
        etype = event.get("type")
        if etype == "message_update":
            return self._translate_thinking_delta(sid, event)
        if etype == "message_end":
            return self._translate_message_end(sid, event)
        return []

    def _translate_thinking_delta(self, sid: str, event: Mapping[str, Any]):
        inner = event.get("assistantMessageEvent")
        if not isinstance(inner, Mapping):
            return []
        itype = inner.get("type")
        state = self._state(sid)
        if itype == "thinking_delta":
            delta = inner.get("delta")
            if isinstance(delta, str) and delta:
                idx = _opt_int(inner.get("contentIndex")) or 0
                state.thinking[idx] = state.thinking.get(idx, "") + delta
            return []
        if itype == "thinking_end":
            idx = _opt_int(inner.get("contentIndex")) or 0
            text = state.thinking.pop(idx, "")
            content = inner.get("content")
            if not text and isinstance(content, str):
                text = content
            if not text:
                return []
            return [ThoughtEvent(subagent_id=sid, text=text)]
        return []

    def _translate_message_end(self, sid: str, event: Mapping[str, Any]):
        message = event.get("message")
        if not isinstance(message, Mapping):
            return []
        out = []
        state = self._state(sid)
        # Defensive flush: a stream cut between thinking_end and here can
        # leave an unflushed partial block — surface it rather than lose it.
        for idx in sorted(state.thinking):
            text = state.thinking.pop(idx)
            if text:
                out.append(ThoughtEvent(subagent_id=sid, text=text))
        role = str(message.get("role") or "assistant")
        content = message.get("content")
        texts = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        texts.append(text)
        elif isinstance(content, str):
            texts.append(content)
        out.append(
            MessageEvent(subagent_id=sid, role=role, text="".join(texts))
        )
        state.thinking.clear()
        return out


def _opt_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None
