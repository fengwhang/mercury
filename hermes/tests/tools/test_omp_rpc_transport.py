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


if __name__ == "__main__":
    unittest.main()
