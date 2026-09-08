"""M4a (matrix observatory spec §5, D7/D10/D13): the control router.

Room messages from appservice transactions -> typed user intents -> routed
control actions per agent class, with honest UX states and enforcement
gates. Pure data in, pure data out: the router owns PARSING, GATING and
LEDGER state; the engine bindings (gateway ``slash.exec``/session
injection, omp RPC ``steer``/``prompt``/``abort``/``subagent_steer``/
``subagent_abort``, approval resolution) and the Matrix send of notices
live in the future sidecar_main, which executes the returned actions and
notices. This module must stay importable without aiohttp, homeserver, or
a live omp child — mirroring the renderer's planning/execution split.

Layers:

1. **Parsing** (:func:`parse_intent`) — a message body becomes exactly one
   of: ``SteerText`` (plain chat), ``SidecarVerb`` (``/stop /status /cot
   /approve /deny``, both ``/`` and ``!`` prefixes per D13), or
   ``EngineCommand`` (any other ``/word``/``!word`` — engine-native
   pass-through: hermes rooms -> gateway slash dispatch, omp rooms -> the
   RPC prompt 3-stage path).
2. **Gating**, in order: own-echo drop (virtual users), room->node
   resolution, kind gate (cron rooms notification-only D11, manual runs
   observe-only D14), liveness (settled rooms: steering disabled, §5.3),
   power levels (D7: write access to an agent's room == authority to
   steer; below-``events_default`` senders get an explanatory notice and
   NOTHING is routed — fail-closed when the snapshot is missing), and the
   D13 scope gate (gateway-lifecycle verbs like ``/restart``/``/update``
   and the pause family are accepted only from the gateway agent's room).
3. **Routing per agent class** (:class:`AgentClass`): hermes-side
   orchestrators and children -> session injection; omp mains -> RPC
   ``steer`` mid-run / ``prompt`` (new turn) when idle; omp grandchildren
   -> ``subagent_steer`` over the ancestor child's transport.
4. **Honesty ledger** (§5): steers answer "⏳ queued steer" immediately,
   then "✔ applied" when the child confirms the injection — an
   ``OmpFeed`` message carrying the steer echo (matched verbatim modulo
   decoration) or a synthetic ack hook via :meth:`observe_ack`. ``/stop``
   answers "🛑 stop requested — waiting for boundary" until an abort is
   observed on a lifecycle frame (:meth:`observe_death`).

``/cot on|off`` persists per room in ``state.db`` meta (§5.2, default on)
and is read back by :meth:`ControlRouter.cot_enabled`.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Sequence

from observatory.identity import VIRTUAL_USER_PREFIX
from observatory.renderer import ROOM_META_PREFIX, SETTLED_MARKER
from observatory.state import ObservatoryState, StateError
from observatory.tree import DIRECTIVES_ROOM_KEY

logger = logging.getLogger(__name__)

# --- D13 vocabularies ---------------------------------------------------------

#: Sidecar verbs recognized with BOTH ``/verb`` and ``!verb`` (D13; Matrix
#: clients reserve ``/``, Element passes unknown ``/words`` through).
SIDECAR_VERBS = frozenset({"stop", "status", "cot", "approve", "deny"})

#: Gateway-lifecycle verbs whose blast radius is the gateway process
#: itself (D13): accepted ONLY from the gateway agent's room. They are
#: engine-native commands (gateway slash registry) — the sidecar scopes
#: them, it does not implement them.
GATEWAY_ONLY_VERBS = frozenset(
    {"restart", "update", "pause", "resume", "pause-for-update", "resume-for-update"}
)

#: Option words accepted after /approve and /deny (§5 approvals).
APPROVAL_SCOPES = ("once", "session", "always")

#: Default approval scope when the sender gives none (the guard plumbing's
#: minimal-grant default).
APPROVAL_SCOPE_DEFAULT = "once"

# --- /cot persistence (§5.2) ---------------------------------------------------

COT_META_PREFIX = "cot:"
COT_ON = "on"
COT_OFF = "off"

#: Per-node cap on UNCONFIRMED steers: the child must ack earlier steers
#: before the sidecar queues an unbounded pile (a dead-but-undetected
#: child would otherwise swallow steers forever).
STEER_QUEUE_CAP_DEFAULT = 64

# --- §5 honesty strings (asserted verbatim by tests) ---------------------------

QUEUED_STEER_NOTICE = "⏳ queued steer"
APPLIED_STEER_NOTICE = "✔ applied"
STOP_REQUESTED_NOTICE = "🛑 stop requested — waiting for boundary"
STOP_CONFIRMED_NOTICE = "🛑 stop confirmed — abort observed ({status})"

READ_ONLY_NOTICE = (
    "🔒 read-only: you can watch this agent, but steering and commands "
    "need write power in this room — ask the owner (D7)."
)
PL_UNAVAILABLE_NOTICE = (
    "🔒 power-level snapshot unavailable for this room — treating you as "
    "read-only (fail-closed)."
)
SETTLED_STEER_NOTICE = f"{SETTLED_MARKER} — steering disabled"
CRON_ROOM_NOTICE = (
    "ℹ️ cron rooms are notification-only (D11); jobs are managed elsewhere."
)
MANUAL_RUN_NOTICE = (
    "ℹ️ manual runs are observe-only in v1 (D14) — no steering channel "
    "exists for TUI sessions."
)
SCOPE_GATE_NOTICE = (
    "🚫 /{verb} is a gateway-lifecycle command — accepted only from the "
    "gateway agent's room (D13)."
)
STEER_QUEUE_FULL_NOTICE = (
    "⏳ steer queue full — earlier steers are not confirmed yet; retry "
    "after they apply."
)
STOP_IDLE_NOTICE = "💤 idle — nothing to stop."
COT_USAGE_NOTICE = "usage: /cot on|off — toggles thinking display for this room"
COT_HERMES_NOTE = " (note: hermes-side CoT stays hidden — D5)"
APPROVAL_USAGE_NOTICE = "usage: /{verb} [once|session|always]"
NO_APPROVAL_PENDING_NOTICE = (
    "no pending approval prompt in this room — /approve and /deny reply "
    "to a prompt."
)
APPROVAL_SENT_NOTICE = "✔ /{decision} sent (scope: {scope})"
SUBAGENT_NO_COMMAND_NOTICE = (
    "omp subagents take plain-text steers only — no engine command "
    "surface (D13)."
)


# ============================================================================
# Intents — what a message body MEANS (pure parsing, D13)
# ============================================================================


@dataclass(frozen=True)
class SteerText:
    """Plain chat: steer the agent with this text (§5)."""

    text: str


@dataclass(frozen=True)
class SidecarVerb:
    """A sidecar-intercepted verb: /stop /status /cot /approve /deny."""

    verb: str
    args: tuple[str, ...]
    prefix: str  # "/" | "!"
    raw: str


@dataclass(frozen=True)
class EngineCommand:
    """Any other ``/word``/``!word`` — pass-through to the engine's native
    command surface (gateway slash dispatch for hermes rooms, omp RPC
    prompt 3-stage handling for omp rooms; D13)."""

    verb: str
    text: str  # the raw body, forwarded verbatim
    prefix: str  # "/" | "!"


Intent = SteerText | SidecarVerb | EngineCommand


def parse_intent(body: str) -> Intent:
    """Body -> intent. ``/verb`` and ``!verb`` are equivalent (D13); a
    lone ``/``, ``!``, or ``/ word`` (prefix with no glued verb) is plain
    chat. Verb matching is case-insensitive (``/Stop`` == ``/stop``);
    pass-through keeps the raw text verbatim for the engine registry."""
    text = (body or "").strip()
    if not text or text[0] not in "/!":
        return SteerText(text)
    prefix = text[0]
    tokens = text.split()
    verb = tokens[0][1:].lower()
    if not verb:
        return SteerText(text)
    if verb in SIDECAR_VERBS:
        return SidecarVerb(verb, tuple(tokens[1:]), prefix, text)
    return EngineCommand(verb, text, prefix)


# ============================================================================
# Agent classes — routing destinations (§5 steering table)
# ============================================================================


class AgentClass(Enum):
    """Where control for a node is executed."""

    GATEWAY = "gateway"  # the gateway agent (kind=gateway) — slash full registry
    HERMES_SESSION = "hermes-session"  # hermes-side orchestrators + children
    OMP_MAIN = "omp-main"  # omp child main sessions (own RPC transport)
    OMP_SUBAGENT = "omp-subagent"  # omp in-process grandchildren (via ancestor)


def _extra_kind(node: Mapping[str, Any]) -> str:
    extra = node.get("extra") or {}
    return extra.get("kind", "") if isinstance(extra, Mapping) else ""


# ============================================================================
# Power levels — D7 model (write == steer authority)
# ============================================================================


@dataclass(frozen=True)
class RoomPowerLevels:
    """The slice of ``m.room.power_levels`` the gate needs: a sender may
    write (== steer, D7) iff their level >= ``events_default`` — exactly
    the server's rule for sending ``m.room.message``."""

    users: Mapping[str, int] = field(default_factory=dict)
    events_default: int = 0
    users_default: int = 0

    def level(self, user_id: str) -> int:
        return self.users.get(user_id, self.users_default)

    def can_write(self, user_id: str) -> bool:
        return self.level(user_id) >= self.events_default


