"""Approval ingest wiring (defect 2): gateway prompts reach the bridge.

Defect 2: the gateway agent turn raises its model-switch approval in the
hermes guard queue (``tools.approval``), but nothing wired the sidecar
ingest (no ``gateway_notify`` registration, no approval-frame hook), so
the room prompt never became a ``PendingApproval`` and /approve answered
``no pending approval``.

- notify-then-approve resolves via the gateway resolver (bare /approve
-   with exactly one pending resolves it);
- a gateway prompt is resolvable by a bare /approve in the SAME room,
-   while a bare /approve from another room denies (pending retained);
- a reply/thread target the bridge never observed falls back to the
-   room's sole pending (multi/no pendings keep the room-scoped answer;
-   known cross-room targets still deny);
- a broken gateway resolver (sync raise or cross-process forward failure)
-   keeps the pending for retry (``error:resolver``);
- ``wire_siblings`` registers the gateway notify (canonical session key)
-   plus the omp frame hook, and unwires both on shutdown;
- gateway Matrix turns block on the canonical key shared with the sidecar.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.approvals import (
    ApprovalBridge,
    gateway_notify,
)
from observatory.gateway_session import GATEWAY_APPROVAL_SESSION_KEY
from observatory.identity import assign_slug, virtual_mxid
from observatory.state import ObservatoryState


SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
WRITER = "@alice:mercury.local"
GW = "gw"
SA = "sa-tests"
SA_ROOM = f"!room-{SA}:{SERVER}"
GW_ROOM = f"!room-{GW}:{SERVER}"
KEY = "sess-wiring"


class FakePoster:
    def __init__(self):
        self.sent: list[dict] = []
        self._n = 0

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self._n += 1
        event_id = f"$ev{self._n}"
        self.sent.append({"room_id": room_id, "body": body, "sender": sender,
                          "event_id": event_id})
        return event_id


class Resolver:
    def __init__(self, verdict=1):
        self.calls: list[tuple] = []
        self.verdict = verdict

    def __call__(self, session_key, choice, request_id, reason):
        self.calls.append((session_key, choice, request_id, reason))
        return self.verdict


class SetAuthority:
    def __init__(self, *users):
        self.users = set(users)

    async def __call__(self, room_id, sender):
        return sender in self.users


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id, name, *, engine, parent):
        slug = assign_slug(name, state)
        state.add_node(
            node_id, engine=engine, name=name, slug=slug,
            mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
            parent_node_id=parent,
        )
        state.set_room_id(node_id, f"!room-{node_id}:{SERVER}")

    add(GW, "gateway agent", engine="hermes", parent=None)
    add(SA, "test-sweep", engine="omp", parent=None)
    return state


def make_bridge(tmp_path, **kw):
    poster = FakePoster()
    bridge = ApprovalBridge(
        state=seed_state(tmp_path), poster=poster,
        authority=kw.get("authority") or SetAuthority(OWNER, WRITER),
        resolve_gateway=kw.get("resolver") or Resolver(),
        timeout=kw.get("timeout", 600.0),
    )
    return bridge, poster


def room_event(room_id, sender, body, *, reply_to=None):
    content = {"msgtype": "m.text", "body": body}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return {"type": "m.room.message", "room_id": room_id, "sender": sender,
            "content": content, "event_id": "$in1"}


async def _wait_for(pred, timeout=5.0):
    await asyncio.wait_for(_poll(pred), timeout=timeout)


async def _poll(pred):
    while not pred():
        await asyncio.sleep(0.02)


class TestNotifyThenApprove:
    @pytest.mark.asyncio
    async def test_notify_then_bare_approve_resolves(self, tmp_path):
        resolver = Resolver()
        bridge, poster = make_bridge(tmp_path, resolver=resolver)
        await bridge.submit(GW, "warm", backend="gateway", command="warm")
        notify = gateway_notify(bridge, SA, KEY)
        notify({"request_id": "gw-9", "command": "rm -rf /tmp/z",
                "description": "exec-tier"})
        await _wait_for(lambda: bridge.pending_for(SA, "gw-9") is not None)
        action = await bridge.handle_event(room_event(SA_ROOM, OWNER, "/approve"))
        assert action == "resolved:approve"
        assert resolver.calls == [(KEY, "once", "gw-9", None)]
        assert bridge.pending_for(SA, "gw-9") is None
        assert any("approved" in m["body"] for m in poster.sent
                   if m["room_id"] == SA_ROOM)

    @pytest.mark.asyncio
    async def test_bare_approve_zero_pendings_still_no_pending(self, tmp_path):
        resolver = Resolver()
        bridge, poster = make_bridge(tmp_path, resolver=resolver)
        action = await bridge.handle_event(room_event(SA_ROOM, OWNER, "/approve"))
        assert action == "denied:no_pending"
        assert resolver.calls == []
        assert any("no pending approval" in m["body"] for m in poster.sent
                   if m["room_id"] == SA_ROOM)

    @pytest.mark.asyncio
    async def test_reply_targeted_approve_hits_right_pending(self, tmp_path):
        resolver = Resolver()
        bridge, _poster = make_bridge(tmp_path, resolver=resolver)
        first = await bridge.submit(
            SA, "req-1", backend="gateway", session_key=KEY,
            command="rm -rf /tmp/one", context="first")
        second = await bridge.submit(
            SA, "req-2", backend="gateway", session_key=KEY,
            command="rm -rf /tmp/two", context="second")
        action = await bridge.handle_event(room_event(
            SA_ROOM, OWNER, "/approve", reply_to=second.prompt_event_id))
        assert action == "resolved:approve"
        assert resolver.calls == [(KEY, "once", "req-2", None)]
        assert bridge.pending_for(SA, "req-1") is not None
        assert bridge.pending_for(SA, "req-2") is None
        assert first.prompt_event_id != second.prompt_event_id


def _daemon(monkeypatch, tmp_path):
    import observatory.sidecar_main as sm
    from observatory.renderer import Renderer

    home = tmp_path / "mhome"
    (home / "hermes").mkdir(parents=True)
    daemon = sm.SidecarDaemon.__new__(sm.SidecarDaemon)
    daemon.mercury_home = home
    daemon.paths = SimpleNamespace()
    daemon.state = seed_state(tmp_path)
    daemon.gateway_mxid = daemon.state.get(GW)["mxid"]
    daemon.renderer = Renderer(
        daemon.state, gateway_node_id=GW, server_name="vm",
        owner_mxid=OWNER, executor=None,
    )
    sent: list = []

    async def _send(room_id, body, *, sender, formatted_body=None):
        event_id = f"$ev{len(sent) + 1}"
        sent.append({"room_id": room_id, "body": body, "event_id": event_id})
        return event_id

    daemon.client = SimpleNamespace(send_message=_send, sent=sent)
    daemon.gateway_transport = None
    daemon.control_router = None
    daemon.approvals = None
    daemon.directives = None
    daemon.cron_rooms = None
    daemon.manual_runs = None
    return sm, daemon


def _is_daemon_router(hook, daemon) -> bool:
    """Bound methods compare unequal across reads — compare instance + func."""
    import observatory.sidecar_main as sm
    return (getattr(hook, "__self__", None) is daemon
            and getattr(hook, "__func__", None) is sm.SidecarDaemon._omp_approval_frame_router)


class TestWireSiblings:
    def test_registers_gateway_notify_and_frame_hook(self, tmp_path, monkeypatch):
        import tools.approval as approval_mod
        import tools.omp_rpc_transport as rpc_transport

        sm, daemon = _daemon(monkeypatch, tmp_path)
        seen_notify: dict = {}
        seen_hook: dict = {}

        def fake_register(key, cb):
            seen_notify["key"] = key
            seen_notify["cb"] = cb

        monkeypatch.setattr(approval_mod, "register_gateway_notify", fake_register)
        monkeypatch.setattr(
            approval_mod, "unregister_gateway_notify", lambda key: None)
        monkeypatch.setattr(
            rpc_transport, "set_approval_frame_hook",
            lambda cb: seen_hook.setdefault("cb", cb))

        daemon.wire_siblings()

        assert seen_notify.get("key") == GATEWAY_APPROVAL_SESSION_KEY
        assert callable(seen_notify.get("cb"))
        assert callable(seen_hook.get("cb"))
        try:
            daemon.state.close()
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_frame_router_mirrors_single_omp_child(self, tmp_path, monkeypatch):
        import tools.omp_rpc_transport as rpc_transport

        sm, daemon = _daemon(monkeypatch, tmp_path)
        real_hook = rpc_transport._approval_frame_hook
        monkeypatch.setattr(
            "tools.approval.unregister_gateway_notify", lambda key: None)
        daemon.wire_siblings()
        try:
            assert _is_daemon_router(rpc_transport._approval_frame_hook, daemon)
            daemon._omp_approval_frame_router(
                "ui_9", "select",
                "Allow tool: bash\nReason: exec-tier\nCommand: rm -rf /tmp/nine",
                ("Approve", "Deny"),
            )
            await _wait_for(lambda: daemon.approvals.pending_for(SA, "ui_9") is not None)
            pending = daemon.approvals.pending_for(SA, "ui_9")
            assert pending.command == "rm -rf /tmp/nine"
        finally:
            rpc_transport._approval_frame_hook = real_hook
            daemon._unwire_approval_ingest()
            try:
                daemon.state.close()
            except Exception:
                pass

    def test_unwire_clears_registrations(self, tmp_path, monkeypatch):
        import tools.approval as approval_mod
        import tools.omp_rpc_transport as rpc_transport

        sm, daemon = _daemon(monkeypatch, tmp_path)
        monkeypatch.setattr(
            approval_mod, "unregister_gateway_notify", lambda key: None)
        daemon.wire_siblings()
        assert _is_daemon_router(rpc_transport._approval_frame_hook, daemon)
        daemon._unwire_approval_ingest()
        assert rpc_transport._approval_frame_hook is None
        try:
            daemon.state.close()
        except Exception:
            pass


class TestGatewayTurnSessionKey:
    def test_turn_blocks_on_canonical_key(self, monkeypatch):
        import tools.approval as approval_mod
        from observatory import gateway_session as gs

        seen: dict = {}
        real_register = approval_mod.register_gateway_notify
        real_unregister = approval_mod.unregister_gateway_notify

        def fake_register(key, cb):
            seen["key"] = key
            seen["cb"] = cb
            return real_register(key, cb)

        monkeypatch.setattr(approval_mod, "register_gateway_notify", fake_register)

        def turn(agent, text):
            from tools.approval import get_current_session_key
            seen["ambient"] = get_current_session_key()
            seen["during"] = approval_mod._gateway_notify_cbs.get(
                GATEWAY_APPROVAL_SESSION_KEY)
            return {"final_response": "ok"}

        reply, _events = gs.run_gateway_prompt_with_events(
            "hello", kind="prompt", node_id="gw",
            agent_factory=lambda sid: object(), turn=turn)
        assert reply == "ok"
        assert seen["ambient"] == GATEWAY_APPROVAL_SESSION_KEY
        assert seen["key"] == GATEWAY_APPROVAL_SESSION_KEY
        assert seen["during"] is not None
        assert GATEWAY_APPROVAL_SESSION_KEY not in approval_mod._gateway_notify_cbs
        assert real_unregister is not None


class TestSameRoomBareApprove:
    """A visible gateway prompt is always resolvable by a bare /approve
    in the SAME room; a bare /approve from another room denies."""

    @pytest.mark.asyncio
    async def test_gateway_prompt_then_bare_approve_same_room(self, tmp_path):
        resolver = Resolver()
        bridge, poster = make_bridge(tmp_path, resolver=resolver)
        pending = await bridge.submit(
            GW, "gw-1", backend="gateway",
            session_key=GATEWAY_APPROVAL_SESSION_KEY,
            command="rm -rf /tmp/gw-probe", context="gateway guard")
        assert pending.room_id == GW_ROOM
        action = await bridge.handle_event(room_event(GW_ROOM, OWNER, "/approve"))
        assert action == "resolved:approve"
        assert resolver.calls == [(GATEWAY_APPROVAL_SESSION_KEY, "once", "gw-1", None)]
        assert bridge.pending_for(GW, "gw-1") is None
        assert any("approved" in m["body"] for m in poster.sent
                   if m["room_id"] == GW_ROOM)

    @pytest.mark.asyncio
    async def test_bare_approve_from_other_room_denied(self, tmp_path):
        resolver = Resolver()
        bridge, _poster = make_bridge(tmp_path, resolver=resolver)
        await bridge.submit(
            SA, "req-1", backend="gateway", session_key=KEY,
            command="rm -rf /tmp/other-room", context="sa guard")
        action = await bridge.handle_event(room_event(GW_ROOM, OWNER, "/approve"))
        assert action == "denied:no_pending"
        assert resolver.calls == []
        assert bridge.pending_for(SA, "req-1") is not None


class TestUnknownReplyFallback:
    """Reply/thread metadata the bridge never observed (stripped clients,
    unobserved event ids) falls back to the room's sole pending."""

    @pytest.mark.asyncio
    async def test_reply_to_unknown_event_resolves_sole_pending(self, tmp_path):
        resolver = Resolver()
        bridge, _poster = make_bridge(tmp_path, resolver=resolver)
        await bridge.submit(
            GW, "gw-1", backend="gateway",
            session_key=GATEWAY_APPROVAL_SESSION_KEY,
            command="rm -rf /tmp/gw-fallback", context="gateway guard")
        action = await bridge.handle_event(room_event(
            GW_ROOM, OWNER, "/approve", reply_to="$never-observed"))
        assert action == "resolved:approve"
        assert resolver.calls == [(GATEWAY_APPROVAL_SESSION_KEY, "once", "gw-1", None)]
        assert bridge.pending_for(GW, "gw-1") is None

    @pytest.mark.asyncio
    async def test_reply_to_unknown_event_with_two_pendings_is_ambiguous(self, tmp_path):
        resolver = Resolver()
        bridge, poster = make_bridge(tmp_path, resolver=resolver)
        await bridge.submit(
            GW, "gw-1", backend="gateway",
            session_key=GATEWAY_APPROVAL_SESSION_KEY, command="one")
        await bridge.submit(
            GW, "gw-2", backend="gateway",
            session_key=GATEWAY_APPROVAL_SESSION_KEY, command="two")
        action = await bridge.handle_event(room_event(
            GW_ROOM, OWNER, "/approve", reply_to="$never-observed"))
        assert action == "denied:ambiguous"
        assert resolver.calls == []
        assert bridge.pending_count == 2
        assert any("2 approvals pending" in m["body"] for m in poster.sent
                   if m["room_id"] == GW_ROOM)

    @pytest.mark.asyncio
    async def test_reply_to_wrong_room_prompt_still_denied(self, tmp_path):
        resolver = Resolver()
        bridge, _poster = make_bridge(tmp_path, resolver=resolver)
        pending = await bridge.submit(
            SA, "req-1", backend="gateway", session_key=KEY,
            command="rm -rf /tmp/sa-probe", context="sa guard")
        action = await bridge.handle_event(room_event(
            GW_ROOM, OWNER, "/approve", reply_to=pending.prompt_event_id))
        assert action == "denied:wrong_room"
        assert resolver.calls == []
        assert bridge.pending_for(SA, "req-1") is not None


