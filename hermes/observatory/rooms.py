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
- delegate-child rooms steer via ``delegate_tool.steer_subagent`` (hermes)
  or ``handle_omp_control_action`` (omp).

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

    async def join_channel(self, channel: str) -> bool:
        ...

    async def part_channel(self, channel: str) -> bool:
        ...

    async def say(self, channel: str, text: str) -> bool:
        ...

    async def destroy_channel(self, channel: str) -> bool:
        ...


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


def spawn_channel(name: str) -> str:
    """``/spawn`` / ``/spawnomp`` room for ``name``."""
    return clean_channel(name)


def child_channel(parent_name: str, child_name: str) -> str:
    """Delegate-child room: parent + child names joined (req: parent-child)."""
    return clean_channel(f"{parent_name}-{child_name}")


def agent_nick(name: str) -> str:
    return clean_nick(name)


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


def format_lifecycle(lifecycle: str, *, name: str = "",
                     summary: str = "", status: str = "") -> str:
    if lifecycle == "start":
        return f"{LIFECYCLE_START} subagent '{name}' started — live trace streams here"
    if lifecycle == "stop":
        tail = f": {_truncate(summary)}" if summary else (f" ({status})" if status else "")
        return f"{LIFECYCLE_STOP} subagent '{name}' finished{tail}"
    return f"{NOTICE_PREFIX} subagent '{name}': {lifecycle}"


# --- room manager ----------------------------------------------------------

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
        if kind == "gateway" or depth == 0 and not row.get("parent_node_id"):
            if kind == "gateway":
                return "gateway", row
            return (f"spawn-{engine}", row)
        return "child", row

    # -- ensure ------------------------------------------------------

    async def ensure_room(self, channel: str, *, topic: str = "",
                          greet: str = "") -> bool:
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

    async def publish_lifecycle(self, channel: str, lifecycle: str, **kwargs: Any) -> bool:
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
