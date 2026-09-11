"""M4b (matrix observatory §5/D10): approvals in rooms.

Bridge between Mercury's approval stream and the per-agent Matrix rooms:

* **Ingest** — an approval request raised by ANY agent (any depth, either
  engine) becomes ONE prompt message in THAT agent's room: the command,
  a truncated context line, and the /approve / /deny hint (D10).
* **Resolution** — ``/approve`` / ``/deny`` (also ``!``-prefixed, D13) as
  a Matrix REPLY to the approval prompt resolves the request back through
  its backend, keyed by (agent node, request id):

  - ``gateway`` backend → ``tools.approval.resolve_gateway_approval``
    (the hermes guard queue — the same resolver the gateway chat and the
    api_server runs surface use);
  - ``socket`` backend → the child's approval socket
    (``MERCURY_APPROVAL_SOCKET``, wire shape of
    ``tools/omp_delegation.py`` ``_ApprovalBridgeServer`` and omp's
    ``headless-approval.ts``: ``POST /approve {kind, title}`` →
    ``{"value": "Approve"|"Deny", "confirmed": bool}``). The
    :class:`ObservatoryApprovalServer` here OWNS such a socket and
    answers each POST from the Matrix decision.

* **Authority** — only a user with write power in the agent's room may
  resolve (D7: write access == steer authority). Agent voices
  (``@merc_*``) never count as decisions, message edits never re-decide,
  and reactions NEVER resolve anything (D10 — explicit decision only; a
  👍 on the prompt is ignored).
* **Timeout** — defaults to the existing gateway approval timeout
  (``tools.approval._get_approval_timeout``, config ``approvals.timeout``,
  fallback 300s). On expiry the bridge posts an expiry notice and LETS
  THE EXISTING DEFAULT PATH DECIDE: the gateway wait loop resolves its
  own deny; an unanswered socket POST is answered fail-closed Deny (the
  child's existing default when no human answers).
* **Multi-pending** — one message per pending request; the reply target
  is the approval message itself. Bare ``/approve`` in the agent room is
  accepted only while exactly ONE approval is pending there.

The bridge is deliberately inert I/O-wise: everything Matrix-bound goes
through the injected ``poster`` (a :class:`observatory.matrix_client.MatrixClient`
duck-type), and every decision function (authority, gateway resolver,
timeout, clock) is injectable, so the whole module tests without a
homeserver, a gateway, or an omp child.
"""
from __future__ import annotations

import asyncio
import http.server
import inspect
import itertools
import json
import logging
import os
import re
import shutil
import socket as _socket
import socketserver
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any, Awaitable, Callable, Mapping, Optional

from observatory.identity import VIRTUAL_USER_PREFIX
from observatory.renderer import markdown_to_html, truncate
from observatory.state import ObservatoryState, StateError

log = logging.getLogger(__name__)

# --- D10 vocabulary -----------------------------------------------------------------

#: Option words accepted after /approve (spec §5: "/approve always").
SCOPE_WORDS = ("once", "session", "always")

DEFAULT_SCOPE = "once"

#: Display budgets for the prompt message (command + truncated context).
COMMAND_MAX_CHARS = 240
CONTEXT_MAX_CHARS = 400

#: Fallback approval timeout when ``tools.approval`` cannot be imported
#: (matches its own default, ``approvals.timeout`` 300).
DEFAULT_TIMEOUT_S = 300.0

#: Authority check: async (room_id, sender_mxid) -> bool.
AuthorityCheck = Callable[[str, str], Awaitable[bool]]

#: Gateway resolver: (session_key, choice, request_id, reason) -> int
#: (count resolved; 0 = nothing pending). Sync or async.
GatewayResolver = Callable[[str, str, str, Optional[str]], Any]

#: Poster: async send_message(room_id, body, *, sender, formatted_body=None)
#: -> event_id — the MatrixClient surface the bridge needs.
Poster = Any


# ============================================================================
# Command parsing — pure
# ============================================================================


@dataclass(frozen=True)
class ApprovalCommand:
    """One parsed /approve or /deny room command (D10/D13)."""

    verb: str  # "approve" | "deny"
    scope: str  # approve: once|session|always; deny: ""
    reason: str  # deny-only free text ("" when absent)
    text: str  # the raw command body as received