class TestResolverErrorKeepsPending:
    """A broken gateway resolver never drops the pending (retryable)."""

    @pytest.mark.asyncio
    async def test_bare_approve_resolver_error_keeps_pending(self, tmp_path):
        class _Boom:
            def __call__(self, *a):
                raise RuntimeError("resolver down")

        bridge, _poster = make_bridge(tmp_path, resolver=_Boom())
        await bridge.submit(
            GW, "gw-1", backend="gateway",
            session_key=GATEWAY_APPROVAL_SESSION_KEY, command="rm -rf /tmp/boom")
        action = await bridge.handle_event(room_event(GW_ROOM, OWNER, "/approve"))
        assert action == "error:resolver"
        assert bridge.pending_for(GW, "gw-1") is not None

    @pytest.mark.asyncio
    async def test_forward_failure_keeps_pending_for_retry(self, tmp_path, monkeypatch):
        from observatory.approvals import ApprovalBridge
        from observatory.gateway_transport import GatewayTransportError

        sm, daemon = _daemon(monkeypatch, tmp_path)
        try:
            class _FailingTransport:
                async def resolve_approval(self, *a, **k):
                    raise GatewayTransportError("gateway down")

            daemon.gateway_transport = _FailingTransport()
            poster = FakePoster()
            bridge = ApprovalBridge(
                state=daemon.state, poster=poster,
                authority=SetAuthority(OWNER, WRITER),
                resolve_gateway=daemon._resolve_gateway_approval,
                timeout=600.0,
            )
            await bridge.submit(
                GW, "gw-1", backend="gateway",
                session_key=GATEWAY_APPROVAL_SESSION_KEY,
                command="rm -rf /tmp/gw-down", context="gateway guard")
            action = await bridge.handle_event(room_event(GW_ROOM, OWNER, "/approve"))
            assert action == "error:resolver"
            assert bridge.pending_for(GW, "gw-1") is not None
        finally:
            try:
                daemon.state.close()
            except Exception:
                pass
