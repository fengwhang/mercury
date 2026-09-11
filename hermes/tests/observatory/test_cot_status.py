"""Single status message per gateway turn (/cot default OFF).

Laws:
- /cot defaults OFF (rooms show tool calls + status, not raw traces).
- /cot on restores separate quoted thinking messages.
- Thinking collapses into edits of ONE status event (same event id).
- Seal: short reply edits the status into the reply; long replies leave
  the status and send separately.
- Status strings come ONLY from display.py thinking faces/verbs.
"""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

import observatory.sidecar_main as sm
from observatory.config_gen import ObservatoryPaths
from observatory.control import COT_META_PREFIX, ControlRouter
from observatory.state import ObservatoryState
from tests.observatory.test_sidecar_main import FakeMatrixClient


def _faces() -> list:
    for holder in ("Display", "KawaiiSpinner"):
        try:
            mod = __import__("agent.display", fromlist=[holder])
            cls = getattr(mod, holder, None)
            if cls is None:
                continue
            get = getattr(cls, "get_thinking_faces", None)
            if callable(get):
                out = get()
                if out:
                    return list(out)
            base = list(getattr(cls, "KAWAII_THINKING", []) or [])
            if base:
                return base
        except Exception:
            continue
    return []


def _verbs() -> list:
    for holder in ("Display", "KawaiiSpinner"):
        try:
            mod = __import__("agent.display", fromlist=[holder])
            cls = getattr(mod, holder, None)
            if cls is None:
                continue
            get = getattr(cls, "get_thinking_verbs", None)
            if callable(get):
                out = get()
                if out:
                    return list(out)
            base = list(getattr(cls, "THINKING_VERBS", []) or [])
            if base:
                return base
        except Exception:
            continue
    return []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
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
def daemon(fake_home: Path, monkeypatch) -> sm.SidecarDaemon:
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port(), e2ee=False)
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


def _sends(client) -> list:
    return [c for c in client.calls if c[0] == "send"]


def _edits(client) -> list:
    return [c for c in client.calls if c[0] == "edit"]


def _gw_room(d: sm.SidecarDaemon) -> str:
    assert d.state is not None
    return d.state.get(sm.GATEWAY_NODE_ID)["room_id"]


class FakeLiveTransport:
    def __init__(self, reply: str = "ok", events=None):
        self.reply = reply
        self.events = list(events or [])

    async def prompt(self, text, *, kind="prompt", node_id="gw", **kw):
        return self.reply

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw", **kw):
        return self.reply, list(self.events)


# --- status text: only Display strings, deterministic ---------------------------


def test_status_text_uses_only_display_strings():
    faces = _faces()
    verbs = _verbs()
    assert faces and verbs
    for seq in range(32):
        body = sm.cot_status_text(seq)
        assert body.endswith("…")
        assert any(f in body for f in faces)
        assert any(v in body for v in verbs)
    assert sm.cot_status_text(0) == sm.cot_status_text(0)
    assert sm.cot_status_text(0) == sm.cot_status_text(len(faces) * len(verbs))
    assert sm.cot_status_text(0) != sm.cot_status_text(1)


def test_no_random_import_in_sidecar():
    src = Path(sm.__file__).read_text(encoding="utf-8")
    assert "import random" not in src


def test_seal_short_boundary():
    assert sm.cot_status_seal_short("ok") is True
    assert sm.cot_status_seal_short("   ") is False
    assert sm.cot_status_seal_short("") is False
    assert sm.cot_status_seal_short("x" * sm.COT_STATUS_SEAL_SHORT_MAX_CHARS) is True
    assert sm.cot_status_seal_short("x" * (sm.COT_STATUS_SEAL_SHORT_MAX_CHARS + 1)) is False


# --- default OFF -----------------------------------------------------------------


def test_cot_defaults_off(tmp_path: Path):
    state = ObservatoryState(tmp_path / "state.db")
    try:
        router = ControlRouter(state, gateway_node_id="gw", pl_provider=lambda _: None)
        assert router.cot_enabled("gw") is False
        state.set_meta(COT_META_PREFIX + "gw", "on")
        assert router.cot_enabled("gw") is True
        state.set_meta(COT_META_PREFIX + "gw", "off")
        assert router.cot_enabled("gw") is False
    finally:
        state.close()


# --- /cot on restores separate quoted traces --------------------------------------


