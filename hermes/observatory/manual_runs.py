"""M5b (D14): manual omp runs — observe-only rooms under the "Manual runs"
subspace, mirroring terminal ``omp`` sessions the gateway never spawned.

Discovery: ``$PI_CODING_AGENT_DIR/sessions/<sanitized-cwd>/<ts>_<uuid>.jsonl``
(verified layout: one directory per sanitized cwd, session files one level
deep — artifact dirs with subagent/advisor transcripts nest DEEPER and are
excluded, as are ``__advisor*.jsonl`` and ``*.bak``). Sessions already
claimed by discovery (a live node's ``session_ref``/``extra.session_file``)
are skipped — delegate children keep their agent rooms, never a duplicate
here.

Tailing: byte-offset watchers per file (polling; mtime/new-file watch).
New bytes are split into complete JSONL lines (partial trailing line is
buffered) and each ``message`` entry is converted to the SAME typed events
omp_feed emits — ``ToolEvent``/``ThoughtEvent``/``MessageEvent`` — by
REUSING OmpFeed's own frame→event converters through synthesized wire
frames (``thinking_end`` for thinking blocks, ``message_end`` for text);
toolCall blocks have no wire analog and become ``ToolEvent`` directly.

Read-only by design (no RPC server exists in TUI mode): no steer plumbing,

and every room carries the ``read-only (manual run)`` topic marker.
"""
from __future__ import annotations

import json
import logging
import os
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from observatory import tree
from observatory.identity import assign_slug, virtual_mxid
from observatory.omp_feed import (
    MessageEvent,
    OmpFeed,
    ThoughtEvent,
    ToolEvent,
)
from observatory.renderer import (
    DetachChild,
    PurgeRoom,
    RenderIntent,
    Renderer,
    SPACE_META_PREFIX,
    SendMessage,
    markdown_to_html,
)
from observatory.state import ObservatoryState, StateError

logger = logging.getLogger(__name__)

#: Topic marker on every manual-run room (D14 read-only signal).
READONLY_TOPIC = "read-only (manual run)"

#: Node id prefix; session uuid follows.
MANUAL_NODE_PREFIX = "manual:"

#: Quiet window before a room is reaped (D14).
DEFAULT_QUIET_WINDOW = 24.0 * 3600.0

#: Poll cadence for the file watch (seconds).
DEFAULT_POLL_INTERVAL = 5.0

FeedEvent = ToolEvent | ThoughtEvent | MessageEvent


# ============================================================================
# Session file discovery — pure-ish filesystem helpers
# ============================================================================


def sessions_root(agent_dir: str | Path) -> Path:
    return Path(agent_dir) / "sessions"


def scan_sessions(agent_dir: str | Path) -> list[Path]:
    """Top-level session files: ``sessions/<cwd-dir>/*.jsonl``. Artifact
    directories (subagent transcripts) nest deeper and are excluded, as
    are advisor transcripts and backups. Deterministic order."""
    root = sessions_root(agent_dir)
    if not root.is_dir():
        return []
    out: list[Path] = []
    for cwd_dir in sorted(root.iterdir()):
        if not cwd_dir.is_dir():
            continue
        for f in sorted(cwd_dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".jsonl"):
                continue
            if f.name.startswith("__advisor") or ".bak" in f.name:
                continue
            out.append(f)
    return out


def session_key(path: Path) -> str:
    """Stable node identity: the uuid in ``<ts>_<uuid>.jsonl`` (whole stem
    as fallback for hand-named files)."""
    stem = path.stem
    _, _, rest = stem.partition("_")
    return rest or stem


