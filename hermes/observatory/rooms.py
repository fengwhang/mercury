"""IRC room manager for the observatory (in-gateway-process).

Replaces the Matrix sidecar/tree/renderer stack with the boring
equivalent: one IRC channel per live agent.

- Gateway agent → ``#<server>_gateway`` (``server_name`` from provision).
- ``/spawn`` / ``/spawnomp <name>`` → ``#<name>``.
- Delegate-task / omp subagents → ``#<parent>-<child>`` (parent names the
  child; the room streams the child's tool calls + thinking traces).
- ``/exit`` in a spawned room → engine stop + server-side channel destroy.

Inbound chat lands here only for ROUTING (which node owns this channel?);
the actual turns run on existing machinery with CLI parity:

- spawned hermes rooms are plain gateway sessions keyed by channel
  (adapter dispatch — slash commands, approvals, mid-turn queueing free);
- spawned omp rooms pump ``OmpRpcChild.run_task`` / ``steer``;
- delegate-child rooms steer the live child transport registered by the
  feed watcher (omp ``transport.steer``; one-shot children are
  read-only traces).

The IRC transport is a :class:`BotSink` — the gateway IRC adapter
registers itself on connect (:func:`set_bot_sink`); tests inject fakes.
No gateway imports at module level so this stays light under pytest.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

from observatory.ircd import clean_channel, clean_nick

logger = logging.getLogger(__name__)

#: Prefixes for streamed child frames (IRC has no markdown/quoting).
TOOL_PREFIX = "\U0001f527"
THINK_PREFIX = "\U0001f4ad"
MSG_PREFIX = ""
LIFECYCLE_START = "\U0001f680"
LIFECYCLE_STOP = "✅"
NOTICE_PREFIX = "ℹ️ "

#: Frame text cap per IRC line burst (adapter splits long sends anyway).
FRAME_TEXT_LIMIT = 400


class BotSink(Protocol):
    """What the room manager needs from the IRC transport."""

    async def join_channel(self, channel: str) -> bool: ...

    async def part_channel(self, channel: str) -> bool: ...

    async def say(self, channel: str, text: str) -> bool: ...

    async def destroy_channel(self, channel: str) -> bool: ...

    async def invite_user(self, nick: str, channel: str) -> bool: ...


_sink_lock = threading.Lock()
_current_sink: BotSink | None = None


def set_bot_sink(sink: BotSink | None) -> None:
    """Register the live IRC transport (adapter on connect; None on drop)."""
    global _current_sink
    with _sink_lock:
        _current_sink = sink


def get_bot_sink() -> BotSink | None:
    with _sink_lock:
        return _current_sink


# --- channel naming --------------------------------------------------------


def gateway_channel(server_name: str) -> str:
    """``#<server>_gateway`` — e.g. server ``mercury`` → ``#mercury_gateway``."""
    base = clean_channel(server_name or "mercury").lstrip("#")
    return f"#{base}_gateway"


def _server_prefix(server: str | None = None) -> str:
    """Live network label (best-effort, never raises).

    ``None`` resolves from the live ircd.json; pass ``""`` for the bare
    legacy form (already-qualified names like the gateway nick).
    """
    if server is not None:
        return str(server).strip().lower()
    try:
        from observatory.provision import live_server_name  # no cycle

        return str(live_server_name(None) or "").strip().lower()
    except Exception:
        return ""


def spawn_channel(name: str, server: str | None = None) -> str:
    """``/spawn`` / ``/spawnomp`` room: ``#<server>_<name>``."""
    prefix = _server_prefix(server)
    base = f"{prefix}_{name}" if prefix else str(name)
    return clean_channel(base)


def child_channel(parent_name: str, child_name: str,
                  server: str | None = None) -> str:
    """Delegate-child room: ``#<server>_<parent>-<child>``."""
    prefix = _server_prefix(server)
    base = (f"{prefix}_{parent_name}-{child_name}" if prefix
            else f"{parent_name}-{child_name}")
    return clean_channel(base)


