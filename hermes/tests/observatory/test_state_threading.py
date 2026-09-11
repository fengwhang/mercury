"""Thread-safety of the Observatory agent-tree store (VM round 3, defect 1).

``/spawn`` + ``/spawnomp`` run INSIDE the gateway event loop via the
Matrix pass-through while ``try_boot_sidecar`` opened the same state on
a daemon boot thread — sqlite objects created on one thread, used on
another. The store opens with ``check_same_thread=False`` + an RLock;
this proves cross-thread use never raises.
"""
from __future__ import annotations

import threading
from pathlib import Path

from observatory.state import ObservatoryState


def _node(node_id: str, **over: object) -> dict:
    kw = dict(
        engine="hermes",
        name=node_id,
        slug=node_id,
        mxid=f"@merc_{node_id}:mercury.local",
        session_ref=f"session:{node_id}",
    )
    kw.update(over)
    return kw  # type: ignore[return-value]


def test_state_cross_thread_use(tmp_path: Path):
    """Open on main, drive from another thread: no ProgrammingError."""
    with ObservatoryState(tmp_path / "state.db") as st:
        st.add_node("gw", **_node("gw"))
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                st.add_node("child", parent_node_id="gw", **_node("child"))
                assert st.get("child")["depth"] == 1
                st.set_meta("k", "v")
                assert st.get_meta("k") == "v"
                assert any(n["node_id"] == "child" for n in st.get_live())
                assert st.find_live_by_slug("child")
                assert st.children_of("gw")
                assert st.get_subtree("gw")
                st.set_room_id("child", "!r:hs")
                st.mark_dead("child")
                st.mark_deleted_and_purge("child")
            except BaseException as exc:  # noqa: BLE001 — collected, asserted below
                errors.append(exc)

        t = threading.Thread(target=worker, name="state-xthread-probe")
        t.start()
        t.join(timeout=30)
        assert not t.is_alive()
        assert errors == []
        assert st.get("gw")["status"] == "live"


def test_state_concurrent_writers(tmp_path: Path):
    """N threads inserting + reading at once: nothing lost, nothing raised."""
    with ObservatoryState(tmp_path / "state.db") as st:
        st.add_node("gw", **_node("gw"))
        errors: list[BaseException] = []
        barrier = threading.Barrier(5)

        def worker(i: int) -> None:
            try:
                nid = f"w{i}"
                barrier.wait(timeout=30)
                st.add_node(nid, parent_node_id="gw", **_node(nid, slug=f"w{i}"))
                for _ in range(20):
                    st.get(nid)
                    st.get_live()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not any(t.is_alive() for t in threads)
        assert errors == []
        assert len(st.children_of("gw")) == 5