def read_header(path: Path, *, max_lines: int = 3) -> Optional[dict[str, Any]]:
    """The ``{"type": "session", ...}`` header entry (line 2 behind the
    fixed-width title slot on v3 files; tolerated anywhere in the first
    few lines). None when missing/corrupt."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(max_lines), f):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session":
                    return entry
    except OSError:
        return None
    return None


# ============================================================================
# Session entries → omp_feed typed events (converters REUSED, not duplicated)
# ============================================================================


def _args_str(args: Any) -> Optional[str]:
    if args is None:
        return None
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


class SessionParser:
    """``message`` JSONL entries → the exact typed events OmpFeed emits.

    Thinking/text go through ``OmpFeed._translate_event`` on synthesized
    ``subagent_event`` frames — the real converters own text-block joining
    and thinking-content fallback. toolCall blocks (``name`` +
    ``arguments`` object, no wire analog) construct ``ToolEvent`` directly.
    One converter-only ``OmpFeed`` instance is shared per parser; it is
    never started and never fed a real frame source.
    """

    def __init__(self) -> None:
        self._feed = OmpFeed(None)

    def parse_entry(self, entry: Any, agent_id: str) -> list[FeedEvent]:
        if not isinstance(entry, Mapping) or entry.get("type") != "message":
            return []
        msg = entry.get("message")
        if not isinstance(msg, Mapping):
            return []
        role = str(msg.get("role") or "assistant")
        content = msg.get("content")
        if isinstance(content, str):
            blocks: list[Any] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            return []

        events: list[FeedEvent] = []
        texts: list[str] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, Mapping):
                continue
            btype = block.get("type")
            if btype == "thinking":
                events.extend(
                    self._feed._translate_event(
                        {
                            "id": agent_id,
                            "event": {
                                "type": "message_update",
                                "message": {"role": role},
                                "assistantMessageEvent": {
                                    "type": "thinking_end",
                                    "contentIndex": index,
                                    "content": block.get("thinking") or "",
                                },
                            },
                        }
                    )
                )
            elif btype == "toolCall":
                name = str(block.get("name") or "")
                if name:
                    events.append(
                        ToolEvent(
                            subagent_id=agent_id,
                            tool=name,
                            args=_args_str(block.get("arguments")),
                        )
                    )
            elif btype == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts:
            events.extend(
                self._feed._translate_event(
                    {
                        "id": agent_id,
                        "event": {
                            "type": "message_end",
                            "message": {
                                "role": role,
                                "content": [
                                    {"type": "text", "text": t} for t in texts
                                ],
                            },
                        },
                    }
                )
            )
        return events


# ============================================================================
# Byte-offset tail
# ============================================================================


class _Tail:
    """Incremental line reader for one growing JSONL file."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self._buf = b""
        self._mtime: float | None = None

    def read_new_entries(self) -> list[Any]:
        """Complete JSON entries appended since the last call; a partial
        trailing line is buffered until its newline arrives. Truncation
        (size < offset) resets to 0 — a rewritten file replays cleanly.
        Same-size rewrites are detected via mtime (size alone cannot see
        them); idle files keep their offset so nothing ever replays."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        try:
            with self.path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size < self.offset:
                    self.offset = 0
                    self._buf = b""
                elif (
                    self.offset > 0
                    and mtime is not None
                    and self._mtime is not None
                    and mtime != self._mtime
                    and size <= self.offset
                ):
                    self.offset = 0
                    self._buf = b""
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        except OSError:
            return []
        self._mtime = mtime
        self.offset += len(chunk)
        data = self._buf + chunk
        *lines, self._buf = data.split(b"\n")
        entries: list[Any] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # torn or corrupt line — never kill the tail
        return entries


# ============================================================================
# Process liveness
# ============================================================================


def process_holds_session(session_path: Path, *, proc_root: str | Path = "/proc") -> bool:
    """True when any live process references the session — cmdline args
    carrying its path or uuid (``omp --resume <file>``, ``--fork <file>``,
    exporters). /proc scan IS the pid discovery; absence of a referencing
    process means "gone" (with the mtime window as the outer condition)."""
    needle = str(session_path).encode()
    short = session_key(session_path).encode()
    try:
        pids = [p for p in Path(proc_root).iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for pid_dir in pids:
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        if any(needle in part or short in part for part in raw.split(b"\0")):
            return True
    return False


# ============================================================================
# Watcher
# ============================================================================


@dataclass(frozen=True)
class SetRoomTopic:
    """``m.room.topic`` state write — the one intent the renderer has no
    op for; applied by the live path through the client directly."""

    room_key: str
    topic: str
    sender: str


@dataclass
class ManualPollResult:
    """One watch pass: fresh rooms, new transcript events, reaped rooms."""

    new_nodes: list[dict[str, Any]] = field(default_factory=list)
    events: dict[str, list[FeedEvent]] = field(default_factory=dict)
    reaped: list[str] = field(default_factory=list)


class ManualRunsWatcher:
    """Observe-only mirror of manual omp sessions (D14).

    Constructed over a :class:`~observatory.renderer.Renderer` (pure when
    executor=None). ``poll()`` is the one-shot watch pass (filesystem +
    state only); ``render_poll()`` is the live pass that also executes the
    planned intents and drops reaped rows. The sidecar's apply_plan pass
    must run between node creation and message rendering (rooms exist
    before anything is sent into them)."""

    def __init__(
        self,
        renderer: Renderer,
        *,
        agent_dir: str | Path,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        quiet_window: float = DEFAULT_QUIET_WINDOW,
        clock: Callable[[], float] = time.time,
        proc_root: str | Path = "/proc",
        process_probe: Optional[Callable[[Path], bool]] = None,
    ):
        self.renderer = renderer
        self.state: ObservatoryState = renderer.state
        self.agent_dir = Path(agent_dir)
        self.poll_interval = max(0.05, float(poll_interval))
        self.quiet_window = float(quiet_window)
        self._clock = clock
        self._parser = SessionParser()
        self._tails: dict[str, _Tail] = {}
        self._tracked: dict[str, Path] = {}
        self._activity: dict[str, float] = {}
        self._proc_root = proc_root
        self._probe = process_probe or (
            lambda path: process_holds_session(path, proc_root=self._proc_root)
        )

    # --- discovery -----------------------------------------------------------

    def _claimed_elsewhere(self, path: Path) -> bool:
        """Session already owned by discovery (delegate children keep their
        agent rooms — never a duplicate manual-run room)."""
        target = str(path)
        for row in self.state.get_live():
            if row["node_id"].startswith(MANUAL_NODE_PREFIX):
                continue
            if row.get("session_ref") == target:
                return True
            if str((row.get("extra") or {}).get("session_file") or "") == target:
                return True
        return False

    def poll(self) -> ManualPollResult:
        result = ManualPollResult()
        now = self._clock()
        seen: set[str] = set()

        for path in scan_sessions(self.agent_dir):
            seen.add(str(path))
            node_id = MANUAL_NODE_PREFIX + session_key(path)
            self._tracked[node_id] = path
            try:
                self.state.get(node_id)
            except StateError:
                if self._claimed_elsewhere(path):
                    continue
                header = read_header(path) or {}
                title = header.get("title")
                name = str(title).strip() if isinstance(title, str) and title.strip() else path.stem.split("_", 1)[0]
                slug = assign_slug(f"manual-{session_key(path)[:8]}", self.state)
                result.new_nodes.append(
                    self.state.add_node(
                        node_id,
                        engine="omp",
                        name=name,
                        slug=slug,
                        mxid=virtual_mxid(slug),
                        session_ref=str(path),
                        extra={
                            "kind": tree.KIND_MANUAL_RUN,
                            "session_file": str(path),
                        },
                    )
                )
                # Discovery is not activity: a freshly discovered session
                # ages from its FILE's last write (a 36h-quiet session found
                # by a cold sidecar reaps on the first eligible pass).
                try:
                    self._activity[node_id] = path.stat().st_mtime
                except OSError:
                    self._activity[node_id] = now
            is_new_tail = node_id not in self._tails
            tail = self._tails.setdefault(node_id, _Tail(path))
            entries = tail.read_new_entries()
            if entries and not is_new_tail:
                # Bytes appended AFTER discovery are activity; the initial
                # sync read is not (it would freeze every old session's
                # quiet clock at discovery time).
                self._activity[node_id] = now
            events: list[FeedEvent] = []
            for entry in entries:
                events.extend(self._parser.parse_entry(entry, node_id))
            if events:
                result.events[node_id] = events
            elif node_id not in self._activity:
                # Baseline for nodes that pre-date this watcher instance.
                try:
                    self._activity[node_id] = path.stat().st_mtime
                except OSError:
                    self._activity[node_id] = now

        for node_id, path in list(self._tracked.items()):
            if str(path) not in seen:
                self._reap(node_id)
                result.reaped.append(node_id)
                continue
            last = self._activity.get(node_id)
            if last is not None and (now - last) > self.quiet_window:
                if not self._probe(path):
                    self._reap(node_id)
                    result.reaped.append(node_id)
        return result

    def _reap(self, node_id: str) -> None:
        """Drop in-memory watchers; row/matrix removal is the render path's
        job (``plan_reap``/``render_poll``)."""
        self._tails.pop(node_id, None)
        self._tracked.pop(node_id, None)
        self._activity.pop(node_id, None)

    # --- planning (pure) --------------------------------------------------------

    def plan_new(self, row: Mapping[str, Any]) -> tuple[RenderIntent | SetRoomTopic, ...]:
        """Fresh-room intents: an observed-notice message plus the
        read-only topic marker."""
        node_id = row["node_id"]
        src = (
            f"👀 manual run observed — **{row['name']}** "
            f"(`{self.agent_dir.name} session`), read-only mirror"
        )
        return (
            SendMessage(node_id, row["mxid"], src, markdown_to_html(src)),
            SetRoomTopic(node_id, READONLY_TOPIC, row["mxid"]),
        )

    def plan_events(self, node_id: str, events: Sequence[FeedEvent]) -> tuple[RenderIntent, ...]:
        """Typed events → room messages, reusing the renderer's composers
        (one message per tool call, quoted thinking) plus plain text for
        completed messages — user prompts quoted, assistant text as-is."""
        row = self.state.get(node_id)
        intents: list[RenderIntent] = []
        for event in events:
            if isinstance(event, ToolEvent):
                intents.extend(self.renderer.plan_tool_call(node_id, event.tool, event.args))
            elif isinstance(event, ThoughtEvent):
                intents.extend(self.renderer.plan_thinking(node_id, event.text))
            elif isinstance(event, MessageEvent) and event.text:
                if event.role == "user":
                    intents.append(
                        SendMessage(
                            node_id,
                            row["mxid"],
                            f"» {event.text}",
                            markdown_to_html(f"> {event.text}"),
                        )
                    )
                else:
                    intents.append(
                        SendMessage(
                            node_id,
                            row["mxid"],
                            event.text,
                            markdown_to_html(event.text),
                        )
                    )
        return tuple(intents)

    def plan_reap(self, node_id: str) -> tuple[RenderIntent, ...]:
        """Admin-DELETE intents for a reaped manual run: detach the room
        from the "Manual runs" subspace (when the subspace is known) and
        purge it. The row drops afterwards (D17 — no tombstone)."""
        row = self.state.get(node_id)
        intents: list[RenderIntent] = []
        room_id = row.get("room_id")
        if room_id:
            try:
                subspace = self.state.get_meta(
                    SPACE_META_PREFIX + tree.MANUAL_RUNS_SPACE_KEY
                )
            except StateError:
                subspace = None
            if subspace:
                intents.append(DetachChild(subspace, room_id, self.renderer.gateway_mxid))
            intents.append(PurgeRoom(room_id))
        return tuple(intents)

    # --- live path ----------------------------------------------------------------

    async def render_poll(self) -> ManualPollResult:
        """Live watch pass: poll → converge the space plan (rooms for
        fresh nodes exist before anything is sent into them) → execute
        notice/topic/message/purge intents → drop reaped rows. Topic
        markers go straight to the client (``m.room.topic`` state event,
        as the room's own virtual user)."""
        executor = self.renderer.executor
        if executor is None:
            raise RuntimeError("live path requires a Renderer with an IntentExecutor")
        result = self.poll()
        await self.renderer.apply_plan(self.renderer.build_plan())

        intents: list[RenderIntent] = []
        topics: list[SetRoomTopic] = []
        for row in result.new_nodes:
            planned = self.plan_new(row)
            intents.extend(i for i in planned if isinstance(i, RenderIntent))
            topics.extend(i for i in planned if isinstance(i, SetRoomTopic))
        for node_id, events in result.events.items():
            intents.extend(self.plan_events(node_id, events))
        for node_id in result.reaped:
            intents.extend(self.plan_reap(node_id))

        if intents:
            await executor.execute(intents)
        for topic in topics:
            await executor.client.send_state_event(
                executor.room_id(topic.room_key),
                "m.room.topic",
                "",
                {"topic": topic.topic},
                sender=topic.sender,
            )
        for node_id in result.reaped:
            self.state.mark_deleted_and_purge(node_id)
        return result