class PowerLevelSnapshot:
    """Dict-backed PL provider: ``router(room_id) -> RoomPowerLevels |
    None``. The real sidecar swaps in a live snapshot from the client API;
    tests build static ones. A room missing from the snapshot reads as
    None — the router fails CLOSED (read-only notice), never guesses."""

    def __init__(self, rooms: Mapping[str, RoomPowerLevels]):
        self._rooms = dict(rooms)

    def __call__(self, room_id: str) -> Optional[RoomPowerLevels]:
        return self._rooms.get(room_id)


PowerLevelProvider = Callable[[str], Optional[RoomPowerLevels]]

#: Busyness oracle for omp mains: True = mid-run (steer), False = idle
#: (prompt a new turn). Absent probe -> assume busy (steer is the safe,
#: non-duplicating default).
BusyProbe = Callable[[str], bool]


# ============================================================================
# Actions + notices — pure data the sidecar executes
# ============================================================================


@dataclass(frozen=True)
class InjectText:
    """Hermes-side: inject text into the session at the next iteration
    boundary (gateway ``slash.exec``/injection path). ``kind`` distinguishes
    steers from engine command pass-through for the binding layer."""

    node_id: str
    text: str
    kind: str  # "steer" | "command"


@dataclass(frozen=True)
class AbortSession:
    """/stop on a hermes-side agent: session abort at the next boundary."""

    node_id: str
    reason: str


