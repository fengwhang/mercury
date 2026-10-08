"""Restart receipts are a write-ahead admission boundary, not caller guesses."""
from __future__ import annotations

import importlib
import json
import os
import stat
import asyncio
import socket
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

def test_request_is_durable_private_and_profile_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    sync = os.fsync
    synced_records = []

    def observe_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            synced_records.extend(json.loads(line) for line in
                                  (tmp_path / "logs/gateway-restart-requests.jsonl")
                                  .read_text(encoding="utf-8").splitlines())
        sync(fd)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    receipt = provenance.record_restart_request(
        source="control_socket", reason="restart", automatic=False,
        active_delegations=["delegation-a"],
    )
    path = tmp_path / "logs/gateway-restart-requests.jsonl"
    assert synced_records == [receipt]  # flush precedes fsync, which precedes return
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert receipt["request_id"]
    assert receipt["timestamp"].endswith("+00:00")
    assert receipt["gateway_pid"] == os.getpid()
    assert receipt["gateway_start_time"] is not None
    assert receipt["source"] == "control_socket"
    assert receipt["reason"] == "restart"
    assert receipt["automatic"] is False
    assert receipt["actor"]["authentication"] == "unknown"
    assert receipt["active_delegations"] == ["delegation-a"]
    assert receipt["state"] == "requested"


