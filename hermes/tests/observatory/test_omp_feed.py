"""M3b OmpFeed tests: typed events off a control-capable fake omp server.

Pattern: tests/tools/test_omp_rpc_transport.py's `_ControlFakeServer` —
the fake IS the RPC server (spawned via OmpRpcChild.command_override), so
the real vendored RpcClient drives the whole path (ready/negotiate,
request/response, unknown-notification listener on the reader thread).
The fake emits each subagent frame type after the host subscribes; the
tests assert the typed events that land on the feed's asyncio queue.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OMP_RPC_SRC = REPO_ROOT / "omp" / "python" / "omp-rpc" / "src"

sys.path.insert(0, str(OMP_RPC_SRC))
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory.omp_feed import (  # noqa: E402
    MessageEvent,
    NodeEvent,
    ThoughtEvent,
    ToolEvent,
)
from tools import omp_rpc_transport  # noqa: E402

# A control-capable fake that, once the host sets an "events" subscription,
# emits the full grandchild frame story: lifecycle add → tool progress →
# thinking deltas → message_end → lifecycle death. get_subagent_messages
# serves a byte-growing transcript so catch-up offsets advance.
_FEED_SERVER = r'''
import json, os, sys, time

LOG = os.environ.get("FAKE_RPC_LOG", "/dev/null")

def emit(out, frame):
    with open(LOG, "a") as f:
        f.write(json.dumps(frame) + "\n")
    out.write(json.dumps(frame) + "\n")
    out.flush()

# Server-side transcript: each get_subagent_messages call appends 40 bytes
# of "content", so nextByte tracks what the host has consumed.
TRANSCRIPT_CALLS = [0]

def subagent_frames(out):
    def frame(ftype, payload):
        emit(out, {"type": ftype, "payload": payload})

    frame("subagent_lifecycle", {
        "id": "gc-1", "index": 0, "agent": "task", "agentSource": "tool",
        "status": "started", "sessionFile": "/tmp/fake-gc1.jsonl",
        "parentToolCallId": "call_task_1", "description": "grandchild one",
    })
    frame("subagent_progress", {
        "id": "gc-1", "index": 0, "agent": "task", "agentSource": "tool",
        "task": "grandchild one", "parentToolCallId": "call_task_1",
        "sessionFile": "/tmp/fake-gc1.jsonl",
        "progress": {"id": "gc-1", "status": "running", "toolCount": 0,
                      "currentTool": "bash", "currentToolArgs": "echo hi",
                      "recentTools": [], "recentOutput": []},
    })
    # duplicate progress (same tool+args) — must NOT re-emit a ToolEvent
    frame("subagent_progress", {
        "id": "gc-1", "index": 0, "agent": "task", "agentSource": "tool",
        "task": "grandchild one", "parentToolCallId": "call_task_1",
        "progress": {"id": "gc-1", "status": "running", "toolCount": 1,
                      "currentTool": "bash", "currentToolArgs": "echo hi",
                      "recentTools": [], "recentOutput": []},
    })
    # tool transition bash → grep
    frame("subagent_progress", {
        "id": "gc-1", "index": 0, "agent": "task", "agentSource": "tool",
        "task": "grandchild one", "parentToolCallId": "call_task_1",
        "progress": {"id": "gc-1", "status": "running", "toolCount": 1,
                      "currentTool": "grep", "currentToolArgs": "-r TODO .",
                      "recentTools": [], "recentOutput": []},
    })
    frame("subagent_event", {
        "id": "gc-1",
        "event": {"type": "message_update", "message": {"role": "assistant"},
                   "assistantMessageEvent": {"type": "thinking_delta",
                                             "contentIndex": 0,
                                             "delta": "Plan part "}},
    })
    frame("subagent_event", {
        "id": "gc-1",
        "event": {"type": "message_update", "message": {"role": "assistant"},
                   "assistantMessageEvent": {"type": "thinking_delta",
                                             "contentIndex": 0,
                                             "delta": "two"}},
    })
    frame("subagent_event", {
        "id": "gc-1",
        "event": {"type": "message_update", "message": {"role": "assistant"},
                   "assistantMessageEvent": {"type": "thinking_end",
                                             "contentIndex": 0,
                                             "content": "Plan part two"}},
    })
    # a second thinking block that never sees thinking_end — the
    # message_end flush must surface it defensively
    frame("subagent_event", {
        "id": "gc-1",
        "event": {"type": "message_update", "message": {"role": "assistant"},
                   "assistantMessageEvent": {"type": "thinking_delta",
                                             "contentIndex": 1,
                                             "delta": "unfinished tail"}},
    })
    frame("subagent_event", {
        "id": "gc-1",
        "event": {"type": "message_end",
                   "message": {"role": "assistant", "content": [
                       {"type": "thinking", "thinking": "(hidden)"},
                       {"type": "text", "text": "did the thing"},
                   ]}},
    })
    frame("subagent_lifecycle", {
        "id": "gc-1", "index": 0, "agent": "task", "agentSource": "tool",
        "status": "completed", "sessionFile": "/tmp/fake-gc1.jsonl",
        "parentToolCallId": "call_task_1",
    })
    # unknown frame type + malformed payload — must be ignored
    emit(out, {"type": "something_new", "payload": {"x": 1}})
    emit(out, {"type": "subagent_lifecycle"})  # payload missing
    frame("subagent_lifecycle", {
        "id": "gc-2", "index": 1, "agent": "task", "agentSource": "tool",
        "status": "started", "sessionFile": "/tmp/fake-gc2.jsonl",
        "parentToolCallId": "call_task_2",
    })
    frame("subagent_lifecycle", {
        "id": "gc-2", "index": 1, "agent": "task", "agentSource": "tool",
        "status": "aborted",
    })

def respond(out, frame, command, data=None):
    emit(out, {"type": "response", "id": frame.get("id"),
               "command": command, "success": True,
               **({"data": data} if data is not None else {})})

def main():
    out = sys.stdout
    emit(out, {"type": "ready", "protocolVersion": 1,
               "supportedProtocolVersions": [1, 2],
               "maxFrameBytes": 1048576,
               "maxReassembledFrameBytes": 67108864})
    sent_frames = False
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            frame = json.loads(line)
        except Exception:
            continue
        with open(LOG, "a") as f:
            f.write(json.dumps({"type": "HOST_SENT", "frame": frame}) + "\n")
        t = frame.get("type")
        if t == "negotiate_protocol":
            respond(out, frame, "negotiate_protocol", {"protocolVersion": 2})
        elif t == "set_subagent_subscription":
            respond(out, frame, "set_subagent_subscription",
                    {"level": frame.get("level")})
            if frame.get("level") == "events" and not sent_frames:
                sent_frames = True
                subagent_frames(out)
        elif t == "get_subagent_messages":
            TRANSCRIPT_CALLS[0] += 1
            calls = TRANSCRIPT_CALLS[0]
            respond(out, frame, "get_subagent_messages", {
                "sessionFile": "/tmp/fake-gc1.jsonl",
                "fromByte": frame.get("fromByte") or 0,
                "nextByte": 40 * calls,
                "reset": False,
                "entries": [{"kind": "message"}],
                "messages": [{"role": "assistant",
                              "content": [{"type": "text",
                                           "text": f"chunk-{calls}"}]}],
            })
        elif t == "get_subagents":
            respond(out, frame, "get_subagents", {"subagents": []})
        elif t == "prompt":
            respond(out, frame, "prompt", {"agentInvoked": True})
        elif t == "steer":
            respond(out, frame, "steer")
        elif t == "abort":
            respond(out, frame, "abort")

if __name__ == "__main__":
    main()
'''


class _FeedFakeServer:
    """Spawn the feed-capable fake server; expose its frame log."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="mercury-m3b-feed-")
        self.script = os.path.join(self._dir, "fake_omp_feed.py")
        self.log = os.path.join(self._dir, "frames.jsonl")
        with open(self.script, "w") as f:
            f.write(_FEED_SERVER)

    def command(self):
        return [sys.executable, self.script]

    def host_frames(self, frame_type):
        if not os.path.exists(self.log):
            return []
        out = []
        with open(self.log) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "HOST_SENT" and \
                        rec["frame"].get("type") == frame_type:
                    out.append(rec["frame"])
        return out


