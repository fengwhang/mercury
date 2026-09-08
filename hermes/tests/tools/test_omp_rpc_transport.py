"""Tests for tools/omp_rpc_transport (C1: RPC approval routing).

These tests drive the REAL vendored omp_rpc client (omp/python/omp-rpc)
against a fake omp RPC server (a Python subprocess speaking the documented
wire protocol from omp/docs/rpc.md). The fake server:

  - emits the ready frame, negotiates v2 when offered
  - answers ``prompt`` with success + agentInvoked
  - streams agent_start -> message_end(assistant) -> agent_end
  - emits an approval-gate ``extension_ui_request`` (method=select,
    options [Approve, Deny], message with a ``Command:`` line) when the
    task text demands one, and awaits the host's response
  - asserts the host DENIED when our routing decides deny

What is under test is MERCURY's seam: prompt parsing, Approve/Deny
detection, hermes guard-stack invocation (monkeypatched here), and the
fail-closed default for unrouted dialogs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "hermes" / "tools"
OMP_RPC_SRC = REPO_ROOT / "omp" / "python" / "omp-rpc" / "src"

sys.path.insert(0, str(OMP_RPC_SRC))
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from tools import omp_rpc_transport  # noqa: E402

_FAKE_SERVER = r'''
import json, sys, time

def read_requests(out, emit):
    # 1. wait for the task prompt command
    prompt_cmd = None
    ui_response = None
    while True:
        line = sys.stdin.readline()
        if not line:
            return None, None
        try:
            frame = json.loads(line)
        except Exception:
            continue
        t = frame.get("type")
        if t == "negotiate_protocol":
            emit({"type": "response", "id": frame.get("id"),
                  "command": "negotiate_protocol", "success": True,
                  "data": {"protocolVersion": 2}})
            continue
        if t == "prompt":
            emit({"type": "response", "id": frame.get("id"),
                  "command": "prompt", "success": True,
                  "data": {"agentInvoked": True}})
            prompt_cmd = frame
            break
    # 2. if the task wants an approval gate, emit it and await the answer
    wants_approval = "RUN_APPROVAL_GATE" in (prompt_cmd or {}).get("message", "")
    if wants_approval:
        emit({
            "type": "extension_ui_request",
            "id": "ui_1",
            "method": "select",
            "title": "Allow tool: bash\nReason: exec-tier command requires approval\nCommand: rm -rf /tmp/mercury-c1-probe",
            "options": ["Approve", "Deny"],
        })
        deadline = time.time() + 20
        while time.time() < deadline:
            line = sys.stdin.readline()
            if not line:
                sys.exit(3)
            try:
                frame = json.loads(line)
            except Exception:
                continue
            if frame.get("type") == "extension_ui_response" and frame.get("id") == "ui_1":
                ui_response = frame
                break
        if ui_response is None:
            emit({"type": "agent_end", "messages": [], "message_count": 0})
            return
        verdict = ui_response.get("value")
        summary = f"GATE={verdict}"
    else:
        summary = "no gate needed"
    # 3. stream the assistant reply and finish
    emit({"type": "agent_start"})
    emit({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": summary}],
    }})
    emit({"type": "agent_end", "messages": [], "message_count": 1,
          "isTerminal": True})


def main():
    def emit(frame):
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    emit({"type": "ready", "protocolVersion": 1,
          "supportedProtocolVersions": [1, 2],
          "maxFrameBytes": 1048576,
          "maxReassembledFrameBytes": 67108864})
    read_requests(sys.stdout, emit)


if __name__ == "__main__":
    main()
'''


class _FakeOmpServer:
    """Spawn the fake RPC server; expose its transcript via a file."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="mercury-c1-")
        self.script = os.path.join(self._dir, "fake_omp_rpc.py")
        self.log = os.path.join(self._dir, "frames.jsonl")
        with open(self.script, "w") as f:
            f.write(_FAKE_SERVER)

    def command(self):
        return [sys.executable, self.script]


