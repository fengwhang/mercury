"""M3c renderer: state + tree plan + feed events -> Matrix render intents
(spec §3 layout, §5 event flow, D8 deletion, D15 dashboard, D16 admin purge).

Two layers:

1. **Pure planning** — :class:`Renderer` plan methods return tuples of
   frozen intent dataclasses (:data:`RenderIntent`). No matrix, no I/O;
   the renderer is fully unit-testable without a homeserver.
2. **Execution** — :class:`IntentExecutor` binds intents to a
   :class:`~observatory.matrix_client.MatrixClient`, resolving symbolic
   keys (node ids, pseudo keys like ``directives``) to concrete
   room/space ids via :class:`~observatory.state.ObservatoryState`, and
   recording created ids + dashboard event ids back into it (re-runs and
   the D18 respawn pass re-attach instead of duplicating).

D8 enforcement lives at render time (:meth:`Renderer.plan_death`):

- depth-1 death -> final summary to the PARENT's room only, then the
  instant purge (admin DELETE) of the agent's room + space; descendants
  cascade-purge with it (parent lifetime was their only grace).
- depth->=2 death -> "settled — transcript only" marker in its own room +
  summary to the parent's room; artifacts survive until the parent dies.
- depth-0 death (/exit) -> cascade purge of the whole subtree; summary to
  the gateway room. The gateway agent itself never dies here (D18).

Event rendering per §5: one message per tool call (name + args truncated
~200 chars with a ``[full]`` marker; results elided except errors), omp
thinking as separate quoted messages, lifecycle spawned/died messages,
and one rolling DASHBOARD message per orchestrator room + a root
dashboard in the gateway room — edit-in-place, never a second message
(:meth:`Renderer.plan_dashboard`; the event id persists in state meta).
"""
from __future__ import annotations

import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from observatory import tree
from observatory.state import ObservatoryState, StateError, purge_on_death

log = logging.getLogger(__name__)

# --- message composition constants (§5) ------------------------------------------

#: Tool-call args truncation budget (spec §5: "~200 chars").
TOOL_ARGS_MAX_CHARS = 200
_ERROR_MAX_CHARS = 400
_SUMMARY_MAX_CHARS = 800

#: D8 settled marker for dead-but-not-purged rooms (steering disabled).
SETTLED_MARKER = "🪦 settled — transcript only"

#: State meta key prefix holding the rolling dashboard event id per room.
DASHBOARD_META_PREFIX = "dash:"
ROOM_META_PREFIX = "room:"
SPACE_META_PREFIX = "space:"


# ============================================================================
# Render intents — pure data
# ============================================================================

@dataclass(frozen=True)
class CreateSpace:
    """Provision a space; ``sender`` (virtual user) becomes its creator."""

    key: str
    name: str
    sender: str


@dataclass(frozen=True)
class CreateRoom:
    """Provision a room inside ``space_key``; ``sender`` becomes creator."""

    key: str
    name: str
    space_key: str
    sender: str
    kind: str = "chat"  # chat | directives | cron | manual-run


@dataclass(frozen=True)
class AttachSpace:
    """Nest a child space under its parent space (m.space.child add)."""

    parent_key: str
    child_key: str
    sender: str


@dataclass(frozen=True)
class AttachRoom:
    """Place a room in a space (m.space.child with a room id)."""

    space_key: str
    room_key: str
    sender: str


@dataclass(frozen=True)
class DetachChild:
    """Remove a stale child from a space (m.space.child ``{}``). Concrete
    ids — they come from state/snapshot, already resolved."""

    space_id: str
    child_id: str
    sender: str


@dataclass(frozen=True)
class SendMessage:
    """m.room.message into ``room_key``; ``tag`` names a state-meta slot
    the executor records the resulting event id under (dashboards)."""

    room_key: str
    sender: str
    body: str
    formatted_body: str | None = None
    tag: str | None = None


@dataclass(frozen=True)
class EditMessage:
    """m.replace of a prior event (dashboard rolling edit — D15)."""

    room_key: str
    sender: str
    event_id: str
    body: str
    formatted_body: str | None = None


@dataclass(frozen=True)
class InviteUser:
    room_key: str
    user_id: str
    sender: str


@dataclass(frozen=True)
class JoinRoom:
    room_id: str
    sender: str


@dataclass(frozen=True)
class LeaveRoom:
    room_id: str
    sender: str


@dataclass(frozen=True)
class SetUserPower:
    """D7: write access to an agent's room == steer authority."""

    room_key: str
    user_id: str
    level: int
    sender: str


@dataclass(frozen=True)
class PurgeRoom:
    """Admin DELETE (D8) — a space is purged through its room id too."""

    room_id: str


RenderIntent = (
    CreateSpace | CreateRoom | AttachSpace | AttachRoom | DetachChild
    | SendMessage | EditMessage | InviteUser | JoinRoom | LeaveRoom
    | SetUserPower | PurgeRoom
)


# ============================================================================
# Message composition — pure text builders (§5)
# ============================================================================

def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """(display text, truncated?) — collapse whitespace, hard-cut at limit."""
    flat = _one_line(text)
    if len(flat) <= limit:
        return flat, False
    return flat[:limit], True