@dataclass(frozen=True)
class OmpSteer:
    """omp main mid-run: RPC ``steer`` (injected at the next safe
    boundary; the in-flight tool call is never cut)."""

    node_id: str
    text: str


@dataclass(frozen=True)
class OmpPrompt:
    """omp main idle: RPC ``prompt`` — steer degrades to a new turn."""

    node_id: str
    text: str


@dataclass(frozen=True)
class OmpAbortMain:
    """/stop on an omp main: RPC ``abort``."""

    node_id: str
    reason: str


@dataclass(frozen=True)
class OmpSubagentSteer:
    """omp grandchild: ``subagent_steer`` over the ancestor child's
    transport (registry id comes from the node's ``extra``)."""

    node_id: str
    text: str


@dataclass(frozen=True)
class OmpSubagentAbort:
    """/stop on an omp grandchild: ``subagent_abort``."""

    node_id: str
    reason: str


@dataclass(frozen=True)
class ResolveApproval:
    """/approve|/deny: hand the decision to the existing approval plumbing
    (§5; ``tools.approval.resolve_gateway_approval`` in the real binding)."""

    node_id: str
    decision: str  # "approve" | "deny"
    scope: str  # once | session | always
    reply_to: Optional[str]
    prompt_id: str


Action = (
    InjectText
    | AbortSession
    | OmpSteer
    | OmpPrompt
    | OmpAbortMain
    | OmpSubagentSteer
    | OmpSubagentAbort
    | ResolveApproval
)


@dataclass(frozen=True)
class ControlNotice:
    """A message the agent's virtual user posts back into its room.
    ``reply_to`` (the triggering event id) marks rejection notices so the
    sender sees them as replies."""

    node_id: str
    body: str
    reply_to: Optional[str] = None


