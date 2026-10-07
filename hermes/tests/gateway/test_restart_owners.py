"""Lifecycle contract proof only; not a native hub/service integration proof."""
from __future__ import annotations

import importlib
import hashlib
import json
import stat
import os
import subprocess
import signal
import select
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys

import pytest

from gateway.status import get_process_start_time


@pytest.fixture
def registry():
    module = importlib.import_module("gateway.restart_owners")
    return importlib.reload(module)


def register(registry, home, *, owner="hub", pid=None, started_at=None,
             active=lambda: 0, checkpoint=lambda reason: None, detach=lambda: None):
    pid = os.getpid() if pid is None else pid
    return registry.register_owner(
        owner, profile_home=home, pid=pid,
        started_at=get_process_start_time(pid) if started_at is None else started_at,
        active_work=active, checkpoint=checkpoint, detach=detach,
    )


def test_activity_counts_real_work_not_live_idle_presence(registry, tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        work = [0]
        token = register(registry, tmp_path, pid=process.pid, active=lambda: work[0])
        assert registry.active_work_count() == 0
        work[0] = 3
        assert registry.active_work_count() == 3
        register(registry, tmp_path, owner="reused", pid=process.pid,
                 started_at=get_process_start_time(process.pid) + 1, active=lambda: 99)
        assert registry.active_work_count() == 3
        process.kill()
        process.wait(timeout=5)
        assert registry.active_work_count() == 0
        assert registry.unregister_owner(token)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def manifest(home, name="state.json"):
    home.mkdir(parents=True, exist_ok=True)
    path = home / name
    path.write_text('{"goal":"original goal","receipts":["done"],"grant":"private"}')
    path.chmod(0o600)
    return path


def test_checkpoint_detach_resume_is_private_durable_cas(registry, tmp_path, monkeypatch):
    path = manifest(tmp_path)
    detached = []
    token = register(registry, tmp_path, checkpoint=lambda reason: path,
                     detach=lambda: detached.append("restart-only"))
    with pytest.raises(RuntimeError):
        registry.detach_owners()
    synced = []
    sync = os.fsync

    def observe_sync(fd):
        synced.append((stat.S_ISDIR(os.fstat(fd).st_mode),
                       os.readlink(f"/proc/self/fd/{fd}")))
        sync(fd)

    monkeypatch.setattr(os, "fsync", observe_sync)
    receipts = registry.checkpoint_owners("planned restart")
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["registration_token"] == token
    assert receipt["coordinator_pid"] == os.getpid()
    assert receipt["coordinator_started_at"] == get_process_start_time(os.getpid())
    assert receipt["gateway_pid"] == os.getpid()
    assert receipt["gateway_started_at"] == get_process_start_time(os.getpid())
    assert receipt["checkpoint_path"] == str(path)
    assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert receipt["reason"] == "planned restart"
    assert receipt["timestamp"]
    index = tmp_path / "runtime/restart-owners.json"
    assert stat.S_IMODE(index.stat().st_mode) == 0o600
    assert stat.S_IMODE(index.parent.stat().st_mode) == 0o700
    assert "original goal" not in index.read_text()
    assert "grant" not in index.read_text()
    assert any(not directory and name == str(path) for directory, name in synced)
    assert any(directory and name == str(path.parent) for directory, name in synced)
    assert any(not directory and "restart-owners" in name for directory, name in synced)
    assert any(directory and name == str(index.parent) for directory, name in synced)
    assert registry.pending_checkpoints(tmp_path) == receipts
    assert registry.detach_owners() == 1
    assert registry.detach_owners() == 0
    assert detached == ["restart-only"]
    assert not registry.complete_owner_resume("hub", profile_home=tmp_path, sha256="wrong")
    assert registry.complete_owner_resume("hub", profile_home=tmp_path, sha256=receipt["sha256"])
    assert not registry.complete_owner_resume("hub", profile_home=tmp_path, sha256=receipt["sha256"])
    assert registry.pending_checkpoints(tmp_path) == []
    assert registry.unregister_owner(token)


def test_old_token_cannot_unregister_or_detach_replacement(registry, tmp_path):
    path = manifest(tmp_path)
    detached = []
    old = register(registry, tmp_path, active=lambda: 1,
                   checkpoint=lambda reason: path, detach=lambda: detached.append("old"))
    registry.checkpoint_owners("restart")
    new = register(registry, tmp_path, active=lambda: 2,
                   checkpoint=lambda reason: path, detach=lambda: detached.append("new"))
    assert old != new
    assert not registry.unregister_owner(old)
    assert registry.active_work_count() == 2
    with pytest.raises(RuntimeError):
        registry.detach_owners()
    registry.checkpoint_owners("restart")
    assert registry.detach_owners() == 1
    assert detached == ["new"]
    assert registry.unregister_owner(new)
    assert registry.pending_checkpoints(tmp_path)  # unregister is runtime-only


def test_callback_failure_blocks_barrier_even_after_previous_success(registry, tmp_path):
    path = manifest(tmp_path)
    fail = [False]
    detached = []

    def checkpoint(reason):
        if fail[0]:
            raise OSError("checkpoint failed")
        return path

    register(registry, tmp_path, checkpoint=checkpoint, detach=lambda: detached.append(True))
    registry.checkpoint_owners("first")
    fail[0] = True
    with pytest.raises(OSError, match="checkpoint failed"):
        registry.checkpoint_owners("second")
    with pytest.raises(RuntimeError):
        registry.detach_owners()
    assert detached == []


@pytest.mark.parametrize("phase", ["callback", "persistence"])
def test_registry_mutation_blocks_checkpoint_barrier(registry, tmp_path, monkeypatch, phase):
    path = manifest(tmp_path)
    mutated = []

    def mutate():
        if not mutated:
            mutated.append(register(registry, tmp_path, owner="replacement",
                                    checkpoint=lambda reason: path))

    def checkpoint(reason):
        if phase == "callback":
            mutate()
        return path

    register(registry, tmp_path, checkpoint=checkpoint)
    if phase == "persistence":
        replace = os.replace

        def changing_replace(*args, **kwargs):
            mutate()
            return replace(*args, **kwargs)

        monkeypatch.setattr(os, "replace", changing_replace)
    with pytest.raises(RuntimeError, match="registry changed"):
        registry.checkpoint_owners("restart")
    with pytest.raises(RuntimeError):
        registry.detach_owners()


def test_unreadable_activity_is_conservative_until_proven_dead(registry, tmp_path, monkeypatch):
    def unreadable():
        raise OSError("unreadable cache")

    register(registry, tmp_path, active=unreadable)
    monkeypatch.setattr(registry, "process_identity_state", lambda pid, birth: "unverified")
    assert registry.active_work_count() == 1
    monkeypatch.setattr(registry, "process_identity_state", lambda pid, birth: "dead")
    assert registry.active_work_count() == 0


@pytest.mark.parametrize("kind", ["public", "symlink", "parent_symlink", "outside", "directory"])
def test_checkpoint_rejects_unsafe_private_state(registry, tmp_path, kind):
    home = tmp_path / "profile"
    path = manifest(home)
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        link = home / "link"
        link.symlink_to(path)
        path = link
    elif kind == "parent_symlink":
        (home / "alias").symlink_to(home, target_is_directory=True)
        path = home / "alias/state.json"
    elif kind == "outside":
        path = manifest(tmp_path, "outside.json")
    else:
        path = home
    register(registry, home, checkpoint=lambda reason: path)
    with pytest.raises((OSError, ValueError)):
        registry.checkpoint_owners("restart")
    with pytest.raises(RuntimeError):
        registry.detach_owners()
    assert not (home / "runtime/restart-owners.json").exists()


@pytest.mark.parametrize("operation", ["write", "file_sync", "directory_sync"])
def test_persistence_failure_never_authorizes_detach(registry, tmp_path, monkeypatch, operation):
    path = manifest(tmp_path)
    register(registry, tmp_path, checkpoint=lambda reason: path)
    sync = os.fsync
    replace = os.replace

    def fail_sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if directory == (operation == "directory_sync"):
            raise OSError("durability failure")
        sync(fd)

    def fail_replace(*args, **kwargs):
        raise OSError("durability failure")

    monkeypatch.setattr(os, "replace" if operation == "write" else "fsync",
                        fail_replace if operation == "write" else fail_sync)
    with pytest.raises(OSError, match="durability failure"):
        registry.checkpoint_owners("restart")
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(RuntimeError):
        registry.detach_owners()


def test_pending_validates_digest_but_explicit_discard_can_revoke_bad_state(registry, tmp_path):
    path = manifest(tmp_path)
    register(registry, tmp_path, checkpoint=lambda reason: path)
    receipt = registry.checkpoint_owners("restart")[0]
    path.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="digest"):
        registry.pending_checkpoints(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        registry.complete_owner_resume("hub", profile_home=tmp_path, sha256=receipt["sha256"])
    assert registry.discard_checkpoint("hub", profile_home=tmp_path)
    assert not registry.discard_checkpoint("hub", profile_home=tmp_path)
    assert registry.pending_checkpoints(tmp_path) == []
    assert path.exists()  # native owner owns grant deletion, not this registry


def test_profile_identity_is_pinned_and_receipts_do_not_cross_profiles(registry, tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    path_a, path_b = manifest(first), manifest(second)
    monkeypatch.chdir(tmp_path)
    a = register(registry, "first", checkpoint=lambda reason: path_a, active=lambda: 2)
    b = register(registry, second, checkpoint=lambda reason: path_b, active=lambda: 3)
    monkeypatch.chdir(second)
    assert registry.active_work_count() == 5
    assert len(registry.checkpoint_owners("restart")) == 2
    assert registry.pending_checkpoints(first)[0]["registration_token"] == a
    assert registry.pending_checkpoints(second)[0]["registration_token"] == b
    assert registry.discard_checkpoint("hub", profile_home=first)
    assert registry.pending_checkpoints(first) == []
    assert len(registry.pending_checkpoints(second)) == 1


def test_checkpoint_callback_can_mutate_registration_from_another_thread(registry, tmp_path):
    path = manifest(tmp_path)

    def checkpoint(reason):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(register, registry, tmp_path, owner="other",
                        checkpoint=lambda reason: path).result(timeout=3)
        return path

    register(registry, tmp_path, checkpoint=checkpoint)
    with pytest.raises(RuntimeError, match="registry changed"):
        registry.checkpoint_owners("restart")


@pytest.mark.parametrize("state", ["dead", "unverified"])
def test_detach_requires_exact_live_coordinator_identity(registry, tmp_path, monkeypatch, state):
    path = manifest(tmp_path)
    detached = []
    register(registry, tmp_path, checkpoint=lambda reason: path,
             detach=lambda: detached.append(True))
    registry.checkpoint_owners("restart")
    monkeypatch.setattr(registry, "process_identity_state", lambda pid, birth: state)
    with pytest.raises(RuntimeError, match="identity"):
        registry.detach_owners()
    assert detached == []


@pytest.mark.parametrize("kind", ["symlink", "public", "corrupt", "digest", "outside"])
def test_pending_rejects_invalid_index_or_checkpoint(registry, tmp_path, kind):
    path = manifest(tmp_path)
    register(registry, tmp_path, checkpoint=lambda reason: path)
    registry.checkpoint_owners("restart")
    index = tmp_path / "runtime/restart-owners.json"
    if kind == "symlink":
        backup = tmp_path / "index-backup"
        index.rename(backup)
        index.symlink_to(backup)
    elif kind == "public":
        index.chmod(0o644)
    elif kind == "corrupt":
        index.write_text("{}")
    else:
        data = json.loads(index.read_text())
        if kind == "digest":
            data["owners"][0]["sha256"] = "0" * 64
        else:
            data["owners"][0]["checkpoint_path"] = "/tmp/not-under-profile.json"
        index.write_text(json.dumps(data))
    with pytest.raises((OSError, ValueError)):
        registry.pending_checkpoints(tmp_path)


def test_fsync_failure_after_index_replace_never_authorizes_detach(registry, tmp_path, monkeypatch):
    path = manifest(tmp_path)
    register(registry, tmp_path, checkpoint=lambda reason: path)
    sync = os.fsync

    def fail_index_directory(fd):
        if (stat.S_ISDIR(os.fstat(fd).st_mode)
                and os.readlink(f"/proc/self/fd/{fd}") == str(tmp_path / "runtime")):
            raise OSError("post-replace durability failure")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fail_index_directory)
    with pytest.raises(OSError, match="post-replace"):
        registry.checkpoint_owners("restart")
    monkeypatch.setattr(os, "fsync", sync)
    assert (tmp_path / "runtime/restart-owners.json").exists()
    with pytest.raises(RuntimeError):
        registry.detach_owners()


_WORKER = r'''
import os, sys, time
from pathlib import Path
home = Path(sys.argv[1])
with (home / "side-effects").open("a") as handle:
    handle.write("already completed\n")
    handle.flush()
    os.fsync(handle.fileno())
print("completed", flush=True)
time.sleep(60)
'''

_OWNER = r'''
import json, os, socket, sys, time
from pathlib import Path
from gateway.restart_owners import register_owner, checkpoint_owners, detach_owners
from gateway.status import get_process_start_time
home = Path(sys.argv[1])
worker_pid = int(sys.argv[2])
path = home / "native-private-state.json"
transport, peer_transport = socket.socketpair()
def checkpoint(reason):
    state = {"goal": "finish original goal", "grants": ["fixture-grant"],
             "peers": [{"id": "worker", "pid": worker_pid}],
             "messages": ["original message"],
             "tasks": [{"id": "side-effect", "status": "completed"}],
             "receipts": [{"id": "side-effect", "result": "already completed"}]}
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    return path
def detach():
    # Fixture owns restart-only transport detach, not task/grant deletion.
    transport.close()
register_owner("fixture-hub", profile_home=home, pid=os.getpid(),
               started_at=get_process_start_time(os.getpid()), active_work=lambda: 1,
               checkpoint=checkpoint, detach=detach)
receipts = checkpoint_owners("model-free SIGKILL lifecycle fixture")
assert detach_owners() == 1
assert transport.fileno() == -1
assert peer_transport.fileno() >= 0
assert path.exists()
print(json.dumps(receipts), flush=True)
time.sleep(60)
'''

_RECOVER = r'''
import json, sys
from pathlib import Path
from gateway.restart_owners import pending_checkpoints, complete_owner_resume
home = Path(sys.argv[1])
pending = pending_checkpoints(home)
if not pending:
    print(json.dumps({"resumed": False}), flush=True)
else:
    receipt = pending[0]
    private = json.loads(Path(receipt["checkpoint_path"]).read_text())
    # Reconstruct actual fixture peer/message/task/receipt state first.
    restored = {key: private[key] for key in
                ("goal", "grants", "peers", "messages", "tasks", "receipts")}
    completed = {item["id"] for item in restored["receipts"]}
    if "side-effect" not in completed:
        with (home / "side-effects").open("a") as handle:
            handle.write("reexecuted\n")
    assert complete_owner_resume("fixture-hub", profile_home=home, sha256=receipt["sha256"])
    print(json.dumps({"resumed": True, "restored": restored}), flush=True)
'''


@pytest.mark.parametrize("discard", [False, True])
def test_lifecycle_fixture_survives_owner_and_worker_sigkill_without_replaying_completed_effect(
        registry, tmp_path, discard):
    """Actual subprocess kill/recovery; deliberately NOT native hub service proof."""
    worker = subprocess.Popen([sys.executable, "-u", "-c", _WORKER, str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    owner = None
    exits = {}
    try:
        assert select.select([worker.stdout], [], [], 5)[0], "worker readiness timeout"
        assert worker.stdout.readline().strip() == "completed"
        owner = subprocess.Popen([sys.executable, "-u", "-c", _OWNER, str(tmp_path), str(worker.pid)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([owner.stdout], [], [], 5)[0], "owner checkpoint timeout"
        line = owner.stdout.readline()
        if not line:
            raise AssertionError(owner.communicate(timeout=5))
        receipts = json.loads(line)
        assert receipts[0]["coordinator_pid"] == owner.pid
        assert receipts[0]["coordinator_started_at"] == get_process_start_time(owner.pid)
        assert registry.pending_checkpoints(tmp_path) == receipts
        evidence = os.environ.get("RESTART_OWNERS_EVIDENCE")
        if evidence and not discard:
            destination = Path(evidence)
            shutil.copyfile(tmp_path / "runtime/restart-owners.json", destination / "restart-owners.json")
            (destination / "restart-owners.json").chmod(0o600)
        if discard:
            assert registry.discard_checkpoint("fixture-hub", profile_home=tmp_path)
        owner.kill()
        worker.kill()
        exits = {"owner_exit": owner.wait(timeout=5), "worker_exit": worker.wait(timeout=5)}
        assert exits == {"owner_exit": -signal.SIGKILL, "worker_exit": -signal.SIGKILL}
        results = []
        for _ in range(2):
            result = subprocess.run([sys.executable, "-u", "-c", _RECOVER, str(tmp_path)],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            results.append(json.loads(result.stdout))
        assert results[0]["resumed"] is not discard
        assert results[1] == {"resumed": False}
        if not discard:
            restored = results[0]["restored"]
            assert restored["goal"] == "finish original goal"
            assert restored["peers"] == [{"id": "worker", "pid": worker.pid}]
            assert restored["messages"] == ["original message"]
            assert restored["tasks"] == [{"id": "side-effect", "status": "completed"}]
            assert restored["receipts"] == [{"id": "side-effect", "result": "already completed"}]
        assert (tmp_path / "side-effects").read_text() == "already completed\n"
        assert registry.pending_checkpoints(tmp_path) == []
        if evidence:
            (Path(evidence) / f"restart-owners-kill-{'discard' if discard else 'resume'}.json").write_text(
                json.dumps({"proof": "model-free lifecycle contract; not native hub/service proof",
                            **exits, "drivers": results, "side_effect_executions": 1}, indent=2) + "\n")
    finally:
        for process in (owner, worker):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


def test_actual_processes_cas_consume_one_receipt_once(registry, tmp_path):
    path = manifest(tmp_path)
    register(registry, tmp_path, checkpoint=lambda reason: path)
    receipt = registry.checkpoint_owners("restart")[0]
    code = (
        "import json, sys; from gateway.restart_owners import complete_owner_resume; "
        "print(json.dumps(complete_owner_resume('hub', profile_home=sys.argv[1], sha256=sys.argv[2])))"
    )

    def acknowledge():
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path), receipt["sha256"]],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: acknowledge(), range(4)))
    assert sorted(results) == [False, False, False, True]
    assert registry.pending_checkpoints(tmp_path) == []


@pytest.mark.parametrize("field", ["registration_token", "coordinator_pid",
                                  "coordinator_started_at", "gateway_pid", "gateway_started_at"])
def test_detach_rejects_receipt_for_another_registration_or_process(registry, tmp_path, field):
    path = manifest(tmp_path)
    detached = []
    register(registry, tmp_path, checkpoint=lambda reason: path,
             detach=lambda: detached.append(True))
    registry.checkpoint_owners("restart")
    index = tmp_path / "runtime/restart-owners.json"
    data = json.loads(index.read_text())
    value = data["owners"][0][field]
    data["owners"][0][field] = "another-token" if isinstance(value, str) else value + 1
    index.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="matching durable"):
        registry.detach_owners()
    assert detached == []
