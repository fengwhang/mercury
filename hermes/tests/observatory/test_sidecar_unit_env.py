"""Sidecar unit gateway-env mirror + spawn per-node model fallback.

VM bug report 2026-09-11: the sidecar unit rendered ONLY
``Environment=PYTHONPATH=...`` while the gateway unit pins HERMES_HOME,
MERCURY_HOME, MERCURY_CONFIG, PI_CODING_AGENT_DIR and HERMES_OMP_BIN. The
sidecar therefore loaded the onboarding-only ``HERMES_HOME/config.yaml``
instead of the unified ``MERCURY_CONFIG`` — approvals.mode off was lost
(default manual) and ``build_hermes_agent`` found model.default empty
(``RuntimeError ... no model configured``).

Covers: the gateway env block in the rendered unit, and the spawn-side
``extra.model`` stamp carrying the effective (handle-resolved) model so
hermes children never depend solely on ambient config at respawn.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.config_gen import render_sidecar_unit
from observatory.spawn import OrchestratorRegistry, spawn_orchestrator
from observatory.state import ObservatoryState


def _kwargs() -> dict:
    return {
        "python_bin": "/opt/mercury/hermes/.venv/bin/python",
        "hermes_root": "/opt/mercury/hermes",
        "mercury_home": "/home/phoenix/.mercury",
        "log_dir": "/home/phoenix/.mercury/observatory/logs",
    }


class TestSidecarUnitEnv:
    def test_pins_unified_config_and_homes(self):
        unit = render_sidecar_unit(**_kwargs())
        assert 'Environment="MERCURY_HOME=/home/phoenix/.mercury"' in unit
        assert 'Environment="MERCURY_CONFIG=/home/phoenix/.mercury/config.yaml"' in unit
        assert 'Environment="HERMES_HOME=/home/phoenix/.mercury/hermes"' in unit

    def test_pins_agent_dir_and_omp_bin(self):
        unit = render_sidecar_unit(**_kwargs())
        assert 'Environment="PI_CODING_AGENT_DIR=/home/phoenix/.mercury/omp"' in unit
        assert (
            "Environment=\"HERMES_OMP_BIN=/opt/mercury/omp/packages/coding-agent/dist/omp\""
            in unit
        )

    def test_path_and_venv_mirror_gateway(self):
        unit = render_sidecar_unit(**_kwargs())
        assert 'Environment="VIRTUAL_ENV=/opt/mercury/hermes/.venv"' in unit
        assert "Environment=\"PATH=/opt/mercury/hermes/.venv/bin:" in unit
        assert "Environment=PYTHONPATH=/opt/mercury/hermes" in unit

    def test_explicit_overrides_win(self):
        unit = render_sidecar_unit(
            **_kwargs(),
            mercury_config="/custom/config.yaml",
            omp_bin="/custom/omp",
        )
        assert 'Environment="MERCURY_CONFIG=/custom/config.yaml"' in unit
        assert 'Environment="HERMES_OMP_BIN=/custom/omp"' in unit

    def test_sidecar_reexport_matches_config_gen(self):
        from observatory import config_gen
        from observatory import sidecar_main

        kwargs = _kwargs()
        assert sidecar_main.render_sidecar_unit(**kwargs) == (
            config_gen.render_sidecar_unit(**kwargs)
        )


class _FakeHermesAgent:
    def __init__(self, session_id: str, model: str | None = None):
        self.session_id = session_id
        if model is not None:
            self.model = model

    def close(self) -> None: ...


def _fake_omp_child(session_file: str, model: str | None = None):
    child = SimpleNamespace(
        session_file=session_file,
        _client=SimpleNamespace(
            get_state=lambda: SimpleNamespace(session_file=session_file)
        ),
        stopped=False,
    )
    if model is not None:
        child.model = model

    def _stop() -> None:
        child.stopped = True

    child.stop = _stop
    return child


def _state(tmp_path: Path) -> ObservatoryState:
    return ObservatoryState(tmp_path / "state.db")


class TestSpawnModelFallback:
    @pytest.mark.asyncio
    async def test_hermes_stamps_handle_effective_model(self, tmp_path):
        """model=None at spawn still stamps the handle-resolved model."""
        state = _state(tmp_path)
        try:
            registry = OrchestratorRegistry()
            agent = _FakeHermesAgent("sess-1", model="anthropic/claude-x")
            row = await spawn_orchestrator(
                "auth",
                "hermes",
                server_name="mercury.local",
                state=state,
                registry=registry,
                agent_factory=lambda: agent,
            )
            assert row["extra"]["model"] == "anthropic/claude-x"
            assert registry.get(row["node_id"]).model == "anthropic/claude-x"
        finally:
            state.close()

    @pytest.mark.asyncio
    async def test_hermes_explicit_model_passes_through(self, tmp_path):
        state = _state(tmp_path)
        try:
            registry = OrchestratorRegistry()
            row = await spawn_orchestrator(
                "auth",
                "hermes",
                server_name="mercury.local",
                state=state,
                registry=registry,
                model="z-ai/glm-5.3",
                agent_factory=lambda: _FakeHermesAgent("sess-1"),
            )
            assert row["extra"]["model"] == "z-ai/glm-5.3"
            assert registry.get(row["node_id"]).model == "z-ai/glm-5.3"
        finally:
            state.close()

    @pytest.mark.asyncio
    async def test_omp_stamps_handle_effective_model(self, tmp_path):
        state = _state(tmp_path)
        try:
            registry = OrchestratorRegistry()
            session_file = str(tmp_path / "session-1.jsonl")
            row = await spawn_orchestrator(
                "worker",
                "omp",
                server_name="mercury.local",
                state=state,
                registry=registry,
                omp_child_factory=lambda: _fake_omp_child(
                    session_file, model="z-ai/glm-5.3"
                ),
            )
            assert row["extra"]["model"] == "z-ai/glm-5.3"
            assert registry.get(row["node_id"]).model == "z-ai/glm-5.3"
        finally:
            state.close()
