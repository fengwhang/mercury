"""Gateway-to-sidecar spawn handle handoff (post-boot + live-proc reattach).

D18 law: same session / same MXID+rooms, never silently fork a second
session, never resurrect exited nodes.
"""
from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

import observatory.sidecar_main as sm
from observatory.spawn import OrchestratorHandle, OrchestratorRegistry, spawn_orchestrator

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

    def interrupt(self, message=None, **kwargs) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeOmpChild:
    def __init__(self, session_file: str):
        self.session_file = session_file
        self.prompts: list[str] = []
        self.steers: list[str] = []
        self.stopped = False
        self._client = SimpleNamespace(
            get_state=lambda sf=session_file: SimpleNamespace(session_file=sf),
            _process=SimpleNamespace(poll=lambda: None, pid=424242),
        )

    def run_task(self, prompt: str, timeout=None):
        self.prompts.append(prompt)
        return {"status": "completed", "summary": f"omp-echo:{prompt}"}

    def steer(self, text: str) -> None:
        self.steers.append(text)

    def abort(self, reason=None) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    from observatory.provision import ObservatoryPaths

    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir, paths.logs_dir):
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
def daemon(fake_home, monkeypatch):
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port(), e2ee=False)
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


def _msg(room_id: str, body: str) -> dict:
    return {"type": "m.room.message", "event_id": "$ev1", "room_id": room_id,
            "sender": OWNER, "origin_server_ts": 1,
            "content": {"msgtype": "m.text", "body": body}}


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
async def test_gateway_reboot_preserves_live_registry(fake_home, monkeypatch):
    """A gateway reboot must not drop post-boot spawn handles: boot_sidecar
    reuses the existing LAST_BOOT registry when no explicit registry is given."""
    from observatory import platform_hook

    old = platform_hook.LAST_BOOT
    try:
        platform_hook.LAST_BOOT = None
        first = await platform_hook.boot_sidecar(
            fake_home, client=None, discovery=False)
        assert first.registry is not None
        # _boot_thread_body publishes the boot (gateway restart reads it back).
        platform_hook.LAST_BOOT = first
        first.registry.register(OrchestratorHandle(
            node_id="orch-keep", engine="hermes", name="keep",
            session_ref="sess-keep"))
        second = await platform_hook.boot_sidecar(
            fake_home, client=None, discovery=False)
        assert second.registry is first.registry
        assert second.registry.get("orch-keep") is not None
    finally:
        platform_hook.LAST_BOOT = old


