"""VM fixes 2: BUG1/2/3/5 + double-message + subagent room/stream/purge.

Owned files: e2ee.ensure_room_share, sidecar followup/internal/abort,
renderer 403-skip, gateway_session blockquote dedupe + interrupt.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from observatory import gateway_session as gs
from observatory import sidecar_main as sm
from observatory.control import AbortSession, ControlNotice, InjectText
from observatory.gateway_transport import GatewayTransport
from observatory.state import ObservatoryState


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeMatrixClient:
    homeserver_url: str = "http://127.0.0.1:18008"
    as_token: str = "as-tok"
    server_name: str = "mercury.local"
    admin_token: str = "admin-tok"
    on_admin_401: Any = None
    calls: list = field(default_factory=list)
    next_id: int = 0

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def register_virtual_user(self, localpart: str) -> str:
        self.calls.append(("register", localpart))
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
        self.calls.append(("client_api", method, path, sender, json_body))
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
            if space == room_id:
                continue
            rooms.append({"room_id": space,
                          "children_state": [
                              {"type": "m.space.child", "state_key": c,
                               "origin_server_ts": i} for i, c in enumerate(kids)]})
        return {"rooms": rooms}

    async def close(self) -> None:
        self.calls.append(("close",))

    async def get_power_levels(self, room_id, *, sender):
        return {}

    async def invite(self, room_id, user_id, *, sender):
        self.calls.append(("invite", room_id, user_id, sender))

    async def join_room(self, room_id, *, sender):
        return room_id

    async def leave_room(self, room_id, *, sender):
        pass

    async def delete_room(self, room_id, **kwargs):
        self.calls.append(("delete", room_id))
        return {}


class FakeTransport(GatewayTransport):
    def __init__(self, reply: str = "ok", events=None, error=None):
        self.reply = reply
        self.events = list(events or [])
        self.error = error
        self.prompts: list = []
        self.interrupts: list = []

    async def prompt(self, text, *, kind="prompt", node_id="gw", internal=False):
        self.prompts.append((text, kind, node_id, internal))
        if self.error is not None:
            raise self.error
        return self.reply

    async def prompt_with_events(self, text, *, kind="prompt", node_id="gw", internal=False):
        self.prompts.append((text, kind, node_id, internal))
        if self.error is not None:
            raise self.error
        return self.reply, list(self.events)

    async def interrupt(self, reason="matrix /stop"):
        self.interrupts.append(reason)
        return {"interrupted": True, "reason": reason}

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


async def _drain(d: sm.SidecarDaemon, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while d._gateway_tasks and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not [t for t in list(d._gateway_tasks) if not t.done()]


# --- BUG1: e2ee reshare rotation -------------------------------------------


class _FakeSession:
    def __init__(self, sid="OLDSESSION", shared=True, expired=False):
        self.id = sid
        self.session_id = sid
        self.shared = shared
        self.expired = expired
        self.creation_time = "2026-01-01T00:00:00"


class _FakeCryptoStore:
    def __init__(self, session=None):
        self._session = session
        self.removed: list = []
        self.shared_to: list = []

    async def get_outbound_group_session(self, room_id):
        return self._session

    async def remove_outbound_group_session(self, room_id):
        self.removed.append(str(room_id))
        self._session = None


class _FakeMachine:
    def __init__(self, store):
        self.crypto_store = store
        self.shared: list = []

    async def share_group_session(self, room_id, members):
        self.shared.append((str(room_id), list(members)))
        self.crypto_store._session = _FakeSession(sid="NEWSESSION")


class _FakeCrypto:
    def __init__(self, machine):
        self.machine = machine

    async def load(self):
        return None


@pytest.mark.asyncio
async def test_bug1_rotates_on_first_seen_device():
    from observatory import e2ee as e2ee_mod

    store = _FakeCryptoStore(session=_FakeSession(sid="OLDSESSION"))
    machine = _FakeMachine(store)
    mgr = e2ee_mod.E2EEManager(client=object(), state=object(),
                               crypto_dir="/tmp", owner_mxid="@owner:hs")
    mgr.machine_for = lambda mxid: _FakeCrypto(machine)  # type: ignore[method-assign]
    async def _trust_a(sender):
        return {"trusted": ["NEWD"], "known": [], "refused": [], "fetched": ["NEWD"]}
    mgr.ensure_owner_trust = _trust_a  # type: ignore[method-assign]
    async def _members_a(room_id, **kwargs):
        return ["@owner:hs"]
    mgr._room_members = _members_a  # type: ignore[method-assign]
    report = await mgr.ensure_room_share("!room:hs", "@merc_gw:hs")
    assert store.removed == ["!room:hs"]
    assert report["shared"] == ["!room:hs"]
    assert machine.shared and machine.shared[0][0] == "!room:hs"


@pytest.mark.asyncio
async def test_bug1_rotates_on_changed_keys_refused():
    from observatory import e2ee as e2ee_mod

    store = _FakeCryptoStore(session=_FakeSession(sid="OLDSESSION"))
    machine = _FakeMachine(store)
    mgr = e2ee_mod.E2EEManager(client=object(), state=object(),
                               crypto_dir="/tmp", owner_mxid="@owner:hs")
    mgr.machine_for = lambda mxid: _FakeCrypto(machine)  # type: ignore[method-assign]
    async def _trust_b(sender):
        return {"trusted": [], "known": [], "refused": ["CHANGED"], "fetched": ["CHANGED"]}
    mgr.ensure_owner_trust = _trust_b  # type: ignore[method-assign]
    async def _members_b(room_id, **kwargs):
        return ["@owner:hs"]
    mgr._room_members = _members_b  # type: ignore[method-assign]
    report = await mgr.ensure_room_share("!room:hs", "@merc_gw:hs")
    assert store.removed == ["!room:hs"]
    assert report["shared"] == ["!room:hs"]


@pytest.mark.asyncio
async def test_bug1_no_rotation_when_devices_stable():
    from observatory import e2ee as e2ee_mod

    store = _FakeCryptoStore(session=_FakeSession(sid="OLDSESSION"))
    machine = _FakeMachine(store)
    mgr = e2ee_mod.E2EEManager(client=object(), state=object(),
                               crypto_dir="/tmp", owner_mxid="@owner:hs")
    mgr.machine_for = lambda mxid: _FakeCrypto(machine)  # type: ignore[method-assign]
    async def _trust_c(sender):
        return {"trusted": [], "known": ["D1"], "refused": [], "fetched": ["D1"]}
    mgr.ensure_owner_trust = _trust_c  # type: ignore[method-assign]
    async def _members_c(room_id):
        return ["@owner:hs"]
    mgr._room_members = _members_c  # type: ignore[method-assign]
    report = await mgr.ensure_room_share("!room:hs", "@merc_gw:hs")
    assert store.removed == []
    assert report["shared"] == []
    assert machine.shared == []


# --- BUG2: followup spam gate + truncate + internal -------------------------


def test_bug2_routine_success_skipped():
    assert sm._needs_room_reply("Key rotation verified complete", "completed") is False
    assert sm._needs_room_reply("", "completed") is False


def test_bug2_failure_and_questions_pass():
    assert sm._needs_room_reply("Key rotation verified complete", "failed") is True
    assert sm._needs_room_reply("Done, but one question: approve deploy?", "completed") is True
    assert sm._needs_room_reply("error writing file", "completed") is True


@pytest.mark.asyncio
async def test_bug2_followup_truncates_and_marks_internal(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeTransport(reply="verified ok")
        daemon.gateway_transport = transport
        long_summary = "x" * 600 + " question?"
        await daemon._maybe_post_delegate_followup(
            "deleg/0", daemon._gateway_node_id(), "worker",
            status="completed", summary=long_summary)
        await _drain(daemon)
        assert transport.prompts, "routine+question summary must still inject"
        text, kind, node_id, internal = transport.prompts[0]
        assert internal is True
        assert len(text) < len(long_summary) + 100
        assert "…" in text
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_bug2_routine_followup_continues_quietly(daemon: sm.SidecarDaemon):
    """CLI parity: a routine child result still continues the parent
    turn — quietly (no room-visible message)."""
    await daemon.boot()
    try:
        transport = FakeTransport(reply="ok")
        daemon.gateway_transport = transport
        sends_before = len(_sends(daemon.client))
        await daemon._maybe_post_delegate_followup(
            "deleg/0", daemon._gateway_node_id(), "rotator",
            status="completed", summary="Nightly key rotation verified complete")
        await _drain(daemon)
        assert len(transport.prompts) == 1, "routine result must still continue the parent"
        assert len(_sends(daemon.client)) == sends_before, "quiet turn posts no room message"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_bug2_internal_collapses_live_and_replay(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        daemon.gateway_transport = FakeTransport(
            reply="done",
            events=[{"seq": 0, "type": "tool_call", "tool": "bash", "args": {},
                      "internal": True},
                    {"seq": 1, "type": "thinking", "text": "working",
                      "internal": True}])
        sends_before = len(_sends(daemon.client))
        await daemon._deliver_gateway_prompt(daemon._gateway_node_id(), "followup", internal=True)
        # ONE liveness notice + final reply; zero per-event room messages
        bodies = [c[2] for c in _sends(daemon.client)[sends_before:]]
        assert any("still working" in b for b in bodies)
        assert not any("bash" in b for b in bodies)
        assert bodies[-1] == "done"
        # live datagrams for internal turns record seqs but render nothing
        daemon._gateway_live_seqs[daemon._gateway_node_id()] = set()
        await daemon._handle_turn_progress_datagram(
            {"node_id": daemon._gateway_node_id(), "seq": 7,
             "event": {"type": "tool_call", "tool": "bash", "args": {}},
             "internal": True})
        assert 7 in daemon._gateway_live_seqs[daemon._gateway_node_id()]
        assert len(_sends(daemon.client)) == len(bodies) + sends_before
    finally:
        await daemon.shutdown()


# --- BUG3: /stop abort wiring -----------------------------------------------


def test_bug3_interrupt_calls_hard_cancel():
    gs._session_agents.clear()
    try:
        seen: dict = {}

        class FakeAgent:
            def interrupt(self, reason, hard_cancel=False):
                seen["reason"] = reason
                seen["hard"] = hard_cancel

        gs._session_agents["gateway"] = FakeAgent()
        out = gs.interrupt_gateway_agent("matrix /stop")
        assert out == {"interrupted": True, "reason": "matrix /stop"}
        assert seen == {"reason": "matrix /stop", "hard": True}
    finally:
        gs._session_agents.clear()


def test_bug3_interrupt_idle_reports_nothing_to_stop():
    gs._session_agents.clear()
    try:
        out = gs.interrupt_gateway_agent("matrix /stop")
        assert out["interrupted"] is False
    finally:
        gs._session_agents.clear()


@pytest.mark.asyncio
async def test_bug3_gateway_abort_cancels_tasks_and_confirms(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        transport = FakeTransport(reply="late")
        daemon.gateway_transport = transport
        gw_id = daemon._gateway_node_id()

        async def _slow(text, *, kind="prompt", node_id="gw", internal=False):
            await asyncio.sleep(30)
            return "late", []

        transport.prompt_with_events = _slow  # type: ignore[method-assign]
        task = asyncio.create_task(daemon._deliver_gateway_prompt(gw_id, "slow"))
        daemon._gateway_tasks.add(task)
        task.add_done_callback(daemon._gateway_tasks.discard)
        await asyncio.sleep(0.05)
        assert daemon._gateway_delivery_in_flight()
        outcome = SimpleNamespace(node_id=gw_id,
                                  actions=(AbortSession(gw_id, "matrix /stop"),),
                                  notices=(), disposition="stop")
        handled = await daemon._handle_gateway_abort_outcome(outcome)
        assert handled is True
        assert transport.interrupts == ["matrix /stop"]
        await asyncio.sleep(0.05)
        assert not daemon._gateway_delivery_in_flight()
        bodies = [c[2] for c in _sends(daemon.client)]
        assert any("stop confirmed" in b for b in bodies)
    finally:
        await daemon.shutdown()


# --- BUG5: 403 discovery skip -------------------------------------------------


@pytest.mark.asyncio
async def test_bug5_send_403_skips_room(daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        from observatory.renderer import SendMessage
        from observatory.matrix_client import MatrixError

        assert daemon.renderer is not None and daemon.state is not None
        gw_id = daemon._gateway_node_id()
        real_send = daemon.client.send_message

        async def _send_403(room_id, body, *, sender, formatted_body=None):
            raise MatrixError("PUT", "/send", 403, {"errcode": "M_FORBIDDEN"})

        daemon.client.send_message = _send_403  # type: ignore[method-assign]
        records = await daemon.renderer.executor.execute(
            [SendMessage(gw_id, "@merc_gw:mercury.local", "hello")])
        assert records and records[0]["op"] == "skipped"
        assert records[0]["reason"] == "not-member"
        daemon.client.send_message = real_send
    finally:
        await daemon.shutdown()


# --- FOLLOW-UP A: blockquote-aware double-message dedupe ----------------------


def test_followupA_strip_markup_matches_blockquote():
    assert gs._strip_thinking_markup("> hello world") == "hello world"
    assert gs._strip_thinking_markup("<blockquote><p><em>hello world</em></p></blockquote>") == "hello world"


def test_followupA_thinking_equal_to_reply_dropped_even_blockquoted():
    def _factory(session_id="gateway"):
        class A:
            tool_progress_callback = None
            thinking_callback = None
            reasoning_callback = None

            def run_conversation(self, text):
                if callable(self.thinking_callback):
                    self.thinking_callback("> final answer here")
                return {"final_response": "final answer here"}

        return A()

    reply, events = gs.run_gateway_prompt_with_events(
        "go", agent_factory=lambda sid: _factory(sid),
        turn=lambda agent, text: agent.run_conversation(text))
    assert reply == "final answer here"
    assert [e for e in events if e.get("type") == "thinking"] == []


def test_followupA_distinct_thinking_kept():
    def _factory(session_id="gateway"):
        class A:
            tool_progress_callback = None
            thinking_callback = None
            reasoning_callback = None

            def run_conversation(self, text):
                if callable(self.thinking_callback):
                    self.thinking_callback("scratch reasoning path")
                return {"final_response": "final answer here"}

        return A()

    reply, events = gs.run_gateway_prompt_with_events(
        "go", agent_factory=lambda sid: _factory(sid),
        turn=lambda agent, text: agent.run_conversation(text))
    assert reply == "final answer here"
    assert any(e.get("type") == "thinking" for e in events)


# --- FOLLOW-UP B: subagent room/stream/purge (D8) ------------------------------


@pytest.mark.asyncio
async def test_followupB_grandchild_streams_then_parent_purge_cascades(
        daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None
        gw_id = daemon._gateway_node_id()
        # gateway-origin child (depth 1): datagram start provisions room
        child_id = "deleg-vm2/0"
        await daemon._handle_child_lifecycle_datagram(
            {"node_id": child_id, "lifecycle": "start", "name": "worker",
             "parent_session": "session:gateway"})
        child = daemon.state.get(child_id)
        assert child["room_id"], "child room must exist (no empty room)"
        assert child["depth"] == 1
        child_sends = [c for c in _sends(daemon.client) if c[1] == child["room_id"]]
        assert child_sends, "child lifecycle message must render"
        # grandchild streams tool + thought into its own room
        sub_id = "sub-1"
        await daemon._handle_child_feed_datagram(
            {"node_id": child_id,
             "feed": {"feed": "node", "subagent_id": sub_id, "agent": "helper"}})
        gc_id = f"{child_id}/gc:{sub_id}"
        gc = daemon.state.get(gc_id)
        assert gc["room_id"], "grandchild room must exist"
        await daemon._handle_child_feed_datagram(
            {"node_id": child_id,
             "feed": {"feed": "tool", "subagent_id": sub_id, "tool": "bash",
                      "args": {"cmd": "true"}}})
        await daemon._handle_child_feed_datagram(
            {"node_id": child_id,
             "feed": {"feed": "thought", "subagent_id": sub_id, "text": "reasoning"}})
        gc_sends = [c for c in _sends(daemon.client) if c[1] == gc["room_id"]]
        assert any("bash" in c[2] for c in gc_sends), "tool must stream to grandchild room"
        # grandchild death (depth 2): settles, room survives until parent dies
        await daemon._handle_child_feed_datagram(
            {"node_id": child_id,
             "feed": {"feed": "node", "subagent_id": sub_id, "kind": "death",
                      "status": "completed"}})
        assert daemon.state.get(gc_id)["status"] == "dead"
        # child death (depth 1): D8 instant purge of the whole subtree,
        # summary to the parent (gateway) room only
        gw_room = daemon.state.get(gw_id)["room_id"]
        await daemon._handle_child_lifecycle_datagram(
            {"node_id": child_id, "lifecycle": "stop", "status": "completed",
             "summary": "worker finished all tasks"})
        with pytest.raises(Exception):
            daemon.state.get(child_id)
        with pytest.raises(Exception):
            daemon.state.get(gc_id)
        bodies = [c[2] for c in _sends(daemon.client) if c[1] == gw_room]
        assert any("worker" in b for b in bodies)
        deletes = [c for c in daemon.client.calls if c[0] == "delete"]
        assert deletes, "D8 instant purge must delete rooms/spaces"
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_followupB_tool_feed_without_node_event_not_dropped(
        daemon: sm.SidecarDaemon):
    await daemon.boot()
    try:
        assert daemon.state is not None
        child_id = "deleg-vm2b/0"
        await daemon._handle_child_lifecycle_datagram(
            {"node_id": child_id, "lifecycle": "start", "name": "worker",
             "parent_session": "session:gateway"})
        # tool frame arrives with no prior node frame and no boxes entry,
        # but the row exists (created by another path): must still render.
        sub_id = "sub-orphan"
        gc_id = f"{child_id}/gc:{sub_id}"
        from observatory.identity import assign_slug, virtual_mxid
        slug = assign_slug("orphan", daemon.state)
        daemon.state.add_node(
            gc_id, engine="omp", name="orphan", slug=slug,
            mxid=virtual_mxid(slug, server_name=daemon.server_name),
            session_ref=f"omp-subagent:{sub_id}", parent_node_id=child_id,
            extra={"subagent_id": sub_id})
        await daemon.renderer.apply_plan(
            daemon.renderer.build_plan(host="testhost"))
        await daemon._handle_child_feed_datagram(
            {"node_id": child_id,
             "feed": {"feed": "tool", "subagent_id": sub_id, "tool": "bash",
                      "args": {}}})
        gc_room = daemon.state.get(gc_id)["room_id"]
        assert any(c[1] == gc_room and "bash" in c[2] for c in _sends(daemon.client))
    finally:
        await daemon.shutdown()
