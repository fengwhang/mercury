"""M5a (matrix observatory D18): the respawn pass — 0-agent resilience.

On sidecar/gateway start, BEFORE serving traffic: for every live 0-agent
in observatory state.db, resume its session and re-attach the same
rooms/spaces/MXIDs. Restart is not death — nothing is deleted here.

Per engine:

- **hermes-side**: rebuild an ``AIAgent`` bound to the stored session id
  on the same hermes state.db (the session row + transcript ARE the
  agent; the turn prologue resolves the compression-lineage tip and
  loads prior history on the first prompt).
- **omp-side**: restart a headless RPC child pinned to the orchestrator's
  existing session JSONL (``--resume <path>``; the file lives under the
  observatory dir because spawn pinned it with ``--session-dir``).

Then the renderer re-ensures the space/room tree (``apply_plan`` is a
converging diff — idempotent, no duplicate rooms; membership invites are
part of creation, so re-ensure also heals rooms that lost the owner).

Crash-recovery ordering: the purge journal (``observatory.spawn``) is
replayed FIRST — a /exit that crashed mid-purge must finish annihilating
before any live-node resume runs, and journal replay only ever deletes.

Handoff contract (D18, explicit): **subagents get NO respawn.** Orphaned
``async_delegations`` rows (children whose parent orchestrator died with
the old process) are left to the EXISTING restart-recovery machinery —
``tools/async_delegation.restore_running_delegations`` (stale monitor /
force-finalize) runs in the hermes engine process on its own boot and
finalizes those rows; this pass neither reads nor writes that table.
Their matrix rooms die with the parent's purge cascade (D8), so nothing
observable is left dangling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from observatory.renderer import Renderer
from observatory.spawn import (
    SESSION_MATERIALIZED_KEY,
    SKIP_RESPAWN_KINDS,
    OrchestratorHandle,
    OrchestratorRegistry,
    build_hermes_agent,
    build_omp_child,
    is_session_materialized,
    omp_session_file,
    replay_purge_journal,
)
from observatory.state import ObservatoryState

logger = logging.getLogger(__name__)


@dataclass
class RespawnReport:
    """One respawn pass outcome (operator-visible, test-asserted)."""

    resumed: list[str] = field(default_factory=list)   # node ids resumed
    skipped: list[dict[str, Any]] = field(default_factory=list)  # node id + reason
    failed: list[dict[str, Any]] = field(default_factory=list)   # node id + error
    deferred_purges: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "resumed": list(self.resumed),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "deferred_purges": list(self.deferred_purges),
        }


# ============================================================================
# Per-engine resume (lazy heavy imports; injectable for tests)
# ============================================================================


def resume_hermes_orchestrator(
    row: dict[str, Any],
    *,
    mercury_home: str | Path | None = None,
    agent_factory: Optional[Callable[[str], Any]] = None,
) -> Any:
    """Rebuild the hermes 0-agent's AIAgent from its stored session id.

    Fails hard (raises) when the session row is gone — the respawn pass
    reports the failure WITHOUT touching the node (D18: only /exit or
    session reset kills a 0-agent; a failed resume is an operator event,
    not a death). A ref that never completed one turn raises the
    never-materialized error (run on the live spawn handle, never resume);
    only a previously-materialized ref raises "deleted without /exit?".
    """
    session_id = str(row.get("session_ref") or "")
    if not session_id:
        raise RuntimeError(f"respawn: node {row['node_id']} has no session_ref")

    if agent_factory is not None:
        return agent_factory(session_id)

    # Validate the row exists — exactly like `mercury --resume`: no row
    # means not found (the compression-lineage tip walk happens at turn
    # time inside run_conversation, mirroring the CLI's behavior).
    from mercury_state import SessionDB

    home = Path(mercury_home) if mercury_home is not None else None
    db_path = (home / "hermes" / "state.db") if home is not None else SessionDB().db_path
    session_db = SessionDB(db_path=db_path)
    try:
        if session_db.get_session(session_id) is None:
            if not is_session_materialized(row):
                raise RuntimeError(
                    f"respawn: session {session_id} never materialized "
                    f"in {db_path} (first turn pending — run on the live "
                    "spawn handle, never resume)"
                )
            raise RuntimeError(
                f"respawn: session {session_id} not found in {db_path} "
                "(deleted without /exit?)"
            )
    finally:
        session_db.close()

    return build_hermes_agent(
        mercury_home=mercury_home,
        session_id=session_id,
        model=(row.get("extra") or {}).get("model"),
    )


def restart_omp_orchestrator(
    row: dict[str, Any],
    *,
    mercury_home: str | Path | None = None,
    workdir: Optional[str] = None,
    omp_child_factory: Optional[Callable[[str], Any]] = None,
) -> Any:
    """Restart the omp 0-agent's RPC child on its existing session JSONL.

    The resumed child must land on the SAME file (D18: same session, same
    MXID/rooms); a mismatch is a hard failure and the child is stopped —
    never silently fork a second session. A ref that never completed one
    turn raises the never-materialized error (run live, never resume).
    """
    session_file = str(row.get("session_ref") or "")
    if not session_file:
        raise RuntimeError(f"respawn: node {row['node_id']} has no session_ref")
    if not Path(session_file).is_file():
        if not is_session_materialized(row):
            raise RuntimeError(
                f"respawn: omp session file {session_file} never materialized "
                "(first turn pending — run on the live spawn handle, never resume)"
            )
        raise RuntimeError(
            f"respawn: omp session file {session_file} is gone "
            "(deleted without /exit?)"
        )

    child = (
        omp_child_factory(session_file)
        if omp_child_factory is not None
        else build_omp_child(
            resume_session=session_file,
            mercury_home=mercury_home,
            workdir=workdir,
        )
    )
    live = omp_session_file(child)
    if Path(live).resolve() != Path(session_file).resolve():
        try:
            child.stop()
        except Exception:  # noqa: BLE001
            logger.exception("respawn: mismatched omp child teardown failed")
        raise RuntimeError(
            f"respawn: omp child resumed onto {live}, expected {session_file}"
        )
    return child


# ============================================================================
# The pass
# ============================================================================


async def respawn_pass(
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    renderer: Optional[Renderer] = None,
    mercury_home: str | Path | None = None,
    workdir: Optional[str] = None,
    hermes_factory: Optional[Callable[[str], Any]] = None,
    omp_child_factory: Optional[Callable[[str], Any]] = None,
) -> RespawnReport:
    """D18 respawn pass. Idempotent: nodes already holding a live handle
    in ``registry`` are skipped, renderer re-ensure converges, journal
    replay only deletes. Never raises for per-node failures — each lands
    in the report so one broken agent cannot block the sidecar boot."""
    report = RespawnReport()

    # 1. Crash recovery first: finish any /exit that died mid-purge.
    executor = getattr(renderer, "executor", None) if renderer is not None else None
    report.deferred_purges = await replay_purge_journal(state, executor=executor)

    # 2. Resume every live depth-0 node (deterministic order from state).
    for row in state.get_live():
        node_id = row["node_id"]
        if row["depth"] != 0:
            continue  # D18: subagents get NO respawn (see module docstring)
        kind = (row.get("extra") or {}).get("kind", "")
        if kind in SKIP_RESPAWN_KINDS:
            # gateway: its session lives in (and is resumed by) the gateway
            # process itself — this pass must not double-own it.
            # manual-run: observe-only discovery nodes (D14), nothing to
            # resume. Spawned orchestrators carry kind "" (tree convention).
            report.skipped.append(
                {"node_id": node_id, "reason": f"{kind or 'agent'} node"}
            )
            continue
        if registry.get(node_id) is not None:
            report.skipped.append({"node_id": node_id, "reason": "already resumed"})
            continue

        try:
            if row["engine"] == "hermes":
                agent = resume_hermes_orchestrator(
                    row,
                    mercury_home=mercury_home,
                    agent_factory=hermes_factory,
                )
                handle = OrchestratorHandle(
                    node_id=node_id,
                    engine="hermes",
                    name=row["name"],
                    session_ref=str(row["session_ref"]),
                    model=(row.get("extra") or {}).get("model"),
                    agent=agent,
                )
            elif row["engine"] == "omp":
                child = restart_omp_orchestrator(
                    row,
                    mercury_home=mercury_home,
                    workdir=workdir,
                    omp_child_factory=omp_child_factory,
                )
                handle = OrchestratorHandle(
                    node_id=node_id,
                    engine="omp",
                    name=row["name"],
                    session_ref=str(row["session_ref"]),
                    model=(row.get("extra") or {}).get("model"),
                    rpc=child,
                )
            else:  # state.ENGINES guards inserts; defensive here
                raise RuntimeError(f"unknown engine {row['engine']!r}")
        except Exception as exc:  # noqa: BLE001 — per-node isolation
            logger.error("respawn: node %s failed: %s", node_id, exc)
            report.failed.append({"node_id": node_id, "error": str(exc)})
            continue

        registry.register(handle)
        report.resumed.append(node_id)
        logger.info("respawn: resumed %s (%s, session %s)",
                    node_id, handle.engine, handle.session_ref)

    # 3. Re-ensure rooms/spaces/membership — idempotent renderer re-run
    # (converging diff; heals a homeserver that lost state, re-invites
    # nothing that already holds membership).
    if renderer is not None and renderer.executor is not None:
        try:
            applied = await renderer.apply_plan(renderer.build_plan())
            if applied:
                logger.info("respawn: re-ensured %d render intents", len(applied))
        except Exception:  # noqa: BLE001 — render failure must not kill boot
            logger.exception("respawn: renderer re-ensure failed")

    return report
