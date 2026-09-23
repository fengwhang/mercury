"""Spawned-orchestrator lifecycle: /spawn (hermes) /spawnomp (omp) /exit.
``/spawn <name>`` (hermes engine) and ``/spawnomp <name>`` (omp engine)
create top-level 0-agents; ``/exit`` annihilates one. This module owns the
ENGINE side of that lifecycle — the session/process handles and the
state.db rows — and drives the IRC side (one channel per agent):

- **spawn**: build the engine handle (hermes rooms are plain gateway
  sessions keyed by channel, so no handle is built here — the first
  message in the room starts the session; omp rooms get a headless RPC
  child with its session JSONL pinned under the observatory dir via
  ``--session-dir``), register the depth-0 node, and JOIN the channel.
- **exit**: depth-0 cascade — the WHOLE subtree's channels are destroyed
  server-side and every row deleted. Crash-atomicity: the dead-marks and
  the write-ahead channel journal land in ONE sqlite transaction
  (``begin_exit``); the journal replays on startup
  (``replay_purge_journal``, called by boot resync), so a crash
  mid-destroy never resurrects a killed agent — a journaled node is
  dead-or-deleted in every observable state, and a crashed destroy
  completes on the next boot instead of being forgotten.

Engine-ordering law for /exit: durable dead-mark FIRST, engine kill
second. A crash between them orphans a process (operator-visible) but can
never leave a killed agent live in state.db for boot resync to
restart — that would be resurrection.

Subagent stop at parent death: depth>=1 children of a 0-agent are
delegate_task children owned by the delegation machinery's live-child
registries (``tools/omp_delegation`` / ``delegate_tool``); their process
stop rides that existing path. This module's cascade covers their IRC
channels and state rows.
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
from observatory.rooms import (
    agent_nick,
    drop_omp_room,
    get_bot_sink,
    register_omp_room,
    spawn_channel,
)
from observatory.state import ENGINES, ObservatoryState, StateError

logger = logging.getLogger(__name__)

#: ``extra.kind`` convention: spawned agents carry NO kind — plain agent
#: nodes; only the gateway agent ("gateway") is kind-stamped. Boot resync
#: resumes every depth-0 live node except those kinds ("every live
#: 0-agent").
GATEWAY_KIND = "gateway"
SKIP_RESPAWN_KINDS = ("gateway", "manual-run")

#: state.db meta key holding the write-ahead purge journal.
PURGE_JOURNAL_KEY = "purge-journal"

#: omp session JSONLs for spawned orchestrators live here (under the
#: observatory root: the session file is the resume handle).
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
    """Fresh node id: ``orch-<hex8>`` (distinct keyspace from delegation
    ``sa-``/``deleg_`` ids and render_live's fixed literals)."""
    return f"orch-{uuid.uuid4().hex[:8]}"


def parse_spawn_args(args: str) -> tuple[str, str | None]:
    """Split ``/spawn`` args into ``(name, profile)``.

    ``bravo -p alpha`` / ``-p alpha bravo`` / ``--profile=alpha bravo`` all
    yield ``("bravo", "alpha")`` — the IRC twin of ``mercury -p alpha``.
    No ``-p`` → ``(name, None)``. Raises ``ValueError`` on missing name,
    extra positionals, or a dangling ``-p``.
    """
    import shlex

    try:
        tokens = shlex.split(args or "")
    except ValueError as exc:
        raise ValueError(f"spawn: cannot parse args: {exc}")
    name_parts: list[str] = []
    profile: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-p", "--profile"):
            i += 1
            if i >= len(tokens):
                raise ValueError("spawn: -p needs a profile name")
            if profile is not None:
                raise ValueError("spawn: duplicate -p flag")
            profile = tokens[i]
        elif tok.startswith("--profile="):
            if profile is not None:
                raise ValueError("spawn: duplicate -p flag")
            profile = tok.split("=", 1)[1]
            if not profile:
                raise ValueError("spawn: -p needs a profile name")
        elif tok.startswith("-"):
            raise ValueError(f"spawn: unknown flag {tok}")
        else:
            name_parts.append(tok)
        i += 1
    if not name_parts:
        raise ValueError("spawn: name is required")
    if len(name_parts) > 1:
        raise ValueError("spawn: expected one agent name")
    return name_parts[0], profile


def omp_sessions_dir(mercury_home: str | Path | None = None) -> Path:
    """``<MERCURY_HOME>/observatory/omp-sessions`` — created on demand."""
    from observatory.provision import _mercury_home

    path = ObservatoryPaths(_mercury_home(mercury_home)).root / OMP_SESSIONS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


# ============================================================================
# In-memory handle registry (rebuilt by boot resync)
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
    crash) drops it and boot resync
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
    profile_home: str | Path | None = None,
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
    if profile_home is not None:
        # A profile spawn is the whole point: the child MUST read the
        # profile's config/memories, never inherit the gateway's home.
        child_env["HERMES_HOME"] = str(profile_home)
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
    (boot resync resumes by session file / session id):
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


