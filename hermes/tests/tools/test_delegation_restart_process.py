"""Real process kill/restart gates; no provider, live home, or UI mutation."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from gateway.status import get_process_start_time
from tools.async_delegation import process_identity_state

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "delegation_restart_worker.py"


def wait_marker(path, proc):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        if proc.poll() is not None:
            raise AssertionError(f"Fixture exited {proc.returncode}; inspect {path.parent}/*.log")
        time.sleep(0.01)
    raise AssertionError(f"Fixture marker timeout: {path}")


def start_fixture(home, mode, *args):
    env = {**os.environ, "HERMES_HOME": str(home), "MERCURY_HOME": str(home),
           "PYTHONPATH": str(FIXTURE.parents[2])}
    log = (home / f"{mode}-{time.time_ns()}.log").open("wb")
    proc = subprocess.Popen([sys.executable, str(FIXTURE), mode, *args], env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    return proc


def kill_owned_child(identity, home):
    pid = identity["child_pid"]
    assert process_identity_state(pid, identity["child_started_at"]) == "live"
    (home / "kill-child").touch()
    deadline = time.monotonic() + 5
    while process_identity_state(pid, identity["child_started_at"]) == "live":
        assert time.monotonic() < deadline, "fixture child failed to SIGKILL itself"
        time.sleep(0.01)


@pytest.mark.parametrize(("outcome", "service_shutdown"), [
    ("completed", False), ("interrupted", False),
    pytest.param("interrupted", True, id="service-like-cgroup-kill"),
])
def test_owner_kill_reconcile_and_delivery_ack_restart(tmp_path, outcome, service_shutdown):
    owner = start_fixture(tmp_path, "owner")
    recovery = None
    identity = None
    try:
        identity = wait_marker(tmp_path / "owner-ready", owner)
        owner.kill()
        assert owner.wait(timeout=5) == -signal.SIGKILL
        if service_shutdown:
            # Model KillMode=mixed final cgroup cleanup: after the main exits,
            # the service supervisor kills its child even in a new session.
            # The controlled fixture self-kills; no live service/PID is touched.
            kill_owned_child(identity, tmp_path)
        recovery = start_fixture(tmp_path, "recover", "hold-ack")
        ready = wait_marker(tmp_path / "recovery-ready", recovery)
        expected_live = {"root", "independent"} if service_shutdown else {
            "root", "process-fixture/0", "grandchild", "independent"}
        assert set(ready["live"]) == expected_live
        if outcome == "completed":
            (tmp_path / "release-child").touch()
        elif not service_shutdown:
            kill_owned_child(identity, tmp_path)
        accepted = wait_marker(tmp_path / "accepted", recovery)
        assert accepted["event"]["status"] == outcome
        assert accepted["event"]["parent_session_id"] == "parent"
        assert accepted["messages"] == 1
        # Kill after the receiver persisted the input but BEFORE producer ack.
        recovery.kill()
        assert recovery.wait(timeout=5) == -signal.SIGKILL
        recovery = start_fixture(tmp_path, "recover")
        final = wait_marker(tmp_path / "final", recovery)
        assert recovery.wait(timeout=5) == 0
        assert final["durable"]["state"] == outcome
        assert final["durable"]["delivery_state"] == "delivered"
        assert final["parent_messages"] == 1
        assert json.loads((tmp_path / "adapter-count").read_text()) == 1
        assert final["replayed"] == 0
        assert set(final["live"]) == {"root", "independent"}
        assert final["checkpoint"]["prompt"] == "frozen full fixture specification"
        assert (tmp_path / "worker.jsonl").exists()
        if outcome == "completed":
            assert final["event"]["results"][0]["summary"] == "verified fixture terminal outcome"
        else:
            assert final["event"]["results"][0]["summary"] is None
            assert final["event"]["results"][0]["recovery"]["session_file"] == str(tmp_path / "worker.jsonl")
            continuation = start_fixture(tmp_path, "resume")
            resumed = wait_marker(tmp_path / "resumed", continuation)
            assert continuation.wait(timeout=5) == 0
            assert resumed["goal"] == "frozen fixture goal"
            assert resumed["prompt"] == "frozen full fixture specification"
            assert resumed["prior_transcript"] == str(tmp_path / "worker.jsonl")
            assert resumed["completed_steps"] == ["one", "two"]
            assert resumed["original_status"] == "interrupted"
            assert resumed["continuation_status"] == "completed"
            assert (tmp_path / "step-one-attempts").read_text() == "1"
            assert (tmp_path / "step-two").read_text() == "remaining goal finished"
    finally:
        for proc in (owner, recovery):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if identity and process_identity_state(identity["child_pid"], identity["child_started_at"]) == "live":
            kill_owned_child(identity, tmp_path)


def test_child_finished_before_owner_terminal_write(tmp_path):
    owner = start_fixture(tmp_path, "owner")
    identity = None
    recovery = None
    try:
        identity = wait_marker(tmp_path / "owner-ready", owner)
        (tmp_path / "release-child").touch()
        wait_marker(tmp_path / "child-finished", owner)
        owner.kill()
        assert owner.wait(timeout=5) == -signal.SIGKILL
        recovery = start_fixture(tmp_path, "recover")
        final = wait_marker(tmp_path / "final", recovery)
        assert recovery.wait(timeout=5) == 0
        assert final["event"]["status"] == "completed"
        assert final["durable"]["delivery_state"] == "delivered"
        assert final["parent_messages"] == 1
    finally:
        for proc in (owner, recovery):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        if identity and process_identity_state(identity["child_pid"], identity["child_started_at"]) == "live":
            kill_owned_child(identity, tmp_path)