class TestPromptParsing(unittest.TestCase):
    def test_extracts_command_line(self):
        msg = ("Allow tool: bash\nReason: exec-tier command requires approval\n"
               "Command: rm -rf /tmp/x")
        self.assertEqual(
            omp_rpc_transport.extract_command_from_prompt(msg), "rm -rf /tmp/x")

    def test_multiline_command_preserved(self):
        msg = "Allow tool: bash\nCommand: echo a \\\n  b"
        self.assertEqual(
            omp_rpc_transport.extract_command_from_prompt(msg), "echo a \\\n  b")

    def test_no_command_line_returns_none(self):
        self.assertIsNone(
            omp_rpc_transport.extract_command_from_prompt("Allow tool: read"))
        self.assertIsNone(omp_rpc_transport.extract_command_from_prompt(""))

    def test_approval_select_detection(self):
        self.assertTrue(omp_rpc_transport.looks_like_approval_select(
            ("Approve", "Deny"), "select"))
        self.assertFalse(omp_rpc_transport.looks_like_approval_select(
            ("Yes", "No"), "select"))
        self.assertFalse(omp_rpc_transport.looks_like_approval_select(
            ("Approve", "Deny"), "confirm"))
        self.assertFalse(omp_rpc_transport.looks_like_approval_select(
            None, "select"))


class TestHermesApprovalDecision(unittest.TestCase):
    def test_guard_stack_approved_routes_true(self):
        recorded = {}

        def fake_guards(command, env_type=None, **kw):
            recorded["command"] = command
            recorded["env_type"] = env_type
            return {"approved": True, "message": None}

        orig = getattr(omp_rpc_transport, "check_all_command_guards", None)
        import tools.approval as approval_mod
        real = approval_mod.check_all_command_guards
        approval_mod.check_all_command_guards = fake_guards
        try:
            self.assertTrue(
                omp_rpc_transport.hermes_approval_decision("ls -la"))
        finally:
            approval_mod.check_all_command_guards = real
        self.assertEqual(recorded["command"], "ls -la")

    def test_guard_exception_fails_closed(self):
        import tools.approval as approval_mod
        real = approval_mod.check_all_command_guards

        def boom(*a, **kw):
            raise RuntimeError("guard stack down")

        approval_mod.check_all_command_guards = boom
        try:
            self.assertFalse(
                omp_rpc_transport.hermes_approval_decision("ls"))
        finally:
            approval_mod.check_all_command_guards = real


class TestRpcChildFlow(unittest.TestCase):
    """End-to-end against the fake server through the REAL client."""

    def _run(self, task_text, decision):
        fake = _FakeOmpServer()
        results = {}

        def fake_decision(command, session_key=None):
            results["command"] = command
            return decision

        real_decision = omp_rpc_transport.hermes_approval_decision
        omp_rpc_transport.hermes_approval_decision = fake_decision
        try:
            entry = omp_rpc_transport.run_omp_task_rpc(
                omp_path=sys.executable,
                model="prov/m-1",
                prompt=task_text,
                env={"MERCURY_FAKE": "1"},
                timeout=30.0,
                workdir=fake._dir,
                command_override=fake.command(),
            )
        finally:
            omp_rpc_transport.hermes_approval_decision = real_decision
        results["entry"] = entry
        return results

    def test_approval_routed_and_denied(self):
        results = self._run(
            "RUN_APPROVAL_GATE then report", decision=False)
        self.assertIn("GATE=Deny", results["entry"].get("summary") or "")
        self.assertEqual(results["entry"]["status"], "completed")
        self.assertEqual(
            results["command"], "rm -rf /tmp/mercury-c1-probe")

    def test_approval_routed_and_approved(self):
        results = self._run(
            "RUN_APPROVAL_GATE then report", decision=True)
        self.assertIn("GATE=Approve", results["entry"].get("summary") or "")
        self.assertEqual(
            results["command"], "rm -rf /tmp/mercury-c1-probe")

    def test_plain_task_completes_without_gate(self):
        results = self._run("just answer", decision=False)
        self.assertEqual(results["entry"]["status"], "completed")
        self.assertIn("no gate needed", results["entry"]["summary"])
        self.assertNotIn("command", results)


