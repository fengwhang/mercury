"""M4b (matrix observatory §5/D10) approval-bridge tests.

Harness (network-free, homeserver-free):

- a recording :class:`FakePoster` stands in for the MatrixClient surface
  (send_message → deterministic event ids);
- a seeded :class:`ObservatoryState` carries the §3 tree with room ids;
- the approval SOCKET side runs for real: :class:`ObservatoryApprovalServer`
  binds a unix socket and the tests POST to it over HTTP exactly the way
  omp's ``headless-approval.ts`` child does (:class:`ApprovalSocketClient`)
  — this is the fake-approval-socket-server harness;
- the SIM timeline (``observatory.sim.ScriptedTimeline``) builds the live
  agent tree, proving approvals land in the right room at ANY depth.

Laws under test: one prompt per pending request; reply-target resolution
(in_reply_to + thread root, bare-command sole-pending fallback);
/approve scope words once|session|always; /deny free-text reason;
keyed routing back through resolve_gateway_approval / the approval
socket; D7 sender-authority (power-level check) and wrong-room denial;
reactions and agent voices ignored (D10 — explicit decision only);
timeout defaulting to the gateway approval timeout with the EXISTING
default path deciding the outcome.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory import sim  # noqa: E402
from observatory.approvals import (  # noqa: E402
    ApprovalBridge,
    ApprovalCommand,
    ApprovalSocketClient,
    MatrixAuthority,
    ObservatoryApprovalServer,
    SocketAnswer,
    approval_prompt_message,
    can_write,
    default_approval_timeout,
    gateway_notify,
    is_agent_voice,
    parse_approval_command,
    required_write_level,
    rpc_frame_callback,
    split_approval_prompt,
    user_power_level,
)
from observatory.identity import assign_slug, virtual_mxid  # noqa: E402
from observatory.state import ObservatoryState  # noqa: E402

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
WRITER = "@alice:mercury.local"
READER = "@bob:mercury.local"
GW = "gw"
ORCH = "orch"
SA = "sa-tests"          # depth-1 omp child
SSA = "ssa-lint"         # depth-2 grandchild


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakePoster:
    """Recording stand-in for the MatrixClient send surface."""

    def __init__(self):
        self.sent: list[dict] = []
        self._n = 0

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self._n += 1
        event_id = f"$ev{self._n}"
        self.sent.append(
            {
                "room_id": room_id,
                "body": body,
                "sender": sender,
                "formatted_body": formatted_body,
                "event_id": event_id,
            }
        )
        return event_id

    def bodies(self, room_id):
        return [m["body"] for m in self.sent if m["room_id"] == room_id]


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Resolver:
    """Recording gateway resolver with a configurable verdict."""

    def __init__(self, verdict=1, raise_on_call=False):
        self.calls: list[tuple] = []
        self.verdict = verdict
        self.raise_on_call = raise_on_call

    def __call__(self, session_key, choice, request_id, reason):
        self.calls.append((session_key, choice, request_id, reason))
        if self.raise_on_call:
            raise RuntimeError("resolver down")
        return self.verdict


class SetAuthority:
    """Deterministic authority: exactly the listed users may write."""

    def __init__(self, *users):
        self.users = set(users)
        self.calls: list[tuple] = []

    async def __call__(self, room_id, sender):
        self.calls.append((room_id, sender))
        return sender in self.users


def seed_state(tmp_path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, engine, parent):
        slug = assign_slug(name, state)
        state.add_node(
            node_id,
            engine=engine,
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug),
            session_ref=f"session:{node_id}",
            parent_node_id=parent,
        )
        state.set_room_id(node_id, f"!room-{node_id}:{SERVER}")

    add(GW, "gateway agent", engine="hermes", parent=None)
    add(ORCH, "auth-refactor", engine="hermes", parent=None)
    add(SA, "test-sweep", engine="omp", parent=ORCH)
    add(SSA, "lint-fix", engine="omp", parent=SA)
    return state


SA_ROOM = f"!room-{SA}:{SERVER}"
SSA_ROOM = f"!room-{SSA}:{SERVER}"
SA_MXID = virtual_mxid("test-sweep")
SSA_MXID = virtual_mxid("lint-fix")


def make_bridge(tmp_path, *, authority=None, resolver=None, timeout=600.0, clock=None):
    poster = FakePoster()
    bridge = ApprovalBridge(
        state=seed_state(tmp_path),
        poster=poster,
        authority=authority or SetAuthority(OWNER, WRITER),
        resolve_gateway=resolver or Resolver(),
        timeout=timeout,
        clock=clock or FakeClock(),
    )
    return bridge, poster


def room_event(room_id, sender, body, *, reply_to=None, thread_root=None):
    content = {"msgtype": "m.text", "body": body}
    rel = {}
    if reply_to:
        rel["m.in_reply_to"] = {"event_id": reply_to}
    if thread_root:
        rel["rel_type"] = "m.thread"
        rel["event_id"] = thread_root
    if rel:
        content["m.relates_to"] = rel
    return {"type": "m.room.message", "room_id": room_id, "sender": sender, "content": content}


def reaction_event(room_id, sender, target_event, key="👍"):
    return {
        "type": "m.reaction",
        "room_id": room_id,
        "sender": sender,
        "content": {
            "m.relates_to": {"rel_type": "m.annotation", "event_id": target_event, "key": key}
        },
    }


async def submit_gateway(bridge, node_id=SA, request_id="req-1", **kw):
    return await bridge.submit(
        node_id, request_id, backend="gateway", session_key="sess-A",
        command="rm -rf /tmp/probe", context="exec-tier command", **kw
    )


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


class TestParseApprovalCommand:
    def test_approve_defaults_to_once(self):
        assert parse_approval_command("/approve") == ApprovalCommand(
            "approve", "once", "", "/approve"
        )

    def test_bang_prefix_and_scopes(self):
        for text, scope in (("/approve once", "once"), ("!approve session", "session"),
                            ("/APPROVE ALWAYS", "always")):
            cmd = parse_approval_command(text)
            assert cmd.verb == "approve" and cmd.scope == scope

    def test_unknown_scope_word_falls_back_to_once(self):
        assert parse_approval_command("/approve now").scope == "once"

    def test_deny_reason_free_text(self):
        cmd = parse_approval_command("!deny needs a narrower glob")
        assert cmd.verb == "deny" and cmd.reason == "needs a narrower glob"

    def test_non_approval_text_rejected(self):
        for text in ("/stop", "!spawnomp x", "approve", "", "/approved", "/approveall"):
            assert parse_approval_command(text) is None


# ---------------------------------------------------------------------------
# Power levels (pure D7 math)
# ---------------------------------------------------------------------------


class TestPowerLevels:
    PL = {"users": {OWNER: 100, READER: 0}, "users_default": 0, "events_default": 0}

    def test_owner_and_plain_users_can_write_by_default(self):
        assert can_write(self.PL, OWNER)
        assert can_write(self.PL, "@stranger:mercury.local")

    def test_readonly_user_blocked_when_events_default_raised(self):
        pl = dict(self.PL, events_default=50)
        assert can_write(pl, OWNER)
        assert not can_write(pl, READER)
        assert not can_write(pl, "@stranger:mercury.local")

    def test_message_event_override(self):
        pl = dict(self.PL, events={"m.room.message": 25}, users={OWNER: 100, WRITER: 25})
        assert required_write_level(pl) == 25
        assert can_write(pl, WRITER)
        assert not can_write(pl, READER)

    def test_users_default_floor(self):
        pl = {"users_default": 10, "events_default": 10}
        assert user_power_level(pl, "@anon:x") == 10
        assert can_write(pl, "@anon:x")

    def test_matrix_authority_uses_live_power_levels(self):
        class Client:
            def __init__(self):
                self.rooms = {SA_ROOM: dict(TestPowerLevels.PL, events_default=50)}

            async def get_power_levels(self, room_id, *, sender):
                return self.rooms[room_id]

        authority = MatrixAuthority(Client(), reader_mxid=virtual_mxid("gateway-agent"))
        assert asyncio.run(authority(SA_ROOM, OWNER))
        assert not asyncio.run(authority(SA_ROOM, READER))


# ---------------------------------------------------------------------------
# Message composition (pure)
# ---------------------------------------------------------------------------


class TestMessages:
    def test_prompt_carries_command_context_hint(self):
        body, html = approval_prompt_message("rm -rf /tmp/x", "exec-tier gate", request_id="abc123")
        assert "rm -rf /tmp/x" in body
        assert "exec-tier gate" in body
        assert "/approve" in body and "/deny" in body
        assert "```" not in body and "`" not in body  # plain fallback is fence-free
        assert "<pre>" in html and "<code" in html and "<strong>" in html

    def test_prompt_truncates_long_context(self):
        body, _ = approval_prompt_message("cmd", "x" * 10_000)
        assert len(body) < 600

    def test_agent_voice_detection(self):
        assert is_agent_voice(SA_MXID)
        assert not is_agent_voice(OWNER)

    def test_split_approval_prompt_extracts_command_and_context(self):
        title = "Allow tool: bash\nReason: exec-tier\nCommand: rm -rf /tmp/y"
        command, context = split_approval_prompt(title)
        assert command == "rm -rf /tmp/y"
        assert "Allow tool: bash" in context and "Command:" not in context

    def test_default_timeout_falls_back_without_tools(self):
        saved = sys.modules.pop("tools.approval", None)
        try:
            sys.modules["tools.approval"] = None  # forces the import to fail
            assert default_approval_timeout() == 300.0
        finally:
            if saved is not None:
                sys.modules["tools.approval"] = saved
            else:
                sys.modules.pop("tools.approval", None)


# ---------------------------------------------------------------------------
# Queueing + gateway resolution
# ---------------------------------------------------------------------------


class TestQueueAndResolve:
    @pytest.mark.asyncio
    async def test_one_message_per_pending_request(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)
        p1 = await submit_gateway(bridge, SA, "req-1")
        p2 = await submit_gateway(bridge, SA, "req-2")
        assert bridge.pending_count == 2
        assert p1.prompt_event_id != p2.prompt_event_id
        assert poster.bodies(SA_ROOM) == [
            m["body"] for m in poster.sent if "req" in m["body"] or True
        ][:2]
        assert len(poster.bodies(SA_ROOM)) == 2
        assert all("rm -rf /tmp/probe" in b for b in poster.bodies(SA_ROOM))
        assert poster.sent[0]["sender"] == SA_MXID  # the agent's own voice

    @pytest.mark.asyncio
    async def test_reply_approve_resolves_keyed_request(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)
        resolver = bridge._resolve_gateway_fn
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(
            room_event(SA_ROOM, OWNER, "/approve", reply_to=p.prompt_event_id)
        )
        assert action == "resolved:approve"
        assert resolver.calls == [("sess-A", "once", "req-1", None)]
        assert bridge.pending_count == 0
        assert any("✔ approved (once)" in b for b in poster.bodies(SA_ROOM))

    @pytest.mark.asyncio
    async def test_scope_words_and_deny_reason_routed(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        resolver = bridge._resolve_gateway_fn
        p1 = await submit_gateway(bridge, SA, "req-1")
        await bridge.handle_event(
            room_event(SA_ROOM, OWNER, "/approve session", reply_to=p1.prompt_event_id)
        )
        p2 = await submit_gateway(bridge, SA, "req-2")
        await bridge.handle_event(
            room_event(SA_ROOM, WRITER, "!deny too broad", reply_to=p2.prompt_event_id)
        )
        assert resolver.calls == [
            ("sess-A", "session", "req-1", None),
            ("sess-A", "deny", "req-2", "too broad"),
        ]

    @pytest.mark.asyncio
    async def test_thread_root_reply_targets_prompt(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(
            room_event(SA_ROOM, OWNER, "/deny", thread_root=p.prompt_event_id)
        )
        assert action == "resolved:deny"

    @pytest.mark.asyncio
    async def test_bare_command_resolves_sole_pending(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        await submit_gateway(bridge, SA, "req-1")
        assert await bridge.handle_event(room_event(SA_ROOM, OWNER, "/approve")) == (
            "resolved:approve"
        )

    @pytest.mark.asyncio
    async def test_bare_command_ambiguous_with_two_pendings(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)
        await submit_gateway(bridge, SA, "req-1")
        await submit_gateway(bridge, SA, "req-2")
        action = await bridge.handle_event(room_event(SA_ROOM, OWNER, "/approve"))
        assert action == "denied:ambiguous"
        assert bridge.pending_count == 2  # neither resolved
        assert bridge._resolve_gateway_fn.calls == []
        assert any("2 approvals pending" in b for b in poster.bodies(SA_ROOM))

    @pytest.mark.asyncio
    async def test_bare_command_no_pending(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        assert await bridge.handle_event(room_event(SA_ROOM, OWNER, "/approve")) == (
            "denied:no_pending"
        )

    @pytest.mark.asyncio
    async def test_async_resolver_supported(self, tmp_path):
        calls = []

        async def resolver(session_key, choice, request_id, reason):
            calls.append((session_key, choice, request_id))
            return 1

        poster = FakePoster()
        bridge = ApprovalBridge(
            state=seed_state(tmp_path), poster=poster,
            authority=SetAuthority(OWNER), resolve_gateway=resolver,
            timeout=600.0, clock=FakeClock(),
        )
        p = await submit_gateway(bridge, SA, "req-1")
        assert await bridge.handle_event(
            room_event(SA_ROOM, OWNER, "/approve", reply_to=p.prompt_event_id)
        ) == "resolved:approve"
        assert calls == [("sess-A", "once", "req-1")]

    @pytest.mark.asyncio
    async def test_backend_empty_verdict_is_late(self, tmp_path):
        bridge, poster = make_bridge(tmp_path, resolver=Resolver(verdict=0))
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(
            room_event(SA_ROOM, OWNER, "/approve", reply_to=p.prompt_event_id)
        )
        assert action == "late:already_resolved"
        assert bridge.pending_count == 0
        assert any("too late" in b for b in poster.bodies(SA_ROOM))

    @pytest.mark.asyncio
    async def test_resolver_error_keeps_pending_retryable(self, tmp_path):
        bridge, _ = make_bridge(tmp_path, resolver=Resolver(raise_on_call=True))
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(
            room_event(SA_ROOM, OWNER, "/approve", reply_to=p.prompt_event_id)
        )
        assert action == "error:resolver"
        assert bridge.pending_count == 1  # retryable


# ---------------------------------------------------------------------------
# Authority, wrong room, reactions, voices
# ---------------------------------------------------------------------------


class TestAuthorityAndIgnoring:
    @pytest.mark.asyncio
    async def test_sender_without_write_power_denied(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)  # READER not in the authority set
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(
            room_event(SA_ROOM, READER, "/approve", reply_to=p.prompt_event_id)
        )
        assert action == "denied:no_authority"
        assert any("lacks write authority" in b for b in poster.bodies(SA_ROOM))
        assert bridge._resolve_gateway_fn.calls == []   # no resolution
        assert bridge.pending_count == 1                # still pending

    @pytest.mark.asyncio
    async def test_wrong_room_reply_denied_not_resolved(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)
        p = await submit_gateway(bridge, SA, "req-1")  # prompt lives in SA's room
        action = await bridge.handle_event(
            room_event(SSA_ROOM, OWNER, "/approve", reply_to=p.prompt_event_id)
        )
        assert action == "denied:wrong_room"
        assert bridge._resolve_gateway_fn.calls == []
        assert bridge.pending_count == 1
        assert any("wrong room" in b for b in poster.bodies(SSA_ROOM))

    @pytest.mark.asyncio
    async def test_reaction_events_ignored(self, tmp_path):
        """D10: reactions NEVER resolve — feed one, assert no resolution."""
        bridge, poster = make_bridge(tmp_path)
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(reaction_event(SA_ROOM, OWNER, p.prompt_event_id))
        assert action == "ignored:reaction"
        assert bridge._resolve_gateway_fn.calls == []
        assert bridge.pending_count == 1
        assert len(poster.bodies(SA_ROOM)) == 1  # only the prompt — no notice

    @pytest.mark.asyncio
    async def test_agent_voice_never_decides(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        p = await submit_gateway(bridge, SA, "req-1")
        action = await bridge.handle_event(
            room_event(SA_ROOM, SA_MXID, "/approve", reply_to=p.prompt_event_id)
        )
        assert action == "ignored:agent_voice"
        assert bridge._resolve_gateway_fn.calls == []
        assert bridge.pending_count == 1

    @pytest.mark.asyncio
    async def test_edited_command_ignored(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        p = await submit_gateway(bridge, SA, "req-1")
        event = room_event(SA_ROOM, OWNER, "/approve * /deny", reply_to=p.prompt_event_id)
        event["content"]["m.relates_to"] = {
            "rel_type": "m.replace", "event_id": "$older"
        }
        assert await bridge.handle_event(event) == "ignored:edit"
        assert bridge._resolve_gateway_fn.calls == []

    @pytest.mark.asyncio
    async def test_non_approval_message_not_ours(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        await submit_gateway(bridge, SA, "req-1")
        assert await bridge.handle_event(room_event(SA_ROOM, OWNER, "looks good")) is None
        assert await bridge.handle_event(
            {"type": "m.typing", "room_id": SA_ROOM, "sender": OWNER, "content": {}}
        ) is None


# ---------------------------------------------------------------------------
# Timeout — existing default path decides
# ---------------------------------------------------------------------------


class TestExpiry:
    @pytest.mark.asyncio
    async def test_gateway_expiry_posts_notice_without_resolving(self, tmp_path):
        clock = FakeClock()
        bridge, poster = make_bridge(tmp_path, timeout=120.0, clock=clock)
        await submit_gateway(bridge, SA, "req-1")
        clock.advance(120.0)
        expired = await bridge.check_expiry()
        assert [p.request_id for p in expired] == ["req-1"]
        assert bridge.pending_count == 0
        assert bridge._resolve_gateway_fn.calls == []  # existing path decides
        assert any("expired after 120s" in b for b in poster.bodies(SA_ROOM))

    @pytest.mark.asyncio
    async def test_not_expired_yet(self, tmp_path):
        clock = FakeClock()
        bridge, _ = make_bridge(tmp_path, timeout=120.0, clock=clock)
        await submit_gateway(bridge, SA, "req-1")
        clock.advance(119.9)
        assert await bridge.check_expiry() == []
        assert bridge.pending_count == 1

    @pytest.mark.asyncio
    async def test_socket_expiry_fails_closed(self, tmp_path):
        clock = FakeClock()
        bridge, _ = make_bridge(tmp_path, timeout=60.0, clock=clock)
        answer = SocketAnswer()
        await bridge.submit(
            SA, "sock-1", backend="socket", command="curl x", answer=answer
        )
        clock.advance(60.0)
        await bridge.check_expiry()
        assert answer.snapshot() == ("Deny", False)

    def test_forget_node_unblocks_socket_fail_closed(self, tmp_path):
        bridge, _ = make_bridge(tmp_path)
        answer = SocketAnswer()
        asyncio.run(
            bridge.submit(SA, "sock-1", backend="socket", command="curl x", answer=answer)
        )
        bridge.forget_node(SA)
        assert bridge.pending_count == 0
        assert answer.snapshot() == ("Deny", False)


# ---------------------------------------------------------------------------
# The approval socket — real unix-socket HTTP, omp child wire shape
# ---------------------------------------------------------------------------


class TestApprovalSocket:
    @pytest.mark.asyncio
    async def test_post_approved_from_matrix(self, tmp_path):
        bridge, poster = make_bridge(tmp_path, timeout=30.0)
        server = ObservatoryApprovalServer(bridge, SA, timeout=10.0)
        path = await server.start()
        try:
            client = ApprovalSocketClient(path, timeout=15.0)
            loop = asyncio.get_running_loop()
            post = loop.run_in_executor(
                None,
                lambda: client.post_approve(
                    kind="select",
                    title="Allow tool: bash\nReason: exec-tier\nCommand: rm -rf /tmp/one",
                ),
            )
            for _ in range(200):  # wait for the prompt to hit the room
                if bridge.pending_count:
                    break
                await asyncio.sleep(0.02)
            assert bridge.pending_count == 1
            pending = bridge.pendings_in_room(SA_ROOM)[0]
            assert pending.command == "rm -rf /tmp/one"
            assert any("rm -rf /tmp/one" in b for b in poster.bodies(SA_ROOM))
            action = await bridge.handle_event(
                room_event(SA_ROOM, OWNER, "/approve", reply_to=pending.prompt_event_id)
            )
            assert action == "resolved:approve"
            status, payload = await post
            assert status == 200
            assert payload == {"value": "Approve", "confirmed": True}
        finally:
            server.stop()

    @pytest.mark.asyncio
    async def test_post_denied_from_matrix(self, tmp_path):
        bridge, _ = make_bridge(tmp_path, timeout=30.0)
        server = ObservatoryApprovalServer(bridge, SA, timeout=10.0)
        path = await server.start()
        try:
            client = ApprovalSocketClient(path, timeout=15.0)
            loop = asyncio.get_running_loop()
            post = loop.run_in_executor(
                None, lambda: client.post_approve(kind="confirm", title="overwrite file?")
            )
            for _ in range(200):
                if bridge.pending_count:
                    break
                await asyncio.sleep(0.02)
            pending = bridge.pendings_in_room(SA_ROOM)[0]
            await bridge.handle_event(
                room_event(SA_ROOM, OWNER, "/deny keep it", reply_to=pending.prompt_event_id)
            )
            status, payload = await post
            assert payload == {"value": "Deny", "confirmed": False}
        finally:
            server.stop()

    @pytest.mark.asyncio
    async def test_post_timeout_fails_closed(self, tmp_path):
        clock = FakeClock()
        bridge, _ = make_bridge(tmp_path, timeout=45.0, clock=clock)
        server = ObservatoryApprovalServer(bridge, SA, timeout=30.0)
        path = await server.start()
        try:
            client = ApprovalSocketClient(path, timeout=15.0)
            loop = asyncio.get_running_loop()
            post = loop.run_in_executor(
                None, lambda: client.post_approve(kind="select", title="Command: curl evil")
            )
            for _ in range(200):
                if bridge.pending_count:
                    break
                await asyncio.sleep(0.02)
            clock.advance(45.0)                 # bridge budget elapses first
            await bridge.check_expiry()         # existing default path: deny
            status, payload = await post
            assert payload == {"value": "Deny", "confirmed": False}
        finally:
            server.stop()

    @pytest.mark.asyncio
    async def test_unknown_node_answers_deny_immediately(self, tmp_path):
        bridge, _ = make_bridge(tmp_path, timeout=30.0)
        server = ObservatoryApprovalServer(bridge, "ghost-node", timeout=10.0)
        path = await server.start()
        try:
            status, payload = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: ApprovalSocketClient(path, timeout=15.0).post_approve(
                    kind="select", title="Command: x"
                ),
            )
            assert status == 200
            assert payload == {"value": "Deny", "confirmed": False}
            assert bridge.pending_count == 0
        finally:
            server.stop()

    def test_wrong_path_404(self, tmp_path):
        import http.client

        bridge, _ = make_bridge(tmp_path)
        asyncio.run(bridge.submit(GW, "warm", backend="gateway", command="warm"))  # capture loop
        server = ObservatoryApprovalServer(bridge, SA, timeout=5.0)

        async def check():
            path = await server.start()
            conn = approvals_unix_conn(path)
            conn.request("POST", "/nope", body=b"{}")
            response = conn.getresponse()
            response.read()
            conn.close()
            return response.status

        from observatory.approvals import _UnixHTTPConnection

        def approvals_unix_conn(path):
            return _UnixHTTPConnection(path, timeout=5.0)

        try:
            assert asyncio.run(check()) == 404
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# Sim timeline — approvals at any depth land in THAT agent's room
# ---------------------------------------------------------------------------


class TestSimTimelineDepths:
    @staticmethod
    def tree_from_timeline(tmp_path):
        """Replay the scripted fan-out; adds become live state nodes."""
        from observatory.discovery import NodeEvent as DiscoveryNodeEvent
        from observatory.omp_feed import NodeEvent as OmpNodeEvent

        state = ObservatoryState(tmp_path / "sim.db")

        def node_id_for(event):
            if isinstance(event, DiscoveryNodeEvent):
                return f"{event.delegation_id}/{event.task_index}"
            return event.subagent_id

        for beat in sim.ScriptedTimeline():
            event = beat.event
            if not isinstance(event, (DiscoveryNodeEvent, OmpNodeEvent)):
                continue
            if event.kind != "add":
                continue
            node_id = node_id_for(event)
            name = getattr(event, "name", None) or getattr(event, "agent", None) or node_id
            slug = assign_slug(name, state)
            engine = "omp" if isinstance(event, OmpNodeEvent) else "hermes"
            parent = None
            if isinstance(event, DiscoveryNodeEvent) and event.parent_session != sim.GATEWAY_SESSION:
                parent = f"{sim.ORCH_DELEGATION}/0"
            elif isinstance(event, OmpNodeEvent):
                parent = f"{sim.CHILD_DELEGATION}/1"
            state.add_node(
                node_id, engine=engine, name=name, slug=slug,
                mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
                parent_node_id=parent,
            )
            state.set_room_id(node_id, f"!room-{node_id}:{SERVER}")
        return state

    @pytest.mark.asyncio
    async def test_approvals_route_to_each_depths_own_room(self, tmp_path):
        state = self.tree_from_timeline(tmp_path)
        poster = FakePoster()
        bridge = ApprovalBridge(
            state=state, poster=poster, authority=SetAuthority(OWNER),
            resolve_gateway=Resolver(), timeout=600.0, clock=FakeClock(),
        )
        orch = f"{sim.ORCH_DELEGATION}/0"          # depth 0
        child_a = f"{sim.CHILD_DELEGATION}/0"      # depth 1
        child_b = f"{sim.CHILD_DELEGATION}/1"      # depth 1
        grandchild = sim.GC_ID                     # depth 2 (in-process omp)
        rooms = set()
        for i, node in enumerate((orch, child_a, child_b, grandchild)):
            pending = await bridge.submit(
                node, f"req-{i}", backend="gateway", session_key=f"sess-{node}",
                command=f"rm -rf /tmp/{i}", context="sim approval",
            )
            assert pending is not None
            assert pending.room_id == f"!room-{node}:{SERVER}"
            rooms.add(pending.room_id)
        assert len(rooms) == 4  # four agents, four DISTINCT rooms
        # each resolves by a reply in its own room, keyed by its own request
        for i, node in enumerate((orch, child_a, child_b, grandchild)):
            pending = bridge.pending_for(node, f"req-{i}")
            action = await bridge.handle_event(
                room_event(pending.room_id, OWNER, "/deny too risky",
                           reply_to=pending.prompt_event_id)
            )
            assert action == "resolved:deny"
        assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Ingest adapters (thread-side sources → sidecar loop)
# ---------------------------------------------------------------------------


class TestAdapters:
    @pytest.mark.asyncio
    async def test_gateway_notify_schedules_submit(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)
        await bridge.submit(GW, "warm", backend="gateway", command="warm")  # capture loop
        notify = gateway_notify(bridge, SA, "sess-A")
        notify({  # runs on the blocked agent thread in production
            "request_id": "gw-9",
            "command": "dd if=/dev/zero of=/tmp/z",
            "description": "dangerous command gate",
        })
        for _ in range(50):
            if bridge.pending_count == 2:  # warm + gw-9
                break
            await asyncio.sleep(0.02)
        pending = bridge.pending_for(SA, "gw-9")
        assert pending is not None
        assert pending.session_key == "sess-A"
        assert any("dd if=/dev/zero" in b for b in poster.bodies(SA_ROOM))

    @pytest.mark.asyncio
    async def test_rpc_frame_callback_splits_prompt(self, tmp_path):
        bridge, poster = make_bridge(tmp_path)
        await bridge.submit(GW, "warm", backend="gateway", command="warm")
        on_frame = rpc_frame_callback(bridge, SA, "sess-A")
        on_frame(
            "ui_7", "select",
            "Allow tool: bash\nReason: exec-tier\nCommand: rm -rf /tmp/seven",
            ("Approve", "Deny"),
        )
        for _ in range(50):
            if bridge.pending_count == 2:
                break
            await asyncio.sleep(0.02)
        pending = bridge.pending_for(SA, "ui_7")
        assert pending.command == "rm -rf /tmp/seven"
        assert "Allow tool: bash" in (poster.sent[-1]["body"])


# ---------------------------------------------------------------------------
# The omp_rpc_transport approval-frame hook (additive M4b seam)
# ---------------------------------------------------------------------------


class _StubRequest(types.SimpleNamespace):
    def is_passive(self):
        return False


class _StubClient:
    """Serves one approval select, then idles until stopped."""

    def __init__(self, request):
        self.request = request
        self.consumed = threading.Event()
        self.sent: list[tuple] = []
        self.cancelled: list[str] = []

    def next_ui_request(self, timeout=None):
        if not self.consumed.is_set():
            self.consumed.set()
            return self.request
        time.sleep(0.05)
        raise TimeoutError("queue empty")

    def send_ui_value(self, request_id, value):
        self.sent.append((request_id, value))

    def cancel_ui_request(self, request_id):
        self.cancelled.append(request_id)


class TestRpcApprovalFrameHook:
    REQUEST = _StubRequest(
        id="ui_1", method="select",
        title="Allow tool: bash\nCommand: rm -rf /tmp/hook",
        options=("Approve", "Deny"), message=None,
    )

    def _run_serve(self, client, decision, hook):
        from tools import omp_rpc_transport as transport

        transport.set_approval_frame_hook(hook)
        recorded = {}
        real = transport.hermes_approval_decision

        def fake_decision(command, session_key=None):
            recorded["command"] = command
            return decision

        transport.hermes_approval_decision = fake_decision
        stop = threading.Event()
        try:
            thread = threading.Thread(
                target=transport.serve_approvals, args=(client, stop), daemon=True
            )
            thread.start()
            client.consumed.wait(timeout=5)
            for _ in range(100):
                if client.sent or client.cancelled:
                    break
                time.sleep(0.02)
            stop.set()
            thread.join(timeout=5)
        finally:
            transport.hermes_approval_decision = real
            transport.set_approval_frame_hook(None)
        return recorded

    def test_hook_fires_and_decision_unchanged(self):
        from tools import omp_rpc_transport as transport

        frames = []
        client = _StubClient(self.REQUEST)
        self._run_serve(client, decision=True, hook=lambda *frame: frames.append(frame))
        assert frames == [
            ("ui_1", "select", self.REQUEST.title, ("Approve", "Deny"))
        ]
        assert client.sent == [("ui_1", "Approve")]  # guard decision untouched

    def test_hook_exception_swallowed(self):
        client = _StubClient(self.REQUEST)

        def boom(*a):
            raise RuntimeError("observer down")

        self._run_serve(client, decision=False, hook=boom)
        assert client.sent == [("ui_1", "Deny")]

    def test_no_hook_identical_behavior(self):
        client = _StubClient(self.REQUEST)
        recorded = self._run_serve(client, decision=True, hook=None)
        assert recorded["command"] == "rm -rf /tmp/hook"
        assert client.sent == [("ui_1", "Approve")]

    def test_set_hook_returns_previous(self):
        from tools import omp_rpc_transport as transport

        first = lambda *a: None  # noqa: E731
        second = lambda *a: None  # noqa: E731
        try:
            assert transport.set_approval_frame_hook(first) is None
            assert transport.set_approval_frame_hook(second) is first
        finally:
            transport.set_approval_frame_hook(None)