def parse_approval_command(text: str) -> Optional[ApprovalCommand]:
    """Parse ``/approve [once|session|always]`` / ``/deny [reason]``
    (and the ``!``-prefixed forms, D13). Returns None for anything else —
    other room text belongs to the other control-router slices."""
    body = (text or "").strip()
    if not body or body[0] not in "/!":
        return None
    words = body[1:].split(None, 1)
    if not words:
        return None
    verb = words[0].lower()
    rest = words[1] if len(words) > 1 else ""
    if verb == "approve":
        scope = ""
        if rest:
            head = rest.split(None, 1)
            if head[0].lower() in SCOPE_WORDS:
                scope = head[0].lower()
        return ApprovalCommand("approve", scope or DEFAULT_SCOPE, "", body)
    if verb == "deny":
        return ApprovalCommand("deny", "", rest.strip(), body)
    return None


# ============================================================================
# Power levels — pure D7 math
# ============================================================================


def user_power_level(power_levels: Mapping[str, Any], user_id: str) -> int:
    """Effective PL of ``user_id`` (explicit users map, else users_default)."""
    users = power_levels.get("users") or {}
    if user_id in users:
        return int(users[user_id])
    return int(power_levels.get("users_default", 0))


def required_write_level(power_levels: Mapping[str, Any]) -> int:
    """PL needed to send m.room.message (event-specific, else events_default)."""
    events = power_levels.get("events") or {}
    if "m.room.message" in events:
        return int(events["m.room.message"])
    return int(power_levels.get("events_default", 0))


def can_write(power_levels: Mapping[str, Any], user_id: str) -> bool:
    """D7: write access to an agent's room == authority to steer (approve)."""
    return user_power_level(power_levels, user_id) >= required_write_level(power_levels)


def is_agent_voice(mxid: str) -> bool:
    """True for observatory virtual users — agent output, never a decision."""
    return str(mxid or "").lstrip("@").startswith(VIRTUAL_USER_PREFIX)


# ============================================================================
# Message composition — pure (§5 style: plain body + minimal markdown html)
# ============================================================================


def _de_fenced(markdown: str) -> str:
    """Plain-text fallback body: code fences collapse to indented lines."""
    lines: list[str] = []
    fence = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            fence = not fence
            continue
        lines.append(f"    {line}" if fence else line)
    plain = "\n".join(lines)
    return re.sub(r"`([^`\n]+)`", r"\1", plain)  # de-backtick inline code


def approval_prompt_message(
    command: str, context: str = "", *, request_id: str = ""
) -> tuple[str, str]:
    """The D10 approval prompt: command + truncated context + reply hint."""
    cmd, cut = truncate(command, COMMAND_MAX_CHARS)
    ctx, ctx_cut = truncate(context, CONTEXT_MAX_CHARS) if context else ("", False)
    src = "🔐 **approval needed** — reply to this message with `/approve` or `/deny`\n"
    src += f"```bash\n{cmd}{' …' if cut else ''}\n```"
    if ctx:
        src += f"\nContext: {ctx}{' …' if ctx_cut else ''}"
    src += (
        "\nOptions: `/approve once` · `/approve session` · `/approve always`"
        " · `/deny [reason]`"
    )
    if request_id:
        src += f"\nrequest `{request_id[:12]}`"
    return _de_fenced(src), markdown_to_html(src)


def resolved_notice(cmd: ApprovalCommand, *, command: str = "") -> tuple[str, str]:
    """Posted in the agent room when a decision landed."""
    head, _ = truncate(command, 80)
    if cmd.verb == "approve":
        src = f"✔ approved (`{cmd.scope}`)" + (f" — {head}" if head else "")
    else:
        src = "🚫 denied" + (f" — {cmd.reason}" if cmd.reason else "")
        if head:
            src += f"\n{head}"
    return _de_fenced(src), markdown_to_html(src)


def expiry_notice(timeout_s: float) -> tuple[str, str]:
    """Posted when the approval timeout elapses (existing path decides)."""
    src = (
        f"⌛ approval expired after {int(timeout_s)}s unanswered — "
        "the default denial path takes over"
    )
    return src, markdown_to_html(src)


