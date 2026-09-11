"""Contract tests for the spawned-orchestrator lifecycle (M5a, spec D9
/spawn //spawnomp //exit; D8 depth-0 cascade; D18 purge-journal crash
atomicity).

Filesystem is tmp_path-only; engines are injected doubles (the REAL omp
child is exercised by the LIVE mini-gate, not here). Matrix side uses the
IntentExecutor against a recording fake client — no homeserver.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixError
from observatory.renderer import (
    DetachChild,
    IntentExecutor,
    LeaveRoom,
    PurgeRoom,
    Renderer,
    SendMessage,
)
from observatory.spawn import (
    OrchestratorHandle,
    OrchestratorRegistry,
    begin_exit,
    deserialize_intents,
    exit_orchestrator,
    finish_exit,
    omp_session_file,
    omp_sessions_dir,
    omp_spawn_argv,
    read_purge_journal,
    replay_purge_journal,
    serialize_intents,
    spawn_orchestrator,
    validate_spawn_session_ref,
)
from observatory.state import ObservatoryState, StateError

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"


# ---------------------------------------------------------------------------
# Harness: seeded state + fake engines + fake matrix client
# ---------------------------------------------------------------------------


def seed_gateway(state: ObservatoryState) -> str:
    slug = assign_slug("gateway agent", state)
    state.add_node(
        GW,
        engine="hermes",
        name="gateway agent",
        slug=slug,
        mxid=virtual_mxid(slug),
        session_ref="session:gw",
        parent_node_id=None,
        extra={"kind": "gateway"},
    )
    # matrix ids: /exit summaries render into the gateway room through the
    # executor, so it must resolve.
    state.set_space_id(GW, "!space-gw")
    state.set_room_id(GW, "!room-gw")
    return GW


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


class FakeHermesAgent:
    """Started-hermes-orchestrator double: has a session_id, closes."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeOmpChild:
    """Started-omp-orchestrator double: OmpRpcChild surface subset
    (stop(), _client.get_state().session_file)."""

    def __init__(self, session_file: str):
        self.session_file = session_file
        self.stopped = False
        self._client = SimpleNamespace(
            get_state=lambda: SimpleNamespace(session_file=session_file)
        )

    def stop(self) -> None:
        self.stopped = True


def _matrix_error(status: int, message: str) -> MatrixError:
    return MatrixError(
        "DELETE", "/_synapse/admin/v2/rooms/x",
        status, {"errcode": "M_UNKNOWN", "error": message},
    )


@dataclass
class FakeClient:
    """Recording matrix client (test_renderer pattern) + wedged-purge faults."""

    calls: list = field(default_factory=list)
    next_id: int = 0
    fail_purge: dict = field(default_factory=dict)

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def room_hierarchy(self, room_id: str, *, sender: str, suggested_only: bool = False):
        self.calls.append(("hierarchy", room_id, sender))
        return {"rooms": [{"room_id": room_id, "room_type": "m.space",
                           "children_state": []}]}

    async def create_room(self, *, name, sender, preset, invite, space=False):
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space))
        return self._id("!space" if space else "!room")

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender, formatted_body))
        return self._id("$ev")

    async def edit_message(self, room_id, event_id, body, *, sender, formatted_body=None):
        self.calls.append(("edit", room_id, event_id, body, sender, formatted_body))
        return self._id("$ev")

    async def invite(self, room_id: str, user_id: str, *, sender):
        self.calls.append(("invite", room_id, user_id, sender))

    async def join_room(self, room_id: str, *, sender):
        self.calls.append(("join", room_id, sender))
        return room_id

    async def leave_room(self, room_id: str, *, sender):
        self.calls.append(("leave", room_id, sender))

    async def leave_room_as_owner(self, room_id: str):
        self.calls.append(("leave-owner", room_id))

    async def delete_room(self, room_id: str, *, block=False, purge=True):
        self.calls.append(("delete", room_id, block, purge))
        if room_id in self.fail_purge:
            raise _matrix_error(self.fail_purge[room_id], f"wedged {room_id}")


