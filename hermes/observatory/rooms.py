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

_loop_lock = threading.Lock()
_gateway_loop = None


def set_event_loop(loop) -> None:
    """Stash the gateway event loop for thread-safe sends.

    Called at adapter connect (the loop that owns the transport).
    Worker threads (delegation engine, feed watcher) publish through
    ``say_nowait``/``call_soon`` below, which hop onto this loop —
    never touching asyncio objects cross-thread.
    """
    global _gateway_loop
    with _loop_lock:
        _gateway_loop = loop


def _loop_now():
    with _loop_lock:
        return _gateway_loop


def say_nowait(channel: str, text: str) -> bool:
    """Fire-and-forget one line into a room from any thread.

    True when handed to the gateway loop; False when no loop or sink
    (caller keeps nothing — live trace only, never retried).
    Never raises.
    """
    try:
        text = str(text or "")
        channel = str(channel or "")
        if not text or not channel:
            return False
        bot = get_bot_sink()
        loop = _loop_now()
        if bot is None or loop is None:
            return False
        import asyncio as _asyncio

        _asyncio.run_coroutine_threadsafe(bot.say(channel, text), loop)
        return True
    except Exception:
        return False


def call_soon(coro):
    """Schedule a bot coroutine on the gateway loop from any thread.

    Returns the concurrent Future, or None when unschedulable.
    Never raises.
    """
    try:
        bot = get_bot_sink()
        loop = _loop_now()
        if bot is None or loop is None or coro is None:
            return None
        import asyncio as _asyncio

        return _asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        return None


def set_bot_sink(sink: BotSink | None) -> None:
    """Register the live IRC transport (adapter on connect; None on drop)."""
    global _current_sink
    with _sink_lock:
        _current_sink = sink


def get_bot_sink() -> BotSink | None:
    with _sink_lock:
        return _current_sink


# --- channel naming --------------------------------------------------------


#: Verbs the omp engine owns (mirror of the TS slash registry;
#: adapter's drift test pins the two together).
OMP_COMMAND_VERBS = frozenset({
    "add", "add-dir", "advisor", "agents", "append", "auto", "branch", "browser",
    "btw", "budget", "cancel", "changelog", "cleanse", "clear", "collab", "collapse",
    "compact", "compare", "computer", "configure", "context", "copy", "debug", "delete",
    "diagnose", "dirs", "disable", "discover", "disposition", "done", "drop", "dump",
    "edit", "elide", "enable", "enqueue", "exit", "expand", "export", "extended-context",
    "extensions", "fast", "force", "fork", "fresh", "full", "git", "goal",
    "guided-goal", "handoff", "headless", "help", "hotkeys", "hub", "images", "import",
    "info", "install", "installed", "jobs", "join", "leave", "list", "live",
    "login", "logout", "loop", "marketplace", "mcp", "memory", "model", "models",
    "move", "new", "notifications", "off", "omfg", "on", "open", "pause",
    "pin", "plan", "plan-review", "plugins", "prewalk", "prompts", "providers", "q",
    "queue", "quit", "reauth", "rebuild", "reconnect", "reload", "reload-plugins", "remove",
    "remove-dir", "rename", "reset", "resources", "restart", "resume", "retry", "rewind",
    "rm", "scan", "scans", "security", "session", "set", "settings", "setup",
    "shake", "share", "show", "skillful", "smithery-login", "smithery-logout", "smithery-search", "ssh",
    "start", "stats", "status", "stop", "switch", "sync", "tan", "test",
    "thinking", "todo", "tools", "trace", "tree", "unauth", "uninstall", "update",
    "upgrade", "usage", "validate", "vibe", "view", "visible", "vision", "worktree",
    "wt",
})

#: Observatory verbs: room lifecycle owned by the gateway side.
#: In omp rooms these must reach gateway dispatch and must NEVER
#: be pumped into the omp task (an "/exit" task is nonsense).
OBSERVATORY_ROOM_VERBS = frozenset({
    "spawn", "spawnomp", "exit", "stop", "approve", "deny",
})


