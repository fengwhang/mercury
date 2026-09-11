"""Contract tests for the Observatory renderer (M3c, spec §3/§5/D8/D15).

Planning layer is pure: a seeded :class:`ObservatoryState` + a
:class:`Renderer` with NO executor — intents are asserted as data. The
executor is tested against a recording fake client (no aiohttp, no
homeserver). Laws under test: §3 provision order and senders, §5 message
composition (args truncation + [full] marker, elided results, quoted
thinking, rolling dashboard edit-in-place), D8 death intents per depth
(instant purge + cascade at depth 1 and 0, settled marker at depth >= 2),
and id recording back into state.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from observatory import tree
from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import (
    DASHBOARD_META_PREFIX,
    SETTLED_MARKER,
    AttachRoom,
    AttachSpace,
    CreateRoom,
    CreateSpace,
    DetachChild,
    EditMessage,
    IntentExecutor,
    PurgeRoom,
    Renderer,
    SendMessage,
    dashboard_message,
    markdown_to_html,
    snapshot_from_hierarchy,
    thinking_message,
    tool_call_message,
)
from observatory.state import ObservatoryState, StateError

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch"
SA = "sa-tests"          # depth-1 child of the orchestrator
SSA = "ssa-lint"         # depth-2 grandchild
CRON = "cron:nightly"

EMPTY_SNAPSHOT = {"spaces": {}, "rooms": {}}


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id: str, name: str, *, engine: str, parent: str | None, extra: dict | None = None):
        slug = assign_slug(name, state)
        return state.add_node(
            node_id,
            engine=engine,
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug),
            session_ref=f"session:{node_id}",
            parent_node_id=parent,
            extra=extra,
        )

    add(GW, "gateway agent", engine="hermes", parent=None, extra={"kind": "gateway"})
    add(CRON, "nightly", engine="hermes", parent=GW, extra={"kind": "cron-job"})
    add(ORCH, "auth-refactor", engine="hermes", parent=None)
    add(SA, "test-sweep", engine="omp", parent=ORCH)
    add(SSA, "lint-fix", engine="omp", parent=SA)
    return state


@pytest.fixture
def state(tmp_path: Path) -> ObservatoryState:
    return seed_state(tmp_path)


@pytest.fixture
def renderer(state: ObservatoryState) -> Renderer:
    return Renderer(
        state,
        gateway_node_id=GW,
        server_name=SERVER,
        owner_mxid=OWNER,
        executor=None,  # planning-only
    )


def realized(plan: tree.SpacePlan, snap: dict) -> tree.SpacePlan:
    """Copy ``plan`` with synthetic matrix ids set on every space/room,
    building a matching snapshot (everything exists and is attached)."""

    def realize(space: tree.SpacePlan) -> tree.SpacePlan:
        rooms = tuple(replace(r, matrix_id=f"!r-{r.key}:{SERVER}") for r in space.rooms)
        subs = tuple(realize(s) for s in space.subspaces)
        new = replace(space, matrix_id=f"!s-{space.key}:{SERVER}", rooms=rooms, subspaces=subs)
        snap["spaces"][new.matrix_id] = {
            "name": new.name,
            "children": [r.matrix_id for r in rooms] + [s.matrix_id for s in subs],
        }
        for r in rooms:
            snap["rooms"][r.matrix_id] = {"name": r.name}
        return new

    return realize(plan)


def _label(intent) -> tuple[str, str | None]:
    """Human key for an intent: attach ops carry parent/child keys, not a
    single ``key`` (``getattr(i, 'key')`` reads None for them)."""
    if isinstance(intent, AttachSpace):
        return (type(intent).__name__, intent.child_key)
    if isinstance(intent, AttachRoom):
        return (type(intent).__name__, intent.room_key)
    return (type(intent).__name__, getattr(intent, "key", None))

# --- §3 provisioning ---------------------------------------------------------------


class TestPlanProvision:
    def test_fresh_plan_orders_children_per_spec3(self, state, renderer):
        plan = renderer.build_plan(host="gatehost")
        intents = renderer.plan_provision(EMPTY_SNAPSHOT, plan)

        kinds = [_label(i) for i in intents]
        # §3 order inside the root space: gateway agent subspace first,
        # then directives, cron rooms, orchestrator subspaces — op order IS
        # the m.space.child order. The gateway agent is a normal depth-0
        # node (unified): its room nests in its own subspace keyed by node id.
        assert kinds == [
            ("CreateSpace", "root"),
            ("CreateSpace", GW),
            ("AttachSpace", GW),   # child_key; parent = root space
            ("CreateRoom", GW),            # gateway agent room
            ("AttachRoom", GW),
            ("CreateRoom", "directives"),
            ("AttachRoom", "directives"),
            ("CreateRoom", CRON),
            ("AttachRoom", CRON),
            ("CreateSpace", ORCH),
            ("AttachSpace", ORCH),         # child_key; parent = root space
            ("CreateRoom", ORCH),
            ("AttachRoom", ORCH),
            ("CreateSpace", SA),
            ("AttachSpace", SA),
            ("CreateRoom", SA),
            ("AttachRoom", SA),
            ("CreateSpace", SSA),
            ("AttachSpace", SSA),
            ("CreateRoom", SSA),
            ("AttachRoom", SSA),
        ]

    def test_senders_are_the_owning_virtual_users(self, state, renderer):
        plan = renderer.build_plan()
        intents = renderer.plan_provision(EMPTY_SNAPSHOT, plan)
        spaces = {i.key: i for i in intents if isinstance(i, CreateSpace)}
        rooms = {i.key: i for i in intents if isinstance(i, CreateRoom)}

        assert spaces[GW].sender == state.get(GW)["mxid"]
        # directives is a pseudo key -> the gateway agent speaks
        assert rooms["directives"].sender == state.get(GW)["mxid"]
        assert rooms[ORCH].sender == state.get(ORCH)["mxid"]
        assert spaces[SSA].sender == state.get(SSA)["mxid"]
        # attach voice = the PARENT space owner
        attach = next(i for i in intents if isinstance(i, AttachSpace) and i.child_key == SA)
        assert attach.sender == state.get(ORCH)["mxid"]

    def test_converged_plan_and_snapshot_yield_no_intents(self, state, renderer):
        snap: dict = {"spaces": {}, "rooms": {}}
        plan = realized(renderer.build_plan(host="gatehost"), snap)
        assert renderer.plan_provision(snap, plan) == ()

    def test_missing_room_only_recreates_that_room(self, state, renderer):
        state.set_space_id(GW, f"!s-gwsub:{SERVER}")
        state.set_room_id(GW, f"!r-gw:{SERVER}")
        state.set_meta("space:root", f"!s-root:{SERVER}")
        plan = renderer.build_plan()
        snap = {
            "spaces": {
                f"!s-root:{SERVER}": {"name": "x", "children": [f"!r-gw:{SERVER}"]},
                f"!s-gwsub:{SERVER}": {"name": "x", "children": []},
            },
            "rooms": {f"!r-gw:{SERVER}": {"name": "y"}},
        }
        intents = renderer.plan_provision(snap, plan)
        # §3 nests the gateway room inside its own subspace, so the
        # stale direct child (!r-gw under the root) is detached first; the
        # known gateway space/room themselves are never recreated.
        detach, rest = intents[0], intents[1:]
        assert isinstance(detach, DetachChild)
        assert (detach.space_id, detach.child_id) == (f"!s-root:{SERVER}", f"!r-gw:{SERVER}")
        assert [_label(i) for i in rest] == [
            ("AttachSpace", GW),
            ("AttachRoom", GW),
            ("CreateRoom", "directives"),
            ("AttachRoom", "directives"),
            ("CreateRoom", CRON),
            ("AttachRoom", CRON),
            ("CreateSpace", ORCH),
            ("AttachSpace", ORCH),
            ("CreateRoom", ORCH),
            ("AttachRoom", ORCH),
            ("CreateSpace", SA),
            ("AttachSpace", SA),
            ("CreateRoom", SA),
            ("AttachRoom", SA),
            ("CreateSpace", SSA),
            ("AttachSpace", SSA),
            ("CreateRoom", SSA),
            ("AttachRoom", SSA),
        ]


# --- §5 message composition -----------------------------------------------------------


class TestToolCallMessages:
    def test_long_args_truncated_with_full_marker(self):
        args = "x" * 500
        body, formatted = tool_call_message("bash", args)
        assert "bash" in body
        assert "[full]" in body
        assert "x" * 500 not in body
        assert len(body) < 500
        assert "<code>bash</code>" in formatted

    def test_short_args_no_marker(self):
        body, _ = tool_call_message("read", "src/main.py:1-40")
        assert "read" in body
        assert "[full]" not in body
        assert "src/main.py:1-40" in body

    def test_results_elided_errors_shown(self):
        body, formatted = tool_call_message("bash", "ls", error="exit 1: boom <script>")
        assert "exit 1: boom" in body
        assert "⚠️" in body
        assert "&lt;script&gt;" in formatted  # error text escaped in HTML

    def test_plain_body_has_no_code_backticks(self):
        body, _ = tool_call_message("grep", "pattern")
        assert "`" not in body


class TestThinkingMessages:
    def test_quoted_italic(self):
        body, formatted = thinking_message("maybe retry\nwith backoff")
        assert body == "maybe retry\nwith backoff"
        assert formatted.startswith("<blockquote><p><em>")
        assert formatted.endswith("</em></p></blockquote>")

    def test_html_in_thinking_is_escaped(self):
        _, formatted = thinking_message("<img src=x onerror=alert(1)>")
        assert "<img" not in formatted
        assert "&lt;img" in formatted


class TestDashboardMessages:
    def test_counts_line(self):
        body, formatted = dashboard_message(
            "auth-refactor", agents=2, delegations=1, blocked=1, extra=["- sa-tests running"]
        )
        assert "📊 auth-refactor" in body
        assert "live agents: 2 · delegations: 1 · blocked: 1" in body
        assert "<strong>" in formatted


class TestMarkdown:
    def test_raw_script_stripped_and_rest_escaped(self):
        html = markdown_to_html("hello <script>alert(1)</script> <b>world</b>")
        assert "<script>" not in html
        assert "<b>" not in html
        assert "&lt;b&gt;world&lt;/b&gt;" in html

    def test_code_span_protected_from_emphasis(self):
        html = markdown_to_html("`a*b*c` and **bold**")
        assert "<code>a*b*c</code>" in html
        assert "<strong>bold</strong>" in html

    def test_js_link_href_dropped(self):
        html = markdown_to_html("[click](javascript:alert(1))")
        assert 'href=""' in html


# --- §5 renderer events ----------------------------------------------------------------


class TestRendererEvents:
    def test_lifecycle_spawned_message_to_own_room(self, state, renderer):
        intents = renderer.plan_lifecycle(ORCH)
        assert len(intents) == 1
        msg = intents[0]
        assert isinstance(msg, SendMessage)
        assert msg.room_key == ORCH
        assert msg.sender == state.get(ORCH)["mxid"]
        assert "🚀" in msg.body and "auth-refactor" in msg.body
        assert "top-level" in msg.body

    def test_child_spawned_names_parent(self, state, renderer):
        msg = renderer.plan_lifecycle(SA)[0]
        assert "child of auth-refactor" in msg.body

    def test_tool_call_intent_shape(self, state, renderer):
        msg = renderer.plan_tool_call(SA, "write", "path=src/x.py content=...")[0]
        assert msg.room_key == SA
        assert msg.sender == state.get(SA)["mxid"]
        assert msg.formatted_body and "write" in msg.formatted_body

    def test_thinking_intent_shape(self, state, renderer):
        msg = renderer.plan_thinking(SSA, "pondering")[0]
        assert msg.room_key == SSA
        assert msg.formatted_body.startswith("<blockquote>")


class TestPlanDeath:
    def _with_ids(self, state: ObservatoryState) -> None:
        for node in (GW, ORCH, SA, SSA):
            state.set_space_id(node, f"!s-{node}:x")
            state.set_room_id(node, f"!r-{node}:x")

    def test_depth1_death_purges_and_summarizes_to_parent_only(self, state, renderer):
        self._with_ids(state)
        intents = renderer.plan_death(SA, status="completed", summary="3 tests green")
        sends = [i for i in intents if isinstance(i, SendMessage)]
        purges = [i.room_id for i in intents if isinstance(i, PurgeRoom)]

        # summary to the PARENT's room ONLY (its own room is being purged),
        # spoken by the PARENT's voice (the dying agent is no member there)
        assert len(sends) == 1
        assert sends[0].room_key == ORCH
        assert sends[0].sender == state.get(ORCH)["mxid"]
        assert "3 tests green" in sends[0].body
        assert SETTLED_MARKER not in sends[0].body

        # instant purge of SA room+space; D8 cascade takes the live SSA child
        assert set(purges) == {f"!r-{SA}:x", f"!s-{SA}:x", f"!r-{SSA}:x", f"!s-{SSA}:x"}
        detach = next(i for i in intents if isinstance(i, DetachChild))
        assert (detach.space_id, detach.child_id) == ("!s-orch:x", f"!s-{SA}:x")
        assert purges.index(f"!r-{SA}:x") < purges.index(f"!r-{SSA}:x")  # top-down

    def test_depth2_death_settles_and_survives(self, state, renderer):
        self._with_ids(state)
        intents = renderer.plan_death(SSA, status="completed", summary="lint fixed")
        assert not any(isinstance(i, PurgeRoom) for i in intents)
        settled, summary = intents
        assert isinstance(settled, SendMessage) and settled.room_key == SSA
        assert settled.body == SETTLED_MARKER
        assert isinstance(summary, SendMessage) and summary.room_key == SA
        assert "lint fixed" in summary.body

    def test_depth0_death_cascades_whole_subtree(self, state, renderer):
        self._with_ids(state)
        intents = renderer.plan_death(ORCH, status="exit", summary="done")
        sends = [i for i in intents if isinstance(i, SendMessage)]
        purges = {i.room_id for i in intents if isinstance(i, PurgeRoom)}

        # roots summarize to the GATEWAY room; gateway artifacts untouched
        assert len(sends) == 1 and sends[0].room_key == GW
        assert purges == {f"!r-{ORCH}:x", f"!s-{ORCH}:x", f"!r-{SA}:x", f"!s-{SA}:x",
                          f"!r-{SSA}:x", f"!s-{SSA}:x"}
        assert "!s-gw:x" not in purges and "!r-gw:x" not in purges

    def test_death_purge_set_by_depth(self, state, renderer):
        assert [r["node_id"] for r in renderer.death_purge_set(SA)] == [SA, SSA]
        assert renderer.death_purge_set(SSA) == []
        assert [r["node_id"] for r in renderer.death_purge_set(ORCH)] == [ORCH, SA, SSA]


# --- §5.4 dashboard ------------------------------------------------------------------------


class TestPlanDashboard:
    def test_first_dashboard_sends_tagged(self, state, renderer):
        body, formatted = dashboard_message("auth-refactor", agents=1)
        intents = renderer.plan_dashboard(ORCH, body, formatted=formatted)
        assert len(intents) == 1
        msg = intents[0]
        assert isinstance(msg, SendMessage)
        assert msg.tag == DASHBOARD_META_PREFIX + ORCH

    def test_subsequent_dashboard_edits_in_place(self, state, renderer):
        state.set_meta(DASHBOARD_META_PREFIX + ORCH, "$dash1")
        body, formatted = dashboard_message("auth-refactor", agents=2)
        intents = renderer.plan_dashboard(ORCH, body, formatted=formatted)
        assert len(intents) == 1
        edit = intents[0]
        assert isinstance(edit, EditMessage)
        assert edit.event_id == "$dash1"
        assert edit.body == body


# --- snapshot ---------------------------------------------------------------------------------


class TestSnapshot:
    def test_hierarchy_parse(self):
        hierarchy = {
            "rooms": [
                {
                    "room_id": "!s-gw:x",
                    "room_type": "m.space",
                    "name": "Mercury — gatehost",
                    "children_state": [
                        {"type": "m.space.child", "state_key": "!r-gw:x", "origin_server_ts": 3},
                        {"type": "m.space.child", "state_key": "!s-o:x", "origin_server_ts": 1},
                        {"type": "m.room.topic", "state_key": "", "origin_server_ts": 2},
                    ],
                },
                {"room_id": "!r-gw:x", "name": "gateway agent"},
            ]
        }
        snap = snapshot_from_hierarchy(hierarchy)
        # children keep children_state event order (not ts-sorted here)
        assert snap["spaces"]["!s-gw:x"]["children"] == ["!r-gw:x", "!s-o:x"]
        assert snap["rooms"]["!r-gw:x"]["name"] == "gateway agent"


# ============================================================================
# IntentExecutor — recording fake client, no aiohttp
# ============================================================================


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    next_id: int = 0
    rooms: dict = field(default_factory=dict)  # room_id -> {"name", "space"}

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def create_room(self, *, name, sender, preset, invite, space=False):
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space))
        rid = self._id("!room")
        self.rooms[rid] = {"name": name, "space": space}
        return rid

    async def room_hierarchy(self, space_id, *, sender=None):
        """Minimal /hierarchy shaped from created rooms + child attaches,
        so apply_plan's snapshot() sees what execute() built."""
        children: dict[str, list] = {}
        for c in self.calls:
            if c[0] == "child" and not c[5]:
                children.setdefault(c[1], []).append(c[2])
        out = []
        for rid, info in self.rooms.items():
            entry: dict = {"room_id": rid, "name": info["name"]}
            if info["space"]:
                entry["room_type"] = "m.space"
                entry["children_state"] = [
                    {"type": "m.space.child", "state_key": ch}
                    for ch in children.get(rid, [])
                ]
            out.append(entry)
        return {"rooms": out}

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender, formatted_body))
        return self._id("$ev")

    async def edit_message(self, room_id, event_id, body, *, sender, formatted_body=None):
        self.calls.append(("edit", room_id, event_id, body, sender))
        return self._id("$ev")

    async def delete_room(self, room_id, *, block=False, purge=True):
        self.calls.append(("delete", room_id, block, purge))

    async def leave_room(self, room_id, *, sender):
        self.calls.append(("leave", room_id, sender))

    async def leave_room_as_owner(self, room_id):
        self.calls.append(("leave-owner", room_id))


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