def purge_ids(client: FakeClient) -> set[str]:
    return {c[1] for c in client.calls if c[0] == "delete"}


@dataclass
class Engines:
    """Injected engine doubles + their bookkeeping."""

    agents: list = field(default_factory=list)
    omps: list = field(default_factory=list)

    def hermes_factory(self):
        def build():
            agent = FakeHermesAgent(f"sess-{len(self.agents) + 1}")
            self.agents.append(agent)
            return agent

        return build

    def omp_factory(self, session_dir: Path):
        def build():
            n = len(self.omps) + 1
            child = FakeOmpChild(str(session_dir / f"session-{n}.jsonl"))
            self.omps.append(child)
            return child

        return build


@pytest.fixture()
def state(tmp_path: Path) -> ObservatoryState:
    s = ObservatoryState(tmp_path / "state.db")
    seed_gateway(s)
    yield s
    s.close()


@pytest.fixture()
def registry() -> OrchestratorRegistry:
    return OrchestratorRegistry()


@pytest.fixture()
def engines() -> Engines:
    return Engines()


# ---------------------------------------------------------------------------
# spawn (D9)
# ---------------------------------------------------------------------------


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_hermes_registers_depth0_node(self, state, registry, engines):
        row = await spawn_orchestrator(
            "auth-refactor",
            "hermes",
            server_name=SERVER,
            state=state,
            registry=registry,
            agent_factory=engines.hermes_factory(),
        )
        assert row["depth"] == 0
        assert row["parent_node_id"] is None
        assert row["status"] == "live"
        assert row["engine"] == "hermes"
        assert row["name"] == "auth-refactor"
        assert row["session_ref"] == "sess-1"
        assert "kind" not in row["extra"]  # plain agent node (tree convention)
        handle = registry.get(row["node_id"])
        assert handle is not None
        assert handle.agent is engines.agents[0]
        assert handle.engine == "hermes"

    @pytest.mark.asyncio
    async def test_spawn_omp_registers_session_file(self, state, registry, engines, tmp_path):
        row = await spawn_orchestrator(
            "docs-sweep",
            "omp",
            server_name=SERVER,
            state=state,
            registry=registry,
            omp_child_factory=engines.omp_factory(tmp_path / "omp-sessions"),
        )
        assert row["depth"] == 0
        assert row["parent_node_id"] is None
        assert row["engine"] == "omp"
        assert row["session_ref"].endswith(".jsonl")
        assert str(tmp_path / "omp-sessions") in row["session_ref"]
        handle = registry.get(row["node_id"])
        assert handle.rpc is engines.omps[0]

    @pytest.mark.asyncio
    async def test_spawn_gets_space_and_room_via_renderer_intents(
        self, state, registry, engines
    ):
        client = FakeClient()
        renderer = make_renderer(state, client)
        row = await spawn_orchestrator(
            "auth-refactor",
            "hermes",
            server_name=SERVER,
            state=state,
            registry=registry,
            renderer=renderer,
            agent_factory=engines.hermes_factory(),
        )
        fresh = state.get(row["node_id"])
        assert fresh["space_id"] and fresh["room_id"]
        # the orchestrator subspace is attached under matrix (non-remove child op)
        children = [(c[1], c[2]) for c in client.calls if c[0] == "child" and not c[5]]
        assert any(child == fresh["space_id"] for _, child in children)
        assert "kind" not in row["extra"]  # plain agent node (tree convention)
        # lifecycle message rendered into the new room
        assert any(
            c[0] == "send" and c[1] == fresh["room_id"] and "spawned" in c[2]
            for c in client.calls
        )

    @pytest.mark.asyncio
    async def test_spawn_slug_reuse_only_when_predecessor_dead(
        self, state, registry, engines
    ):
        first = await spawn_orchestrator(
            "auth", "hermes", server_name=SERVER, state=state, registry=registry,
            agent_factory=engines.hermes_factory(),
        )
        second = await spawn_orchestrator(
            "auth", "hermes", server_name=SERVER, state=state, registry=registry,
            agent_factory=engines.hermes_factory(),
        )
        # both live → D17 collision suffix
        assert first["slug"] == "auth" and second["slug"] == "auth-2"
        # predecessor dead → invisible to collisions (D17)
        state.mark_dead(second["node_id"])
        third = await spawn_orchestrator(
            "auth", "hermes", server_name=SERVER, state=state, registry=registry,
            agent_factory=engines.hermes_factory(),
        )
        assert third["slug"] == "auth-2"
        assert third["mxid"] == second["mxid"]  # MXID inherited, nothing else

    @pytest.mark.asyncio
    async def test_spawn_validates_name_and_engine(self, state, registry):
        with pytest.raises(ValueError, match="name"):
            await spawn_orchestrator("  ", "hermes", server_name=SERVER, state=state, registry=registry)
        with pytest.raises(ValueError, match="engine"):
            await spawn_orchestrator("x", "telegram", server_name=SERVER, state=state, registry=registry)

    def test_omp_argv_fresh_vs_resume(self, tmp_path):
        fresh = omp_spawn_argv(
            omp_path="/bin/omp", model="zai/glm-5.3",
            session_dir=tmp_path, thinking_level="xhigh",
        )
        assert fresh == [
            "/bin/omp", "--mode", "rpc", "--model", "zai/glm-5.3",
            "--thinking", "xhigh", "--session-dir", str(tmp_path),
        ]
        resume = omp_spawn_argv(
            omp_path="/bin/omp", model="zai/glm-5.3",
            session_dir=tmp_path, resume_session="/tmp/s.jsonl",
        )
        assert "--resume" in resume and "/tmp/s.jsonl" in resume
        assert "--session-dir" not in resume

