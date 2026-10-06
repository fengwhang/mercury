"""Immediate-parent naming, profile inheritance, and root approval ownership."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from observatory import gateway_session, rooms
from observatory.omp_feed import OmpFeed
from observatory.state import ObservatoryState, StateError


class Bot:
    def __init__(self):
        self.destroyed = []
        self.messages = []

    async def join_channel(self, channel):
        return True

    async def invite_user(self, nick, channel):
        return True

    async def say(self, channel, text, **kwargs):
        self.messages.append((channel, text))
        return True

    async def destroy_channel(self, channel):
        self.destroyed.append(channel)
        return True


@pytest.fixture
def family(tmp_path, monkeypatch):
    from observatory import provision

    monkeypatch.setattr(provision, "live_server_name", lambda home=None: "nixpi4")
    state = ObservatoryState(tmp_path / "tree.db")
    for node, engine in (("gw", "hermes"), ("testbot", "hermes"), ("coder", "omp")):
        name = "gateway" if node == "gw" else node
        state.add_node(node, engine=engine, name=name, slug=name, mxid=name,
                       session_ref=f"session:{node}", extra={"profile": "coding"})
        state.set_room_id(node, f"#nixpi4_{name}")
    manager = rooms.RoomManager(state, Bot())
    monkeypatch.setattr(rooms, "_current_manager", manager)
    yield manager, state
    for node in ("testbot", "coder"):
        rooms.drop_omp_room(node)
    state.close()


@pytest.mark.asyncio
async def test_native_nested_rooms_inherit_immediate_parent_and_profile(family):
    manager, state = family
    await manager._ensure_child_room_for("task", {
        "parent_name": "testbot", "name": "test", "engine": "omp"})
    feed = OmpFeed(SimpleNamespace())
    cache = {}
    for child, parent in (("child", "Main"), ("grandchild", "child")):
        event = feed._translate_lifecycle({
            "id": child, "name": child, "parentAgentId": parent,
            "agent": "subagent", "status": "started"})[0]
        await manager._publish_routed_frame("task", "#nixpi4_testbot-test",
            gateway_session._feed_event_to_dict(event), cache)
    assert state.get("task")["room_id"] == "#nixpi4_testbot-test"
    assert state.get("task/sub-child")["room_id"] == "#nixpi4_testbot-test-child"
    descendant = state.get("task/sub-grandchild")
    assert descendant["room_id"] == "#nixpi4_testbot-test-child-grandchild"
    assert descendant["parent_node_id"] == "task/sub-child"
    assert descendant["depth"] == 3
    assert descendant["extra"]["profile"] == "coding"
    await manager._retire_child_room("task/sub-child")
    assert state.get("task/sub-grandchild")["status"] == "live"
    assert state.get("task/sub-grandchild")["extra"].get("task_state") != "completed"
    await manager._retire_child_room("task")
    for node in ("task", "task/sub-child", "task/sub-grandchild"):
        with pytest.raises(StateError):
            state.get(node)
    assert len(manager.bot.destroyed) == 3
    assert state.get("testbot")["status"] == state.get("gw")["status"] == "live"
    assert not await manager._ensure_child_room_for("task/sub-grandchild", {
        "name": "grandchild", "parent_name": "task/sub-child", "engine": "omp"})


@pytest.mark.parametrize("surface", ("mirc", "headless"))
def test_hermes_dispatch_preserves_room_and_profile_across_a_real_child_process(family, tmp_path, monkeypatch, surface):
    from gateway.session_context import clear_session_vars, set_session_vars
    from mercury_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import omp_delegation as delegation

    manager, state = family
    main = tmp_path / "mercury-nightly"
    home = main / "hermes" / "profiles" / "coding"
    home.mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(main))
    binary = tmp_path / "probe-omp"
    binary.write_text(f"#!{sys.executable}\nimport os, json\nprint(json.dumps({{k: os.environ.get(k) for k in ('MERCURY_PROFILE_HOME', 'MERCURY_OBSERVATORY_DEPTH')}}))\n")
    binary.chmod(0o755)
    monkeypatch.setattr(delegation, "_resolve_omp_binary", lambda: str(binary))
    monkeypatch.setattr(delegation, "_rpc_disabled", lambda: True)
    monkeypatch.setattr(delegation, "_omp_delegate_env", lambda *args: ({"OMP_MODEL": "test/model"}, None))
    monkeypatch.setattr(delegation, "_render_omp_config_once", lambda *args: None)
    monkeypatch.setattr(delegation, "_isolate_worktree_enabled", lambda: False)
    observed = []
    register = delegation._register_live_child
    def capture(meta, transport):
        observed.append(dict(meta))
        register(meta, transport)
    monkeypatch.setattr(delegation, "_register_live_child", capture)
    parent = SimpleNamespace(session_id="rotated-conversation-uuid", _delegate_depth=1, cwd=str(tmp_path))
    tokens = set_session_vars(platform="irc", chat_id="#nixpi4_testbot" if surface == "mirc" else "", session_key="irc:group:#nixpi4_testbot")
    home_token = set_hermes_home_override(str(home))
    try:
        def dispatch():
            return delegation.dispatch_omp_delegation(parent, {"tasks": [{"name": "test", "goal": "hello"}]})
        output = dispatch() if surface == "mirc" else gateway_session.run_gateway_prompt(
            "delegate", node_id="testbot", session_id="testbot", agent_factory=lambda sid: parent,
            turn=lambda agent, text: {"final_response": dispatch()})
        result = json.loads(output)
    finally:
        reset_hermes_home_override(home_token)
        clear_session_vars(tokens)
    assert result["results"][0]["status"] == "completed"
    child_env = json.loads(result["results"][0]["summary"])
    assert child_env == {"MERCURY_PROFILE_HOME": str(home), "MERCURY_OBSERVATORY_DEPTH": "1"}
    assert observed[0]["parent_node_id"] == "testbot"
    channel = asyncio.run(manager._ensure_child_room_for(observed[0]["child_id"], {
        "parent_name": observed[0]["parent_node_id"], "name": "test", "engine": "omp"}))
    assert channel == "#nixpi4_testbot-test"


@pytest.mark.asyncio
async def test_detached_omp_approval_remains_at_root_after_parent_turn_ends(family, monkeypatch):
    from tools import approval
    from tools.omp_rpc_transport import hermes_tool_approval_decision

    manager, state = family
    key = "irc:group:#nixpi4_coder"
    prompts = []
    def notify(channel, text, **kwargs):
        prompts.append((channel, text))
        assert approval.resolve_gateway_approval("irc:group:#nixpi4_gateway", "once") == 0
        approval.resolve_gateway_approval(key, "once")
        return True
    monkeypatch.setattr(rooms, "say_nowait", notify)
    async def no_feed(*args):
        return None
    monkeypatch.setattr(manager, "_start_live_omp_feed", no_feed)
    class Rpc:
        def run_task(self, prompt):
            from contextvars import copy_context
            self.context = copy_context()
            return {"summary": "child launched", "turn_frames": []}
    rpc = Rpc()
    rooms.register_omp_room("coder", "#nixpi4_coder", rpc)
    await manager._run_spawned_omp_task("#nixpi4_coder", "coder", "owner", "delegate", rpc, approval_session_key=key)
    try:
        approved = await asyncio.to_thread(rpc.context.run, hermes_tool_approval_decision,
            "[child] [grandchild] Allow tool: bash\nCommand: true")
        assert approved is True
        assert prompts[0][0] == "#nixpi4_coder"
        assert "!approve" in prompts[0][1]
    finally:
        rooms.drop_omp_room("coder")
    assert key not in approval._gateway_notify_cbs


@pytest.mark.asyncio
async def test_descendant_approval_notice_reaches_its_own_root(family, monkeypatch):
    manager, state = family
    await manager._ensure_child_room_for("child", {
        "name": "child", "parent_name": "testbot", "engine": "omp"})
    await manager._ensure_child_room_for("grandchild", {
        "name": "grandchild", "parent_name": "child", "engine": "hermes"})
    sent = []
    monkeypatch.setattr(rooms, "say_nowait", lambda channel, text, **kwargs: sent.append((channel, text)))
    gateway_session.push_approval_prompt("grandchild", request_id="request", command="bash")
    assert sent[0][0] == "#nixpi4_testbot"
    assert gateway_session._headless_approval_key("grandchild") == gateway_session._headless_approval_key("testbot")
    assert gateway_session._headless_approval_key("testbot") != gateway_session._headless_approval_key("coder")


@pytest.mark.asyncio
async def test_completed_native_name_can_be_reused_only_with_a_new_transcript(family):
    manager, state = family
    item = {"name": "hello", "parent_name": "coder", "engine": "omp", "session_ref": "old.jsonl"}
    node = "coder/sub-Hello"
    await manager._ensure_child_room_for(node, item)
    await manager._retire_child_room(node)
    assert await manager._ensure_child_room_for(node, item) == ""
    assert await manager._ensure_child_room_for(node, {**item, "session_ref": "new.jsonl"}) == "#nixpi4_coder-hello"
    assert state.get(node)["session_ref"] == "new.jsonl"


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ("death", "add", "message"))
async def test_old_native_generation_cannot_mutate_a_reused_child_room(family, frame):
    manager, state = family
    node = "coder/sub-Hello"
    item = {"name": "hello", "parent_name": "coder", "engine": "omp", "session_ref": "old.jsonl"}
    await manager._ensure_child_room_for(node, item)
    await manager._retire_child_room(node)
    channel = await manager._ensure_child_room_for(node, {**item, "session_ref": "new.jsonl"})
    cache = {"Hello": channel}
    replacement = state.get(node)
    messages = list(manager.bot.messages)
    destroyed = list(manager.bot.destroyed)
    await manager._publish_routed_frame("coder", "#nixpi4_coder", {
        "feed": "message" if frame == "message" else "node",
        "kind": frame, "role": "assistant", "text": "old transcript",
        "subagent_id": "Hello", "session_file": "old.jsonl",
        "name": "hello", "status": "completed",
    }, cache)
    assert state.get(node) == replacement
    assert cache == {"Hello": channel}
    assert manager.bot.destroyed == destroyed
    assert manager.bot.messages == messages


@pytest.mark.asyncio
async def test_exit_native_parent_stops_descendants_and_preserves_the_root(family):
    from observatory.spawn import OrchestratorHandle, OrchestratorRegistry, exit_orchestrator

    manager, state = family
    calls = []
    rpc = SimpleNamespace(subagent_abort=lambda sid: calls.append(sid), stop=lambda: calls.append("ROOT STOPPED"))
    registry = OrchestratorRegistry()
    handle = OrchestratorHandle(node_id="coder", engine="omp", name="coder", session_ref="coder.jsonl", rpc=rpc)
    registry.register(handle)
    await manager._ensure_child_room_for("child", {
        "name": "child", "parent_name": "coder", "engine": "omp", "subagent_id": "Child"})
    await manager._ensure_child_room_for("grandchild", {
        "name": "grandchild", "parent_name": "child", "engine": "omp", "subagent_id": "Grandchild"})
    result = await exit_orchestrator("child", state=state, registry=registry, bot=manager.bot)
    assert result["deferred"] == []
    assert calls == ["Grandchild", "Child"]
    assert registry.get("coder") is handle
    assert state.get("coder")["status"] == "live"
    with pytest.raises(StateError):
        state.get("grandchild")