def markdown_to_html(text: str) -> str:
    """Minimal Markdown -> org.matrix.custom.html for renderer-composed
    bodies (tool calls, summaries, dashboards).

    Patterns lifted from the secondary-chat matrix plugin
    (``plugins/platforms/matrix/adapter.py`` — pre-sanitize raw HTML,
    protect code, escape everything else, then a small transform set).
    Deliberately small: this converts OUR compositions, not arbitrary
    user markdown.
    """
    # Pre-sanitize raw HTML (plugin's _pre_sanitize patterns).
    result = re.sub(r"(?is)<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", "", text or "")
    result = re.sub(r"""(?is)\s+on[a-z0-9_-]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", result)

    protected: list[str] = []

    def _protect(fragment: str) -> str:
        protected.append(fragment)
        return f"\x00{len(protected) - 1}\x00"

    # Fenced + inline code (contents fully escaped, no nested markdown).
    result = re.sub(
        r"```(\w*)\n(.*?)```",
        lambda m: _protect(
            f'<pre><code class="language-{html.escape(m.group(1))}">'
            f"{html.escape(m.group(2))}</code></pre>"
            if m.group(1)
            else f"<pre><code>{html.escape(m.group(2))}</code></pre>"
        ),
        result,
        flags=re.DOTALL,
    )
    result = re.sub(
        r"`([^`\n]+)`",
        lambda m: _protect(f"<code>{html.escape(m.group(1))}</code>"),
        result,
    )
    # Links (javascript:/data: hrefs dropped).
    def _link(m: re.Match[str]) -> str:
        url = m.group(2).strip()
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        href = "" if scheme in {"javascript", "data", "vbscript"} else html.escape(url, quote=True)
        return _protect(f'<a href="{href}">{html.escape(m.group(1))}</a>')

    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, result)

    # Escape everything outside protected regions.
    parts = re.split(r"(\x00\d+\x00)", result)
    parts = [p if p.startswith("\x00") else html.escape(p) for p in parts]
    result = "".join(parts)

    # Line-oriented blocks.
    out: list[str] = []
    quote: list[str] = []
    for line in result.split("\n"):
        stripped = line.strip()
        if stripped.startswith("&gt; ") or stripped == "&gt;":
            quote.append(stripped[5:] if len(stripped) > 5 else "")
            continue
        if quote:
            out.append(f"<blockquote>{'<br>'.join(quote)}</blockquote>")
            quote = []
        header = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if header:
            level = len(header.group(1))
            out.append(f"<h{level}>{header.group(2)}</h{level}>")
        elif re.match(r"^[\s]*[-*+]\s+.+$", line):
            out.append(f"<li>{re.match(r'^[\s]*[-*+]\s+(.+)$', line).group(1)}</li>")
        else:
            out.append(line)
    if quote:
        out.append(f"<blockquote>{'<br>'.join(quote)}</blockquote>")

    result = "".join(
        f"{chunk}<br>" if chunk and i < len(out) - 1 else chunk for i, chunk in enumerate(out)
    )

    # Inline emphasis (outside code — code is already protected away).
    result = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", result)
    result = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", result)
    result = re.sub(r"~~(.+?)~~", r"<del>\1</del>", result)
    result = re.sub(r"</li><br>", "</li>", result)
    # Restore protected code/link fragments (already escaped HTML).
    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], result)


def tool_call_message(tool: str, args: str | None = None, *, error: str | None = None) -> tuple[str, str]:
    """One message per tool call (§5): name + args truncated ~200 chars with
    a ``[full]`` marker; results elided except errors. Full text lives in
    local transcripts only. Returns (plain body, formatted_body)."""
    src = f"🔧 `{tool}`"
    if args:
        shown, cut = truncate(args, TOOL_ARGS_MAX_CHARS)
        src += f" — {shown}{' [full]' if cut else ''}"
    if error:
        err, _ = truncate(error, _ERROR_MAX_CHARS)
        src += f"\n⚠️ error: {err}"
    body = re.sub(r"`([^`\n]+)`", r"\1", src)  # plain fallback: de-code-fence
    return body, markdown_to_html(src)


def thinking_message(text: str) -> tuple[str, str]:
    """Omp thinking: separate QUOTED/italic message between tool calls."""
    escaped = html.escape(text or "")
    return text or "", f"<blockquote><p><em>{escaped}</em></p></blockquote>"


def spawned_message(name: str, *, engine: str, parent: str | None = None) -> tuple[str, str]:
    origin = f", child of {parent}" if parent else " (top-level)"
    src = f"🚀 spawned — **{name}** ({engine}{origin})"
    return f"🚀 spawned — {name} ({engine}{origin})", markdown_to_html(src)


def death_summary_message(
    name: str, *, status: str | None = None, summary: str | None = None
) -> tuple[str, str]:
    """Final summary posted to the room that SURVIVES (parent / gateway)."""
    src = f"🏁 **{name}** settled"
    if status:
        src += f" ({status})"
    plain = f"🏁 {name} settled" + (f" ({status})" if status else "")
    if summary:
        text, _ = truncate(summary, _SUMMARY_MAX_CHARS)
        src += f"\n\n> {text}"
        plain += f"\n{text}"
    return plain, markdown_to_html(src)


def dashboard_message(
    title: str, *, agents: int, delegations: int = 0, blocked: int = 0, extra: Sequence[str] = ()
) -> tuple[str, str]:
    """Rolling dashboard body (§5.4): live tree, N agents, M delegations,
    K blocked. Edited in place — never a new message."""
    counts = f"live agents: {agents} · delegations: {delegations} · blocked: {blocked}"
    lines = [f"📊 {title}", counts, *extra]
    body = "\n".join(lines)
    return body, markdown_to_html(f"**📊 {title}**\n{counts}" + ("\n" + "\n".join(extra) if extra else ""))


# ============================================================================
# Snapshot — hierarchy -> tree.diff_plan input
# ============================================================================

def snapshot_from_hierarchy(hierarchy: dict[str, Any]) -> dict[str, Any]:
    """Parse a /hierarchy response into the snapshot shape
    :func:`observatory.tree.diff_plan` consumes:
    ``{"spaces": {sid: {"name", "children": [...]}}, "rooms": {rid: {"name"}}}``.
    Children keep ``children_state`` event order."""
    spaces: dict[str, dict[str, Any]] = {}
    rooms: dict[str, dict[str, Any]] = {}
    for room in hierarchy.get("rooms", []) or []:
        rid = room.get("room_id")
        if not rid:
            continue
        if room.get("room_type") == "m.space":
            children = [
                ev.get("state_key")
                for ev in room.get("children_state", []) or []
                if ev.get("type") == "m.space.child" and ev.get("state_key")
            ]
            spaces[rid] = {"name": room.get("name") or "", "children": children}
        else:
            rooms[rid] = {"name": room.get("name") or ""}
    return {"spaces": spaces, "rooms": rooms}


def children_by_ts(children_state: Iterable[dict[str, Any]]) -> list[str]:
    """Attach order of a space's children — ``children_state`` events
    sorted by ``origin_server_ts`` (the order the renderer sent them)."""
    events = [
        ev for ev in children_state or [] if ev.get("type") == "m.space.child" and ev.get("state_key")
    ]
    return [ev["state_key"] for ev in sorted(events, key=lambda ev: ev.get("origin_server_ts") or 0)]


# ============================================================================
# Renderer — pure planning + live composition
# ============================================================================

class Renderer:
    """Consumes (state, tree plan, feed events) and produces render
    intents. Construct with ``executor=None`` for pure planning (tests);
    with an :class:`IntentExecutor` the ``render_*``/``apply_*`` methods
    also drive Matrix."""

    def __init__(
        self,
        state: ObservatoryState,
        *,
        gateway_node_id: str,
        server_name: str,
        owner_mxid: str,
        executor: "IntentExecutor | None" = None,
    ):
        self.state = state
        self.gateway_node_id = gateway_node_id
        self.server_name = server_name
        self.owner_mxid = owner_mxid
        self.executor = executor

    # --- state reads ----------------------------------------------------------

    def _node(self, node_id: str) -> dict[str, Any]:
        return self.state.get(node_id)  # StateError on unknown — fail hard

    @property
    def _gateway(self) -> dict[str, Any]:
        return self._node(self.gateway_node_id)

    @property
    def gateway_mxid(self) -> str:
        return self._gateway["mxid"]

    def _sender_for_key(self, key: str) -> str:
        """Voice for a target key: the node's own virtual user when the key
        is a node (agents, cron jobs); the gateway agent for pseudo keys
        (directives, manual-runs)."""
        try:
            return self._node(key)["mxid"]
        except StateError:
            return self.gateway_mxid

    def _parent_room_key(self, row: dict[str, Any]) -> str:
        parent_id = row.get("parent_node_id")
        if parent_id:
            try:
                self._node(parent_id)
                return parent_id
            except StateError:
                pass
        return self.gateway_node_id  # roots summarize to the gateway room

    # --- §3 provisioning ----------------------------------------------------------------

    def build_plan(self, *, host: str | None = None) -> tree.SpacePlan:
        """Desired space plan from CURRENT state (fixed pseudo ids from
        meta so re-apply never duplicates the directives room etc.)."""
        fixed_room_ids: dict[str, str] = {}
        fixed_space_ids: dict[str, str] = {}
        for key, prefix, target in (
            (tree.DIRECTIVES_ROOM_KEY, ROOM_META_PREFIX, fixed_room_ids),
            (tree.GATEWAY_AGENT_SPACE_KEY, SPACE_META_PREFIX, fixed_space_ids),
            (tree.MANUAL_RUNS_SPACE_KEY, SPACE_META_PREFIX, fixed_space_ids),
        ):
            try:
                target[key] = self.state.get_meta(prefix + key)
            except StateError:
                pass
        forest = tree.build_forest(self.state.get_live())
        return tree.desired_plan(
            forest,
            gateway_node_id=self.gateway_node_id,
            host=host,
            fixed_room_ids=fixed_room_ids,
            fixed_space_ids=fixed_space_ids,
        )

    def plan_provision(
        self, snapshot: dict[str, Any], plan: tree.SpacePlan
    ) -> tuple[RenderIntent, ...]:
        """Tree diff (tree.diff_plan) -> executable intents, in §3 child
        order (gateway agent subspace, directives, cron rooms, orchestrator
        subspaces — op order IS the m.space.child send order)."""
        ops = tree.diff_plan(snapshot, plan)
        _, rooms_by_key = tree.plan_index(plan)
        room_space: dict[str, str] = {}

        def walk(space: tree.SpacePlan) -> None:
            for room in space.rooms:
                room_space[room.key] = space.key
            for sub in space.subspaces:
                walk(sub)

        walk(plan)
        space_key_by_id = {
            s.matrix_id: k for k, s in tree.plan_index(plan)[0].items() if s.matrix_id
        }

        intents: list[RenderIntent] = []
        for op in ops:
            if isinstance(op, tree.CreateSpace):
                intents.append(CreateSpace(op.key, op.name, self._sender_for_key(op.key)))
            elif isinstance(op, tree.CreateRoom):
                intents.append(
                    CreateRoom(
                        op.key,
                        op.name,
                        room_space[op.key],
                        self._sender_for_key(op.key),
                        kind=rooms_by_key[op.key].kind,
                    )
                )
            elif isinstance(op, tree.AttachChild):
                intents.append(
                    AttachSpace(op.parent_key, op.child_key, self._sender_for_key(op.parent_key))
                )
            elif isinstance(op, tree.AddRoom):
                intents.append(
                    AttachRoom(op.space_key, op.room_key, self._sender_for_key(op.space_key))
                )
            elif isinstance(op, tree.DetachChild):
                sender = self._sender_for_key(space_key_by_id.get(op.parent_id, ""))
                intents.append(DetachChild(op.parent_id, op.child_id, sender))
        return tuple(intents)

    # --- §5 events ----------------------------------------------------------------------

    def plan_lifecycle(self, node_id: str) -> tuple[RenderIntent, ...]:
        row = self._node(node_id)
        parent = None
        if row.get("parent_node_id"):
            try:
                parent = self._node(row["parent_node_id"])["name"]
            except StateError:
                parent = None
        body, formatted = spawned_message(row["name"], engine=row["engine"], parent=parent)
        return (SendMessage(node_id, row["mxid"], body, formatted),)

    def plan_tool_call(
        self,
        node_id: str,
        tool: str,
        args: str | None = None,
        *,
        error: str | None = None,
    ) -> tuple[RenderIntent, ...]:
        """§5.1 — results are elided (never rendered) except errors."""
        row = self._node(node_id)
        body, formatted = tool_call_message(tool, args, error=error)
        return (SendMessage(node_id, row["mxid"], body, formatted),)

    def plan_thinking(self, node_id: str, text: str) -> tuple[RenderIntent, ...]:
        """§5.2 — omp thinking as separate quoted messages."""
        row = self._node(node_id)
        body, formatted = thinking_message(text)
        return (SendMessage(node_id, row["mxid"], body, formatted),)

    def plan_agent_message(self, node_id: str, text: str) -> tuple[RenderIntent, ...]:
        """An agent's own reply → its room, in its own voice (e.g. the
        gateway agent answering a Matrix prompt). Plaintext — replies
        carry the model's raw text, never dashboard tags."""
        row = self._node(node_id)
        return (SendMessage(node_id, row["mxid"], text),)

    # --- D8 death ------------------------------------------------------------------------

    def death_purge_set(self, node_id: str) -> list[dict[str, Any]]:
        """Rows whose matrix artifacts die WITH this node: the whole
        subtree when the node itself purges (depth 1 instant rule, or the
        depth-0 /exit cascade); nothing for a depth>=2 settle."""
        row = self._node(node_id)
        if row["depth"] == 0 or purge_on_death(row["depth"]):
            return self.state.get_subtree(node_id)
        return []

    def plan_death(
        self, node_id: str, *, status: str | None = None, summary: str | None = None
    ) -> tuple[RenderIntent, ...]:
        row = self._node(node_id)
        purge = self.death_purge_set(node_id)
        body, formatted = death_summary_message(row["name"], status=status, summary=summary)

        # The dying agent's virtual user is NOT a member of the parent's
        # room — the summary is spoken by the PARENT's voice (its own room),
        # falling back to the gateway agent for roots.
        parent_key = self._parent_room_key(row)
        parent_sender = self._sender_for_key(parent_key)

        if not purge:
            # depth >= 2: marker in its OWN room (steering disabled), summary
            # to the parent; artifacts survive until the parent dies (D8).
            return (
                SendMessage(node_id, row["mxid"], SETTLED_MARKER),
                SendMessage(parent_key, parent_sender, body, formatted),
            )

        # Purging death (depth 1 instant, depth 0 /exit cascade): summary to
        # the PARENT room only — the dying room is being annihilated.
        intents: list[RenderIntent] = [
            SendMessage(parent_key, parent_sender, body, formatted)
        ]
        gw_space = self._gateway.get("space_id")
        purge_ids = {r["node_id"] for r in purge}
        for r in purge:
            # Detach ONLY from a parent space that SURVIVES this purge — a
            # purged parent takes its whole space (and the child state with
            # it) down via the same admin DELETE.
            parent_in_purge = r.get("parent_node_id") in purge_ids
            if not parent_in_purge and r.get("space_id"):
                parent_space = None
                if r.get("parent_node_id"):
                    try:
                        parent_space = self._node(r["parent_node_id"]).get("space_id")
                    except StateError:
                        parent_space = None
                if parent_space is None:
                    parent_space = gw_space  # roots attach to the gateway space
                if parent_space:
                    # detach voice = the PARENT space owner (a member of
                    # that space; the gateway agent for roots)
                    detach_sender = self._sender_for_key(r.get("parent_node_id") or "")
                    intents.append(DetachChild(parent_space, r["space_id"], detach_sender))
            for rid in (r.get("room_id"), r.get("space_id")):
                if rid:
                    intents.append(PurgeRoom(rid))
        return tuple(intents)

    # --- §5.4 dashboard -------------------------------------------------------------------

    def plan_dashboard(
        self, room_key: str, body: str, *, formatted: str | None = None
    ) -> tuple[RenderIntent, ...]:
        """Rolling dashboard: FIRST call sends (tagged — the executor
        records the event id in state meta), every later call edits that
        same event in place. Never a second message."""
        tag = DASHBOARD_META_PREFIX + room_key
        try:
            event_id = self.state.get_meta(tag)
        except StateError:
            return (SendMessage(room_key, self._sender_for_key(room_key), body, formatted, tag=tag),)
        return (EditMessage(room_key, self._sender_for_key(room_key), event_id, body, formatted),)

    # --- live paths: plan + execute ----------------------------------------------------------

    async def _execute(self, intents: Sequence[RenderIntent]) -> list[dict[str, Any]]:
        if self.executor is None:
            raise RuntimeError("Renderer constructed without an IntentExecutor")
        return await self.executor.execute(intents)

    async def snapshot(self, plan: tree.SpacePlan) -> dict[str, Any]:
        """Current matrix state from the root hierarchy, AUGMENTED with
        every plan id the state already knows (a created-but-unattached
        space must count as known, or re-apply would duplicate it).

        Backfill rule (VM defect: after a wipe+reprovision the server is
        new but state.db still holds pre-wipe ids — a CLI agent's subagent
        got a fresh room while the CLI agent itself stayed roomless):
        a state-known id the server does NOT confirm is a phantom, and
        phantoms NEVER count as known. Dropping them makes the next diff
        recreate the parent WITH its subtree in one consistent plan, so
        children of unmirrored parents stay unmirrored — no orphan rooms.
        Confirmation is the owner-token admin read
        (``admin_room_alive`` — membership-independent, no sync): only a
        clean False drops the id. Any other outcome (exists, no-access,
        probe error, or a client double without the probe) keeps today's
        attach-only behavior — never a duplicate, never a new loud
        failure.
        """
        snap: dict[str, Any] = {"spaces": {}, "rooms": {}}
        gw_space = self._gateway.get("space_id")
        if gw_space:
            hierarchy = await self.executor.client.room_hierarchy(gw_space, sender=self.gateway_mxid)
            snap = snapshot_from_hierarchy(hierarchy)
        spaces_by_key, rooms_by_key = tree.plan_index(plan)
        probe = getattr(self.executor.client, "admin_room_alive", None)
        for key, space in spaces_by_key.items():
            if space.matrix_id and space.matrix_id not in snap["spaces"]:
                if await self._server_confirms(space.matrix_id, probe):
                    snap["spaces"][space.matrix_id] = {"name": space.name, "children": []}
                else:
                    log.info("snapshot: phantom space id %s for %r — recreating with its subtree",
                             space.matrix_id, key)
        for key, room in rooms_by_key.items():
            if room.matrix_id and room.matrix_id not in snap["rooms"]:
                if await self._server_confirms(room.matrix_id, probe):
                    snap["rooms"][room.matrix_id] = {"name": room.name}
                else:
                    log.info("snapshot: phantom room id %s for %r — recreating",
                             room.matrix_id, key)
        return snap

    @staticmethod
    async def _server_confirms(matrix_id: str, probe) -> bool:
        """True when a state-known-but-unlisted id may count as known."""
        if probe is None:
            return True  # legacy double without the admin probe — today's behavior
        try:
            return bool(await probe(matrix_id))
        except Exception:  # noqa: BLE001 — probe failure keeps attach-only (never a duplicate)
            log.debug("snapshot: room probe failed for %s — keeping state id", matrix_id)
            return True

    async def apply_plan(self, plan: tree.SpacePlan) -> list[RenderIntent]:
        """Converge matrix onto the plan (idempotent). Returns the applied
        intents (empty when already converged)."""
        intents = self.plan_provision(await self.snapshot(plan), plan)
        if intents:
            await self._execute(intents)
        return list(intents)

    async def render_lifecycle(self, node_id: str) -> list[RenderIntent]:
        intents = self.plan_lifecycle(node_id)
        await self._execute(intents)
        return list(intents)

    async def render_tool_call(
        self, node_id: str, tool: str, args: str | None = None, *, error: str | None = None
    ) -> list[RenderIntent]:
        intents = self.plan_tool_call(node_id, tool, args, error=error)
        await self._execute(intents)
        return list(intents)

    async def render_thinking(self, node_id: str, text: str) -> list[RenderIntent]:
        intents = self.plan_thinking(node_id, text)
        await self._execute(intents)
        return list(intents)

    async def render_agent_message(self, node_id: str, text: str) -> list[RenderIntent]:
        intents = self.plan_agent_message(node_id, text)
        await self._execute(intents)
        return list(intents)

    async def render_dashboard(
        self, room_key: str, body: str, *, formatted: str | None = None
    ) -> list[RenderIntent]:
        intents = self.plan_dashboard(room_key, body, formatted=formatted)
        await self._execute(intents)
        return list(intents)

    async def _execute_death(self, intents: Sequence[RenderIntent]) -> list[dict[str, Any]]:
        """Tolerant death batch (D8 convergence, spawn.py purge parity).

        Every intent is attempted even when an earlier one fails: a PurgeRoom
        404-already-gone records gone:true (the desired end state — a retried
        room purge must never abort its space purge); any other PurgeRoom
        error is collected as fatal but still lets siblings attempt (one wedged
        room must not orphan the rest); Send/Detach errors are soft (cosmetic
        once rooms purge) and never block. Non-404 purge failures raise at the
        end so rows survive for retry; 404/soft never block row removal.
        """
        from observatory.matrix_client import MatrixError  # lazy: no aiohttp at import

        records: list[dict[str, Any]] = []
        fatal: list[str] = []
        for op in intents:
            try:
                records.extend(await self._execute([op]))
            except MatrixError as exc:
                if isinstance(op, PurgeRoom) and exc.status == 404:
                    records.append({"op": "purge", "room_id": op.room_id, "gone": True})
                    continue
                if isinstance(op, PurgeRoom):
                    fatal.append(f"{type(op).__name__} {getattr(op, 'room_id', '')}: {exc}")
                    records.append({"op": "purge-failed", "room_id": getattr(op, "room_id", ""), "error": str(exc)})
                    continue
                log.warning("death batch soft failure %r: %s", op, exc)
                records.append({"op": "soft-failed", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 — classified, not swallowed
                if isinstance(op, PurgeRoom):
                    fatal.append(f"{type(op).__name__} {getattr(op, 'room_id', '')}: {exc}")
                    records.append({"op": "purge-failed", "room_id": getattr(op, "room_id", ""), "error": str(exc)})
                    continue
                log.warning("death batch soft failure %r: %s", op, exc)
                records.append({"op": "soft-failed", "error": str(exc)})
        if fatal:
            raise RuntimeError("death purge not converged: " + "; ".join(fatal))
        return records

    async def render_death(
        self, node_id: str, *, status: str | None = None, summary: str | None = None
    ) -> list[RenderIntent]:
        """D8 render-time enforcement. Tombstones first (rows must survive
        planning), execute the tolerant death batch, THEN drop every purged row
        (D17: no tombstone may survive to leak state into a successor).
        Depth>=2 settles (empty purge set) still tombstones the dying row so
        the settled gate disables steering."""
        purge = self.death_purge_set(node_id)
        for r in purge:
            if r["status"] == "live":
                self.state.mark_dead(r["node_id"])
        if not purge:
            try:
                if self.state.get(node_id)["status"] == "live":
                    self.state.mark_dead(node_id)
            except StateError:
                pass
        intents = self.plan_death(node_id, status=status, summary=summary)
        await self._execute_death(intents)
        # Rows drop deepest-first: children reference parents (FK), and the
        # purge set is BFS top-down, so walk it in reverse.
        for r in reversed(purge):
            self.state.mark_deleted_and_purge(r["node_id"])
        return list(intents)


# ============================================================================
# IntentExecutor — binds intents to a MatrixClient
# ============================================================================

class IntentExecutor:
    """Executes render intents against :class:`MatrixClient`, resolving
    symbolic keys through state and recording ids back into it:

    - agent/cron nodes -> their ``space_id``/``room_id`` columns;
    - pseudo keys (``directives``, ``manual-runs``) -> ``space:``/``room:``
      meta;
    - tagged sends (dashboards) -> ``dash:`` meta (the event id).

    Room/space creation ALWAYS invites the owner and pins owner PL 100
    (D7 — owner is admin everywhere; invited users per config later).
    """

    def __init__(
        self,
        client: Any,  # MatrixClient (duck-typed for tests)
        state: ObservatoryState,
        *,
        owner_mxid: str,
        server_name: str,
        space_preset: str = "private_chat",
        room_preset: str = "trusted_private_chat",
        gateway_mxid: str = "",
    ):
        self.client = client
        self.state = state
        self.owner_mxid = owner_mxid
        self.server_name = server_name
        self.space_preset = space_preset
        self.room_preset = room_preset
        self.gateway_mxid = gateway_mxid

    @staticmethod
    def _is_not_member_error(exc: BaseException) -> bool:
        """True for membership-shaped 403/404 failures (never a real error)."""
        if getattr(exc, "status", None) in (403, 404):
            return True
        text = str(exc).lower()
        return (
            "403" in text
            or "404" in text
            or "not in room" in text
            or "not a member" in text
            or "not joined" in text
        )

    def _mxid_for_key(self, key: str) -> str | None:
        """Voice mxid for a space/room key: node mxid, else gateway ghost."""
        node = self._node_or_none(key)
        if node is not None:
            mxid = node.get("mxid")
            if mxid:
                return str(mxid)
        return self.gateway_mxid or None

    def _parent_voice_for_create(
        self, op: CreateSpace | CreateRoom, *, space: bool
    ) -> str | None:
        """Parent node's voice for a creation op (invited + joined)."""
        if not space:
            assert isinstance(op, CreateRoom)
            # Node-backed rooms (agent children): the PARENT NODE's voice —
            # not the room's own space (which is the child's own ghost and
            # therefore self). Pseudo rooms (directives/cron) have no node
            # and fall back to their containing space's voice.
            node = self._node_or_none(op.key)
            if node is not None:
                parent_id = node.get("parent_node_id")
                if parent_id:
                    voice = self._mxid_for_key(str(parent_id))
                    if voice and voice != op.sender:
                        return voice
            voice = self._mxid_for_key(op.space_key)
            if voice and voice != op.sender:
                return voice
            return None
        node = self._node_or_none(op.key)
        if node is not None:
            parent_id = node.get("parent_node_id")
            if parent_id:
                voice = self._mxid_for_key(str(parent_id))
                if voice and voice != op.sender:
                    return voice
        # Pseudo spaces (gw-agent) and roots: the gateway ghost is the parent.
        if self.gateway_mxid and self.gateway_mxid != op.sender:
            return self.gateway_mxid
        return None

    def _create_invites(
        self, op: CreateSpace | CreateRoom, *, space: bool
    ) -> tuple[str, ...]:
        """Creation invite list: owner + gateway ghost + parent voice."""
        invites: list[str] = []
        for mxid in (
            self.owner_mxid,
            self.gateway_mxid or "",
            self._parent_voice_for_create(op, space=space) or "",
        ):
            if not mxid or mxid == op.sender or mxid in invites:
                continue
            invites.append(mxid)
        return tuple(invites)

    async def ensure_ghost_in_room(self, room_id: str, mxid: str) -> bool:
        """Best-effort ghost join (gateway / parent voice). Never raises."""
        if not mxid:
            return False
        try:
            await self.client.join_room(room_id, sender=mxid)
            return True
        except AttributeError:
            log.debug("ghost auto-join unavailable for %s (%s)", room_id, mxid)
            return False
        except Exception as exc:  # noqa: BLE001 — best-effort membership
            log.warning("ghost auto-join failed for %s (%s): %s", room_id, mxid, exc)
            return False

    def _attach_fallback_senders(self, op: AttachSpace | AttachRoom) -> list[str]:
        """Fallback senders for an attach: child ghost, then owner."""
        fallbacks: list[str] = []
        child_key = op.child_key if isinstance(op, AttachSpace) else op.room_key
        node = self._node_or_none(child_key)
        if node is not None:
            mxid = node.get("mxid")
            if mxid and str(mxid) != op.sender and str(mxid) not in fallbacks:
                fallbacks.append(str(mxid))
        if (
            self.owner_mxid
            and self.owner_mxid != op.sender
            and self.owner_mxid not in fallbacks
        ):
            fallbacks.append(self.owner_mxid)
        return fallbacks

    # --- id resolution -----------------------------------------------------------------

    def _node_or_none(self, key: str) -> dict[str, Any] | None:
        try:
            return self.state.get(key)
        except StateError:
            return None

    def room_id(self, key: str) -> str:
        node = self._node_or_none(key)
        if node is not None:
            rid = node.get("room_id")
        else:
            try:
                rid = self.state.get_meta(ROOM_META_PREFIX + key)
            except StateError:
                rid = None
        if not rid:
            raise StateError(f"no room id resolved for key {key!r}")
        return rid

    def space_id(self, key: str) -> str:
        node = self._node_or_none(key)
        if node is not None:
            sid = node.get("space_id")
        else:
            try:
                sid = self.state.get_meta(SPACE_META_PREFIX + key)
            except StateError:
                sid = None
        if not sid:
            raise StateError(f"no space id resolved for key {key!r}")
        return sid

    def _record_space(self, key: str, space_id: str) -> None:
        if self._node_or_none(key) is not None:
            self.state.set_space_id(key, space_id)
        else:
            self.state.set_meta(SPACE_META_PREFIX + key, space_id)

    def _record_room(self, key: str, room_id: str) -> None:
        if self._node_or_none(key) is not None:
            self.state.set_room_id(key, room_id)
        else:
            self.state.set_meta(ROOM_META_PREFIX + key, room_id)

    # --- execution -----------------------------------------------------------------------

    async def _create(self, op: CreateSpace | CreateRoom, *, space: bool) -> str:
        room_id = await self.client.create_room(
            name=op.name,
            sender=op.sender,
            preset=self.space_preset if space else self.room_preset,
            invite=self._create_invites(op, space=space),
            space=space,
        )
        try:
            await self.client.set_power_levels(room_id, {self.owner_mxid: 100}, sender=op.sender)
        except Exception as exc:  # noqa: BLE001 — power failure never orphans the id
            if self._is_not_member_error(exc):
                log.warning("create power skipped for %s (not-member): %s", room_id, exc)
            else:
                raise
        # Owner auto-join (VM defect: the owner saw invites / join prompts
        # on their own spaces+rooms): the sidecar accepts the creation
        # invite on the owner's behalf with the owner's own credential —
        # the same POST /join Element/FluffyChat send on a Join tap.
        await self.ensure_owner_in_room(room_id)
        # Gateway-ghost membership at creation: the gateway ghost performs
        # members reads, decrypt-failure notices and attach sends as sender,
        # so it must be joined (not merely invited) to every child room and
        # space. The parent node's voice joins too — never raises.
        if self.gateway_mxid and self.gateway_mxid != op.sender:
            await self.ensure_ghost_in_room(room_id, self.gateway_mxid)
        parent_voice = self._parent_voice_for_create(op, space=space)
        if parent_voice and parent_voice != self.gateway_mxid:
            await self.ensure_ghost_in_room(room_id, parent_voice)
        if space:
            self._record_space(op.key, room_id)
        else:
            self._record_room(op.key, room_id)
        return room_id

    async def ensure_owner_in_room(self, room_id: str) -> bool:
        """Best-effort owner join of one room/space id. True when the owner
        is now in the room (joined or already there); False when the join
        was skipped or failed. Never raises — the creation invite is the
        fallback, so a failed join only leaves a normal pending invite."""
        try:
            await self.client.join_room_as_owner(room_id)
            return True
        except AttributeError:
            # Client double without the owner-join surface (older fakes):
            # the invite still stands, nothing to heal.
            log.debug("owner auto-join unavailable for %s (no join_room_as_owner)", room_id)
            return False
        except Exception as exc:  # noqa: BLE001 — best-effort membership
            log.warning("owner auto-join failed for %s: %s", room_id, exc)
            return False

    async def ensure_owner_in_plan(self, plan: "tree.SpacePlan") -> int:
        """Converge-time heal: join the owner to every planned space/room
        id (covers pre-existing rooms whose invite was never accepted —
        the VM's current state). Best-effort per room; returns the join
        count. Never raises."""
        spaces, rooms = tree.plan_index(plan)
        ids = [s.matrix_id for s in spaces.values() if s.matrix_id]
        ids += [r.matrix_id for r in rooms.values() if r.matrix_id]
        joined = 0
        for rid in ids:
            if await self.ensure_owner_in_room(rid):
                joined += 1
        return joined

    async def execute(self, intents: Iterable[RenderIntent]) -> list[dict[str, Any]]:
        """Run intents in order; returns an execution log (one record per
        intent — evidence for the gate log and the sidecar's own audit)."""
        records: list[dict[str, Any]] = []
        for op in intents:
            if isinstance(op, CreateSpace):
                try:
                    rid = await self._create(op, space=True)
                except Exception as exc:  # noqa: BLE001 — tolerant converge batch
                    if self._is_not_member_error(exc):
                        log.warning("converge: skipping non-member create_space %r: %s", op.key, exc)
                        records.append({"op": "skipped", "key": op.key, "reason": "not-member", "error": str(exc)})
                        continue
                    raise
                records.append({"op": "create_space", "key": op.key, "space_id": rid})
            elif isinstance(op, CreateRoom):
                try:
                    rid = await self._create(op, space=False)
                except Exception as exc:  # noqa: BLE001 — tolerant converge batch
                    if self._is_not_member_error(exc):
                        log.warning("converge: skipping non-member create_room %r: %s", op.key, exc)
                        records.append({"op": "skipped", "key": op.key, "reason": "not-member", "error": str(exc)})
                        continue
                    raise
                records.append({"op": "create_room", "key": op.key, "room_id": rid})
            elif isinstance(op, AttachSpace):
                parent, child = self.space_id(op.parent_key), self.space_id(op.child_key)
                senders = [op.sender, *self._attach_fallback_senders(op)]
                attached = False
                last_exc: Exception | None = None
                for sender in senders:
                    try:
                        await self.client.set_space_child(
                            parent, child, sender=sender, via=(self.server_name,)
                        )
                        attached = True
                        records.append({"op": "attach_space", "parent": parent, "child": child})
                        break
                    except Exception as exc:  # noqa: BLE001 — member fallback, then skip
                        if not self._is_not_member_error(exc):
                            raise
                        last_exc = exc
                        continue
                if not attached:
                    log.warning("converge: skipping non-member attach_space %s -> %s: %s", parent, child, last_exc)
                    records.append({"op": "skipped", "parent": parent, "child": child, "reason": "not-member", "error": str(last_exc)})
                    continue
            elif isinstance(op, AttachRoom):
                space, room = self.space_id(op.space_key), self.room_id(op.room_key)
                senders = [op.sender, *self._attach_fallback_senders(op)]
                attached = False
                last_exc = None
                for sender in senders:
                    try:
                        await self.client.set_space_child(
                            space, room, sender=sender, via=(self.server_name,)
                        )
                        attached = True
                        records.append({"op": "attach_room", "space": space, "room": room})
                        break
                    except Exception as exc:  # noqa: BLE001 — member fallback, then skip
                        if not self._is_not_member_error(exc):
                            raise
                        last_exc = exc
                        continue
                if not attached:
                    log.warning("converge: skipping non-member attach_room %s -> %s: %s", space, room, last_exc)
                    records.append({"op": "skipped", "space": space, "room": room, "reason": "not-member", "error": str(last_exc)})
                    continue
            elif isinstance(op, DetachChild):
                await self.client.set_space_child(
                    op.space_id, op.child_id, sender=op.sender, remove=True
                )
                records.append({"op": "detach", "space": op.space_id, "child": op.child_id})
            elif isinstance(op, SendMessage):
                rid = self.room_id(op.room_key)
                try:
                    event_id = await self.client.send_message(
                        rid, op.body, sender=op.sender, formatted_body=op.formatted_body
                    )
                except Exception as exc:
                    # BUG5 (403 discovery): the gateway/virtual user is not a
                    # member of the target room (stale membership after a
                    # wipe/reprovision). Skip the room instead of failing
                    # the whole discovery event — log + record, never raise.
                    if getattr(exc, "status", None) in (403, 404) or "403" in str(exc) or "not in room" in str(exc).lower() or "not a member" in str(exc).lower():
                        log.warning("render_lifecycle: skipping non-member room %s sender %s: %s", rid, op.sender, exc)
                        records.append({"op": "skipped", "room": rid, "reason": "not-member", "error": str(exc)})
                        continue
                    raise
                if op.tag:
                    self.state.set_meta(op.tag, event_id)
                records.append({"op": "send", "room": rid, "event_id": event_id, "tag": op.tag})
            elif isinstance(op, EditMessage):
                rid = self.room_id(op.room_key)
                try:
                    event_id = await self.client.edit_message(
                        rid, op.event_id, op.body, sender=op.sender, formatted_body=op.formatted_body
                    )
                except Exception as exc:
                    if getattr(exc, "status", None) in (403, 404) or "403" in str(exc) or "not in room" in str(exc).lower() or "not a member" in str(exc).lower():
                        log.warning("render edit: skipping non-member room %s: %s", rid, exc)
                        records.append({"op": "skipped", "room": rid, "reason": "not-member", "error": str(exc)})
                        continue
                    raise
                records.append({"op": "edit", "room": rid, "replaces": op.event_id,
                                "event_id": event_id})
            elif isinstance(op, InviteUser):
                rid = self.room_id(op.room_key)
                await self.client.invite(rid, op.user_id, sender=op.sender)
                records.append({"op": "invite", "room": rid, "user": op.user_id})
            elif isinstance(op, JoinRoom):
                rid = await self.client.join_room(op.room_id, sender=op.sender)
                records.append({"op": "join", "room": rid, "user": op.sender})
            elif isinstance(op, LeaveRoom):
                await self.client.leave_room(op.room_id, sender=op.sender)
                records.append({"op": "leave", "room": op.room_id, "user": op.sender})
            elif isinstance(op, SetUserPower):
                rid = self.room_id(op.room_key)
                await self.client.set_power_levels(rid, {op.user_id: op.level}, sender=op.sender)
                records.append({"op": "power", "room": rid, "user": op.user_id,
                                "level": op.level})
            elif isinstance(op, PurgeRoom):
                await self.client.delete_room(op.room_id)
                records.append({"op": "purge", "room_id": op.room_id})
            else:  # pragma: no cover — the union is closed
                raise TypeError(f"unknown render intent {op!r}")
        return records