def classify_omp_slash(text: str) -> str:
    """Route a line in a spawned-omp room: chat | omp | observatory | gateway.

    omp: the verb is omp-owned — pump as task, gateway stays out
    (no hermes-flavored double answer). observatory: gateway-owned
    room lifecycle — never a task. gateway: hermes-only or unknown
    verbs — gateway dispatch owns the reply (including the
    unknown-command notice). chat: plain text pumps as a task.
    """
    stripped = str(text or "").lstrip()
    if not stripped.startswith("/"):
        return "chat"
    verb = stripped[1:].split(None, 1)[0].lower() if len(stripped) > 1 else ""
    if not verb:
        return "gateway"
    if verb in OBSERVATORY_ROOM_VERBS:
        return "observatory"
    if verb in OMP_COMMAND_VERBS:
        return "omp"
    return "gateway"

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
        try:
            from observatory.provision import live_server_name

            live = live_server_name(None) or ""
        except Exception:
            live = ""
        name = str(item.get("name") or node_id)
        parent = str(item.get("parent_name") or "")
        parent_row = self._resolve_parent(parent)
        if parent_row is None:
            # Gateway sessions carry bare session UUIDs (no channel to
            # match), so gateway-owned delegates resolve to nothing.
            # A delegate by definition has a parent — fall back to the
            # gateway row rather than stranding it at depth 0 (immortal).
            try:
                from observatory.provision import GATEWAY_NODE_ID

                parent_row = self.state.get(GATEWAY_NODE_ID)
            except Exception:
                parent_row = None
        parent_name = str((parent_row or {}).get("name") or "gateway")
        parent_channel = str((parent_row or {}).get("room_id") or "").lstrip("#")
        if parent_channel:
            channel = clean_channel(f"{parent_channel}-{name}")
        else:
            channel = child_channel(parent_name, name, server=live or None)
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
                mxid=agent_nick(channel.lstrip("#"), server=""),
                session_ref=str(item.get("session_ref") or node_id),
                parent_node_id=str(parent_row.get("node_id"))
                if parent_row is not None
                else None,
                extra={"kind": "delegate", **(
                    {"subagent_id": str(item.get("subagent_id") or "")}
                    if str(item.get("subagent_id") or "") else {})},
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
        logger.info("observatory: room ensured %s for %s", channel, node_id)
        # No bouncer subscription: The Lounge sees rooms via INVITE
        # and prunes them itself on destroy.
        try:
            from observatory.provision import get_lounge_nick

            bot = self.bot
            if bot is not None:
                await bot.invite_user(get_lounge_nick(None), channel)
        except Exception:
            logger.debug("rooms: lounge invite failed for %s", channel)
        try:
            # Voice of the room: without its own identity every frame
            # arrives stamped vm_gateway (the bot connection). The nick
            # mirrors the room name (mxid convention) so #vm_alpha-bravo
            # speaks as vm_alpha-bravo.
            from observatory.identity import ensure_identity

            await ensure_identity(
                agent_nick(channel.lstrip("#"), server=""), channel)
        except Exception:
            logger.debug("rooms: identity ensure failed for %s", channel)
        return channel

    async def _retire_child_room(self, node_id: str, *, summary: str = "") -> None:
        """Death/purge for a finished delegate child (D8 timing).

        Every stop dead-marks the row. Depth 1 (child of a 0-agent) purges
        immediately: summary to the parent room, channel destroyed,
        row deleted, identity dropped — plus any delegate descendants
        (their parent just died). Depth >= 2 keeps row + room as reading
        grace until the parent dies. Never raises.
        """
        try:
            row = self.state.get(node_id)
        except Exception:
            return
        depth = row.get("depth", 1)
        try:
            depth = int(depth)
        except Exception:
            depth = 1
        try:
            self.state.mark_dead(node_id)
        except Exception:
            pass
        try:
            from observatory.state import purge_on_death

            purge = purge_on_death(depth)
        except Exception:
            purge = depth == 1
        if not purge:
            return
        parent_channel = ""
        try:
            parent_id = str(row.get("parent_node_id") or "")
            if parent_id:
                parent_channel = str(
                    self.state.get(parent_id).get("room_id") or ""
                )
        except Exception:
            parent_channel = ""
        if summary and parent_channel:
            try:
                await self.publish(
                    parent_channel,
                    f"subagent '{row.get('name') or node_id}' finished: {summary}",
                )
            except Exception:
                pass
        await self._purge_child_subtree(node_id)

    async def _purge_child_subtree(self, node_id: str) -> None:
        """Destroy + delete a dead node and its delegate descendants."""
        try:
            row = self.state.get(node_id)
        except Exception:
            return
        channel = str(row.get("room_id") or "")
        children: list[str] = []
        try:
            for r in self.state.get_subtree(node_id):
                cid = str(r.get("node_id") or "")
                if cid and cid != node_id:
                    children.append(cid)
        except Exception:
            pass
        for cid in children:
            try:
                await self._purge_child_subtree(cid)
            except Exception:
                continue
        if channel:
            try:
                await self.destroy_room(channel)
            except Exception:
                pass
            # No unsubscribe step: without a bouncer there is no
            # phone-side subscription — The Lounge prunes the destroyed
            # room itself.
            try:
                from observatory.identity import drop_identity

                await drop_identity(channel)
            except Exception:
                pass
        try:
            self.state.mark_deleted_and_purge(node_id)
            logger.info("observatory: room purged %s", channel)
        except Exception:
            pass

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

            _asyncio.get_running_loop().create_task(
                self._run_spawned_omp_task(channel, node_id, sender, text, rpc),
                name=f"observatory-omp-room-{node_id}",
            )
        except Exception:
            entry["busy"] = False
            return "couldn't start that task — try again."
        # Silent start: the trace itself is the feedback. (The steer
        # path below still answers "steered mid-run.")
        return ""

    async def _run_spawned_omp_task(
        self, channel: str, node_id: str, sender: str, text: str, rpc: Any
    ) -> None:
        """Background body of one spawned-omp turn: live trace, then answer.

        Runs off the adapter receive loop so the bot keeps reading (and
        answering PINGs) mid-turn — follow-up text steers instead of
        queueing. Never raises; the summary (or failure) is published
        into the room, then the room is marked idle.
        """
        seen: set[str] = set()
        feed: Any = None
        pump_task: Any = None
        try:
            import asyncio as _asyncio

            feed = await self._start_live_omp_feed(rpc, channel, seen)
            if feed is not None:
                pump_task = _asyncio.get_running_loop().create_task(
                    self._pump_live_omp_feed(feed, node_id, channel, {}, seen)
                )
            result = await _asyncio.to_thread(
                rpc.run_task, f"[{sender} over IRC] {text}"
            )
        except Exception as exc:
            logger.debug("rooms: background omp task failed", exc_info=True)
            with _omp_lock:
                for _entry in _omp_rooms.values():
                    if _entry.get("rpc") is rpc:
                        _entry["busy"] = False
            try:
                await self.publish(channel, f"(task failed: {exc})")
            except Exception:
                pass
            return
        finally:
            if feed is not None:
                try:
                    await feed.stop()
                except Exception:
                    pass
            if pump_task is not None:
                try:
                    await pump_task
                except Exception:
                    pass
        try:
            summary = str((result or {}).get("summary") or "")
            frames = (result or {}).get("turn_frames") or []
            for frame in frames:
                if _omp_room_skips_frame(frame):
                    continue
                try:
                    from observatory.omp_feed import child_frame_key

                    if child_frame_key(frame) in seen:
                        continue
                except Exception:
                    pass
                await self.publish_frame(channel, frame)
            await self.publish(channel, summary or "(no output)")
        except Exception as exc:
            logger.debug("rooms: omp reply failed", exc_info=True)
            try:
                await self.publish(channel, "(reply render failed)")
            except Exception:
                pass
        finally:
            with _omp_lock:
                for _entry in _omp_rooms.values():
                    if _entry.get("rpc") is rpc:
                        _entry["busy"] = False

    async def _start_live_omp_feed(
        self, rpc: Any, channel: str, seen: set[str]
    ) -> Any:
        """Subscribe an OmpFeed for live thought/tool streaming. None on failure."""
        try:
            from observatory.omp_feed import OmpFeed

            feed = OmpFeed(rpc)
            await feed.start()
            return feed
        except Exception:
            logger.debug("rooms: live omp feed unavailable", exc_info=True)
            return None

    async def _pump_live_omp_feed(
        self, feed: Any, node_id: str, channel: str,
        grands: dict[str, str], seen: set[str],
    ) -> None:
        """Forward live feed frames, routing N>1 into their own rooms."""
        try:
            from observatory.gateway_session import (
                _feed_event_to_dict as _to_payload,
            )
            from observatory.omp_feed import child_frame_key
        except Exception:
            return
        try:
            async for typed in feed.events():
                try:
                    payload = _to_payload(typed)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if _omp_room_skips_frame(payload):
                    continue
                try:
                    seen.add(child_frame_key(payload))
                except Exception:
                    pass
                try:
                    await self._publish_routed_frame(
                        node_id, channel, payload, grands)
                except Exception:
                    continue
        except Exception:
            pass

    async def _publish_routed_frame(
        self, owner_id: str, channel: str, feed: dict[str, Any],
        grands: dict[str, str],
    ) -> None:
        """One feed frame into the owner's room or its own N>1 room."""
        try:
            sub = str(feed.get("subagent_id") or "")
            kind = str(feed.get("kind") or "")
            if str(feed.get("feed") or "") == "node":
                await self._apply_grandchild_node(owner_id, feed, grands)
                return
            if not sub:
                await self.publish_frame(channel, feed)
                return
            target = grands.get(sub)
            if not target:
                node_id = f"{owner_id}/sub-{sub}"
                try:
                    target = self.channel_for_node(node_id)
                except Exception:
                    target = ""
            if not target:
                node_id = f"{owner_id}/sub-{sub}"
                name = (str(feed.get("agent") or "").strip()
                        or str(feed.get("task") or "").strip()
                        or f"sub-{sub[:8]}")
                target = await self._ensure_child_room_for(node_id, {
                    "name": name,
                    "parent_name": owner_id,
                    "engine": "omp",
                    "subagent_id": sub,
                    "session_ref": str(feed.get("session_file") or node_id),
                })
                if target:
                    grands[sub] = target
            if target:
                flat = dict(feed)
                flat["subagent_id"] = ""
                await self.publish_frame(target, flat)
        except Exception:
            pass

    async def _apply_grandchild_node(
        self, owner_id: str, feed: dict[str, Any], grands: dict[str, str],
    ) -> None:
        """Node add/death frame → create or retire the grandchild room."""
        try:
            sid = str(feed.get("subagent_id") or "")
            if not sid:
                return
            kind = str(feed.get("kind") or "")
            flat = dict(feed)
            flat["subagent_id"] = ""
            if kind == "add":
                target = grands.get(sid)
                if not target:
                    node_id = f"{owner_id}/sub-{sid}"
                    name = (str(feed.get("agent") or "").strip()
                            or str(feed.get("task") or "").strip()
                            or f"sub-{sid[:8]}")
                    target = await self._ensure_child_room_for(node_id, {
                        "name": name,
                        "parent_name": owner_id,
                        "engine": "omp",
                        "subagent_id": sid,
                        "session_ref": str(
                            feed.get("session_file") or node_id),
                    })
                    if target:
                        grands[sid] = target
                if target:
                    line = format_frame(flat)
                    if line:
                        await self.publish(target, line)
            elif kind == "death":
                node_id = f"{owner_id}/sub-{sid}"
                channel = ""
                try:
                    channel = self.channel_for_node(node_id)
                except Exception:
                    pass
                if channel:
                    line = format_frame(flat)
                    if line:
                        await self.publish(channel, line)
                grands.pop(sid, None)
                await self._retire_child_room(node_id)
        except Exception:
            pass