def test_transitions_correlate_without_implicitly_accepting(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    receipt = provenance.record_restart_request(
        source="signal", reason="SIGUSR1", automatic=False, request_id="exact-id",
    )
    records = [receipt]
    for state in ("deferred", "accepted", "stopping", "exit"):
        records.append(provenance.record_restart_transition(
            receipt["request_id"], state, active_delegations=["delegation-a"],
            exit_code=75 if state == "exit" else None,
        ))
    assert [record["state"] for record in records] == [
        "requested", "deferred", "accepted", "stopping", "exit",
    ]
    assert {record["request_id"] for record in records} == {"exact-id"}
    assert records[0]["actor"]["authentication"] == "unknown"
    assert records[-1]["exit_code"] == 75
    path = tmp_path / "logs/gateway-restart-requests.jsonl"
    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == records


@pytest.mark.parametrize("fields", [{"gateway_pid": 1}, {"argv": ["secret"]}])
def test_transition_rejects_unsafe_or_reserved_fields(tmp_path, monkeypatch, fields):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    with pytest.raises(ValueError):
        provenance.record_restart_transition("id", "accepted", **fields)
    assert not (tmp_path / "logs/gateway-restart-requests.jsonl").exists()


@pytest.mark.parametrize("state", ["requested", "pretend-exit", ""])
def test_transition_rejects_unknown_states(tmp_path, monkeypatch, state):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    with pytest.raises(ValueError):
        provenance.record_restart_transition("id", state)


def test_generated_ids_are_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    ids = {provenance.record_restart_request(
        source="internal", reason="restart", automatic=True,
    )["request_id"] for _ in range(20)}
    assert len(ids) == 20


@pytest.mark.parametrize("operation", ["request", "transition"])
@pytest.mark.parametrize("failure", ["open", "file_sync", "directory_sync"])
def test_persistence_failure_never_returns_receipt(tmp_path, monkeypatch, operation, failure):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    original_open, original_sync = os.open, os.fsync

    def fail_open(*args, **kwargs):
        if str(args[0]).endswith("gateway-restart-requests.jsonl"):
            raise OSError("injected open failure")
        return original_open(*args, **kwargs)

    def fail_sync(fd):
        is_file = stat.S_ISREG(os.fstat(fd).st_mode)
        if is_file == (failure == "file_sync"):
            raise OSError("injected sync failure")
        original_sync(fd)

    monkeypatch.setattr(os, "open" if failure == "open" else "fsync",
                        fail_open if failure == "open" else fail_sync)
    with pytest.raises(OSError, match="injected"):
        if operation == "request":
            provenance.record_restart_request(source="internal", reason="restart", automatic=True)
        else:
            provenance.record_restart_transition("id", "accepted")


def test_existing_journal_is_made_private(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    path = tmp_path / "logs/gateway-restart-requests.jsonl"
    path.parent.mkdir()
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)
    provenance.record_restart_request(source="internal", reason="restart", automatic=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_journal_refuses_symlink_without_touching_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    target = tmp_path / "unrelated"
    target.write_text("unchanged", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/gateway-restart-requests.jsonl").symlink_to(target)
    with pytest.raises(OSError):
        provenance.record_restart_request(source="internal", reason="restart", automatic=True)
    assert target.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credentials")
def test_real_unix_streamwriter_peer_is_kernel_authenticated(tmp_path):
    provenance = importlib.import_module("gateway.restart_provenance")

    async def exercise():
        result = asyncio.get_running_loop().create_future()

        async def connected(reader, writer):
            try:
                result.set_result(provenance.authenticated_control_actor(
                    writer.get_extra_info("socket"),
                ))
            except Exception as exc:
                result.set_exception(exc)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(connected, path=str(tmp_path / "control.sock"))
        try:
            reader, writer = await asyncio.open_unix_connection(str(tmp_path / "control.sock"))
            try:
                return await asyncio.wait_for(result, timeout=3)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    actor = asyncio.run(exercise())
    assert actor["authentication"] == "unix_peer_credentials"
    assert actor["pid"] == os.getpid()
    assert actor["uid"] == os.getuid()
    assert actor["gid"] == os.getgid()
    assert actor["same_gateway_cgroup"] is True
    assert actor["start_time"] is not None
    assert actor["ancestry"][0]["pid"] == os.getpid()
    assert len(actor["ancestry"]) <= 8
    assert all(set(item) == {"pid", "start_time", "executable", "operation"}
               for item in actor["ancestry"])


@pytest.mark.parametrize(("executable", "argv", "expected"), [
    ("python3.13", ["python", "-m", "mercury_cli.main", "--profile", "PRIVATE",
                   "gateway", "restart", "--force"], ["mercury", "gateway", "restart", "--force"]),
    ("mercury", ["mercury", "gateway", "restart", "--token", "SECRET"],
     ["mercury", "gateway", "restart"]),
    ("systemctl", ["systemctl", "--user", "restart", "secret-profile.service"],
     ["systemctl", "--user", "restart"]),
    ("systemctl", ["systemctl", "daemon-reload"], ["systemctl", "daemon-reload"]),
    ("bash", ["bash", "-c", "mercury gateway restart --token SECRET"], None),
    ("python3", ["python", "-c", "secret", "gateway", "restart"], None),
    ("unrelated", ["unrelated", "gateway", "restart", "SECRET"], None),
    ("mercury", ["mercury", "--token", "gateway", "restart"], None),
    ("mercury", ["mercury"], None),
    ("python3", [], None),
])
def test_operation_tokens_are_anchored_allowlisted_not_arbitrary_argv(executable, argv, expected):
    provenance = importlib.import_module("gateway.restart_provenance")
    assert provenance._safe_operation(executable, argv) == expected


def test_missing_peer_credentials_never_invents_a_pid(monkeypatch):
    provenance = importlib.import_module("gateway.restart_provenance")
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    actor = provenance.authenticated_control_actor(None)
    assert actor["authentication"] == "filesystem_acl"
    assert actor["pid"] is None
    assert actor["uid"] is None
    assert actor["start_time"] is None
    assert actor["ancestry"] == []


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credentials")
def test_failed_peer_credentials_are_explicitly_unknown():
    provenance = importlib.import_module("gateway.restart_provenance")
    actor = provenance.authenticated_control_actor(None)
    assert actor["authentication"] == "unknown"
    assert actor["pid"] is None
    assert actor["ancestry"] == []


def test_rejected_transition_and_work_summary_are_durable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    record = provenance.record_restart_transition(
        "id", "rejected", checkpointed_delegations=["delegation-a"], active_work=2,
    )
    assert record["state"] == "rejected"
    assert record["checkpointed_delegations"] == ["delegation-a"]
    assert record["active_work"] == 2


def test_actor_journal_omits_raw_argv_environment_and_sensitive_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    actor = {
        "authentication": "unix_peer_credentials", "pid": os.getpid(),
        "uid": os.getuid(), "start_time": 123, "same_gateway_cgroup": True,
        "argv": ["SECRET"], "env": {"TOKEN": "SECRET"},
        "ancestry": [{
            "pid": os.getpid(), "start_time": 123, "executable": "/bin/python3",
            "operation": ["python3", "--token", "SECRET"], "argv": ["SECRET"],
        }],
        "context": {"verb": "restart", "session_id": "session-a", "token": "SECRET"},
    }
    receipt = provenance.record_restart_request(
        source="control_socket", reason="restart", automatic=False, actor=actor,
    )
    assert "SECRET" not in json.dumps(receipt)
    assert receipt["actor"]["same_gateway_cgroup"] is True
    assert receipt["actor"]["context"] == {"verb": "restart", "session_id": "session-a"}
    assert receipt["actor"]["ancestry"][0]["operation"] is None
    assert receipt["actor"]["ancestry"][0]["executable"] == "python3"


def test_unreadable_or_reused_peer_has_unknown_cgroup(monkeypatch):
    provenance = importlib.import_module("gateway.restart_provenance")
    monkeypatch.setattr(provenance, "get_process_start_time", lambda pid: None)
    left, right = socket.socketpair()
    try:
        actor = provenance.authenticated_control_actor(left)
    finally:
        left.close()
        right.close()
    assert actor["same_gateway_cgroup"] is None


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credentials")
def test_distinct_real_peer_sensitive_argv_never_enters_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")
    path = tmp_path / "socket"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        server.settimeout(5)
        script = (
            "import socket,sys; s=socket.socket(socket.AF_UNIX); "
            "s.connect(sys.argv[1]); print('connected',flush=True); sys.stdin.read(1)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", script, str(path), "--token", "SENSITIVE-ARGV"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                peer, _ = server.accept()
                with peer:
                    actor = provenance.authenticated_control_actor(peer)
                assert actor["pid"] == child.pid
                assert actor["uid"] == os.getuid()
                assert actor["ancestry"][0]["operation"] is None
                receipt = provenance.record_restart_request(
                    source="control_socket", reason="restart", automatic=False, actor=actor,
                )
                assert receipt["actor"]["pid"] == child.pid
                assert "SENSITIVE-ARGV" not in json.dumps(receipt)
                assert "SENSITIVE-ARGV" not in (tmp_path / "logs/gateway-restart-requests.jsonl").read_text(encoding="utf-8")
            finally:
                child.communicate(input="x", timeout=5)
            assert child.returncode == 0


def test_profile_context_home_takes_precedence_over_process_home(tmp_path, monkeypatch):
    from mercury_constants import reset_hermes_home_override, set_hermes_home_override
    provenance = importlib.import_module("gateway.restart_provenance")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "process-home"))
    profile_home = tmp_path / "selected-profile"
    token = set_hermes_home_override(profile_home)
    try:
        provenance.record_restart_request(source="profile", reason="restart", automatic=False)
    finally:
        reset_hermes_home_override(token)
    assert (profile_home / "logs/gateway-restart-requests.jsonl").is_file()
    assert not (tmp_path / "process-home").exists()


def test_concurrent_append_preserves_complete_correlated_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provenance = importlib.import_module("gateway.restart_provenance")

    def request(index):
        return provenance.record_restart_request(
            source="concurrent", reason="large record " + ("x" * 16000),
            automatic=False, request_id=f"concurrent-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(request, range(24)))
    lines = (tmp_path / "logs/gateway-restart-requests.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 24
    assert {record["request_id"] for record in records} == {record["request_id"] for record in receipts}
    assert all(len(record["reason"]) == 16013 for record in records)


def test_cgroup_membership_rechecks_start_and_never_persists_paths(monkeypatch):
    provenance = importlib.import_module("gateway.restart_provenance")
    starts = iter([123, 124])
    monkeypatch.setattr(provenance, "get_process_start_time", lambda pid: next(starts))
    from pathlib import Path
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "0::/private-cgroup")
    assert provenance._same_gateway_cgroup(os.getpid(), 123) is None