class TestIntentExecutor:
    @pytest.mark.asyncio
    async def test_create_space_and_room_record_ids_and_pin_owner(
        self, state: ObservatoryState, fake: FakeClient
    ):
        ex = IntentExecutor(
            fake, state, owner_mxid=OWNER, server_name=SERVER,
            gateway_mxid=state.get(GW)["mxid"],
        )
        await ex.execute(
            [
                CreateSpace(GW, "Mercury — gatehost", state.get(GW)["mxid"]),
                CreateRoom("directives", "Directives", GW, state.get(GW)["mxid"], kind="directives"),
                CreateRoom(CRON, "nightly", GW, state.get(CRON)["mxid"], kind="cron"),
            ]
        )
        # node ids land in columns; pseudo keys in meta
        assert state.get(GW)["space_id"] == "!room1"
        assert state.get_meta("room:directives") == "!room2"
        assert state.get(CRON)["room_id"] == "!room3"
        # every creation invites the owner and pins owner PL 100 (D7);
        # the gateway ghost is NEVER invited to child rooms/spaces
        # (it stays in its OWN room/space + directives/root only) —
        # gateway-parented cron rooms invite the owner only.
        creates = [c for c in fake.calls if c[0] == "create_room"]
        assert all(OWNER in c[4] for c in creates)
        gw_mxid = state.get(GW)["mxid"]
        cron_invite = next(c[4] for c in creates if c[1] == "nightly")
        assert gw_mxid not in cron_invite
        assert tuple(cron_invite) == (OWNER,)
        powers = [c for c in fake.calls if c[0] == "power"]
        assert all(p[2] == {OWNER: 100} for p in powers)
        assert len(powers) == len(creates)
        # space creation vs room creation presets differ
        assert creates[0][5] is True and creates[1][5] is False

    @pytest.mark.asyncio
    async def test_attach_resolves_ids_and_sends_via(self, state, fake):
        state.set_space_id(GW, "!s-gw:x")
        state.set_room_id(CRON, "!r-cron:x")
        ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)
        await ex.execute([AttachRoom(GW, CRON, state.get(GW)["mxid"])])
        call = fake.calls[0]
        assert call == ("child", "!s-gw:x", "!r-cron:x", state.get(GW)["mxid"], (SERVER,), False)

    @pytest.mark.asyncio
    async def test_unknown_room_key_fails_hard(self, state, fake):
        ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)
        with pytest.raises(StateError):
            await ex.execute([SendMessage("nope", "@merc_x:x", "hi")])

    @pytest.mark.asyncio
    async def test_tagged_send_records_event_id(self, state, fake):
        state.set_room_id(ORCH, "!r-orch:x")
        ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)
        await ex.execute(
            [SendMessage(ORCH, state.get(ORCH)["mxid"], "📊 v1", tag=DASHBOARD_META_PREFIX + ORCH)]
        )
        assert state.get_meta(DASHBOARD_META_PREFIX + ORCH) == "$ev1"

    @pytest.mark.asyncio
    async def test_edit_uses_recorded_event_id(self, state, fake):
        state.set_room_id(ORCH, "!r-orch:x")
        state.set_meta(DASHBOARD_META_PREFIX + ORCH, "$orig")
        ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)
        await ex.execute([EditMessage(ORCH, state.get(ORCH)["mxid"], "$orig", "📊 v2")])
        assert fake.calls[0][2] == "$orig"

    @pytest.mark.asyncio
    async def test_purge_admin_deletes_room(self, state, fake):
        ex = IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER)
        await ex.execute([PurgeRoom("!r-x:y")])
        assert fake.calls[0] == ("delete", "!r-x:y", False, True)

    @pytest.mark.asyncio
    async def test_full_death_execution_drops_purged_rows(self, state, fake):
        renderer = Renderer(
            state,
            gateway_node_id=GW,
            server_name=SERVER,
            owner_mxid=OWNER,
            executor=IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER),
        )
        for node in (GW, ORCH, SA, SSA):
            state.set_space_id(node, f"!s-{node}:x")
            state.set_room_id(node, f"!r-{node}:x")

        await renderer.render_death(SA, status="completed", summary="done")

        assert fake.calls[0][0] == "send" and fake.calls[0][1] == "!r-orch:x"
        deletes = [c[1] for c in fake.calls if c[0] == "delete"]
        assert set(deletes) == {f"!r-{SA}:x", f"!s-{SA}:x", f"!r-{SSA}:x", f"!s-{SSA}:x"}
        # D17: no tombstone rows survive the purge
        with pytest.raises(StateError):
            state.get(SA)
        with pytest.raises(StateError):
            state.get(SSA)
        assert state.get(ORCH)["node_id"] == ORCH  # parent keeps its artifacts

    @pytest.mark.asyncio
    async def test_apply_provision_executes_and_records(self, state, fake):
        renderer = Renderer(
            state,
            gateway_node_id=GW,
            server_name=SERVER,
            owner_mxid=OWNER,
            executor=IntentExecutor(fake, state, owner_mxid=OWNER, server_name=SERVER),
        )
        plan = renderer.build_plan(host="gatehost")
        applied = await renderer.apply_plan(plan)
        assert applied  # fresh snapshot -> full provisioning
        assert state.get(GW)["space_id"] and state.get(GW)["room_id"]
        assert state.get_meta("room:directives")
        # idempotent: a second apply converges (no new rooms created)
        creates_before = [c for c in fake.calls if c[0] == "create_room"]
        reapplied = await renderer.apply_plan(renderer.build_plan(host="gatehost"))
        creates_after = [c for c in fake.calls if c[0] == "create_room"]
        assert reapplied == []
        assert len(creates_after) == len(creates_before)
