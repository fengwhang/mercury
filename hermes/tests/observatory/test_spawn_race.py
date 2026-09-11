"""Spawn-race + gateway-parent + gateway mid-turn steer regressions.

VM 2026-09-11 (v0.0.40): the first message to a freshly-spawned depth-0
orchestrator ALWAYS dropped with CHILD_UNAVAILABLE_NOTICE on both engines —
the lazy session file/row never materialized before the first turn, the
cross-process adopt missed (gateway vs sidecar registries never share an
object), and cold resume falsely reported "(deleted without /exit?)".
Gateway-room mid-turn steering had the same shape: the steer verb raced the
agent build window and fell back to a fresh turn blocked on the session lock.

Laws pinned here:
- spawn-then-first-prompt runs (fresh handle, no unavailable notice) even
  when the lazy file/row is absent and no registry handle is held;
- never-materialized vs deleted resume errors are distinct (the deleted
  message never fires for a spawn-fresh ref);
- child death under the gateway resumes the gateway parent — idle (prompt)
  and busy (steer into the running turn);
- gateway mid-turn text steers into the in-flight turn (real
  gateway_session registry, not a faked steer verb); idle text prompts.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import observatory.sidecar_main as sm
from observatory import gateway_session as gs
from observatory.control import QUEUED_STEER_NOTICE, ControlNotice, InjectText
from observatory.gateway_transport import GatewayTransport
from observatory.spawn import spawn_orchestrator
from tests.observatory.test_sidecar_main import FakeMatrixClient

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHermesAgent:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.turns: list[str] = []
        self.closed = False

    def run_conversation(self, text: str):
        self.turns.append(text)
        return {"final_response": f"echo:{text}"}

    def close(self) -> None:
        self.closed = True


class FakeOmpChild:
    def __init__(self, session_file: str):
        self.session_file = session_file
        self.prompts: list[str] = []
        self.steers: list[str] = []
        self.stopped = False
        self._client = SimpleNamespace(
            get_state=lambda: SimpleNamespace(session_file=session_file)
        )

    def run_task(self, prompt: str, timeout=None):
        self.prompts.append(prompt)
        return {"status": "completed", "summary": f"omp-echo:{prompt}"}

    def steer(self, text: str) -> None:
        self.steers.append(text)

    def stop(self) -> None:
        self.stopped = True


class FakeSteerAgent:
    """Real-steer-shape agent for the gateway_session registry."""

    def __init__(self):
        self.steers: list[str] = []
        self.interrupts: list = []

    def steer(self, text: str):
        self.steers.append(text)
        return True

    def interrupt(self, reason=None, **kwargs):
        self.interrupts.append((reason, kwargs))


class FakeGatewayTransport(GatewayTransport):
    def __init__(self, reply: str = "ok", events=None):
        self.reply = reply
        self.events = list(events or [])
        self.prompts: list = []

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw",
                                 room_id=None, internal=False):
        self.prompts.append((text, kind, node_id, internal))
        return self.reply, list(self.events)

    async def steer(self, text: str, *, node_id: str = "gw"):
        return {"steered": False, "reason": "no steer surface"}

    async def interrupt(self, reason: str = "matrix /stop"):
        return {"interrupted": False, "reason": "idle"}


class FakeSteerTransport(GatewayTransport):
    def __init__(self, reply: str = "ok", *, steer_ok: bool = True, slow: bool = False):
        self.reply = reply
        self.steer_ok = steer_ok
        self.slow = slow
        self.prompts: list = []
        self.steers: list = []
        self.interrupts: list = []

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw",
                                 room_id=None, internal=False):
        self.prompts.append((text, kind, node_id))
        if self.slow:
            await asyncio.sleep(30)
        return self.reply, []

    async def steer(self, text: str, *, node_id: str = "gw"):
        self.steers.append((text, node_id))
        if self.steer_ok:
            return {"steered": True, "reason": ""}
        return {"steered": False, "reason": "miss"}

    async def interrupt(self, reason: str = "matrix /stop"):
        self.interrupts.append(reason)
        return {"interrupted": True, "reason": reason}


class RealSteerTransport(GatewayTransport):
    """Steer verb hits the real gateway_session registry; prompt is faked."""

    def __init__(self, reply: str = "late", *, slow: bool = False):
        self.reply = reply
        self.slow = slow
        self.prompts: list = []

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw",
                                 room_id=None, internal=False):
        self.prompts.append((text, kind, node_id))
        if self.slow:
            await asyncio.sleep(30)
        return self.reply, []

    async def steer(self, text: str, *, node_id: str = "gw"):
        return gs.steer_gateway_agent(text)

    async def interrupt(self, reason: str = "matrix /stop"):
        return gs.interrupt_gateway_agent(reason)


@pytest.fixture(autouse=True)
def _clean_gateway_registry():
    gs._session_agents.clear()
    gs._session_locks.clear()
    try:
        yield
    finally:
        gs._session_agents.clear()
        gs._session_locks.clear()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    from observatory.provision import ObservatoryPaths

    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir,
              paths.logs_dir):
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
        sm.provision, "provision",
        lambda **kwargs: {"tuwunel": {"action": "current", "version": "v1.9.0",
                                      "binary": "x", "offline": True}},
    )
    return home


@pytest.fixture()
def daemon(fake_home, monkeypatch) -> sm.SidecarDaemon:
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
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        pending = [t for t in list(d._gateway_tasks | d._child_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.sleep(0.05)
    pending = [t for t in list(d._gateway_tasks | d._child_tasks) if not t.done()]
    assert not pending, "delivery task did not finish"


def _room_sends(d: sm.SidecarDaemon, room_id: str) -> list:
    return [c for c in d.client.calls if c[0] == "send" and c[1] == room_id]


# --- spawn-then-first-prompt uses the live/fresh handle -----------------------


@pytest.mark.asyncio
async def test_spawn_race_hermes_first_prompt_runs_without_row(
    daemon: sm.SidecarDaemon, monkeypatch,
):
    """Cross-process race: gateway holds the live handle, this daemon holds
    none, and the hermes SessionDB row never materialized — the first prompt
    still runs (fresh handle on the same ref), never CHILD_UNAVAILABLE."""
    import observatory.spawn as spawn_mod

    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-race-h")
        row = await spawn_orchestrator(
            "race-h", "hermes", server_name=SERVER, state=daemon.state,
            registry=daemon.registry, renderer=daemon.renderer,
            agent_factory=lambda: agent,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        # Cross-process gap: the live handle lives gateway-side; this daemon
        # sees the row but no handle. The lazy SessionDB row is absent by
        # construction (fresh id, no turn has run anywhere in this process).
        daemon.registry.unregister(node_id)
        fresh = FakeHermesAgent("sess-race-h")
        monkeypatch.setattr(spawn_mod, "build_hermes_agent", lambda **kw: fresh)

        await daemon._on_transaction("tx-race-h", [_msg(room["room_id"], "hello race")])
        await _drain(daemon)

        assert fresh.turns == ["hello race"]
        assert daemon.registry.get(node_id) is not None
        assert f"child-fresh:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert any(c[2] == "echo:hello race" for c in sends)
        assert not any("unavailable" in c[2] for c in sends)
        # First completed turn materializes the ref for future resumes.
        assert daemon.state.get(node_id)["extra"].get("session_materialized") is True
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_spawn_race_omp_first_prompt_runs_without_file(
    daemon: sm.SidecarDaemon, monkeypatch,
):
    """Same race omp-side: the allocated JSONL never hit disk, no handle is
    held here — the first prompt still runs, never CHILD_UNAVAILABLE."""
    import observatory.spawn as spawn_mod

    await daemon.boot()
    try:
        ref = str(daemon.mercury_home / "observatory" / "omp-sessions" / "race-o.jsonl")
        child = FakeOmpChild(ref)
        row = await spawn_orchestrator(
            "race-o", "omp", server_name=SERVER, state=daemon.state,
            registry=daemon.registry, renderer=daemon.renderer,
            mercury_home=daemon.mercury_home,
            omp_child_factory=lambda: child,
        )
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        assert not Path(ref).exists()  # lazy: nothing on disk before turn one
        daemon.registry.unregister(node_id)
        fresh = FakeOmpChild(ref)
        monkeypatch.setattr(
            spawn_mod, "build_omp_child", lambda **kw: fresh
        )

        await daemon._on_transaction("tx-race-o", [_msg(room["room_id"], "hello omp race")])
        await _drain(daemon)

        assert fresh.prompts == ["hello omp race"]
        assert daemon.registry.get(node_id) is not None
        assert f"child-fresh:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert any("omp-echo:hello omp race" in c[2] for c in sends)
        assert not any("unavailable" in c[2] for c in sends)
    finally:
        await daemon.shutdown()


# --- never-materialized vs deleted are distinct --------------------------------


def test_resume_hermes_never_materialized_message(tmp_path: Path):
    from observatory.respawn import resume_hermes_orchestrator
    from observatory.state import ObservatoryState
    from observatory.identity import assign_slug, virtual_mxid

    state = ObservatoryState(tmp_path / "state.db")
    try:
        slug = assign_slug("race-h", state)
        state.add_node(
            "orch-h", engine="hermes", name="race-h", slug=slug,
            mxid=virtual_mxid(slug, server_name=SERVER),
            session_ref="sess-never-h",
            extra={"session_materialized": False},
        )
        row = state.get("orch-h")
        (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
        with pytest.raises(RuntimeError, match="never materialized"):
            resume_hermes_orchestrator(row, mercury_home=tmp_path)
        with pytest.raises(RuntimeError) as excinfo:
            resume_hermes_orchestrator(row, mercury_home=tmp_path)
        assert "deleted without /exit?" not in str(excinfo.value)
    finally:
        state.close()


def test_resume_hermes_deleted_message_stands(tmp_path: Path, monkeypatch):
    import observatory.respawn as respawn_mod
    from observatory.respawn import resume_hermes_orchestrator
    from observatory.state import ObservatoryState
    from observatory.identity import assign_slug, virtual_mxid

    state = ObservatoryState(tmp_path / "state.db")
    try:
        slug = assign_slug("old-h", state)
        state.add_node(
            "orch-old", engine="hermes", name="old-h", slug=slug,
            mxid=virtual_mxid(slug, server_name=SERVER),
            session_ref="sess-deleted-h",
            extra={"session_materialized": True},
        )
        row = state.get("orch-old")
        (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            respawn_mod, "build_hermes_agent",
            lambda **kw: pytest.fail("deleted ref must not build"),
        )
        with pytest.raises(RuntimeError, match=r"deleted without /exit\?"):
            resume_hermes_orchestrator(row, mercury_home=tmp_path)
    finally:
        state.close()


def test_restart_omp_never_materialized_vs_deleted(tmp_path: Path):
    from observatory.respawn import restart_omp_orchestrator
    from observatory.state import ObservatoryState
    from observatory.identity import assign_slug, virtual_mxid

    state = ObservatoryState(tmp_path / "state.db")
    try:
        missing = str(tmp_path / "gone.jsonl")
        slug = assign_slug("race-o", state)
        state.add_node(
            "orch-o", engine="omp", name="race-o", slug=slug,
            mxid=virtual_mxid(slug, server_name=SERVER),
            session_ref=missing, extra={"session_materialized": False},
        )
        with pytest.raises(RuntimeError, match="never materialized"):
            restart_omp_orchestrator(state.get("orch-o"))
        state.update_extra("orch-o", session_materialized=True)
        with pytest.raises(RuntimeError, match=r"deleted without /exit\?"):
            restart_omp_orchestrator(state.get("orch-o"))
    finally:
        state.close()


# --- gateway-parent continuation on child death --------------------------------


@pytest.mark.asyncio
async def test_gateway_parent_followup_idle_prompts(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeGatewayTransport(reply="noted")
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        await daemon._maybe_post_delegate_followup(
            "deleg/0", gw_id, "worker", status="completed",
            summary="Nightly key rotation verified complete",
        )
        await _drain(daemon)
        assert len(transport.prompts) == 1
        text, kind, node_id, internal = transport.prompts[0]
        assert node_id == gw_id and internal is True
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_gateway_parent_followup_busy_steers(daemon: sm.SidecarDaemon):
    """Busy gateway still gets the child result: steered, not dropped."""
    await daemon.boot()
    try:
        transport = FakeSteerTransport(reply="late", slow=True, steer_ok=True)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        slow = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(slow)
        slow.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)
        assert daemon._gateway_delivery_in_flight()

        await daemon._maybe_post_delegate_followup(
            "deleg/1", gw_id, "worker", status="completed",
            summary="Nightly key rotation verified complete",
        )
        assert transport.steers, "busy-gateway followup must steer, not drop"
        assert f"gateway-followup-steer:deleg/1" in daemon.routing_log
        slow.cancel()
        try:
            await slow
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await daemon.shutdown()


# --- gateway-room mid-turn steer reaches the running turn ----------------------


def test_steer_waits_through_agent_build_window():
    """Steer landing while the inject handler is still building the agent
    waits for the cache instead of reporting idle (the VM ignore)."""
    late = FakeSteerAgent()

    def _cache_late():
        time.sleep(0.2)
        with gs._locks_guard:
            gs._session_agents["gateway"] = late

    t = threading.Thread(target=_cache_late, daemon=True)
    t.start()
    out = gs.steer_gateway_agent("turn left")
    t.join(timeout=5)
    assert out.get("steered") is True
    assert late.steers == ["turn left"]


@pytest.mark.asyncio
async def test_midturn_gateway_text_steers_running_turn(daemon: sm.SidecarDaemon):
    """Mid-turn gateway-room text reaches the in-flight turn via the real
    gateway_session steer surface — no second delivery task spawns."""
    await daemon.boot()
    try:
        agent = FakeSteerAgent()
        with gs._locks_guard:
            gs._session_agents["gateway"] = agent
        transport = RealSteerTransport(reply="late", slow=True)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()

        slow = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(slow)
        slow.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)
        assert daemon._gateway_delivery_in_flight()

        outcome = SimpleNamespace(
            node_id=gw_id,
            actions=(InjectText(gw_id, "turn left", "steer"),),
            notices=(ControlNotice(gw_id, QUEUED_STEER_NOTICE),),
            disposition="steer",
        )
        await daemon._handle_gateway_prompt_outcome(outcome)

        assert agent.steers == ["turn left"]
        assert transport.prompts == [] or transport.prompts == [("slow", "prompt", gw_id)]
        assert len([t for t in list(daemon._gateway_tasks) if not t.done()]) == 1
        assert f"gateway-steer:{gw_id}" in daemon.routing_log
        slow.cancel()
        try:
            await slow
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_idle_gateway_text_prompts_fresh_turn(daemon: sm.SidecarDaemon):
    """Idle gateway-room text starts a fresh turn (no steer attempt)."""
    await daemon.boot()
    try:
        transport = RealSteerTransport(reply="fresh reply")
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        assert not daemon._gateway_delivery_in_flight()

        outcome = SimpleNamespace(
            node_id=gw_id,
            actions=(InjectText(gw_id, "hello?", "steer"),),
            notices=(ControlNotice(gw_id, QUEUED_STEER_NOTICE),),
            disposition="steer",
        )
        await daemon._handle_gateway_prompt_outcome(outcome)
        deadline = asyncio.get_running_loop().time() + 5.0
        while daemon._gateway_tasks and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert transport.prompts == [("hello?", "steer", gw_id)]
        assert all(c[2] != QUEUED_STEER_NOTICE for c in _room_sends(daemon, daemon.state.get(gw_id)["room_id"]))
    finally:
        await daemon.shutdown()