_manager_lock = threading.Lock()
_current_manager: "RoomManager | None" = None


def _omp_room_skips_frame(payload: Any) -> bool:
    """Spawnomp rooms: drop user/assistant message frames.

    The user's own text is already visible (they sent it); the
    assistant's text ships as the reply summary. Publishing either
    looks like an echo or a double response. Tool/thought frames
    always stream. Never raises.
    """
    try:
        if not isinstance(payload, dict):
            return False
        if str(payload.get("feed") or "") != "message":
            return False
        return str(payload.get("role") or "") in ("user", "assistant")
    except Exception:
        return False

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


def channel_for_node_id(node_id: str) -> str:
    """Best-effort channel for a node, callable from any thread.

    Prefers the live manager; else opens a throwaway state handle.
    Never raises.
    """
    try:
        manager = get_room_manager()
        if manager is not None:
            channel = manager.channel_for_node(node_id)
            if channel:
                return channel
    except Exception:
        pass
    try:
        from observatory.provision import _mercury_home
        from observatory.state import ObservatoryState, default_state_db_path

        state = ObservatoryState(default_state_db_path(_mercury_home(None)))
        try:
            return str(state.get(node_id).get("room_id") or "")
        finally:
            try:
                state.close()
            except Exception:
                pass
    except Exception:
        return ""


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