def wrong_room_notice() -> tuple[str, str]:
    src = "⛔ wrong room — that approval prompt belongs to another agent's room"
    return src, markdown_to_html(src)


def not_authoritative_notice(sender: str) -> tuple[str, str]:
    src = f"⛔ {sender} lacks write authority in this room — approval ignored"
    return _de_fenced(src), markdown_to_html(src)


def no_pending_notice() -> tuple[str, str]:
    src = "no pending approval to resolve in this room"
    return src, markdown_to_html(src)


def ambiguous_notice(count: int) -> tuple[str, str]:
    src = (
        f"{count} approvals pending here — reply to the exact approval "
        "message you are deciding"
    )
    return src, markdown_to_html(src)


def late_notice() -> tuple[str, str]:
    src = (
        "too late — that approval was already resolved elsewhere "
        "(timed out or answered on another surface)"
    )
    return src, markdown_to_html(src)


# ============================================================================
# Defaults — lazy bridges into the hermes approval stack
# ============================================================================


def default_approval_timeout() -> float:
    """The existing gateway approval timeout (config ``approvals.timeout``,
    default 300). Falls back to the same default when ``tools.approval``
    is unavailable (sidecar imported outside the hermes package)."""
    try:
        from tools.approval import _get_approval_timeout

        return float(_get_approval_timeout())
    except Exception:
        return DEFAULT_TIMEOUT_S


def _default_gateway_resolver(
    session_key: str, choice: str, request_id: str, reason: Optional[str]
) -> int:
    from tools.approval import resolve_gateway_approval

    return int(
        resolve_gateway_approval(
            session_key, choice, reason=reason, request_id=request_id or None
        )
    )


def split_approval_prompt(title: str) -> tuple[str, str]:
    """(command, context) from an omp approval prompt title: the
    ``Command:`` line (to end — the transport's DOTALL regex) is the
    command, everything before it is presentation context."""
    try:
        from tools.omp_rpc_transport import extract_command_from_prompt

        command = extract_command_from_prompt(title)
    except Exception:
        command = None
    if not command:
        return title, ""
    context = re.sub(r"Command:\s*.*\Z", "", title, flags=re.DOTALL).strip()
    return command, context


# ============================================================================
# Pending state
# ============================================================================


