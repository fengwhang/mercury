"""M5b (spec §6 / D12): the directives room — membership manager and
mention-gated delivery fan-out.

Membership = the gateway agent + every live 0-agent (spawned orchestrators
of both engines — plain depth-0 roots). Cron pseudo-rooms and manual-run
roots are NOT agents and never join. Membership is derived from state.db on
every call, so the same idempotent diff serves spawn (join), death (leave)
and the D18 respawn pass (re-ensure).

Delivery is mention-gated (D12): a directive reaches ONLY the members whose
virtual user is @-mentioned in the owner's message; ``@room``/``@everyone``
expands to every member. Mentions are read from ``m.mentions.user_ids``
(MSC3952 intentional mentions) with a plain-text ``@mention`` scan as
fallback. No valid member mention → short help notice naming the members.
Delivery per engine: gateway agent → its session injection; hermes 0-agents
→ the sidecar's injection callable (labeled ``[directive] <text>``); omp
0-agents → RPC steer / new turn. Agents NEVER reply in this room —
:meth:`DirectivesManager.outbound_allowed` is the renderer-side filter that
suppresses member-MXID sends into the directives room (the sidecar's own
notices speak as the gateway agent and always pass).

All matrix effects are renderer RenderIntents; the ``plan_*`` methods are
pure (renderer with ``executor=None``) and mirrored by live paths that
execute through the renderer's executor. The rolling delivery receipt
reuses the renderer's dashboard edit machinery (first send tagged, every
later update an in-place edit — D15).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from observatory import tree
from observatory.identity import VIRTUAL_USER_PREFIX
from observatory.renderer import (
    ROOM_META_PREFIX,
    InviteUser,
    JoinRoom,
    LeaveRoom,
    RenderIntent,
    Renderer,
    SendMessage,
    markdown_to_html,
)
from observatory.state import ObservatoryState, StateError

logger = logging.getLogger(__name__)

#: Hermes-side injection label (spec §6: inject as labeled user message
#: ``"[directive] <text>"`` — new turn when idle, queued after the current
#: turn when busy; the queueing is the injection path's own semantics).
DIRECTIVE_LABEL = "[directive]"

#: Delivery statuses the rolling receipt renders with their spec icons.
STATUS_APPLIED = "applied"
STATUS_QUEUED = "queued"

#: Body tokens that expand to every member (the D12 supertool).
_EVERYONE_RE = re.compile(r"(?<![\w@])@(?:room|everyone)\b", re.IGNORECASE)

#: Delivery sinks: async callables the sidecar injects. Each returns a
#: short status string for the receipt ("applied"/"queued"/...).
HermesSink = Callable[[Mapping[str, Any], str], Awaitable[Optional[str]]]
OmpSink = Callable[[Mapping[str, Any], str], Awaitable[Optional[str]]]
GatewaySink = Callable[[str], Awaitable[Optional[str]]]


# ============================================================================
# Mention parsing — pure
# ============================================================================


@dataclass(frozen=True)
class MentionScan:
    """Result of scanning one m.room.message content for mentions."""

    everyone: bool = False
    mxids: frozenset[str] = frozenset()


def parse_mentions(
    content: Mapping[str, Any], members: Sequence[tuple[str, str]]
) -> MentionScan:
    """Owner message content → (everyone?, mentioned mxids).

    ``members`` are ``(mxid, display name)`` pairs of current members.
    Sources, in order: ``m.mentions.user_ids`` (intentional mentions),
    then a fallback ``@mention`` text scan of ``body`` matching the full
    localpart (``@merc_<slug>``), the bare slug (``@<slug>``), and the
    display name — covering clients that render mentions as plain text.
    Unknown mxids pass through; membership filtering happens at resolution.
    """
    body = content.get("body") if isinstance(content, Mapping) else None
    body = body if isinstance(body, str) else ""
    everyone = bool(_EVERYONE_RE.search(body))

    mentioned: set[str] = set()
    mentions = content.get("m.mentions")
    if isinstance(mentions, Mapping):
        ids = mentions.get("user_ids")
        if isinstance(ids, list):
            mentioned.update(i for i in ids if isinstance(i, str))

    if body:
        lowered = body.lower()
        for mxid, name in members:
            localpart = mxid[1:].split(":", 1)[0]
            slug = (
                localpart[len(VIRTUAL_USER_PREFIX):]
                if localpart.startswith(VIRTUAL_USER_PREFIX)
                else localpart
            )
            for candidate in (f"@{localpart}", f"@{slug}", f"@{name}"):
                if candidate.lower() in lowered:
                    mentioned.add(mxid)
                    break
    return MentionScan(everyone=everyone, mxids=frozenset(mentioned))


# ============================================================================
# Message composition — pure
# ============================================================================


def receipt_body(entries: Sequence[tuple[str, str]], *, now: Optional[float] = None) -> tuple[str, str]:
    """Rolling delivery receipt (spec §6 example: ``✔ applied:
    auth-refactor 14:02``). ``(plain, formatted)`` like every renderer
    composer."""
    stamp = time.strftime("%H:%M", time.localtime(now if now is not None else time.time()))
    lines = [f"📋 directive — {len(entries)} target(s)"]
    for name, status in entries:
        icon = {STATUS_APPLIED: "✔", STATUS_QUEUED: "🕓"}.get(status, "✖")
        lines.append(f"{icon} {status}: {name} {stamp}")
    body = "\n".join(lines)
    return body, markdown_to_html(body)


def help_notice_body(members: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """No-valid-mention help notice naming the current members (D12)."""
    names = ", ".join(f"@{row['name']}" for row in members)
    src = (
        "💬 No member mentioned — @-mention agents by name, or use "
        "@room / @everyone for everyone.\n"
        f"Members: {names}"
    )
    return src, markdown_to_html(src)


# ============================================================================
# Manager
# ============================================================================


@dataclass
class DirectiveOutcome:
    """What one directives-room message produced."""

    ignored: bool = False                       # non-owner sender: no effect
    targets: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[tuple[str, str]] = field(default_factory=list)  # (name, status)
    intents: tuple[RenderIntent, ...] = ()


class DirectivesManager:
    """Membership + delivery for the directives room (spec §6).

    Constructed over a :class:`~observatory.renderer.Renderer` (pure when
    its executor is None); delivery sinks are async callables owned by the
    sidecar. All state reads go through the renderer's ``ObservatoryState``.
    """

    def __init__(
        self,
        renderer: Renderer,
        *,
        deliver_hermes: Optional[HermesSink] = None,
        deliver_omp: Optional[OmpSink] = None,
        deliver_gateway: Optional[GatewaySink] = None,
    ):
        self.renderer = renderer
        self.state: ObservatoryState = renderer.state
        self.owner_mxid = renderer.owner_mxid
        self.deliver_hermes = deliver_hermes
        self.deliver_omp = deliver_omp
        self.deliver_gateway = deliver_gateway

    # --- membership ------------------------------------------------------------

    @property
    def gateway_node_id(self) -> str:
        return self.renderer.gateway_node_id

    @property
    def gateway_mxid(self) -> str:
        return self.state.get(self.gateway_node_id)["mxid"]

    def member_rows(self) -> list[dict[str, Any]]:
        """D12 membership, state-derived: the gateway agent first, then
        every live plain depth-0 agent (both /spawn kinds) in spawn order.
        Manual runs (kind ``manual-run``) and cron pseudo-rooms are not
        agents and never join; deeper nodes are not 0-agents."""
        zeros = [
            row
            for row in self.state.get_live()
            if row["depth"] == 0
            and row["node_id"] != self.gateway_node_id
            and not (row.get("extra") or {}).get("kind")
        ]
        return [self.state.get(self.gateway_node_id), *zeros]

    def directives_room_id(self) -> Optional[str]:
        try:
            return self.state.get_meta(ROOM_META_PREFIX + tree.DIRECTIVES_ROOM_KEY)
        except StateError:
            return None

    def plan_membership(self, current_members: Iterable[str]) -> tuple[RenderIntent, ...]:
        """Reconcile room membership (idempotent — same call for spawn,
        death and the D18 respawn re-ensure).

        ``current_members`` are the mxids currently JOINED (the caller's
        snapshot). Joins: invite (gateway voice) + join (member voice).
        Leaves: joined virtual users that are no longer members. Humans
        (owner, invited users) are never touched — only ``@merc_`` users.
        Returns () when the room is not provisioned yet.
        """
        room_id = self.directives_room_id()
        if not room_id:
            return ()
        current = set(current_members)
        desired = {row["mxid"]: row for row in self.member_rows()}
        intents: list[RenderIntent] = []
        for mxid, row in desired.items():
            if mxid in current:
                continue
            intents.append(InviteUser(tree.DIRECTIVES_ROOM_KEY, mxid, self.gateway_mxid))
            intents.append(JoinRoom(room_id, mxid))
        for mxid in sorted(current):
            if mxid.startswith(f"@{VIRTUAL_USER_PREFIX}") and mxid not in desired:
                intents.append(LeaveRoom(room_id, mxid))
        return tuple(intents)

    async def sync_membership(self, current_members: Iterable[str]) -> list[RenderIntent]:
        """Live membership reconcile (requires an executor-backed renderer)."""
        intents = self.plan_membership(current_members)
        if intents:
            await _execute(self.renderer, intents)
        return list(intents)

    # --- renderer-side outbound filter (agents never reply in-room) ------------

    def is_member_agent(self, mxid: str) -> bool:
        """True for a live 0-agent's virtual user (gateway excluded)."""
        return any(row["mxid"] == mxid for row in self.member_rows()[1:])

    def outbound_allowed(self, room: str, sender_mxid: str) -> bool:
        """Filter for the render path: outbound into the directives room is
        suppressed for member agents (they reply in their OWN rooms); the
        sidecar's own notices speak as the gateway agent / owner and pass.
        ``room`` may be the pseudo key ``directives`` or the concrete room id.
        """
        room_id = self.directives_room_id()
        if room != tree.DIRECTIVES_ROOM_KEY and room != room_id:
            return True
        if sender_mxid in (self.gateway_mxid, self.owner_mxid):
            return True
        return not self.is_member_agent(sender_mxid)

    # --- delivery ----------------------------------------------------------------

    def resolve_targets(self, content: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Mention-gated member subset for one message (pure)."""
        members = self.member_rows()
        scan = parse_mentions(content, [(r["mxid"], r["name"]) for r in members])
        if scan.everyone:
            return members
        return [row for row in members if row["mxid"] in scan.mxids]

    async def _deliver(self, row: Mapping[str, Any], text: str) -> str:
        node_id = row["node_id"]
        try:
            if node_id == self.gateway_node_id:
                if self.deliver_gateway is None:
                    return "failed (no gateway sink)"
                status = await self.deliver_gateway(f"{DIRECTIVE_LABEL} {text}")
            elif row["engine"] == "omp":
                if self.deliver_omp is None:
                    return "failed (no omp sink)"
                status = await self.deliver_omp(row, text)
            else:
                if self.deliver_hermes is None:
                    return "failed (no hermes sink)"
                status = await self.deliver_hermes(row, f"{DIRECTIVE_LABEL} {text}")
        except Exception as exc:  # noqa: BLE001 — one bad sink must not kill fan-out
            logger.exception("directives: delivery to %s failed", node_id)
            return f"failed ({exc.__class__.__name__})"
        return status if isinstance(status, str) and status else STATUS_APPLIED

    def _help_notice(self, members: Sequence[Mapping[str, Any]]) -> SendMessage:
        body, formatted = help_notice_body(members)
        return SendMessage(
            tree.DIRECTIVES_ROOM_KEY, self.gateway_mxid, body, formatted
        )

    def _receipt(self, entries: Sequence[tuple[str, str]]) -> tuple[RenderIntent, ...]:
        """Rolling edited delivery receipt — the renderer's dashboard
        machinery (tagged first send, in-place edits after; D15)."""
        body, formatted = receipt_body(entries)
        return self.renderer.plan_dashboard(
            tree.DIRECTIVES_ROOM_KEY, body, formatted=formatted
        )

    async def handle_message(
        self, sender_mxid: str, content: Mapping[str, Any]
    ) -> DirectiveOutcome:
        """One owner message from the directives room → deliveries + notices.

        Non-owner senders are ignored outright (D12: write = owner PL only —
        the room's power levels are the real gate; this is the sidecar's
        belt-and-braces). Owner-only delivery, help notice on no valid
        member mention, receipt edit on every fan-out.
        """
        if sender_mxid != self.owner_mxid:
            return DirectiveOutcome(ignored=True)

        body = content.get("body")
        text = body.strip() if isinstance(body, str) else ""
        members = self.member_rows()
        targets = self.resolve_targets(content)
        intents: list[RenderIntent] = []
        entries: list[tuple[str, str]] = []

        if not text:
            intents.append(self._help_notice(members))
        elif not targets:
            intents.append(self._help_notice(members))
        else:
            for row in targets:
                status = await self._deliver(row, text)
                entries.append((row["name"], status))
            intents.extend(self._receipt(entries))

        await _execute(self.renderer, tuple(intents))
        return DirectiveOutcome(targets=targets, statuses=entries, intents=tuple(intents))


async def _execute(renderer: Renderer, intents: Iterable[RenderIntent]) -> None:
    """Run intents through the renderer's executor (live paths only)."""
    if renderer.executor is None:
        raise RuntimeError("live path requires a Renderer with an IntentExecutor")
    await renderer.executor.execute(intents)