def agent_nick(name: str, server: str | None = None) -> str:
    """Agent nick: ``<server>_<name>`` (gateway: ``<server>_gateway``)."""
    prefix = _server_prefix(server)
    base = f"{prefix}_{name}" if prefix else str(name)
    return clean_nick(base).lower()


# --- frame formatting ------------------------------------------------------


def _truncate(text: str, limit: int = FRAME_TEXT_LIMIT) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def format_frame(feed: dict[str, Any] | Any) -> str | None:
    """One OmpFeed/datagram frame dict → one IRC line; None to skip.

    Shapes (see gateway_session._feed_event_to_dict): ``feed`` ∈
    {message, node, tool, thought} with ``text`` / ``tool`` / ``status``
    keys; ``subagent_id == ""`` is the child's own main session, a
    non-empty id tags a grandchild frame with ``[id]``.
    """
    if not isinstance(feed, dict):
        return None
    kind = str(feed.get("feed") or "")
    sub = str(feed.get("subagent_id") or "")
    tag = f"[{sub}] " if sub else ""
    if kind == "message":
        text = _truncate(feed.get("text") or "", FRAME_TEXT_LIMIT * 2)
        return f"{tag}{text}" if text else None
    if kind == "tool":
        tool = str(feed.get("tool") or "tool")
        args = _truncate(feed.get("args") or feed.get("text") or "")
        body = f"{tool} {args}".strip()
        return f"{TOOL_PREFIX} {tag}{body}"
    if kind == "thought":
        text = _truncate(feed.get("text") or "")
        return f"{THINK_PREFIX} {tag}{text}" if text else None
    if kind == "node":
        status = str(feed.get("status") or "")
        label = str(feed.get("label") or feed.get("name") or "")
        body = f"{label} {status}".strip()
        return f"{NOTICE_PREFIX} {tag}{body}" if body else None
    return None


def format_lifecycle(
    lifecycle: str, *, name: str = "", summary: str = "", status: str = ""
) -> str:
    if lifecycle == "start":
        return f"{LIFECYCLE_START} subagent '{name}' started — live trace streams here"
    if lifecycle == "stop":
        tail = (
            f": {_truncate(summary)}" if summary else (f" ({status})" if status else "")
        )
        return f"{LIFECYCLE_STOP} subagent '{name}' finished{tail}"
    return f"{NOTICE_PREFIX} subagent '{name}': {lifecycle}"


# --- room manager ----------------------------------------------------------


def _channel_in_ref(channel: str, ref: str) -> bool:
    """True when gateway session key ``ref`` embeds ``#channel``.

    Group session keys join parts with ``:`` (``ns:irc:group:#ace:…``),
    so a boundary-aware substring match maps a delegating turn back to
    the room (and node) that spawned it.
    """
    try:
        import re as _re

        return bool(
            _re.search(r"(?:^|:)" + _re.escape(channel) + r"(?:$|:)", str(ref or ""))
        )
    except Exception:
        return False


