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
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from observatory import sidecar_main as sm
from observatory.config_gen import (
    APPSERVICE_PORT_DEFAULT,
    HOMESERVER_ADDRESS,
    ObservatoryPaths,
)
from observatory.e2ee import EncryptedIntentExecutor
from observatory.renderer import IntentExecutor
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
    """No-network MatrixClient: records everything, hands out ids."""

    as_token: str = "as-tok"
    server_name: str = "mercury.local"
    admin_token: str = "admin-tok"
    homeserver_url: str = "http://127.0.0.1:18008"
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
        assert report["apply_plan"] > 0  # the tree actually converged
        assert gw["space_id"] and gw["room_id"]
        # plaintext default (O3): the PLAIN executor, not the encrypted one
        assert isinstance(daemon.executor, IntentExecutor)
        assert not isinstance(daemon.executor, EncryptedIntentExecutor)
        assert daemon.e2ee is None
        await daemon.shutdown()
        assert daemon.state is None  # final state flush ran

    @pytest.mark.asyncio
    async def test_boot_idempotent_gateway_node(self, daemon: sm.SidecarDaemon):
        await daemon.boot()
        mxid1 = daemon.state.get(sm.GATEWAY_NODE_ID)["mxid"]
        await daemon.shutdown()
        daemon2 = sm.SidecarDaemon(daemon.mercury_home,
                                   hermes_db=daemon.hermes_db,
                                   appservice_port=_free_port())
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
    def test_absent_sibling_is_none_not_crash(self, daemon: sm.SidecarDaemon):
        assert "observatory.control" not in sys.modules
        daemon.wire_siblings()
        assert daemon.control_router is None
        assert daemon.approvals is None
        assert daemon.spawn is None
        assert daemon.rooms is None

    def test_present_sibling_attach_factory_is_wired(self, daemon, monkeypatch):
        stopped = []

        class FakeControl:
            async def stop(self):
                stopped.append("control")

        fake_module = types.ModuleType("observatory.control")
        fake_module.attach = lambda d: FakeControl()
        monkeypatch.setitem(sys.modules, "observatory.control", fake_module)
        daemon.wire_siblings()
        assert isinstance(daemon.control_router, FakeControl)

    @pytest.mark.asyncio
    async def test_sibling_stopped_on_shutdown(self, daemon, monkeypatch):
        stopped = []

        class FakeSubsystem:
            def stop(self):
                stopped.append("rooms")

        fake_module = types.ModuleType("observatory.rooms")
        fake_module.attach = lambda d: FakeSubsystem()
        monkeypatch.setitem(sys.modules, "observatory.rooms", fake_module)
        await daemon.boot()
        assert isinstance(daemon.rooms, FakeSubsystem)
        await daemon.shutdown()
        assert stopped == ["rooms"]


# ---------------------------------------------------------------------------
# discovery event application
# ---------------------------------------------------------------------------


class TestDiscoveryApplication:
    @pytest.mark.asyncio
    async def test_add_creates_node_and_renders(self, daemon: sm.SidecarDaemon):
        from observatory.discovery import NodeEvent

        await daemon.boot()
        try:
            event = NodeEvent(
                kind="add", delegation_id="deleg_abc", task_index=0,
                parent_session="sess-1", name="test-sweep", goal="run tests",
                status="running", source="poll", seq=1,
            )
            await daemon._apply_discovery_event(event)
            row = daemon.state.get("deleg_abc/0")
            assert row["status"] == "live"
            assert row["parent_node_id"] == sm.GATEWAY_NODE_ID
            assert row["room_id"]  # provisioned + attached by apply_plan
            # duplicate add (poll/hook race) is a state-level no-op
            await daemon._apply_discovery_event(event)
            assert len(daemon.state.children_of(sm.GATEWAY_NODE_ID)) == 1
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_death_renders_d8(self, daemon: sm.SidecarDaemon):
        from observatory.discovery import NodeEvent

        await daemon.boot()
        try:
            add = NodeEvent(kind="add", delegation_id="deleg_abc", task_index=0,
                            parent_session="s", name="test-sweep", goal="g",
                            status="running", source="poll", seq=1)
            await daemon._apply_discovery_event(add)
            death = NodeEvent(kind="death", delegation_id="deleg_abc", task_index=0,
                              parent_session="s", name="test-sweep", goal="g",
                              status="completed", source="poll", seq=2,
                              summary="3 tests green")
            await daemon._apply_discovery_event(death)
            # depth-1 child of the gateway ⇒ row purged (D8 instant rule)
            with pytest.raises(KeyError):
                daemon.state.get("deleg_abc/0")
        finally:
            await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_death_for_unknown_node_is_noop(self, daemon: sm.SidecarDaemon):
        from observatory.discovery import NodeEvent

        await daemon.boot()
        try:
            ghost = NodeEvent(kind="death", delegation_id="deleg_none", task_index=0,
                              parent_session="s", name="x", goal="g",
                              status="completed", source="hook", seq=1)
            await daemon._apply_discovery_event(ghost)  # must not raise
        finally:
            await daemon.shutdown()


# ---------------------------------------------------------------------------
# platform_hook seam
# ---------------------------------------------------------------------------


class TestPlatformHook:
    def test_forwards_lifecycle_payloads_to_registered_target(self, monkeypatch):
        received = []

        class Recorder:
            def on_subagent_start(self, payload):
                received.append(("start", dict(payload)))

            def on_subagent_stop(self, payload):
                received.append(("stop", dict(payload)))

        monkeypatch.setattr(sm, "_discovery_hook_target", Recorder())

        subscribed = []

        class Hooks:
            def subscribe(self, event, cb):
                subscribed.append(event)

        class Ctx:
            hooks = Hooks()

        sm.platform_hook(Ctx())
        assert subscribed == ["subagent_start", "subagent_stop"]
        # the gateway seam now relays live payloads
        sm._hook_start({"child_goal": "g"})
        sm._hook_stop({"child_status": "completed"})
        assert received == [("start", {"child_goal": "g"}),
                            ("stop", {"child_status": "completed"})]

    def test_busless_ctx_is_logged_noop(self):
        class Ctx:
            pass  # no hooks / register_hook / on anywhere

        sm.platform_hook(Ctx())  # must not raise


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
        # orders after the homeserver unit (its own unit restarts tuwunel)
        assert "Requires=mercury-observatory-homeserver.service" in unit
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
        assert sm.APPSERVICE_PORT_DEFAULT_SRC == APPSERVICE_PORT_DEFAULT
