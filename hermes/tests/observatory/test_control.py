"""Contract tests for the M4a control router (spec §5, D7/D10/D13).

Planning is data-only: a seeded :class:`ObservatoryState` + a
:class:`ControlRouter` with a static :class:`PowerLevelSnapshot` — no
homeserver, gateway WS, or omp child. Laws under test:

- D13 parsing: ``/verb`` and ``!verb`` equivalence, sidecar verb registry,
  everything else starting with ``/``/``!`` passes through to the engine's
  native command surface;
- §5 routing per agent class (gateway/hermes session injection, omp steer
  with idle->prompt degradation, omp grandchild subagent_steer);
- D7 power levels: write == steer authority; read-only senders get the
  explanatory notice and NOTHING routed (fail-closed when the snapshot is
  missing or broken);
- D13 scope gate: gateway-lifecycle verbs rejected outside the gateway
  room;
- §5 honesty: "⏳ queued steer" -> "✔ applied" on the feed echo / synthetic
  ack, "/stop" -> "🛑 stop requested — waiting for boundary" until a
  lifecycle frame reports the death;
- /cot on|off persisted in state.db meta (default on);
- invalid targets (unknown room, settled rooms, cron/manual-run kinds,
  own-echo virtual users, edits, non-text bodies) answered honestly;
- the canned :class:`~observatory.sim.ScriptedTimeline` drives the
  ledger transitions end-to-end exactly as the feed glue will.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from observatory import sim
from observatory.control import (
    APPLIED_STEER_NOTICE,
    COT_META_PREFIX,
    GATEWAY_ONLY_VERBS,
    NO_APPROVAL_PENDING_NOTICE,
    PL_UNAVAILABLE_NOTICE,
    QUEUED_STEER_NOTICE,
    READ_ONLY_NOTICE,
    SETTLED_STEER_NOTICE,
    STOP_CONFIRMED_NOTICE,
    STOP_REQUESTED_NOTICE,
    AbortSession,
    AgentClass,
    ControlRouter,
    EngineCommand,
    InjectText,
    OmpAbortMain,
    OmpPrompt,
    OmpSteer,
    OmpSubagentAbort,
    OmpSubagentSteer,
    PendingApproval,
    PowerLevelSnapshot,
    ResolveApproval,
    RoomPowerLevels,
    SidecarVerb,
    SteerText,
    parse_intent,
)
from observatory.discovery import NodeEvent as DiscoveryNodeEvent
from observatory.identity import VIRTUAL_USER_PREFIX, assign_slug, virtual_mxid
from observatory.omp_feed import MessageEvent, NodeEvent as OmpNodeEvent
from observatory.state import ObservatoryState, StateError

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
READER = "@reader:mercury.local"
VIRTUAL_SENDER = f"@{VIRTUAL_USER_PREFIX}gate:{SERVER}"

# Seeded tree (mirrors renderer tests): gateway -> cron + spawned hermes
# orchestrator -> one hermes child, one omp child (main) -> omp grandchild;
# plus a settled (dead) grandchild room and an observe-only manual run.
GW = "gw"
CRON = "cron:nightly"
ORCH = "orch"
SA = "sa-tests"      # hermes delegate_task child
OMPC = "ompc-lint"   # omp delegate_task child — MAIN session of its omp process
GC = "gc-lint"       # omp in-process grandchild under OMPC
DEADGC = "dead-gc"   # settled depth-2 room (survives until parent dies)
MANUAL = "manual:fix"  # manual TUI run — observe-only (D14)
GW_SIM = "sim-gw"


def room_of(node_id: str) -> str:
    return f"!room-{node_id}:{SERVER}"


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id: str, name: str, *, engine: str, parent: str | None, extra: dict | None = None):
        slug = assign_slug(name, state)
        state.add_node(
            node_id,
            engine=engine,
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug),
            session_ref=f"session:{node_id}",
            parent_node_id=parent,
            extra=extra,
        )
        state.set_room_id(node_id, room_of(node_id))

    add(GW, "gateway agent", engine="hermes", parent=None, extra={"kind": "gateway"})
    add(CRON, "nightly", engine="hermes", parent=GW, extra={"kind": "cron-job"})
    add(ORCH, "auth-refactor", engine="hermes", parent=None)
    add(SA, "test-sweep", engine="hermes", parent=ORCH)
    add(OMPC, "lint-sweep", engine="omp", parent=ORCH)
    add(GC, "lint-fix", engine="omp", parent=OMPC)
    add(DEADGC, "old-checks", engine="omp", parent=OMPC)
    state.mark_dead(DEADGC)
    add(MANUAL, "manual-fix", engine="omp", parent=None, extra={"kind": "manual-run"})
    return state


def make_pl(*, read_only_rooms: tuple[str, ...] = ()) -> PowerLevelSnapshot:
    """OWNER writes everywhere; READER writes except in read-only rooms
    (events_default raised above their level — §4)."""
    rooms = {}
    for node in (GW, CRON, ORCH, SA, OMPC, GC, DEADGC, MANUAL):
        room = room_of(node)
        if node in read_only_rooms:
            rooms[room] = RoomPowerLevels(
                users={OWNER: 100, READER: 0}, events_default=50
            )
        else:
            rooms[room] = RoomPowerLevels(users={OWNER: 100, READER: 0})
    return PowerLevelSnapshot(rooms)


def msg(
    node_id: str,
    body: str,
    *,
    sender: str = OWNER,
    event_id: str = "$e1",
    reply_to: str | None = None,
    msgtype: str = "m.text",
) -> dict:
    content: dict = {"body": body, "msgtype": msgtype}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return {
        "type": "m.room.message",
        "room_id": room_of(node_id),
        "sender": sender,
        "event_id": event_id,
        "content": content,
    }


@pytest.fixture
def state(tmp_path: Path) -> ObservatoryState:
    return seed_state(tmp_path)


@pytest.fixture
def router(state: ObservatoryState) -> ControlRouter:
    return ControlRouter(
        state, gateway_node_id=GW, pl_provider=make_pl()
    )


@pytest.fixture
def idle_router(state: ObservatoryState) -> ControlRouter:
    """omp mains report idle: steers degrade to prompt-new-turn."""
    return ControlRouter(
        state,
        gateway_node_id=GW,
        pl_provider=make_pl(),
        busy_probe=lambda node_id: False,
    )


# --- D13 parsing ----------------------------------------------------------------


class TestParseIntent:
    @pytest.mark.parametrize("body", ["hello there", "  padded  ", "👋", "", "/"])
    def test_plain_chat_is_steer(self, body):
        assert parse_intent(body) == SteerText((body or "").strip())

    @pytest.mark.parametrize("prefix", ["/", "!"])
    @pytest.mark.parametrize(
        ("body_tmpl", "verb", "args"),
        [
            ("{p}stop", "stop", ()),
            ("{p}stop  save   work ", "stop", ("save", "work")),
            ("{p}status", "status", ()),
            ("{p}cot on", "cot", ("on",)),
            ("{p}approve", "approve", ()),
            ("{p}deny always", "deny", ("always",)),
        ],
    )
    def test_sidecar_verbs_both_prefixes(self, prefix, body_tmpl, verb, args):
        intent = parse_intent(body_tmpl.format(p=prefix))
        # parse strips the body (raw) and splits args on runs of whitespace.
        assert intent == SidecarVerb(verb, args, prefix, body_tmpl.format(p=prefix).strip())

    def test_verb_match_is_case_insensitive(self):
        assert parse_intent("/Stop now").verb == "stop"

    @pytest.mark.parametrize("prefix", ["/", "!"])
    def test_engine_commands_pass_through(self, prefix):
        raw = f"{prefix}model sonnet"
        intent = parse_intent(raw)
        assert isinstance(intent, EngineCommand)
        assert intent.verb == "model"
        assert intent.text == raw  # forwarded verbatim to the engine registry

    def test_gateway_lifecycle_verbs_are_engine_commands(self):
        for verb in GATEWAY_ONLY_VERBS:
            intent = parse_intent(f"/{verb}")
            assert isinstance(intent, EngineCommand)
            assert intent.verb == verb

    @pytest.mark.parametrize("body", ["!", "! hello", "/  double"])
    def test_bare_prefix_or_spaced_word_is_chat(self, body):
        assert isinstance(parse_intent(body), SteerText)


# --- agent classes ---------------------------------------------------------------


class TestAgentClass:
    def test_seeded_tree_classification(self, router):
        assert router.agent_class_of(GW) is AgentClass.GATEWAY
        assert router.agent_class_of(ORCH) is AgentClass.HERMES_SESSION
        assert router.agent_class_of(SA) is AgentClass.HERMES_SESSION
        assert router.agent_class_of(OMPC) is AgentClass.OMP_MAIN
        assert router.agent_class_of(GC) is AgentClass.OMP_SUBAGENT

    def test_deeper_omp_chain_stays_subagent(self, state, router):
        state.add_node(
            "gc3", engine="omp", name="gc3", slug="gc3",
            mxid=virtual_mxid("gc3"), session_ref="s", parent_node_id=GC,
        )
        # The omp ROOT (parent not omp) is the main session; every omp
        # descendant rides the same transport as a subagent.
        assert router.agent_class_of("gc3") is AgentClass.OMP_SUBAGENT

    def test_root_omp_orchestrator_is_main(self, state, router):
        state.add_node(
            "spawnomp", engine="omp", name="worker", slug="worker",
            mxid=virtual_mxid("worker"), session_ref="s", parent_node_id=None,
        )
        assert router.agent_class_of("spawnomp") is AgentClass.OMP_MAIN


# --- §5 routing matrix: steer / stop × agent class --------------------------------


class TestSteerRouting:
    @pytest.mark.parametrize(
        ("node", "action_type"),
        [
            (GW, InjectText),
            (ORCH, InjectText),
            (SA, InjectText),
            (OMPC, OmpSteer),
            (GC, OmpSubagentSteer),
        ],
    )
    def test_busy_default_routes_per_class(self, router, node, action_type):
        outcome = router.route(msg(node, "focus on tests"))
        assert outcome.disposition == "steer"
        assert outcome.node_id == node
        assert len(outcome.actions) == 1
        action = outcome.actions[0]
        assert type(action) is action_type
        assert action.text == "focus on tests"
        if isinstance(action, InjectText):
            assert action.kind == "steer"
        assert [n.body for n in outcome.notices] == [QUEUED_STEER_NOTICE]
        pend = [p for p in router.pending_steers if p.node_id == node]
        assert len(pend) == 1 and pend[0].state == "queued"

    def test_omp_main_idle_degrades_to_prompt_new_turn(self, idle_router):
        outcome = idle_router.route(msg(OMPC, "new idea"))
        assert isinstance(outcome.actions[0], OmpPrompt)
        assert outcome.actions[0].text == "new idea"

    def test_steer_queued_then_ledger_capped(self, state):
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=make_pl(), steer_queue_cap=2
        )
        assert router.route(msg(GC, "one")).disposition == "steer"
        assert router.route(msg(GC, "two")).disposition == "steer"
        full = router.route(msg(GC, "three"))
        assert full.disposition == "notice:steer-queue-full"
        assert full.actions == ()
        assert len([p for p in router.pending_steers if p.state == "queued"]) == 2

    def test_ack_frees_queue_slot(self, state):
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=make_pl(), steer_queue_cap=1
        )
        router.route(msg(GC, "one"))
        full = router.route(msg(GC, "two"))
        assert full.disposition == "notice:steer-queue-full"
        router.observe_ack(GC, "[steer] one")
        ok = router.route(msg(GC, "two"))
        assert ok.disposition == "steer"


class TestStopRouting:
    @pytest.mark.parametrize("prefix", ["/", "!"])
    @pytest.mark.parametrize(
        ("node", "action_type"),
        [
            (GW, AbortSession),
            (ORCH, AbortSession),
            (SA, AbortSession),
            (OMPC, OmpAbortMain),
            (GC, OmpSubagentAbort),
        ],
    )
    def test_stop_routes_per_class_both_prefixes(
        self, router, prefix, node, action_type
    ):
        outcome = router.route(msg(node, f"{prefix}stop wrap up"))
        assert outcome.disposition == "stop"
        action = outcome.actions[0]
        assert type(action) is action_type
        assert action.reason == "wrap up"
        assert [n.body for n in outcome.notices] == [STOP_REQUESTED_NOTICE]
        assert router.pending_stops[0].state == "requested"

    def test_stop_without_reason_gets_default(self, router):
        assert router.route(msg(OMPC, "/stop")).actions[0].reason == "matrix /stop"

    def test_stop_on_idle_omp_main_is_honest(self, idle_router):
        outcome = idle_router.route(msg(OMPC, "/stop"))
        assert outcome.disposition == "notice:stop-idle"
        assert outcome.actions == ()
        assert router_has_no_pending_stop(idle_router)

    def test_stop_reply_carries_reply_to(self, router):
        outcome = router.route(msg(SA, "/stop", reply_to="$prompt"))
        assert outcome.notices[0].reply_to == "$prompt"


def router_has_no_pending_stop(router: ControlRouter) -> bool:
    return all(s.state == "confirmed" for s in router.pending_stops) or not router.pending_stops


# --- /status ---------------------------------------------------------------------


class TestStatusVerb:
    @pytest.mark.parametrize("body", ["/status", "!status"])
    def test_status_composes_room_state(self, router, body):
        outcome = router.route(msg(GC, body))
        assert outcome.disposition == "status"
        assert outcome.actions == ()
        text = outcome.notices[0].body
        assert "lint-fix" in text
        assert "omp/omp-subagent" in text
        assert "thinking display: on" in text

    def test_status_reflects_ledger(self, router):
        router.route(msg(GC, "watch the boundaries"))
        router.observe_ack(GC, "watch the boundaries")
        router.route(msg(GC, "/stop"))
        text = router.route(msg(GC, "/status")).notices[0].body
        assert "steers queued/applied: 0/1" in text
        assert "stop: requested" in text

    def test_status_on_omp_reports_busy(self, router):
        text = router.route(msg(OMPC, "/status")).notices[0].body
        assert "busy: yes" in text  # no probe -> assume busy (safe default)


# --- /cot (§5.2) -----------------------------------------------------------------


class TestCotVerb:
    def test_cot_defaults_on(self, router):
        assert router.cot_enabled(GC) is True

    @pytest.mark.parametrize("prefix", ["/", "!"])
    def test_cot_off_persists_in_state_meta(self, router, state, prefix):
        outcome = router.route(msg(GC, f"{prefix}cot off"))
        assert outcome.disposition == "cot"
        assert "off" in outcome.notices[0].body
        assert state.get_meta(COT_META_PREFIX + GC) == "off"
        assert router.cot_enabled(GC) is False
        # survives router restarts (state, not memory)
        fresh = ControlRouter(state, gateway_node_id=GW, pl_provider=make_pl())
        assert fresh.cot_enabled(GC) is False

    def test_cot_on_reenables(self, router, state):
        router.route(msg(GC, "/cot off"))
        router.route(msg(GC, "/cot on"))
        assert state.get_meta(COT_META_PREFIX + GC) == "on"
        assert router.cot_enabled(GC) is True

    def test_cot_bad_arg_is_usage(self, router, state):
        outcome = router.route(msg(GC, "/cot maybe"))
        assert outcome.disposition == "notice:cot-usage"
        with pytest.raises(StateError):  # nothing persisted
            state.get_meta(COT_META_PREFIX + GC)

    def test_cot_on_hermes_room_notes_hidden_cot(self, router):
        omp = router.route(msg(GC, "/cot on")).notices[0].body
        hermes = router.route(msg(SA, "/cot on")).notices[0].body
        assert "D5" not in omp
        assert "D5" in hermes


# --- /approve / /deny (D10) ------------------------------------------------------


class TestApprovals:
    def test_decision_without_prompt_is_rejected_honestly(self, router):
        outcome = router.route(msg(SA, "/approve"))
        assert outcome.disposition == "notice:no-approval-pending"
        assert outcome.notices[0].body == NO_APPROVAL_PENDING_NOTICE
        assert outcome.actions == ()

    def test_approve_resolves_registered_prompt(self, router):
        router.observe_approval_prompt(SA, "apr-1", "rm -rf build/")
        outcome = router.route(msg(SA, "/approve", reply_to="$apr-event"))
        assert outcome.disposition == "approval"
        action = outcome.actions[0]
        assert action == ResolveApproval(
            SA, "approve", "once", "$apr-event", "apr-1"
        )
        assert "✔ /approve sent (scope: once)" in outcome.notices[0].body
        assert router.pending_approval(SA) is None  # consumed exactly once
        again = router.route(msg(SA, "/approve"))
        assert again.disposition == "notice:no-approval-pending"

    @pytest.mark.parametrize(
        ("body", "decision", "scope"),
        [
            ("!deny session", "deny", "session"),
            ("/approve always", "approve", "always"),
            ("!deny", "deny", "once"),
        ],
    )
    def test_scopes_and_prefixes(self, router, body, decision, scope):
        router.observe_approval_prompt(GC, "apr-2")
        action = router.route(msg(GC, body)).actions[0]
        assert (action.decision, action.scope) == (decision, scope)

    def test_unknown_scope_is_usage(self, router):
        router.observe_approval_prompt(GC, "apr-3")
        outcome = router.route(msg(GC, "/approve forever"))
        assert outcome.disposition == "notice:approval-usage"
        assert router.pending_approval(GC) is not None  # prompt stays pending


# --- engine-native pass-through (D13) --------------------------------------------


class TestPassThrough:
    def test_gateway_room_gets_full_slash_registry(self, router):
        outcome = router.route(msg(GW, "/restart"))
        assert outcome.disposition == "command"
        assert outcome.actions == (InjectText(GW, "/restart", "command"),)

    def test_spawned_hermes_session_gets_session_scoped_commands(self, router):
        outcome = router.route(msg(ORCH, "/compact"))
        assert outcome.actions == (InjectText(ORCH, "/compact", "command"),)

    def test_omp_main_busy_commands_steer(self, router):
        outcome = router.route(msg(OMPC, "!model sonnet"))
        assert isinstance(outcome.actions[0], OmpSteer)
        assert outcome.actions[0].text == "!model sonnet"

    def test_omp_main_idle_commands_prompt(self, idle_router):
        assert isinstance(idle_router.route(msg(OMPC, "/model x")).actions[0], OmpPrompt)

    def test_subagents_have_no_command_surface(self, router):
        outcome = router.route(msg(GC, "/model sonnet"))
        assert outcome.disposition == "notice:subagent-no-commands"
        assert outcome.actions == ()


# --- D13 scope gate ----------------------------------------------------------------


class TestScopeGate:
    @pytest.mark.parametrize("verb", sorted(GATEWAY_ONLY_VERBS))
    @pytest.mark.parametrize("node", [ORCH, SA, OMPC, GC])
    def test_gateway_lifecycle_verbs_rejected_outside_gateway_room(
        self, router, verb, node
    ):
        outcome = router.route(msg(node, f"/{verb}"))
        assert outcome.disposition == "notice:scope-gate"
        assert outcome.actions == ()
        assert verb in outcome.notices[0].body
        assert "gateway" in outcome.notices[0].body

    def test_bang_prefix_gated_too(self, router):
        assert router.route(msg(SA, "!update")).disposition == "notice:scope-gate"

    def test_session_scoped_commands_not_gated(self, router):
        outcome = router.route(msg(ORCH, "/reset"))
        assert outcome.disposition == "command"

    def test_every_gateway_only_verb_passes_in_gateway_room(self, router):
        for verb in sorted(GATEWAY_ONLY_VERBS):
            outcome = router.route(msg(GW, f"/{verb}"))
            assert outcome.disposition == "command", verb


# --- D7 power levels --------------------------------------------------------------


class TestPowerLevels:
    @pytest.fixture
    def readonly_router(self, state):
        return ControlRouter(
            state,
            gateway_node_id=GW,
            pl_provider=make_pl(read_only_rooms=(SA,)),
        )

    def test_reader_in_writable_room_may_steer(self, router):
        outcome = router.route(msg(GC, "hi", sender=READER))
        assert outcome.disposition == "steer"

    def test_read_only_sender_gets_notice_and_no_routing(self, readonly_router):
        outcome = readonly_router.route(msg(SA, "steer me", sender=READER))
        assert outcome.disposition == "notice:read-only"
        assert outcome.notices[0].body == READ_ONLY_NOTICE
        # notices echo the incoming m.relates_to chain, not the event id —
        # this message was no reply, so there is nothing to thread under.
        assert outcome.notices[0].reply_to is None
        assert outcome.actions == ()
        assert [p for p in readonly_router.pending_steers if p.node_id == SA] == []

    @pytest.mark.parametrize(
        "body",
        ["/stop", "/status", "/approve always", "/reset", "!update"],
    )
    def test_every_verb_blocked_for_read_only_sender(self, readonly_router, body):
        assert readonly_router.route(msg(SA, body, sender=READER)).disposition == (
            "notice:read-only"
        )

    def test_owner_still_steers_the_read_only_room(self, readonly_router):
        assert readonly_router.route(msg(SA, "go")).disposition == "steer"

    def test_missing_snapshot_fails_closed(self, state):
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=lambda room: None
        )
        outcome = router.route(msg(GW, "hello"))
        assert outcome.disposition == "notice:power-levels-unavailable"
        assert outcome.notices[0].body == PL_UNAVAILABLE_NOTICE
        assert outcome.actions == ()

    def test_broken_provider_fails_closed(self, state):
        def boom(_room):
            raise RuntimeError("snapshot fetch died")

        router = ControlRouter(state, gateway_node_id=GW, pl_provider=boom)
        assert router.route(msg(GW, "hello")).disposition == (
            "notice:power-levels-unavailable"
        )


# --- §5 honesty ledger --------------------------------------------------------------


class TestHonestyLedger:
    def test_queued_then_applied_on_exact_echo(self, router):
        router.route(msg(GC, "cover the quiet gap"))
        (notice,) = router.observe_ack(GC, "cover the quiet gap")
        assert notice.body == APPLIED_STEER_NOTICE
        assert router.pending_steers[0].state == "applied"

    def test_decorated_echo_still_matches(self, router):
        router.route(msg(OMPC, "also cover X"))
        notices = router.observe_ack(OMPC, "[steer] also cover X")
        assert [n.body for n in notices] == [APPLIED_STEER_NOTICE]

    def test_unrelated_echo_leaves_it_queued(self, router):
        router.route(msg(GC, "cover the quiet gap"))
        assert router.observe_ack(GC, "unrelated assistant text") == ()
        assert router.pending_steers[0].state == "queued"

    def test_ack_is_scoped_to_the_node(self, router):
        router.route(msg(GC, "same words"))
        router.route(msg(OMPC, "same words"))
        assert router.observe_ack(OMPC, "same words") != ()
        states = {p.node_id: p.state for p in router.pending_steers}
        assert states == {GC: "queued", OMPC: "applied"}

    def test_newest_matching_steer_flips_first(self, router):
        router.route(msg(GC, "do the thing"))
        router.route(msg(GC, "do the thing"))
        assert len(router.observe_ack(GC, "do the thing")) == 1
        states = [p.state for p in router.pending_steers if p.node_id == GC]
        assert states == ["queued", "applied"]  # oldest still queued


class TestStopLifecycle:
    def test_requested_until_abort_observed(self, router):
        router.route(msg(OMPC, "/stop"))
        assert router.pending_stops[0].state == "requested"
        (notice,) = router.observe_death(OMPC, "aborted")
        assert notice.body == STOP_CONFIRMED_NOTICE.format(status="aborted")
        assert router.pending_stops[0].state == "confirmed"
        assert router.pending_stops[0].status == "aborted"

    def test_any_terminal_status_confirms_the_boundary(self, router):
        router.route(msg(SA, "/stop"))
        (notice,) = router.observe_death(SA, "completed")
        assert "completed" in notice.body

    def test_death_without_pending_stop_is_silent(self, router):
        assert router.observe_death(GC, "completed") == ()

    def test_double_death_confirms_once(self, router):
        router.route(msg(GC, "/stop"))
        first = router.observe_death(GC, "aborted")
        assert router.observe_death(GC, "aborted") == ()
        assert len(first) == 1

    def test_queued_steers_expire_on_death(self, router):
        router.route(msg(GC, "pending work"))
        router.observe_death(GC, "failed")
        assert [p for p in router.pending_steers if p.node_id == GC] == []


# --- invalid targets ----------------------------------------------------------------


class TestInvalidTargets:
    def test_unknown_room_dropped(self, router):
        outcome = router.route(msg("nowhere", "hello"))
        assert outcome.disposition == "drop:unknown-room"
        assert outcome.actions == () and outcome.notices == ()

    def test_directives_room_named_in_drop(self, state, router):
        from observatory.renderer import ROOM_META_PREFIX
        from observatory.tree import DIRECTIVES_ROOM_KEY

        state.set_meta(ROOM_META_PREFIX + DIRECTIVES_ROOM_KEY, "!dir:mercury.local")
        outcome = router.route(msg("nowhere", "hello") | {"room_id": "!dir:mercury.local"})
        assert outcome.disposition == "drop:directives-room"

    def test_settled_room_refuses_steering(self, router):
        outcome = router.route(msg(DEADGC, "one more thing"))
        assert outcome.disposition == "notice:settled"
        assert outcome.notices[0].body == SETTLED_STEER_NOTICE
        assert outcome.actions == ()

    def test_settled_room_refuses_stop_too(self, router):
        assert router.route(msg(DEADGC, "/stop")).disposition == "notice:settled"

    def test_cron_room_is_notification_only(self, router):
        outcome = router.route(msg(CRON, "run now"))
        assert outcome.disposition == "notice:cron-room"
        assert outcome.actions == ()

    def test_manual_run_is_observe_only(self, router):
        outcome = router.route(msg(MANUAL, "steer please"))
        assert outcome.disposition == "notice:manual-run"
        assert outcome.actions == ()

    def test_own_virtual_echo_ignored(self, router):
        outcome = router.route(msg(GC, "/stop", sender=VIRTUAL_SENDER))
        assert outcome.disposition == "drop:own-echo"

    @pytest.mark.parametrize(
        ("event", "disposition"),
        [
            ({"type": "m.room.member", "room_id": room_of(GW), "content": {}}, "drop:not-a-message"),
            (msg(GW, "x") | {"content": {"body": "edit", "m.new_content": {"body": "edit"}}}, "drop:edit"),
            (msg(GW, "waves", msgtype="m.emote"), "drop:msgtype"),
            (msg(GW, "   "), "drop:empty-body"),
            (msg(GW, "x") | {"content": {}}, "drop:empty-body"),
            (msg(GW, "x") | {"content": "not-a-dict"}, "drop:bad-content"),
            (msg(GW, "x") | {"content": {"msgtype": "m.text", "body": 42}}, "drop:empty-body"),
        ],
    )
    def test_non_control_shapes_dropped(self, router, event, disposition):
        assert router.route(event).disposition == disposition


# --- appservice transaction intake ----------------------------------------------------


class TestHandleTransaction:
    @pytest.mark.asyncio
    async def test_batch_routes_every_event_in_order(self, router):
        events = [msg(SA, "/stop", event_id="$1"), {"type": "m.typing"}, msg(GC, "hi", event_id="$2")]
        outcomes = await router.handle_transaction("txn-1", events)
        assert [o.disposition for o in outcomes] == [
            "stop", "drop:not-a-message", "steer",
        ]

    @pytest.mark.asyncio
    async def test_bad_entries_do_not_hide_good_ones(self, router):
        outcomes = await router.handle_transaction("txn-2", ["junk", msg(GC, "hi")])
        assert [o.disposition for o in outcomes] == [
            "drop:not-an-event", "steer",
        ]

    @pytest.mark.asyncio
    async def test_router_bug_isolated_per_event(self, router, monkeypatch):
        calls = {"n": 0}

        def flaky(event):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AssertionError("boom")
            return ControlRouter.route(router, event)

        monkeypatch.setattr(router, "route", flaky)
        outcomes = await router.handle_transaction("txn-3", [msg(GC, "a"), msg(GC, "b")])
        assert [o.disposition for o in outcomes] == [
            "drop:router-error", "steer",
        ]


# --- sim-timeline-driven end to end (the M4a gate shape) -------------------------------


class TestSimTimeline:
    """The canned 3-deep fan-out story drives the ledger exactly as the
    sidecar feed glue will: omp_feed messages ack steers, omp_feed and
    discovery deaths confirm stops. Node mapping is the glue's job (the
    timeline speaks delegation keys and registry ids, state speaks node
    ids) — modelled here by two small dicts."""

    @pytest.fixture
    def sim_state(self, tmp_path: Path) -> ObservatoryState:
        """The sim cast as state nodes: orchestrator (hermes root), child
        A (hermes), child B (omp main) + its in-process grandchild."""
        state = ObservatoryState(tmp_path / "sim.db")

        def add(node_id, name, *, engine, parent):
            slug = assign_slug(name, state)
            state.add_node(
                node_id, engine=engine, name=name, slug=slug,
                mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
                parent_node_id=parent,
            )
            state.set_room_id(node_id, room_of(node_id))

        add("sim-orch", sim.ORCH_NAME, engine="hermes", parent=None)
        add("sim-a", sim.CHILD_A_NAME, engine="hermes", parent="sim-orch")
        add("sim-b", sim.CHILD_B_NAME, engine="omp", parent="sim-orch")
        add("sim-gc", "gc-render-checks", engine="omp", parent="sim-b")
        return state

    @pytest.fixture
    def sim_router(self, sim_state) -> ControlRouter:
        # The sim story steers/stops as the owner; every sim room grants
        # owner write (a missing snapshot would fail closed and route
        # nothing — PowerLevelSnapshot documents the fail-closed law).
        return ControlRouter(
            sim_state,
            gateway_node_id=GW_SIM,
            pl_provider=lambda room_id: RoomPowerLevels(users={OWNER: 100}),
        )

    @staticmethod
    def discovery_map() -> dict[tuple[str, int], str]:
        return {
            (sim.ORCH_DELEGATION, 0): "sim-orch",
            (sim.CHILD_DELEGATION, 0): "sim-a",
            (sim.CHILD_DELEGATION, 1): "sim-b",
        }

    def feed_glue(self, router: ControlRouter, collected: list):
        """Timeline events -> router observations (the sidecar glue)."""
        dmap = self.discovery_map()

        def ingest(event) -> None:
            if isinstance(event, MessageEvent):
                if event.role == "user":
                    collected.extend(router.observe_ack("sim-gc", event.text))
            elif isinstance(event, OmpNodeEvent) and event.kind == "death":
                collected.extend(router.observe_death("sim-gc", event.status))
            elif isinstance(event, DiscoveryNodeEvent) and event.kind == "death":
                node = dmap.get((event.delegation_id, event.task_index))
                if node:
                    collected.extend(router.observe_death(node, event.status))

        return ingest

    def test_full_story_queues_steers_and_acks_via_feed_echo(self, sim_router):
        collected: list = []
        # The grandchild is steered with the exact text the timeline's
        # 8.0s user echo carries ("[steer] <text>" — decoration tolerated).
        steer_text = "also cover the quiet gap while the orchestrator idles"
        outcome = sim_router.route(msg("sim-gc", steer_text))
        assert isinstance(outcome.actions[0], OmpSubagentSteer)
        assert [n.body for n in outcome.notices] == [QUEUED_STEER_NOTICE]

        sim.drive(ScriptedTimeline_proxy(), self.feed_glue(sim_router, collected))

        bodies = [n.body for n in collected]
        assert bodies.count("✔ applied") == 1  # the 8.0s echo, exactly once
        states = [p.state for p in sim_router.pending_steers if p.node_id == "sim-gc"]
        assert states == ["applied"]

    def test_stop_confirmed_by_scripted_grandchild_abort(self, sim_router):
        # /stop against the grandchild, then a scripted aborted death beat.
        outcome = sim_router.route(msg("sim-gc", "/stop"))
        assert isinstance(outcome.actions[0], OmpSubagentAbort)

        beats = [
            sim.gc_death(seq=1, status="aborted"),
        ]
        collected: list = []
        sim.drive(beats_as_timed(beats), self.feed_glue(sim_router, collected))
        assert [n.body for n in collected] == [
            STOP_CONFIRMED_NOTICE.format(status="aborted")
        ]

    def test_discovery_death_confirms_hermes_child_stop(self, sim_router):
        outcome = sim_router.route(msg("sim-a", "/stop"))
        assert isinstance(outcome.actions[0], AbortSession)

        beat = sim.node_death(
            parent_session=sim.ORCH_SESSION,
            delegation_id=sim.CHILD_DELEGATION,
            task_index=0,
            seq=1,
            child_session_id=sim.CHILD_A_SESSION,
            child_subagent_id=f"{sim.CHILD_DELEGATION}/0",
            status="aborted",
        )
        collected: list = []
        sim.drive(beats_as_timed([beat]), self.feed_glue(sim_router, collected))
        assert collected[0].body == STOP_CONFIRMED_NOTICE.format(status="aborted")
        assert sim_router.pending_stops[0].state == "confirmed"


def beats_as_timed(events):
    return [sim.TimedEvent(t=float(i), event=e) for i, e in enumerate(events)]


def ScriptedTimeline_proxy():
    return sim.ScriptedTimeline()