class RoomManager:
    """Channel lifecycle + publish fan-out over a state.db tree.

    ``state`` is an ``ObservatoryState`` (rows carry ``room_id``=channel,
    ``mxid``=nick, ``space_id``=""). ``bot`` defaults to the global sink.
    All methods are best-effort except where noted; never raise on IRC
    transport failure (the engine turn is the source of truth, the room
    is the mirror).
    """

    def __init__(self, state: Any, bot: BotSink | None = None):
        self.state = state
        self._bot = bot

    @property
    def bot(self) -> BotSink | None:
        return self._bot if self._bot is not None else get_bot_sink()

    # -- lookup ------------------------------------------------------

    def live_rows(self) -> list[dict[str, Any]]:
        try:
            return list(self.state.get_live())
        except Exception:
            return []

    def node_for_channel(self, channel: str) -> dict[str, Any] | None:
        want = (channel or "").lower()
        for row in self.live_rows():
            try:
                if str(row.get("room_id") or "").lower() == want:
                    return row
            except Exception:
                continue
        return None

    def channel_for_node(self, node_id: str) -> str:
        try:
            return str(self.state.get(node_id).get("room_id") or "")
        except Exception:
            return ""

    def inbound_route(self, channel: str) -> tuple[str, dict[str, Any] | None]:
        """Classify an inbound channel message for the adapter.

        Returns ``(route, row)`` where route ∈ gateway | spawn-hermes |
        spawn-omp | child | passthrough. Unknown channels pass through to
        normal gateway dispatch (a fresh per-channel session).
        """
        row = self.node_for_channel(channel)
        if row is None:
            return "passthrough", None
        try:
            extra = row.get("extra") or {}
            kind = str(extra.get("kind") or "")
            depth = int(row.get("depth", 0))
            engine = str(row.get("engine") or "hermes")
        except Exception:
            return "passthrough", row
        if kind == "gateway":
            return "gateway", row
        if kind == "delegate" or depth >= 1:
            return "child", row
        return (f"spawn-{engine}", row)

    # -- ensure ------------------------------------------------------

    async def ensure_room(
        self, channel: str, *, topic: str = "", greet: str = ""
    ) -> bool:
        """Bot JOINs ``channel`` (IRC creates on first join); greeting optional."""
        bot = self.bot
        if bot is None:
            logger.debug("rooms: no bot sink — room %s deferred", channel)
            return False
        try:
            ok = await bot.join_channel(channel)
        except Exception:
            logger.debug("rooms: join %s failed", channel, exc_info=True)
            return False
        if ok and greet:
            try:
                await bot.say(channel, greet)
            except Exception:
                logger.debug("rooms: greet %s failed", channel, exc_info=True)
        return ok

    async def publish(self, channel: str, text: str) -> bool:
        """One (possibly multi-line) message into ``channel``."""
        bot = self.bot
        if bot is None or not text:
            return False
        try:
            return bool(await bot.say(channel, text))
        except Exception:
            logger.debug("rooms: publish to %s failed", channel, exc_info=True)
            return False

    async def publish_frame(self, channel: str, feed: dict[str, Any] | Any) -> bool:
        line = format_frame(feed)
        if not line:
            return False
        return await self.publish(channel, line)

    async def publish_lifecycle(
        self, channel: str, lifecycle: str, **kwargs: Any
    ) -> bool:
        return await self.publish(channel, format_lifecycle(lifecycle, **kwargs))

    async def destroy_room(self, channel: str) -> bool:
        """Server-side destroy (OPER DESTROY): members PARTed, history dropped."""
        bot = self.bot
        if bot is None:
            return False
        try:
            return bool(await bot.destroy_channel(channel))
        except Exception:
            logger.debug("rooms: destroy %s failed", channel, exc_info=True)
            return False

    # -- queue pump ----------------------------------------------------

    async def drain_queue(self) -> int:
        """Publish every queued lifecycle/feed/approval frame. Returns count.

        When the bot is down the queue is LEFT INTACT (returns 0) — frames
        wait for the next pump after reconnect instead of being dropped.
        """
        if self.bot is None:
            return 0
        count = 0
        while True:
            try:
                item = _QUEUE.get_nowait()
            except Exception:
                return count
            count += 1
            try:
                await self._apply_queued(item)
            except Exception:
                logger.debug("rooms: queued item failed", exc_info=True)

    async def _apply_queued(self, item: dict[str, Any]) -> None:
        op = str(item.get("op") or "")
        node_id = str(item.get("node_id") or "")
        if op == "feed":
            channel = self.channel_for_node(
                node_id
            ) or await self._ensure_child_room_for(node_id, item)
            if channel:
                await self.publish_frame(channel, item.get("feed"))
        elif op == "lifecycle":
            channel = await self._ensure_child_room_for(node_id, item)
            if channel:
                await self.publish_lifecycle(
                    channel,
                    str(item.get("lifecycle") or ""),
                    name=str(item.get("name") or node_id),
                    summary=str(item.get("summary") or ""),
                    status=str(item.get("status") or ""),
                )
            if str(item.get("lifecycle") or "") == "stop":
                drop_child_steer(node_id)
        elif op == "approval":
            channel = self.channel_for_node(node_id)
            if channel:
                await self.publish(
                    channel,
                    f"🔒 approval requested: `{item.get('command', '')}`"
                    f" — {item.get('description', '')} "
                    f"(reply /approve or /deny in the parent room)",
                )

    def _resolve_parent(self, parent: str) -> dict[str, Any] | None:
        """Parent ref → live row: node id, session_ref, then gateway
        session-key channel scan (group keys embed ``#channel``)."""
        if not parent:
            return None
        try:
            return self.state.get(parent)
        except Exception:
            pass
        for row in self.live_rows():
            try:
                if str(row.get("session_ref") or "") == parent:
                    return row
            except Exception:
                continue
        for row in self.live_rows():
            try:
                channel = str(row.get("room_id") or "")
                if channel and _channel_in_ref(channel, parent):
                    return row
            except Exception:
                continue
        return None

    async def _ensure_child_room_for(self, node_id: str, item: dict[str, Any]) -> str:
        """Channel for a delegate child: existing row, else create from the frame."""
        channel = self.channel_for_node(node_id)
        if channel:
            return channel
        name = str(item.get("name") or node_id)
        parent = str(item.get("parent_name") or "")
        parent_row = self._resolve_parent(parent)
        parent_name = str((parent_row or {}).get("name") or "gateway")
        channel = child_channel(parent_name, name)
        try:
            depth = int((parent_row or {}).get("depth", 0)) + 1
        except Exception:
            depth = 1
        try:
            slug = f"{parent_name}-{name}".lower()[:64]
            self.state.add_node(
                node_id,
                engine=str(item.get("engine") or "hermes"),
                name=name,
                slug=slug,
                mxid=agent_nick(name),
                session_ref=str(item.get("session_ref") or node_id),
                parent_node_id=str(parent_row.get("node_id"))
                if parent_row is not None
                else None,
                extra={"kind": "delegate"},
            )
            try:
                self.state.set_room_id(node_id, channel)
            except Exception:
                logger.debug("rooms: set_room_id %s failed", node_id, exc_info=True)
        except Exception:
            logger.debug("rooms: child row for %s exists", node_id, exc_info=True)
        await self.ensure_room(
            channel, greet=f"live trace for subagent '{name}' streams here"
        )
        return channel

    async def handle_child_message(self, channel: str, sender: str, text: str) -> str:
        """User message in a delegate-child room → steer the live child."""
        row = self.node_for_channel(channel)
        node_id = str((row or {}).get("node_id") or "")
        with _steer_lock:
            fn = _steer_fns.get(node_id)
        if fn is None:
            return "that subagent already finished — its room is history now."
        try:
            import asyncio as _asyncio

            if _asyncio.iscoroutinefunction(fn):
                ok = await fn(text)
            else:
                ok = fn(text)
        except Exception as exc:
            logger.debug("rooms: steer %s failed", node_id, exc_info=True)
        if ok is False:
            return "subagent is no longer accepting input."
        return f"steered (as {sender})."

    async def handle_omp_message(self, channel: str, sender: str, text: str) -> str:
        """User message in a spawned-omp room: idle → task, busy → steer."""
        row = self.node_for_channel(channel)
        node_id = str((row or {}).get("node_id") or "")
        with _omp_lock:
            entry = _omp_rooms.get(node_id)
        if entry is None:
            return "that omp agent is gone — /spawnomp a fresh one."
        rpc = entry.get("rpc")
        if rpc is None:
            return "omp agent not running."
        if entry.get("busy"):
            try:
                rpc.steer(f"[{sender} over IRC] {text}")
                return "steered mid-run."
            except Exception as exc:
                return f"steer failed: {exc}"
        entry["busy"] = True
        try:
            import asyncio as _asyncio

            result = await _asyncio.to_thread(
                rpc.run_task, f"[{sender} over IRC] {text}"
            )
        finally:
            entry["busy"] = False
        try:
            summary = str((result or {}).get("summary") or "")
            frames = (result or {}).get("turn_frames") or []
            for frame in frames:
                await self.publish_frame(channel, frame)
            return summary or "(no output)"
        except Exception as exc:
            logger.debug("rooms: omp reply failed", exc_info=True)
            return f"(reply render failed: {exc})"


