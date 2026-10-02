"""Concurrent recovery preserves one engine and honors durable !exit."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from observatory import rooms, spawn
from observatory.state import ObservatoryState


def recover_setup(tmp_path, monkeypatch):
    state = ObservatoryState(tmp_path / "state.db")
    state.add_node("agent", engine="omp", name="agent", slug="agent", mxid="agent",
                   session_ref="saved.jsonl")
    state.set_room_id("agent", "#agent")
    registry = spawn.OrchestratorRegistry()
    started, release = threading.Event(), threading.Event()
    child = SimpleNamespace(stop=Mock())
    built = []

    def build(**kwargs):
        built.append(kwargs)
        started.set()
        assert release.wait(5)
        return child

    monkeypatch.setattr(spawn, "build_omp_child", build)

    def recover():
        return spawn.resurrect_omp_handle(
            state=state, registry=registry, node_id="agent", channel="#agent")

    return state, registry, started, release, child, built, recover


def test_boot_and_message_recovery_share_one_child(tmp_path, monkeypatch):
    state, registry, started, release, child, built, recover = recover_setup(tmp_path, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(recover)
            assert started.wait(5)
            second = workers.submit(recover)
            release.set()
            assert first.result(5) is child
            assert second.result(5) is child
        assert len(built) == 1
        assert built[0]["resume_session"] == "saved.jsonl"
        assert registry.get("agent").rpc is child
        rooms._omp_rooms["agent"]["busy"] = True
        assert recover() is child
        assert rooms._omp_rooms["agent"]["busy"]
    finally:
        release.set()
        rooms.drop_omp_room("agent")
        state.close()


def test_exit_during_startup_cannot_resurrect_agent(tmp_path, monkeypatch):
    state, registry, started, release, child, _, recover = recover_setup(tmp_path, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(recover)
            assert started.wait(5)
            spawn.begin_exit(state, "agent")
            release.set()
            with pytest.raises(RuntimeError, match="exited during recovery"):
                pending.result(5)
        child.stop.assert_called_once()
        assert registry.get("agent") is None
        assert "agent" not in rooms._omp_rooms
        assert state.get("agent")["status"] == "dead"
    finally:
        release.set()
        state.close()