class SocketAnswer:
    """One-shot, thread-safe answer slot for a pending approval-socket POST.

    The child's HTTP POST blocks on :meth:`wait`; the Matrix decision (or
    the expiry path) lands via :meth:`set_result` exactly once.
    """

    __slots__ = ("_lock", "_event", "_value", "_confirmed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._value = "Deny"
        self._confirmed = False

    def set_result(self, value: str, confirmed: Optional[bool] = None) -> bool:
        """Record the decision. False when already answered (late loser)."""
        with self._lock:
            if self._event.is_set():
                return False
            self._value = "Approve" if value == "Approve" else "Deny"
            self._confirmed = self._value == "Approve" if confirmed is None else bool(confirmed)
            self._event.set()
            return True

    def wait(self, timeout: float) -> bool:
        return self._event.wait(max(0.0, timeout))

    def snapshot(self) -> tuple[str, bool]:
        return self._value, self._confirmed


@dataclass
class PendingApproval:
    """One pending approval mirrored into an agent room."""

    node_id: str
    request_id: str
    backend: str  # "gateway" | "socket"
    room_id: str
    prompt_event_id: str
    session_key: str = ""
    command: str = ""
    answer: Optional[SocketAnswer] = None  # socket backend
    budget: float = 0.0  # seconds this approval waited before expiry
    created: float = 0.0
    deadline: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        return (self.node_id, self.request_id)


# ============================================================================
# ApprovalBridge
# ============================================================================


class ApprovalBridge:
    """Approval stream ⇄ agent rooms (one per sidecar).

    Constructor seams, all injectable for tests:

    - ``state`` — :class:`observatory.state.ObservatoryState`; a node's
      row supplies the room id and the virtual-user voice of the room.
    - ``poster`` — MatrixClient-shaped ``send_message`` (the only Matrix
      call the bridge makes).
    - ``authority`` — async ``(room_id, sender) -> bool``; production uses
      :class:`MatrixAuthority` (the D7 power-level check).
    - ``resolve_gateway`` — the backend resolver; default wraps
      ``tools.approval.resolve_gateway_approval`` lazily.
    - ``timeout`` — per-pending budget; default
      :func:`default_approval_timeout` (the existing gateway timeout).
    - ``clock`` — monotonic clock.
    """

    def __init__(
        self,
        *,
        state: ObservatoryState,
        poster: Poster,
        authority: AuthorityCheck,
        resolve_gateway: Optional[GatewayResolver] = None,
        timeout: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.state = state
        self.poster = poster
        self.authority = authority
        self._resolve_gateway_fn = resolve_gateway or _default_gateway_resolver
        self._timeout = timeout
        self._clock = clock
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # (node_id, request_id) -> PendingApproval
        self._pending: dict[tuple[str, str], PendingApproval] = {}
        # prompt event id -> (node_id, request_id) — the reply-target index
        self._by_prompt_event: dict[str, tuple[str, str]] = {}

    # --- loop plumbing (ingest from non-async threads) ------------------------

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """The sidecar loop, captured on first async bridge use (or
        :meth:`capture_loop` — the approval socket server calls it)."""
        return self._loop

    def capture_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _capture(self) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

    # --- lookups ---------------------------------------------------------------

    def effective_timeout(self) -> float:
        # Live read (no cache) when no explicit timeout was given: the
        # bridge budget must track config approvals.timeout (default 300)
        # — a cached first read drifts from the guard wait loop and the
        # room prompt expires on a different clock than the waiter.
        if self._timeout is not None:
            return self._timeout
        return default_approval_timeout()

    def _room_voice(self, node_id: str) -> tuple[str, str]:
        """(room_id, virtual-user mxid) of an agent node — StateError when
        the node is unknown or not yet provisioned."""
        row = self.state.get(node_id)
        room_id = row.get("room_id")
        mxid = row.get("mxid")
        if not room_id or not mxid:
            raise StateError(f"node {node_id!r} has no provisioned room")
        return str(room_id), str(mxid)

    def _voice_for_room(self, room_id: str) -> Optional[str]:
        """The virtual user owning a room (the voice for notices there)."""
        for row in self.state.get_live():
            if row.get("room_id") == room_id and row.get("mxid"):
                return str(row["mxid"])
        return None

    def pending_for(self, node_id: str, request_id: str) -> Optional[PendingApproval]:
        return self._pending.get((node_id, request_id))

    def pendings_in_room(self, room_id: str) -> list[PendingApproval]:
        return [p for p in self._pending.values() if p.room_id == room_id]

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # --- ingest --------------------------------------------------------------------

    async def submit(
        self,
        node_id: str,
        request_id: str,
        *,
        backend: str,
        session_key: str = "",
        command: str = "",
        context: str = "",
        answer: Optional[SocketAnswer] = None,
        timeout: Optional[float] = None,
    ) -> Optional[PendingApproval]:
        """Post ONE approval prompt into ``node_id``'s room and track it.

        Returns the pending (multi-pending: one message per request), or
        None when the node/room cannot be resolved — the caller then
        falls back to the existing default path (an approval-socket POST
        is answered fail-closed immediately)."""
        self._capture()
        try:
            room_id, sender = self._room_voice(node_id)
        except StateError:
            log.warning("M4b: approval for unknown/unprovisioned node %r dropped", node_id)
            return None
        body, formatted = approval_prompt_message(command, context, request_id=request_id)
        event_id = await self.poster.send_message(
            room_id, body, sender=sender, formatted_body=formatted
        )
        now = self._clock()
        budget = timeout if timeout is not None else self.effective_timeout()
        old = self._pending.pop((node_id, request_id), None)
        if old is not None:
            self._by_prompt_event.pop(old.prompt_event_id, None)
        pending = PendingApproval(
            node_id=node_id,
            request_id=request_id,
            backend=backend,
            room_id=room_id,
            prompt_event_id=event_id,
            session_key=session_key,
            command=command,
            answer=answer,
            budget=budget,
            created=now,
            deadline=now + max(0.0, budget),
        )
        self._pending[pending.key] = pending
        self._by_prompt_event[event_id] = pending.key
        return pending

    # --- expiry ----------------------------------------------------------------------

    async def check_expiry(self) -> list[PendingApproval]:
        """Expire overdue pendings: post the expiry notice, drop them, and
        LET THE EXISTING DEFAULT PATH DECIDE —

        - gateway backend: NOT resolved here; ``tools.approval``'s blocking
          wait owns the same timeout and resolves its own denial;
        - socket backend: the POST is answered fail-closed Deny (the
          child's existing default when no human answers).

        Returns the expired pendings (already dropped)."""
        now = self._clock()
        expired = [p for p in self._pending.values() if now >= p.deadline]
        for p in expired:
            self._drop(p)
            if p.answer is not None:
                p.answer.set_result("Deny", False)
            try:
                await self._notice(p.room_id, *expiry_notice(p.budget))
            except Exception:
                log.exception("M4b: expiry notice failed for %s", p.key)
        return expired

    # --- control-router entry ----------------------------------------------------------

    async def handle_event(self, event: Mapping[str, Any]) -> Optional[str]:
        """One pushed Matrix event (appservice transaction shape).

        Returns a short routing action for the sidecar's audit log, or
        None when the event is not approval-router traffic at all:

        - ``ignored:reaction``     — D10: reactions NEVER resolve
        - ``ignored:edit``        — a re-decided command edit
        - ``ignored:agent_voice`` — virtual users are agent output
        - ``denied:no_authority`` — sender fails the D7 power-level check
        - ``denied:wrong_room``   — reply targets a prompt in another room
        - ``denied:ambiguous``    — bare command, 2+ pendings in the room
        - ``denied:no_pending``   — nothing pending for this room/target
        - ``late:already_resolved`` — backend had nothing pending
        - ``resolved:approve`` / ``resolved:deny``
        """
        self._capture()
        etype = str(event.get("type") or "")
        content = event.get("content") or {}
        if etype != "m.room.message":
            rel = content.get("m.relates_to") or {}
            if etype == "m.reaction" or rel.get("rel_type") == "m.annotation":
                return "ignored:reaction"  # D10: explicit decision only
            return None
        rel = content.get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace":
            return "ignored:edit"  # an edit never re-decides
        sender = str(event.get("sender") or "")
        room_id = str(event.get("room_id") or "")
        if is_agent_voice(sender):
            return "ignored:agent_voice"
        command = parse_approval_command(str(content.get("body") or ""))
        if command is None:
            return None  # other control-router slices own it

        if not await self._authoritative(room_id, sender):
            await self._notice(room_id, *not_authoritative_notice(sender))
            return "denied:no_authority"

        pending, action = await self._target(room_id, content)
        if pending is None:
            return action

        if pending.backend == "socket":
            if pending.answer is None or not pending.answer.set_result(
                "Approve" if command.verb == "approve" else "Deny"
            ):
                self._drop(pending)
                await self._notice(room_id, *late_notice())
                return "late:already_resolved"
            self._drop(pending)
            await self._notice(room_id, *resolved_notice(command, command=pending.command))
            return f"resolved:{command.verb}"

        choice = command.scope if command.verb == "approve" else "deny"
        try:
            resolved = self._resolve_gateway_fn(
                pending.session_key, choice, pending.request_id, command.reason or None
            )
            if inspect.isawaitable(resolved):
                resolved = await resolved
        except Exception:
            # Resolver broke (not "nothing pending") — keep the pending so
            # the user can retry; the intake transaction must not wedge.
            log.exception("M4b: gateway resolver raised for %s", pending.key)
            return "error:resolver"
        if not int(resolved or 0):
            self._drop(pending)
            await self._notice(room_id, *late_notice())
            return "late:already_resolved"
        self._drop(pending)
        await self._notice(room_id, *resolved_notice(command, command=pending.command))
        return f"resolved:{command.verb}"

    async def _target(
        self, room_id: str, content: Mapping[str, Any]
    ) -> tuple[Optional[PendingApproval], str]:
        """The pending this command decides, per D10 targeting.

        Reply (``m.in_reply_to``) or thread-root matching against the
        approval prompt's event id first; a BARE command is accepted only
        while exactly one approval is pending in this room. A reply that
        targets a prompt living in ANOTHER room is denied with a notice —
        never resolved (wrong-room rule). A reply/thread matching NO live
        prompt (stale target, stripped metadata, or an event id the bridge
        never observed) falls back to the room's sole pending so a visible
        prompt stays resolvable by /approve in the SAME room; with zero or
        several pendings there the room-scoped answer stands."""
        rel = content.get("m.relates_to") or {}
        candidates: list[str] = []
        reply = rel.get("m.in_reply_to") or {}
        if reply.get("event_id"):
            candidates.append(str(reply["event_id"]))
        if rel.get("rel_type") == "m.thread" and rel.get("event_id"):
            candidates.append(str(rel["event_id"]))
        for event_id in candidates:
            key = self._by_prompt_event.get(event_id)
            if key is None:
                continue
            pending = self._pending.get(key)
            if pending is None:
                continue
            if pending.room_id != room_id:
                await self._notice(room_id, *wrong_room_notice())
                return None, "denied:wrong_room"
            return pending, f"targeted:{event_id}"
        here = self.pendings_in_room(room_id)
        if candidates:
            # Reply/thread matching no live prompt: stale, already resolved,
            # stripped, or never observed — the user still typed /approve in
            # a room with a visible prompt. Resolve the sole pending; keep
            # the empty/ambiguous answers otherwise.
            if len(here) == 1:
                return here[0], "targeted:sole-fallback"
            if not here:
                await self._notice(room_id, *no_pending_notice())
                return None, "denied:no_pending"
            await self._notice(room_id, *ambiguous_notice(len(here)))
            return None, "denied:ambiguous"
        if not here:
            await self._notice(room_id, *no_pending_notice())
            return None, "denied:no_pending"
        if len(here) > 1:
            await self._notice(room_id, *ambiguous_notice(len(here)))
            return None, "denied:ambiguous"
        return here[0], "targeted:sole"

    async def _authoritative(self, room_id: str, sender: str) -> bool:
        try:
            return bool(await self.authority(room_id, sender))
        except Exception:
            log.exception("M4b: authority check failed for %s in %s", sender, room_id)
            return False

    async def _post(self, room_id: str, body: str, formatted: str) -> Optional[str]:
        sender = self._voice_for_room(room_id)
        if sender is None:
            log.warning("M4b: no voice found for room %s — notice dropped", room_id)
            return None
        return await self.poster.send_message(
            room_id, body, sender=sender, formatted_body=formatted
        )

    async def _notice(self, room_id: str, body: str, formatted: str) -> None:
        try:
            await self._post(room_id, body, formatted)
        except Exception:
            log.exception("M4b: notice post failed in %s", room_id)

    def _drop(self, pending: PendingApproval) -> None:
        self._pending.pop(pending.key, None)
        self._by_prompt_event.pop(pending.prompt_event_id, None)
    def discard_pending(self, node_id: str, request_id: str) -> bool:
        """Drop one mirrored pending the guard already decided (M4b stale fix).

        The omp guard owns the decision inline; when it auto-resolves, the
        Matrix prompt submitted just before it can never be human-resolved.
        Dropping it (no late notice — nothing is late, the guard decided)
        keeps a later /approve honest (no_pending, not late:already_resolved
        against a moot prompt). Returns True when something was dropped.
        """
        pending = self._pending.get((node_id, request_id))
        if pending is None:
            return False
        self._drop(pending)
        return True

    def discard_node_request(self, request_id: str) -> bool:
        """Drop any pending carrying *request_id* regardless of node (settle hook)."""
        for key, pending in list(self._pending.items()):
            if pending.request_id == request_id:
                self._drop(pending)
                return True
        return False

    # --- teardown -----------------------------------------------------------------------

    def forget_node(self, node_id: str) -> None:
        """Agent died: drop its pendings without resolving (the gateway
        queue drains via its own unregister/timeout; a blocked socket POST
        is unblocked fail-closed). No notice — the room may be purging."""
        for key, pending in list(self._pending.items()):
            if pending.node_id == node_id:
                self._drop(pending)
                if pending.answer is not None:
                    pending.answer.set_result("Deny", False)


# ============================================================================
# MatrixAuthority — the production D7 check
# ============================================================================


class MatrixAuthority:
    """Async authority check backed by live ``m.room.power_levels``.

    Reads state as ``reader`` (any member virtual user — production: the
    gateway agent); write == PL >= the room's m.room.message threshold."""

    def __init__(self, client: Any, *, reader_mxid: str):
        self.client = client
        self.reader_mxid = reader_mxid

    async def __call__(self, room_id: str, sender: str) -> bool:
        pl = await self.client.get_power_levels(room_id, sender=self.reader_mxid)
        return can_write(pl or {}, sender)


# ============================================================================
# ObservatoryApprovalServer — the Matrix-answerable approval socket
# ============================================================================


class _UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    address_family = _socket.AF_UNIX
    allow_reuse_address = True

    def server_bind(self):
        # Same discipline as omp_delegation._ApprovalBridgeServer:
        # HTTPServer.server_bind() resolves a hostname (a multi-second DNS
        # stall on resolver-less boxes) and unpacks a unix path as
        # (host, port). Bind plainly; nothing reads server_name here.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "mercury-observatory-approvals"
        self.server_port = 0

    def get_request(self):
        req, _ = self.socket.accept()
        return req, ("unix", 0)


class ObservatoryApprovalServer:
    """Unix-socket HTTP server speaking the omp approval-socket wire
    protocol (``POST /approve {kind, title[, message]}`` → ``{"value":
    "Approve"|"Deny", "confirmed": bool}`` — the shape of omp's
    ``headless-approval.ts`` client and hermes' ``_ApprovalBridgeServer``).

    Unlike the delegation bridge (guard stack decides inline), the
    DECISION HERE IS THE MATRIX REPLY: each POST is mirrored into
    ``node_id``'s room as a pending approval and the HTTP response is
    whatever /approve or /deny answers — or fail-closed Deny on timeout /
    unknown node, exactly the child's existing default path.

    One server per agent node: children spawned with
    ``MERCURY_APPROVAL_SOCKET=<this socket>`` get their approvals surfaced
    in THAT agent's room. ``start()`` must run on the sidecar loop."""

    def __init__(
        self,
        bridge: ApprovalBridge,
        node_id: str,
        *,
        timeout: Optional[float] = None,
    ):
        self._bridge = bridge
        self._node_id = node_id
        # None = live bridge budget (tracks approvals.timeout); an explicit
        # value pins the socket wait (tests). Never snapshot the bridge
        # default at construction — that freezes a stale budget.
        self._timeout: Optional[float] = timeout
        self._dir = tempfile.mkdtemp(prefix="mercury-approval-")
        self._path = os.path.join(self._dir, "approval.sock")
        self._counter = itertools.count(1)
        self._started = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — http.server API
                if self.path != "/approve":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    self.send_error(400)
                    return
                value, confirmed = outer._decide(payload)
                body = json.dumps({"value": value, "confirmed": confirmed}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self._server = _UnixHTTPServer(self._path, Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="mercury-observatory-approvals",
        )
    def _budget(self) -> float:
        if self._timeout is not None:
            return self._timeout
        return self._bridge.effective_timeout()

    def _decide(self, payload: Mapping[str, Any]) -> tuple[str, bool]:
        """Mirror one POST into the agent's room; wait for the Matrix
        decision; fail closed (Deny) on timeout or unmirrorable request."""
        kind = str(payload.get("kind") or "select")
        title = str(payload.get("title") or "")
        message = str(payload.get("message") or "")
        if kind == "select":
            command, context = split_approval_prompt(title)
        else:
            command, context = title, message
        answer = SocketAnswer()
        assert self._loop is not None  # start() ran on the sidecar loop
        budget = self._budget()
        future = asyncio.run_coroutine_threadsafe(
            self._bridge.submit(
                self._node_id,
                f"sock-{next(self._counter)}",
                backend="socket",
                command=command,
                context=context or message,
                answer=answer,
                timeout=budget,
            ),
            self._loop,
        )
        try:
            pending = future.result(timeout=budget + 5.0)
        except Exception:
            log.exception("M4b: approval submit failed (fail-closed deny)")
            return "Deny", False
        if pending is None:
            return "Deny", False  # no room: nobody can answer — existing default
        if not answer.wait(budget):
            answer.set_result("Deny", False)  # local expiry — existing default path
        return answer.snapshot()

    # --- lifecycle ------------------------------------------------------------------

    async def start(self) -> str:
        """Bind + serve; returns the socket path (set as the child's
        ``MERCURY_APPROVAL_SOCKET``). Must be called on the sidecar loop."""
        self._loop = self._bridge.capture_loop()
        os.chmod(self._path, 0o600)
        self._thread.start()
        self._started = True
        return self._path

    def stop(self) -> None:
        # shutdown() blocks until serve_forever exits — skip it entirely
        # when the server never started (constructor-only instances).
        if self._started:
            try:
                self._server.shutdown()
            except Exception:
                pass
        self._started = False
        try:
            self._server.server_close()
        except Exception:
            pass
        shutil.rmtree(self._dir, ignore_errors=True)

# ============================================================================
# Ingest adapters — thread-side sources onto the sidecar loop
# ============================================================================


def gateway_notify(
    bridge: ApprovalBridge, node_id: str, session_key: str
) -> Callable[[Mapping[str, Any]], None]:
    """Per-session notify callback for
    ``tools.approval.register_gateway_notify(session_key, cb)``: mirrors
    hermes guard approval requests into ``node_id``'s room.

    The callback runs on the blocked agent thread; the submit is scheduled
    onto the sidecar loop (the pattern the register_gateway_notify
    docstring prescribes). The agent thread itself keeps blocking in the
    gateway wait loop — resolution arrives via
    ``resolve_gateway_approval`` exactly as on every other surface."""

    def notify(approval_data: Mapping[str, Any]) -> None:
        loop = bridge.loop
        if loop is None or loop.is_closed():
            log.warning("M4b: gateway notify before loop capture — dropped")
            return
        data = dict(approval_data)
        asyncio.run_coroutine_threadsafe(
            bridge.submit(
                node_id,
                str(data.get("request_id") or ""),
                backend="gateway",
                session_key=session_key,
                command=str(data.get("command") or ""),
                context=str(data.get("description") or ""),
            ),
            loop,
        )

    return notify


def rpc_frame_callback(
    bridge: ApprovalBridge, node_id: str, session_key: str
) -> Callable[[str, str, str, tuple], None]:
    """Callback for ``tools.omp_rpc_transport.set_approval_frame_hook``:
    mirrors an omp RPC child's approval gate (extension_ui_request select)
    into that child's room while the guard stack still owns the decision.

    ``session_key`` is the delegation's inherited hermes session — the
    queue the guard decision blocks in, and therefore the queue a Matrix
    /approve resolves through resolve_gateway_approval."""

    def on_frame(request_id: str, method: str, title: str, options: tuple) -> None:
        loop = bridge.loop
        if loop is None or loop.is_closed():
            return
        command, context = split_approval_prompt(title)
        asyncio.run_coroutine_threadsafe(
            bridge.submit(
                node_id,
                request_id or f"rpc:{node_id}",
                backend="gateway",
                session_key=session_key,
                command=command or title,
                context=context or "omp RPC approval gate",
            ),
            loop,
        )

    return on_frame


# ============================================================================
# Test/client helper — HTTP over the approval unix socket (omp child shape)
# ============================================================================


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a unix socket — how the sidecar and the tests speak the
    approval-socket protocol (omp's client uses fetch({unix: …}))."""

    def __init__(self, socket_path: str, *, timeout: float = 30.0):
        super().__init__("mercury-approval.local", timeout=timeout)
        self._unix_path = socket_path

    def connect(self):
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


class ApprovalSocketClient:
    """Minimal client for the approval-socket wire protocol over a unix
    socket — the exact request omp's ``headless-approval.ts`` makes.
    Used by tests and by the sidecar's own probes."""

    def __init__(self, socket_path: str, *, timeout: float = 30.0):
        self._path = socket_path
        self._timeout = timeout

    def post_approve(
        self, *, kind: str = "select", title: str = "", message: str = ""
    ) -> tuple[int, dict]:
        conn = _UnixHTTPConnection(self._path, timeout=self._timeout)
        payload = json.dumps({"kind": kind, "title": title, "message": message})
        conn.request(
            "POST", "/approve", body=payload, headers={"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            parsed = json.loads(raw or b"{}")
        except Exception:
            parsed = {}
        return response.status, parsed
