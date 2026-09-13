"""Cross-process live-ingest contract tests (sidecar wave sidecar-ingest-fix).

Covers the datagram path between the gateway process and the sidecar
process (``$MERCURY_HOME/observatory/gateway-progress.sock``):

- turn-progress listener + live render, replay seq-dedupe, empty-reply
  replay fix, post-delegate follow-up nudge (ported from the
  sidecar-ingest wave);
- gateway-child feed WITHOUT any same-process import: the gateway reads
  its OWN ``_live_children`` table and pushes ``child_lifecycle`` /
  ``child_event`` datagrams; the sidecar creates/renders child nodes
  from wire bytes alone;
- gateway push helpers + feed watcher (gateway side of the contract);
- slash pass-through: known verbs reach the live runner's full
  ``_handle_message`` as a Matrix event with the session override.

Owns no production files; reads the daemon through its public seams.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import observatory.sidecar_main as sm
from observatory import gateway_session as gs
from observatory.gateway_transport import (
    GatewayTransport,
    gateway_progress_sock_path,
)
from observatory.state import ObservatoryState, StateError  # noqa: F401 (fixture typing)
from tests.observatory.test_sidecar_main import FakeMatrixClient


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeLiveTransport(GatewayTransport):
    """Canned (reply, events) transport; records prompts."""

    def __init__(self, reply: str = "ok", events=None, error=None):
        self.reply = reply
        self.events = list(events or [])
        self.error = error
        self.prompts: list[tuple[str, str, str]] = []

    async def prompt(self, text, *, kind="prompt", node_id="gw"):
        self.prompts.append((text, kind, node_id))
        if self.error is not None:
            raise self.error
        return self.reply

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw"):
        self.prompts.append((text, kind, node_id))
        if self.error is not None:
            raise self.error
        return self.reply, list(self.events)


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    from observatory.config_gen import ObservatoryPaths

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


async def _drain_gateway_tasks(d: sm.SidecarDaemon, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while d._gateway_tasks and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    pending = [t for t in list(d._gateway_tasks) if not t.done()]
    assert not pending, "gateway delivery task did not finish"


def _gw_room(d: sm.SidecarDaemon) -> str:
    assert d.state is not None
    return d.state.get(sm.GATEWAY_NODE_ID)["room_id"]


# --- live datagram render ----------------------------------------------------


@pytest.mark.asyncio
async def test_live_datagram_renders_tool_and_records_seq(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.renderer is not None and daemon.client is not None
        room = _gw_room(daemon)
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        payload = {
            "node_id": sm.GATEWAY_NODE_ID,
            "seq": 7,
            "event": {"type": "tool_call", "tool": "bash", "args": {"cmd": "ls"}, "seq": 7},
        }
        await daemon._handle_gateway_live_datagram(json.dumps(payload).encode())
        after = [c for c in _sends(daemon.client) if c[1] == room]
        assert len(after) == before + 1
        assert "bash" in after[-1][2]
        assert daemon._gateway_live_seqs.get(sm.GATEWAY_NODE_ID) == {7}
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_live_thinking_gated_by_cot(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        room = _gw_room(daemon)
        daemon.state.set_meta("cot:" + sm.GATEWAY_NODE_ID, "on")
        think = {"node_id": sm.GATEWAY_NODE_ID, "seq": 11,
                 "event": {"type": "thinking", "text": "hmm live", "seq": 11}}
        await daemon._handle_gateway_live_datagram(json.dumps(think).encode())
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room]
        assert any("hmm live" in b for b in bodies)
        # cot off (default) → thinking collapses to status edits, never separate sends
        daemon.state.set_meta("cot:" + sm.GATEWAY_NODE_ID, "off")
        think2 = {"node_id": sm.GATEWAY_NODE_ID, "seq": 12,
                  "event": {"type": "thinking", "text": "hidden thought", "seq": 12}}
        await daemon._handle_gateway_live_datagram(json.dumps(think2).encode())
        bodies2 = [c[2] for c in _sends(daemon.client) if c[1] == room]
        assert not any("hidden thought" in b for b in bodies2)
        assert any(b.endswith("…") for b in bodies2)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_live_socket_real_datagram_roundtrip(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon._gateway_live_enabled
        sock_path = gateway_progress_sock_path(daemon.mercury_home)
        assert sock_path.exists()
        room = _gw_room(daemon)
        assert daemon.client is not None
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            cli.sendto(json.dumps({
                "node_id": sm.GATEWAY_NODE_ID, "seq": 21,
                "event": {"type": "tool_call", "tool": "grep", "args": "foo", "seq": 21},
            }).encode(), str(sock_path))
        finally:
            cli.close()
        for _ in range(100):
            await asyncio.sleep(0.02)
            now = [c for c in _sends(daemon.client) if c[1] == room]
            if len(now) > before:
                break
        now = [c for c in _sends(daemon.client) if c[1] == room]
        assert len(now) == before + 1
        assert "grep" in now[-1][2]
        assert 21 in daemon._gateway_live_seqs.get(sm.GATEWAY_NODE_ID, set())
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_unknown_kind_and_bad_json_ignored(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        before = len(_sends(daemon.client))
        await daemon._handle_gateway_live_datagram(b"not json{")
        await daemon._handle_gateway_live_datagram(
            json.dumps({"kind": "nope", "node_id": "gw"}).encode())
        await daemon._handle_gateway_live_datagram(
            json.dumps({"kind": "child_lifecycle"}).encode())
        assert len(_sends(daemon.client)) == before
        assert daemon.state is not None
    finally:
        await daemon.shutdown()


# --- replay dedupe + delivery clearing ---------------------------------------


@pytest.mark.asyncio
async def test_replay_skips_live_seq_and_renders_unseqed(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        room = _gw_room(daemon)
        daemon._gateway_live_seqs[sm.GATEWAY_NODE_ID] = {1, 2}
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._replay_gateway_events(sm.GATEWAY_NODE_ID, [
            {"type": "tool_call", "tool": "bash", "args": "dup", "seq": 1},
            {"type": "tool_call", "tool": "ls", "args": "fresh", "seq": 3},
            {"type": "tool_call", "tool": "cat"},  # no seq → always renders
        ])
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert not any("dup" in b for b in bodies)
        assert any("fresh" in b for b in bodies)
        assert any("cat" in b for b in bodies)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_deliver_clears_live_set_at_start(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        daemon.gateway_transport = FakeLiveTransport(reply="ok", events=[])
        daemon._gateway_live_seqs[sm.GATEWAY_NODE_ID] = {99}
        await daemon._deliver_gateway_prompt(sm.GATEWAY_NODE_ID, "hi?", kind="prompt")
        assert daemon._gateway_live_seqs.get(sm.GATEWAY_NODE_ID) == set()
    finally:
        await daemon.shutdown()


# --- empty-reply fix ----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_reply_still_replays_events(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        room = _gw_room(daemon)
        daemon.gateway_transport = FakeLiveTransport(
            reply="   ",
            events=[{"type": "tool_call", "tool": "bash", "args": "kept"}],
        )
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._deliver_gateway_prompt(sm.GATEWAY_NODE_ID, "hi?", kind="prompt")
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("kept" in b for b in bodies)  # tool history not dropped
        # default OFF: turn status + tool history, no empty reply render
        assert len(bodies) == 2
        assert any(b.endswith("…") for b in bodies)
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_empty_reply_empty_events_posts_only_status(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.client is not None
        before = len(_sends(daemon.client))
        daemon.gateway_transport = FakeLiveTransport(reply="  ", events=[])
        await daemon._deliver_gateway_prompt(sm.GATEWAY_NODE_ID, "hi?", kind="prompt")
        after = _sends(daemon.client)[before:]
        assert len(after) == 1
        assert after[0][2].endswith("…")
    finally:
        await daemon.shutdown()


# --- bind failure --------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_failure_disables_live_but_replay_works(
    daemon: sm.SidecarDaemon, monkeypatch
):
    _orig_bind = socket.socket.bind

    def _boom(self, *a, **k):
        if getattr(self, "family", None) == socket.AF_UNIX:
            raise OSError("bind taken")
        return _orig_bind(self, *a, **k)

    monkeypatch.setattr(socket.socket, "bind", _boom)
    daemon._start_gateway_live_listener()
    assert daemon._gateway_live_enabled is False
    assert daemon._gateway_live_sock is None
    # batched replay still works after boot
    await daemon.boot()
    try:
        assert daemon.client is not None
        room = _gw_room(daemon)
        before = len([c for c in _sends(daemon.client) if c[1] == room])
        await daemon._replay_gateway_events(sm.GATEWAY_NODE_ID, [
            {"type": "tool_call", "tool": "bash", "args": "via-replay"},
        ])
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == room][before:]
        assert any("via-replay" in b for b in bodies)
    finally:
        await daemon.shutdown()


# --- cross-process gateway-child feed ------------------------------------------
#
# The sidecar MUST NOT import the gateway's in-process table (separate
# processes in production). Regression guard first, then the wire contract.


def test_no_same_process_child_import():
    assert not hasattr(sm.SidecarDaemon, "_ensure_gateway_child_feeds")
    source = inspect.getsource(sm)
    assert "import tools.omp_delegation" not in source
    assert "from tools.omp_delegation" not in source


@pytest.mark.asyncio
async def test_child_start_datagram_creates_node_and_renders(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        child = "deleg_c1/0"
        before = len(_sends(daemon.client))
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "c1-kid", "goal": "do thing",
            "delegation_id": "deleg_c1", "task_index": 0,
        }).encode())
        row = daemon.state.get(child)
        assert row["name"] == "c1-kid"
        assert row["parent_node_id"] == sm.GATEWAY_NODE_ID
        assert row["status"] == "live"
        assert row.get("room_id"), "child node must get a planned room"
        bodies = [c[2] for c in _sends(daemon.client)][before:]
        assert bodies, "lifecycle render must post into the child room"
        # duplicate start (discovery race) never duplicates the node
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "c1-kid",
        }).encode())
        assert daemon.state.get(child)["room_id"] == row["room_id"]
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_child_feed_datagrams_render_grandchildren(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        child = "deleg_c2/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "c2-kid",
        }).encode())
        before = len(_sends(daemon.client))
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "node", "kind": "add", "subagent_id": "sa-1",
                     "status": "running", "agent": "inner",
                     "parent_tool_call_id": None},
        }).encode())
        gc_node = f"{child}/gc:sa-1"
        assert daemon.state.get(gc_node)["status"] == "live"
        daemon.state.set_meta("cot:" + gc_node, "on")
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "tool", "subagent_id": "sa-1",
                     "tool": "bash", "args": "ls -la"},
        }).encode())
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "thought", "subagent_id": "sa-1",
                     "text": "pondering the listing"},
        }).encode())
        bodies = [c[2] for c in _sends(daemon.client)][before:]
        assert any("bash" in b for b in bodies)
        assert any("pondering the listing" in b for b in bodies)
        # grandchild death tombstones the node
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "node", "kind": "death", "subagent_id": "sa-1",
                     "status": "completed"},
        }).encode())
        assert daemon.state.get(gc_node)["status"] != "live"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_child_feed_before_start_builds_stub_not_drop(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        child = "deleg_c3/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_event", "node_id": child,
            "feed": {"feed": "tool", "subagent_id": "sa-9",
                     "tool": "read", "args": "f"},
        }).encode())
        # reorder/stub: node exists so the frame is never dropped …
        assert daemon.state.get(child)["status"] == "live"
        # … and the late start enriches nothing destructively
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "c3-kid",
        }).encode())
        assert daemon.state.get(child)["status"] == "live"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_child_stop_renders_death_and_nudges_when_idle(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        assert daemon.state is not None and daemon.client is not None
        daemon.gateway_transport = FakeLiveTransport(reply="noted", events=[])
        child = "deleg_c4/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "c4-kid",
        }).encode())
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "stop",
            "status": "unknown",
        }).encode())
        try:
            row = daemon.state.get(child)
            assert row["status"] != "live"
        except StateError:
            pass  # D8: depth-1 child rows purge instantly with the death render
        await _drain_gateway_tasks(daemon)
        assert daemon.gateway_transport is not None
        prompts = daemon.gateway_transport.prompts
        assert len(prompts) == 1
        text, kind, node = prompts[0]
        assert kind == "prompt" and node == sm.GATEWAY_NODE_ID
        assert "[subagent c4-kid unknown]" in text
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_child_stop_delivers_nudge_after_short_busy(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    fake = FakeLiveTransport(reply="noted", events=[])
    # Short busy so the followup's bounded wait sees the slot clear and
    # delivers exactly once (never dropped on a busy gateway).
    busy = asyncio.create_task(asyncio.sleep(0.05))
    daemon._gateway_tasks.add(busy)
    busy.add_done_callback(daemon._gateway_tasks.discard)
    try:
        daemon.gateway_transport = fake
        child = "deleg_c5/0"
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "start",
            "name": "c5-kid",
        }).encode())
        await daemon._handle_gateway_live_datagram(json.dumps({
            "kind": "child_lifecycle", "node_id": child, "lifecycle": "stop",
            "status": "unknown",
        }).encode())
        await _drain_gateway_tasks(daemon)
        assert len(fake.prompts) == 1
        text, kind, node = fake.prompts[0]
        assert kind == "prompt" and node == sm.GATEWAY_NODE_ID
        assert "[subagent c5-kid unknown]" in text
    finally:
        busy.cancel()
        try:
            await busy
        except (asyncio.CancelledError, Exception):
            pass
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_discovery_death_nudges_gateway_child(daemon: sm.SidecarDaemon):
    """Discovery-path death under the gateway still posts the follow-up."""
    from observatory.discovery import NodeEvent

    await daemon.boot()
    try:
        assert daemon.state is not None
        fake = FakeLiveTransport(reply="noted", events=[])
        daemon.gateway_transport = fake
        await daemon._apply_discovery_event(NodeEvent(
            kind="add", delegation_id="deleg_d1", task_index=0,
            parent_session="", name="d1-kid", goal="g",
            status="running", source="poll", seq=1,
        ))
        await daemon._apply_discovery_event(NodeEvent(
            kind="death", delegation_id="deleg_d1", task_index=0,
            parent_session="", name="d1-kid", goal="g",
            status="completed", source="poll", seq=2,
            summary="did stuff",
        ))
        await _drain_gateway_tasks(daemon)
        assert len(fake.prompts) == 1
        text, kind, node = fake.prompts[0]
        assert "[subagent d1-kid completed]" in text
        assert "did stuff" in text
        assert node == sm.GATEWAY_NODE_ID
    finally:
        await daemon.shutdown()


# --- post-delegate narration -----------------------------------------------------


@pytest.mark.asyncio
async def test_post_delegate_followup_sends_when_idle_and_waits_when_busy(
    daemon: sm.SidecarDaemon,
):
    await daemon.boot()
    try:
        fake = FakeLiveTransport(reply="noted", events=[])
        daemon.gateway_transport = fake
        gw = sm.GATEWAY_NODE_ID
        # idle → one labeled follow-up inject
        await daemon._maybe_post_delegate_followup(
            "deleg_x/0", gw, "kid-a", status="completed", summary="did stuff")
        await _drain_gateway_tasks(daemon)
        assert len(fake.prompts) == 1
        text, kind, node = fake.prompts[0]
        assert kind == "prompt" and node == gw
        assert "[subagent kid-a completed]" in text
        assert "did stuff" in text
        # busy → waited: first the in-flight slot was busy, then the wait saw
        # the slot clear and delivered exactly one followup (never dropped)
        fake.prompts.clear()
        busy = asyncio.create_task(asyncio.sleep(0.05))
        daemon._gateway_tasks.add(busy)
        busy.add_done_callback(daemon._gateway_tasks.discard)
        try:
            await daemon._maybe_post_delegate_followup(
                "deleg_y/0", gw, "kid-b", status="completed", summary="more")
            await _drain_gateway_tasks(daemon)
            assert len(fake.prompts) == 1
            text, kind, node = fake.prompts[0]
            assert kind == "prompt" and node == gw
            assert "[subagent kid-b completed]" in text
            assert "more" in text
        finally:
            busy.cancel()
            try:
                await busy
            except (asyncio.CancelledError, Exception):
                pass
        # non-gateway parent → never nudges
        fake.prompts.clear()
        await daemon._maybe_post_delegate_followup(
            "orch-1/sa", "orch-1", "kid-c", status="completed", summary="x")
        await asyncio.sleep(0.05)
        assert fake.prompts == []
    finally:
        await daemon.shutdown()


# --- gateway-side push contract ---------------------------------------------------
#
# Bytes on the socket, no shared memory: the sidecar could be another host.


def test_child_transport_feedable_probe():
    class _FeedableClient:
        def on_unknown_notification(self, listener):
            return lambda: None

    class _Feedable:
        def __init__(self):
            self._client = _FeedableClient()

        def set_subagent_subscription(self, level):
            return None

    assert gs._child_transport_feedable(_Feedable()) is True
    assert gs._child_transport_feedable(object()) is False
    assert gs._child_transport_feedable(None) is False


class _FakeRpcClient:
    """Vendored-RpcClient-shaped frame source (real OmpRpcChild surface)."""

    def __init__(self, outer: "_FakeRpcChild"):
        self._outer = outer

    def on_unknown_notification(self, listener):
        self._outer.listener = listener
        return lambda: None


async def _arecv(sock: socket.socket, timeout: float = 5.0) -> dict:
    """One datagram off the loop (blocking recv would starve the watcher)."""
    loop = asyncio.get_running_loop()
    raw = await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout)
    return json.loads(raw.decode("utf-8"))


class _FakeRpcChild:
    """Minimal OmpFeed surface: direct subagent RPC + _client frames."""

    def __init__(self):
        self.listener = None
        self.sub_level = None
        self._client = _FakeRpcClient(self)

    def set_subagent_subscription(self, level):
        self.sub_level = level
        return {}


@pytest.fixture()
def gw_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    return tmp_path


def _bind_progress(gw_home: Path) -> socket.socket:
    path = gw_home / "observatory" / "gateway-progress.sock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    sock.settimeout(2.0)
    return sock


def test_push_child_lifecycle_envelope(gw_home: Path):
    sock = _bind_progress(gw_home)
    try:
        gs.push_child_lifecycle(
            "deleg_p/0", "start", name="p-kid", goal="g",
            delegation_id="deleg_p", task_index=0, parent_session="sess-1",
        )
        gs.push_child_lifecycle("deleg_p/0", "stop", status="unknown")
        first = json.loads(sock.recv(65535).decode("utf-8"))
        second = json.loads(sock.recv(65535).decode("utf-8"))
    finally:
        sock.close()
    assert first == {
        "kind": "child_lifecycle", "node_id": "deleg_p/0",
        "lifecycle": "start", "name": "p-kid", "goal": "g",
        "delegation_id": "deleg_p", "task_index": 0,
        "parent_session": "sess-1",
    }
    assert second == {
        "kind": "child_lifecycle", "node_id": "deleg_p/0",
        "lifecycle": "stop", "status": "unknown",
    }


def test_push_child_feed_event_envelope(gw_home: Path):
    sock = _bind_progress(gw_home)
    try:
        gs.push_child_feed_event(
            "deleg_p/1", {"feed": "tool", "subagent_id": "sa-1",
                          "tool": "bash", "args": "ls"})
        msg = json.loads(sock.recv(65535).decode("utf-8"))
    finally:
        sock.close()
    assert msg == {
        "kind": "child_event", "node_id": "deleg_p/1",
        "feed": {"feed": "tool", "subagent_id": "sa-1",
                 "tool": "bash", "args": "ls"},
    }


def test_feed_event_to_dict_translation():
    from observatory.omp_feed import (
        MessageEvent,
        NodeEvent,
        ThoughtEvent,
        ToolEvent,
    )

    node = gs._feed_event_to_dict(NodeEvent(
        kind="add", subagent_id="sa-1", parent_tool_call_id=None,
        status="running", agent="kid", task="do", session_file=None))
    assert node is not None and node["feed"] == "node"
    assert node["subagent_id"] == "sa-1"

    tool = gs._feed_event_to_dict(ToolEvent(
        subagent_id="sa-1", tool="bash", args="ls"))
    assert tool is not None and tool["feed"] == "tool"

    thought = gs._feed_event_to_dict(ThoughtEvent(subagent_id="sa-1", text="hmm"))
    assert thought is not None and thought["feed"] == "thought"

    # message frames forward (subagent rooms stream text live); junk skipped
    message = gs._feed_event_to_dict(MessageEvent(
        subagent_id="sa-1", role="assistant", text="hi"))
    assert message is not None and message["feed"] == "message"
    assert message["text"] == "hi"
    assert gs._feed_event_to_dict({"nope": True}) is None
    assert gs._feed_event_to_dict(None) is None


@pytest.mark.asyncio
async def test_watcher_pushes_start_frames_stop(gw_home: Path, monkeypatch):
    sock = _bind_progress(gw_home)
    sock.setblocking(False)
    fake = _FakeRpcChild()
    table: dict[str, dict] = {
        "deleg_w/0": {
            "child_id": "deleg_w/0", "delegation_id": "deleg_w",
            "task_index": 0, "name": "w-kid", "goal": "w goal",
            "transport": fake, "transport_kind": "rpc", "steerable": True,
            "owner_session_id": "sess-9",
        }
    }
    monkeypatch.setattr(gs, "_snapshot_live_children", lambda: dict(table))
    watcher = asyncio.create_task(gs._child_watcher_async(0.02))
    try:
        start = await _arecv(sock)
        assert start["kind"] == "child_lifecycle"
        assert start["lifecycle"] == "start"
        assert start["node_id"] == "deleg_w/0"
        assert start["name"] == "w-kid"
        assert start["parent_session"] == "sess-9"
        for _ in range(250):
            if fake.listener is not None and fake.sub_level == "events":
                break
            await asyncio.sleep(0.02)
        assert fake.sub_level == "events"
        assert fake.listener is not None
        # lifecycle frame → forwarded node event
        fake.listener(SimpleNamespace(payload={
            "type": "subagent_lifecycle",
            "payload": {"id": "sa-7", "status": "started",
                        "agent": "inner", "description": "job"},
        }))
        node_msg = await _arecv(sock)
        assert node_msg["kind"] == "child_event"
        assert node_msg["node_id"] == "deleg_w/0"
        assert node_msg["feed"]["feed"] == "node"
        assert node_msg["feed"]["subagent_id"] == "sa-7"
        # progress frame → forwarded tool event
        fake.listener(SimpleNamespace(payload={
            "type": "subagent_progress",
            "payload": {"progress": {"id": "sa-7", "currentTool": "bash",
                                     "currentToolArgs": "ls"}},
        }))
        tool_msg = await _arecv(sock)
        assert tool_msg["feed"]["feed"] == "tool"
        assert tool_msg["feed"]["tool"] == "bash"
        # disappearance → stop (status honestly unknown: the table
        # carries no terminal state)
        table.clear()
        stop = await _arecv(sock)
        assert stop == {"kind": "child_lifecycle", "node_id": "deleg_w/0",
                        "lifecycle": "stop", "status": "unknown"}
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        sock.close()


@pytest.mark.asyncio
async def test_watcher_lifecycle_only_for_oneshot_child(gw_home: Path, monkeypatch):
    """Kill-only Popen children (no RPC surface) still push lifecycle."""
    sock = _bind_progress(gw_home)
    sock.setblocking(False)
    table: dict[str, dict] = {
        "deleg_1/0": {
            "child_id": "deleg_1/0", "name": "one-shot",
            "transport": object(), "transport_kind": "oneshot",
            "steerable": False,
        }
    }
    monkeypatch.setattr(gs, "_snapshot_live_children", lambda: dict(table))
    watcher = asyncio.create_task(gs._child_watcher_async(0.02))
    try:
        start = await _arecv(sock)
        assert (start["lifecycle"], start["node_id"]) == ("start", "deleg_1/0")
        table.clear()
        stop = await _arecv(sock)
        assert (stop["lifecycle"], stop["status"]) == ("stop", "unknown")
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
        sock.close()


# --- end-to-end over the real socket ----------------------------------------------
#
# Gateway push bytes → sidecar listener → child node. No shared memory.


@pytest.mark.asyncio
async def test_gateway_to_sidecar_child_start_over_socket(
    daemon: sm.SidecarDaemon, monkeypatch,
):
    await daemon.boot()
    try:
        assert daemon.state is not None
        monkeypatch.setenv("MERCURY_HOME", str(daemon.mercury_home))
        child = "deleg_e2e/0"
        gs.push_child_lifecycle(child, "start", name="e2e-kid", goal="g",
                                delegation_id="deleg_e2e", task_index=0)
        for _ in range(100):
            await asyncio.sleep(0.02)
            try:
                row = daemon.state.get(child)
                if row["status"] == "live":
                    break
            except Exception:
                pass
        assert daemon.state.get(child)["name"] == "e2e-kid"
    finally:
        await daemon.shutdown()


# --- slash pass-through ------------------------------------------------------------
#
# Known verbs reach the live runner's full _handle_message as a Matrix
# event with the session override; unknown verbs and missing runners
# fall back to a turn (None).


class _FakeRunner:
    def __init__(self, reply="cmd-out"):
        self.reply = reply
        self.events: list = []

    def _session_key_for_source(self, source):
        return "matrix-test-key"

    async def _handle_message(self, event):
        self.events.append(event)
        return self.reply


def test_passthrough_unknown_verb_falls_back_to_turn():
    assert gs._dispatch_slash_command("/definitely-not-a-command-xyz") is None


def test_passthrough_needs_live_runner(monkeypatch):
    monkeypatch.setattr(gs, "_live_runner", lambda: None)
    assert gs._dispatch_slash_command("/version") is None


def test_passthrough_calls_runner_with_matrix_event_and_override(monkeypatch):
    from gateway.config import Platform

    fake = _FakeRunner(reply="v0.0.30")
    monkeypatch.setattr(gs, "_live_runner", lambda: fake)
    out = gs._dispatch_slash_command("/version")
    assert out == "v0.0.30"
    assert len(fake.events) == 1
    event = fake.events[0]
    assert event.text == "/version"
    assert event.source.platform == Platform.MATRIX
    assert event.source.chat_type == "dm"
    assert event.internal is True
    assert event.metadata["gateway_session_id"] == "gateway"
    assert event.metadata["gateway_session_key"] == "matrix-test-key"


def test_passthrough_none_reply_stringifies_empty(monkeypatch):
    fake = _FakeRunner(reply=None)
    monkeypatch.setattr(gs, "_live_runner", lambda: fake)
    assert gs._dispatch_slash_command("/version") == ""


@pytest.mark.asyncio
async def test_passthrough_running_loop_falls_back_to_turn(monkeypatch):
    fake = _FakeRunner(reply="late")
    monkeypatch.setattr(gs, "_live_runner", lambda: fake)
    # this test itself runs inside a running loop → nested run refused
    assert gs._dispatch_slash_command("/version") is None
    assert fake.events == []