_manager_lock = threading.Lock()
_current_manager: "RoomManager | None" = None


def set_room_manager(manager: "RoomManager | None") -> None:
    """Register the gateway-process room manager (platform_hook boot)."""
    global _current_manager
    with _manager_lock:
        _current_manager = manager


def get_room_manager() -> "RoomManager | None":
    with _manager_lock:
        return _current_manager


def route_channel(channel: str) -> tuple[str, dict[str, Any] | None]:
    """Adapter inbound hook: classify without importing state here."""
    manager = get_room_manager()
    if manager is None:
        return "passthrough", None
    try:
        return manager.inbound_route(channel)
    except Exception:
        return "passthrough", None


# --- producer queue (sync fire-and-forget → async pump) --------------------
_QUEUE: "queue.Queue[dict[str, Any]]" = __import__("queue").Queue()


def submit_lifecycle(node_id: str, lifecycle: str, **fields: Any) -> None:
    """Sync, never raises: enqueue a child lifecycle frame for the pump."""
    try:
        _QUEUE.put_nowait({
            "op": "lifecycle",
            "node_id": node_id,
            "lifecycle": lifecycle,
            **fields,
        })
    except Exception:
        pass


def submit_feed(node_id: str, feed: dict[str, Any] | Any) -> None:
    """Sync, never raises: enqueue one child feed frame for the pump."""
    try:
        payload = (
            dict(feed)
            if isinstance(feed, dict)
            else {"feed": "message", "text": str(feed)}
        )
        _QUEUE.put_nowait({"op": "feed", "node_id": node_id, "feed": payload})
    except Exception:
        pass