@dataclass(frozen=True)
class RoutingOutcome:
    """One routed message: what the sidecar must DO (actions), SAY
    (notices), and WHY (disposition — audit + tests)."""

    node_id: Optional[str]
    disposition: str
    actions: tuple[Action, ...] = ()
    notices: tuple[ControlNotice, ...] = ()


# ============================================================================
# Honesty ledger (§5 queued/applied, stop boundary waits)
# ============================================================================


@dataclass
class PendingSteer:
    node_id: str
    text: str
    state: str  # "queued" | "applied"
    created_epoch: float


@dataclass
class PendingStop:
    node_id: str
    state: str  # "requested" | "confirmed"
    created_epoch: float
    status: Optional[str] = None  # lifecycle status that confirmed it


@dataclass
class PendingApproval:
    node_id: str
    prompt_id: str
    summary: str
    created_epoch: float


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _echo_matches(pending_text: str, echo: str) -> bool:
    """Ack rule: the feed echo carries the steer verbatim, possibly
    decorated (e.g. a ``[steer] …`` prefix) — never paraphrased."""
    n = _norm(pending_text)
    e = _norm(echo)
    return bool(n) and (n == e or e.endswith(n) or n in e)


def _reply_to(content: Mapping[str, Any]) -> Optional[str]:
    relates = content.get("m.relates_to")
    if isinstance(relates, Mapping):
        in_reply = relates.get("m.in_reply_to")
        if isinstance(in_reply, Mapping):
            event_id = in_reply.get("event_id")
            if isinstance(event_id, str) and event_id:
                return event_id
    return None


def _is_virtual_user(sender: str) -> bool:
    """True for the sidecar's own virtual users (``@merc_*``) — their
    messages are echoes, never control input."""
    if not sender.startswith("@"):
        return False
    localpart = sender[1:].split(":", 1)[0]
    return localpart.startswith(VIRTUAL_USER_PREFIX)


# ============================================================================
# The router
# ============================================================================


