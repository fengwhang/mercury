"""Contract tests for the directives room (M5b, spec §6 / D12).

Planning layer is pure (Renderer without executor); live paths run against
the recording FakeClient + IntentExecutor pattern of test_renderer. Laws
under test: membership = gateway + live plain 0-agents (never manual runs,
cron pseudo-rooms, or deeper nodes); mention-gated delivery via
m.mentions + @mention text fallback; @room/@everyone expansion; help
notice on no valid mention; per-engine fan-out with the [directive] label;
agents never reply in-room (outbound filter); rolling edited receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from observatory import tree
from observatory.directives import (
    DIRECTIVE_LABEL,
    DirectivesManager,
    MentionScan,
    help_notice_body,
    parse_mentions,
    receipt_body,
)
from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import (
    DASHBOARD_META_PREFIX,
    EditMessage,
    IntentExecutor,
    InviteUser,
    JoinRoom,
    LeaveRoom,
    SendMessage,
    Renderer,
)
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH_H = "orch-hermes"
ORCH_O = "orch-omp"
MANUAL = "manual-1"
SUB = "sub"


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, engine="hermes", parent=None, extra=None):
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

    add(GW, "gateway agent", extra={"kind": "gateway"})
    add(ORCH_H, "auth-refactor")
    add(ORCH_O, "docs-sweep", engine="omp")
    add(MANUAL, "terminal omp", engine="omp", extra={"kind": "manual-run"})
    add(SUB, "deep child", parent=ORCH_H)
    state.set_meta("room:directives", "!r-directives:x")
    return state


@dataclass
class Sinks:
    """Recording delivery sinks: what each engine received + canned status."""

    hermes: list[tuple[str, str]] = field(default_factory=list)
    omp: list[tuple[str, str]] = field(default_factory=list)
    gateway: list[str] = field(default_factory=list)
    fail_omp: bool = False

    async def a_hermes(self, row, text):
        self.hermes.append((row["node_id"], text))
        return "queued"

    async def a_omp(self, row, text):
        self.omp.append((row["node_id"], text))
        if self.fail_omp:
            raise RuntimeError("rpc down")
        return "applied"

    async def a_gateway(self, text):
        self.gateway.append(text)
        return "applied"


def make_manager(state: ObservatoryState, sinks: Sinks | None = None) -> DirectivesManager:
    renderer = Renderer(
        state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER, executor=None
    )
    kwargs = {}
    if sinks is not None:
        kwargs = dict(
            deliver_hermes=sinks.a_hermes,
            deliver_omp=sinks.a_omp,
            deliver_gateway=sinks.a_gateway,
        )
    return DirectivesManager(renderer, **kwargs)


@pytest.fixture
def state(tmp_path: Path) -> ObservatoryState:
    return seed_state(tmp_path)


@pytest.fixture
def manager(state: ObservatoryState) -> DirectivesManager:
    return make_manager(state)


# --- membership ------------------------------------------------------------------


class TestMembership:
    def test_members_are_gateway_plus_plain_zero_agents(self, manager):
        rows = manager.member_rows()
        assert [r["node_id"] for r in rows] == [GW, ORCH_H, ORCH_O]

    def test_plan_membership_invites_and_joins_missing(self, manager):
        intents = manager.plan_membership(current_members={OWNER})
        assert intents == (
            InviteUser(tree.DIRECTIVES_ROOM_KEY, manager.state.get(GW)["mxid"], manager.gateway_mxid),
            JoinRoom("!r-directives:x", manager.state.get(GW)["mxid"]),
            InviteUser(tree.DIRECTIVES_ROOM_KEY, manager.state.get(ORCH_H)["mxid"], manager.gateway_mxid),
            JoinRoom("!r-directives:x", manager.state.get(ORCH_H)["mxid"]),
            InviteUser(tree.DIRECTIVES_ROOM_KEY, manager.state.get(ORCH_O)["mxid"], manager.gateway_mxid),
            JoinRoom("!r-directives:x", manager.state.get(ORCH_O)["mxid"]),
        )

    def test_plan_membership_leaves_dead_agents_never_humans(self, manager, state):
        members = {row["mxid"] for row in manager.member_rows()}
        intents = manager.plan_membership(
            current_members={OWNER, "@merc_ghost:x", *members}
        )
        # Every live member already joined; the unknown ghost leaves.
        # Humans (OWNER) are never touched.
        assert intents == (LeaveRoom("!r-directives:x", "@merc_ghost:x"),)

    def test_plan_membership_idempotent_when_converged(self, manager):
        current = {row["mxid"] for row in manager.member_rows()}
        assert manager.plan_membership(current) == ()

    def test_plan_membership_noop_without_room(self, state):
        state.set_meta("room:directives", "x")  # placeholder then delete via raw
        state._db.execute("DELETE FROM meta WHERE key = 'room:directives'")
        state._db.commit()
        manager = make_manager(state)
        assert manager.plan_membership(current_members=set()) == ()

    @pytest.mark.asyncio
    async def test_sync_membership_executes_via_executor(self, state, fake_client):
        renderer = Renderer(
            state,
            gateway_node_id=GW,
            server_name=SERVER,
            owner_mxid=OWNER,
            executor=IntentExecutor(fake_client, state, owner_mxid=OWNER, server_name=SERVER),
        )
        manager = DirectivesManager(renderer)
        executed = await manager.sync_membership(current_members={OWNER})
        assert len(executed) == 6
        calls = [c[0] for c in fake_client.calls]
        assert calls.count("invite") == 3 and calls.count("join") == 3


# --- mention parsing ----------------------------------------------------------------


class TestParseMentions:
    MEMBERS = [
        (f"@merc_auth-refactor:{SERVER}", "auth-refactor"),
        (f"@merc_docs-sweep:{SERVER}", "docs-sweep"),
        (f"@merc_gateway-agent:{SERVER}", "gateway agent"),
    ]
    def test_intentional_mentions_win(self):
        scan = parse_mentions(
            {"body": "please @docs-sweep run", "m.mentions": {"user_ids": [self.MEMBERS[0][0]]}},
            self.MEMBERS,
        )
        assert isinstance(scan, MentionScan)
        # m.mentions (intentional) UNION the @mention text fallback —
        # the body names docs-sweep in plain text too.
        assert scan.mxids == {self.MEMBERS[0][0], self.MEMBERS[1][0]}
        assert scan.everyone is False

    def test_everyone_tokens(self):
        for body in ("@room do it", "hey @EVERYONE", "(@everyone) sync"):
            assert parse_mentions({"body": body}, self.MEMBERS).everyone is True

    def test_text_fallback_localpart_slug_and_display_name(self):
        for body in (
            "ping @merc_auth-refactor now",   # full localpart
            "ping @auth-refactor now",        # bare slug
            "ping @docs-sweep now",           # display name pill
        ):
            scan = parse_mentions({"body": body}, self.MEMBERS)
            assert len(scan.mxids) == 1, body

    def test_unknown_mentions_pass_through_unfiltered(self):
        scan = parse_mentions(
            {"body": "hi", "m.mentions": {"user_ids": ["@stranger:elsewhere"]}},
            self.MEMBERS,
        )
        assert scan.mxids == {"@stranger:elsewhere"}  # membership filter is later

    def test_no_mentions(self):
        assert parse_mentions({"body": "just talking"}, self.MEMBERS).mxids == frozenset()


# --- delivery -------------------------------------------------------------------------


class TestDelivery:
    @pytest.fixture
    def live(self, tmp_path):
        """Manager over an executor-backed renderer + recording client."""
        state = seed_state(tmp_path)
        client = FakeClient()
        renderer = Renderer(
            state,
            gateway_node_id=GW,
            server_name=SERVER,
            owner_mxid=OWNER,
            executor=IntentExecutor(client, state, owner_mxid=OWNER, server_name=SERVER),
        )
        sinks = Sinks()
        return DirectivesManager(
            renderer,
            deliver_hermes=sinks.a_hermes,
            deliver_omp=sinks.a_omp,
            deliver_gateway=sinks.a_gateway,
        ), sinks, client, state

    @pytest.mark.asyncio
    async def test_mention_subset_fans_out_with_label(self, live):
        manager, sinks, client, state = live
        outcome = await manager.handle_message(
            OWNER,
            {
                "body": "@docs-sweep finish the docs",
                "m.mentions": {"user_ids": [state.get(ORCH_O)["mxid"]]},
            },
        )
        assert [n for n, _ in outcome.statuses] == ["docs-sweep"]
        # omp gets the raw text (RPC steer); hermes would get the label.
        # The text is the owner's full message body (mentions included).
        assert sinks.omp == [(ORCH_O, "@docs-sweep finish the docs")]
        assert sinks.hermes == [] and sinks.gateway == []
        # receipt: first call is a tagged SEND into the directives room
        sends = [c for c in client.calls if c[0] == "send"]
        assert sends[-1][1] == "!r-directives:x"
        assert "✔ applied: docs-sweep" in sends[-1][2]

    @pytest.mark.asyncio
    async def test_hermes_gets_directive_label(self, live):
        manager, sinks, client, state = live
        await manager.handle_message(
            OWNER,
            {
                "body": f"@{state.get(ORCH_H)['name']} go",
                "m.mentions": {"user_ids": [state.get(ORCH_H)["mxid"]]},
            },
        )
        assert sinks.hermes == [(ORCH_H, f"{DIRECTIVE_LABEL} @auth-refactor go")]

    @pytest.mark.asyncio
    async def test_everyone_reaches_all_members(self, live):
        manager, sinks, client, state = live
        outcome = await manager.handle_message(OWNER, {"body": "@everyone standup"})
        assert [n for n, _ in outcome.statuses] == ["gateway agent", "auth-refactor", "docs-sweep"]
        assert sinks.gateway == [f"{DIRECTIVE_LABEL} @everyone standup"]
        assert sinks.hermes == [(ORCH_H, f"{DIRECTIVE_LABEL} @everyone standup")]
        assert sinks.omp == [(ORCH_O, "@everyone standup")]

    @pytest.mark.asyncio
    async def test_no_valid_mention_posts_help_notice(self, live):
        manager, sinks, client, _ = live
        outcome = await manager.handle_message(OWNER, {"body": "hello agents"})
        assert outcome.targets == [] and sinks.hermes == sinks.omp == []
        sends = [c for c in client.calls if c[0] == "send"]
        body = sends[-1][2]
        assert "No member mentioned" in body
        for name in ("gateway agent", "auth-refactor", "docs-sweep"):
            assert f"@{name}" in body

    @pytest.mark.asyncio
    async def test_non_owner_is_ignored(self, live):
        manager, sinks, client, _ = live
        outcome = await manager.handle_message(
            "@intruder:x", {"body": "@everyone exfiltrate", "m.mentions": {"user_ids": ["@x:y"]}}
        )
        assert outcome.ignored and outcome.intents == ()
        assert client.calls == [] and sinks.gateway == []

    @pytest.mark.asyncio
    async def test_receipt_edits_in_place_after_first_send(self, live):
        manager, sinks, client, state = live
        mxid = state.get(ORCH_O)["mxid"]
        await manager.handle_message(OWNER, {"body": "@docs-sweep one", "m.mentions": {"user_ids": [mxid]}})
        first_tag_event = client.sent[-1]
        assert state.get_meta(DASHBOARD_META_PREFIX + tree.DIRECTIVES_ROOM_KEY)
        await manager.handle_message(OWNER, {"body": "@docs-sweep two", "m.mentions": {"user_ids": [mxid]}})
        edits = [c for c in client.calls if c[0] == "edit"]
        assert len(edits) == 1
        assert edits[0][2] == first_tag_event  # replaces the original receipt event

    @pytest.mark.asyncio
    async def test_sink_failure_recorded_not_fatal(self, live):
        manager, sinks, client, state = live
        sinks.fail_omp = True
        outcome = await manager.handle_message(
            OWNER,
            {"body": "@docs-sweep boom", "m.mentions": {"user_ids": [state.get(ORCH_O)["mxid"]]}},
        )
        assert outcome.statuses == [("docs-sweep", "failed (RuntimeError)")]
        sends = [c for c in client.calls if c[0] == "send"]
        assert "✖ failed (RuntimeError): docs-sweep" in sends[-1][2]


# --- outbound filter --------------------------------------------------------------------


class TestOutboundFilter:
    def test_member_agents_suppressed_in_directives_room(self, manager, state):
        for node in (ORCH_H, ORCH_O):
            assert manager.outbound_allowed("!r-directives:x", state.get(node)["mxid"]) is False
            assert manager.outbound_allowed("directives", state.get(node)["mxid"]) is False

    def test_gateway_owner_and_sidecar_notices_pass(self, manager, state):
        assert manager.outbound_allowed("!r-directives:x", state.get(GW)["mxid"])
        assert manager.outbound_allowed("directives", OWNER)
        assert manager.outbound_allowed("directives", "@merc_some-subagent:x") is True  # non-member

    def test_other_rooms_unaffected(self, manager, state):
        assert manager.outbound_allowed("!r-anywhere:x", state.get(ORCH_H)["mxid"])


# --- composers ----------------------------------------------------------------------------


    def test_receipt_body_spec_shape(self):
        body, formatted = receipt_body([("auth-refactor", "applied"), ("docs-sweep", "queued")], now=0)
        assert "📋 directive — 2 target(s)" in body
        assert "✔ applied: auth-refactor" in body
        assert "🕓 queued: docs-sweep" in body
        # formatted is the markdown-rendered html of the same body
        assert "auth-refactor" in formatted and "docs-sweep" in formatted

    def test_help_notice_lists_all_members(self):
        body, _ = help_notice_body([{"name": "gateway agent"}, {"name": "auth-refactor"}])
        assert "@gateway agent, @auth-refactor" in body


# ============================================================================
# FakeClient — recording client (test_renderer pattern, plus invite/join/
# leave + sent-event tracking)
# ============================================================================


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    sent: list = field(default_factory=list)  # event ids in send order
    _n: int = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def create_room(self, *, name, sender, preset=None, invite=(), space=False):
        self.calls.append(("create_room", name, sender, tuple(invite), space))
        return self._id("!room")

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users)))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender, formatted_body))
        ev = self._id("$ev")
        self.sent.append(ev)
        return ev

    async def edit_message(self, room_id, event_id, body, *, sender, formatted_body=None):
        self.calls.append(("edit", room_id, event_id, body, sender))
        ev = self._id("$ev")
        self.sent.append(ev)
        return ev

    async def invite(self, room_id, user_id, *, sender):
        self.calls.append(("invite", room_id, user_id, sender))

    async def join_room(self, room_id, *, sender):
        self.calls.append(("join", room_id, sender))
        return room_id

    async def leave_room(self, room_id, *, sender):
        self.calls.append(("leave", room_id, sender))

    async def delete_room(self, room_id, *, block=False, purge=True):
        self.calls.append(("delete", room_id))

    async def send_state_event(self, room_id, event_type, state_key, content, *, sender):
        self.calls.append(("state", room_id, event_type, state_key, dict(content), sender))

    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        return {"rooms": [{"room_id": room_id, "children_state": []}]}


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
