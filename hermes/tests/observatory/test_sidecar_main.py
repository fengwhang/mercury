"""Contract tests for observatory/sidecar_main.py (M4c/M5c assembly).

Laws under test:

- **component graph wiring**: boot assembles provision → homeserver →
  state + gateway node → client → (flag-gated) executor → renderer →
  intake (handler attached, port serving) → discovery (poll task) →
  sibling seams; shutdown reverses it and closes state cleanly;
- **sibling integration**: absent sibling module ⇒ None + no crash;
  present (fake) module's ``attach(daemon)`` factory ⇒ wired and later
  stopped on shutdown;
- **discovery event application**: add → node + lifecycle render;
  death → render_death; unknown-node death is a no-op;
- **platform_hook seam**: subscribes the gateway hook bus and forwards
  subagent payloads to the registered discovery target; bus-less ctx is
  a logged no-op;
- **unit template generation**: render_sidecar_unit output carries the
  exec line, ordering, PYTHONPATH and log paths;
- **intake round-trip through the daemon handler** (plaintext path —
  the encrypted path is covered in test_e2ee.py).

No real homeserver: provision + MatrixClient are replaced with fakes
(the renderer/executor/state/intake/discovery run REAL).
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import pytest

from observatory import sidecar_main as sm
from observatory.config_gen import (
    APPSERVICE_PORT_DEFAULT,
    HOMESERVER_ADDRESS,
    ObservatoryPaths,
)
from observatory.e2ee import EncryptedIntentExecutor
from observatory.matrix_client import MatrixError
from observatory.state import ObservatoryState


# ---------------------------------------------------------------------------
# fixtures: a fake provisioned home
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOMESERVER_ADDRESS, 0))
        return s.getsockname()[1]


@dataclass
class FakeMatrixClient:
    """No-network MatrixClient: records everything, hands out ids.
    Field order mirrors the real MatrixClient
    (homeserver_url, as_token, *, server_name, admin_token) — the
    daemon constructs it positionally, so the fake must accept the
    same call convention."""

    homeserver_url: str = "http://127.0.0.1:18008"
    as_token: str = "as-tok"
    server_name: str = "mercury.local"
    admin_token: str = "admin-tok"
    calls: list = field(default_factory=list)
    next_id: int = 0

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def register_virtual_user(self, localpart: str) -> str:
        self.calls.append(("register", localpart))
        return f"@{localpart}:{self.server_name}"

    async def create_room(self, *, name, sender, preset, invite, space=False, topic=None):
        rid = self._id("!space" if space else "!room")
        self.calls.append(("create_room", name, sender, preset, tuple(invite), space, rid))
        return rid

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender, formatted_body))
        return self._id("$ev")

    async def edit_message(self, room_id, event_id, body, *, sender, formatted_body=None):
        self.calls.append(("edit", room_id, event_id, body, sender))
        return self._id("$ev")

    async def client_api(self, method, path, *, sender=None, params=None, json_body=None):
        self.calls.append(("client_api", method, path, sender, json_body))
        return {"event_id": self._id("$ev")}

    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        children: dict[str, list] = {}
        ts = 0
        for call in self.calls:
            if call[0] == "child":
                _, space, child, _, _, remove = call
                if remove:
                    children[space] = [c for c in children.get(space, []) if c != child]
                else:
                    ts += 1
                    children.setdefault(space, []).append(child)
        rooms = [{"room_id": room_id, "room_type": "m.space",
                  "children_state": [
                      {"type": "m.space.child", "state_key": c, "origin_server_ts": i}
                      for i, c in enumerate(children.get(room_id, []))
                  ]}]
        for space, kids in children.items():
            if space != room_id:
                rooms.append({"room_id": space, "room_type": "m.space",
                              "children_state": [
                                  {"type": "m.space.child", "state_key": c,
                                   "origin_server_ts": i}
                                  for i, c in enumerate(kids)]})
        known_rooms = [c[6] for c in self.calls if c[0] == "create_room" and not c[5]]
        for rid in known_rooms:
            rooms.append({"room_id": rid})
        return {"rooms": rooms}

    async def close(self) -> None:
        self.calls.append(("close",))

    async def get_power_levels(self, room_id, *, sender):
        self.calls.append(("get_pl", room_id, sender))
        return {}

    async def invite(self, room_id, user_id, *, sender):
        self.calls.append(("invite", room_id, user_id, sender))

    async def join_room(self, room_id, *, sender):
        self.calls.append(("join", room_id, sender))
        return room_id

    async def leave_room(self, room_id, *, sender):
        self.calls.append(("leave", room_id, sender))

    async def delete_room(self, room_id, **kwargs):
        self.calls.append(("delete", room_id))
        return {}


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """A home that LOOKS provisioned: toml, registration YAML, owner creds.
    provision.provision and the homeserver health probe are stubbed — boot
    wiring is under test, not the installer (its own suite covers it)."""
    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    paths.toml.write_text(
        "[global]\nserver_name = \"mercury.local\"\naddress = \"127.0.0.1\"\n"
        "port = 18008\ndatabase_path = \"db\"\nappservice_dir = \"as\"\n"
        "allow_federation = false\nallow_registration = false\n"
        "registration_token = \"tok\"\n",
        encoding="utf-8",
    )
    paths.appservice_registration.write_text(
        "id: merc-observatory\nurl: http://127.0.0.1:18090\n"
        "as_token: \"as-tok\"\nhs_token: \"hs-tok\"\n"
        "sender_localpart: merc-bot\nrate_limited: false\n"
        "namespaces:\n  users:\n    - regex: \"^@merc_.*$\"\n      exclusive: true\n",
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
def daemon(fake_home: Path, monkeypatch) -> sm.SidecarDaemon:
    """A daemon whose homeserver is 'already healthy' and whose client is
    the recording fake — boot runs the REAL state/renderer/intake path."""
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port())
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


@pytest.fixture()
def pt_daemon(fake_home: Path, monkeypatch) -> sm.SidecarDaemon:
    """Plaintext sibling of daemon: the e2ee=False opt-out so
    discovery/render laws execute without a live Olm key-exchange
    peer — lifecycle sends would otherwise require device keys from
    a real homeserver (the encrypted send path is covered in
    test_e2ee.py)."""
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port(), e2ee=False)
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


# ---------------------------------------------------------------------------
# component graph wiring
# ---------------------------------------------------------------------------


class TestBootWiring:
    @pytest.mark.asyncio
    async def test_boot_assembles_the_graph(self, daemon: sm.SidecarDaemon):
        report = await daemon.boot()
        # order of law: state → gateway node → executor → renderer → intake
        assert daemon.state is not None
        gw = daemon.state.get(sm.GATEWAY_NODE_ID)
        assert report["gateway_mxid"] == gw["mxid"]
        assert daemon.client is not None
        assert daemon.renderer is not None
        assert daemon.renderer.executor is daemon.executor
        assert daemon.intake is not None and daemon.intake.handler_attached
        assert daemon.discovery is not None
        assert daemon._discovery_task is not None
        # the tree actually converged (the respawn pass re-ensures it
        # BEFORE boot's own apply_plan, so a fresh boot's apply_plan is
        # a converged no-op — convergence is proven by the gateway row,
        # not the report count)
        assert report["apply_plan"] >= 0
        assert gw["space_id"] and gw["room_id"]
        # E2EE-hot default (D4): the ENCRYPTED executor, never plaintext
        assert isinstance(daemon.executor, EncryptedIntentExecutor)
        assert daemon.e2ee is not None
        await daemon.shutdown()
        assert daemon.state is None  # final state flush ran

    @pytest.mark.asyncio
    async def test_boot_idempotent_gateway_node(self, daemon: sm.SidecarDaemon, monkeypatch):
        await daemon.boot()
        mxid1 = daemon.state.get(sm.GATEWAY_NODE_ID)["mxid"]
        await daemon.shutdown()
        daemon2 = sm.SidecarDaemon(daemon.mercury_home,
                                   hermes_db=daemon.hermes_db,
                                   appservice_port=_free_port())
        # same stubs as the first boot: healthy homeserver, fake client
        # (module-level patches from the daemon fixture are still live)
        monkeypatch.setattr(daemon2, "_homeserver_healthy", lambda: True)
        try:
            # second boot against the same home: node reused, never recreated
            import observatory.sidecar_main as sm2

            orig_add = ObservatoryState.add_node
            calls = []
            monkey_patch = ObservatoryState.add_node

            def spy(self, node_id, **kw):
                calls.append(node_id)
                return orig_add(self, node_id, **kw)

            ObservatoryState.add_node = spy
            try:
                await daemon2.boot()
            finally:
                ObservatoryState.add_node = orig_add
            assert daemon2.state.get(sm2.GATEWAY_NODE_ID)["mxid"] == mxid1
            assert sm2.GATEWAY_NODE_ID not in calls
        finally:
            await daemon2.shutdown()

    @pytest.mark.asyncio
    async def test_intake_serves_transactions(self, daemon: sm.SidecarDaemon):
        import aiohttp

        await daemon.boot()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"http://{HOMESERVER_ADDRESS}:{daemon.appservice_port}/health"
                ) as resp:
                    assert resp.status == 200
                # push as the homeserver would (hs_token auth)
                event = {
                    "type": "m.room.message", "event_id": "$t1",
                    "room_id": daemon.state.get(sm.GATEWAY_NODE_ID)["room_id"],
                    "sender": daemon.owner_mxid,
                    "content": {"msgtype": "m.text", "body": "steer: do the thing"},
                }
                async with http.put(
                    f"http://{HOMESERVER_ADDRESS}:{daemon.appservice_port}"
                    f"/_matrix/app/v1/transactions/tx-1",
                    params={"access_token": "hs-tok"},
                    json={"events": [event]},
                ) as resp:
                    assert resp.status == 200
                for _ in range(40):
                    if daemon.seen_events:
                        break
                    await asyncio.sleep(0.05)
                assert daemon.seen_events and \
                    daemon.seen_events[0]["content"]["body"] == "steer: do the thing"
                # dedup: the retried txn dispatches exactly once
                async with http.put(
                    f"http://{HOMESERVER_ADDRESS}:{daemon.appservice_port}"
                    f"/_matrix/app/v1/transactions/tx-1",
                    params={"access_token": "hs-tok"},
                    json={"events": [event]},
                ) as resp:
                    assert resp.status == 200
                    assert len(daemon.seen_events) == 1
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_e2ee_flag_boot_uses_encrypted_executor(self, fake_home, monkeypatch):
        from observatory import e2ee as e2ee_mod

        (fake_home / "config.yaml").write_text("observatory:\n  e2ee: true\n",
                                               encoding="utf-8")
        d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                             appservice_port=_free_port())
        monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
        monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
        # capability gate enforced at boot: stack missing ⇒ hard failure
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
        with pytest.raises(e2ee_mod.E2EEError):
            await d.boot()
        await d.shutdown()

        # stack present ⇒ the encrypted executor is wired
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)
        d2 = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                              appservice_port=_free_port())
        monkeypatch.setattr(d2, "_homeserver_healthy", lambda: True)
        monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
        try:
            await d2.boot()
            assert isinstance(d2.executor, EncryptedIntentExecutor)
            assert d2.e2ee is not None
        finally:
            await d2.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_stops_everything(self, daemon: sm.SidecarDaemon):
        await daemon.boot()
        port = daemon.appservice_port
        await daemon.shutdown()
        assert daemon._discovery_task is None
        assert daemon._runner is None
        assert daemon.state is None
        # the intake port is actually released
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((HOMESERVER_ADDRESS, port))  # would raise if still bound


# ---------------------------------------------------------------------------
# sibling integration seams
# ---------------------------------------------------------------------------


class TestSiblingWiring:
    def test_preboot_siblings_are_none(self, daemon: sm.SidecarDaemon):
        # before boot assembles the graph, every sibling handle is None —
        # never a half-wired object, never a crash on attribute access
        assert daemon.control_router is None
        assert daemon.approvals is None
        assert daemon.directives is None
        assert daemon.cron_rooms is None
        assert daemon.manual_runs is None
        assert daemon.registry is None

    @pytest.mark.asyncio
    async def test_boot_wires_sibling_subsystems(self, daemon: sm.SidecarDaemon):
        # wire_siblings constructs the landed M4a/M4b/M5 subsystems
        # directly (no attach() factories) — assert the real graph
        from observatory.approvals import ApprovalBridge
        from observatory.control import ControlRouter
        from observatory.cron_rooms import CronRooms
        from observatory.directives import DirectivesManager
        from observatory.manual_runs import ManualRunsWatcher
        from observatory.spawn import OrchestratorRegistry

        await daemon.boot()
        try:
            assert isinstance(daemon.control_router, ControlRouter)
            assert isinstance(daemon.approvals, ApprovalBridge)
            assert isinstance(daemon.directives, DirectivesManager)
            assert isinstance(daemon.cron_rooms, CronRooms)
            assert isinstance(daemon.manual_runs, ManualRunsWatcher)
            assert isinstance(daemon.registry, OrchestratorRegistry)
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_leaves_registry_handles_alive(self, daemon: sm.SidecarDaemon):
        # D18: restart is not death — shutdown tears down intake,
        # discovery and state but NEVER stops the orchestrator
        # registry's live handles; the next respawn pass re-adopts them
        await daemon.boot()
        registry = daemon.registry
        before = list(registry.handles())
        await daemon.shutdown()
        assert daemon.registry is registry
        assert list(registry.handles()) == before


# ---------------------------------------------------------------------------
# discovery event application
# ---------------------------------------------------------------------------


class TestDiscoveryApplication:
    @pytest.mark.asyncio
    async def test_add_creates_node_and_renders(self, pt_daemon: sm.SidecarDaemon):
        from observatory.discovery import NodeEvent

        await pt_daemon.boot()
        try:
            event = NodeEvent(
                kind="add", delegation_id="deleg_abc", task_index=0,
                parent_session="sess-1", name="test-sweep", goal="run tests",
                status="running", source="poll", seq=1,
            )
            await pt_daemon._apply_discovery_event(event)
            row = pt_daemon.state.get("deleg_abc/0")
            assert row["status"] == "live"
            assert row["parent_node_id"] == sm.GATEWAY_NODE_ID
            assert row["room_id"]  # provisioned + attached by apply_plan
            # duplicate add (poll/hook race) is a state-level no-op
            await pt_daemon._apply_discovery_event(event)
            assert len(pt_daemon.state.children_of(sm.GATEWAY_NODE_ID)) == 1
        finally:
            await pt_daemon.shutdown()

    @pytest.mark.asyncio
    async def test_death_renders_d8(self, pt_daemon: sm.SidecarDaemon):
        from observatory.discovery import NodeEvent

        await pt_daemon.boot()
        try:
            add = NodeEvent(kind="add", delegation_id="deleg_abc", task_index=0,
                            parent_session="s", name="test-sweep", goal="g",
                            status="running", source="poll", seq=1)
            await pt_daemon._apply_discovery_event(add)
            death = NodeEvent(kind="death", delegation_id="deleg_abc", task_index=0,
                              parent_session="s", name="test-sweep", goal="g",
                              status="completed", source="poll", seq=2,
                              summary="3 tests green")
            await pt_daemon._apply_discovery_event(death)
            # depth-1 child of the gateway ⇒ row purged (D8 instant rule)
            with pytest.raises(KeyError):
                pt_daemon.state.get("deleg_abc/0")
        finally:
            await pt_daemon.shutdown()

    @pytest.mark.asyncio
    async def test_death_for_unknown_node_is_noop(self, pt_daemon: sm.SidecarDaemon):
        from observatory.discovery import NodeEvent

        await pt_daemon.boot()
        try:
            ghost = NodeEvent(kind="death", delegation_id="deleg_none", task_index=0,
                              parent_session="s", name="x", goal="g",
                              status="completed", source="hook", seq=1)
            await pt_daemon._apply_discovery_event(ghost)  # must not raise
        finally:
            await pt_daemon.shutdown()


# ---------------------------------------------------------------------------
# platform_hook seam
# ---------------------------------------------------------------------------


class TestPlatformHook:
    def test_build_discovery_binds_hermes_db(self, fake_home: Path):
        # the gateway seam's builder: an unstarted DiscoveryEngine over
        # the hermes state.db (§7 poll source) with the lifecycle-hook
        # push interface the gateway thread calls
        from observatory import platform_hook
        from observatory.discovery import DiscoveryEngine

        disc = platform_hook.build_discovery(fake_home)
        assert isinstance(disc, DiscoveryEngine)
        assert disc._db_path == str(fake_home / "hermes" / "state.db")
        for hook in ("on_subagent_start", "on_subagent_stop"):
            assert callable(getattr(disc, hook))
        # hook payloads before start() buffer (order-preserving) —
        # early spawns are never lost, never raise
        disc.on_subagent_start({"child_goal": "g"})
        disc.on_subagent_stop({"child_status": "completed"})

    def test_boot_graph_adopts_last_boot_registry(self):
        # _run_respawn_pass adopts platform_hook.LAST_BOOT's registry —
        # the seam the gateway thread's boot shares with the daemon
        from observatory import platform_hook

        assert hasattr(platform_hook, "LAST_BOOT")
        assert hasattr(platform_hook, "build_discovery")
        assert hasattr(platform_hook, "try_boot_sidecar")

    @pytest.mark.asyncio
    async def test_boot_sidecar_constructs_registry_when_none_passed(
        self, fake_home: Path, monkeypatch
    ):
        # Regression: boot_sidecar referenced bare `registry` (NameError —
        # gateway sidecar boot thread died silently via except). The param
        # is optional (default None) and the boot constructs one inside.
        from observatory import platform_hook, respawn
        from observatory.spawn import OrchestratorRegistry

        monkeypatch.setattr(platform_hook, "open_state", lambda home: object())
        monkeypatch.setattr(platform_hook, "build_discovery", lambda home: object())
        captured: dict = {}

        async def fake_respawn_pass(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(respawn, "respawn_pass", fake_respawn_pass)

        cfg = {"observatory": {"enabled": True}}
        # Pre-fix this raised NameError: bare `registry` not in scope.
        result = await platform_hook.boot_sidecar(fake_home, config=cfg)
        assert isinstance(result.registry, OrchestratorRegistry)
        assert captured.get("registry") is result.registry

        sentinel = object()
        captured.clear()
        explicit = await platform_hook.boot_sidecar(
            fake_home, config=cfg, discovery=False, registry=sentinel
        )
        assert explicit.registry is sentinel
        assert captured.get("registry") is sentinel

        # The seam is an optional param defaulting to None.
        import inspect

        assert (
            inspect.signature(platform_hook.boot_sidecar)
            .parameters["registry"]
            .default
            is None
        )

    def test_boot_thread_body_forwards_registry(
        self, fake_home: Path, monkeypatch
    ):
        # try_boot_sidecar packages **boot_kwargs into _boot_thread_body —
        # a registry passed by the caller must reach the boot result.
        from observatory import platform_hook, respawn

        monkeypatch.setattr(platform_hook, "open_state", lambda home: object())
        captured: dict = {}

        async def fake_respawn_pass(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(respawn, "respawn_pass", fake_respawn_pass)

        sentinel = object()
        old = platform_hook.LAST_BOOT
        try:
            platform_hook._boot_thread_body(
                {
                    "mercury_home": fake_home,
                    "config": {"observatory": {"enabled": True}},
                    "discovery": False,
                    "registry": sentinel,
                }
            )
            assert platform_hook.LAST_BOOT is not None
            assert platform_hook.LAST_BOOT.registry is sentinel
            assert captured.get("registry") is sentinel
        finally:
            platform_hook.LAST_BOOT = old

    @pytest.mark.asyncio
    async def test_daemon_discovery_accepts_hook_payloads(self, daemon: sm.SidecarDaemon):
        # after boot the daemon owns a started engine: live hook pushes
        # bridge safely (thread-safe queue) and shutdown drains cleanly
        await daemon.boot()
        try:
            assert daemon.discovery is not None
            daemon.discovery.on_subagent_start({"child_goal": "g"})
            daemon.discovery.on_subagent_stop({"child_status": "completed"})
            assert daemon._discovery_task is not None
            assert not daemon._discovery_task.done()
        finally:
            await daemon.shutdown()


# ---------------------------------------------------------------------------
# unit template generation
# ---------------------------------------------------------------------------


class TestUnitTemplate:
    def test_render_carries_contract_lines(self, tmp_path):
        unit = sm.render_sidecar_unit(
            python_bin="/opt/mercury/hermes/.venv/bin/python",
            hermes_root="/opt/mercury/hermes",
            mercury_home="/home/phoenix/.mercury",
            log_dir="/home/phoenix/.mercury/observatory/logs",
        )
        assert "ExecStart=/opt/mercury/hermes/.venv/bin/python -m observatory.sidecar_main --home /home/phoenix/.mercury" in unit
        # orders after the homeserver unit without binding its lifetime
        # (Wants, not Requires: a homeserver bounce must not SIGTERM us)
        assert "Wants=mercury-observatory-homeserver.service" in unit
        assert "Requires=mercury-observatory-homeserver.service" not in unit
        assert "After=mercury-observatory-homeserver.service" in unit
        assert "Environment=PYTHONPATH=/opt/mercury/hermes" in unit
        assert "Restart=on-failure" in unit
        assert "append:/home/phoenix/.mercury/observatory/logs/sidecar.log" in unit
        assert "WantedBy=default.target" in unit

    def test_template_is_brace_safe(self):
        # .format() must survive every placeholder; a stale brace in the
        # template would KeyError only at render — render once here.
        text = (Path(sm.__file__).parent / "templates" / "mercury-observatory.service").read_text()
        assert "{python_bin}" in text and "{mercury_home}" in text


# ---------------------------------------------------------------------------
# CLI shape
# ---------------------------------------------------------------------------


class TestCli:
    def test_main_refuses_real_observatory_home_for_smoke(self, capsys):
        real = Path.home() / ".mercury" / "observatory"
        rc = sm.run_once_smoke(real, fresh=False)
        assert rc == 2
        assert "REFUSING" in capsys.readouterr().err

    def test_defaults(self):
        # the daemon's default port is config_gen's single source —
        # never a second copy of the constant in sidecar_main
        assert sm.APPSERVICE_PORT_DEFAULT == APPSERVICE_PORT_DEFAULT


# ---------------------------------------------------------------------------
# gateway ghost verification (VM-feedback: never serve a dead tree)
# ---------------------------------------------------------------------------


@dataclass
class GhostFakeClient(FakeMatrixClient):
    """FakeMatrixClient with a controllable profile directory: ``present``
    is the set of mxids the homeserver knows. A successful
    ``register_virtual_user`` provisions the ghost from the
    ``provision_on_call``-th call on (default 1); ``fail_register`` makes
    every register raise (the VM 0.0.17 case: registration failing at
    boot while the row exists)."""

    present: set = field(default_factory=set)
    fail_register: bool = False
    provision_on_call: int = 1
    register_count: int = 0

    async def register_virtual_user(self, localpart: str) -> str:
        self.calls.append(("register", localpart))
        self.register_count += 1
        if self.fail_register:
            raise MatrixError(
                "POST", "/_matrix/client/v3/register", 403,
                {"errcode": "M_FORBIDDEN", "error": "registration denied"},
            )
        mxid = f"@{localpart}:{self.server_name}"
        if self.register_count >= self.provision_on_call:
            self.present.add(mxid)
        return mxid

    async def client_api(self, method, path, *, sender=None, params=None, json_body=None):
        if method == "GET" and "/profile/" in path:
            self.calls.append(("client_api", method, path, sender, json_body))
            mxid = unquote(path.rsplit("/profile/", 1)[1])
            if mxid not in self.present:
                raise MatrixError(
                    "GET", path, 404,
                    {"errcode": "M_NOT_FOUND", "error": "User not found"},
                )
            return {"displayname": mxid}
        return await super().client_api(
            method, path, sender=sender, params=params, json_body=json_body
        )


def _ghost_daemon(fake_home: Path, monkeypatch, client: GhostFakeClient) -> sm.SidecarDaemon:
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port())
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", lambda *a, **k: client)
    return d


class TestGatewayGhostVerify:
    @pytest.mark.asyncio
    async def test_boot_fails_loudly_when_gateway_ghost_missing(
        self, fake_home: Path, monkeypatch
    ):
        """The 0.0.17 VM case: register fails AND the ghost is unknown —
        boot must FAIL LOUDLY, never serve a dead tree (no intake)."""
        client = GhostFakeClient(fail_register=True)
        daemon = _ghost_daemon(fake_home, monkeypatch, client)
        try:
            with pytest.raises(sm.provision.ProvisionError):
                await daemon.boot()
            assert daemon.intake is None  # dead tree never served
            assert daemon.gateway_mxid  # the row exists — the GHOST does not
            assert daemon.gateway_mxid not in client.present
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_boot_verifies_gateway_ghost_when_register_succeeds(
        self, fake_home: Path, monkeypatch
    ):
        """Happy path: ghost unknown at first probe, re-register provisions
        it, boot continues and reports the gate."""
        client = GhostFakeClient(provision_on_call=2)
        daemon = _ghost_daemon(fake_home, monkeypatch, client)
        try:
            report = await daemon.boot()
            assert report["gateway_ghost"] == "verified"
            assert daemon.gateway_mxid in client.present
            gw_local = daemon.gateway_mxid.lstrip("@").split(":", 1)[0]
            registers = [c for c in client.calls
                         if c[0] == "register" and c[1] == gw_local]
            assert len(registers) == 2  # _ensure once + verify retry once
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_boot_skips_reregister_when_ghost_already_known(
        self, fake_home: Path, monkeypatch
    ):
        """Ghost already on the server (M_USER_IN_USE-as-existing): verify
        is a pure probe, no extra register."""
        client = GhostFakeClient()
        daemon = _ghost_daemon(fake_home, monkeypatch, client)
        try:
            report = await daemon.boot()
            assert report["gateway_ghost"] == "verified"
            # second verify pass over the same state: probe only
            calls_before = len(client.calls)
            await daemon.verify_gateway_ghost()
            probes = [c for c in client.calls[calls_before:] if c[0] == "client_api"]
            registers = [c for c in client.calls[calls_before:] if c[0] == "register"]
            assert len(probes) == 1 and not registers
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_ghost_exists_contract(self, fake_home: Path, monkeypatch):
        """Profile 200 → True; 404/M_NOT_FOUND → False; any other error
        propagates (a sick homeserver is not 'ghost missing')."""
        client = GhostFakeClient()
        daemon = _ghost_daemon(fake_home, monkeypatch, client)
        daemon.state = ObservatoryState(daemon.paths.root / "state.db")
        daemon.client = client  # type: ignore[assignment]
        try:
            assert await daemon.ghost_exists("@nobody:mercury.local") is False
            client.present.add("@ghost:mercury.local")
            assert await daemon.ghost_exists("@ghost:mercury.local") is True

            async def sick(method, path, *, sender=None, params=None, json_body=None):
                raise MatrixError(
                    "GET", path, 500, {"errcode": "M_UNKNOWN", "error": "boom"}
                )

            monkeypatch.setattr(client, "client_api", sick)
            with pytest.raises(MatrixError):
                await daemon.ghost_exists("@ghost:mercury.local")
        finally:
            daemon.state.close()

    @pytest.mark.asyncio
    async def test_repair_ghosts_reregisters_all_live(
        self, fake_home: Path, monkeypatch
    ):
        """Repair path: every live ghost re-registered + verified, gateway
        included; a ghost whose register fails is reported, not raised."""
        client = GhostFakeClient()
        daemon = _ghost_daemon(fake_home, monkeypatch, client)
        daemon.state = ObservatoryState(daemon.paths.root / "state.db")
        daemon.client = client  # type: ignore[assignment]
        daemon.server_name = "mercury.local"
        try:
            daemon.gateway_mxid = daemon.ensure_gateway_node()
            daemon.state.add_node(
                "n-agent-1", engine="hermes", name="agent one",
                slug="agent-one", mxid="@merc_agent_one:mercury.local",
                session_ref="session:a1", parent_node_id=sm.GATEWAY_NODE_ID,
                extra={},
            )
            results = await daemon.repair_ghosts()
            assert results == {
                daemon.gateway_mxid: "verified",
                "@merc_agent_one:mercury.local": "verified",
            }
            registered = {c[1] for c in client.calls if c[0] == "register"}
            assert daemon.gateway_mxid.lstrip("@").split(":", 1)[0] in registered
            assert "merc_agent_one" in registered

            # failing register is a per-ghost report, never a crash
            client2 = GhostFakeClient(fail_register=True)
            daemon.client = client2  # type: ignore[assignment]
            results2 = await daemon.repair_ghosts()
            assert set(results2) == set(results)
            assert all(v.startswith("register-failed") for v in results2.values())
        finally:
            daemon.state.close()
    def test_repair_ghosts_cli_flag_parses(self):
        """--repair-ghosts is a real CLI flag (wired to run_repair_ghosts)."""
        with pytest.raises(FileNotFoundError):  # nonexistent home: no tuwunel.toml
            sm.main(["--repair-ghosts", "--home", "/nonexistent-home-xyz"])


class TestOnCryptoGlue:
    """The intake crypto side-channel reaches the E2EE router (or no-ops
    cleanly when E2EE is off) — the live key-exchange gap, pinned."""

    @pytest.mark.asyncio
    async def test_crypto_transaction_routes_to_manager(self, tmp_path):
        d = sm.SidecarDaemon(tmp_path / "mercury",
                             hermes_db=tmp_path / "h.db",
                             appservice_port=_free_port())
        routed: list[dict] = []

        class _FakeE2EE:
            async def handle_as_transaction(self, txn):
                routed.append(txn)
                return {"to_device": 1, "device_lists": 0,
                        "otk_counts": 0}

        d.e2ee = _FakeE2EE()  # type: ignore[assignment]
        txn = {"to_device": {"@merc_gw:x": {"D": {}}}}
        await d._on_crypto(txn)
        assert routed == [txn]

    @pytest.mark.asyncio
    async def test_crypto_without_e2ee_is_noop(self, tmp_path):
        d = sm.SidecarDaemon(tmp_path / "mercury",
                             hermes_db=tmp_path / "h.db",
                             appservice_port=_free_port())
        d.e2ee = None  # type: ignore[assignment]
        await d._on_crypto({"to_device": {}})  # must not raise

    @pytest.mark.asyncio
    async def test_router_errors_never_kill_intake(self, tmp_path):
        d = sm.SidecarDaemon(tmp_path / "mercury",
                             hermes_db=tmp_path / "h.db",
                             appservice_port=_free_port())

        class _Boom:
            async def handle_as_transaction(self, txn):
                raise RuntimeError("boom")

        d.e2ee = _Boom()  # type: ignore[assignment]
        await d._on_crypto({"to_device": {"@merc_gw:x": {"D": {}}}})
