"""M5a (matrix observatory D9/D8/D18): spawned-orchestrator lifecycle.

``/spawn <name>`` (hermes engine) and ``/spawnomp <name>`` (omp engine)
create top-level 0-agents; ``/exit`` annihilates one. This module owns the
ENGINE side of that lifecycle — the session/process handles and the
state.db rows — and drives the renderer for the matrix side:

- **spawn**: build the engine handle (fresh hermes session via the same
  AIAgent/SessionDB machinery the delegate path and ``mercury -z`` use;
  headless omp RPC child with its session JSONL pinned under the
  observatory dir via ``--session-dir``), register the depth-0 node, then
  converge the renderer plan so the agent's space+room exist (§3).
- **exit**: D8 depth-0 cascade — the WHOLE subtree's rooms/spaces are
  purged and every row deleted. Crash-atomicity (D18-critical): the
  dead-marks and the write-ahead purge journal land in ONE sqlite
  transaction (``begin_exit``); the purge intents are replayed from the
  journal on startup (``replay_purge_journal``, called by the respawn
  pass), so a crash mid-purge never resurrects a killed agent — a journaled
  node is dead-or-deleted in every observable state, and a crashed purge
  completes on the next boot instead of being forgotten.

Engine-ordering law for /exit: durable dead-mark FIRST, engine kill
second. A crash between them orphans a process (operator-visible) but can
never leave a killed agent live in state.db for the respawn pass to
restart — that would be resurrection.

Subagent stop at parent death (D8 "children stopped first"): depth>=1
children of a 0-agent are delegate_task children owned by the delegation
machinery's live-child registries (``tools/omp_delegation`` /
``delegate_tool``); their process stop rides that existing path. This
module's cascade covers their matrix artifacts and state rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from observatory.config_gen import ObservatoryPaths
from observatory.identity import assign_slug, virtual_mxid
from observatory.renderer import (
    DetachChild,
    LeaveRoom,
    PurgeRoom,
    RenderIntent,
    Renderer,
    SendMessage,
)
from observatory.state import ENGINES, ObservatoryState, StateError

logger = logging.getLogger(__name__)

#: ``extra.kind`` convention (tree.py / render_live.seed_nodes): spawned
#: agents carry NO kind — plain agent nodes (kind "") plan as orchestrator
#: subspaces; only the gateway agent ("gateway"), cron jobs ("cron-job")
#: and manual runs ("manual-run") are kind-stamped. The respawn pass
#: therefore resumes every depth-0 live node except those kinds (D18
#: "every live 0-agent").
GATEWAY_KIND = "gateway"
SKIP_RESPAWN_KINDS = ("gateway", "manual-run")

#: state.db meta key holding the write-ahead purge journal (D18).
PURGE_JOURNAL_KEY = "purge-journal"

#: omp session JSONLs for spawned orchestrators live here (under the
#: observatory root, D9: the session file is the respawn handle).
OMP_SESSIONS_DIRNAME = "omp-sessions"

#: Default omp startup wait — mirrors tools/omp_delegation.RPC_STARTUP_TIMEOUT.
RPC_STARTUP_TIMEOUT = float(os.environ.get("HERMES_OMP_RPC_STARTUP", "20"))
#: ``extra`` key marking whether the engine handle ever completed one turn.
#: Both engines persist lazily (omp JSONL after the first assistant message,
#: hermes SessionDB row on the first turn), so a freshly-spawned ref is
#: NEVER on disk yet. ``False`` at spawn → ``True`` after the first
#: completed child turn (sidecar marks it). Resume treats a missing
#: file/row with ``False`` as never-materialized (build fresh, never the
#: "deleted without /exit?" error); a missing file/row with ``True`` (or a
#: legacy row without the key) is still a deletion.
SESSION_MATERIALIZED_KEY = "session_materialized"


def is_session_materialized(row: dict[str, Any] | Any) -> bool:
    """True once the node's engine completed one turn (default True for
    legacy rows without the key — only spawn-fresh rows read False)."""
    try:
        extra = (row.get("extra") or {}) if isinstance(row, dict) else {}
    except Exception:
        return True
    if SESSION_MATERIALIZED_KEY not in extra:
        return True
    return bool(extra.get(SESSION_MATERIALIZED_KEY))


def mark_session_materialized(state: Any, node_id: str) -> None:
    """Best-effort flip to materialized after a completed turn; never raises."""
    try:
        state.update_extra(node_id, **{SESSION_MATERIALIZED_KEY: True})
    except Exception:
        pass


def orchestrator_node_id() -> str:
    """Fresh node id: ``orch-<hex8>`` (distinct keyspace from discovery's
    ``sa-``/``deleg_`` ids and render_live's fixed literals)."""
    return f"orch-{uuid.uuid4().hex[:8]}"


def omp_sessions_dir(mercury_home: str | Path | None = None) -> Path:
    """``<MERCURY_HOME>/observatory/omp-sessions`` — created on demand."""
    from observatory.provision import _mercury_home

    path = ObservatoryPaths(_mercury_home(mercury_home)).root / OMP_SESSIONS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


# ============================================================================
# In-memory handle registry (rebuilt by the D18 respawn pass)
# ============================================================================


@dataclass
class OrchestratorHandle:
    """One live spawned 0-agent: its state.db node plus the engine handle.

    Exactly one of ``agent`` (hermes AIAgent, in-process) / ``rpc``
    (``OmpRpcChild``, subprocess) is set, per ``engine``.
    """

    node_id: str
    engine: str
    name: str
    session_ref: str  # hermes: session id; omp: session JSONL path
    model: Optional[str] = None
    agent: Any = None
    rpc: Any = None

    def stop(self) -> None:
        """Best-effort engine teardown; never raises (exit must proceed)."""
        if self.rpc is not None:
            try:
                self.rpc.stop()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.exception("spawn: omp rpc child stop failed for %s", self.node_id)
        if self.agent is not None:
            try:
                self.agent.close()
            except Exception:  # noqa: BLE001
                logger.exception("spawn: hermes agent close failed for %s", self.node_id)
        self.rpc = None
        self.agent = None


class OrchestratorRegistry:
    """Thread-safe ``node_id -> OrchestratorHandle`` map.

    The sidecar process owns one instance; a restart (gateway update,
    crash) drops it and the D18 respawn pass (``observatory.respawn``)
    rebuilds it from state.db.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[str, OrchestratorHandle] = {}

    def register(self, handle: OrchestratorHandle) -> None:
        with self._lock:
            self._handles[handle.node_id] = handle

    def unregister(self, node_id: str) -> Optional[OrchestratorHandle]:
        with self._lock:
            return self._handles.pop(node_id, None)

    def get(self, node_id: str) -> Optional[OrchestratorHandle]:
        with self._lock:
            return self._handles.get(node_id)

    def node_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._handles)

    def handles(self) -> list[OrchestratorHandle]:
        with self._lock:
            return list(self._handles.values())

    def stop_all(self) -> None:
        for handle in self.handles():
            handle.stop()
        with self._lock:
            self._handles.clear()


# ============================================================================
# Engine handle builders (lazy heavy imports; injectable for tests)
# ============================================================================


def build_hermes_agent(
    *,
    mercury_home: str | Path | None = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    platform: str = "cli",
) -> Any:
    """Fresh OR resumed hermes orchestrator session — the same machinery a
    CLI chat turn uses (oneshot ``_run_agent`` shape: config-resolved
    runtime + dedicated SessionDB on the mercury home's hermes state.db).

    ``session_id=None`` spawns fresh; passing an existing id resumes (the
    turn prologue in ``run_conversation`` resolves the compression-lineage
    tip and loads prior history). Sandbox-testable: everything resolves
    through ``$MERCURY_HOME``/``$HERMES_HOME`` env.
    """
    from mercury_cli.config import load_config
    from mercury_cli.runtime_provider import resolve_runtime_provider
    from mercury_state import SessionDB

    home = Path(mercury_home) if mercury_home is not None else None
    if home is not None:
        os.environ.setdefault("MERCURY_HOME", str(home))
    cfg = load_config()

    # Mirror oneshot._run_agent (model.default singular, dict split, env):
    # explicit arg → HERMES_INFERENCE_MODEL → config model.default/model.
    # NEVER the plural 'models' key (no such key — it resolved "" and the
    # runtime silently auto-picked a provider the user never chose, e.g.
    # zai via an inherited ZAI_API_KEY landing on a stale glm default).
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        cfg_model = model_cfg
    else:
        _raw = model_cfg.get("default") or model_cfg.get("model") or ""
        if isinstance(_raw, dict):
            from mercury_cli.config import split_model_config_default
            cfg_model, _ = split_model_config_default(_raw)
        else:
            cfg_model = str(_raw or "")
    env_model = os.environ.get("HERMES_INFERENCE_MODEL", "").strip()
    effective_model = (model or "").strip() or env_model or cfg_model
    if not effective_model:
        raise RuntimeError(
            "no model configured (model.default empty and "
            "HERMES_INFERENCE_MODEL unset) — refusing to silently fall back "
            "to an unchosen provider; run `mercury model` first"
        )
    runtime = resolve_runtime_provider(
        requested=None,
        target_model=effective_model or None,
    )

    # Dedicated handle on the home's hermes state.db (NEVER the caller's
    # live object — same law as delegate_tool's child_session_db).
    if home is not None:
        db_path = home / "hermes" / "state.db"
    else:
        db_path = SessionDB().db_path  # resolves $HERMES_HOME lazily
    session_db = SessionDB(db_path=db_path)

    from run_agent import AIAgent

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        requested_provider=runtime.get("requested_provider"),
        api_mode=runtime.get("api_mode"),
        model=effective_model,
        quiet_mode=True,
        platform=platform,
        session_id=session_id,
        session_db=session_db,
        credential_pool=runtime.get("credential_pool"),
    )
    agent._owns_session_db = True  # nobody else holds this handle
    return agent


def omp_spawn_argv(
    *,
    omp_path: str,
    model: str,
    session_dir: Path | str,
    thinking_level: Optional[str] = None,
    resume_session: Optional[str] = None,
) -> list[str]:
    """Headless omp orchestrator argv (D9: ``like `mercury omp` in bash``,
    i.e. ``--mode rpc``): fresh with ``--session-dir`` under the
    observatory dir, or resumed onto its existing session JSONL via
    ``--resume <path>``."""
    argv = [omp_path, "--mode", "rpc", "--model", model]
    if thinking_level:
        argv += ["--thinking", thinking_level]
    if resume_session:
        argv += ["--resume", str(resume_session)]
    else:
        argv += ["--session-dir", str(session_dir)]
    return argv


def build_omp_child(
    *,
    model: Optional[str] = None,
    mercury_home: str | Path | None = None,
    resume_session: Optional[str] = None,
    workdir: Optional[str] = None,
    omp_path: Optional[str] = None,
    thinking_level: Optional[str] = None,
    startup_timeout: float = RPC_STARTUP_TIMEOUT,
) -> Any:
    """Start one headless omp orchestrator (``OmpRpcChild`` with a pinned
    argv). Model defaults to the bridge-validated delegate slot
    (``OMP_MODEL``); env mirrors the delegation path (ONE-env .env keys
    ride along). Returns the STARTED child."""
    from tools.omp_delegation import (
        _delegate_thinking_level,
        _omp_delegate_env,
        _resolve_omp_binary,
        _shared_env_overrides,
    )
    from tools.omp_rpc_transport import OmpRpcChild

    env_err: Optional[str] = None
    resolved_model = (model or "").strip()
    if not resolved_model:
        delegate_env, env_err = _omp_delegate_env()
        resolved_model = delegate_env.get("OMP_MODEL", "")
    if not resolved_model:
        raise RuntimeError(
            "spawnomp: no model configured (bridge produced no OMP_MODEL"
            + (f": {env_err}" if env_err else "")
            + ")"
        )

    binary = omp_path or _resolve_omp_binary()
    if binary is None:
        raise RuntimeError(
            "spawnomp: omp binary not found (HERMES_OMP_BIN or PATH)"
        )

    child_env = _shared_env_overrides()
    if mercury_home is not None:
        child_env.setdefault("MERCURY_HOME", str(mercury_home))
    child = OmpRpcChild(
        omp_path=binary,
        model=resolved_model,
        workdir=workdir,
        env=child_env,
        startup_timeout=startup_timeout,
        approval_callback=None,  # yolo per unified approvals; M4 revisits
        thinking_level=thinking_level or _delegate_thinking_level(),
        command_override=omp_spawn_argv(
            omp_path=binary,
            model=resolved_model,
            session_dir=omp_sessions_dir(mercury_home),
            thinking_level=thinking_level or _delegate_thinking_level(),
            resume_session=resume_session,
        ),
    )
    child.start()
    return child


def omp_session_file(child: Any) -> str:
    """The child's live session JSONL path (RpcSessionState.sessionFile)."""
    client = getattr(child, "_client", None)
    if client is None:
        raise RuntimeError("spawnomp: child not started (no RPC client)")
    state = client.get_state()
    session_file = getattr(state, "session_file", None)
    if not session_file:
        raise RuntimeError("spawnomp: child reported no session file")
    return str(session_file)


def _ensure_writable_dir(path: Path, *, what: str) -> Path:
    """mkdir -p ``path`` and fail LOUD when it is not writable.

    Spawn-time gate for lazy sessions: the engine materializes its handle
    (omp JSONL after the first assistant message, hermes SessionDB row on
    the first turn) only AFTER spawn, so spawn can only promise a
    writable home for it — never an existing file/row.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise RuntimeError(
            f"spawn: {what} directory {path} cannot be created ({exc}) — "
            "refusing a spawn whose session can never materialize"
        ) from exc
    if not os.access(path, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"spawn: {what} directory {path} is not writable — "
            "refusing a spawn whose session can never materialize"
        )
    return path


def validate_spawn_session_ref(
    engine: str,
    session_ref: str,
    *,
    mercury_home: str | Path | None = None,
) -> None:
    """Spawn-time session_ref check: dir-writable, never file-exists.

    Both engines persist lazily (omp writes its session JSONL only after
    the first assistant message — ``SessionManager.isSessionOnDisk`` /
    issue #8860; hermes creates its SessionDB row on the first turn via
    ``AIAgent._ensure_db_session``), so the allocated ref canNOT exist
    yet at spawn time. This gate therefore checks:

    - hermes: the ref is a non-empty session id and the home's
      ``hermes/`` state dir is writable (the row's future home);
    - omp: the ref is a non-empty path INSIDE this home's
      ``observatory/omp-sessions`` dir (a ref escaping it means the
      child was built under another home — e.g. omp defaults landing
      under ``~/.mercury`` — and the sidecar daemon would resume to
      nothing) and its parent dir is writable.

    The FILE/ROW-exists check lives at RESUME time
    (``respawn.restart_omp_orchestrator`` / ``resume_hermes_orchestrator``):
    a handle that never materializes fails stale there, never silent.
    Raise :class:`RuntimeError` (operator-visible via the gateway
    ``✗ /spawn failed`` reply) instead of persisting a row that can
    never answer.
    """
    ref = str(session_ref or "")
    if engine == "hermes":
        if not ref:
            raise RuntimeError("spawn: hermes agent built without a session id")
        home = Path(mercury_home) if mercury_home is not None else None
        if home is not None:
            _ensure_writable_dir(home / "hermes", what="hermes session")
        return
    elif engine == "omp":
        if not ref:
            raise RuntimeError("spawn: omp child reported no session file")
        expected_dir = omp_sessions_dir(mercury_home)
        try:
            Path(ref).expanduser().resolve().relative_to(expected_dir.resolve())
            inside = True
        except ValueError:
            inside = False
        if not inside:
            raise RuntimeError(
                f"spawn: omp session file {ref!r} escapes this home's "
                f"sessions dir ({expected_dir}) — the child was built "
                "under another home and the sidecar daemon would resume "
                "to nothing (pass the live boot's mercury home)"
            )
        _ensure_writable_dir(Path(ref).expanduser().parent, what="omp session")
        return
    raise ValueError(f"spawn: engine must be one of {ENGINES}, got {engine!r}")


async def _register_spawn_ghost(renderer: Any, mxid: str) -> None:
    """Register-then-converge (spawn-ghost fix, defect 1).
    The minted ghost must exist as a real homeserver user BEFORE the
    renderer converges: tuwunel auto-provisions enough for a masqueraded
    createRoom, but the appservice login (E2EE per-ghost device) 400s
    ``M_INVALID_PARAM`` for a non-existent user and every child-voice
    send then crashes. Mirrors the gateway-datagram child paths
    (best-effort: a blip logs and converge still tries auto-provision).
    """
    try:
        localpart = str(mxid or "").lstrip("@").split(":", 1)[0]
        if not localpart:
            return
        client = getattr(getattr(renderer, "executor", None), "client", None)
        if client is None:
            return
        try:
            await client.register_virtual_user(localpart)
        except AttributeError:
            logger.debug(
                "spawn: ghost register unavailable for %s (no register surface)",
                localpart,
            )
        except Exception as exc:  # noqa: BLE001 — ghost may auto-provision
            logger.info("spawn: register %s: %s (continuing)", localpart, exc)
    except Exception:  # noqa: BLE001 — pre-register never fails spawn
        logger.debug("spawn: ghost pre-register skipped", exc_info=True)

# ============================================================================
# spawn_orchestrator (D9)
# ============================================================================


async def spawn_orchestrator(
    name: str,
    engine: str,
    *,
    server_name: str,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    renderer: Optional[Renderer] = None,
    mercury_home: str | Path | None = None,
    model: Optional[str] = None,
    workdir: Optional[str] = None,
    agent_factory: Optional[Callable[[], Any]] = None,
    omp_child_factory: Optional[Callable[[], Any]] = None,
    validate_session_ref: Optional[bool] = None,
) -> dict[str, Any]:
    """Create one 0-agent orchestrator: engine handle + depth-0 state node
    + space/room via renderer intents (§3 — the node appears in the
    gateway space as a subspace with its chat room).

    ``agent_factory`` / ``omp_child_factory`` replace the real engine
    builders (tests inject doubles; they must return started objects with
    ``session_id`` / ``rpc``-shaped handles respectively). No cap on live
    orchestrators (D9).

    ``server_name`` is REQUIRED (no default — fail loud, never fall back):
    the spawned ghost is minted as ``virtual_mxid(slug,
    server_name=server_name)`` and MUST live on the operator's live domain.
    A silent ``mercury.local`` fallback mints an off-domain sender; tuwunel
    answers every createRoom as that sender with HTTP 400 M_EXCLUSIVE
    (namespace ``^@merc_.*$`` is localpart-only) so the node never gets
    its space/room (IDs stay NULL, 257-error retry loop). The only
    production caller (gateway ``_handle_observatory_spawn``) derives it
    from the live boot (gateway ghost mxid domain, else renderer
    ``server_name``); tests pass ``"mercury.local"`` explicitly.
    """
    if not str(server_name or "").strip():
        raise ValueError("spawn: server_name is required (live domain — never default)")
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("spawn: name is required (D6)")
    if engine not in ENGINES:
        raise ValueError(f"spawn: engine must be one of {ENGINES}, got {engine!r}")

    handle_agent = None
    handle_rpc = None
    if engine == "hermes":
        handle_agent = (agent_factory or (lambda: build_hermes_agent(
            mercury_home=mercury_home, model=model)))()
        session_ref = str(getattr(handle_agent, "session_id", "") or "")
        if not session_ref:
            raise RuntimeError("spawn: hermes agent built without a session id")
    else:
        handle_rpc = (omp_child_factory or (lambda: build_omp_child(
            model=model,
            mercury_home=mercury_home,
            workdir=workdir,
        )))()
        session_ref = omp_session_file(handle_rpc)
    # Per-node model fallback: stamp the EFFECTIVE model (the live handle's
    # resolved value — explicit arg → HERMES_INFERENCE_MODEL / OMP_MODEL →
    # ambient config) into extra.model, so respawn/resume (
    # respawn.resume_hermes_orchestrator, sidecar_main) never depends solely
    # on ambient config. Doubles without a .model attr fall back to the arg.
    _handle = handle_agent if engine == "hermes" else handle_rpc
    stamped_model = (str(getattr(_handle, "model", "") or "").strip()
                     or (model or "").strip() or None)
    _auto_validate = validate_session_ref
    if _auto_validate is None:
        _auto_validate = agent_factory is None and omp_child_factory is None
    if _auto_validate:
        try:
            validate_spawn_session_ref(engine, session_ref, mercury_home=mercury_home)
        except Exception:
            if handle_rpc is not None:
                try:
                    handle_rpc.stop()
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    logger.debug("spawn: dangling omp child stop failed", exc_info=True)
            if handle_agent is not None:
                try:
                    handle_agent.close()
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    logger.debug("spawn: dangling hermes agent close failed", exc_info=True)
            raise

    node_id = orchestrator_node_id()
    slug = assign_slug(clean, state)
    row = state.add_node(
        node_id,
        engine=engine,
        name=clean,
        slug=slug,
        mxid=virtual_mxid(slug, server_name=server_name),
        session_ref=session_ref,
        parent_node_id=None,  # depth 0 by next_depth()
        extra={
            # NO "kind" — see the convention note above (tree.desired_plan
            # includes only kind-"" agent roots as orchestrator subspaces).
            "model": stamped_model,
            SESSION_MATERIALIZED_KEY: False,
        },
    )
    registry.register(OrchestratorHandle(
        node_id=node_id,
        engine=engine,
        name=clean,
        session_ref=session_ref,
        model=stamped_model,
        agent=handle_agent,
        rpc=handle_rpc,
    ))

    if renderer is not None and renderer.executor is not None:
        await _register_spawn_ghost(renderer, str(row.get("mxid") or ""))
        # §3: converge the plan — creates the orchestrator subspace + room
        # (idempotent: re-apply against the snapshot is a no-op).
        await renderer.apply_plan(renderer.build_plan())
        try:
            await renderer.render_lifecycle(node_id)  # 🚀 spawned message
        except Exception:  # noqa: BLE001 — cosmetic; spawn already durable
            logger.exception("spawn: lifecycle render failed for %s", node_id)
    return row


# ============================================================================
# Purge journal — write-ahead intent record (D18 crash atomicity)
# ============================================================================

# RenderIntent union members the journal must round-trip. Serialized by
# dataclass field; reconstructed as the real renderer dataclasses so the
_INTENT_SPECS: tuple[tuple[type, str, tuple[str, ...]], ...] = (
    (SendMessage, "send", ("room_key", "sender", "body", "formatted_body", "tag")),
    (LeaveRoom, "leave", ("room_id", "sender")),
    (DetachChild, "detach", ("space_id", "child_id", "sender")),
    (PurgeRoom, "purge", ("room_id",)),
)


def serialize_intents(intents: Sequence[RenderIntent]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for intent in intents:
        for cls, op, fields in _INTENT_SPECS:
            if isinstance(intent, cls):
                out.append({"op": op, **{f: getattr(intent, f) for f in fields}})
                break
        else:
            raise TypeError(f"purge journal: unsupported intent {intent!r}")
    return out


def deserialize_intents(payload: Sequence[Mapping[str, Any]]) -> tuple[RenderIntent, ...]:
    out: list[RenderIntent] = []
    for raw in payload:
        op = raw.get("op")
        for cls, op_name, fields in _INTENT_SPECS:
            if op == op_name:
                out.append(cls(**{f: raw.get(f) for f in fields}))
                break
        else:
            logger.warning("purge journal: unknown intent op %r skipped", op)
    return tuple(out)


def read_purge_journal(state: ObservatoryState) -> list[dict[str, Any]]:
    try:
        raw = state.get_meta(PURGE_JOURNAL_KEY)
    except StateError:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        logger.exception("purge journal: corrupt JSON — dropping (rows already dead)")
        return []
    return entries if isinstance(entries, list) else []



@dataclass
class ExitRecord:
    """What ``begin_exit`` made durable (also the journal entry payload)."""

    journal_id: str
    node_id: str
    status: str
    summary: Optional[str]
    created_epoch: float
    rows: list[dict[str, Any]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)

    def to_entry(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "node_id": self.node_id,
            "status": self.status,
            "summary": self.summary,
            "created_epoch": self.created_epoch,
            "rows": self.rows,
            "intents": self.intents,
        }

    @staticmethod
    def from_entry(entry: Mapping[str, Any]) -> "ExitRecord":
        return ExitRecord(
            journal_id=str(entry.get("journal_id") or ""),
            node_id=str(entry.get("node_id") or ""),
            status=str(entry.get("status") or "exit"),
            summary=entry.get("summary"),
            created_epoch=float(entry.get("created_epoch") or 0.0),
            rows=list(entry.get("rows") or []),
            intents=list(entry.get("intents") or []),
        )


def begin_exit(
    state: ObservatoryState,
    node_id: str,
    *,
    renderer: Renderer,
    status: str = "exit",
    summary: Optional[str] = None,
) -> ExitRecord:
    """D18-CRITICAL durable step of ``/exit``: in ONE sqlite transaction,
    append the write-ahead purge journal entry AND tombstone every subtree
    node. Crash before commit → nothing happened (the agent stays live;
    correct — /exit never reached durability). Crash after commit → the
    journal replays on startup (``replay_purge_journal``) and the purge
    completes; the respawn pass only resumes LIVE nodes, so a killed
    agent can never come back.

    The intents are planned BEFORE the transaction (pure reads of the
    still-live rows) and stored verbatim in the journal — replay must not
    depend on state rows still existing.
    """
    row = state.get(node_id)  # StateError on unknown — fail hard
    if row["depth"] != 0:
        raise ValueError(
            f"exit: {node_id!r} is depth {row['depth']}, not a 0-agent "
            "(D8: only /exit on spawned orchestrators)"
        )
    # planning Renderer is enough (executor not touched here)
    intents = renderer.plan_death(node_id, status=status, summary=summary)
    subtree = state.get_subtree(node_id)  # BFS top-down (parents first)
    record = ExitRecord(
        journal_id=f"pj-{uuid.uuid4().hex[:8]}",
        node_id=node_id,
        status=status,
        summary=summary,
        created_epoch=time.time(),
        rows=[
            {
                "node_id": r["node_id"],
                "parent_node_id": r.get("parent_node_id"),
                "depth": r["depth"],
            }
            for r in subtree
        ],
        intents=serialize_intents(intents),
    )
    entries = read_purge_journal(state)
    entries.append(record.to_entry())
    died = time.time()
    # The atomic core: journal write + every dead-mark commit together.
    # Locked: this state is shared with the gateway event loop (/spawn
    # pass-through) while boot opened it on another thread.
    with state.locked() as db:
        with db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PURGE_JOURNAL_KEY, json.dumps(entries, ensure_ascii=False)),
            )
            for r in subtree:
                db.execute(
                    "UPDATE nodes SET status = 'dead', died_epoch = ? WHERE node_id = ?",
                    (died, r["node_id"]),
                )
    return record


@dataclass
class PurgeOutcome:
    """Result of one intent-batch execution.

    ``fatal`` — a PurgeRoom that did NOT verifiably die (any error other
    than 404-already-gone): annihilation has not converged, the journal
    entry must survive and retry.
    ``soft`` — send/detach/leave failures (cosmetic once rooms are purged);
    logged, never block row removal (D17: lingering tombstones are the
    worse leak)."""

    records: list[dict[str, Any]] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)


def _has_annihilation(intents: Sequence[RenderIntent]) -> bool:
    """True when the batch contains destructive matrix work (purge /
    detach) that an executor MUST run before rows may be deleted."""
    return any(isinstance(i, (PurgeRoom, DetachChild)) for i in intents)


async def _execute_purge_intents(
    executor: Any, intents: Sequence[RenderIntent]
) -> PurgeOutcome:
    from observatory.matrix_client import MatrixError

    out = PurgeOutcome()
    for intent in intents:
        try:
            out.records.extend(await executor.execute([intent]))
        except MatrixError as exc:
            if isinstance(intent, PurgeRoom) and exc.status == 404:
                out.records.append(
                    {"op": "purge", "room_id": intent.room_id, "gone": True}
                )
                continue
            (out.fatal if isinstance(intent, PurgeRoom) else out.soft).append(
                f"{type(intent).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 — classified, not swallowed
            (out.fatal if isinstance(intent, PurgeRoom) else out.soft).append(
                f"{type(intent).__name__}: {exc}"
            )
    return out


def finish_exit(state: ObservatoryState, record: ExitRecord) -> None:
    """Complete one journal entry: delete its rows (deepest-first — FKs
    point child→parent and the journal's row list is BFS top-down) and
    drop the entry, in ONE transaction. D17: no tombstone survives to
    leak state into a same-named successor."""
    remaining = [
        e for e in read_purge_journal(state)
        if e.get("journal_id") != record.journal_id
    ]
    with state.locked() as db:
        with db:
            for r in reversed(record.rows):
                db.execute(
                    "DELETE FROM nodes WHERE node_id = ?", (r["node_id"],)
                )
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PURGE_JOURNAL_KEY, json.dumps(remaining, ensure_ascii=False)),
            )


async def replay_purge_journal(
    state: ObservatoryState, *, executor: Any = None
) -> list[dict[str, Any]]:
    """D18 startup replay: every journal entry re-executes its purge
    intents (idempotent — 404 purges are success) and, once annihilation
    converged, finishes (rows deleted, entry dropped). Entries with a
    still-failing purge stay journaled and are retried on the next boot;
    their rows are already dead, so the respawn pass skips them either
    way."""
    deferred: list[dict[str, Any]] = []
    for entry in read_purge_journal(state):
        record = ExitRecord.from_entry(entry)
        intents = deserialize_intents(record.intents)
        fatal: list[str] = []
        soft: list[str] = []
        if executor is not None:
            outcome = await _execute_purge_intents(executor, intents)
            fatal, soft = outcome.fatal, outcome.soft
        elif _has_annihilation(intents):
            fatal = ["no executor available (state-only boot)"]
        # executor-less entries with only cosmetic intents (summary sends)
        # complete: the annihilation is vacuous and D17 forbids lingering
        # tombstones.
        for err in soft:
            logger.warning(
                "purge journal: entry %s soft failure (rows still removed): %s",
                record.journal_id, err,
            )
        if fatal:
            deferred.append({
                "journal_id": record.journal_id,
                "node_id": record.node_id,
                "errors": fatal,
            })
            logger.warning(
                "purge journal: entry %s deferred (%s)",
                record.journal_id, "; ".join(fatal),
            )
            continue
        finish_exit(state, record)
        logger.info(
            "purge journal: entry %s replayed (%d intents, %d rows deleted)",
            record.journal_id, len(intents), len(record.rows),
        )
    return deferred


# ============================================================================
# exit_orchestrator (D9 /exit → D8 depth-0 cascade)
# ============================================================================


async def exit_orchestrator(
    node_id: str,
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    renderer: Renderer,
    status: str = "exit",
    summary: Optional[str] = None,
) -> dict[str, Any]:
    """``/exit`` on a spawned 0-agent.

    Sequence (each step crash-safe on its own):
    1. ``begin_exit`` — atomic dead-mark + purge journal (D18).
    2. stop the engine handle (kill AFTER the durable mark — see module
       law) and drop it from the registry.
    3. execute the purge intents against matrix (idempotent; a purge that
       verifiably happened — success or 404 — is done, cosmetic
       send/detach failures never block row removal).
    4. ``finish_exit`` — rows + journal entry deleted atomically.

    Returns ``{"record": ExitRecord, "records": [executor log],
    "deferred": [fatal errors]}`` — ``deferred`` non-empty means
    annihilation did not converge and the journal entry was kept for
    replay on the next boot.
    """
    record = begin_exit(state, node_id, renderer=renderer, status=status, summary=summary)
    handle = registry.unregister(node_id)
    if handle is not None:
        handle.stop()
    intents = deserialize_intents(record.intents)
    records: list[dict[str, Any]] = []
    deferred: list[str] = []
    executor = getattr(renderer, "executor", None)
    if executor is not None:
        outcome = await _execute_purge_intents(executor, intents)
        records, deferred = outcome.records, outcome.fatal
    elif _has_annihilation(intents):
        deferred = ["no executor attached to renderer (state-only mode)"]
    if not deferred:
        finish_exit(state, record)
    return {"record": record, "records": records, "deferred": deferred}




def run_spawn(
    name: str,
    engine: str,
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    **kwargs: Any,
) -> dict[str, Any]:
    """Sync ``/spawn`` entry for command handlers without a loop:
    runs :func:`spawn_orchestrator` on a private event loop. ``kwargs``
    MUST carry the live ``server_name`` (required — no default)."""
    return asyncio.run(spawn_orchestrator(
        name, engine, state=state, registry=registry, **kwargs
    ))

def run_exit(
    node_id: str,
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    renderer: Renderer,
    **kwargs: Any,
) -> dict[str, Any]:
    """Sync ``/exit`` entry (same pattern as :func:`run_spawn`)."""
    return asyncio.run(exit_orchestrator(
        node_id, state=state, registry=registry, renderer=renderer, **kwargs
    ))
