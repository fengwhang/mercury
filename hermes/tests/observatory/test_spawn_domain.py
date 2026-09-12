"""DOMAIN NOT DEFAULT.

Spawn ghosts mint on the live domain (defect 1).
Contract:
- ``spawn_orchestrator`` takes a REQUIRED ``server_name`` (no default —
  fail loud, never fall back) and mints the ghost on it;
- the gateway ``_handle_observatory_spawn`` threads the live domain with
  documented precedence (gateway ghost mxid domain, else renderer
  ``server_name``, else loud failure);
- no production path calls ``virtual_mxid(slug)`` bare
  (``virtual_mxid`` keeps its default ONLY so unit tests mint cheaply).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.spawn import OrchestratorRegistry, spawn_orchestrator
from observatory.state import ObservatoryState


SERVER = "mercury.local"
LIVE = "vm"


class _Agent:
    def __init__(self, session_id: str = "sess-1"):
        self.session_id = session_id


@pytest.fixture()
def live_state(tmp_path: Path) -> ObservatoryState:
    """Gateway ghost minted on the LIVE domain (as the sidecar would)."""
    s = ObservatoryState(tmp_path / "state.db")
    slug = assign_slug("gateway agent", s)
    s.add_node(
        "gw",
        engine="hermes",
        name="gateway agent",
        slug=slug,
        mxid=virtual_mxid(slug, server_name=LIVE),
        session_ref="session:gw",
        parent_node_id=None,
        extra={"kind": "gateway"},
    )
    yield s
    s.close()


@pytest.fixture()
def registry() -> OrchestratorRegistry:
    return OrchestratorRegistry()


def _mixin():
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


def _gw_event(args: str = ""):
    return SimpleNamespace(
        get_command_args=lambda: args,
        metadata={
            "observatory_node_id": "gw",
            "observatory_room_id": f"!room-gw:{LIVE}",
        },
    )


class TestSpawnDomain:
    @pytest.mark.asyncio
    async def test_spawn_mints_passed_domain(self, live_state, registry):
        row = await spawn_orchestrator(
            "auth-refactor", "hermes",
            server_name=LIVE, state=live_state, registry=registry,
            agent_factory=lambda: _Agent(),
        )
        assert row["mxid"].endswith(f":{LIVE}"), row["mxid"]
        assert "mercury.local" not in row["mxid"]
        assert row["mxid"] == f"@merc_{row['slug']}:{LIVE}"

    @pytest.mark.asyncio
    async def test_spawn_without_server_name_raises(self, live_state, registry):
        with pytest.raises(TypeError):
            await spawn_orchestrator(
                "auth-refactor", "hermes",
                state=live_state, registry=registry,
                agent_factory=lambda: _Agent(),
            )

    @pytest.mark.asyncio
    async def test_spawn_empty_server_name_raises(self, live_state, registry):
        with pytest.raises(ValueError, match="server_name"):
            await spawn_orchestrator(
                "auth-refactor", "hermes",
                server_name="  ", state=live_state, registry=registry,
                agent_factory=lambda: _Agent(),
            )

    def test_server_name_has_no_default(self):
        param = inspect.signature(spawn_orchestrator).parameters["server_name"]
        assert param.default is inspect.Parameter.empty
        assert param.kind is inspect.Parameter.KEYWORD_ONLY

    def test_no_bare_virtual_mxid_on_production_paths(self):
        hermes = Path(__file__).resolve().parents[2]
        prod = [
            hermes / "observatory" / "spawn.py",
            hermes / "observatory" / "cron_rooms.py",
            hermes / "observatory" / "manual_runs.py",
            hermes / "observatory" / "render_live.py",
            hermes / "observatory" / "scripts" / "e2ee_live_gate.py",
        ]
        bare = re.compile(r"virtual_mxid\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)")
        offenders = []
        for path in prod:
            src = path.read_text(encoding="utf-8")
            for m in bare.finditer(src):
                # `virtual_mxid(slug, server_name=...)` spans past the
                # closing paren of the bare match — only flag true bares.
                tail = src[m.end():m.end() + 40].lstrip()
                if not tail.startswith(","):
                    offenders.append(f"{path.name}: {m.group(0)}")
        assert offenders == []


class TestHandlerDomainPrecedence:
    def test_ghost_domain_beats_renderer(self, live_state):
        h = _mixin()
        renderer = SimpleNamespace(gateway_node_id="gw", server_name="other.local")
        assert h._observatory_server_name(live_state, renderer) == LIVE

    def test_renderer_fallback_without_ghost_domain(self, tmp_path):
        h = _mixin()
        s = ObservatoryState(tmp_path / "state.db")
        try:
            slug = assign_slug("gateway agent", s)
            s.add_node(
                "gw", engine="hermes", name="gateway agent", slug=slug,
                mxid="@merc_bare-nodomain", session_ref="session:gw",
                parent_node_id=None, extra={"kind": "gateway"},
            )
            renderer = SimpleNamespace(gateway_node_id="gw", server_name="vm2")
            assert h._observatory_server_name(s, renderer) == "vm2"
        finally:
            s.close()

    def test_no_domain_fails_loud(self):
        h = _mixin()
        assert h._observatory_server_name(None, None) == ""

    @pytest.mark.asyncio
    async def test_handler_passes_live_domain(self, live_state, monkeypatch):
        import observatory.spawn as spawn_mod
        from observatory import platform_hook

        seen = {}

        async def fake_spawn(name, engine, **kwargs):
            seen.update(kwargs)
            return {"node_id": "orch-new", "name": name}

        monkeypatch.setattr(spawn_mod, "spawn_orchestrator", fake_spawn)
        renderer = SimpleNamespace(gateway_node_id="gw", server_name="stale.local")
        boot = SimpleNamespace(state=live_state, registry=SimpleNamespace(), renderer=renderer)
        monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
        h = _mixin()
        out = await h._handle_spawn_command(_gw_event("docs-sweep"))
        assert seen.get("server_name") == LIVE, seen
        assert "orch-new" in out

    @pytest.mark.asyncio
    async def test_handler_refuses_without_domain(self, tmp_path, monkeypatch):
        from observatory import platform_hook

        s = ObservatoryState(tmp_path / "state.db")
        try:
            boot = SimpleNamespace(state=s, registry=SimpleNamespace(), renderer=None)
            monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
            h = _mixin()
            out = await h._handle_spawn_command(_gw_event("docs-sweep"))
            # No gateway row + no provisioned home: the honest setup
            # message (BUG1-SPAWN-SERVERNAME), never an off-domain ghost.
            assert "mercury setup observatory" in out
        finally:
            s.close()

    @pytest.mark.asyncio
    async def test_handler_refuses_bare_mxid_without_domain(self, tmp_path, monkeypatch):
        from observatory import platform_hook

        s = ObservatoryState(tmp_path / "state.db")
        try:
            slug = assign_slug("gateway agent", s)
            s.add_node(
                "gw", engine="hermes", name="gateway agent", slug=slug,
                mxid="@merc_bare-nodomain", session_ref="session:gw",
                parent_node_id=None, extra={"kind": "gateway"},
            )
            boot = SimpleNamespace(state=s, registry=SimpleNamespace(), renderer=None)
            monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
            h = _mixin()
            out = await h._handle_spawn_command(_gw_event("docs-sweep"))
            # Gateway row exists but carries no domain and no other leg is
            # live: fail loud, never mint off-domain.
            assert "server_name unavailable" in out
        finally:
            s.close()
