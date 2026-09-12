"""BUG1-SPAWN-SERVERNAME red loop: gateway-thread spawn on the live domain.

Gateway process has no live renderer (it lives in the sidecar daemon), so
/spawn derives server_name from the shared state.db gateway ghost mxid
domain. When the gateway-thread boot (client=None, renderer None) opens a
provisioned home whose state.db has no gateway row yet, both precedence
legs miss and /spawn fails with "live server_name unavailable".

Fixed behavior: provision/boot project the live toml server_name + gateway
ghost mxid into shared state.db (never a mercury.local default), the
handler resolves the live domain from state meta/toml as well, and an
unprovisioned home fails LOUD with the setup message.
"""
from __future__ import annotations

import sys as _sys
import types as _types
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.config_gen import ObservatoryPaths
from observatory.state import ObservatoryState

LIVE = "livevm"

TOML_LIVE = (
    "[global]\nserver_name = \"livevm\"\naddress = \"127.0.0.1\"\n"
    "port = 18008\ndatabase_path = \"db\"\nappservice_dir = \"as\"\n"
    "allow_federation = false\nallow_registration = false\n"
    "registration_token = \"tok\"\n"
)


def _provisioned_home(tmp_path: Path, *, server_name: str = LIVE) -> Path:
    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.bin_dir, paths.db_dir,
              paths.appservices_dir, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    toml = TOML_LIVE if server_name == LIVE else TOML_LIVE.replace(LIVE, server_name)
    paths.toml.write_text(toml, encoding="utf-8")
    return home


def _mixin():
    _au = _sys.modules.get("agent.account_usage")
    if _au is None:
        _au = _types.ModuleType("agent.account_usage")
        _au.fetch_account_usage = lambda *a, **k: None
        _au.render_account_usage_lines = lambda *a, **k: []
        _sys.modules["agent.account_usage"] = _au
    from gateway.slash_commands import GatewaySlashCommandsMixin
    return GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)


def _gw_event():
    return SimpleNamespace(
        get_command_args=lambda: "docs-sweep",
        metadata={"observatory_node_id": "gw",
                  "observatory_room_id": f"!room-gw:{LIVE}"},
    )


class TestBootProjectsLiveDomain:
    @pytest.mark.asyncio
    async def test_boot_sidecar_seeds_gateway_on_live_domain(self, tmp_path):
        """Gateway-thread boot (client=None, renderer None) still seeds the
        gateway ghost mxid on the LIVE toml domain into shared state.db."""
        from observatory import platform_hook

        home = _provisioned_home(tmp_path)
        result = await platform_hook.boot_sidecar(
            home, config={"observatory": {"enabled": True}},
            client=None, discovery=False,
        )
        assert result.state is not None
        try:
            gw = result.state.get("gw")
        finally:
            try:
                result.state.close()
            except Exception:
                pass
        assert gw["mxid"].endswith(f":{LIVE}"), gw["mxid"]
        assert "mercury.local" not in gw["mxid"]

    def test_server_name_resolves_from_live_toml_when_state_bare(self, tmp_path):
        """No gateway mxid domain + no renderer: live toml domain wins,
        never mercury.local, never empty."""
        from observatory import platform_hook
        from observatory.identity import assign_slug

        home = _provisioned_home(tmp_path)
        state = ObservatoryState(tmp_path / "state.db")
        try:
            slug = assign_slug("gateway agent", state)
            state.add_node(
                "gw", engine="hermes", name="gateway agent", slug=slug,
                mxid="@merc_bare-nodomain", session_ref="session:gw",
                parent_node_id=None, extra={"kind": "gateway"},
            )
            boot = SimpleNamespace(state=state, registry=SimpleNamespace(),
                                   renderer=None, mercury_home=str(home))
            old = platform_hook.LAST_BOOT
            platform_hook.LAST_BOOT = boot
            try:
                h = _mixin()
                assert h._observatory_server_name(state, None) == LIVE
            finally:
                platform_hook.LAST_BOOT = old
        finally:
            state.close()

    @pytest.mark.asyncio
    async def test_spawn_succeeds_from_gateway_room_on_live_domain(self, tmp_path, monkeypatch):
        """End-to-end symptom: provisioned home, empty shared state,
        renderer None (gateway process) — /spawn mints on the live domain."""
        import observatory.spawn as spawn_mod
        from observatory import platform_hook

        home = _provisioned_home(tmp_path)
        state = ObservatoryState(tmp_path / "state.db")
        seen = {}

        async def fake_spawn(name, engine, **kwargs):
            seen.update(kwargs)
            return {"node_id": "orch-new", "name": name}

        monkeypatch.setattr(spawn_mod, "spawn_orchestrator", fake_spawn)
        boot = SimpleNamespace(state=state, registry=SimpleNamespace(),
                               renderer=None, mercury_home=str(home))
        monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
        try:
            h = _mixin()
            out = await h._handle_spawn_command(_gw_event())
        finally:
            state.close()
        assert seen.get("server_name") == LIVE, (seen, out)
        assert "orch-new" in out
        assert "server_name unavailable" not in out

    @pytest.mark.asyncio
    async def test_unprovisioned_home_fails_loud_with_setup(self, tmp_path, monkeypatch):
        """No toml + no gateway node: honest setup message, never the
        server_name error."""
        from observatory import platform_hook

        home = tmp_path / "mercury"  # nothing provisioned: no toml, no state gw
        state = ObservatoryState(tmp_path / "state.db")
        boot = SimpleNamespace(state=state, registry=SimpleNamespace(),
                               renderer=None, mercury_home=str(home))
        monkeypatch.setattr(platform_hook, "LAST_BOOT", boot)
        try:
            h = _mixin()
            out = await h._handle_spawn_command(_gw_event())
        finally:
            state.close()
        assert "mercury setup observatory" in out
