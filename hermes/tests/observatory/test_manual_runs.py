"""Contract tests for manual omp runs (M5b, D14).

Session files are written by the test in the REAL omp session JSONL format
(title slot + session header + message entries with thinking/toolCall/text
blocks — shapes verified against live sessions under
$PI_CODING_AGENT_DIR/sessions). Laws under test: discovery scope (top-level
sessions only, advisor/backup/artifact files excluded); discovery-owned
sessions never duplicated; tailing yields the SAME typed events omp_feed
emits (isinstance-checked against classes imported from the real module);
read-only topic marker; 24h-quiet + process-gone reap with admin DELETE
intents and row drop; partial lines buffered; never touching the session
files themselves.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from observatory import tree
from observatory.identity import assign_slug, virtual_mxid
from observatory.manual_runs import (
    DEFAULT_QUIET_WINDOW,
    MANUAL_NODE_PREFIX,
    READONLY_TOPIC,
    ManualRunsWatcher,
    SessionParser,
    process_holds_session,
    read_header,
    scan_sessions,
    session_key,
)
from observatory.omp_feed import MessageEvent, ThoughtEvent, ToolEvent
from observatory.renderer import (
    IntentExecutor,
    PurgeRoom,
    Renderer,
    SendMessage,
    SetUserPower,
)
from observatory.state import ObservatoryState, StateError

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"

UUID = "01a07c1d-6d3c-7158-8e91-6236156aca04"
STEM = f"2026-09-07T13-44-58-428Z_{UUID}"


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")
    slug = assign_slug("gateway agent", state)
    state.add_node(
        GW, engine="hermes", name="gateway agent", slug=slug,
        mxid=virtual_mxid(slug), session_ref="session:gw", extra={"kind": "gateway"},
    )
    return state


@dataclass
class FakeClient:
    calls: list = field(default_factory=list)
    _n: int = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def create_room(self, *, name, sender, preset=None, invite=(), space=False):
        self.calls.append(("create_room", name, sender, space))
        return self._id("!room")

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users)))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender))
        return self._id("$ev")

    async def send_state_event(self, room_id, event_type, state_key, content, *, sender):
        self.calls.append(("state", room_id, event_type, dict(content), sender))

    async def delete_room(self, room_id, *, block=False, purge=True):
        self.calls.append(("delete", room_id))

    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        return {"rooms": [{"room_id": room_id, "children_state": []}]}


def write_session(
    agent_dir: Path,
    stem: str = STEM,
    *,
    title: str | None = "fix the config bug",
    lines: list[dict] | None = None,
    cwd_dir: str = "-tmp",
) -> Path:
    """A real-shaped omp session file: title slot, session header, entries."""
    d = agent_dir / "sessions" / cwd_dir
    d.mkdir(parents=True, exist_ok=True)
    pad = " " * max(0, 256 - len(title or ""))
    out = [
        {"type": "title", "v": 1, "title": title or "", "updatedAt": "2026-09-07T13:44:58.428Z", "pad": pad},
        {"type": "session", "version": 3, "id": session_key_from(stem),
         "timestamp": "2026-09-07T13:44:58.428Z", "cwd": "/tmp", "title": title},
    ]
    out.extend(lines or [])
    path = d / f"{stem}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in out), encoding="utf-8")
    return path


def session_key_from(stem: str) -> str:
    _, _, rest = stem.partition("_")
    return rest or stem


def user_msg(text: str) -> dict:
    return {"type": "message", "id": "u1", "parentId": None,
            "timestamp": "2026-09-07T13:45:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant_msg(*blocks: dict) -> dict:
    return {"type": "message", "id": "a1", "parentId": "u1",
            "timestamp": "2026-09-07T13:45:05.000Z",
            "message": {"role": "assistant", "content": list(blocks)}}


def make_watcher(
    tmp_path: Path,
    agent_dir: Path,
    *,
    executor: bool = False,
    clock=None,
    process_probe=None,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    # This file suites the OPTED-IN mirroring behavior (the constructor
    # defaults to off); mirroring-off is covered by test_mirror_cli.py.
    mode: str = "full",
):
    state = seed_state(tmp_path)
    client = FakeClient()
    ex = (
        IntentExecutor(client, state, owner_mxid=OWNER, server_name=SERVER)
        if executor
        else None
    )
    renderer = Renderer(
        state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER, executor=ex
    )
    return (
        ManualRunsWatcher(
            renderer,
            agent_dir=agent_dir,
            clock=clock or (lambda: 1_000_000.0),
            process_probe=process_probe,
            quiet_window=quiet_window,
            mode=mode,
        ),
        state,
        client,
    )


# --- discovery scope ------------------------------------------------------------------


class TestDiscovery:
    def test_scan_sessions_top_level_only(self, tmp_path):
        agent = tmp_path / "omp"
        write_session(agent, stem="s1_11111111-1111-1111-1111-111111111111")
        # artifact dir nested under the session's own stem: excluded
        art = agent / "sessions" / "-tmp" / "s1_11111111-1111-1111-1111-111111111111"
        art.mkdir(parents=True)
        (art / "subagent.jsonl").write_text("{}", encoding="utf-8")
        # advisor transcript + backup: excluded
        (agent / "sessions" / "-tmp" / "__advisor.jsonl").write_text("{}", encoding="utf-8")
        (agent / "sessions" / "-tmp" / f"{STEM}.bak.jsonl").write_text("{}", encoding="utf-8")
        files = scan_sessions(agent)
        assert len(files) == 1 and files[0].stem == "s1_11111111-1111-1111-1111-111111111111"

    def test_missing_sessions_root_is_empty(self, tmp_path):
        assert scan_sessions(tmp_path / "nothing") == []

    def test_session_key_is_uuid(self):
        assert session_key(Path(f"/x/{STEM}.jsonl")) == UUID
        assert session_key(Path("/x/handnamed.jsonl")) == "handnamed"

    def test_read_header_finds_session_line(self, tmp_path):
        path = write_session(tmp_path, title="my title")
        header = read_header(path)
        assert header and header["type"] == "session" and header["title"] == "my title"

    def test_new_session_becomes_manual_run_node(self, tmp_path):
        agent = tmp_path / "omp"
        write_session(agent)
        watcher, state, _ = make_watcher(tmp_path, agent)
        result = watcher.poll()
        assert len(result.new_nodes) == 1
        row = result.new_nodes[0]
        node_id = MANUAL_NODE_PREFIX + UUID
        assert row["node_id"] == node_id
        assert row["extra"]["kind"] == tree.KIND_MANUAL_RUN
        assert row["engine"] == "omp" and row["depth"] == 0
        assert row["name"] == "fix the config bug"  # header title wins
        assert row["session_ref"].endswith(f"{STEM}.jsonl")
        assert state.get(node_id)["status"] == "live"

    def test_untitled_session_falls_back_to_timestamp_name(self, tmp_path):
        agent = tmp_path / "omp"
        write_session(agent, title=None)
        watcher, _, _ = make_watcher(tmp_path, agent)
        row = watcher.poll().new_nodes[0]
        assert row["name"].startswith("2026-09-07T13-44-58")

    def test_discovery_owned_session_not_duplicated(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        watcher, state, _ = make_watcher(tmp_path, agent)
        # discovery claims the session file for a delegate child
        slug = assign_slug("child", state)
        state.add_node(
            "child", engine="omp", name="child", slug=slug, mxid=virtual_mxid(slug),
            session_ref=str(path), parent_node_id=GW,
        )
        assert watcher.poll().new_nodes == []

    def test_manual_runs_plan_places_room_in_subspace(self, tmp_path):
        agent = tmp_path / "omp"
        write_session(agent)
        watcher, state, _ = make_watcher(tmp_path, agent)
        watcher.poll()
        plan = Renderer(
            state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER
        ).build_plan(host="h")
        subspace_keys = [s.key for s in plan.subspaces]
        assert tree.MANUAL_RUNS_SPACE_KEY in subspace_keys
        manual = next(s for s in plan.subspaces if s.key == tree.MANUAL_RUNS_SPACE_KEY)
        assert [r.key for r in manual.rooms] == [MANUAL_NODE_PREFIX + UUID]
        assert manual.rooms[0].kind == "manual-run"


# --- session parsing (omp_feed reuse) ----------------------------------------------------


class TestSessionParser:
    def test_message_entry_yields_all_three_event_types(self):
        parser = SessionParser()
        entry = assistant_msg(
            {"type": "thinking", "thinking": "Plan: read then fix."},
            {"type": "toolCall", "id": "call_1", "name": "bash",
             "arguments": {"command": "echo hi"}},
            {"type": "text", "text": "Fixed it."},
        )
        events = parser.parse_entry(entry, "agent-1")
        assert [type(e) for e in events] == [ThoughtEvent, ToolEvent, MessageEvent]
        thought, tool, message = events
        assert isinstance(thought, ThoughtEvent)
        assert thought.text == "Plan: read then fix."
        assert isinstance(tool, ToolEvent)
        assert tool.tool == "bash" and json.loads(tool.args) == {"command": "echo hi"}
        assert isinstance(message, MessageEvent)
        assert message.role == "assistant" and message.text == "Fixed it."

    def test_user_message_is_message_event_only(self):
        events = SessionParser().parse_entry(user_msg("go fix"), "a")
        assert [type(e) for e in events] == [MessageEvent]
        assert events[0].role == "user"

    def test_non_message_lines_are_skipped(self):
        parser = SessionParser()
        for entry in (
            {"type": "title", "title": "x"},
            {"type": "session", "id": "s"},
            {"type": "model_change", "model": "m"},
            "not a dict",
            {"type": "message", "message": "bogus"},
            {"type": "message", "message": {"role": "toolResult",
             "content": [{"type": "toolResult", "id": "t"}]}},
        ):
            assert parser.parse_entry(entry, "a") == []

    def test_string_content_treated_as_text(self):
        entry = {"type": "message", "message": {"role": "user", "content": "plain"}}
        events = SessionParser().parse_entry(entry, "a")
        assert len(events) == 1 and events[0].text == "plain"

    def test_empty_thinking_block_dropped(self):
        entry = assistant_msg({"type": "thinking", "thinking": ""})
        assert SessionParser().parse_entry(entry, "a") == []


# --- tailing -------------------------------------------------------------------------------


class TestTailing:
    def test_poll_parses_only_new_bytes(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent, lines=[user_msg("one")])
        watcher, _, _ = make_watcher(tmp_path, agent)
        first = watcher.poll()
        assert len(first.events) == 1

        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(assistant_msg({"type": "text", "text": "two"})) + "\n")
        second = watcher.poll()
        node_events = second.events[MANUAL_NODE_PREFIX + UUID]
        assert [e.text for e in node_events] == ["two"]  # nothing replayed
        assert second.new_nodes == []

    def test_partial_line_buffered_until_complete(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        watcher, _, _ = make_watcher(tmp_path, agent)
        watcher.poll()
        torn = json.dumps(user_msg("torn"))[:20]
        with path.open("a", encoding="utf-8") as f:
            f.write(torn)
        assert watcher.poll().events == {}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(user_msg("torn"))[20:] + "\n")
        events = watcher.poll().events[MANUAL_NODE_PREFIX + UUID]
        assert [e.text for e in events] == ["torn"]

    def test_truncated_file_replays_from_zero(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent, lines=[user_msg("v1")])
        watcher, _, _ = make_watcher(tmp_path, agent)
        watcher.poll()
        write_session(agent, lines=[user_msg("v2")])  # rewrite (smaller/equal)
        events = watcher.poll().events[MANUAL_NODE_PREFIX + UUID]
        assert [e.text for e in events] == ["v2"]

    def test_corrupt_line_skipped_not_fatal(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        watcher, _, _ = make_watcher(tmp_path, agent)
        watcher.poll()
        with path.open("a", encoding="utf-8") as f:
            f.write("{torn json\n")
            f.write(json.dumps(user_msg("good")) + "\n")
        events = watcher.poll().events[MANUAL_NODE_PREFIX + UUID]
        assert [e.text for e in events] == ["good"]
    @pytest.mark.asyncio
    async def test_render_poll_sends_tool_thinking_and_messages(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent, lines=[
            user_msg("do it"),
            assistant_msg(
                {"type": "thinking", "thinking": "hmm"},
                {"type": "toolCall", "name": "edit", "arguments": {"path": "x"}},
                {"type": "text", "text": "done"},
            ),
        ])
        watcher, state, client = make_watcher(tmp_path, agent, executor=True)
        node = MANUAL_NODE_PREFIX + UUID
        # First live pass: discovery + room creation + notice/topic + backlog.
        out1 = await watcher.render_poll()
        assert [r["node_id"] for r in out1.new_nodes] == [node]
        room = state.get(node)["room_id"]
        assert room
        # Second pass: appended content streams as new messages.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(assistant_msg({"type": "text", "text": "later"})) + "\n")
        out2 = await watcher.render_poll()
        assert node in out2.events
        sends = [c for c in client.calls if c[0] == "send"]
        bodies = [s[2] for s in sends]
        assert any("» do it" in b for b in bodies)          # user quote
        assert any("edit" in b for b in bodies)             # tool call message
        assert any("hmm" in b for b in bodies)              # thinking message
        assert any(b == "done" for b in bodies)             # assistant text
        assert any(b == "later" for b in bodies)            # second-pass message
        topics = [c for c in client.calls if c[0] == "state"]
        assert topics and topics[0][1] == room and topics[0][2] == "m.room.topic"
        assert topics[0][3] == {"topic": READONLY_TOPIC}

# --- read-only + reaping ---------------------------------------------------------------------


class TestReadOnlyAndReaping:
    def test_plan_new_carries_topic_marker(self, tmp_path):
        agent = tmp_path / "omp"
        write_session(agent)
        watcher, _, _ = make_watcher(tmp_path, agent)
        row = watcher.poll().new_nodes[0]
        planned = watcher.plan_new(row)
        topics = [i for i in planned if type(i).__name__ == "SetRoomTopic"]
        assert topics and topics[0].topic == READONLY_TOPIC
        assert topics[0].sender == row["mxid"]
        assert any(isinstance(i, SendMessage) for i in planned)

    def test_quiet_and_process_gone_reaps(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        t0 = 1_000_000.0
        os.utime(path, (t0, t0))  # discovery ages from the file's mtime
        clock = {"now": t0}
        watcher, state, _ = make_watcher(
            tmp_path, agent, clock=lambda: clock["now"], process_probe=lambda p: False
        )
        node = MANUAL_NODE_PREFIX + UUID
        watcher.poll()
        state.set_room_id(node, "!r-manual:x")
        state.set_meta("space:manual-runs", "!s-mr:x")
        clock["now"] = t0 + DEFAULT_QUIET_WINDOW + 1
        result = watcher.poll()
        assert result.reaped == [node]
        intents = watcher.plan_reap(node)
        assert intents == (
            __import__("observatory.renderer", fromlist=["DetachChild"]).DetachChild(
                "!s-mr:x", "!r-manual:x", state.get(GW)["mxid"]
            ),
            PurgeRoom("!r-manual:x"),
        )
    def test_live_process_keeps_quiet_room_alive(self, tmp_path):
        agent = tmp_path / "omp"
        write_session(agent)
        t0 = 1_000_000.0
        clock = {"now": t0}
        watcher, _, _ = make_watcher(
            tmp_path, agent, clock=lambda: clock["now"], process_probe=lambda p: True
        )
        watcher.poll()
        clock["now"] = t0 + DEFAULT_QUIET_WINDOW * 3
        assert watcher.poll().reaped == []

    def test_recent_activity_keeps_room(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        t0 = 1_000_000.0
        clock = {"now": t0}
        watcher, _, _ = make_watcher(
            tmp_path, agent, clock=lambda: clock["now"], process_probe=lambda p: False
        )
        watcher.poll()
        clock["now"] = t0 + 60
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(user_msg("still working")) + "\n")
        watcher.poll()  # activity resets the quiet clock
        clock["now"] += DEFAULT_QUIET_WINDOW - 10  # not yet 24h since activity
        assert watcher.poll().reaped == []

    def test_deleted_session_file_reaps_without_purge_intents(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        watcher, state, _ = make_watcher(tmp_path, agent, process_probe=lambda p: False)
        watcher.poll()
        path.unlink()
        result = watcher.poll()
        assert result.reaped == [MANUAL_NODE_PREFIX + UUID]
    @pytest.mark.asyncio
    async def test_render_poll_reap_executes_delete_and_drops_row(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        t0 = 1_000_000.0
        os.utime(path, (t0, t0))  # discovery ages from the file's mtime
        clock = {"now": t0}
        watcher, state, client = make_watcher(
            tmp_path, agent, executor=True, clock=lambda: clock["now"],
            process_probe=lambda p: False,
        )
        node = MANUAL_NODE_PREFIX + UUID
        watcher.poll()
        state.set_room_id(node, "!r-manual:x")
        state.set_meta("space:manual-runs", "!s-mr:x")
        clock["now"] = t0 + DEFAULT_QUIET_WINDOW + 1
        await watcher.render_poll()
        assert ("delete", "!r-manual:x") in client.calls
        with pytest.raises(StateError):
            state.get(node)

    def test_preexisting_quiet_session_ages_from_mtime(self, tmp_path):
        agent = tmp_path / "omp"
        path = write_session(agent)
        t0 = 1_000_000.0
        old = t0 - DEFAULT_QUIET_WINDOW - 3600
        os.utime(path, (old, old))
        watcher, _, _ = make_watcher(
            tmp_path, agent, clock=lambda: t0, process_probe=lambda p: False
        )
        result = watcher.poll()
        assert result.reaped == [MANUAL_NODE_PREFIX + UUID]



# --- process probe ----------------------------------------------------------------------------


class TestProcessProbe:
    def test_proc_scan_finds_referencing_process(self, tmp_path):
        proc = tmp_path / "proc"
        pid = proc / "123"
        pid.mkdir(parents=True)
        session = tmp_path / f"{STEM}.jsonl"
        (pid / "cmdline").write_bytes(
            b"/usr/bin/omp\0--resume\0" + str(session).encode() + b"\0"
        )
        assert process_holds_session(session, proc_root=proc) is True

    def test_no_reference_means_gone(self, tmp_path):
        proc = tmp_path / "proc"
        pid = proc / "123"
        pid.mkdir(parents=True)
        (pid / "cmdline").write_bytes(b"/usr/bin/omp\0--resume\0/some/other.jsonl\0")
        session = tmp_path / f"{STEM}.jsonl"
        assert process_holds_session(session, proc_root=proc) is False

    def test_missing_proc_root_is_gone(self, tmp_path):
        session = tmp_path / f"{STEM}.jsonl"
        assert process_holds_session(session, proc_root=tmp_path / "nope") is False

    def test_uuid_short_form_matches(self, tmp_path):
        proc = tmp_path / "proc"
        pid = proc / "9"
        pid.mkdir(parents=True)
        (pid / "cmdline").write_bytes(f"omp\0--fork\0{UUID}\0".encode())
        session = tmp_path / f"{STEM}.jsonl"
        assert process_holds_session(session, proc_root=proc) is True
