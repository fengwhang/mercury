"""D8 annihilation convergence (spec §1 D8, §5.3, §9).

Depth-1 death purges room+space instantly with the summary to the parent
only; depth>=2 settles (marker + summary, artifacts survive); depth-0
cascades the whole subtree. Regression focus: a 404-already-gone room
purge (retried death, double hook+poll delivery) must never abort the
sibling space purge — the reported "room purges but the space survives"
stuck state. Send/Detach failures are soft and never block purges; a
non-404 wedged purge raises after attempting every sibling so rows
survive for retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixError
from observatory.renderer import (
    DetachChild,
    IntentExecutor,
    PurgeRoom,
    Renderer,
    SendMessage,
    SETTLED_MARKER,
)
from observatory.state import ObservatoryState, StateError, purge_on_death

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"
ORCH = "orch"
SA = "sa-tests"
SSA = "ssa-lint"


def _matrix_error(status: int, message: str) -> MatrixError:
    return MatrixError(
        "DELETE", "/_synapse/admin/v1/rooms/x",
        status, {"errcode": "M_NOT_FOUND" if status == 404 else "M_UNKNOWN", "error": message},
    )


@dataclass
class FakeClient:
    """Recording MatrixClient surface the death batch touches."""

    calls: list = field(default_factory=list)
    fail_purge: dict = field(default_factory=dict)  # room_id -> HTTP status
    fail_detach: int | None = None
    fail_send: int | None = None
    _n: int = 0

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body))
        if self.fail_send is not None:
            raise _matrix_error(self.fail_send, "send wedged")
        self._n += 1
        return f"$e{self._n}"

    async def set_space_child(self, space_id, child_id, *, sender, remove=False):
        self.calls.append(("detach", space_id, child_id))
        if self.fail_detach is not None:
            raise _matrix_error(self.fail_detach, "detach wedged")

    async def delete_room(self, room_id, *, block=False, purge=True):
        self.calls.append(("delete", room_id, block, purge))
        if room_id in self.fail_purge:
            raise _matrix_error(self.fail_purge[room_id], f"wedged {room_id}")
        return {}


def seed_state(tmp_path: Path) -> ObservatoryState:
    state = ObservatoryState(tmp_path / "state.db")

    def add(node_id: str, name: str, *, engine: str, parent: str | None):
        slug = assign_slug(name, state)
        return state.add_node(
            node_id,
            engine=engine,
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug),
            session_ref=f"session:{node_id}",
            parent_node_id=parent,
        )

    add(GW, "gateway agent", engine="hermes", parent=None)
    add(ORCH, "auth-refactor", engine="hermes", parent=None)
    add(SA, "test-sweep", engine="omp", parent=ORCH)
    add(SSA, "lint-fix", engine="omp", parent=SA)
    for node in (GW, ORCH, SA, SSA):
        state.set_space_id(node, f"!s-{node}:x")
        state.set_room_id(node, f"!r-{node}:x")
    return state


def make_renderer(state: ObservatoryState, client: FakeClient) -> Renderer:
    return Renderer(
        state,
        gateway_node_id=GW,
        server_name=SERVER,
        owner_mxid=OWNER,
        executor=IntentExecutor(client, state, owner_mxid=OWNER, server_name=SERVER),
    )


def deletes(client: FakeClient) -> set[str]:
    return {c[1] for c in client.calls if c[0] == "delete"}


def test_purge_predicate_is_depth1_only():
    assert purge_on_death(0) is False
    assert purge_on_death(1) is True
    assert purge_on_death(2) is False
    assert purge_on_death(9) is False


def test_depth1_plan_emits_room_and_space_purges(tmp_path):
    state = seed_state(tmp_path)
    renderer = Renderer(state, gateway_node_id=GW, server_name=SERVER, owner_mxid=OWNER, executor=None)
    intents = renderer.plan_death(SA, status="completed", summary="3 tests green")
    sends = [i for i in intents if isinstance(i, SendMessage)]
    purges = {i.room_id for i in intents if isinstance(i, PurgeRoom)}
    assert len(sends) == 1 and sends[0].room_key == ORCH
    assert "3 tests green" in sends[0].body
    assert purges == {"!r-sa-tests:x", "!s-sa-tests:x", "!r-ssa-lint:x", "!s-ssa-lint:x"}
    detach = next(i for i in intents if isinstance(i, DetachChild))
    assert (detach.space_id, detach.child_id) == ("!s-orch:x", "!s-sa-tests:x")


@pytest.mark.asyncio
async def test_depth1_room_gone_still_purges_space(tmp_path):
    """Regression: room 404 (retried/double death) never aborts the space purge."""
    state = seed_state(tmp_path)
    client = FakeClient(fail_purge={"!r-sa-tests:x": 404})
    await make_renderer(state, client).render_death(SA, status="completed", summary="3 tests green")
    assert deletes(client) == {"!r-sa-tests:x", "!s-sa-tests:x", "!r-ssa-lint:x", "!s-ssa-lint:x"}
    for node in (SA, SSA):
        with pytest.raises(StateError):
            state.get(node)


@pytest.mark.asyncio
async def test_depth1_detach_failure_does_not_block_purges(tmp_path):
    state = seed_state(tmp_path)
    client = FakeClient(fail_detach=500)
    await make_renderer(state, client).render_death(SA, status="completed", summary="3 tests green")
    assert deletes(client) == {"!r-sa-tests:x", "!s-sa-tests:x", "!r-ssa-lint:x", "!s-ssa-lint:x"}
    with pytest.raises(StateError):
        state.get(SA)


@pytest.mark.asyncio
async def test_depth1_parent_send_failure_does_not_block_purges(tmp_path):
    state = seed_state(tmp_path)
    client = FakeClient(fail_send=500)
    await make_renderer(state, client).render_death(SA, status="completed", summary="3 tests green")
    assert deletes(client) == {"!r-sa-tests:x", "!s-sa-tests:x", "!r-ssa-lint:x", "!s-ssa-lint:x"}
    with pytest.raises(StateError):
        state.get(SA)


@pytest.mark.asyncio
async def test_depth1_wedged_space_raises_but_siblings_attempted_then_retry_heals(tmp_path):
    state = seed_state(tmp_path)
    client = FakeClient(fail_purge={"!s-sa-tests:x": 500})
    with pytest.raises(RuntimeError, match="not converged"):
        await make_renderer(state, client).render_death(SA, status="completed", summary="x")
    # every sibling was still attempted; rows survive dead for retry
    assert deletes(client) == {"!r-sa-tests:x", "!s-sa-tests:x", "!r-ssa-lint:x", "!s-ssa-lint:x"}
    assert state.get(SA)["status"] == "dead"
    client.fail_purge.clear()
    await make_renderer(state, client).render_death(SA, status="completed", summary="x")
    with pytest.raises(StateError):
        state.get(SA)


@pytest.mark.asyncio
async def test_depth2_death_settles_marks_dead_without_purge(tmp_path):
    state = seed_state(tmp_path)
    client = FakeClient()
    intents = await make_renderer(state, client).render_death(SSA, status="completed", summary="lint fixed")
    assert deletes(client) == set()
    assert not any(isinstance(i, PurgeRoom) for i in intents)
    settled = next(i for i in intents if isinstance(i, SendMessage) and i.room_key == SSA)
    assert settled.body == SETTLED_MARKER
    assert state.get(SSA)["status"] == "dead"


@pytest.mark.asyncio
async def test_depth0_exit_cascades_whole_subtree(tmp_path):
    state = seed_state(tmp_path)
    client = FakeClient()
    intents = await make_renderer(state, client).render_death(ORCH, status="exit", summary="done")
    sends = [i for i in intents if isinstance(i, SendMessage)]
    assert len(sends) == 1 and sends[0].room_key == GW
    assert deletes(client) == {
        "!r-orch:x", "!s-orch:x",
        "!r-sa-tests:x", "!s-sa-tests:x",
        "!r-ssa-lint:x", "!s-ssa-lint:x",
    }
    assert "!s-gw:x" not in deletes(client) and "!r-gw:x" not in deletes(client)
    for node in (ORCH, SA, SSA):
        with pytest.raises(StateError):
            state.get(node)


@pytest.mark.asyncio
async def test_depth1_purge_frees_mxid_for_successor(tmp_path):
    """D17: purged rows leave no tombstone; a same-named successor inherits the MXID only."""
    state = seed_state(tmp_path)
    client = FakeClient()
    mxid = state.get(SA)["mxid"]
    slug = state.get(SA)["slug"]
    await make_renderer(state, client).render_death(SA, status="completed", summary="x")
    assert state.find_live_by_slug(slug) == []
    state.add_node(
        SA, engine="omp", name="test-sweep", slug=slug, mxid=mxid,
        session_ref="session:sa-tests-2", parent_node_id=ORCH,
    )
    assert state.get(SA)["status"] == "live"
    assert state.get(SA)["mxid"] == mxid
