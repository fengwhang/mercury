"""Combined lifecycle acceptance: real ExecStopPost kills only owned fixture PIDs.

No live service/cgroup discovery: PID enumeration is substituted with the
coordinator and peer handles created here. os.kill and restart are real.
This gate deliberately stays RED until the production lifecycle protection
and native-hub registration seam are integrated; it is not skipped/xfail.
"""
import os
import subprocess
from types import SimpleNamespace

from tests.tools.test_native_agent_hub import BUN, FIXTURE, ROOT
from tools.native_agent_hub import NativeHubSession
from gateway import cgroup_cleanup


def test_service_kill_restart_preserves_granted_peer_and_delivers_first_reply(tmp_path, monkeypatch):
    parent = SimpleNamespace(session_id="service-restart", _native_hub_enabled=True,
                             _native_hub_profile=str(tmp_path), _native_hub_conversation_id="service-restart",
                             _native_hub_turn_running=True)
    rendezvous = tmp_path / "scope.json"
    owner = NativeHubSession(parent, command=BUN + [str(FIXTURE), "server"], rendezvous=rendezvous)
    child = subprocess.Popen(BUN + [str(FIXTURE), "ServicePeer", "linger"], cwd=ROOT / "omp",
                             env={**os.environ, **owner.child_env("ServicePeer")}, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    replacement = None
    try:
        assert child.stdout.readline().strip() == "ready"
        receipt = owner.tool({"op": "send", "to": "ServicePeer", "message": "held_pending"})
        assert receipt["details"]["receipts"][0]["outcome"] == "injected"
        assert child.stdout.readline().strip() == "accepted"
        coordinator = owner._process
        assert coordinator.poll() is None and child.poll() is None
        owner.detach()
        # The only reachable OS PIDs belong to this test's verified Popen handles.
        monkeypatch.setattr(cgroup_cleanup, "_read_cgroup_pids", lambda path: [coordinator.pid, child.pid])
        killed = cgroup_cleanup.reap_cgroup("/isolated-fixture-service")
        assert killed == 0, "planned restart reaper killed registered native coordinator/peer"
        replacement = NativeHubSession(parent, command=BUN + [str(FIXTURE), "server"], rendezvous=rendezvous)
        assert replacement.address == owner.address
        assert [peer["id"] for peer in replacement.tool({"op": "list"})["details"]["peers"]] == ["ServicePeer"]
        assert replacement.drain() == []  # no accepted-message replay
        sent = replacement.tool({"op": "send", "to": "ServicePeer", "message": "ask"})
        assert sent["details"]["receipts"][0]["outcome"] == "injected"
        reply = replacement.tool({"op": "wait", "from": "ServicePeer", "timeoutMs": 1000})
        assert reply["details"]["waited"]["body"] == "source-fixture-answer"
        assert replacement.drain() == []
    finally:
        if replacement is not None:
            replacement.close()
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
        if owner._process.poll() is None:
            # Explicit cleanup of the durable owned fixture, not a saved PID.
            revived = NativeHubSession(parent, command=BUN + [str(FIXTURE), "server"], rendezvous=rendezvous)
            revived.close()
        owner._process.wait(timeout=5)