class ControlRouter:
    """Message ingestion -> gated, class-routed control actions.

    Collaborators are injectable so tests need no homeserver, no gateway
    WS, no omp child:

    - ``state`` — :class:`~observatory.state.ObservatoryState` (rooms,
      engines, tree position, /cot persistence).
    - ``gateway_node_id`` — the gateway agent's node (D13 scope anchor).
    - ``pl_provider`` — ``room_id -> RoomPowerLevels | None`` (D7).
    - ``busy_probe`` — optional ``node_id -> bool`` (omp mains: steer vs
      prompt-new-turn).
    """

    def __init__(
        self,
        state: ObservatoryState,
        *,
        gateway_node_id: str,
        pl_provider: PowerLevelProvider,
        busy_probe: Optional[BusyProbe] = None,
        steer_queue_cap: int = STEER_QUEUE_CAP_DEFAULT,
        clock: Callable[[], float] = time.time,
    ):
        self.state = state
        self.gateway_node_id = gateway_node_id
        self.pl_provider = pl_provider
        self.busy_probe = busy_probe
        self.steer_queue_cap = steer_queue_cap
        self._clock = clock
        self._steers: list[PendingSteer] = []
        self._stops: dict[str, PendingStop] = {}
        self._approvals: dict[str, PendingApproval] = {}

    # --- reads ---------------------------------------------------------------

    def agent_class_of(self, node_id: str) -> AgentClass:
        """Node -> routing class. An omp node is a MAIN session iff its
        parent is not itself omp (the delegated RPC child IS the main
        session of the omp process); any deeper omp node is an in-process
        subagent steered through the ancestor's transport."""
        if node_id == self.gateway_node_id:
            return AgentClass.GATEWAY
        row = self.state.get(node_id)
        if row["engine"] == "hermes":
            return AgentClass.HERMES_SESSION
        parent_id = row.get("parent_node_id")
        if not parent_id:
            return AgentClass.OMP_MAIN
        try:
            parent = self.state.get(parent_id)
        except StateError:  # orphaned row mid-purge: treat as main
            return AgentClass.OMP_MAIN
        return AgentClass.OMP_MAIN if parent["engine"] != "omp" else AgentClass.OMP_SUBAGENT

    def cot_enabled(self, node_id: str) -> bool:
        """§5.2 per-room thinking toggle, persisted in state meta. Default
        ON; anything but an explicit ``off`` reads as on."""
        try:
            return self.state.get_meta(COT_META_PREFIX + node_id) != COT_OFF
        except StateError:
            return True

    @property
    def pending_steers(self) -> tuple[PendingSteer, ...]:
        return tuple(self._steers)

    @property
    def pending_stops(self) -> tuple[PendingStop, ...]:
        return tuple(self._stops.values())

    def pending_approval(self, node_id: str) -> Optional[PendingApproval]:
        return self._approvals.get(node_id)

    # --- ingestion -----------------------------------------------------------

    async def handle_transaction(
        self, txn_id: str, events: Sequence[Mapping[str, Any]]
    ) -> list[RoutingOutcome]:
        """``TransactionIntake`` EventHandler shape (appservice.py): route
        every event, isolate per-event failures (the intake already
        refuses to let handler bugs kill the queue; this keeps one bad
        event from hiding the rest of its transaction)."""
        outcomes: list[RoutingOutcome] = []
        for event in events:
            if not isinstance(event, Mapping):
                outcomes.append(RoutingOutcome(None, "drop:not-an-event"))
                continue
            try:
                outcomes.append(self.route(event))
            except Exception:  # noqa: BLE001 — per-event isolation
                logger.exception("control router failed on event in txn %s", txn_id)
                outcomes.append(RoutingOutcome(None, "drop:router-error"))
        return outcomes

    def route(self, event: Mapping[str, Any]) -> RoutingOutcome:
        """One Matrix event -> one outcome. Gates in order (see module
        docstring); the FIRST rejection wins and nothing is routed."""
        if event.get("type") != "m.room.message":
            return RoutingOutcome(None, "drop:not-a-message")
        content = event.get("content") or {}
        if not isinstance(content, Mapping):
            return RoutingOutcome(None, "drop:bad-content")
        if content.get("m.new_content") is not None:
            return RoutingOutcome(None, "drop:edit")
        if content.get("msgtype", "m.text") not in ("m.text", "m.notice"):
            return RoutingOutcome(None, "drop:msgtype")
        body = content.get("body")
        if not isinstance(body, str) or not body.strip():
            return RoutingOutcome(None, "drop:empty-body")
        sender = str(event.get("sender") or "")
        if _is_virtual_user(sender):
            return RoutingOutcome(None, "drop:own-echo")

        room_id = str(event.get("room_id") or "")
        node = self._find_node_by_room(room_id)
        if node is None:
            disposition = (
                "drop:directives-room"
                if self._is_directives_room(room_id)
                else "drop:unknown-room"
            )
            return RoutingOutcome(None, disposition)
        node_id = node["node_id"]
        reply_to = _reply_to(content)

        kind = _extra_kind(node)
        if kind == "cron-job":
            return self._notice(node_id, "notice:cron-room", CRON_ROOM_NOTICE, reply_to)
        if kind == "manual-run":
            return self._notice(node_id, "notice:manual-run", MANUAL_RUN_NOTICE, reply_to)
        if node.get("status") != "live":
            return self._notice(
                node_id, "notice:settled", SETTLED_STEER_NOTICE, reply_to
            )

        try:
            levels = self.pl_provider(room_id)
        except Exception:  # noqa: BLE001 — a broken snapshot must fail closed
            logger.exception("power-level provider failed for room %s", room_id)
            levels = None
        if levels is None:
            return self._notice(
                node_id, "notice:power-levels-unavailable", PL_UNAVAILABLE_NOTICE, reply_to
            )
        if not levels.can_write(sender):
            return self._notice(node_id, "notice:read-only", READ_ONLY_NOTICE, reply_to)

        intent = parse_intent(body)
        if isinstance(intent, SteerText):
            return self._route_steer(node, intent.text)
        if isinstance(intent, SidecarVerb):
            return self._route_verb(node, intent, reply_to)
        return self._route_command(node, intent)

    # --- routing per class ----------------------------------------------------

    def _route_steer(self, node: Mapping[str, Any], text: str) -> RoutingOutcome:
        node_id = node["node_id"]
        agent_class = self.agent_class_of(node_id)
        if agent_class in (AgentClass.GATEWAY, AgentClass.HERMES_SESSION):
            actions: tuple[Action, ...] = (InjectText(node_id, text, "steer"),)
        elif agent_class is AgentClass.OMP_MAIN:
            actions = (
                (OmpPrompt(node_id, text) if not self._is_busy(node_id) else OmpSteer(node_id, text),)
            )
        else:
            actions = (OmpSubagentSteer(node_id, text),)
        pending = self._enqueue_steer(node_id, text)
        if pending is None:
            return self._notice(
                node_id, "notice:steer-queue-full", STEER_QUEUE_FULL_NOTICE
            )
        return RoutingOutcome(
            node_id,
            "steer",
            actions,
            (ControlNotice(node_id, QUEUED_STEER_NOTICE),),
        )

    def _route_verb(
        self, node: Mapping[str, Any], intent: SidecarVerb, reply_to: Optional[str]
    ) -> RoutingOutcome:
        node_id = node["node_id"]
        verb = intent.verb
        if verb == "stop":
            return self._route_stop(node, intent, reply_to)
        if verb == "status":
            return RoutingOutcome(
                node_id, "status", notices=(ControlNotice(node_id, self._status_body(node)),)
            )
        if verb == "cot":
            return self._route_cot(node, intent)
        return self._route_approval(node, intent, reply_to)

    def _route_stop(
        self, node: Mapping[str, Any], intent: SidecarVerb, reply_to: Optional[str]
    ) -> RoutingOutcome:
        node_id = node["node_id"]
        agent_class = self.agent_class_of(node_id)
        reason = " ".join(intent.args).strip() or "matrix /stop"
        if agent_class is AgentClass.OMP_MAIN and not self._is_busy(node_id):
            return self._notice(node_id, "notice:stop-idle", STOP_IDLE_NOTICE, reply_to)
        if agent_class in (AgentClass.GATEWAY, AgentClass.HERMES_SESSION):
            action: Action = AbortSession(node_id, reason)
        elif agent_class is AgentClass.OMP_MAIN:
            action = OmpAbortMain(node_id, reason)
        else:
            action = OmpSubagentAbort(node_id, reason)
        self._stops[node_id] = PendingStop(node_id, "requested", self._clock())
        return RoutingOutcome(
            node_id,
            "stop",
            (action,),
            (ControlNotice(node_id, STOP_REQUESTED_NOTICE, reply_to),),
        )

    def _route_cot(self, node: Mapping[str, Any], intent: SidecarVerb) -> RoutingOutcome:
        node_id = node["node_id"]
        arg = intent.args[0].lower() if intent.args else ""
        if len(intent.args) > 1 or arg not in (COT_ON, COT_OFF):
            return self._notice(node_id, "notice:cot-usage", COT_USAGE_NOTICE)
        self.state.set_meta(COT_META_PREFIX + node_id, arg)
        body = f"🧠 thinking display: {arg}"
        if self.agent_class_of(node_id) in (AgentClass.GATEWAY, AgentClass.HERMES_SESSION):
            body += COT_HERMES_NOTE
        return RoutingOutcome(node_id, "cot", notices=(ControlNotice(node_id, body),))

    def _route_approval(
        self, node: Mapping[str, Any], intent: SidecarVerb, reply_to: Optional[str]
    ) -> RoutingOutcome:
        node_id = node["node_id"]
        decision = intent.verb  # "approve" | "deny"
        scope = intent.args[0].lower() if intent.args else APPROVAL_SCOPE_DEFAULT
        if len(intent.args) > 1 or scope not in APPROVAL_SCOPES:
            return self._notice(
                node_id,
                "notice:approval-usage",
                APPROVAL_USAGE_NOTICE.format(verb=decision),
                reply_to,
            )
        pending = self._approvals.pop(node_id, None)
        if pending is None:
            return self._notice(
                node_id, "notice:no-approval-pending", NO_APPROVAL_PENDING_NOTICE, reply_to
            )
        action = ResolveApproval(node_id, decision, scope, reply_to, pending.prompt_id)
        notice = ControlNotice(
            node_id, APPROVAL_SENT_NOTICE.format(decision=decision, scope=scope), reply_to
        )
        return RoutingOutcome(node_id, "approval", (action,), (notice,))

    def _route_command(
        self, node: Mapping[str, Any], intent: EngineCommand
    ) -> RoutingOutcome:
        node_id = node["node_id"]
        agent_class = self.agent_class_of(node_id)
        if intent.verb in GATEWAY_ONLY_VERBS and agent_class is not AgentClass.GATEWAY:
            return self._notice(
                node_id,
                "notice:scope-gate",
                SCOPE_GATE_NOTICE.format(verb=intent.verb),
            )
        if agent_class in (AgentClass.GATEWAY, AgentClass.HERMES_SESSION):
            # Gateway slash dispatch (full registry in the gateway room;
            # session-scoped registry for spawned agents — D13).
            actions: tuple[Action, ...] = (InjectText(node_id, intent.text, "command"),)
        elif agent_class is AgentClass.OMP_MAIN:
            # omp prompt 3-stage: ACP builtins intercept; unmatched text
            # reaches the model.
            actions = (
                (OmpPrompt(node_id, intent.text) if not self._is_busy(node_id) else OmpSteer(node_id, intent.text),)
            )
        else:
            return self._notice(
                node_id, "notice:subagent-no-commands", SUBAGENT_NO_COMMAND_NOTICE
            )
        return RoutingOutcome(node_id, "command", actions)

    # --- observation (feed glue drives the honesty ledger) ---------------------

    def observe_ack(self, node_id: str, echo_text: str) -> tuple[ControlNotice, ...]:
        """The child confirmed an injection: an ``OmpFeed`` message
        carrying the steer echo (verbatim, maybe decorated), or the
        synthetic ack hook the gateway binding fires. Newest matching
        queued steer for the node flips to applied."""
        echo = _norm(echo_text)
        for pending in reversed(self._steers):
            if (
                pending.node_id == node_id
                and pending.state == "queued"
                and _echo_matches(pending.text, echo)
            ):
                pending.state = "applied"
                return (ControlNotice(node_id, APPLIED_STEER_NOTICE),)
        return ()

    def observe_death(self, node_id: str, status: str) -> tuple[ControlNotice, ...]:
        """A lifecycle frame reports the node dead (discovery or omp_feed
        vocabulary — the glue maps to node ids). Confirms a pending stop;
        queued steers expire silently (the renderer owns death rendering)."""
        notices: list[ControlNotice] = []
        stop = self._stops.get(node_id)
        if stop is not None and stop.state == "requested":
            stop.state = "confirmed"
            stop.status = status
            notices.append(
                ControlNotice(node_id, STOP_CONFIRMED_NOTICE.format(status=status))
            )
        self._steers = [
            p for p in self._steers if p.node_id != node_id or p.state == "applied"
        ]
        return tuple(notices)

    def observe_approval_prompt(
        self, node_id: str, prompt_id: str, summary: str = ""
    ) -> None:
        """A guard approval prompt surfaced in the agent's room (§5): the
        sidecar glue registers it so /approve|/deny replies resolve."""
        self._approvals[node_id] = PendingApproval(
            node_id, prompt_id, summary, self._clock()
        )

    # --- internals --------------------------------------------------------------

    def _is_busy(self, node_id: str) -> bool:
        if self.busy_probe is None:
            return True
        return bool(self.busy_probe(node_id))

    def _enqueue_steer(self, node_id: str, text: str) -> Optional[PendingSteer]:
        queued = [
            p for p in self._steers if p.node_id == node_id and p.state == "queued"
        ]
        if len(queued) >= self.steer_queue_cap:
            return None
        pending = PendingSteer(node_id, text, "queued", self._clock())
        self._steers.append(pending)
        # Bounded ledger: applied entries serve /status and ack dedup only;
        # prune oldest beyond a small multiple of the queue cap.
        max_total = self.steer_queue_cap * 4
        if len(self._steers) > max_total:
            applied = [p for p in self._steers if p.state == "applied"]
            for stale in applied[: len(self._steers) - max_total]:
                self._steers.remove(stale)
        return pending

    def _status_body(self, node: Mapping[str, Any]) -> str:
        node_id = node["node_id"]
        agent_class = self.agent_class_of(node_id)
        lines = [
            f"📊 {node['name']} — {node['engine']}/{agent_class.value}",
            f"depth {node['depth']} · {node['status']}",
        ]
        # omp classes always report busyness; without a probe "yes" is the
        # same safe default the steer routing uses (steer, never prompt).
        if agent_class in (AgentClass.OMP_MAIN, AgentClass.OMP_SUBAGENT):
            lines.append(f"busy: {'yes' if self._is_busy(node_id) else 'no'}")
        steers = [p for p in self._steers if p.node_id == node_id]
        queued = sum(1 for p in steers if p.state == "queued")
        applied = sum(1 for p in steers if p.state == "applied")
        lines.append(f"steers queued/applied: {queued}/{applied}")
        stop = self._stops.get(node_id)
        if stop is not None:
            suffix = f" ({stop.status})" if stop.status else ""
            lines.append(f"stop: {stop.state}{suffix}")
        approval = self._approvals.get(node_id)
        if approval is not None:
            lines.append(f"approval pending: {approval.prompt_id}")
        lines.append(
            f"thinking display: {COT_ON if self.cot_enabled(node_id) else COT_OFF}"
        )
        return "\n".join(lines)

    def _notice(
        self,
        node_id: str,
        disposition: str,
        body: str,
        reply_to: Optional[str] = None,
    ) -> RoutingOutcome:
        return RoutingOutcome(
            node_id, disposition, notices=(ControlNotice(node_id, body, reply_to),)
        )

    def _find_node_by_room(self, room_id: str) -> Optional[dict[str, Any]]:
        """room_id -> node row. Live nodes first (the steer-while-running
        law makes them the only real targets); then dead descendants of
        live nodes — settled rooms keep answering with the §5.3 marker."""
        if not room_id:
            return None
        live = self.state.get_live()
        for row in live:
            if row.get("room_id") == room_id:
                return row
        for row in live:
            for sub in self.state.get_subtree(row["node_id"]):
                if sub["node_id"] != row["node_id"] and sub.get("room_id") == room_id:
                    return sub
        return None

    def _is_directives_room(self, room_id: str) -> bool:
        """§6 directives room: mention-gated delivery is Phase 5 — M4a
        only names the drop so logs don't misread it as an unknown room."""
        try:
            return (
                self.state.get_meta(ROOM_META_PREFIX + DIRECTIVES_ROOM_KEY) == room_id
            )
        except StateError:
            return False


