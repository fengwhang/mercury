"""Gateway steer-miss contract: idle never buffers, miss never drops.

Covers the mid-turn steer wiring the room-text path owns:
- ``steer_gateway_agent`` on an idle MODERN agent (liveness flags present,
  no live turn) must report miss — buffering into ``_pending_steer`` with
  no live turn to drain it loses the text while the caller believes it
  landed (H2 false-success).
- A LANDED mid-turn steer acks ``✔ applied``, never ``⏳ queued steer``
  (nothing was queued — the queued notice would lie).
- ``_steer_gateway_or_interrupt`` is the single miss→interrupt helper both
  the room-text path and the delegate-followup path use, so a miss always
  ends as interrupt-then-fresh-turn, never a silent drop.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory import gateway_session as gs
from observatory import sidecar_main as sm
from observatory.config_gen import HOMESERVER_ADDRESS, ObservatoryPaths
from observatory.control import (
    APPLIED_STEER_NOTICE,
    QUEUED_STEER_NOTICE,
    ControlNotice,
    InjectText,
)
from observatory.gateway_transport import GatewayTransport
from tests.observatory.test_sidecar_main import FakeMatrixClient


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOMESERVER_ADDRESS, 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _clean_session_registry():
    gs._session_agents.clear()
    gs._session_locks.clear()
    try:
        yield
    finally:
        gs._session_agents.clear()
        gs._session_locks.clear()


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


def _sends(client) -> list:
    return [c for c in client.calls if c[0] == "send"]


class LiveFlagAgent:
    """Fake with REAL AIAgent steer/redirect semantics + liveness flags.

    redirect: True only while a model request is active; degrades to the
    steer buffer during tool exec; False when idle. steer: always buffers
    (like the real one — which is exactly the false-success trap when
    idle, since nothing live will drain it).
    """

    def __init__(self):
        self._model_request_active = threading.Event()
        self._executing_tools = False
        self.redirects: list[str] = []
        self.steers: list[str] = []
        self.interrupts: list = []

    def redirect(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        if self._executing_tools:
            return self.steer(text)
        if not self._model_request_active.is_set():
            return False
        self.redirects.append(text.strip())
        return True

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True

    def interrupt(self, reason=None, **kwargs):
        self.interrupts.append((reason, kwargs))


class FakeMissTransport(GatewayTransport):
    """Slow prompt keeps a turn in flight; steer outcome is fixed."""

    def __init__(self, reply: str = "late", *, steer_ok: bool = True):
        self.reply = reply
        self.steer_ok = steer_ok
        self.prompts: list = []
        self.steers: list = []
        self.interrupts: list = []

    async def prompt(self, text, *, kind="prompt", node_id="gw", internal=False):
        self.prompts.append((text, kind, node_id))
        await asyncio.sleep(30)
        return self.reply

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw",
                                 room_id=None, internal=False):
        self.prompts.append((text, kind, node_id))
        await asyncio.sleep(30)
        return self.reply, []

    async def steer(self, text, *, node_id="gw"):
        self.steers.append((text, node_id))
        if self.steer_ok:
            return {"steered": True, "reason": ""}
        return {"steered": False, "reason": "idle — nothing to steer"}

    async def interrupt(self, reason="matrix /stop"):
        self.interrupts.append(reason)
        return {"interrupted": True, "reason": reason}


# --- gateway_session: idle modern agent must MISS -------------------------------


def test_idle_modern_agent_steer_reports_miss():
    """H2: idle cached agent (flags present, no live turn) must NOT absorb
    the steer into the buffer and report success — nothing will drain it."""
    agent = LiveFlagAgent()
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("stop doing that")
    assert out["steered"] is False
    assert agent.steers == []


def test_live_model_request_redirects_without_buffering():
    agent = LiveFlagAgent()
    agent._model_request_active.set()
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("turn left")
    assert out == {"steered": True, "reason": ""}
    assert agent.redirects == ["turn left"]
    assert agent.steers == []


def test_tool_exec_steer_buffers_into_live_turn():
    agent = LiveFlagAgent()
    agent._executing_tools = True
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("keep the tests green")
    assert out == {"steered": True, "reason": ""}
    assert agent.steers == ["keep the tests green"]


# --- sidecar: landed steer acks applied, never queued ----------------------------


@pytest.mark.asyncio
async def test_room_text_success_acks_applied_not_queued(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeMissTransport(reply="late", steer_ok=True)
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

        assert transport.steers == [("turn left", gw_id)]
        # No second delivery spawned for the steered text.
        assert len([t for t in list(daemon._gateway_tasks) if not t.done()]) == 1
        bodies = [c[2] for c in _sends(daemon.client)]
        assert APPLIED_STEER_NOTICE in bodies
        assert QUEUED_STEER_NOTICE not in bodies
        assert f"gateway-steer:{gw_id}" in daemon.routing_log
        slow.cancel()
        try:
            await slow
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await daemon.shutdown()


# --- sidecar: shared steer-or-interrupt helper (room-text + followup) -------------


@pytest.mark.asyncio
async def test_steer_or_interrupt_miss_runs_interrupt(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeMissTransport(steer_ok=False)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        ok = await daemon._steer_gateway_or_interrupt(gw_id, "new plan")
        assert ok is False
        assert transport.steers == [("new plan", gw_id)]
        assert transport.interrupts == ["matrix steer"]
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_steer_or_interrupt_success_skips_interrupt(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeMissTransport(steer_ok=True)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        ok = await daemon._steer_gateway_or_interrupt(gw_id, "turn left")
        assert ok is True
        assert transport.interrupts == []
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_followup_miss_shape_lands_text_as_next_turn(daemon: sm.SidecarDaemon):
    """The delegate-followup busy-branch shape: miss → shared fallback
    interrupt → the text runs as the next turn (never silently dropped).
    This is the exact sequence the one-line followup integration pastes."""
    await daemon.boot()
    try:
        transport = FakeMissTransport(reply="late", steer_ok=False)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()

        slow = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(slow)
        slow.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)
        assert daemon._gateway_delivery_in_flight()

        text = "[subagent turkey done] all green verify the result and reply to the room"
        # Followup busy-branch shape (mirrors _followup_to_gateway):
        if daemon._gateway_delivery_in_flight():
            landed = await daemon._steer_gateway_or_interrupt(gw_id, text)
            assert landed is False  # miss → fallback interrupt already ran
        task = asyncio.create_task(
            daemon._deliver_gateway_prompt(gw_id, text, kind="prompt", internal=True, quiet=False),
            name="observatory-gateway-followup-turkey",
        )
        daemon._gateway_tasks.add(task)
        task.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)

        assert transport.interrupts == ["matrix steer"]
        assert any(p[0] == text for p in transport.prompts)
        for t in list(daemon._gateway_tasks):
            if not t.done():
                t.cancel()
    finally:
        await daemon.shutdown()
