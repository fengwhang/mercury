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