__all__ = [
    "APPROVAL_SCOPE_DEFAULT",
    "APPROVAL_SCOPES",
    "APPROVAL_SENT_NOTICE",
    "APPROVAL_USAGE_NOTICE",
    "AbortSession",
    "Action",
    "AgentClass",
    "BusyProbe",
    "CRON_ROOM_NOTICE",
    "COT_HERMES_NOTE",
    "COT_META_PREFIX",
    "COT_OFF",
    "COT_ON",
    "COT_USAGE_NOTICE",
    "ControlNotice",
    "ControlRouter",
    "EngineCommand",
    "GATEWAY_ONLY_VERBS",
    "InjectText",
    "Intent",
    "MANUAL_RUN_NOTICE",
    "NO_APPROVAL_PENDING_NOTICE",
    "OmpAbortMain",
    "OmpPrompt",
    "OmpSteer",
    "OmpSubagentAbort",
    "OmpSubagentSteer",
    "PL_UNAVAILABLE_NOTICE",
    "PendingApproval",
    "PendingSteer",
    "PendingStop",
    "PowerLevelProvider",
    "PowerLevelSnapshot",
    "QUEUED_STEER_NOTICE",
    "READ_ONLY_NOTICE",
    "ResolveApproval",
    "RoutingOutcome",
    "SCOPE_GATE_NOTICE",
    "SETTLED_STEER_NOTICE",
    "SIDECAR_VERBS",
    "STEER_QUEUE_CAP_DEFAULT",
    "STEER_QUEUE_FULL_NOTICE",
    "STOP_CONFIRMED_NOTICE",
    "STOP_IDLE_NOTICE",
    "STOP_REQUESTED_NOTICE",
    "SidecarVerb",
    "SUBAGENT_NO_COMMAND_NOTICE",
    "SteerText",
    "RoomPowerLevels",
    "parse_intent",
]