@pytest.mark.asyncio
async def test_postboot_spawn_adopted_after_registry_divergence(daemon, monkeypatch):
    """Post-boot spawn then simulated gateway restart (new LAST_BOOT object,
    sidecar registry dropped) still reaches the engine via the LIVE registry."""
    from observatory import platform_hook

    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-postboot-handoff")
        gateway_registry = OrchestratorRegistry()
        row = await spawn_orchestrator(
            "postboot2", "hermes", server_name=SERVER, state=daemon.state,
            registry=gateway_registry, renderer=daemon.renderer,
            agent_factory=lambda: agent)
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        # Simulate gateway restart: NEW BootResult object carrying the LIVE
        # registry (handles preserved), sidecar registry dropped (restart).
        monkeypatch.setattr(
            platform_hook, "LAST_BOOT",
            SimpleNamespace(registry=gateway_registry,
                            mercury_home=str(daemon.mercury_home)))
        from observatory.spawn import OrchestratorRegistry as Reg
        daemon.registry = Reg()
        assert daemon.registry.get(node_id) is None
        await daemon._on_transaction("tx-h1", [_msg(room["room_id"], "hello handoff")])
        await _drain(daemon)
        assert agent.turns == ["hello handoff"]
        assert daemon.registry.get(node_id) is not None
        assert f"child-adopted:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert any(c[2] == "echo:hello handoff" for c in sends)
        assert not any("unavailable" in c[2] for c in sends)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_king_live_omp_reattach_no_second_proc(daemon, monkeypatch, tmp_path):
    """King shape: omp row, allocated ref missing from disk, live RPC proc in
    the LIVE registry under its real file. Adopt repoints state, attaches the
    feed, spawns NO second proc."""
    from observatory import platform_hook
    from observatory.spawn import omp_sessions_dir

    await daemon.boot()
    try:
        sess_dir = omp_sessions_dir(daemon.mercury_home)
        sess_dir.mkdir(parents=True, exist_ok=True)
        live_file = sess_dir / "king-live.jsonl"
        live_file.write_text("{}\n", encoding="utf-8")
        missing_ref = str(sess_dir / "2026-09-13T00-28-45-758Z_king.jsonl")
        assert not __import__("pathlib").Path(missing_ref).exists()

        child = FakeOmpChild(str(live_file))
        gateway_registry = OrchestratorRegistry()
        row = await spawn_orchestrator(
            "king", "omp", server_name=SERVER, state=daemon.state,
            registry=gateway_registry, renderer=daemon.renderer,
            mercury_home=daemon.mercury_home,
            omp_child_factory=lambda: FakeOmpChild(missing_ref))
        node_id = row["node_id"]
        # Force the king shape: allocated ref never hit disk, live proc holds
        # its real file under the session dir.
        daemon.state.set_session_ref(node_id, missing_ref)
        gateway_registry.unregister(node_id)
        gateway_registry.register(OrchestratorHandle(
            node_id=node_id, engine="omp", name="king",
            session_ref=str(live_file), rpc=child))
        room = daemon.state.get(node_id)
        monkeypatch.setattr(
            platform_hook, "LAST_BOOT",
            SimpleNamespace(registry=gateway_registry,
                            mercury_home=str(daemon.mercury_home)))
        from observatory.spawn import OrchestratorRegistry as Reg
        daemon.registry = Reg()

        spawned: list[str] = []
        import observatory.sidecar_main as smod

        real_build = None
        try:
            import observatory.spawn as spawn_mod
            real_build = spawn_mod.build_omp_child

            def _no_second_proc(**kwargs):
                spawned.append(str(kwargs))
                raise AssertionError("second omp proc forked (D18)")

            monkeypatch.setattr(spawn_mod, "build_omp_child", _no_second_proc)
            await daemon._on_transaction("tx-k1", [_msg(room["room_id"], "king hello")])
            await _drain(daemon)
        finally:
            if real_build is not None:
                pass
        assert spawned == [], "sidecar forked a second omp proc"
        assert child.prompts == ["king hello"]
        assert daemon.state.get(node_id)["session_ref"] == str(live_file)
        assert node_id in daemon.omp_feeds
        assert f"child-adopted:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert not any("unavailable" in c[2] for c in sends)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_ace_hermes_missing_row_reattach(daemon, monkeypatch):
    """Ace shape: hermes row whose session id has no SessionDB row yet, live
    agent in the LIVE registry. Adopt resumes-or-fresh-builds by session id
    with a state repoint and the message reaches the engine."""
    from observatory import platform_hook

    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-ace-live")
        gateway_registry = OrchestratorRegistry()
        row = await spawn_orchestrator(
            "ace", "hermes", server_name=SERVER, state=daemon.state,
            registry=gateway_registry, renderer=daemon.renderer,
            agent_factory=lambda: FakeHermesAgent("sess-stale-ace"))
        node_id = row["node_id"]
        gateway_registry.unregister(node_id)
        gateway_registry.register(OrchestratorHandle(
            node_id=node_id, engine="hermes", name="ace",
            session_ref="sess-ace-live", agent=agent))
        room = daemon.state.get(node_id)
        monkeypatch.setattr(
            platform_hook, "LAST_BOOT",
            SimpleNamespace(registry=gateway_registry,
                            mercury_home=str(daemon.mercury_home)))
        from observatory.spawn import OrchestratorRegistry as Reg
        daemon.registry = Reg()
        await daemon._on_transaction("tx-a1", [_msg(room["room_id"], "ace hello")])
        await _drain(daemon)
        assert agent.turns == ["ace hello"]
        assert daemon.state.get(node_id)["session_ref"] == "sess-ace-live"
        assert f"child-adopted:{node_id}" in daemon.routing_log
        sends = _room_sends(daemon, room["room_id"])
        assert not any("unavailable" in c[2] for c in sends)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_truly_nothing_orphan_notice_fires(daemon):
    """Truly-nothing-exists (no handle, no session) still posts the loud
    v0.0.45 orphan notice at boot — never silence."""
    from observatory.identity import assign_slug, virtual_mxid
    from observatory.state import ObservatoryState

    pre = ObservatoryState(daemon.paths.root / "state.db")
    try:
        slug = assign_slug("ghost-nothing", pre)
        row = pre.add_node(
            "orch-nothing1", engine="hermes", name="ghost-nothing", slug=slug,
            mxid=virtual_mxid(slug, server_name=SERVER),
            session_ref="sess-nothing-here", parent_node_id=None,
            extra={"model": None, "session_materialized": False})
        node_id = row["node_id"]
    finally:
        pre.close()
    await daemon.boot()
    try:
        assert daemon.registry.get(node_id) is None
        assert f"child-orphan:{node_id}" in daemon.routing_log
        after = daemon.state.get(node_id)
        room_id = after.get("room_id")
        assert room_id
        sends = _room_sends(daemon, room_id)
        assert any("first turn" in c[2] and "/exit" in c[2] for c in sends)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_exited_node_never_resurrected(daemon, monkeypatch):
    """An exited node is never resurrected via a stale live-registry entry."""
    from observatory import platform_hook

    await daemon.boot()
    try:
        agent = FakeHermesAgent("sess-dead-live")
        gateway_registry = OrchestratorRegistry()
        row = await spawn_orchestrator(
            "doomed", "hermes", server_name=SERVER, state=daemon.state,
            registry=daemon.registry, renderer=daemon.renderer,
            agent_factory=lambda: FakeHermesAgent("sess-doomed"))
        node_id = row["node_id"]
        room = daemon.state.get(node_id)
        daemon.state.mark_dead(node_id)
        gateway_registry.register(OrchestratorHandle(
            node_id=node_id, engine="hermes", name="doomed",
            session_ref=str(row["session_ref"]), agent=agent))
        monkeypatch.setattr(
            platform_hook, "LAST_BOOT",
            SimpleNamespace(registry=gateway_registry,
                            mercury_home=str(daemon.mercury_home)))
        daemon.registry.unregister(node_id)
        handle = daemon._child_handle(node_id)
        assert handle is None
        assert agent.turns == []
        assert daemon.registry.get(node_id) is None
    finally:
        await daemon.shutdown()