def submit_approval(node_id: str, **fields: Any) -> None:
    """Sync, never raises: enqueue an approval prompt for the pump."""
    try:
        _QUEUE.put_nowait({"op": "approval", "node_id": node_id, **fields})
    except Exception:
        pass


# --- child steer registry --------------------------------------------------
_steer_lock = threading.Lock()
_steer_fns: dict[str, Any] = {}


def register_child_steer(node_id: str, fn: Any) -> None:
    """Register the in-process steerer for a delegate child room."""
    with _steer_lock:
        _steer_fns[node_id] = fn


def drop_child_steer(node_id: str) -> None:
    with _steer_lock:
        _steer_fns.pop(node_id, None)


# --- spawned-omp room registry ----------------------------------------------
_omp_lock = threading.Lock()
_omp_rooms: dict[str, dict[str, Any]] = {}


def register_omp_room(node_id: str, channel: str, rpc: Any) -> None:
    with _omp_lock:
        _omp_rooms[node_id] = {"channel": channel, "rpc": rpc, "busy": False}


def drop_omp_room(node_id: str) -> None:
    with _omp_lock:
        _omp_rooms.pop(node_id, None)


_pump_task = None


async def pump_forever(manager: "RoomManager", interval: float = 2.0) -> None:
    """Drain the producer queue forever (cancellation stops it)."""
    import asyncio as _asyncio

    while True:
        try:
            await manager.drain_queue()
        except Exception:
            logger.debug("rooms: pump drain failed", exc_info=True)
        try:
            await _asyncio.sleep(max(0.5, float(interval)))
        except _asyncio.CancelledError:
            raise
        except Exception:
            return


async def start_pump(manager: "RoomManager", interval: float = 2.0) -> bool:
    """Start the shared pump task once; True when (now) running."""
    import asyncio as _asyncio

    global _pump_task
    try:
        if _pump_task is not None and not _pump_task.done():
            return True
        _pump_task = _asyncio.get_running_loop().create_task(
            pump_forever(manager, interval)
        )
        return True
    except Exception:
        logger.debug("rooms: pump start failed", exc_info=True)
        return False
