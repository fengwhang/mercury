"""Router idle notices + M4b stale/discard + /exit planning fallback."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory.approvals import ApprovalBridge  # noqa: E402
from observatory.control import (  # noqa: E402
    QUEUED_STEER_NOTICE,
    ControlRouter,
    InjectText,
    OmpPrompt,
    OmpSteer,
    PowerLevelSnapshot,
    RoomPowerLevels,
)
from observatory.identity import assign_slug, virtual_mxid  # noqa: E402
from observatory.state import ObservatoryState  # noqa: E402

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch"
OMPC = "ompc"


def room_of(node_id: str) -> str:
    return f"!room-{node_id}:{SERVER}"


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id: str, name: str, *, engine: str, parent=None, extra=None):
        slug = assign_slug(name, state)
        state.add_node(
            node_id, engine=engine, name=name, slug=slug,
            mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
            parent_node_id=parent, extra=extra,
        )
        state.set_room_id(node_id, room_of(node_id))

    add(GW, "gateway agent", engine="hermes", parent=None, extra={"kind": "gateway"})
    add(ORCH, "docs sweep", engine="hermes", parent=None)
    add(OMPC, "lint sweep", engine="omp", parent=None)
    return state


def make_pl() -> PowerLevelSnapshot:
    rooms = {
        room_of(n): RoomPowerLevels(users={OWNER: 100}, events_default=0)
        for n in (GW, ORCH, OMPC)
    }
    return PowerLevelSnapshot(rooms)


def msg(node_id: str, body: str) -> dict:
    return {
        "type": "m.room.message", "room_id": room_of(node_id),
        "sender": OWNER, "event_id": "$e1",
        "content": {"body": body, "msgtype": "m.text"},
    }


class TestIdleNotices:
    def test_omp_idle_prompt_has_no_queued_notice(self, tmp_path):
        state = seed_state(tmp_path)
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=make_pl(),
            busy_probe=lambda _nid: False,
        )
        out = router.route(msg(OMPC, "new idea"))
        assert isinstance(out.actions[0], OmpPrompt)
        assert [n.body for n in out.notices] == []
        assert [p for p in router.pending_steers if p.node_id == OMPC] == []

    def test_omp_busy_steer_keeps_queued_notice(self, tmp_path):
        state = seed_state(tmp_path)
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=make_pl(),
            busy_probe=lambda _nid: True,
        )
        out = router.route(msg(OMPC, "keep going"))
        assert isinstance(out.actions[0], OmpSteer)
        assert [n.body for n in out.notices] == [QUEUED_STEER_NOTICE]
        assert len([p for p in router.pending_steers if p.node_id == OMPC]) == 1

    def test_hermes_idle_has_no_queued_notice(self, tmp_path):
        state = seed_state(tmp_path)
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=make_pl(),
            busy_probe=lambda _nid: False,
        )
        out = router.route(msg(ORCH, "hello?"))
        assert isinstance(out.actions[0], InjectText)
        assert [n.body for n in out.notices] == []

    def test_hermes_busy_keeps_queued_notice(self, tmp_path):
        state = seed_state(tmp_path)
        router = ControlRouter(state, gateway_node_id=GW, pl_provider=make_pl())
        out = router.route(msg(ORCH, "hello?"))
        assert [n.body for n in out.notices] == [QUEUED_STEER_NOTICE]

    def test_gateway_keeps_notice_for_downstream_defer(self, tmp_path):
        state = seed_state(tmp_path)
        router = ControlRouter(
            state, gateway_node_id=GW, pl_provider=make_pl(),
            busy_probe=lambda _nid: False,
        )
        out = router.route(msg(GW, "hello?"))
        assert isinstance(out.actions[0], InjectText)
        assert [n.body for n in out.notices] == [QUEUED_STEER_NOTICE]


class FakePoster:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.sent.append({"room_id": room_id, "body": body})
        return f"$ev{len(self.sent)}"


class SetAuthority:
    def __init__(self, *users):
        self.users = set(users)

    async def __call__(self, room_id, sender):
        return sender in self.users


def make_bridge(tmp_path, **kw):
    poster = FakePoster()
    state = seed_state(tmp_path)
    bridge = ApprovalBridge(
        state=state, poster=poster,
        authority=SetAuthority(OWNER), resolve_gateway=lambda *a: 0,
        **kw,
    )
    return bridge, poster, state


class TestBridgeTimeoutLive:
    def test_explicit_timeout_pins(self, tmp_path, monkeypatch):
        import observatory.approvals as appr_mod

        monkeypatch.setattr(appr_mod, "default_approval_timeout", lambda: 300.0)
        bridge, _, _ = make_bridge(tmp_path, timeout=60.0)
        assert bridge.effective_timeout() == 60.0
        monkeypatch.setattr(appr_mod, "default_approval_timeout", lambda: 999.0)
        assert bridge.effective_timeout() == 60.0

    def test_live_default_tracks_config(self, tmp_path, monkeypatch):
        import observatory.approvals as appr_mod

        monkeypatch.setattr(appr_mod, "default_approval_timeout", lambda: 300.0)
        bridge, _, _ = make_bridge(tmp_path)
        assert bridge.effective_timeout() == 300.0
        monkeypatch.setattr(appr_mod, "default_approval_timeout", lambda: 60.0)
        assert bridge.effective_timeout() == 60.0

    @pytest.mark.asyncio
    async def test_submit_uses_live_budget(self, tmp_path, monkeypatch):
        import observatory.approvals as appr_mod

        monkeypatch.setattr(appr_mod, "default_approval_timeout", lambda: 111.0)
        bridge, _, _ = make_bridge(tmp_path)
        pending = await bridge.submit(
            OMPC, "req-live", backend="gateway", session_key="s",
            command="rm -rf /tmp/x", context="ctx",
        )
        assert pending is not None and pending.budget == 111.0


class TestBridgeDiscard:
    @pytest.mark.asyncio
    async def test_discard_makes_approve_honest_no_pending(self, tmp_path):
        bridge, _, _ = make_bridge(tmp_path, timeout=600.0)
        pending = await bridge.submit(
            OMPC, "req-1", backend="gateway", session_key="s",
            command="rm -rf /tmp/x", context="ctx",
        )
        assert pending is not None
        assert bridge.discard_pending(OMPC, "req-1") is True
        assert bridge.pending_for(OMPC, "req-1") is None
        action = await bridge.handle_event({
            "type": "m.room.message", "room_id": room_of(OMPC),
            "sender": OWNER, "content": {"body": "/approve", "msgtype": "m.text"},
        })
        assert action == "denied:no_pending"

    def test_discard_unknown_is_false(self, tmp_path):
        bridge, _, _ = make_bridge(tmp_path, timeout=600.0)
        assert bridge.discard_pending(OMPC, "nope") is False
        assert bridge.discard_node_request("nope") is False


class TestGuardPrecheck:
    def test_hardline_never_mirrors(self):
        from tools.approval import guard_requires_human_approval

        assert guard_requires_human_approval("rm -rf /", "sess-pre") is False

    def test_session_approved_never_mirrors(self):
        from tools.approval import approve_session, clear_session, guard_requires_human_approval

        key = "sess-pre-approved"
        try:
            approve_session(key, "rm")
            assert guard_requires_human_approval("rm -rf /tmp/x", key) is False
        finally:
            clear_session(key)

    def test_dangerous_without_surface_never_mirrors(self):
        from tools.approval import guard_requires_human_approval

        assert guard_requires_human_approval("rm -rf /tmp/x", "sess-no-surface-xyz") is False

    def test_dangerous_with_notify_surface_mirrors(self):
        from tools.approval import (
            guard_requires_human_approval,
            register_gateway_notify,
            unregister_gateway_notify,
        )

        key = "sess-mirror-human"
        register_gateway_notify(key, lambda _data: None)
        try:
            assert guard_requires_human_approval("rm -rf /tmp/x", key) is True
        finally:
            unregister_gateway_notify(key)


class TestOmpMirrorGate:
    def test_mirror_skipped_for_auto_decision(self):
        from tools import omp_rpc_transport as transport

        frames: list = []
        prev_frame = transport.set_approval_frame_hook(lambda *f: frames.append(f))
        prev_settle = transport.set_approval_settle_hook(lambda *a: None)
        real = transport.hermes_approval_decision
        try:
            transport.hermes_approval_decision = lambda command, session_key=None: True

            class Req:
                id = "ui-auto"
                method = "select"
                title = "Allow tool: bash\nCommand: rm -rf /"
                options = ("Approve", "Deny")
                message = None

                def is_passive(self):
                    return False

            # _mirror_wanted must be False for a hardline auto-deny even
            # though the frame looks like an approval select.
            assert transport._mirror_wanted(Req()) is False
        finally:
            transport.hermes_approval_decision = real
            transport.set_approval_frame_hook(prev_frame)
            transport.set_approval_settle_hook(prev_settle)
        assert frames == []


def _mixin_harness():
    import sys as _sys
    import types as _types

    _au = _sys.modules.get("agent.account_usage")
    if _au is None:
        _au = _types.ModuleType("agent.account_usage")
        _au.fetch_account_usage = lambda *a, **k: None
        _au.render_account_usage_lines = lambda *a, **k: []
        _sys.modules["agent.account_usage"] = _au
    from gateway.slash_commands import GatewaySlashCommandsMixin

    return GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
def _orch_event():
    return SimpleNamespace(
        get_command_args=lambda: "",
        metadata={"observatory_node_id": ORCH, "observatory_room_id": room_of(ORCH)},
    )


class TestExitPlanningFallback:
    @pytest.mark.asyncio
    async def test_renderer_none_plans_via_shared_state(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod
        from observatory import platform_hook

        state = seed_state(tmp_path)
        boot = SimpleNamespace(state=state, registry=SimpleNamespace(), renderer=None,
                               mercury_home=str(tmp_path))
        monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
        seen = {}

        async def fake_exit(node_id, *, state=None, registry=None, renderer=None, **k):
            seen["node_id"] = node_id
            seen["executor"] = getattr(renderer, "executor", "missing")
            seen["server"] = getattr(renderer, "server_name", "")
            assert seen["server"] == SERVER
            return {"record": None, "records": [], "deferred": ["no executor attached to renderer (state-only mode)"]}

        monkeypatch.setattr(spawn_mod, "exit_orchestrator", fake_exit)
        h = _mixin_harness()
        out = await h._handle_exit_command(_orch_event())
        assert seen["node_id"] == ORCH
        assert seen["executor"] is None
        assert "shared state" in out
        assert ORCH in out or "docs sweep" in out

    def test_handles_fallback_registry_when_boot_partial(self, tmp_path, monkeypatch):
        from observatory import platform_hook

        state = seed_state(tmp_path)
        boot = SimpleNamespace(state=state, registry=None, renderer=None,
                               mercury_home=str(tmp_path))
        monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
        h = _mixin_harness()
        handles, reason = h._observatory_handles()
        assert handles is not None, reason
        _state, _registry, renderer = handles
        assert renderer is None
        assert _registry is not None
