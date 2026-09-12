"""Gateway-room mid-turn steer: soft-steer into the running turn.

While a gateway turn is in flight, a new gateway-room message must reach
the RUNNING turn via the ``steer`` verb (CLI steer parity) — not queue
silently behind it on the per-session lock. When idle, behavior is
unchanged (fresh prompt turn). Steer miss falls back to
interrupt-then-inject (/stop path proves interrupt works).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory import gateway_session as gs
from observatory import sidecar_main as sm
from observatory.config_gen import HOMESERVER_ADDRESS, ObservatoryPaths
from observatory.control import QUEUED_STEER_NOTICE, ControlNotice, InjectText
from observatory.gateway_transport import (
    ControlSocketGatewayTransport,
    GatewayTransport,
)
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


class FakeSteerAgent:
    def __init__(self):
        self.steers: list[str] = []
        self.interrupts: list = []

    def steer(self, text: str):
        self.steers.append(text)
        return True

    def interrupt(self, reason=None, **kwargs):
        self.interrupts.append((reason, kwargs))


class FakeSteerTransport(GatewayTransport):
    """Records prompts/steers/interrupts; slow prompt keeps a turn in flight."""

    def __init__(self, reply: str = "ok", *, steer_ok: bool = True, slow: bool = False):
        self.reply = reply
        self.steer_ok = steer_ok
        self.slow = slow
        self.prompts: list = []
        self.steers: list = []
        self.interrupts: list = []

    async def prompt(self, text, *, kind="prompt", node_id="gw", internal=False):
        self.prompts.append((text, kind, node_id))
        if self.slow:
            await asyncio.sleep(30)
        return self.reply

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw",
                                 room_id=None, internal=False):
        self.prompts.append((text, kind, node_id))
        if self.slow:
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


# --- gateway_session.steer_gateway_agent --------------------------------------


def test_steer_reaches_cached_agent():
    agent = FakeSteerAgent()
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("new direction")
    assert out == {"steered": True, "reason": ""}
    assert agent.steers == ["new direction"]


def test_steer_idle_reports_nothing_to_steer():
    out = gs.steer_gateway_agent("hello?")
    assert out["steered"] is False


def test_steer_rejects_empty_text():
    gs._session_agents["gateway"] = FakeSteerAgent()
    out = gs.steer_gateway_agent("   ")
    assert out["steered"] is False


def test_steer_without_surface_reports_miss():
    gs._session_agents["gateway"] = object()
    out = gs.steer_gateway_agent("hi")
    assert out == {"steered": False, "reason": "agent has no steer surface"}


def test_steer_declined_reports_miss():
    class Declining:
        def steer(self, text):
            return False

    gs._session_agents["gateway"] = Declining()
    out = gs.steer_gateway_agent("hi")
    assert out["steered"] is False


def test_steer_does_not_take_turn_lock():
    """Steer must reach the agent holding the turn lock, not queue behind it."""
    agent = FakeSteerAgent()
    gs._session_agents["gateway"] = agent
    lock = gs._session_lock("gateway")
    assert lock.acquire(blocking=False) is True
    try:
        out = gs.steer_gateway_agent("mid-turn note")
    finally:
        lock.release()
    assert out["steered"] is True
    assert agent.steers == ["mid-turn note"]


# --- gateway_transport.steer ---------------------------------------------------


@pytest.mark.asyncio
async def test_transport_steer_sends_verb(monkeypatch):
    seen: dict = {}

    def fake_query(home, verb, params=None, timeout=None):
        seen["verb"] = verb
        seen["params"] = params
        return {"steered": True, "reason": ""}

    import gateway.control_socket as cs

    monkeypatch.setattr(cs, "query_gateway_control", fake_query)
    t = ControlSocketGatewayTransport("/fake/mercury")
    out = await t.steer("redirect now", node_id="gw")
    assert out == {"steered": True, "reason": ""}
    assert seen["verb"] == "steer"
    assert seen["params"]["text"] == "redirect now"


@pytest.mark.asyncio
async def test_transport_steer_empty_never_raises():
    t = ControlSocketGatewayTransport("/fake/mercury")
    out = await t.steer("   ")
    assert out["steered"] is False


@pytest.mark.asyncio
async def test_transport_base_steer_is_miss():
    out = await GatewayTransport().steer("hi")
    assert out["steered"] is False


# --- sidecar: idle still prompts ----------------------------------------------


@pytest.mark.asyncio
async def test_idle_gateway_message_still_prompts(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeSteerTransport(reply="fresh reply")
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
        # Drain the spawned delivery so prompts land before asserting.
        deadline = asyncio.get_running_loop().time() + 5.0
        while daemon._gateway_tasks and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        # Idle: fresh prompt turn, no steer attempt, queued notice skipped.
        assert transport.steers == []
        assert transport.prompts == [("hello?", "steer", gw_id)]
        assert all(c[2] != QUEUED_STEER_NOTICE for c in _sends(daemon.client))
        assert not [t for t in list(daemon._gateway_tasks) if not t.done()]
    finally:
        await daemon.shutdown()


# --- sidecar: mid-turn steer reaches the running turn --------------------------


@pytest.mark.asyncio
async def test_midturn_steer_reaches_running_turn(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeSteerTransport(reply="late", slow=True)
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

        # True steer: verb reached the running turn, no second delivery spawned.
        assert transport.steers == [("turn left", gw_id)]
        assert transport.prompts == [] or transport.prompts == [("slow", "prompt", gw_id)]
        assert len([t for t in list(daemon._gateway_tasks) if not t.done()]) == 1
        # Queued-steer notice is the ack for a mid-turn steer.
        assert any(c[2] == QUEUED_STEER_NOTICE for c in _sends(daemon.client))
        assert f"gateway-steer:{gw_id}" in daemon.routing_log
        slow.cancel()
        try:
            await slow
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_midturn_steer_miss_falls_back_to_interrupt(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeSteerTransport(reply="late", slow=True, steer_ok=False)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()

        slow = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(slow)
        slow.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)
        assert daemon._gateway_delivery_in_flight()

        outcome = SimpleNamespace(
            node_id=gw_id,
            actions=(InjectText(gw_id, "new plan", "steer"),),
            notices=(ControlNotice(gw_id, QUEUED_STEER_NOTICE),),
            disposition="steer",
        )
        await daemon._handle_gateway_prompt_outcome(outcome)
        await asyncio.sleep(0.05)

        # Miss → interrupt-then-inject: old turn interrupted, new prompt queued.
        assert transport.steers == [("new plan", gw_id)]
        assert transport.interrupts == ["matrix steer"]
        assert any(p[0] == "new plan" for p in transport.prompts)
        assert f"gateway-steer-fallback:{gw_id}" in daemon.routing_log
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_midturn_command_does_not_steer(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeSteerTransport(reply="late", slow=True)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()

        slow = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(slow)
        slow.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)

        outcome = SimpleNamespace(
            node_id=gw_id,
            actions=(InjectText(gw_id, "/status", "command"),),
            notices=(),
            disposition="command",
        )
        await daemon._handle_gateway_prompt_outcome(outcome)
        assert transport.steers == []
        slow.cancel()
        try:
            await slow
        except (asyncio.CancelledError, Exception):
            pass
        # Let the fallback command delivery finish, then drop it.
        for t in list(daemon._gateway_tasks):
            if not t.done():
                t.cancel()
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_midturn_quiet_action_takes_fresh_turn(daemon: sm.SidecarDaemon):
    """Quiet parent-continuation flag: never steered, always a fresh turn."""
    await daemon.boot()
    try:
        transport = FakeSteerTransport(reply="late", slow=True)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        seen: list = []
        real_deliver = daemon._deliver_gateway_prompt

        async def _spy(node_id, text, *, kind="prompt", internal=False,
                       room_id=None, quiet=False):
            seen.append({"text": text, "quiet": quiet, "kind": kind})
            return await real_deliver(node_id, text, kind=kind, internal=internal,
                                      room_id=room_id, quiet=quiet)

        daemon._deliver_gateway_prompt = _spy  # type: ignore[method-assign]
        slow = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(slow)
        slow.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)

        outcome = SimpleNamespace(
            node_id=gw_id,
            actions=(InjectText(gw_id, "quiet followup", "steer", True),),
            notices=(),
            disposition="steer",
        )
        await daemon._handle_gateway_prompt_outcome(outcome)
        await asyncio.sleep(0.05)
        assert transport.steers == []
        assert any(s.get("quiet") is True and s.get("text") == "quiet followup" for s in seen)
        slow.cancel()
        try:
            await slow
        except (asyncio.CancelledError, Exception):
            pass
        for t in list(daemon._gateway_tasks):
            if not t.done():
                t.cancel()
    finally:
        await daemon.shutdown()


# --- approval-forward registration still wraps steer-kind turns ----------------


def test_steer_kind_turn_still_registers_approval_forward(monkeypatch):
    calls: list = []
    import tools.approval as approval

    monkeypatch.setattr(approval, "register_gateway_notify",
                        lambda *a, **k: calls.append(("register", a)))
    monkeypatch.setattr(approval, "unregister_gateway_notify",
                        lambda *a, **k: calls.append(("unregister", a)))
    monkeypatch.setattr(approval, "set_current_session_key", lambda *a, **k: None)
    monkeypatch.setattr(approval, "reset_current_session_key", lambda *a, **k: None)

    class Agent:
        def run_conversation(self, text):
            return {"final_response": "ok"}

    gs._session_agents["gateway"] = Agent()
    reply, _ = gs.run_gateway_prompt_with_events("hi", kind="steer", node_id="gw")
    assert reply == "ok"
    assert calls and calls[0][0] == "register"
    assert calls[-1][0] == "unregister"


# --- redirect-then-steer (mid-turn gateway text interrupts) -------------------


class FakeRedirectAgent(FakeSteerAgent):
    """Steer agent with a CLI-style live-request redirect surface."""

    def __init__(self, *, redirect_ok: bool = True, redirect_raises: bool = False):
        super().__init__()
        self.redirects: list[str] = []
        self._redirect_ok = redirect_ok
        self._redirect_raises = redirect_raises

    def redirect(self, text: str):
        self.redirects.append(text)
        if self._redirect_raises:
            raise RuntimeError("redirect boom")
        return self._redirect_ok


def test_steer_redirects_live_turn_first():
    """A live request absorbs the steer via redirect — no double delivery."""
    agent = FakeRedirectAgent(redirect_ok=True)
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("turn left now")
    assert out == {"steered": True, "reason": ""}
    assert agent.redirects == ["turn left now"]
    assert agent.steers == []


def test_steer_falls_back_when_no_live_turn():
    """redirect=False (turn ended in the race) degrades to the steer buffer."""
    agent = FakeRedirectAgent(redirect_ok=False)
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("after all, go right")
    assert out == {"steered": True, "reason": ""}
    assert agent.redirects == ["after all, go right"]
    assert agent.steers == ["after all, go right"]


def test_steer_falls_back_when_redirect_raises():
    """A redirect failure never loses the steer — the buffer still takes it."""
    agent = FakeRedirectAgent(redirect_raises=True)
    gs._session_agents["gateway"] = agent
    out = gs.steer_gateway_agent("steady as she goes")
    assert out == {"steered": True, "reason": ""}
    assert agent.steers == ["steady as she goes"]


def test_steer_miss_when_redirect_declines_and_no_steer_surface():
    """redirect=False with no steer surface still reports a clean miss."""

    class RedirectOnly:
        def redirect(self, text):
            return False

    gs._session_agents["gateway"] = RedirectOnly()
    out = gs.steer_gateway_agent("hi")
    assert out == {"steered": False, "reason": "agent has no steer surface"}
