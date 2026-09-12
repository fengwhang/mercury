"""Spawned orchestrators answer in their own rooms (spawn-silent fix).

Regression cover for /spawn + /spawnomp creating the space+room while the
child never responded to plain messages or /commands: spawn one child,
route a Matrix message into its room through the real intake
(``SidecarDaemon._on_transaction`` — PL gate, control router, delivery),
and assert the engine turned and the reply rendered in the child's own
voice — with no restart in between.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

import observatory.sidecar_main as sm
from observatory.spawn import spawn_orchestrator

from tests.observatory.test_sidecar_main import FakeMatrixClient
from observatory.matrix_client import MatrixError

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHermesAgent:
    """Hermes child double: records turns, answers, interrupts."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.turns: list[str] = []
        self.interrupts: list[tuple] = []
        self.closed = False

    def run_conversation(self, text: str):
        self.turns.append(text)
        return {"final_response": f"echo:{text}"}

    def interrupt(self, message=None, **kwargs) -> None:
        self.interrupts.append((message, kwargs))

    def close(self) -> None:
        self.closed = True


class FakeOmpChild:
    """Omp child double: RPC surface subset used by child delivery."""

    def __init__(self, session_file: str):
        self.session_file = session_file
        self.prompts: list[str] = []
        self.steers: list[str] = []
        self.aborts: list = []
        self.stopped = False
        self._client = SimpleNamespace(
            get_state=lambda: SimpleNamespace(session_file=session_file)
        )

    def run_task(self, prompt: str, timeout=None):
        self.prompts.append(prompt)
        return {"status": "completed", "summary": f"omp-echo:{prompt}"}

    def steer(self, text: str) -> None:
        self.steers.append(text)

    def abort(self, reason=None) -> None:
        self.aborts.append(reason)

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    from observatory.provision import ObservatoryPaths

    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (
        paths.root,
        paths.bin_dir,
        paths.db_dir,
        paths.appservices_dir,
        paths.logs_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    paths.toml.write_text(
        '[global]\nserver_name = "mercury.local"\naddress = "127.0.0.1"\n'
        'port = 18008\ndatabase_path = "db"\nappservice_dir = "as"\n'
        "allow_federation = false\nallow_registration = false\n"
        'registration_token = "tok"\n',
        encoding="utf-8",
    )
    paths.appservice_registration.write_text(
        "id: merc-observatory\nurl: http://127.0.0.1:18090\n"
        'as_token: "as-tok"\nhs_token: "hs-tok"\n'
        "sender_localpart: merc-bot\nrate_limited: false\n"
        'namespaces:\n  users:\n    - regex: "^@merc_.*$"\n      exclusive: true\n',
        encoding="utf-8",
    )
    paths.owner_credentials.write_text(
        '{"homeserver_url": "http://127.0.0.1:18008", "user_id": "@owner:mercury.local",'
        ' "password": "pw", "access_token": "admin-tok", "device_id": "DEV"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sm.provision,
        "provision",
        lambda **kwargs: {
            "tuwunel": {
                "action": "current",
                "version": "v1.9.0",
                "binary": "x",
                "offline": True,
            }
        },
    )
    return home


@pytest.fixture()
def daemon(fake_home, monkeypatch):
    d = sm.SidecarDaemon(
        fake_home,
        hermes_db=fake_home / "hermes" / "state.db",
        appservice_port=_free_port(),
        e2ee=False,
    )
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


def _msg(room_id: str, body: str) -> dict:
    return {
        "type": "m.room.message",
        "event_id": "$ev1",
        "room_id": room_id,
        "sender": OWNER,
        "origin_server_ts": 1,
        "content": {"msgtype": "m.text", "body": body},
    }


async def _drain(d: sm.SidecarDaemon, timeout: float = 10.0) -> None:
    import time

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        pending = [t for t in list(d._gateway_tasks | d._child_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.sleep(0.05)
    pending = [t for t in list(d._gateway_tasks | d._child_tasks) if not t.done()]
    assert not pending, "child delivery task did not finish"


def _room_sends(d: sm.SidecarDaemon, room_id: str) -> list:
    return [c for c in d.client.calls if c[0] == "send" and c[1] == room_id]


@pytest.mark.asyncio
async def test_spawned_hermes_answers_plain_message(daemon: sm.SidecarDaemon):
    """Fresh hermes child answers the first message in its room (no restart)."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-1")
        row = await spawn_orchestrator(
            "alpha",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        assert room["room_id"] and room["space_id"]

        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "hello child")])
        await _drain(daemon)

        # Cold PL snapshot did not bounce the first message.
        assert "notice:power-levels-unavailable" not in daemon.routing_log
        assert daemon.routing_log[-1] == "steer"
        # The child's own session turned, and its reply rendered in its voice.
        assert agent.turns == ["hello child"]
        replies = [
            c
            for c in _room_sends(daemon, room["room_id"])
            if c[2] == "echo:hello child"
        ]
        assert len(replies) == 1
        assert replies[0][3] == room["mxid"]
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_spawned_hermes_accepts_slash_command(daemon: sm.SidecarDaemon):
    """An unknown /command in the child room falls through to a turn + reply."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-2")
        row = await spawn_orchestrator(
            "beta",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)

        await daemon._on_transaction(
            "tx-1", [_msg(room["room_id"], "/frobnicate well")]
        )
        await _drain(daemon)

        assert daemon.routing_log[-1] == "command"
        assert agent.turns == ["/frobnicate well"]
        assert any(
            c[2] == "echo:/frobnicate well" and c[3] == room["mxid"]
            for c in _room_sends(daemon, room["room_id"])
        )
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_spawned_omp_answers_while_idle(daemon: sm.SidecarDaemon):
    """Idle omp child takes the prompt path (not a void steer) and replies."""
    await daemon.boot()
    try:
        child = FakeOmpChild(str(daemon.mercury_home / "omp-sessions" / "s1.jsonl"))
        row = await spawn_orchestrator(
            "gamma",
            "omp",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            omp_child_factory=lambda: child,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        assert room["room_id"] and room["space_id"]

        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "hello omp")])
        await _drain(daemon)

        assert "notice:power-levels-unavailable" not in daemon.routing_log
        assert daemon.routing_log[-1] == "steer"
        assert child.prompts == ["hello omp"]
        assert child.steers == []
        assert any(
            c[2] == "omp-echo:hello omp" and c[3] == room["mxid"]
            for c in _room_sends(daemon, room["room_id"])
        )
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_spawned_omp_answers_slash_command(daemon: sm.SidecarDaemon):
    """A non-exit /command in an omp room goes over RPC and renders a reply."""
    await daemon.boot()
    try:
        child = FakeOmpChild(str(daemon.mercury_home / "omp-sessions" / "s2.jsonl"))
        row = await spawn_orchestrator(
            "delta",
            "omp",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            omp_child_factory=lambda: child,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)

        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "/help")])
        await _drain(daemon)

        assert daemon.routing_log[-1] == "command"
        assert child.prompts == ["/help"]
        assert any(
            c[2] == "omp-echo:/help" and c[3] == room["mxid"]
            for c in _room_sends(daemon, room["room_id"])
        )
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_spawned_children_obey_stop(daemon: sm.SidecarDaemon):
    """/stop in a child room interrupts/aborts and confirms in-room."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-3")
        hrow = await spawn_orchestrator(
            "eps",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        hroom = daemon.state.get(hrow["node_id"])
        child = FakeOmpChild(str(daemon.mercury_home / "omp-sessions" / "s3.jsonl"))
        orow = await spawn_orchestrator(
            "zeta",
            "omp",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            omp_child_factory=lambda: child,
        )
        oroom = daemon.state.get(orow["node_id"])
        await daemon._on_transaction("tx-1", [_msg(hroom["room_id"], "/stop")])
        await daemon._on_transaction("tx-2", [_msg(oroom["room_id"], "/stop")])
        await _drain(daemon)

        # Hermes child: interrupted + confirmed in its own voice.
        assert agent.interrupts, "hermes child was never interrupted"
        assert any(
            "stop confirmed" in c[2] and c[3] == hroom["mxid"]
            for c in _room_sends(daemon, hroom["room_id"])
        )
        # Idle omp child: nothing to stop (router stop-idle, no abort).
        assert child.aborts == []
        assert any(
            "idle" in c[2] and c[3] == oroom["mxid"]
            for c in _room_sends(daemon, oroom["room_id"])
        )
        # Busy omp child: abort over RPC + confirmed in-room.
        daemon._child_busy.add(orow["node_id"])
        await daemon._on_transaction("tx-3", [_msg(oroom["room_id"], "/stop please")])
        await _drain(daemon)
        daemon._child_busy.discard(orow["node_id"])
        assert child.aborts == ["please"]
        assert any(
            "stop confirmed" in c[2] and c[3] == oroom["mxid"]
            for c in _room_sends(daemon, oroom["room_id"])
        )
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_spawn_scoping_stays_gateway_only(daemon: sm.SidecarDaemon):
    """/spawn from a child room is still refused (D13 scope gate)."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-4")
        row = await spawn_orchestrator(
            "eta",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        room = daemon.state.get(row["node_id"])

        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "/spawn sneaky")])
        await _drain(daemon)

        assert daemon.routing_log[-1] == "notice:scope-gate"
        assert agent.turns == []
        assert any(
            "gateway-lifecycle" in c[2] for c in _room_sends(daemon, room["room_id"])
        )
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_missing_handle_resumes_on_demand(daemon: sm.SidecarDaemon, monkeypatch):
    """A live state row with no registry handle still answers (adopt gap)."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-5")
        row = await spawn_orchestrator(
            "theta",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        # Materialized row (a completed turn already happened): the on-demand
        # path is a cold resume, not the spawn-fresh path (see
        # test_spawn_race.py for the never-materialized law).
        daemon.state.update_extra(node_id, session_materialized=True)
        # Simulate the adopt gap: the row is live but no process holds a handle.
        daemon.registry.unregister(node_id)
        resumed = FakeHermesAgent("sess-5")
        monkeypatch.setattr(
            "observatory.respawn.resume_hermes_orchestrator",
            lambda r, **kw: resumed,
        )

        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "hello again")])
        await _drain(daemon)

        assert resumed.turns == ["hello again"]
        assert daemon.registry.get(node_id) is not None
        assert any(
            c[2] == "echo:hello again" and c[3] == room["mxid"]
            for c in _room_sends(daemon, room["room_id"])
        )
    finally:
        await daemon.shutdown()
# ---------------------------------------------------------------------------
# Spawn-ghost regressions (2026-09-11 VM incident: /spawnomp + /spawn rooms
# went silent — unregistered ghosts 400 every child-voice E2EE send, the
# queued-steer notice crash vetoed the engine turn, and dangling
# session_refs resumed to silent CHILD_UNAVAILABLE).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spawn_registers_ghost_before_converge(daemon: sm.SidecarDaemon):
    """Register-then-converge: the minted ghost registers BEFORE createRoom."""
    await daemon.boot()
    try:
        mark = len(daemon.client.calls)
        agent = FakeHermesAgent("sess-reg")
        row = await spawn_orchestrator(
            "reggie",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        room = daemon.state.get(row["node_id"])
        localpart = room["mxid"].lstrip("@").split(":", 1)[0]
        spawned = daemon.client.calls[mark:]
        kinds = [c[0] for c in spawned]
        assert "register" in kinds and "create_room" in kinds
        assert kinds.index("register") < kinds.index("create_room")
        assert ("register", localpart) in spawned
        out = await daemon.client.client_api(
            "POST",
            "/_matrix/client/v3/login",
            json_body={
                "type": "m.login.application_service",
                "identifier": {"type": "m.id.user", "user": room["mxid"]},
            },
        )
        assert "event_id" in out
    finally:
        await daemon.shutdown()
@pytest.mark.asyncio
async def test_unregistered_ghost_login_400s():
    """The fake holds the tuwunel law: AS-login for an unknown ghost 400s."""
    client = FakeMatrixClient()
    with pytest.raises(MatrixError) as excinfo:
        await client.client_api(
            "POST",
            "/_matrix/client/v3/login",
            json_body={
                "type": "m.login.application_service",
                "identifier": {"type": "m.id.user", "user": "@merc_nobody:mercury.local"},
            },
        )
    assert excinfo.value.status == 400
    assert excinfo.value.errcode == "M_INVALID_PARAM"
@pytest.mark.asyncio
async def test_notice_failure_does_not_veto_child_turn(daemon: sm.SidecarDaemon, monkeypatch):
    """Decoupling: a child-voice notice crash still delivers the engine turn.

    Mid-turn steer (hermes turn lock held, so the router reports busy and
    emits the queued-steer notice): the child-voice send crashes, the
    gateway-voice fallback still tells the room, and the engine turn runs.
    Idle new turns post no queued notice at all (router fix) — this test
    pins the busy path where the notice exists to fail over.
    """
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-decouple")
        row = await spawn_orchestrator(
            "decouple",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        room = daemon.state.get(row["node_id"])
        real_execute = daemon.renderer.executor.execute
        attempted = {"n": 0}
        async def flaky(intents):
            intents = list(intents)
            if (
                not attempted["n"]
                and len(intents) == 1
                and getattr(intents[0], "sender", "") == room["mxid"]
                and "queued steer" in str(getattr(intents[0], "body", ""))
            ):
                attempted["n"] += 1
                raise MatrixError(
                    "POST",
                    "/_matrix/client/v3/login",
                    400,
                    {"errcode": "M_INVALID_PARAM", "error": "Called create_device for non-existent user"},
                )
            return await real_execute(intents)
        monkeypatch.setattr(daemon.renderer.executor, "execute", flaky)
        node_id = row["node_id"]
        lock = daemon._child_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            daemon._child_locks[node_id] = lock
        await lock.acquire()
        try:
            await daemon._on_transaction("tx-1", [_msg(room["room_id"], "hello child")])
        finally:
            lock.release()
        await _drain(daemon)
        assert attempted["n"] == 1, "flaky child-voice notice was never attempted"
        assert agent.turns == ["hello child"], "notice crash vetoed the engine turn"
        assert any(
            c[2] == "echo:hello child" and c[3] == room["mxid"]
            for c in _room_sends(daemon, room["room_id"])
        )
        assert any(
            "queued steer" in c[2] and c[3] == daemon.gateway_mxid
            for c in _room_sends(daemon, room["room_id"])
        )
    finally:
        await daemon.shutdown()
@pytest.mark.asyncio
async def test_lazy_omp_session_passes_spawn_then_fails_resume(daemon: sm.SidecarDaemon):
    """Lazy law: a not-yet-written omp JSONL PASSES spawn validation
    (the file only materializes after the first assistant message). A
    spawn-fresh ref reports never-materialized at RESUME time (run live,
    never the deleted error); only a previously-materialized ref reports
    deletion — never a loud spawn refusal either way."""
    from observatory.spawn import omp_sessions_dir

    await daemon.boot()
    try:
        live_before = {r["node_id"] for r in daemon.state.get_live()}
        lazy_ref = str(omp_sessions_dir(daemon.mercury_home) / "lazy-not-yet-written.jsonl")
        child = FakeOmpChild(lazy_ref)
        row = await spawn_orchestrator(
            "lazy",
            "omp",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            mercury_home=daemon.mercury_home,
            omp_child_factory=lambda: child,
            validate_session_ref=True,
        )
        assert row["session_ref"] == lazy_ref
        assert {r["node_id"] for r in daemon.state.get_live()} == live_before | {row["node_id"]}
        assert not child.stopped  # no teardown on a passing gate
        # Spawn-fresh resume reports never-materialized (not deletion).
        from observatory.respawn import restart_omp_orchestrator

        with pytest.raises(RuntimeError, match="never materialized"):
            restart_omp_orchestrator(daemon.state.get(row["node_id"]))
        # Previously-materialized refs keep the deletion error.
        daemon.state.update_extra(row["node_id"], session_materialized=True)
        with pytest.raises(RuntimeError, match="is gone"):
            restart_omp_orchestrator(daemon.state.get(row["node_id"]))
    finally:
        await daemon.shutdown()
@pytest.mark.asyncio
async def test_fresh_hermes_session_passes_spawn_without_row(daemon: sm.SidecarDaemon):
    """Same laziness hermes-side: the SessionDB row is created on the
    first turn, so a fresh session id PASSES spawn validation."""
    await daemon.boot()
    try:
        live_before = {r["node_id"] for r in daemon.state.get_live()}
        agent = FakeHermesAgent("sess-fresh-zzz")
        row = await spawn_orchestrator(
            "fresh",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            mercury_home=daemon.mercury_home,
            agent_factory=lambda: agent,
            validate_session_ref=True,
        )
        assert row["session_ref"] == "sess-fresh-zzz"
        assert {r["node_id"] for r in daemon.state.get_live()} == live_before | {row["node_id"]}
        assert not agent.closed
    finally:
        await daemon.shutdown()
@pytest.mark.asyncio
async def test_omp_ref_outside_home_fails_loud_at_spawn(daemon: sm.SidecarDaemon, tmp_path):
    """A session file escaping this home's omp-sessions dir (built under
    another home) still fails LOUD at spawn — the daemon could never
    resume it."""
    await daemon.boot()
    try:
        live_before = {r["node_id"] for r in daemon.state.get_live()}
        child = FakeOmpChild(str(tmp_path / "elsewhere" / "s.jsonl"))
        with pytest.raises(RuntimeError, match="escapes this home"):
            await spawn_orchestrator(
                "stray",
                "omp",
                server_name=SERVER,
                state=daemon.state,
                registry=daemon.registry,
                renderer=daemon.renderer,
                mercury_home=daemon.mercury_home,
                omp_child_factory=lambda: child,
                validate_session_ref=True,
            )
        assert child.stopped  # dangling child torn down, never leaked
        assert {r["node_id"] for r in daemon.state.get_live()} == live_before
    finally:
        await daemon.shutdown()
@pytest.mark.asyncio
async def test_dangling_resume_surfaces_operator_visible_error(daemon: sm.SidecarDaemon):
    """A live MATERIALIZED row whose session is gone logs child-resume-failed
    and still tells the room (session-unavailable) — never silence, and
    never a false 'queued steer' alongside it. (Spawn-fresh rows take the
    fresh-handle path instead — see test_spawn_race.py.)"""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-gone-zzz")
        row = await spawn_orchestrator(
            "gone",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        daemon.state.update_extra(node_id, session_materialized=True)
        daemon.registry.unregister(node_id)
        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "hello?")])
        await _drain(daemon)
        assert f"child-resume-failed:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert any("unavailable" in c[2] for c in sends)
        assert not any("queued steer" in c[2] for c in sends)
    finally:
        await daemon.shutdown()
# ---------------------------------------------------------------------------
# Child-lost regressions: post-boot spawns answer without a restart.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_boot_spawn_visible_without_restart(daemon: sm.SidecarDaemon, monkeypatch):
    """A spawn landing in the gateway-thread boot registry AFTER the
    daemon's boot adopt is still served: the daemon adopts the live
    handle on first use (no restart), with no unavailable notice."""
    from observatory import platform_hook
    from observatory.spawn import OrchestratorRegistry

    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-postboot")
        gateway_registry = OrchestratorRegistry()
        row = await spawn_orchestrator(
            "postboot",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=gateway_registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        assert daemon.registry.get(node_id) is None  # boot adopt cannot see it
        monkeypatch.setattr(
            platform_hook, "LAST_BOOT",
            SimpleNamespace(registry=gateway_registry, mercury_home=str(daemon.mercury_home)),
        )
        await daemon._on_transaction("tx-1", [_msg(room["room_id"], "hello postboot")])
        await _drain(daemon)
        assert agent.turns == ["hello postboot"]
        assert daemon.registry.get(node_id) is not None
        assert f"child-adopted:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert any(c[2] == "echo:hello postboot" for c in sends)
        assert not any("unavailable" in c[2] for c in sends)
    finally:
        await daemon.shutdown()
@pytest.mark.asyncio
async def test_steer_working_child_never_posts_unavailable(daemon: sm.SidecarDaemon):
    """Steering a WORKING (busy) child posts queued-steer and steers —
    never the session-unavailable notice alongside it."""
    await daemon.boot()
    try:
        child = FakeOmpChild(str(daemon.mercury_home / "omp-sessions" / "working.jsonl"))
        row = await spawn_orchestrator(
            "working",
            "omp",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            mercury_home=daemon.mercury_home,
            omp_child_factory=lambda: child,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        daemon._child_busy.add(node_id)  # mid-turn: the next message steers
        try:
            await daemon._on_transaction("tx-1", [_msg(room["room_id"], "keep going")])
        finally:
            daemon._child_busy.discard(node_id)
        assert child.steers == ["keep going"]
        sends = _room_sends(daemon, room["room_id"])
        assert any("queued steer" in c[2] for c in sends)
        assert not any("unavailable" in c[2] for c in sends)
    finally:
        await daemon.shutdown()
def test_registry_hit_attaches_omp_feed(daemon: sm.SidecarDaemon):
    """Same-process registry hits (adopted at boot) still get their omp
    feed — otherwise tool calls and thinking never stream."""
    from observatory.spawn import OrchestratorHandle, OrchestratorRegistry

    daemon.registry = OrchestratorRegistry()
    child = FakeOmpChild(str(daemon.mercury_home / "omp-sessions" / "fed.jsonl"))
    handle = OrchestratorHandle(
        node_id="orch-fed", engine="omp", name="fed",
        session_ref=child.session_file, rpc=child,
    )
    daemon.registry.register(handle)
    assert daemon._child_handle("orch-fed") is handle
    assert "orch-fed" in daemon.omp_feeds
def test_unavailable_notice_needs_no_restart():
    """The unavailable notice never orders a sidecar restart the user
    should never need — it says retry."""
    assert "restart the sidecar" not in sm.CHILD_UNAVAILABLE_NOTICE
    assert "retry" in sm.CHILD_UNAVAILABLE_NOTICE


async def _seed_orch_with_child(daemon, *, engine, handle):
    """Spawned orch (registered handle) + live delegation child row."""
    from observatory.identity import assign_slug, virtual_mxid

    row = await spawn_orchestrator(
        "carlos",
        engine,
        server_name=SERVER,
        state=daemon.state,
        registry=daemon.registry,
        renderer=daemon.renderer,
        mercury_home=daemon.mercury_home,
        **handle,
    )
    orch_id = row["node_id"]
    slug = assign_slug("test-sweep", daemon.state)
    daemon.state.add_node(
        "del-1", engine="hermes", name="test-sweep", slug=slug,
        mxid=virtual_mxid(slug, server_name=SERVER),
        session_ref="delegation:del-1", parent_node_id=orch_id,
        extra={"delegation_id": "del-1", "task_index": 0},
    )
    return orch_id


@pytest.mark.asyncio
async def test_parent_resume_injects_into_hermes_orchestrator(daemon: sm.SidecarDaemon):
    """Child death under a spawned hermes orch continues the parent's
    turn: the summary injects as a steer and the reply lands in the
    parent's room."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-orch")
        orch_id = await _seed_orch_with_child(
            daemon, engine="hermes", handle={"agent_factory": lambda: agent})
        orch_room = daemon.state.get(orch_id)["room_id"]
        await daemon._maybe_post_delegate_followup(
            "del-1", orch_id, "test-sweep",
            status="failed", summary="error: tests failed, approval needed",
        )
        await _drain(daemon)
        assert len(agent.turns) == 1
        assert agent.turns[0].startswith("[subagent test-sweep failed]")
        assert f"parent-followup:{orch_id}" in daemon.routing_log
        assert any(
            c[2].startswith("echo:[subagent test-sweep failed]")
            for c in _room_sends(daemon, orch_room)
        )
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_parent_resume_prompts_idle_omp_orchestrator(daemon: sm.SidecarDaemon):
    """Idle omp orch parent takes the summary as a new prompt turn."""
    from observatory.spawn import omp_sessions_dir

    await daemon.boot()
    try:
        ref = str(omp_sessions_dir(daemon.mercury_home) / "orch-parent.jsonl")
        child = FakeOmpChild(ref)
        orch_id = await _seed_orch_with_child(
            daemon, engine="omp", handle={"omp_child_factory": lambda: child})
        await daemon._maybe_post_delegate_followup(
            "del-1", orch_id, "test-sweep",
            status="failed", summary="error: build failed, fix needed",
        )
        await _drain(daemon)
        assert len(child.prompts) == 1
        assert child.prompts[0].startswith("[subagent test-sweep failed]")
        assert f"parent-followup:{orch_id}" in daemon.routing_log
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_parent_resume_steers_busy_omp_orchestrator(daemon: sm.SidecarDaemon):
    """Busy omp orch parent takes the summary as a steer, not a turn."""
    from observatory.spawn import omp_sessions_dir

    await daemon.boot()
    try:
        ref = str(omp_sessions_dir(daemon.mercury_home) / "orch-busy.jsonl")
        child = FakeOmpChild(ref)
        orch_id = await _seed_orch_with_child(
            daemon, engine="omp", handle={"omp_child_factory": lambda: child})
        daemon._child_busy.add(orch_id)
        try:
            await daemon._maybe_post_delegate_followup(
                "del-1", orch_id, "test-sweep",
                status="failed", summary="error: build failed, fix needed",
            )
        finally:
            daemon._child_busy.discard(orch_id)
        assert child.steers and child.steers[0].startswith("[subagent test-sweep failed]")
        assert not child.prompts
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_routine_child_death_continues_parent_quietly(daemon: sm.SidecarDaemon):
    """Routine-success child death still continues the orch parent —
    quietly (turn runs, room stays silent)."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-quiet-orch")
        orch_id = await _seed_orch_with_child(
            daemon, engine="hermes", handle={"agent_factory": lambda: agent})
        orch_room = daemon.state.get(orch_id)["room_id"]
        sends_before = len(_room_sends(daemon, orch_room))
        await daemon._maybe_post_delegate_followup(
            "del-1", orch_id, "test-sweep",
            status="completed", summary="tests passed",
        )
        await _drain(daemon)
        assert len(agent.turns) == 1
        assert agent.turns[0].startswith("[subagent test-sweep completed]")
        assert f"parent-followup:{orch_id}" in daemon.routing_log
        assert len(_room_sends(daemon, orch_room)) == sends_before
    finally:
        await daemon.shutdown()

@pytest.mark.asyncio
async def test_grandchild_death_walks_up_to_orchestrator(daemon: sm.SidecarDaemon):
    """A depth-2 death resolves past its delegation parent to the live
    orchestrator holding the engine handle."""
    from observatory.identity import assign_slug, virtual_mxid

    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-walkup")
        orch_id = await _seed_orch_with_child(
            daemon, engine="hermes", handle={"agent_factory": lambda: agent})
        slug = assign_slug("lint", daemon.state)
        daemon.state.add_node(
            "del-1/0", engine="hermes", name="lint", slug=slug,
            mxid=virtual_mxid(slug, server_name=SERVER),
            session_ref="delegation:del-1/0", parent_node_id="del-1",
            extra={"delegation_id": "del-1", "task_index": 1},
        )
        await daemon._maybe_post_delegate_followup(
            "del-1/0", "del-1", "lint",
            status="failed", summary="error: lint failed, fix needed",
        )
        await _drain(daemon)
        assert len(agent.turns) == 1
        assert agent.turns[0].startswith("[subagent lint failed]")
    finally:
        await daemon.shutdown()


class FakeRedirectHermesAgent(FakeHermesAgent):
    """Hermes child double with a CLI-style live-request redirect surface."""

    def __init__(self, session_id: str, *, redirect_ok: bool = True):
        super().__init__(session_id)
        self.redirects: list[str] = []
        self.steers: list[str] = []
        self._redirect_ok = redirect_ok

    def redirect(self, text: str):
        self.redirects.append(text)
        return self._redirect_ok

    def steer(self, text: str):
        self.steers.append(text)
        return True


def test_redirect_helper_falls_back_without_surface():
    """No redirect surface (legacy agent) or a declined redirect reads as
    miss — the caller queues a fresh turn so nothing is lost."""
    assert sm.SidecarDaemon._redirect_live_hermes_child(FakeHermesAgent("s"), "hi") is False
    assert sm.SidecarDaemon._redirect_live_hermes_child(object(), "hi") is False
    assert sm.SidecarDaemon._redirect_live_hermes_child(
        FakeRedirectHermesAgent("s", redirect_ok=False), "hi") is False
    assert sm.SidecarDaemon._redirect_live_hermes_child(
        FakeRedirectHermesAgent("s"), "hi") is True


@pytest.mark.asyncio
async def test_midturn_hermes_steer_redirects_live_turn(daemon: sm.SidecarDaemon):
    """Mid-turn hermes steer redirects the live turn — no second turn, no
    extra room render. The live turn's own reply still renders normally."""
    await daemon.boot()
    try:
        agent = FakeRedirectHermesAgent("sess-redirect")
        row = await spawn_orchestrator(
            "redirector",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        lock = daemon._child_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            daemon._child_locks[node_id] = lock
        await lock.acquire()  # a live turn owns the agent
        try:
            assert await daemon._run_hermes_child_turn(node_id, "turn left") is True
        finally:
            lock.release()
        assert agent.redirects == ["turn left"]
        assert agent.turns == [], "mid-turn steer queued a second turn"
        assert agent.steers == [], "redirect absorbed the steer — no double delivery"
        assert not [c for c in _room_sends(daemon, daemon.state.get(node_id)["room_id"])
                    if "turn left" in c[2]], "steer text rendered as a reply"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_quiet_continuation_takes_fresh_turn_during_live_child(
    daemon: sm.SidecarDaemon,
):
    """Quiet parent-continuations never steer: even with a live turn and a
    redirect surface they queue a fresh turn and stay silent in-room."""
    await daemon.boot()
    try:
        agent = FakeRedirectHermesAgent("sess-quietlive")
        row = await spawn_orchestrator(
            "quietlive",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room_id = daemon.state.get(node_id)["room_id"]
        lock = daemon._child_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            daemon._child_locks[node_id] = lock
        await lock.acquire()  # a live turn owns the agent
        task = asyncio.create_task(
            daemon._run_hermes_child_turn(node_id, "sibling finished: ok", quiet=True)
        )
        try:
            await asyncio.sleep(0.05)
            assert not task.done(), "quiet turn must wait for the live turn"
            assert agent.redirects == [], "quiet continuation must never steer"
        finally:
            lock.release()
        await asyncio.wait_for(task, timeout=10.0)
        assert task.result() is True
        assert agent.turns == ["sibling finished: ok"]
        assert not [c for c in _room_sends(daemon, room_id)
                    if "sibling finished: ok" in c[2]], "quiet turn must stay silent"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_member_voice_outage_still_renders_child_turn(
    daemon: sm.SidecarDaemon, monkeypatch
):
    """Ghost-not-member everywhere: the members read degrades to empty and
    the child turn still runs with its reply rendered — never dies silent."""
    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-outage")
        row = await spawn_orchestrator(
            "outage",
            "hermes",
            server_name=SERVER,
            state=daemon.state,
            registry=daemon.registry,
            renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        room = daemon.state.get(row["node_id"])
        real_client_api = daemon.client.client_api

        async def _no_members(method, path, *, sender=None, params=None, json_body=None):
            if "/members" in str(path):
                raise MatrixError(
                    "GET", path, 403, {"errcode": "M_FORBIDDEN", "error": "not a member"})
            return await real_client_api(
                method, path, sender=sender, params=params, json_body=json_body)

        monkeypatch.setattr(daemon.client, "client_api", _no_members)
        assert await daemon._room_members(room["room_id"]) == []
        await daemon._on_transaction("tx-outage", [_msg(room["room_id"], "hello child")])
        await _drain(daemon)
        assert agent.turns == ["hello child"], "member-voice outage vetoed the engine turn"
        assert any(
            c[2] == "echo:hello child" and c[3] == room["mxid"]
            for c in _room_sends(daemon, room["room_id"])
        ), "child reply never rendered during member-voice outage"
    finally:
        await daemon.shutdown()
