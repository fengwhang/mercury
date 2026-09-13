"""Delegate-followup drop-on-busy regression (sidecar lane).

Law: a completed subagent ALWAYS continues its gateway parent. When the
gateway is busy and the mid-turn steer misses, the followup must wait
(bounded) for the in-flight turn and then inject as a normal followup
turn — never log-and-drop. Quiet continuations still run the turn
(``quiet`` preserved); only the room reply is skipped.
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import pytest

import observatory.sidecar_main as sm
from observatory import gateway_session as gs
from observatory.gateway_transport import GatewayTransport
from tests.observatory.test_sidecar_main import FakeMatrixClient


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeBusyTransport(GatewayTransport):
    """Fast prompt recorder; steer always misses (or hits when told)."""

    def __init__(self, reply: str = "late", *, steer_ok: bool = False):
        self.reply = reply
        self.steer_ok = steer_ok
        self.prompts: list = []
        self.steers: list = []

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw",
                                 room_id=None, internal=False):
        self.prompts.append((text, kind, node_id))
        return self.reply, []

    async def steer(self, text: str, *, node_id: str = "gw"):
        self.steers.append((text, node_id))
        if self.steer_ok:
            return {"steered": True, "reason": ""}
        return {"steered": False, "reason": "miss"}

    async def interrupt(self, reason: str = "matrix /stop"):
        return {"interrupted": True, "reason": reason}


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


async def _drain(d: sm.SidecarDaemon, timeout: float = 10.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        pending = [t for t in list(d._gateway_tasks | d._child_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.sleep(0.05)
    pending = [t for t in list(d._gateway_tasks | d._child_tasks) if not t.done()]
    assert not pending, "delivery task did not finish"


def _spy_deliver(daemon: sm.SidecarDaemon) -> list:
    """Record _deliver_gateway_prompt calls (incl. quiet) then run it."""
    calls: list = []
    orig = daemon._deliver_gateway_prompt

    async def _spy(node_id, text, *, kind="prompt", internal=False,
                   room_id=None, quiet=False):
        calls.append({"node_id": node_id, "text": text, "kind": kind,
                      "internal": internal, "quiet": quiet})
        await orig(node_id, text, kind=kind, internal=internal,
                   room_id=room_id, quiet=quiet)

    daemon._deliver_gateway_prompt = _spy  # type: ignore[method-assign]
    return calls


def _busy_task(daemon: sm.SidecarDaemon, delay: float) -> asyncio.Task:
    busy = asyncio.create_task(asyncio.sleep(delay))
    daemon._gateway_tasks.add(busy)
    busy.add_done_callback(daemon._gateway_tasks.discard)
    return busy


@pytest.mark.asyncio
async def test_busy_steer_miss_waits_then_injects_exactly_once(
    daemon: sm.SidecarDaemon,
):
    """Steer miss + finishing turn → followup injected once, not dropped."""
    await daemon.boot()
    try:
        daemon.gateway_transport = FakeBusyTransport(reply="late", steer_ok=False)
        gw_id = daemon._gateway_node_id()
        calls = _spy_deliver(daemon)
        busy = _busy_task(daemon, 0.3)
        assert daemon._gateway_delivery_in_flight()

        await daemon._maybe_post_delegate_followup(
            "deleg/1", gw_id, "worker", status="completed",
            summary="error: build failed, fix needed",
        )
        await busy
        await _drain(daemon)

        assert len(calls) == 1, f"followup dropped on busy gateway: {calls}"
        assert "[subagent worker completed]" in calls[0]["text"]
        assert "build failed" in calls[0]["text"]
        assert "verify the result" in calls[0]["text"]
        assert calls[0]["quiet"] is False
        assert "gateway-followup-wait:deleg/1" in daemon.routing_log
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_busy_steer_miss_quiet_continuation_still_runs(
    daemon: sm.SidecarDaemon,
):
    """Routine success stays quiet but the parent turn still runs."""
    await daemon.boot()
    try:
        daemon.gateway_transport = FakeBusyTransport(reply="late", steer_ok=False)
        gw_id = daemon._gateway_node_id()
        calls = _spy_deliver(daemon)
        busy = _busy_task(daemon, 0.2)
        assert daemon._gateway_delivery_in_flight()

        await daemon._maybe_post_delegate_followup(
            "deleg/2", gw_id, "worker", status="completed",
            summary="Nightly key rotation verified complete",
        )
        await busy
        await _drain(daemon)

        assert len(calls) == 1, f"quiet followup dropped on busy gateway: {calls}"
        assert "verify the result" in calls[0]["text"]
        assert calls[0]["quiet"] is True
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_never_idle_gateway_still_injects_bounded(
    daemon: sm.SidecarDaemon,
):
    """A turn that never ends must not hang the followup forever."""
    await daemon.boot()
    busy = None
    try:
        daemon.gateway_transport = FakeBusyTransport(reply="late", steer_ok=False)
        gw_id = daemon._gateway_node_id()
        calls = _spy_deliver(daemon)
        busy = _busy_task(daemon, 30.0)
        assert daemon._gateway_delivery_in_flight()

        start = time.monotonic()
        await daemon._followup_to_gateway(
            "deleg/9", gw_id,
            "[subagent worker completed] stuck verify the result and reply to the room",
            quiet=True, busy_wait_s=0.3,
        )
        elapsed = time.monotonic() - start
        busy.cancel()
        try:
            await busy
        except (asyncio.CancelledError, Exception):
            pass
        busy = None
        await _drain(daemon)

        assert elapsed < 10.0, f"followup blocked on busy gateway for {elapsed:.1f}s"
        assert len(calls) == 1, f"followup dropped on stuck gateway: {calls}"
        assert calls[0]["quiet"] is True
    finally:
        if busy is not None:
            busy.cancel()
            try:
                await busy
            except (asyncio.CancelledError, Exception):
                pass
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_steer_hit_still_wins_without_inject(
    daemon: sm.SidecarDaemon,
):
    """Steer hit → steered only; no second inject (exactly once)."""
    await daemon.boot()
    busy = None
    try:
        transport = FakeBusyTransport(reply="late", steer_ok=True)
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()
        calls = _spy_deliver(daemon)
        busy = _busy_task(daemon, 30.0)

        await daemon._maybe_post_delegate_followup(
            "deleg/3", gw_id, "worker", status="completed",
            summary="error: build failed, fix needed",
        )

        assert transport.steers, "busy-gateway followup must steer first"
        assert calls == [], f"steer hit must not also inject: {calls}"
        assert "gateway-followup-steer:deleg/3" in daemon.routing_log
        assert not any(e.startswith("gateway-followup-wait:") for e in daemon.routing_log)
    finally:
        if busy is not None:
            busy.cancel()
            try:
                await busy
            except (asyncio.CancelledError, Exception):
                pass
        await daemon.shutdown()
