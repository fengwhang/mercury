"""Contract tests for the D18 respawn pass (M5a, spec §2 component 2 /
D18): live 0-agents resume across sidecar/gateway restart, journal
recovery runs first, subagents never respawn, and a failed resume is an
operator event — never a death.

Filesystem is tmp_path-only; engines are injected doubles (the REAL omp
resume is the LIVE mini-gate's job). The hermes validation path uses a
REAL mercury_state.SessionDB row on a sandbox home.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory import respawn as respawn_mod
from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import IntentExecutor, Renderer
from observatory.respawn import (
    respawn_pass,
    restart_omp_orchestrator,
    resume_hermes_orchestrator,
)
from observatory.spawn import (
    OrchestratorHandle,
    OrchestratorRegistry,
    begin_exit,
)
from observatory.state import ObservatoryState, StateError

# pytest-asyncio strict mode: every async test below carries the marker.
pytestmark = pytest.mark.asyncio

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"


class FakeAgent:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeOmpChild:
    def __init__(self, session_file: str):
        self.session_file = session_file
        self.stopped = False
        self._client = SimpleNamespace(
            get_state=lambda: SimpleNamespace(session_file=session_file)
        )

    def stop(self) -> None:
        self.stopped = True


class FakeClient:
    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        return {"rooms": [{"room_id": room_id, "room_type": "m.space",
                           "children_state": []}]}

    async def create_room(self, *, name, sender, preset, invite, space=False):
        return f"!{'space' if space else 'room'}-{abs(hash(name)) % 10**8}"

    async def set_power_levels(self, rid, levels, *, sender):
        pass

    async def set_space_child(self, s, c, *, sender, via=None, remove=False):
        pass

    async def send_message(self, rid, body, *, sender, formatted_body=None):
        return "$e"

    async def delete_room(self, rid, *, block=False, purge=True):
        pass


def add_node(state, node_id, name, *, engine, parent=None, kind=None, session_ref=None):
    slug = assign_slug(name, state)
    return state.add_node(
        node_id,
        engine=engine,
        name=name,
        slug=slug,
        mxid=virtual_mxid(slug),
        session_ref=session_ref or f"session:{node_id}",
        parent_node_id=parent,
        extra={"kind": kind} if kind else None,
    )


@pytest.fixture()
def state(tmp_path: Path) -> ObservatoryState:
    s = ObservatoryState(tmp_path / "state.db")
    add_node(s, GW, "gateway agent", engine="hermes", kind="gateway")
    yield s
    s.close()


@pytest.fixture()
def registry() -> OrchestratorRegistry:
    return OrchestratorRegistry()


def make_renderer(state: ObservatoryState, client=None) -> Renderer:
    executor = IntentExecutor(
        client, state, owner_mxid=OWNER, server_name=SERVER
    ) if client is not None else None
    return Renderer(
        state,
        gateway_node_id=GW,
        server_name=SERVER,
        owner_mxid=OWNER,
        executor=executor,
    )


class TestRespawnPass:
    async def test_resumes_hermes_and_omp_zero_agents(self, state, registry, tmp_path):
        session_file = tmp_path / "o.jsonl"
        session_file.write_text("{}\n")
        add_node(state, "orch-h", "auth-refactor", engine="hermes",
                 session_ref="sess-h")
        add_node(state, "orch-o", "docs-sweep", engine="omp",
                 session_ref=str(session_file))
        seen_h: list[str] = []
        seen_o: list[str] = []

        report = await respawn_pass(
            state=state,
            registry=registry,
            hermes_factory=lambda sid: seen_h.append(sid) or FakeAgent(sid),
            omp_child_factory=lambda sf: seen_o.append(sf) or FakeOmpChild(sf),
        )
        assert report.resumed == ["orch-h", "orch-o"]  # state order
        assert seen_h == ["sess-h"]
        assert seen_o == [str(session_file)]
        assert registry.get("orch-h").agent.session_id == "sess-h"
        assert registry.get("orch-o").rpc.session_file == str(session_file)
        assert report.failed == []

    async def test_gateway_and_manual_runs_skipped_not_resumed(self, state, registry):
        calls: list[str] = []

        report = await respawn_pass(
            state=state,
            registry=registry,
            hermes_factory=lambda sid: calls.append(sid) or FakeAgent(sid),
        )
        assert report.resumed == []
        assert calls == []  # the gateway node was never handed to a factory
        assert [s["reason"] for s in report.skipped] == ["gateway node"]

        add_node(state, "mr-1", "manual:omp run", engine="omp", kind="manual-run")
        report = await respawn_pass(
            state=state, registry=registry,
            omp_child_factory=lambda sf: calls.append(sf) or FakeOmpChild(sf),
        )
        assert report.resumed == [] and calls == []
        assert any("manual-run" in s["reason"] for s in report.skipped)

    async def test_subagents_never_respawn(self, state, registry):
        add_node(state, "orch", "auth-refactor", engine="hermes")
        add_node(state, "sa", "test-sweep", engine="omp", parent="orch")
        add_node(state, "ssa", "lint-fix", engine="omp", parent="sa")
        report = await respawn_pass(
            state=state, registry=registry,
            hermes_factory=lambda sid: FakeAgent(sid),
            omp_child_factory=lambda sf: FakeOmpChild(sf),
        )
        assert report.resumed == ["orch"]  # depth>=1 left to the stale monitor
        assert registry.get("sa") is None and registry.get("ssa") is None

    async def test_failed_resume_reports_and_keeps_node_live(self, state, registry):
        add_node(state, "orch-o", "docs-sweep", engine="omp",
                 session_ref="/nonexistent/session.jsonl")
        report = await respawn_pass(
            state=state, registry=registry,
            omp_child_factory=lambda sf: FakeOmpChild(sf),
        )
        assert report.resumed == []
        assert len(report.failed) == 1 and report.failed[0]["node_id"] == "orch-o"
        # D18: restart is not death — the row stays live for the operator
        assert state.get("orch-o")["status"] == "live"

    async def test_pass_is_idempotent(self, state, registry):
        add_node(state, "orch-h", "auth-refactor", engine="hermes",
                 session_ref="sess-h")
        first = await respawn_pass(
            state=state, registry=registry,
            hermes_factory=lambda sid: FakeAgent(sid),
        )
        second = await respawn_pass(
            state=state, registry=registry,
            hermes_factory=lambda sid: pytest.fail("must not re-resume"),
        )
        assert first.resumed == ["orch-h"]
        assert second.resumed == []
        assert second.skipped == [
            {"node_id": "gw", "reason": "gateway node"},
            {"node_id": "orch-h", "reason": "already resumed"},
        ]

    async def test_journal_replay_runs_before_resumes(self, state, registry):
        add_node(state, "orch-dying", "docs-sweep", engine="omp",
                 session_ref="/tmp/obs/sessions/d.jsonl")
        state.set_space_id("orch-dying", "!space-d")
        state.set_room_id("orch-dying", "!room-d")
        state.set_space_id(GW, "!space-gw")
        state.set_room_id(GW, "!room-gw")
        # crash mid-/exit: journal written, purge never executed
        begin_exit(state, "orch-dying", renderer=make_renderer(state))

        report = await respawn_pass(
            state=state, registry=registry,
            renderer=make_renderer(state, FakeClient()),
            omp_child_factory=lambda sf: FakeOmpChild(sf),
        )
        # dead journaled node: replayed away, NOT resumed (no resurrection)
        assert report.resumed == []
        with pytest.raises(StateError):
            state.get("orch-dying")
        assert report.deferred_purges == []

    async def test_renderer_reensure_reruns_after_resume(self, state, registry):
        add_node(state, "orch-h", "auth-refactor", engine="hermes")
        client = FakeClient()
        report = await respawn_pass(
            state=state, registry=registry,
            renderer=make_renderer(state, client),
            hermes_factory=lambda sid: FakeAgent(sid),
        )
        assert report.resumed == ["orch-h"]
        # apply_plan ran: the orchestrator's space+room ids are recorded
        fresh = state.get("orch-h")
        assert fresh["space_id"] and fresh["room_id"]

    async def test_state_only_pass_without_renderer(self, state, registry):
        add_node(state, "orch-h", "auth-refactor", engine="hermes",
                 session_ref="sess-h")
        report = await respawn_pass(
            state=state, registry=registry, renderer=None,
            hermes_factory=lambda sid: FakeAgent(sid),
        )
        assert report.resumed == ["orch-h"]


class TestHermesResume:
    def test_missing_session_fails_hard(self, state, tmp_path, monkeypatch):
        row = add_node(state, "orch-h", "auth", engine="hermes",
                       session_ref="sess-gone")
        # never hand control to the heavy AIAgent builder in unit tests
        monkeypatch.setattr(
            respawn_mod, "build_hermes_agent",
            lambda **kw: pytest.fail("must not build when the row is gone"),
        )
        with pytest.raises(RuntimeError, match="not found"):
            resume_hermes_orchestrator(row, mercury_home=tmp_path)

    def test_existing_session_resolves_and_builds(self, state, tmp_path, monkeypatch):
        from mercury_state import SessionDB

        (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
        db = SessionDB(db_path=tmp_path / "hermes" / "state.db")
        db.create_session("sess-live", "cli", model="zai/glm-5.3")
        db.close()

        row = add_node(state, "orch-h", "auth", engine="hermes",
                       session_ref="sess-live")
        built: dict = {}
        monkeypatch.setattr(
            respawn_mod, "build_hermes_agent",
            lambda **kw: built.update(kw) or FakeAgent(kw.get("session_id")),
        )
        agent = resume_hermes_orchestrator(row, mercury_home=tmp_path)
        assert agent.session_id == "sess-live"
        assert built["session_id"] == "sess-live"

    def test_factory_short_circuits_validation(self, state, tmp_path):
        row = add_node(state, "orch-h", "auth", engine="hermes",
                       session_ref="sess-anything")
        agent = resume_hermes_orchestrator(
            row, mercury_home=tmp_path,
            agent_factory=lambda sid: FakeAgent(sid),
        )
        assert agent.session_id == "sess-anything"


class TestOmpRestart:
    def test_missing_session_file_fails_hard(self, state, tmp_path):
        row = add_node(state, "orch-o", "docs", engine="omp",
                       session_ref=str(tmp_path / "gone.jsonl"))
        with pytest.raises(RuntimeError, match="gone"):
            restart_omp_orchestrator(
                row, omp_child_factory=lambda sf: FakeOmpChild(sf)
            )

    def test_session_file_mismatch_stops_child_and_fails(self, state, tmp_path):
        real = tmp_path / "real.jsonl"
        real.write_text("{}\n", encoding="utf-8")
        row = add_node(state, "orch-o", "docs", engine="omp", session_ref=str(real))
        wrong = FakeOmpChild(str(tmp_path / "other.jsonl"))
        with pytest.raises(RuntimeError, match="resumed onto"):
            restart_omp_orchestrator(
                row, omp_child_factory=lambda sf: wrong
            )
        assert wrong.stopped  # never silently fork a second session

    def test_matching_session_file_accepted(self, state, tmp_path):
        real = tmp_path / "real.jsonl"
        real.write_text("{}\n", encoding="utf-8")
        row = add_node(state, "orch-o", "docs", engine="omp", session_ref=str(real))
        child = restart_omp_orchestrator(
            row, omp_child_factory=lambda sf: FakeOmpChild(sf)
        )
        assert child.session_file == str(real)
