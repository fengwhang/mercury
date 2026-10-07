"""Model-free real processes exercising the production restart ledger/receiver.

The worker writes OMP-shaped transcript records; it never writes a terminal
ledger row. Recovery must derive its fate using the production reconciler.
All files, databases, and receivers are confined to the supplied fixture home.
"""
import asyncio
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import time
from types import SimpleNamespace

from tools import async_delegation as ad
from gateway.status import get_process_start_time

HOME = Path(os.environ["HERMES_HOME"])
TRANSCRIPT = HOME / "worker.jsonl"


def marker(name, payload):
    (HOME / name).write_text(json.dumps(payload))


def wait_file(name):
    deadline = time.monotonic() + 15
    while not (HOME / name).exists():
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        time.sleep(0.01)


def append_message(message):
    with TRANSCRIPT.open("a") as handle:
        handle.write(json.dumps({"type": "message", "message": message}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def child():
    append_message({"role": "user", "content": "frozen fixture goal"})
    append_message({"role": "assistant", "stopReason": "toolUse", "content": [
        {"type": "toolCall", "id": "fixture-tool", "name": "fixture", "arguments": {}}]})
    # Observable side effect already committed before interruption. Recovery
    # must inspect this transcript and preserve it, not rerun the whole goal.
    (HOME / "step-one-attempts").write_text("1")
    with (HOME / "step-one").open("x") as handle:
        handle.write("preserved prior side effect")
    append_message({"role": "toolResult", "content": [
        {"type": "text", "text": json.dumps(
            {"completed_step": "one", "artifact": str(HOME / "step-one")})}]})
    marker("child-ready", {"pid": os.getpid()})
    deadline = time.monotonic() + 15
    while not (HOME / "release-child").exists():
        if (HOME / "kill-child").exists():
            # Kill only this fixture's own process, never an unverified PID.
            import signal
            os.kill(os.getpid(), signal.SIGKILL)
        if time.monotonic() > deadline:
            raise TimeoutError("release-child")
        time.sleep(0.01)
    append_message({"role": "assistant", "stopReason": "stop", "content": [
        {"type": "text", "text": "verified fixture terminal outcome"}]})
    with TRANSCRIPT.open("a") as handle:
        handle.write(json.dumps({"type": "custom", "customType": "mercury_delegation_terminal",
            "data": {"childId": "process-fixture/0", "status": "completed",
                     "summary": "verified fixture terminal outcome", "error": None}}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    marker("child-finished", True)


def owner():
    from observatory.state import ObservatoryState
    ad._persist_dispatch({"delegation_id": "process-fixture", "goal": "frozen fixture goal",
        "context": "preserve side effects", "session_key": "agent:main:telegram:dm:123",
        "parent_session_id": "parent", "dispatched_at": time.time()})
    proc = subprocess.Popen([sys.executable, __file__, "child"], start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ad.record_child_spawn("process-fixture/0", "process-fixture", name="Fixture",
        goal="frozen fixture goal", child_pid=proc.pid,
        child_started_at=get_process_start_time(proc.pid), session_file=str(TRANSCRIPT),
        transport_kind="fixture")
    ad.record_child_checkpoint("process-fixture/0", {"prompt": "frozen full fixture specification",
        "workdir": str(HOME), "model": "fixture/model"})
    ad.checkpoint_active_delegations("planned fixture restart")
    state = ObservatoryState(HOME / "observatory" / "state.db")
    for node, depth, parent in [("root", 0, None), ("process-fixture/0", 1, "root"),
                                ("grandchild", 2, "process-fixture/0"), ("independent", 1, "root")]:
        state.add_node(node, parent_node_id=parent, name=node, engine="omp",
            slug=node.replace("/", "-"), mxid=node.replace("/", "_"), depth=depth,
            session_ref=str(TRANSCRIPT) if depth == 1 else node, extra={})
        state.set_room_id(node, "#fixture-" + node.replace("/", "-"))
    state.update_extra("root", task_state="completed")
    state.close()
    wait_file("child-ready")
    marker("owner-ready", {"pid": os.getpid(), "child_pid": proc.pid,
                           "child_started_at": get_process_start_time(proc.pid)})
    wait_file("owner-exit")


async def recover(hold_ack):
    ad._ADOPTION_SWEEP_SECONDS = 0.01
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from mercury_state import SessionDB
    from observatory.room_reaper import reap_orphan_rooms
    from observatory.state import ObservatoryState
    from tools.process_registry import process_registry, format_process_notification

    ad._ADOPTION_SWEEP_SECONDS = 0.01
    pending = process_registry.completion_queue
    ad.restore_undelivered_completions(pending)
    state = ObservatoryState(HOME / "observatory" / "state.db")
    reap_orphan_rooms(state, mercury_home=HOME)
    marker("recovery-ready", {"live": [r["node_id"] for r in state.get_live()]})
    deadline = time.monotonic() + 15
    event = None
    while time.monotonic() < deadline:
        try:
            candidate = pending.get_nowait()
            if candidate.get("delegation_id") == "process-fixture":
                event = candidate
                break
        except queue.Empty:
            pass
        await asyncio.sleep(0.01)
    if event is None:
        raise TimeoutError("terminal completion")
    db = SessionDB(HOME / "state.db")
    if db.get_session("parent") is None:
        db.create_session("parent", source="fixture", model="fixture/model")
    async def load(sid):
        return db.get_messages(sid)
    async def tip(sid):
        return db.get_compression_tip(sid)
    class Receiver:
        async def handle_message(self, incoming):
            db.append_message("parent", "user", incoming.text,
                platform_message_id=incoming.message_id, display_kind="internal_notification")
            previous = json.loads((HOME / "adapter-count").read_text()) if (HOME / "adapter-count").exists() else 0
            marker("adapter-count", previous + 1)
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: Receiver()}
    runner.session_store = SimpleNamespace(_ensure_loaded=lambda: None, _entries={})
    runner._session_source_cache = {}
    runner._session_db = SimpleNamespace(get_compression_tip=tip)
    runner._async_session_store = SimpleNamespace(_store=runner.session_store, load_transcript=load)
    text = format_process_notification(event)
    claim = ad.claim_event_delivery(event, "fixture-receiver")
    if not claim:
        raise RuntimeError("completion claim not recovered")
    assert await runner._inject_watch_notification(text, event) is True
    marker("accepted", {"event": event, "messages": len(db.get_messages("parent"))})
    if hold_ack:
        while not (HOME / "release-ack").exists():
            await asyncio.sleep(0.01)
    ad.complete_event_delivery(event, claim)
    reap_orphan_rooms(state, mercury_home=HOME)
    replay = queue.Queue()
    restored = ad.restore_undelivered_completions(replay)
    marker("final", {"event": event, "durable": ad.get_durable_delegation("process-fixture"),
        "parent_messages": len(db.get_messages("parent")), "replayed": restored,
        "live": [r["node_id"] for r in state.get_live()],
        "checkpoint": ad.list_delegation_children("process-fixture")[0]["checkpoint"]})
    db.close()
    state.close()


def resume():
    """A model-free parent reconciles evidence before continuing its goal."""
    previous = json.loads((HOME / "final").read_text())["event"]
    recovery = previous["results"][0]["recovery"]
    checkpoint = recovery["checkpoint"]
    assert checkpoint["prompt"] == "frozen full fixture specification"
    assert Path(checkpoint["workdir"]) == HOME
    transcript = Path(recovery["session_file"])
    rows = [json.loads(line) for line in transcript.read_text().splitlines()]
    completed = []
    for row in rows:
        message = row.get("message", {})
        if message.get("role") == "toolResult":
            for block in message["content"]:
                receipt = json.loads(block["text"])
                artifact = Path(receipt["artifact"])
                assert artifact.parent == HOME
                assert artifact.read_text() == "preserved prior side effect"
                completed.append(receipt["completed_step"])
    assert completed == ["one"], "resume requires verified prior work"
    original_goal = recovery["goal"]
    assert original_goal == previous["goal"] == "frozen fixture goal"
    task = {"delegation_id": "process-fixture-resume", "goal": original_goal,
            "parent_session_id": previous["parent_session_id"],
            "session_key": previous["session_key"], "dispatched_at": time.time()}
    ad._persist_dispatch(task)
    ad.record_child_spawn("process-fixture-resume/0", task["delegation_id"],
        goal=original_goal, child_pid=os.getpid(),
        child_started_at=get_process_start_time(os.getpid()),
        session_file=str(HOME / "resume-worker.jsonl"), transport_kind="fixture")
    ad.record_child_checkpoint("process-fixture-resume/0",
        {**checkpoint, "resume_from": str(transcript)})
    with (HOME / "step-two").open("x") as handle:
        handle.write("remaining goal finished")
    completed.append("two")
    summary = "verified fixture remaining goal after transcript reconciliation"
    (HOME / "resume-worker.jsonl").write_text(json.dumps(
        {"type": "message", "message": {"role": "assistant", "stopReason": "stop",
         "content": [{"type": "text", "text": summary}]}}) + "\n")
    ad.record_child_terminal("process-fixture-resume/0", "completed", summary=summary)
    ad._persist_completion({**task, "type": "async_delegation", "status": "completed",
        "summary": summary, "completed_at": time.time()}, {"status": "completed", "summary": summary})
    marker("resumed", {"goal": original_goal, "prompt": checkpoint["prompt"],
        "prior_transcript": str(transcript), "completed_steps": completed,
        "original_status": ad.get_durable_delegation("process-fixture")["state"],
        "continuation_status": ad.get_durable_delegation("process-fixture-resume")["state"]})


if __name__ == "__main__":
    if sys.argv[1] == "owner":
        owner()
    elif sys.argv[1] == "child":
        child()
    elif sys.argv[1] == "resume":
        resume()
    else:
        asyncio.run(recover("hold-ack" in sys.argv))