class TestOmpFeedFrames(unittest.TestCase):
    """E2E over the real vendored client against the fake server."""

    def _child(self, fake):
        env = dict(os.environ)
        env["FAKE_RPC_LOG"] = fake.log
        child = omp_rpc_transport.OmpRpcChild(
            omp_path=sys.executable,
            model="prov/m-1",
            env=env,
            command_override=fake.command(),
            startup_timeout=15.0,
        )
        child.start()
        self.addCleanup(child.stop)
        return child

    def test_subscription_and_typed_event_story(self):
        from observatory.omp_feed import OmpFeed

        fake = _FeedFakeServer()
        child = self._child(fake)

        async def main():
            feed = OmpFeed(child)
            await feed.start()
            events = []
            async for event in feed.events():
                events.append(event)
                if len(events) == 8:
                    break
            await feed.stop()
            return feed, events

        feed, events = asyncio.run(main())

        # Subscription went on the wire at level "events".
        subs = fake.host_frames("set_subagent_subscription")
        self.assertEqual(subs[-1].get("level"), "events")

        add = events[0]
        self.assertIsInstance(add, NodeEvent)
        self.assertEqual((add.kind, add.subagent_id, add.status),
                         ("add", "gc-1", "running"))
        self.assertEqual(add.parent_tool_call_id, "call_task_1")
        self.assertEqual(add.session_file, "/tmp/fake-gc1.jsonl")

        tools = [e for e in events if isinstance(e, ToolEvent)]
        # duplicate progress suppressed; transition kept
        self.assertEqual([(t.tool, t.args) for t in tools],
                         [("bash", "echo hi"), ("grep", "-r TODO .")])
        self.assertEqual(tools[0].parent_tool_call_id, "call_task_1")

        thoughts = [e for e in events if isinstance(e, ThoughtEvent)]
        self.assertEqual([t.text for t in thoughts],
                         ["Plan part two", "unfinished tail"])

        messages = [e for e in events if isinstance(e, MessageEvent)]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "assistant")
        self.assertEqual(messages[0].text, "did the thing")

        death = events[-1]
        self.assertIsInstance(death, NodeEvent)
        self.assertEqual((death.kind, death.subagent_id, death.status),
                         ("death", "gc-2", "aborted"))

        # Emission order is wire order; seq is monotonic.
        seqs = [e.seq for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(
            [type(e).__name__ for e in events],
            ["NodeEvent", "ToolEvent", "ToolEvent", "ThoughtEvent",
             "ThoughtEvent", "MessageEvent", "NodeEvent", "NodeEvent"],
        )

    def test_byte_offset_catch_up_and_restore(self):
        from observatory.omp_feed import OmpFeed

        fake = _FeedFakeServer()
        child = self._child(fake)

        async def main():
            feed = OmpFeed(child)
            await feed.start()
            # drain the frame story so gc-1's session file is learned
            got = []
            async for event in feed.events():
                got.append(event)
                if len(got) == 8:
                    break
            await feed.stop()
            return feed, got

        feed, _ = asyncio.run(main())

        # Offsets learned from lifecycle frames start at 0.
        self.assertEqual(feed.offsets()["gc-1"],
                         {"session_file": "/tmp/fake-gc1.jsonl",
                          "next_byte": 0})

        first = feed.catch_up("gc-1")
        self.assertEqual(first["fromByte"], 0)
        self.assertEqual(first["nextByte"], 40)
        self.assertEqual(feed.offsets()["gc-1"]["next_byte"], 40)

        second = feed.catch_up("gc-1")
        self.assertEqual(second["fromByte"], 40)
        self.assertEqual(second["nextByte"], 80)
        self.assertEqual(feed.offsets()["gc-1"]["next_byte"], 80)

        # The wire carried incremental fromByte requests.
        reads = fake.host_frames("get_subagent_messages")
        self.assertEqual([r.get("fromByte") for r in reads], [0, 40])

        # A NEW feed (post-reconnect) resumes from the saved offsets.
        async def reconnect():
            feed2 = OmpFeed(child)
            feed2.restore_offsets(feed.offsets())
            return feed2

        feed2 = asyncio.run(reconnect())
        self.assertEqual(feed2.offsets()["gc-1"]["next_byte"], 80)
        third = feed2.catch_up("gc-1")
        self.assertEqual(third["fromByte"], 80)

    def test_frame_source_resolution_errors(self):
        from observatory.omp_feed import OmpFeed

        class Bare:
            def set_subagent_subscription(self, level):
                pass

        with self.assertRaises(TypeError):
            OmpFeed(Bare())._frame_source(Bare())

    def test_translate_is_pure_and_tolerant(self):
        from observatory.omp_feed import OmpFeed

        class NullChild:
            def set_subagent_subscription(self, level):
                pass

        feed = OmpFeed(NullChild())
        self.assertEqual(feed._translate(None), [])
        self.assertEqual(feed._translate({"type": "nope", "payload": {}}), [])
        self.assertEqual(feed._translate({"type": "subagent_lifecycle"}), [])
        self.assertEqual(
            feed._translate({"type": "subagent_event",
                             "payload": {"id": "x", "event": {"type": "odd"}}}),
            [],
        )
        # thinking-only message_end still yields a MessageEvent (empty text)
        out = feed._translate({
            "type": "subagent_event",
            "payload": {"id": "x", "event": {
                "type": "message_end",
                "message": {"role": "user", "content": "plain string"},
            }},
        })
        self.assertEqual([(type(e).__name__, e.role, e.text) for e in out],
                         [("MessageEvent", "user", "plain string")])


if __name__ == "__main__":
    unittest.main()
