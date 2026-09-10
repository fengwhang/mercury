"""Regression tests for the gateway-room → gateway-session path.

The live bug: a Matrix message in the gateway room produced a
"queued steer" notice but the prompt never reached any session — the
engine transports were log-only. These tests pin the fixed behavior
end to end (message in → prompt delivered as a *prompt* → reply
renders in the room) with a fake gateway transport standing in for the
control-socket round trip (the socket + turn halves carry their own
suites: test_control_socket.py, test_gateway_session.py,
test_gateway_transport.py).

No real homeserver: provision + MatrixClient are faked like
test_sidecar_main.py (whose FakeMatrixClient is reused verbatim).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory import sidecar_main as sm
from observatory.config_gen import HOMESERVER_ADDRESS, ObservatoryPaths
from observatory.control import QUEUED_STEER_NOTICE, InjectText, OmpSteer
from observatory.gateway_transport import (
    ControlSocketGatewayTransport,
    GatewayTransport,
    GatewayTransportError,
)
from tests.observatory.test_sidecar_main import FakeMatrixClient


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOMESERVER_ADDRESS, 0))
        return s.getsockname()[1]


class FakeGatewayTransport(GatewayTransport):
    """Test double: records prompts, answers a canned reply (or raises)."""

    def __init__(self, reply: str = "pong", error: Exception | None = None):
        self.prompts: list[tuple[str, str, str]] = []
        self._reply = reply
        self._error = error

    async def prompt(
        self, text: str, *, kind: str = "prompt", node_id: str = "gw"
    ) -> str:
        self.prompts.append((text, kind, node_id))
        if self._error is not None:
            raise self._error
        return self._reply


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
def daemon(fake_home: Path, monkeypatch) -> sm.SidecarDaemon:
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port(), e2ee=False)
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


def _gw_event(daemon: sm.SidecarDaemon, body: str) -> dict:
    assert daemon.state is not None
    return {
        "type": "m.room.message",
        "event_id": "$gw-hello",
        "room_id": daemon.state.get(sm.GATEWAY_NODE_ID)["room_id"],
        "sender": daemon.owner_mxid,
        "origin_server_ts": 1,
        "content": {"msgtype": "m.text", "body": body},
    }


async def _drain_gateway_tasks(daemon: sm.SidecarDaemon, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while daemon._gateway_tasks and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    pending = [t for t in list(daemon._gateway_tasks) if not t.done()]
    assert not pending, "gateway delivery task did not finish"


def _sends(client: FakeMatrixClient) -> list:
    return [c for c in client.calls if c[0] == "send"]


# ---------------------------------------------------------------------------
# the regression: hello? → prompt → reply renders, no steer notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_room_hello_delivers_prompt_and_renders_reply(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        fake = FakeGatewayTransport(reply="hello yourself")
        daemon.gateway_transport = fake
        assert daemon.state is not None and daemon.client is not None

        await daemon._on_transaction("tx-gw-1", [_gw_event(daemon, "hello?")])
        await _drain_gateway_tasks(daemon)

        # InjectText kind threaded end to end (plain text routes as steer)
        assert fake.prompts == [("hello?", "steer", sm.GATEWAY_NODE_ID)]

        # reply rendered in the gateway room, in the gateway agent's voice
        gw_room = daemon.state.get(sm.GATEWAY_NODE_ID)["room_id"]
        replies = [
            c for c in _sends(daemon.client)
            if c[1] == gw_room and c[3] == daemon.gateway_mxid
        ]
        assert (gw_room, "hello yourself") in [(c[1], c[2]) for c in replies]

        # …and the steer honesty notice never posted for a prompt
        assert all(c[2] != QUEUED_STEER_NOTICE for c in _sends(daemon.client))
        assert "steer" in daemon.routing_log
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_gateway_delivery_failure_posts_unreachable(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        daemon.gateway_transport = FakeGatewayTransport(
            error=GatewayTransportError("down")
        )
        assert daemon.state is not None and daemon.client is not None

        await daemon._on_transaction("tx-gw-2", [_gw_event(daemon, "hello?")])
        await _drain_gateway_tasks(daemon)

        bodies = [c[2] for c in _sends(daemon.client)]
        assert sm.GATEWAY_UNREACHABLE_NOTICE in bodies
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_missing_transport_posts_unreachable(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        daemon.gateway_transport = None
        assert daemon.client is not None

        await daemon._on_transaction("tx-gw-3", [_gw_event(daemon, "hello?")])
        await _drain_gateway_tasks(daemon)

        bodies = [c[2] for c in _sends(daemon.client)]
        assert sm.GATEWAY_UNREACHABLE_NOTICE in bodies
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_empty_reply_renders_nothing(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        daemon.gateway_transport = FakeGatewayTransport(reply="   ")
        assert daemon.client is not None
        before = len(_sends(daemon.client))

        await daemon._on_transaction("tx-gw-4", [_gw_event(daemon, "hello?")])
        await _drain_gateway_tasks(daemon)

        assert len(_sends(daemon.client)) == before
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_boot_wires_real_transport(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = daemon.gateway_transport
        assert isinstance(transport, ControlSocketGatewayTransport)
        assert transport.mercury_home == daemon.mercury_home
    finally:
        await daemon.shutdown()


# ---------------------------------------------------------------------------
# routing predicates (no boot needed)
# ---------------------------------------------------------------------------


def test_is_gateway_prompt_matches_only_gateway_inject_text(
    fake_home: Path,
):
    d = sm.SidecarDaemon(fake_home)
    gw_outcome = SimpleNamespace(
        node_id="gw",
        actions=(InjectText("gw", "hi", "steer"),),
        notices=(),
    )
    assert d._is_gateway_prompt(gw_outcome) is True
    assert d._is_gateway_prompt(
        SimpleNamespace(node_id="other", actions=gw_outcome.actions, notices=())
    ) is False
    assert d._is_gateway_prompt(
        SimpleNamespace(
            node_id="gw", actions=(OmpSteer("gw", "hi"),), notices=()
        )
    ) is False
    assert d._is_gateway_prompt(SimpleNamespace(node_id="gw", actions=(), notices=())) is False


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_delivery(fake_home: Path):
    d = sm.SidecarDaemon(fake_home)

    async def stuck() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(stuck())
    d._gateway_tasks.add(task)
    task.add_done_callback(d._gateway_tasks.discard)
    await d.shutdown()
    assert task.done()
    assert not d._gateway_tasks