class TestSpawnValidation:
    """Spawn-time gate: dir-writable, never file-exists (lazy sessions
    materialize only after the first turn)."""

    def test_lazy_omp_file_passes(self, tmp_path):
        ref = str(omp_sessions_dir(tmp_path) / "lazy.jsonl")
        validate_spawn_session_ref("omp", ref, mercury_home=tmp_path)
        assert not Path(ref).exists()  # still lazy — and still accepted

    def test_omp_ref_outside_home_fails_loud(self, tmp_path):
        with pytest.raises(RuntimeError, match="escapes this home"):
            validate_spawn_session_ref(
                "omp", str(tmp_path / "elsewhere" / "s.jsonl"),
                mercury_home=tmp_path / "home",
            )

    def test_omp_empty_ref_fails(self, tmp_path):
        with pytest.raises(RuntimeError, match="no session file"):
            validate_spawn_session_ref("omp", "", mercury_home=tmp_path)

    def test_fresh_hermes_id_passes_without_row(self, tmp_path):
        validate_spawn_session_ref("hermes", "sess-fresh", mercury_home=tmp_path)

    def test_hermes_empty_id_fails(self, tmp_path):
        with pytest.raises(RuntimeError, match="without a session id"):
            validate_spawn_session_ref("hermes", "", mercury_home=tmp_path)

    def test_unknown_engine_fails(self, tmp_path):
        with pytest.raises(ValueError, match="engine must be"):
            validate_spawn_session_ref("telegram", "x", mercury_home=tmp_path)

    def test_unwritable_parent_dir_fails(self, tmp_path):
        blocker = omp_sessions_dir(tmp_path) / "blocker"
        blocker.write_text("not a dir")
        with pytest.raises(RuntimeError, match="cannot be created"):
            validate_spawn_session_ref(
                "omp", str(blocker / "s.jsonl"),
                mercury_home=tmp_path,
            )


# ---------------------------------------------------------------------------
# intent journal round-trip
# ---------------------------------------------------------------------------


    def test_serialize_deserialize_round_trip(self):
        intents = (
            SendMessage("gw", "@merc_gw:x", "bye", "<b>bye</b>"),
            LeaveRoom("!room", "@owner:mercury.local"),
            DetachChild("!space", "!child", "@merc_gw:x"),
            PurgeRoom("!room"),
        )
        payload = serialize_intents(intents)
        assert [p["op"] for p in payload] == ["send", "leave", "detach", "purge"]
        assert deserialize_intents(payload) == intents

    def test_unknown_intent_rejected(self):
        with pytest.raises(TypeError):
            serialize_intents([object()])

    def test_deserialize_skips_unknown_op(self):
        assert deserialize_intents([{"op": "laser"}]) == ()


