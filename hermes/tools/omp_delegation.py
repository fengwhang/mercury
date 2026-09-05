"""MERCURY-OMP PATCH (B1): delegate_task dispatch → omp one-shot children.

In this distribution delegation has ONE engine: the patched omp build
(single model, roles structurally inert). ``run_agent._dispatch_delegate_task``
and the registry fallback both route here; Mercury child agents are no longer
spawned by the model-facing path.

Contract kept from the old path:
  - top-level delegations (depth 0) run in the BACKGROUND via the async
    delegation registry (one batch unit, one completion event, consolidated
    summaries re-enter the conversation); subagent delegations (depth > 0)
    stay synchronous so an orchestrator can compose within its turn.
  - children return their SUMMARY only (omp stdout), as one result entry
    per task: {task_index, status, summary, exit_reason, truncated, model,
    duration_seconds}.
  - fail-hard: no omp binary / invalid four-slot config → actionable error,
    NEVER a silent fallback to Mercury child agents.

No role routing, ever: model + fallback come from bridge.py --delegate
(delegate_default/delegate_fallback slots), task text is passed VERBATIM as
one argv element, and ~/.omp/agent/config.yml is rendered (star-pinned) once
per process before the first spawn.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Distribution layout: <repo>/mercury/ is this tree; repo root one level up.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _bridge_path() -> Path:
    return Path(os.environ.get("HERMES_OMP_BRIDGE")
                or _REPO_ROOT / "bridge" / "bridge.py")


def _config_path() -> Path:
    """The config file the BRIDGE will read (cache-key truth).

    Mirrors bridge.py's resolution (HERMES_OMP_CONFIG > MERCURY_CONFIG >
    $MERCURY_HOME/config.yaml > ~/.mercury/config.yaml) so the mtime cache
    key tracks the file the bridge actually validates. Post-ship the repo
    root has NO config.yaml (the hand-editable default lives in config/;
    a stale _REPO_ROOT fallback made the cache key permanently None →
    config edits never invalidated the cached delegate env).
    """
    for var in ("HERMES_OMP_CONFIG", "MERCURY_CONFIG"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    home = os.environ.get("MERCURY_HOME")
    if home:
        return Path(home) / "config.yaml"
    return Path.home() / ".mercury" / "config.yaml"


DEFAULT_TIMEOUT = int(os.environ.get("HERMES_OMP_TIMEOUT", "1800"))

# --- process-lifetime caches -------------------------------------------------
_env_cache: Dict[str, Any] = {"mtime": None, "env": None, "err": None}
_rendered = threading.Event()
_live_procs: List["subprocess.Popen"] = []  # global, for action='list' counts
_live_procs_lock = threading.Lock()


def _track_proc(proc: "subprocess.Popen",
                batch_procs: Optional[List["subprocess.Popen"]]) -> None:
    with _live_procs_lock:
        _live_procs.append(proc)
    if batch_procs is not None:
        batch_procs.append(proc)


def _untrack_proc(proc: "subprocess.Popen",
                  batch_procs: Optional[List["subprocess.Popen"]]) -> None:
    with _live_procs_lock:
        try:
            _live_procs.remove(proc)
        except ValueError:
            pass
    if batch_procs is not None:
        try:
            batch_procs.remove(proc)
        except ValueError:
            pass


def _kill_procs(procs: List[Any]) -> None:
    """SIGKILL a scoped list of child handles (batch-scoped interrupt).

    Entries are Popen (``-p`` one-shots) or Popen-duck RPC children
    (``OmpRpcChild``: same .pid / .kill surface). Entries whose pid is
    still None (RPC child raced between construction and spawn) are
    skipped — os.getpgid(None) would return OUR process group.
    """
    for p in list(procs):
        pid = getattr(p, "pid", None)
        if pid is None:
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except OSError:
                pass


def _omp_delegate_env() -> tuple[Dict[str, str], Optional[str]]:
    """Validated delegate env from the bridge (cached on config.yaml mtime).

    Fail-hard: the bridge validating the four-slot config is the gate; a
    refusal aborts delegation with the bridge's FATAL lines verbatim.
    """
    config_yaml = _config_path()
    try:
        mtime = config_yaml.stat().st_mtime
    except OSError:
        mtime = None
    if _env_cache["mtime"] == mtime and _env_cache["env"] is not None:
        return _env_cache["env"], None
    bridge = _bridge_path()
    if not bridge.exists():
        return {}, f"bridge not found at {bridge} (mercury-omp layout broken?)"
    try:
        out = subprocess.run(
            [sys.executable, str(bridge), "--delegate"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {}, "bridge timed out (30s) while validating model config"
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()
        return {}, f"delegate model config invalid — bridge refused:\n{detail}"
    env: Dict[str, str] = {}
    for line in out.stdout.splitlines():
        if line.startswith("OMP_") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    if "OMP_MODEL" not in env:
        return {}, "bridge produced no OMP_MODEL — refusing to delegate"
    _env_cache.update(mtime=mtime, env=env, err=None)
    return env, None



def _shared_env_overrides() -> Dict[str, str]:
    """ONE-env safety net: keys from MERCURY_HOME/.env not already in env.

    The launcher sources the shared env at boot, so children normally
    inherit everything. This net catches the cases where the parent's
    environment predates the .env (long-running gateway, cron, IDE
    subprocess) — reading the same single file both engines share.
    """
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if not mercury:
        return {}
    path = Path(mercury) / ".env"
    if not path.is_file():
        return {}
    overrides: Dict[str, str] = {}
    # engine env parity (user bug: omp reads ONLY ZAI_API_KEY; the wizard used
    # to save GLM_API_KEY first): mirror the alias inside the net as well.
    try:
        from mercury_cli.env_loader import load_hermes_dotenv  # noqa: F401
    except Exception:
        pass
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if not k or " " in k:
                continue
            if k not in os.environ:
                overrides[k] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return overrides



def _profile_context_env(parent_agent: Any) -> Dict[str, str]:
    """Profile-scoped context pointer for omp children (user directive:
    the hermes agent's profile defines where omp subagents' .md references
    resolve — SAME profile mechanics composed into the omp prompt, nested
    included).

    Resolution mirrors the parent's own context assembly (_agent_home):
    profile homes under $MERCURY_HOME/profiles/<name> carry their own
    config/*.md; the shared $MERCURY_HOME/config/*.md is the base layer.
    Children compose file-by-file: a profile file OVERRIDES the shared
    file of the same name; names absent from the profile fall through to
    shared. Nested subagents inherit the pointer through env — omp reads
    MERCURY_PROFILE_HOME, and its own spawns pass the env through.
    """
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if not mercury:
        return {}
    try:
        from agent.system_prompt import _agent_home
        home = _agent_home(parent_agent)
    except Exception:
        home = None
    if home is None:
        return {}
    home = Path(home).resolve()
    shared_root = Path(mercury).resolve()
    if home == shared_root or home == (shared_root / "hermes").resolve():
        return {}  # default profile: shared layer IS the profile layer
    if not str(home).startswith(str(shared_root)):
        return {}  # not a Mercury profile home; leave the shared layer
    return {"MERCURY_PROFILE_HOME": str(home)}


def _resolve_omp_binary() -> Optional[str]:
    cand = os.environ.get("HERMES_OMP_BIN") or "omp"
    return shutil.which(cand)


def _render_omp_config_once() -> None:
    """Render ~/.omp/agent/config.yml (star-pinned roles) once per process.

    Belt-and-suspenders on top of the compiled-in role-strip patch: the
    rendered config pins every role to the session model even if a future
    omp update ships new bundled defs. Render failure is non-fatal (the
    compiled-in patch remains the structural guarantee) but is logged.
    """
    if _rendered.is_set() or not _bridge_path().exists():
        return
    _rendered.set()
    try:
        subprocess.run(
            [sys.executable, str(_bridge_path()), "--render-omp"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _build_task_prompt(goal: str, context: Optional[str],
                       output_schema: Optional[Any]) -> str:
    """Compose the omp one-shot prompt. Goal text is passed VERBATIM."""
    parts: List[str] = []
    if goal:
        parts.append(str(goal))
    if context:
        parts.append(f"\n\nContext:\n{context}")
    if output_schema:
        parts.append(
            "\n\nReturn your FINAL answer as a single JSON object conforming "
            f"to this JSON Schema:\n{json.dumps(output_schema)}"
        )
    return "".join(parts).strip()


# C1 slice 2: RPC-first child transport. Approval-capable (prompt-tier
# decisions route into hermes' guard stack); the -p one-shot remains as
# fallback ONLY for RPC start failure (binary too old / not RPC-capable /
# vendored client import error). ``startup`` bounds the ready-frame wait so
# a non-RPC binary fails over fast instead of stalling the fan-out.
RPC_STARTUP_TIMEOUT = float(os.environ.get("HERMES_OMP_RPC_STARTUP", "20"))


def _rpc_disabled() -> bool:
    """Kill-switch: HERMES_OMP_TRANSPORT=oneshot forces the -p engine."""
    return os.environ.get("HERMES_OMP_TRANSPORT", "").strip().lower() == "oneshot"


def _parent_approval_callback() -> Optional[Callable[..., Any]]:
    """The parent turn's thread-local approval callback, if any.

    Copied onto the RPC responder thread so check_all_command_guards can
    surface a human prompt in the user's chat (same pattern delegate_tool
    uses when spawning Mercury children — see terminal_tool
    set_approval_callback / _callback_tls).
    """
    try:
        from tools.terminal_tool import _get_approval_callback

        return _get_approval_callback()
    except Exception:
        return None


def _run_omp_task(task_index: int, prompt: str, model: str, workdir: Optional[str],
                  timeout: int, fallback_chain: Optional[str],
                  batch_procs: Optional[List["subprocess.Popen"]] = None,
                  profile_home: Optional[str] = None) -> Dict[str, Any]:
    """Run ONE omp child; return a result entry (old entry contract).

    C1 slice 2: prefer the RPC transport (approval routing live); fall
    back to the ``-p`` one-shot when the RPC child cannot START. A task
    that starts under RPC and then fails is a real failure — no silent
    re-run on the other transport (double-execution hazard for
    side-effecting tasks).

    status ∈ {completed, failed, interrupted}; exit_reason ∈ {completed,
    error, timeout, interrupted}; truncated is always False.
    """
    omp_path = _resolve_omp_binary()
    started = time.time()
    if omp_path is None:
        return {
            "task_index": task_index,
            "status": "failed",
            "summary": None,
            "error": (
                "omp binary not found (HERMES_OMP_BIN or PATH). Build the "
                f"patched tree: cd {_REPO_ROOT / 'omp'} && bun install && bun run build"
            ),
            "exit_reason": "error",
            "truncated": False,
            "model": model,
            "duration_seconds": 0.0,
        }

    if not _rpc_disabled():
        try:
            from tools.omp_rpc_transport import (
                OmpRpcStartError,
                run_omp_task_rpc,
            )
        except Exception as exc:
            logger.warning(
                "C1: omp_rpc_transport unavailable (%s) — falling back to "
                "-p one-shot for task %d", exc, task_index)
        else:
            rpc_env = os.environ.copy()
            rpc_env.update(_shared_env_overrides())
            if profile_home:
                rpc_env["MERCURY_PROFILE_HOME"] = profile_home
            if fallback_chain:
                rpc_env["OMP_FALLBACK_CHAIN"] = fallback_chain
            try:
                entry = run_omp_task_rpc(
                    omp_path=omp_path,
                    model=model,
                    prompt=prompt,
                    workdir=workdir,
                    env=rpc_env,
                    timeout=float(timeout),
                    startup_timeout=RPC_STARTUP_TIMEOUT,
                    batch_procs=batch_procs,
                    approval_callback=_parent_approval_callback(),
                )
            except OmpRpcStartError as start_exc:
                logger.warning(
                    "C1: omp RPC start failed (%s) — falling back to -p "
                    "one-shot for task %d", start_exc, task_index)
                entry = _run_omp_one_shot(
                    task_index, prompt, model, omp_path, workdir,
                    timeout, fallback_chain, batch_procs, started)
                entry["transport"] = "oneshot-fallback"
                return entry
            except Exception as exc:  # after a good start: real failure
                return {
                    "task_index": task_index,
                    "status": "failed",
                    "summary": None,
                    "error": f"omp RPC child failed: {exc}",
                    "exit_reason": "error",
                    "truncated": False,
                    "model": model,
                    "duration_seconds": round(time.time() - started, 2),
                }
            entry["task_index"] = task_index
            entry["transport"] = "rpc"
            return entry

    return _run_omp_one_shot(
        task_index, prompt, model, omp_path, workdir,
        timeout, fallback_chain, batch_procs, started)


def _run_omp_one_shot(task_index: int, prompt: str, model: str, omp_path: str,
                      workdir: Optional[str], timeout: int,
                      fallback_chain: Optional[str],
                      batch_procs: Optional[List["subprocess.Popen"]],
                      started: float) -> Dict[str, Any]:
    """The original ``omp --model m -p <prompt>`` one-shot path (B1)."""
    env = os.environ.copy()
    env.update(_shared_env_overrides())
    if profile_home:
        env["MERCURY_PROFILE_HOME"] = profile_home
    if fallback_chain:
        env["OMP_FALLBACK_CHAIN"] = fallback_chain
    # prompt as ONE argv element: verbatim by construction
    cmd = [omp_path, "--model", model, "-p", prompt]

    proc = subprocess.Popen(
        cmd, cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # own process group → clean kill of omp's tree
    )
    _track_proc(proc, batch_procs)
    entry: Dict[str, Any] = {
        "task_index": task_index,
        "model": model,
        "truncated": False,
    }
    try:
        out, err = proc.communicate(timeout=timeout)
        entry["duration_seconds"] = round(time.time() - started, 2)
        if proc.returncode == 0:
            entry.update(
                status="completed",
                summary=(out or "").strip() or "(omp returned no output)",
                exit_reason="completed",
            )
        else:
            detail = (out or "").strip()
            if err and err.strip():
                detail = f"{detail}\n[stderr]\n{err.strip()}".strip()
            entry.update(
                status="failed",
                summary=None,
                error=detail or f"omp exited {proc.returncode}",
                exit_reason="error",
            )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        entry.update(
            duration_seconds=round(time.time() - started, 2),
            status="failed",
            summary=None,
            error=f"omp timed out after {timeout}s (process group killed)",
            exit_reason="timeout",
        )
    finally:
        _untrack_proc(proc, batch_procs)
    return entry


def _kill_live_children() -> None:
    """Legacy global kill — retained only for API compatibility; prefer the
    batch-scoped interrupt closure (see dispatch_omp_delegation)."""
    _kill_procs(_live_procs)


def _sync_run(tasks: List[Dict[str, Any]], env: Dict[str, str],
              workdir: Optional[str], timeout: int,
              max_workers: int,
              batch_procs: Optional[List["subprocess.Popen"]] = None) -> Dict[str, Any]:
    """Bounded-parallel run of all omp children; one entry per task."""
    started = time.time()
    if len(tasks) == 1 or max_workers <= 1:
        _profile_home = env.get("MERCURY_PROFILE_HOME")
        results = [
            _run_omp_task(i, t["prompt"], env["OMP_MODEL"], workdir, timeout,
                          env.get("OMP_FALLBACK_CHAIN"), batch_procs,
                          profile_home=_profile_home)
            for i, t in enumerate(tasks)
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_run_omp_task, i, t["prompt"], env["OMP_MODEL"],
                            workdir, timeout, env.get("OMP_FALLBACK_CHAIN"),
                            batch_procs, profile_home=env.get("MERCURY_PROFILE_HOME"))
                for i, t in enumerate(tasks)
            ]
            results = [f.result() for f in futures]
    return {
        "results": results,
        "total_duration_seconds": round(time.time() - started, 2),
        "engine": "omp",
    }


def dispatch_omp_delegation(parent_agent: Any, function_args: Dict[str, Any]) -> str:
    """B1 entry point — replaces Mercury-child spawn for delegate_task.

    Control actions (list/steer/stop) answer honestly: omp one-shot children
    are not steerable mid-run; results arrive as completion messages.
    """
    from tools.registry import tool_error

    action = str(function_args.get("action") or "").strip().lower()
    if action in ("list", "steer", "stop"):
        if action == "list":
            with _live_procs_lock:
                n = len(_live_procs)
            return json.dumps({
                "action": "list",
                "engine": "omp",
                "running_children": n,
                "note": (
                    "omp one-shot children are not steerable mid-run. Each "
                    "batch delivers its consolidated summaries as one "
                    "completion message when every child finishes."
                ),
            }, ensure_ascii=False)
        return tool_error(
            f"action='{action}' is not supported for omp delegation children "
            "(one-shot processes, no live steering). They finish on their "
            "own and their results re-enter the conversation."
        )

    # --- spawn path -----------------------------------------------------------
    from tools.delegate_tool import (
        _get_max_async_children,
        _get_max_concurrent_children,
        _resolve_workspace_hint,
        _strip_model_hidden_task_fields,
    )

    raw_tasks = _strip_model_hidden_task_fields(function_args.get("tasks"))
    goals: List[Dict[str, Any]] = []
    if isinstance(raw_tasks, list) and raw_tasks:
        for t in raw_tasks:
            if not isinstance(t, dict):
                continue
            g = str(t.get("goal") or "").strip()
            if not g:
                continue
            goals.append({
                "goal": g,
                "context": t.get("context"),
                "output_schema": t.get("output_schema"),
            })
    if not goals:
        g = str(function_args.get("goal") or "").strip()
        if not g:
            return tool_error(
                "delegate_task needs task text: tasks[].goal (preferred) or "
                "the legacy top-level goal."
            )
        goals.append({
            "goal": g,
            "context": function_args.get("context"),
            "output_schema": function_args.get("output_schema"),
        })

    env, err = _omp_delegate_env()
    if err:
        return tool_error(f"delegation aborted (omp engine): {err}")
    # Profile-composed context (user directive): point omp children at the
    # parent's profile layer. Nested spawns inherit via env passthrough.
    env.update(_profile_context_env(parent_agent))
    model = env["OMP_MODEL"]
    if _resolve_omp_binary() is None:
        return tool_error(
            "delegation aborted: omp binary not found "
            f"({os.environ.get('HERMES_OMP_BIN') or 'omp on PATH'}). Build "
            f"the patched tree: cd {_REPO_ROOT / 'omp'} && bun install && "
            "bun run build"
        )
    _render_omp_config_once()

    tasks = [
        {"prompt": _build_task_prompt(g["goal"], g["context"], g["output_schema"])}
        for g in goals
    ]
    workdir = _resolve_workspace_hint(parent_agent)
    timeout = DEFAULT_TIMEOUT
    max_workers = max(1, min(len(tasks), _get_max_concurrent_children()))
    is_subagent = getattr(parent_agent, "_delegate_depth", 0) > 0

    if is_subagent:
        # Orchestrator children need results within their own turn.
        return json.dumps(
            _sync_run(tasks, env, workdir, timeout, max_workers),
            ensure_ascii=False,
        )

    # --- background path: same async registry delivery as the old engine ------
    from tools.approval import get_current_session_key
    from tools.async_delegation import (
        _current_origin_session_id,
        dispatch_async_delegation_batch,
    )
    from gateway.session_context import async_delivery_supported, get_session_env

    # Per-batch proc list: interrupt_fn kills ONLY this batch's children.
    batch_procs: List["subprocess.Popen"] = []

    def _interrupt_batch() -> None:
        _kill_procs(batch_procs)

    try:
        async_ok = async_delivery_supported()
    except Exception:
        async_ok = True
    origin_session_id = _current_origin_session_id()
    if not async_ok and not origin_session_id:
        # Finite session, no wake id: run in-turn so the result is not lost.
        result = _sync_run(tasks, env, workdir, timeout, max_workers, batch_procs)
        result["note"] = (
            "background delivery is unavailable in this session (one-shot "
            "runner); the omp children ran SYNCHRONOUSLY and their results "
            "are included above."
        )
        return json.dumps(result, ensure_ascii=False)

    session_key = get_current_session_key(default="")
    origin_ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "") or ""
    source = get_session_env("HERMES_SESSION_SOURCE", "")
    parent_session_id = getattr(parent_agent, "session_id", None)
    # Desktop/TUI routes on the durable agent session id; gateway chats on the
    # platform conversation key (same nuance as the old engine).
    if source == "tui" and parent_session_id:
        session_key = str(parent_session_id)
    if not session_key:
        # CLI single-process path: stamp the parent's durable session id so
        # the CLI's completion drain can claim this batch (empty key would
        # fail closed).
        session_key = str(parent_session_id or "")

    dispatch = dispatch_async_delegation_batch(
        goals=[g["goal"] for g in goals],
        context=function_args.get("context"),
        toolsets=None,
        role="task",
        model=model,
        session_key=session_key,
        parent_session_id=parent_session_id,
        runner=lambda: _sync_run(tasks, env, workdir, timeout, max_workers,
                                 batch_procs),
        origin_ui_session_id=origin_ui_session_id,
        origin_session_id=origin_session_id,
        interrupt_fn=_interrupt_batch,
        max_async_children=_get_max_async_children(),
    )
    if dispatch.get("status") == "dispatched":
        n = len(tasks)
        note = (
            "omp is running the task in the background. Keep working; its "
            "result re-enters the conversation as a new message. Do not wait "
            "or poll." if n == 1 else
            f"{n} omp children are running in parallel in the background. "
            "Keep working; their consolidated results re-enter the "
            "conversation as a single message once ALL finish. Do not wait "
            "or poll."
        )
        return json.dumps({
            "status": "dispatched",
            "mode": "background",
            "engine": "omp",
            "count": n,
            "delegation_id": dispatch["delegation_id"],
            "model": model,
            "goals": [g["goal"] for g in goals],
            "note": note,
        }, ensure_ascii=False)
    return tool_error(f"delegation rejected: {dispatch.get('error')}")