# ============================================================================
# spawn_orchestrator
# ============================================================================


def _unique_slug(clean: str, state: ObservatoryState) -> str:
    """Live-collision slug: base, base-2, base-3… (dead rows invisible)."""
    import re as _re
    base = _re.sub(r"[^a-z0-9]+", "-", clean.strip().lower()).strip("-")[:48] or "agent"
    slug = base
    n = 2
    while state.find_live_by_slug(slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


async def spawn_orchestrator(
    name: str,
    engine: str,
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    server_name: str = "",
    mercury_home: str | Path | None = None,
    model: Optional[str] = None,
    workdir: Optional[str] = None,
    profile: str | None = None,
    agent_factory: Optional[Callable[[], Any]] = None,
    omp_child_factory: Optional[Callable[[], Any]] = None,
    validate_session_ref: Optional[bool] = None,
) -> dict[str, Any]:
    """Create one 0-agent orchestrator: engine handle + depth-0 state node
    + IRC channel (the bot JOINs; the channel IS the room).

    hermes rooms are plain gateway sessions keyed by channel — no engine
    handle is built here (the first message in the room starts the
    session through normal adapter dispatch, so slash commands,
    approvals, and mid-turn steering work exactly like every other
    gateway chat). omp rooms get a headless RPC child pumped by
    ``rooms.handle_omp_message``.

    ``agent_factory`` / ``omp_child_factory`` replace the real engine
    builders (tests inject doubles). ``server_name`` is accepted for
    caller compatibility and ignored (channels are server-relative).
    No cap on live orchestrators.
    """
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("spawn: name is required")
    if engine not in ENGINES:
        raise ValueError(f"spawn: engine must be one of {ENGINES}, got {engine!r}")
    profile_home: str | None = None
    if profile is not None:
        from mercury_cli.profiles import get_profile_dir, profile_exists

        if not profile_exists(profile):
            raise ValueError(f"spawn: profile '{profile}' does not exist")
        profile_home = str(get_profile_dir(profile))
    handle_agent = None
    handle_rpc = None
    if engine == "hermes":
        if agent_factory is not None:
            handle_agent = agent_factory()
            session_ref = str(getattr(handle_agent, "session_id", "") or "")
            if not session_ref:
                raise RuntimeError("spawn: hermes agent built without a session id")
        else:
            # Gateway-session room: the ref is the channel; the session
            # materializes in the gateway store on the first message.
            session_ref = ""
    else:
        handle_rpc = (omp_child_factory or (lambda: build_omp_child(
            model=model,
            mercury_home=mercury_home,
            workdir=workdir,
            profile_home=profile_home,
        )))()
        session_ref = omp_session_file(handle_rpc)
    _handle = handle_agent if engine == "hermes" else handle_rpc
    stamped_model = (str(getattr(_handle, "model", "") or "").strip()
                     or (model or "").strip() or None)
    _auto_validate = validate_session_ref
    if _auto_validate is None:
        _auto_validate = (agent_factory is None and omp_child_factory is None
                          and engine == "omp")
    if _auto_validate:
        try:
            validate_spawn_session_ref(engine, session_ref, mercury_home=mercury_home)
        except Exception:
            if handle_rpc is not None:
                try:
                    handle_rpc.stop()
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    logger.debug("spawn: dangling omp child stop failed", exc_info=True)
            raise

    node_id = orchestrator_node_id()
    slug = _unique_slug(clean, state)
    channel = spawn_channel(slug, server_name)
    if engine == "hermes" and not session_ref:
        session_ref = channel
    row = state.add_node(
        node_id,
        engine=engine,
        name=clean,
        slug=slug,
        mxid=agent_nick(clean, server_name),
        session_ref=session_ref,
        parent_node_id=None,  # depth 0 by next_depth()
        extra={
            "model": stamped_model,
            SESSION_MATERIALIZED_KEY: False,
            **({"profile": profile} if profile is not None else {}),
        },
    )
    try:
        state.set_room_id(node_id, channel)
    except Exception:
        logger.debug("spawn: set_room_id %s failed", node_id, exc_info=True)
    registry.register(OrchestratorHandle(
        node_id=node_id,
        engine=engine,
        name=clean,
        session_ref=session_ref,
        model=stamped_model,
        agent=handle_agent,
        rpc=handle_rpc,
    ))
    if engine == "omp" and handle_rpc is not None:
        try:
            register_omp_room(node_id, channel, handle_rpc)
        except Exception:
            logger.debug("spawn: omp room register failed", node_id, exc_info=True)

    bot = get_bot_sink()
    if bot is not None:
        try:
            await bot.join_channel(channel)
        except Exception:  # noqa: BLE001 — cosmetic; spawn already durable
            logger.exception("spawn: channel join failed for %s", node_id)
        else:
            try:
                from observatory.provision import get_lounge_nick

                await bot.invite_user(get_lounge_nick(None), channel)
            except Exception:  # noqa: BLE001 — cosmetic; invite is a nudge
                logger.debug("spawn: lounge invite failed for %s", node_id)
            try:
                from observatory.identity import ensure_identity
                from observatory.rooms import agent_nick as _agent_nick

                live = None
                try:
                    from observatory.provision import live_server_name

                    live = live_server_name(None)
                except Exception:
                    live = None
                await ensure_identity(_agent_nick(clean, live), channel)
            except Exception:  # noqa: BLE001 — cosmetic; main bot covers
                logger.debug("spawn: identity ensure failed for %s", node_id)
            try:
                await bot.say(channel, f"spawned {engine} agent '{clean}' — chat here, like CLI.")
            except Exception:  # noqa: BLE001 — cosmetic
                logger.debug("spawn: greet failed for %s", node_id, exc_info=True)
    return state.get(node_id)


# ============================================================================
# Purge journal — write-ahead channel record (crash atomicity)
# ============================================================================

# Journal entries carry channel lists (not render intents): replay
# destroys channels server-side, then deletes rows. Idempotent —
# destroying a gone channel is success.


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
    channels: list[str] = field(default_factory=list)

    def to_entry(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "node_id": self.node_id,
            "status": self.status,
            "summary": self.summary,
            "created_epoch": self.created_epoch,
            "rows": self.rows,
            "channels": self.channels,
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
            channels=list(entry.get("channels") or entry.get("intents") or []),
        )


def begin_exit(
    state: ObservatoryState,
    node_id: str,
    *,
    status: str = "exit",
    summary: Optional[str] = None,
) -> ExitRecord:
    """Durable step of ``/exit``: in ONE sqlite transaction, append the
    write-ahead channel journal entry AND tombstone every subtree node.
    Crash before commit → nothing happened (the agent stays live;
    correct — /exit never reached durability). Crash after commit → the
    journal replays on startup (``replay_purge_journal``) and the destroy
    completes; boot resync only resumes LIVE nodes, so a killed
    agent can never come back.

    The channel list is collected BEFORE the transaction (pure reads of
    the still-live rows) and stored verbatim in the journal — replay must
    not depend on state rows still existing.
    """
    row = state.get(node_id)  # StateError on unknown — fail hard
    if row["depth"] != 0:
        raise ValueError(
            f"exit: {node_id!r} is depth {row['depth']}, not a 0-agent "
            "(only /exit on spawned orchestrators)"
        )
    subtree = state.get_subtree(node_id)  # BFS top-down (parents first)
    channels = [str(r.get("room_id") or "") for r in subtree]
    channels = [c for c in channels if c]
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
        channels=channels,
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
    """Result of one channel-destroy batch.

    ``fatal`` — a channel that did NOT verifiably die: the journal entry
    must survive and retry. Destroying a gone channel is success, never
    fatal. ``soft`` is kept for shape compatibility (always empty)."""
    records: list[dict[str, Any]] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

async def _execute_channel_destroy(bot: Any, channels: list[str]) -> PurgeOutcome:
    """Destroy channels server-side via the bot sink (idempotent)."""
    out = PurgeOutcome()
    for channel in channels:
        try:
            if bot is None:
                out.fatal.append(f"{channel}: no bot sink (daemon down?)")
                continue
            ok = await bot.destroy_channel(str(channel))
            try:
                from observatory.identity import drop_identity

                await drop_identity(str(channel))
            except Exception:  # noqa: BLE001 — cosmetic
                logger.debug("spawn: identity drop failed for %s", channel)
            out.records.append({"op": "destroy", "channel": str(channel),
                                "gone": True, "ok": bool(ok)})
        except Exception as exc:  # noqa: BLE001 — classified, not swallowed
            out.fatal.append(f"{channel}: {exc}")
    return out


def finish_exit(state: ObservatoryState, record: ExitRecord) -> None:
    """Complete one journal entry: delete its rows (deepest-first — FKs
    point child→parent and the journal's row list is BFS top-down) and
    drop the entry, in ONE transaction: no tombstone survives to
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
    state: ObservatoryState, *, bot: Any = None
) -> list[dict[str, Any]]:
    """Startup replay: every journal entry re-destroys its channels
    (idempotent — destroying a gone channel is success) and, once the
    destroy converged, finishes (rows deleted, entry dropped). Entries
    with a still-failing destroy stay journaled and are retried on the
    next boot; their rows are already dead, so boot resync skips
    them either way."""
    deferred: list[dict[str, Any]] = []
    bot = bot if bot is not None else get_bot_sink()
    for entry in read_purge_journal(state):
        record = ExitRecord.from_entry(entry)
        channels = [str(c) for c in (record.channels or [])]
        fatal: list[str] = []
        if bot is not None and channels:
            outcome = await _execute_channel_destroy(bot, channels)
            fatal = outcome.fatal
        elif channels:
            fatal = ["no bot sink available (IRC down?)"]
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
            "purge journal: entry %s replayed (%d channels, %d rows deleted)",
            record.journal_id, len(channels), len(record.rows),
        )
    return deferred


# ============================================================================
# exit_orchestrator (/exit → depth-0 cascade)
# ============================================================================


async def exit_orchestrator(
    node_id: str,
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    bot: Any = None,
    status: str = "exit",
    summary: Optional[str] = None,
) -> dict[str, Any]:
    """``/exit`` on a spawned 0-agent.

    Sequence (each step crash-safe on its own):
    1. ``begin_exit`` — atomic dead-mark + channel journal.
    2. stop the engine handle (kill AFTER the durable mark — see module
       law), drop it from the registry, and drop the omp room pump.
    3. destroy the channels server-side (idempotent; a destroy that
       verifiably happened is done).
    4. ``finish_exit`` — rows + journal entry deleted atomically.

    Returns ``{"record": ExitRecord, "records": [destroy log],
    "deferred": [fatal errors]}`` — ``deferred`` non-empty means the
    destroy did not converge and the journal entry was kept for replay
    on the next boot.
    """
    from observatory.rooms import drop_child_steer
    record = begin_exit(state, node_id, status=status, summary=summary)
    handle = registry.unregister(node_id)
    if handle is not None:
        handle.stop()
    drop_omp_room(node_id)
    try:
        drop_child_steer(node_id)
    except Exception:
        pass
    records: list[dict[str, Any]] = []
    deferred: list[str] = []
    bot = bot if bot is not None else get_bot_sink()
    if bot is not None and record.channels:
        outcome = await _execute_channel_destroy(bot, record.channels)
        records, deferred = outcome.records, outcome.fatal
    elif record.channels:
        deferred = ["no bot sink attached (IRC down?)"]
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
    runs :func:`spawn_orchestrator` on a private event loop."""
    return asyncio.run(spawn_orchestrator(
        name, engine, state=state, registry=registry, **kwargs
    ))

def run_exit(
    node_id: str,
    *,
    state: ObservatoryState,
    registry: OrchestratorRegistry,
    **kwargs: Any,
) -> dict[str, Any]:
    """Sync ``/exit`` entry (same pattern as :func:`run_spawn`)."""
    return asyncio.run(exit_orchestrator(
        node_id, state=state, registry=registry, **kwargs
    ))