@pytest.mark.asyncio
async def test_cot_on_restores_thinking_traces(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        room = _gw_room(daemon)
        daemon.state.set_meta(COT_META_PREFIX + sm.GATEWAY_NODE_ID, "on")
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        payload = {"node_id": sm.GATEWAY_NODE_ID, "seq": 41,
                   "event": {"type": "thinking", "text": "trace-me-on", "seq": 41}}
        await daemon._handle_gateway_live_datagram(json.dumps(payload).encode())
        after = [c for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("trace-me-on" in c[2] for c in after)
        assert daemon._cot_status_event.get(sm.GATEWAY_NODE_ID) is None
    finally:
        await daemon.shutdown()

@pytest.mark.asyncio
async def test_thinking_hidden_when_off_status_instead(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        faces, verbs = _faces(), _verbs()
        room = _gw_room(daemon)
        assert daemon.control_router.cot_enabled(sm.GATEWAY_NODE_ID) is False
        payload = {"node_id": sm.GATEWAY_NODE_ID, "seq": 42,
                   "event": {"type": "thinking", "text": "hidden-trace-off", "seq": 42}}
        await daemon._handle_gateway_live_datagram(json.dumps(payload).encode())
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room]
        assert not any("hidden-trace-off" in b for b in bodies)
        assert any(b.endswith("…") and any(f in b for f in faces)
                   and any(v in b for v in verbs) for b in bodies)
    finally:
        await daemon.shutdown()


# --- single status: edits share one event id ---------------------------------------


@pytest.mark.asyncio
async def test_status_edits_share_same_event_id(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        faces, verbs = _faces(), _verbs()
        eid = await daemon._cot_status_post(sm.GATEWAY_NODE_ID, 0)
        assert eid
        sends_before = len(_sends(daemon.client))
        assert await daemon._cot_status_edit(sm.GATEWAY_NODE_ID, 1) is True
        assert await daemon._cot_status_edit(sm.GATEWAY_NODE_ID, 2) is True
        assert len(_sends(daemon.client)) == sends_before
        edits = _edits(daemon.client)
        assert len(edits) >= 2
        assert all(e[2] == eid for e in edits[-2:])
        for e in edits[-2:]:
            assert e[3].endswith("…")
            assert any(f in e[3] for f in faces)
            assert any(v in e[3] for v in verbs)
        assert sm.cot_status_text(1) in [e[3] for e in edits[-2:]]
    finally:
        await daemon.shutdown()


# --- seal: short edits into reply, long sends separately ----------------------------


@pytest.mark.asyncio
async def test_seal_short_edits_status_into_reply(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        room = _gw_room(daemon)
        daemon.gateway_transport = FakeLiveTransport(
            reply="done", events=[{"type": "thinking", "text": "scratch", "seq": 0}])
        before_sends = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._deliver_gateway_prompt(sm.GATEWAY_NODE_ID, "go?", kind="prompt")
        sends = [c for c in _sends(daemon.client) if c[1] == room][before_sends:]
        edits = [c for c in _edits(daemon.client)
                 if c[1] == room]
        assert len(sends) == 1
        assert not any("scratch" in c[2] for c in sends)
        assert not any(c[2] == "done" for c in sends)
        assert edits and edits[-1][3] == "done"
        assert daemon._cot_status_event.get(sm.GATEWAY_NODE_ID) is None
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_seal_long_leaves_status_and_sends_reply(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        faces, verbs = _faces(), _verbs()
        room = _gw_room(daemon)
        long_reply = "L" * (sm.COT_STATUS_SEAL_SHORT_MAX_CHARS + 50)
        daemon.gateway_transport = FakeLiveTransport(
            reply=long_reply, events=[{"type": "thinking", "text": "scratch", "seq": 3}])
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._deliver_gateway_prompt(sm.GATEWAY_NODE_ID, "go?", kind="prompt")
        sends = [c for c in _sends(daemon.client) if c[1] == room][before:]
        assert any(c[2] == long_reply for c in sends)
        status = [c for c in sends if c[2] != long_reply]
        assert status
        assert any(c[2].endswith("…") and any(f in c[2] for f in faces)
                   and any(v in c[2] for v in verbs) for c in status)
        assert not any("scratch" in c[2] for c in sends)
        assert daemon._cot_status_event.get(sm.GATEWAY_NODE_ID) is None
    finally:
        await daemon.shutdown()