# M0A (matrix observatory §8.1/§8.2): a generic fake RPC server that
# answers CONTROL + OBSERVER commands (steer, abort, get_subagents,
# set_subagent_subscription, get_subagent_messages, subagent_steer,
# subagent_abort) and can hold a prompt turn open until a steer arrives —
# exercising the real mid-run steering path through the vendored client.
_CONTROL_SERVER = r'''
import json, os, sys, time

LOG = os.environ.get("FAKE_RPC_LOG", "/dev/null")

def log(frame):
    with open(LOG, "a") as f:
        f.write(json.dumps(frame) + "\n")

def emit(out, frame):
    log(frame)
    out.write(json.dumps(frame) + "\n")
    out.flush()

SUBAGENTS = [
    {"id": "sa-1", "index": 0, "agent": "task", "agentSource": "tool",
     "status": "running", "task": "probe task", "lastUpdate": 1},
    {"id": "sa-2", "index": 1, "agent": "task", "agentSource": "tool",
     "status": "completed", "lastUpdate": 2},
]

def respond(out, frame, command, data=None):
    emit(out, {"type": "response", "id": frame.get("id"),
               "command": command, "success": True,
               **({"data": data} if data is not None else {})})

def stream_done(out, summary):
    emit(out, {"type": "agent_start"})
    emit(out, {"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": summary}],
    }})
    emit(out, {"type": "agent_end", "messages": [], "message_count": 1,
               "isTerminal": True})

def main():
    out = sys.stdout
    emit(out, {"type": "ready", "protocolVersion": 1,
               "supportedProtocolVersions": [1, 2],
               "maxFrameBytes": 1048576,
               "maxReassembledFrameBytes": 67108864})
    steered_text = None
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            frame = json.loads(line)
        except Exception:
            continue
        log({"type": "HOST_SENT", "frame": frame})
        t = frame.get("type")
        if t == "negotiate_protocol":
            respond(out, frame, "negotiate_protocol",
                    {"protocolVersion": 2})
        elif t == "prompt":
            respond(out, frame, "prompt", {"agentInvoked": True})
            if "WAIT_FOR_STEER" in frame.get("message", ""):
                # hold the turn open until the host steers: keep draining
                # stdin so the steer command itself is answered, then
                # finish with a summary proving the steer arrived
                deadline = time.time() + 30
                while time.time() < deadline and steered_text is None:
                    line = sys.stdin.readline()
                    if not line:
                        return
                    try:
                        inner = json.loads(line)
                    except Exception:
                        continue
                    log({"type": "HOST_SENT", "frame": inner})
                    if inner.get("type") == "steer":
                        steered_text = inner.get("message")
                        respond(out, inner, "steer")
                stream_done(out, f"steered:{steered_text}")
            else:
                stream_done(out, "plain-ok")
        elif t == "steer":
            steered_text = frame.get("message")
            respond(out, frame, "steer")
        elif t == "abort":
            respond(out, frame, "abort")
        elif t == "get_subagents":
            respond(out, frame, "get_subagents", {"subagents": SUBAGENTS})
        elif t == "set_subagent_subscription":
            respond(out, frame, "set_subagent_subscription",
                    {"level": frame.get("level")})
        elif t == "get_subagent_messages":
            respond(out, frame, "get_subagent_messages", {
                "sessionFile": "/tmp/fake-session.jsonl",
                "fromByte": frame.get("fromByte") or 0,
                "nextByte": 42,
                "reset": False,
                "entries": [{"kind": "message"}],
                "messages": [{"role": "assistant",
                              "content": [{"type": "text", "text": "hi"}]}],
            })
        elif t == "subagent_steer":
            respond(out, frame, "subagent_steer")
        elif t == "subagent_abort":
            respond(out, frame, "subagent_abort", {"aborted": True})


if __name__ == "__main__":
    main()
'''