# ---------------------------------------------------------------------------
# /exit: D8 depth-0 cascade + atomic begin
# ---------------------------------------------------------------------------


def seed_orchestrator_with_children(state: ObservatoryState, engines_like="hermes"):
    """gw -> orch (0) -> sa (1) -> ssa (2), with matrix ids on every row."""
    def add(node_id, name, *, engine, parent):
        slug = assign_slug(name, state)
        row = state.add_node(
            node_id,
            engine=engine,
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug),
            session_ref=f"session:{node_id}",
            parent_node_id=parent,
            extra=None,  # plain agent nodes (tree convention — no kind)
        )
        state.set_space_id(node_id, f"!space-{node_id}")
        state.set_room_id(node_id, f"!room-{node_id}")
        return row

    add("orch", "auth-refactor", engine=engines_like, parent=None)
    add("sa", "test-sweep", engine="omp", parent="orch")
    add("ssa", "lint-fix", engine="omp", parent="sa")
    return "orch"


class TestExit:
    @pytest.mark.asyncio
    async def test_exit_purges_whole_subtree_and_drops_rows(
        self, state, registry, engines
    ):
        seed_orchestrator_with_children(state)
        registry.register(OrchestratorHandle(
            node_id="orch", engine="hermes", name="auth-refactor",
            session_ref="session:orch", agent=FakeHermesAgent("session:orch"),
        ))
        handle = registry.get("orch")
        agent = handle.agent  # stop() nulls the handle's engine ref
        client = FakeClient()
        out = await exit_orchestrator(
            "orch", state=state, registry=registry, renderer=make_renderer(state, client),
            summary="branch merged",
        )
        # whole subtree purged: every space + room of orch/sa/ssa
        assert purge_ids(client) == {
            "!room-orch", "!space-orch", "!room-sa", "!space-sa",
            "!room-ssa", "!space-ssa",
        }
        for node in ("orch", "sa", "ssa"):
            with pytest.raises(StateError):
                state.get(node)
        assert read_purge_journal(state) == []
        assert registry.get("orch") is None
        assert agent.closed  # engine killed after the durable mark
        assert out["deferred"] == []

    @pytest.mark.asyncio
    async def test_exit_rejects_nonzero_depth(self, state, registry):
        seed_orchestrator_with_children(state)
        with pytest.raises(ValueError, match="depth"):
            await exit_orchestrator(
                "sa", state=state, registry=registry,
                renderer=make_renderer(state),
            )
        # rejected before durability: nothing dead-marked, nothing journaled
        assert state.get("sa")["status"] == "live"
        assert read_purge_journal(state) == []

    @pytest.mark.asyncio
    async def test_begin_exit_is_one_atomic_commit(self, state):
        seed_orchestrator_with_children(state)
        commits: list[str] = []

        def trace(statement: str) -> None:
            if statement.split(None, 1)[0].upper() in (
                "BEGIN", "COMMIT", "ROLLBACK"
            ):
                commits.append(statement.split(None, 1)[0].upper())

        state._db.set_trace_callback(trace)
        try:
            record = begin_exit(state, "orch", renderer=make_renderer(state))
        finally:
            state._db.set_trace_callback(None)
        assert commits.count("COMMIT") == 1
        # journal entry + every subtree row dead in that one commit
        assert len(read_purge_journal(state)) == 1
        for node in ("orch", "sa", "ssa"):
            assert state.get(node)["status"] == "dead"
        assert record.node_id == "orch"

    @pytest.mark.asyncio
    async def test_replay_completes_journaled_exit_even_if_marked_live(self, state):
        # A journaled exit is dead-or-deleted in every observable state: even
        # if the rows are flipped back to 'live' under it, replay still purges.
        seed_orchestrator_with_children(state)
        record = begin_exit(state, "orch", renderer=make_renderer(state))
        state._db.execute("UPDATE nodes SET status='live' WHERE node_id='orch'")
        state._db.commit()
        client = FakeClient()
        deferred = await replay_purge_journal(state, executor=IntentExecutor(
            client, state, owner_mxid=OWNER, server_name=SERVER,
        ))
        assert deferred == []
        with pytest.raises(StateError):
            state.get("orch")
        assert record.node_id == "orch"

    @pytest.mark.asyncio
    async def test_replay_defers_on_persistent_purge_failure(self, state):
        seed_orchestrator_with_children(state)
        begin_exit(state, "orch", renderer=make_renderer(state))
        client = FakeClient()
        client.fail_purge["!room-sa"] = 500  # homeserver wedged
        deferred = await replay_purge_journal(state, executor=IntentExecutor(
            client, state, owner_mxid=OWNER, server_name=SERVER,
        ))
        assert len(deferred) == 1 and deferred[0]["node_id"] == "orch"
        # rows kept dead + journaled: retried next boot, never resumed
        assert state.get("orch")["status"] == "dead"
        assert len(read_purge_journal(state)) == 1
        # the homeserver heals -> next replay completes
        client.fail_purge.clear()
        deferred = await replay_purge_journal(state, executor=IntentExecutor(
            client, state, owner_mxid=OWNER, server_name=SERVER,
        ))
        assert deferred == []
        with pytest.raises(StateError):
            state.get("orch")

    @pytest.mark.asyncio
    async def test_replay_without_executor_defers(self, state):
        seed_orchestrator_with_children(state)
        begin_exit(state, "orch", renderer=make_renderer(state))
        deferred = await replay_purge_journal(state, executor=None)
        assert len(deferred) == 1
        assert state.get("orch")["status"] == "dead"

    @pytest.mark.asyncio
    async def test_finish_exit_is_atomic_single_commit(self, state):
        seed_orchestrator_with_children(state)
        record = begin_exit(state, "orch", renderer=make_renderer(state))
        commits: list[str] = []

        def trace(statement: str) -> None:
            if statement.split(None, 1)[0].upper() in (
                "BEGIN", "COMMIT", "ROLLBACK"
            ):
                commits.append(statement.split(None, 1)[0].upper())

        state._db.set_trace_callback(trace)
        try:
            finish_exit(state, record)
        finally:
            state._db.set_trace_callback(None)
        assert commits.count("COMMIT") == 1
        assert read_purge_journal(state) == []
        with pytest.raises(StateError):
            state.get("orch")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_stop_all_tears_down_every_engine(self):
        registry = OrchestratorRegistry()
        agent = FakeHermesAgent("s1")
        omp = FakeOmpChild("/tmp/x.jsonl")
        registry.register(OrchestratorHandle(
            node_id="a", engine="hermes", name="a", session_ref="s1", agent=agent))
        registry.register(OrchestratorHandle(
            node_id="b", engine="omp", name="b", session_ref="/tmp/x.jsonl", rpc=omp))
        registry.stop_all()
        assert agent.closed and omp.stopped
        assert registry.node_ids() == []

    def test_concurrent_register_unregister(self):
        registry = OrchestratorRegistry()
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for j in range(50):
                    nid = f"n-{i}-{j}"
                    registry.register(OrchestratorHandle(
                        node_id=nid, engine="hermes", name=nid, session_ref=nid))
                    registry.get(nid)
                    registry.unregister(nid)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert registry.node_ids() == []


# ---------------------------------------------------------------------------
# omp session-file helper
# ---------------------------------------------------------------------------


class TestOmpSessionFile:
    def test_reads_session_file_from_started_child(self):
        child = FakeOmpChild("/tmp/obs/sessions/a.jsonl")
        assert omp_session_file(child) == "/tmp/obs/sessions/a.jsonl"

    def test_unstarted_child_fails_hard(self):
        with pytest.raises(RuntimeError, match="not started"):
            omp_session_file(SimpleNamespace(_client=None))
