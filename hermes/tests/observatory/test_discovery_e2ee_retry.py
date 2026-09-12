"""Discovery-add survives E2EE send failures (H1/H2).

Live case: child room !o4fXZRp2HudRGswbXC:vm converged, but the first
lifecycle send raised ``EncryptionError: No group session created`` and
node setup aborted there — room existed, lifecycle never landed, no
retry. This test drives ``_apply_discovery_event`` with an executor
whose first SendMessage raises, and asserts node + room converge first
with the failed send retried to delivery.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from observatory import sidecar_main as sm
from observatory.config_gen import HOMESERVER_ADDRESS, ObservatoryPaths


class FakeEncryptionError(Exception):
    """Duck-typed mautrix EncryptionError (No group session created)."""


@dataclass
class FakeMatrixClient:
    homeserver_url: str = "http://127.0.0.1:18008"
    as_token: str = "as-tok"
    server_name: str = "mercury.local"
    admin_token: str = "admin-tok"
    on_admin_401: Any = None
    calls: list = field(default_factory=list)
    next_id: int = 0
    registered: set = field(default_factory=set)

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def register_virtual_user(self, localpart: str) -> str:
        self.calls.append(("register", localpart))
        self.registered.add(localpart)
        return f"@{localpart}:{self.server_name}"

    async def create_room(self, *, name, sender, preset, invite, space=False,
                          topic=None, initial_state=None):
        rid = self._id("!space" if space else "!room")
        self.calls.append(("create_room", name, sender, preset, tuple(invite),
                           space, rid, initial_state))
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
        from observatory.matrix_client import MatrixError
        self.calls.append(("client_api", method, path, sender, json_body))
        if method == "POST" and str(path).endswith("/login"):
            try:
                user = str(((json_body or {}).get("identifier") or {}).get("user") or "")
            except Exception:
                user = ""
            localpart = user.lstrip("@").split(":", 1)[0]
            if localpart and localpart not in self.registered:
                raise MatrixError(method, path, 400, {"errcode": "M_INVALID_PARAM", "error": "Called create_device for non-existent user"})
        return {"event_id": self._id("$ev")}


    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        children: dict[str, list] = {}
        for call in self.calls:
            if call[0] == "child":
                _, space, child, _, _, remove = call
                if remove:
                    children[space] = [c for c in children.get(space, []) if c != child]
                else:
                    children.setdefault(space, []).append(child)
        rooms = [{"room_id": room_id, "room_type": "m.space",
                  "children_state": [
                      {"type": "m.space.child", "state_key": c, "origin_server_ts": i}
                      for i, c in enumerate(children.get(room_id, []))]}]
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOMESERVER_ADDRESS, 0))
        return s.getsockname()[1]


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch) -> Path:
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
def pt_daemon(fake_home: Path, monkeypatch) -> sm.SidecarDaemon:
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port(), e2ee=False)
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


class TestDiscoveryE2EERetry:
    @pytest.mark.asyncio
    async def test_add_survives_first_send_failure_and_retries(
        self, pt_daemon: sm.SidecarDaemon
    ):
        """First lifecycle send raising EncryptionError must not abort the
        add: node persists, room/space converge, retry delivers."""
        from observatory.discovery import NodeEvent
        from observatory.renderer import SendMessage

        await pt_daemon.boot()
        try:
            assert pt_daemon.renderer is not None and pt_daemon.state is not None
            executor = pt_daemon.renderer.executor
            assert executor is not None
            real_execute = executor.execute
            calls = {"sends": 0}
            apply_plans = {"n": 0}
            real_apply_plan = pt_daemon.renderer.apply_plan

            async def flaky_execute(intents):
                if any(isinstance(op, SendMessage) for op in intents):
                    calls["sends"] += 1
                    if calls["sends"] == 1:
                        raise FakeEncryptionError("No group session created")
                return await real_execute(intents)

            async def counting_apply_plan(plan):
                apply_plans["n"] += 1
                return await real_apply_plan(plan)

            executor.execute = flaky_execute  # type: ignore[method-assign]
            pt_daemon.renderer.apply_plan = counting_apply_plan  # type: ignore[method-assign]

            # Must not raise: state + converge land before any send.
            await pt_daemon._apply_discovery_event(NodeEvent(
                kind="add", delegation_id="deleg_e2ee", task_index=0,
                parent_session="", name="e2ee-kid", goal="g",
                status="running", source="poll", seq=1,
            ))
            row = pt_daemon.state.get("deleg_e2ee/0")
            assert row["status"] == "live"
            assert row["room_id"], "room must converge before the lifecycle send"
            assert row["space_id"], "space must converge before the lifecycle send"
            assert apply_plans["n"] >= 1, "converge (apply_plan) must run on add"

            # Failed lifecycle send retries instead of dropping: heal the
            # executor, drain, and the lifecycle send lands exactly once.
            sends_before = [
                c for c in pt_daemon.client.calls if c[0] == "send"
            ] if pt_daemon.client is not None else []
            drained = await pt_daemon.renderer.retry_pending_sends()
            assert drained >= 1, "retry queue must deliver the failed lifecycle send"
            sends_after = [
                c for c in pt_daemon.client.calls if c[0] == "send"
            ] if pt_daemon.client is not None else []
            assert len(sends_after) == len(sends_before) + 1
            # Drained queue stays empty: a second drain is a no-op.
            assert await pt_daemon.renderer.retry_pending_sends() == 0
        finally:
            await pt_daemon.shutdown()


class TestLiveSendRetryQueue:
    @pytest.mark.asyncio
    async def test_failed_tool_and_thinking_sends_queue_instead_of_raising(
        self, tmp_path: Path
    ):
        """Tool/thinking sends hit the same wedged-session failure as the
        lifecycle send — they must queue (never raise, never drop)."""
        from observatory.identity import assign_slug, virtual_mxid
        from observatory.renderer import Renderer
        from observatory.state import ObservatoryState

        state = ObservatoryState(tmp_path / "state.db")

        class AlwaysFailingExecutor:
            async def execute(self, intents):
                raise FakeEncryptionError("No group session created")

        def add(node_id: str, name: str, parent: str | None) -> None:
            slug = assign_slug(name, state)
            state.add_node(
                node_id, engine="hermes", name=name, slug=slug,
                mxid=virtual_mxid(slug), session_ref=f"session:{node_id}",
                parent_node_id=parent,
            )

        add("gw", "gateway agent", None)
        add("orch/0", "worker", "gw")
        renderer = Renderer(
            state, gateway_node_id="gw", server_name="mercury.local",
            owner_mxid="@owner:mercury.local", executor=AlwaysFailingExecutor(),
        )

        await renderer.render_tool_call("orch/0", "bash", "ls")
        await renderer.render_thinking("orch/0", "considering")
        assert renderer.pending_retry_count == 2

    @pytest.mark.asyncio
    async def test_retry_queue_drops_after_bounded_attempts(self, tmp_path: Path):
        """A permanently failing room must not spin the queue forever:
        after SEND_RETRY_MAX attempts the send drops and drains go quiet."""
        from observatory.identity import assign_slug, virtual_mxid
        from observatory.renderer import SEND_RETRY_MAX, Renderer
        from observatory.state import ObservatoryState

        state = ObservatoryState(tmp_path / "state.db")

        class AlwaysFailingExecutor:
            async def execute(self, intents):
                raise FakeEncryptionError("No group session created")

        slug = assign_slug("gateway agent", state)
        state.add_node(
            "gw", engine="hermes", name="gateway agent", slug=slug,
            mxid=virtual_mxid(slug), session_ref="session:gw", parent_node_id=None,
        )
        slug2 = assign_slug("worker", state)
        state.add_node(
            "orch/0", engine="hermes", name="worker", slug=slug2,
            mxid=virtual_mxid(slug2), session_ref="session:orch/0",
            parent_node_id="gw",
        )
        renderer = Renderer(
            state, gateway_node_id="gw", server_name="mercury.local",
            owner_mxid="@owner:mercury.local", executor=AlwaysFailingExecutor(),
        )

        await renderer.render_agent_message("orch/0", "hello")
        assert renderer.pending_retry_count == 1
        delivered = 0
        for _ in range(SEND_RETRY_MAX + 2):
            delivered += await renderer.retry_pending_sends()
        assert delivered == 0
        assert renderer.pending_retry_count == 0