class _ControlFakeServer:
    """Spawn the control-capable fake server; expose its frame log."""

    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="mercury-m0a-")
        self.script = os.path.join(self._dir, "fake_omp_control.py")
        self.log = os.path.join(self._dir, "frames.jsonl")
        with open(self.script, "w") as f:
            f.write(_CONTROL_SERVER)

    def command(self):
        return [sys.executable, self.script]

    def host_frames(self):
        """Frames the HOST sent (requests), in order."""
        if not os.path.exists(self.log):
            return []
        out = []
        with open(self.log) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "HOST_SENT":
                    out.append(rec["frame"])
        return out

    def await_host_frame(self, frame_type, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frame in self.host_frames():
                if frame.get("type") == frame_type:
                    return frame
            time.sleep(0.05)
        raise AssertionError(f"host never sent {frame_type!r}")


class TestControlPlaneMethods(unittest.TestCase):
    """M0A: steer/abort + subagent-observer methods over the real client."""

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

    def test_steer_mid_run_reaches_child(self):
        fake = _ControlFakeServer()
        child = self._child(fake)
        result = {}

        def run_task():
            result["entry"] = child.run_task(
                "WAIT_FOR_STEER then report", timeout=30.0)

        worker = threading.Thread(target=run_task)
        worker.start()
        try:
            fake.await_host_frame("prompt")
            child.steer("switch to plan B")
        finally:
            worker.join(timeout=30)
        self.assertEqual(result["entry"]["status"], "completed")
        self.assertIn("steered:switch to plan B",
                      result["entry"]["summary"])
        steers = [f for f in fake.host_frames() if f.get("type") == "steer"]
        self.assertEqual(
            [f.get("message") for f in steers], ["switch to plan B"])

    def test_abort_sends_abort_command(self):
        fake = _ControlFakeServer()
        child = self._child(fake)
        child.abort(reason="unit test stop")
        frame = fake.await_host_frame("abort")
        self.assertEqual(frame.get("type"), "abort")

    def test_observer_methods_round_trip(self):
        fake = _ControlFakeServer()
        child = self._child(fake)

        snapshot = child.get_subagents()
        self.assertEqual([s["id"] for s in snapshot["subagents"]],
                         ["sa-1", "sa-2"])

        ack = child.set_subagent_subscription("events")
        self.assertEqual(ack, {"level": "events"})
        sub = [f for f in fake.host_frames()
               if f.get("type") == "set_subagent_subscription"]
        self.assertEqual(sub[-1].get("level"), "events")

        msgs = child.get_subagent_messages(subagent_id="sa-1", from_byte=7)
        self.assertEqual(msgs["nextByte"], 42)
        self.assertEqual(msgs["fromByte"], 7)
        gm = [f for f in fake.host_frames()
              if f.get("type") == "get_subagent_messages"]
        self.assertEqual(gm[-1].get("subagentId"), "sa-1")
        self.assertEqual(gm[-1].get("fromByte"), 7)

        child.subagent_steer("sa-1", "grandchild redirect")
        ss = [f for f in fake.host_frames() if f.get("type") == "subagent_steer"]
        self.assertEqual(ss[-1].get("subagentId"), "sa-1")
        self.assertEqual(ss[-1].get("text"), "grandchild redirect")

        ack = child.subagent_abort("sa-1", "done for today")
        self.assertEqual(ack, {"aborted": True})
        sa = [f for f in fake.host_frames() if f.get("type") == "subagent_abort"]
        self.assertEqual(sa[-1].get("subagentId"), "sa-1")
        self.assertEqual(sa[-1].get("reason"), "done for today")

    def test_client_side_validation_before_wire(self):
        fake = _ControlFakeServer()
        child = self._child(fake)
        with self.assertRaises(ValueError):
            child.set_subagent_subscription("everything")
        with self.assertRaises(ValueError):
            child.get_subagent_messages()
        with self.assertRaises(ValueError):
            child.subagent_steer("", "text")
        with self.assertRaises(ValueError):
            child.subagent_steer("sa-1", "   ")
        # nothing but the handshake went on the wire
        types = [f.get("type") for f in fake.host_frames()]
        self.assertNotIn("set_subagent_subscription", types)
        self.assertNotIn("get_subagent_messages", types)
        self.assertNotIn("subagent_steer", types)

    def test_control_before_start_raises(self):
        child = omp_rpc_transport.OmpRpcChild(
            omp_path=sys.executable, model="m")
        with self.assertRaises(omp_rpc_transport.OmpRpcControlError):
            child.steer("x")
        with self.assertRaises(omp_rpc_transport.OmpRpcControlError):
            child.abort()

    def test_connection_loss_raises_control_error(self):
        fake = _ControlFakeServer()
        child = self._child(fake)
        child.stop()
        with self.assertRaises(omp_rpc_transport.OmpRpcControlError):
            child.steer("too late")
        with self.assertRaises(omp_rpc_transport.OmpRpcControlError):
            child.get_subagents()

    def test_lifecycle_hooks_fire(self):
        fake = _ControlFakeServer()
        events = []
        entry = omp_rpc_transport.run_omp_task_rpc(
            omp_path=sys.executable,
            model="prov/m-1",
            prompt="plain task",
            env={"FAKE_RPC_LOG": fake.log},
            timeout=30.0,
            workdir=fake._dir,
            command_override=fake.command(),
            child_started=lambda c: events.append(("started", c)),
            child_finished=lambda c: events.append(("finished", c)),
        )
        self.assertEqual(entry["status"], "completed")
        self.assertEqual([e[0] for e in events], ["started", "finished"])
        self.assertIs(events[0][1], events[1][1])  # same child both times


if __name__ == "__main__":
    unittest.main()
