"""Tests for cron/omp_direct_rpc (C1 slice 2b: RPC-first omp_direct).

Drives the REAL vendored omp_rpc client + Mercury's RPC transport against
a fake omp RPC server (same fake-server contract as
tests/tools/test_omp_rpc_transport.py) and asserts the SCHEDULER-side
result tuple contract of omp_direct_rpc_attempt:

  - None            → caller must fall back to the -p one-shot (only on
                      pre-prompt start failure / kill-switch)
  - (ok, doc, out)  → FINAL result, formatted for cron delivery docs
  - approval gates inside the child route through Mercury's guard stack
    (monkeypatched here) and the verdict lands in the delivered summary

Also pins the static contract: SILENT marker matches
cron.scheduler.SILENT_MARKER so the delivery suppressor keys correctly.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_DIR = REPO_ROOT / "hermes"
OMP_RPC_SRC = REPO_ROOT / "omp" / "python" / "omp-rpc" / "src"

sys.path.insert(0, str(OMP_RPC_SRC))
sys.path.insert(0, str(HERMES_DIR))

from cron import omp_direct_rpc  # noqa: E402
from tools import omp_rpc_transport  # noqa: E402

_FAKE_SERVER = r'''
import json, sys, time

def main():
    def emit(frame):
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    emit({"type": "ready", "protocolVersion": 1,
          "supportedProtocolVersions": [1, 2],
          "maxFrameBytes": 1048576,
          "maxReassembledFrameBytes": 67108864})

    prompt_cmd = None
    ui_response = None
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(3)
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

    msg = (prompt_cmd or {}).get("message", "")
    if "RUN_APPROVAL_GATE" in msg:
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
        summary = f"GATE={ui_response.get('value')}"
    else:
        summary = "no gate needed"

    emit({"type": "agent_start"})
    emit({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": summary}],
    }})
    emit({"type": "agent_end", "messages": [], "message_count": 1,
          "isTerminal": True})


if __name__ == "__main__":
    main()
'''


class _FakeOmpServer:
    def __init__(self):
        self._dir = tempfile.mkdtemp(prefix="mercury-c1b-")
        self.script = os.path.join(self._dir, "fake_omp_rpc.py")
        with open(self.script, "w") as f:
            f.write(_FAKE_SERVER)

    def command(self):
        return [sys.executable, self.script]


class TestStaticContract(unittest.TestCase):
    def test_silent_marker_matches_scheduler(self):
        from cron.scheduler import SILENT_MARKER

        self.assertEqual(omp_direct_rpc.SILENT, SILENT_MARKER)

    def test_kill_switch_returns_none(self):
        os.environ["HERMES_OMP_TRANSPORT"] = "oneshot"
        try:
            self.assertIsNone(
                omp_direct_rpc.omp_direct_rpc_attempt(
                    omp_bin="/nonexistent/omp",
                    model="prov/m-1",
                    prompt="anything",
                )
            )
        finally:
            del os.environ["HERMES_OMP_TRANSPORT"]

    def test_start_failure_returns_none_not_exception(self):
        # No kill-switch; a binary that never emits a ready frame must
        # surface as None (one-shot fallback), never raise — and the
        # failure must happen BEFORE any prompt is sent (start() failed),
        # so the fallback cannot double-execute.
        script = os.path.join(tempfile.mkdtemp(prefix="mercury-c1b-"), "silent.py")
        with open(script, "w") as f:
            f.write("import time; time.sleep(60)\n")
        os.environ["HERMES_OMP_RPC_STARTUP"] = "2"
        try:
            result = omp_direct_rpc.omp_direct_rpc_attempt(
                omp_bin=sys.executable,
                model="prov/m-1",
                prompt="never sent",
                command_override=[sys.executable, script],
            )
        finally:
            del os.environ["HERMES_OMP_RPC_STARTUP"]
        self.assertIsNone(result)


class TestRpcAttemptFlow(unittest.TestCase):
    """End-to-end through the REAL client + transport, fake omp child."""

    def _attempt(self, task_text, decision):
        fake = _FakeOmpServer()
        recorded = {}

        def fake_decision(command, session_key=None):
            recorded["command"] = command
            return decision

        real_decision = omp_rpc_transport.hermes_approval_decision
        omp_rpc_transport.hermes_approval_decision = fake_decision
        try:
            result = omp_direct_rpc.omp_direct_rpc_attempt(
                omp_bin=sys.executable,
                model="prov/m-1",
                prompt=task_text,
                env={"MERCURY_FAKE": "1"},
                timeout=30.0,
                workdir=os.path.dirname(fake.script),
                job_id="job-c1b",
                job_name="c1b-probe",
                now_iso="2026-09-05 12:00:00",
                command_override=fake.command(),
            )
        finally:
            omp_rpc_transport.hermes_approval_decision = real_decision
        return result, recorded

    def test_completed_gate_approved_delivers_summary(self):
        result, recorded = self._attempt(
            "RUN_APPROVAL_GATE then report", decision=True)
        assert result is not None
        ok, doc, out, err = result
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIn("GATE=Approve", out)
        self.assertIn("omp_direct (rpc · model prov/m-1)", doc)
        self.assertIn("job-c1b", doc)
        self.assertEqual(recorded["command"], "rm -rf /tmp/mercury-c1-probe")

    def test_completed_gate_denied_still_completes_with_verdict(self):
        # Deny is not a job failure: the child reports the denial and the
        # tick delivers that verdict (same semantics as the -p path,
        # where a denied tool surfaces in omp's stdout).
        result, recorded = self._attempt(
            "RUN_APPROVAL_GATE then report", decision=False)
        assert result is not None
        ok, doc, out, err = result
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIn("GATE=Deny", out)
        self.assertEqual(recorded["command"], "rm -rf /tmp/mercury-c1-probe")

    def test_plain_task_delivers_summary(self):
        result, recorded = self._attempt("just answer", decision=False)
        assert result is not None
        ok, doc, out, err = result
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIn("no gate needed", out)
        self.assertNotIn("command", recorded)


if __name__ == "__main__":
    unittest.main()
