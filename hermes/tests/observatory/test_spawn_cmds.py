"""Contract tests for Matrix /spawn + /spawnomp + /exit wiring (D6/D8/D9/D13).

Covers: registry (gateway-known, NOT cli_only, <name> hint, dispatch),
D13 scope refusals (spawn outside the gateway room; gateway /exit), the
D6 name requirement, handler fan-out to run_spawn/run_exit with the right
engine, and the generic Matrix pass-through
(gateway_session._dispatch_slash_command — no second dispatch path).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.state import ObservatoryState

asyncio = pytest.mark.asyncio
SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch-1"
OMPC = "ompc-1"


def room_of(node_id: str) -> str:
    return f"!room-{node_id}:{SERVER}"


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id: str, name: str, *, engine: str, parent=None, extra=None):
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
        state.set_space_id(node_id, f"!space-{node_id}:{SERVER}")
        state.set_room_id(node_id, room_of(node_id))

    add(GW, "gateway agent", engine="hermes", parent=None, extra={"kind": "gateway"})
    add(ORCH, "docs-sweep", engine="hermes", parent=None)
    add(OMPC, "lint-sweep", engine="omp", parent=None)
    return state


def make_event(args: str, *, node_id: str = "", room_id: str = ""):
    return SimpleNamespace(
        get_command_args=lambda: args,
        metadata={
            "observatory_node_id": node_id,
            "observatory_room_id": room_id,
        },
    )


def gw_event(args: str = ""):
    return make_event(args, node_id=GW, room_id=room_of(GW))


def orch_event(args: str = ""):
    return make_event(args, node_id=ORCH, room_id=room_of(ORCH))


class _MixinHarness:
    """GatewaySlashCommandsMixin without a runner (helpers are self-contained)."""

    def __new__(cls):
        import sys as _sys
        import types as _types
        # slash_commands imports agent.account_usage (httpx) at module top;
        # the spawn/exit helpers under test never touch it. Stub the leaf
        # so the mixin imports in the minimal venv.
        _au = _sys.modules.get("agent.account_usage")
        if _au is None:
            _au = _types.ModuleType("agent.account_usage")
            _au.fetch_account_usage = lambda *a, **k: None
            _au.render_account_usage_lines = lambda *a, **k: []
            _sys.modules["agent.account_usage"] = _au
        from gateway.slash_commands import GatewaySlashCommandsMixin

        inst = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
        return inst


def set_boot(monkeypatch, state, *, renderer=None, registry=None):
    from observatory import platform_hook

    if registry is None:
        registry = SimpleNamespace()
    if renderer is None:
        renderer = SimpleNamespace(gateway_node_id=GW)
    boot = SimpleNamespace(state=state, registry=registry, renderer=renderer)
    monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
    return boot


# --- (1) registry ------------------------------------------------------------


class TestRegistry:
    def test_spawn_registered_gateway_known(self):
        from mercury_cli.commands import (
            is_gateway_known_command,
            resolve_command,
        )

        for verb in ("spawn", "spawnomp"):
            cmd = resolve_command(verb)
            assert cmd is not None
            assert cmd.name == verb
            assert cmd.cli_only is False
            assert cmd.args_hint == "<name>"
            assert cmd.busy_policy == "dispatch"
            assert is_gateway_known_command(verb) is True

    def test_cli_exit_alias_untouched(self):
        from mercury_cli.commands import resolve_command

        cmd = resolve_command("exit")
        assert cmd is not None
        assert cmd.name == "quit"

    def test_spawn_in_gateway_verbs(self):
        from observatory.control import GATEWAY_ONLY_VERBS

        assert "spawn" in GATEWAY_ONLY_VERBS
        assert "spawnomp" in GATEWAY_ONLY_VERBS


# --- (2) /spawn + /spawnomp handlers ------------------------------------------


class TestSpawnHandlers:
    @asyncio
    async def test_spawn_name_required(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        called = []
        monkeypatch.setattr(
            spawn_mod, "spawn_orchestrator", lambda *a, **k: called.append((a, k)) or {}
        )
        h = _MixinHarness()
        out = await h._handle_spawn_command(gw_event())
        assert "name is required" in out
        assert called == []

    @asyncio
    async def test_spawn_calls_run_spawn_hermes(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        seen = {}

        async def fake_spawn(name, engine, *, state=None, registry=None, **kwargs):
            seen["name"] = name
            seen["engine"] = engine
            node_id = "orch-new"
            slug = assign_slug(name, state)
            row = state.add_node(
                node_id,
                engine=engine,
                name=name,
                slug=slug,
                mxid=virtual_mxid(slug),
                session_ref="session:new",
                parent_node_id=None,
            )
            state.set_space_id(node_id, "!space-new:mercury.local")
            state.set_room_id(node_id, "!room-new:mercury.local")
            return row

        monkeypatch.setattr(spawn_mod, "spawn_orchestrator", fake_spawn)
        h = _MixinHarness()
        out = await h._handle_spawn_command(gw_event("docs-sweep"))
        assert seen["engine"] == "hermes"
        assert seen["name"] == "docs-sweep"
        assert "!space-new" in out and "!room-new" in out

    @asyncio
    async def test_spawnomp_calls_run_spawn_omp(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        seen = {}

        async def fake_spawn(name, engine, *, state=None, registry=None, **kwargs):
            seen["engine"] = engine
            return {"node_id": "orch-omp", "name": name}

        monkeypatch.setattr(spawn_mod, "spawn_orchestrator", fake_spawn)
        h = _MixinHarness()
        await h._handle_spawnomp_command(gw_event("lint-sweep"))
        assert seen["engine"] == "omp"

    @asyncio
    async def test_spawn_refused_outside_gateway(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        called = []
        monkeypatch.setattr(
            spawn_mod, "spawn_orchestrator", lambda *a, **k: called.append((a, k)) or {}
        )
        h = _MixinHarness()
        out = await h._handle_spawn_command(orch_event("sneaky"))
        assert called == []
        assert room_of(GW) in out

    @asyncio
    async def test_spawn_no_sidecar_fails_loudly(self, tmp_path, monkeypatch):
        from observatory import platform_hook

        seed_state(tmp_path)
        monkeypatch.setattr(platform_hook, "LAST_BOOT", None)
        h = _MixinHarness()
        out = await h._handle_spawn_command(gw_event("x"))
        assert "mercury setup observatory" in out


# --- (3) /exit handler --------------------------------------------------------


class TestExitHandler:
    @asyncio
    async def test_gateway_exit_refused(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        called = []
        monkeypatch.setattr(
            spawn_mod, "exit_orchestrator", lambda *a, **k: called.append((a, k)) or {}
        )
        h = _MixinHarness()
        out = await h._handle_exit_command(gw_event())
        assert called == []
        assert "no /exit on the gateway agent" in out
        assert "/restart" in out

    @asyncio
    async def test_exit_spawned_calls_run_exit(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        seen = {}

        async def fake_exit(node_id, *, state=None, registry=None, renderer=None, **k):
            seen["node_id"] = node_id
            return {"record": None, "records": [], "deferred": []}

        monkeypatch.setattr(spawn_mod, "exit_orchestrator", fake_exit)
        h = _MixinHarness()
        out = await h._handle_exit_command(orch_event())
        assert seen["node_id"] == ORCH
        assert ORCH in out or "docs-sweep" in out

    @asyncio
    async def test_exit_unknown_room_refused(self, tmp_path, monkeypatch):
        import observatory.spawn as spawn_mod

        state = seed_state(tmp_path)
        set_boot(monkeypatch, state)
        called = []
        monkeypatch.setattr(
            spawn_mod, "exit_orchestrator", lambda *a, **k: called.append((a, k)) or {}
        )
        h = _MixinHarness()
        out = await h._handle_exit_command(
            make_event("", node_id="ghost", room_id="!room-ghost:mercury.local")
        )
        assert called == []
        assert "unknown or foreign" in out


# --- control router scope (D13) -------------------------------------------------


class TestControlScope:
    def _router(self, state):
        from observatory.control import ControlRouter, RoomPowerLevels

        rooms = {
            room_of(GW): RoomPowerLevels(users={OWNER: 100}, events_default=0),
            room_of(ORCH): RoomPowerLevels(users={OWNER: 100}, events_default=0),
            room_of(OMPC): RoomPowerLevels(users={OWNER: 100}, events_default=0),
        }
        return ControlRouter(
            state,
            gateway_node_id=GW,
            pl_provider=lambda room_id: rooms.get(room_id),
        )

    def _msg(self, node_id: str, body: str) -> dict:
        return {
            "type": "m.room.message",
            "room_id": room_of(node_id),
            "sender": OWNER,
            "event_id": "$e1",
            "content": {"body": body, "msgtype": "m.text"},
        }

    def test_spawn_scope_gate_outside_gateway(self, tmp_path):
        state = seed_state(tmp_path)
        router = self._router(state)
        outcome = router.route(self._msg(ORCH, "/spawn sneaky"))
        assert outcome.disposition == "notice:scope-gate"
        assert not outcome.actions

    def test_spawn_gateway_routes_command(self, tmp_path):
        from observatory.control import InjectText

        state = seed_state(tmp_path)
        router = self._router(state)
        outcome = router.route(self._msg(GW, "/spawn docs"))
        assert outcome.disposition == "command"
        assert len(outcome.actions) == 1
        assert isinstance(outcome.actions[0], InjectText)

    def test_exit_omp_main_routes_inject_not_prompt(self, tmp_path):
        from observatory.control import InjectText

        state = seed_state(tmp_path)
        router = self._router(state)
        outcome = router.route(self._msg(OMPC, "/exit"))
        assert outcome.disposition == "command"
        assert len(outcome.actions) == 1
        assert isinstance(outcome.actions[0], InjectText)


# --- generic Matrix pass-through -------------------------------------------------


class TestPassthrough:
    def test_dispatch_exit_reaches_runner(self, monkeypatch):
        import observatory.gateway_session as gs

        seen = {}

        class FakeRunner:
            async def _handle_message(self, event):
                seen["text"] = event.text
                seen["meta"] = dict(event.metadata or {})
                return "exit-reply"

            def _session_key_for_source(self, source):
                return "matrix-test-key"

        monkeypatch.setattr(gs, "_live_runner", lambda: FakeRunner())
        out = gs._dispatch_slash_command(
            "/exit", node_id=ORCH, room_id=room_of(ORCH)
        )
        assert out == "exit-reply"
        assert seen["text"] == "/exit"
        assert seen["meta"]["observatory_node_id"] == ORCH
        assert seen["meta"]["observatory_room_id"] == room_of(ORCH)

    def test_dispatch_unknown_still_none(self, monkeypatch):
        import observatory.gateway_session as gs

        monkeypatch.setattr(gs, "_live_runner", lambda: None)
        assert gs._dispatch_slash_command("/definitely-not-a-command-xyz") is None
