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
import re
import socket
import tempfile
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from tools import wave_mem_profiler as _wave_mem_profiler
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


# HERMES-OMP PATCH (user directive 2026-09-05: NO LIMITS): the delegation
# task wall is REMOVED. Long coding sessions are good; a host-side clock
# killing a working child is backwards. None = wait until the child ends
# (its own process, its own API errors, or user interrupt all still apply).
# HERMES_OMP_TIMEOUT remains available as an explicit opt-in for users who
# WANT a wall on a specific box — unset means unlimited.
DEFAULT_TIMEOUT = (
    float(os.environ["HERMES_OMP_TIMEOUT"])
    if os.environ.get("HERMES_OMP_TIMEOUT", "").strip()
    else None
)

# --- process-lifetime caches -------------------------------------------------
_env_cache: Dict[str, Any] = {"mtime": None, "env": None, "err": None}
# Serializes bridge invocations (see _omp_delegate_env): without it an
# N-thread fan-out spawns N bridge subprocesses for one cached answer.
_env_cache_lock = threading.Lock()
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


# HERMES-OMP PATCH (matrix observatory §8.1 item 2): live-child registry.
# Maps a steer/stop address to the child's LIVE transport handle so
# delegate_task(action='steer'/'stop') can be forwarded into the running
# omp process instead of being rejected. Entries hold the OmpRpcChild
# transport (steerable) or the -p one-shot Popen (kill-only).
_live_children: Dict[str, Dict[str, Any]] = {}
_live_children_lock = threading.Lock()


def _live_child_id(delegation_id: Optional[str], task_index: int) -> str:
    """Addressable id: ``<delegation_id>/<task_index>``."""
    base = delegation_id or f"local-{uuid.uuid4().hex[:8]}"
    return f"{base}/{task_index}"


def _register_live_child(meta: Dict[str, Any], transport: Any) -> None:
    with _live_children_lock:
        _live_children[meta["child_id"]] = {
            **meta, "transport": transport,
            "started_at": time.time(), "stop_requested": False,
        }
    # Keep the legacy process list in sync (counts + _kill_live_children).
    with _live_procs_lock:
        _live_procs.append(transport)


def _unregister_live_child(child_id: str, transport: Any) -> None:
    with _live_children_lock:
        _live_children.pop(child_id, None)
    with _live_procs_lock:
        try:
            _live_procs.remove(transport)
        except ValueError:
            pass


def _resolve_live_child(subagent_id: str) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    """Resolve a steer/stop target id to its live-child record.

    Accepts the full ``<delegation_id>/<task_index>`` id, or a bare
    ``<delegation_id>`` when exactly one child of that batch is live
    (single-task fan-outs — the common case).
    """
    sid = (subagent_id or "").strip()
    if not sid:
        return None, "no subagent_id given"
    with _live_children_lock:
        rec = _live_children.get(sid)
        if rec is not None:
            return rec, None
        candidates = [
            (cid, c) for cid, c in _live_children.items()
            if cid.startswith(sid + "/")
        ]
    if len(candidates) == 1:
        return candidates[0][1], None
    if len(candidates) > 1:
        ids = ", ".join(sorted(cid for cid, _ in candidates))
        return None, (
            f"'{sid}' has multiple live children — target one explicitly: {ids}"
        )
    return None, "not in the live-child registry"


def _owns_live_child(rec: Dict[str, Any], parent_agent: Any) -> bool:
    """Durable-session ownership: only the spawning conversation steers.

    Same spine as delegate_tool._owns_subagent_record tier 2: the record
    is stamped with the spawning parent's durable session id; a caller
    with a DIFFERENT session id is refused. Empty owner (direct python
    callers / tests) stays permissive.
    """
    owner = str(rec.get("owner_session_id") or "")
    if not owner:
        return True
    caller = str(getattr(parent_agent, "session_id", "") or "")
    return not caller or caller == owner


def handle_omp_control_action(
    action: str,
    subagent_id: Optional[str],
    message: Optional[str],
    parent_agent: Any = None,
) -> str:
    """delegate_task control plane over live omp children (§8.1 item 2).

    - list: live children (id, name, goal, transport, running seconds).
    - steer: forward to the child's RPC connection (``steer``) — one-shot
      children answer honestly that they cannot be steered.
    - stop: RPC ``abort`` first (graceful boundary), SIGKILL of the child's
      process group when the connection is lost or the child is one-shot.

    Returns the same JSON/tool_error shapes delegate_task's hermes-side
    control plane uses, so the model sees one contract.
    """
    from tools.registry import tool_error

    if action == "list":
        caller_sid = str(getattr(parent_agent, "session_id", "") or "")
        with _live_children_lock:
            records = [
                rec for rec in _live_children.values()
                if not caller_sid or _owns_live_child(rec, parent_agent)
            ]
        entries = []
        for r in records:
            entries.append({
                "subagent_id": r.get("child_id"),
                "delegation_id": r.get("delegation_id"),
                "task_index": r.get("task_index"),
                "name": r.get("name"),
                "goal": r.get("goal"),
                "model": r.get("model"),
                "transport": r.get("transport_kind"),
                "steerable": bool(r.get("steerable")),
                "running_seconds": round(time.time() - r.get("started_at", time.time()), 1),
            })
        payload: Dict[str, Any] = {
            "action": "list",
            "engine": "omp",
            "count": len(entries),
            "subagents": entries,
        }
        if not entries:
            payload["note"] = (
                "No live omp children right now. Finished children have "
                "delivered (or will deliver) their results as completion "
                "messages."
            )
        return json.dumps(payload, ensure_ascii=False)

    rec, resolve_err = _resolve_live_child(subagent_id or "")
    child_id = (subagent_id or "").strip()
    if rec is not None and not _owns_live_child(rec, parent_agent):
        return tool_error(
            f"No live child '{child_id}' in this conversation's spawn tree. "
            "Use action='list' to see the children you own."
        )
    if rec is None:
        return tool_error(
            f"No live omp child '{child_id}' ({resolve_err}). It may have "
            "already finished — its result arrives as a normal completion "
            "message. Use action='list' to see live children."
        )

    transport = rec.get("transport")
    child_id = rec.get("child_id") or child_id

    if action == "steer":
        text = str(message or "").strip()
        if not text:
            return tool_error(
                "action='steer' requires a non-empty 'message' describing "
                "the course correction."
            )
        if not rec.get("steerable"):
            return tool_error(
                f"Child '{child_id}' runs on the one-shot transport and "
                "cannot be steered mid-run. It finishes on its own and its "
                "result re-enters the conversation."
            )
        try:
            transport.steer(text)
        except Exception as exc:
            logger.warning(
                "M0A: steer to %s failed (%s)", child_id, exc)
            return tool_error(
                f"Steering '{child_id}' failed: {exc}. If the child died, "
                "its result (or failure) arrives as a completion message."
            )
        return json.dumps({
            "action": "steer",
            "subagent_id": child_id,
            "status": "queued",
            "note": (
                "Steer forwarded to the omp child over RPC — it is injected "
                "at the next safe boundary; the in-flight tool call is "
                "never cut."
            ),
        }, ensure_ascii=False)

    if action == "stop":
        with _live_children_lock:
            rec["stop_requested"] = True
        aborted_cleanly = False
        if rec.get("steerable"):
            try:
                transport.abort(reason="delegate_task stop by parent")
                aborted_cleanly = True
            except Exception as exc:
                # Connection loss: fall through to the SIGKILL fallback.
                logger.warning(
                    "M0A: RPC abort of %s failed (%s) — falling back to "
                    "SIGKILL", child_id, exc)
        if not aborted_cleanly:
            _kill_procs([transport])
        return json.dumps({
            "action": "stop",
            "subagent_id": child_id,
            "status": "interrupt_requested",
            "note": (
                "Stop forwarded to the omp child (graceful abort; hard kill "
                "on connection loss). Its partial result still re-enters "
                "the conversation as a completion message — do not wait or "
                "poll."
            ),
        }, ensure_ascii=False)

    return tool_error(
        f"Unknown action '{action}'. Use spawn (default), list, steer, or stop."
    )


def _omp_delegate_env() -> tuple[Dict[str, str], Optional[str]]:
    """Validated delegate env from the bridge (cached on config.yaml mtime).

    Fail-hard: the bridge validating the four-slot config is the gate; a
    refusal aborts delegation with the bridge's FATAL lines verbatim.

    Double-checked locking: an N-child fan-out resolves the thinking level
    (hence this env) from N worker threads at once. Without the lock every
    thread that arrives before the first bridge run finishes spawns its
    OWN bridge subprocess — N identical Python processes for one answer.
    """
    config_yaml = _config_path()
    try:
        mtime = config_yaml.stat().st_mtime
    except OSError:
        mtime = None
    if _env_cache["mtime"] == mtime and _env_cache["env"] is not None:
        return _env_cache["env"], None
    with _env_cache_lock:
        if _env_cache["mtime"] == mtime and _env_cache["env"] is not None:
            return _env_cache["env"], None
        return _omp_delegate_env_locked(mtime)


def _omp_delegate_env_locked(mtime: Any) -> tuple[Dict[str, str], Optional[str]]:
    """Bridge invocation; caller holds ``_env_cache_lock``."""
    bridge = _bridge_path()
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



_THINKING_DEFAULT = "xhigh"
_THINKING_LEVEL_CACHE: Dict[str, Any] = {"mtime": None, "level": None}


def _delegate_thinking_level() -> str:
    """Config-pinned delegate thinking level (models.delegate_thinking_level).

    Default xhigh; read from the bridge's cached env so both spawn paths and
    the RPC transport share one value. Never agent-selected.
    """
    env, _err = _omp_delegate_env()
    return env.get("OMP_THINKING_LEVEL") or _THINKING_DEFAULT


def _shared_env_overrides() -> Dict[str, str]:
    """ONE-env safety net: keys from MERCURY_HOME/.env not already in env.

    The launcher sources the shared env at boot, so children normally
    inherit everything. This net catches the cases where the parent's
    environment predates the .env (long-running gateway, cron, IDE
    subprocess) — reading the same single file both engines share.

    Pure function of the current process env (no caching here): the batch
    builder below calls it once per dispatch and shares the result, so an
    N-child fan-out performs exactly ONE .env read and ONE gateway
    token peek instead of N.
    """
    overrides: Dict[str, str] = {}
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if not mercury:
        return overrides
    path = Path(mercury) / ".env"
    # Nous-managed web selection -> omp-native firecrawl env bridge
    # (gateway URL + Nous token) so omp search/scrape uses hermes' gateway.
    try:
        overrides.update(_nous_search_env_overrides())
    except Exception:
        pass
    if not path.is_file():
        return overrides
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


def _delegate_batch_base_env() -> Dict[str, str]:
    """Build the child-process base env ONCE per delegation batch.

    Linear-memory contract: a fan-out of N omp children must not repeat
    shared work per child. ``os.environ.copy()`` plus the .env safety net
    (file read + config parse + gateway token peek) is identical for every
    child in the batch, so it is computed here and SHARED read-only.
    Workers assemble each child's env as ``dict(base)`` plus that task's
    extras (approval socket, profile home, fallback chain) — one cheap
    shallow copy per child, never a mutation of the shared base.

    No concurrency cap is involved: every task still gets its own worker
    and its own child process; only the duplicated computation is shared.
    """
    base = os.environ.copy()
    base.update(_shared_env_overrides())
    return base



def _nous_search_env_overrides() -> Dict[str, str]:
    """HERMES-OMP PATCH (tool-call inheritance, Nous search): when hermes'
    web selection is the Nous-managed gateway, omp has no native 'nous'
    provider — but the gateway speaks the Firecrawl API shape. Export the
    gateway URL + Nous token under omp's NATIVE firecrawl env names so omp's
    own firecrawl provider (search + scrape) hits the gateway with hermes'
    credentials. Never overrides explicit user-provided values."""
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if not mercury:
        return {}
    overrides: Dict[str, str] = {}
    try:
        from tools.tool_backend_helpers import NOUS_MANAGED_PROVIDER, read_selection
        if read_selection("web") != NOUS_MANAGED_PROVIDER:
            return {}
    except Exception:
        return {}
    try:
        from tools.managed_tool_gateway import (
            build_vendor_gateway_url,
            peek_nous_access_token,
        )
        url = build_vendor_gateway_url("firecrawl")
        token = peek_nous_access_token()
    except Exception:
        return {}
    if not url or not token:
        return {}
    if not os.environ.get("FIRECRAWL_API_URL"):
        overrides["FIRECRAWL_API_URL"] = url
    if not os.environ.get("FIRECRAWL_API_KEY"):
        overrides["FIRECRAWL_API_KEY"] = token
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




# HERMES-OMP PATCH (approval pass-through, user directive): one-shot children
# get the same human-in-the-loop the RPC children have. A tiny Unix-socket
# HTTP server (one per delegation batch) receives approval requests from
# headless omp children; each is answered by hermes' full guard stack —
# the same stack the user's own terminal calls face, including the
# interactive callback that surfaces the prompt in the user's chat.
class _ApprovalBridgeServer:
    def __init__(self, approval_callback=None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import socketserver
        self._cb = approval_callback
        self._dir = tempfile.mkdtemp(prefix="mercury-approval-")
        self._path = os.path.join(self._dir, "approval.sock")
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/approve":
                    self.send_error(404); return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                title = str(body.get("title") or "")
                kind = str(body.get("kind") or "select")
                from tools.omp_rpc_transport import (
                    extract_command_from_prompt, hermes_approval_decision,
                )
                # Thread-local callback propagation: HTTP handler threads do
                # NOT inherit the delegation thread's approval callback, and
                # without it a dangerous command would fail closed (deny)
                # instead of surfacing to the USER. Install it per request.
                if outer._cb is not None:
                    try:
                        from tools.terminal_tool import set_approval_callback
                        set_approval_callback(outer._cb)
                    except Exception:
                        pass
                command = extract_command_from_prompt(title)
                if kind == "select" and command:
                    # User directive: omp children inherit the hermes
                    # agent's permission surface, and EVERY approval
                    # roadblock reaches the USER through hermes.
                    # 1. Guard stack first: allowlist/smart inheritance,
                    #    hardline + user-deny stay ABSOLUTE.
                    # 2. If the guards neither approved nor actively denied
                    #    (pending_approval in a child that can't drain a
                    #    gateway queue, or a fail-closed no-context deny),
                    #    ask the user DIRECTLY through the parent's chat
                    #    callback — that answer is final.
                    from tools.approval import check_all_command_guards
                    try:
                        decision = check_all_command_guards(
                            command, env_type="container")
                    except Exception:
                        decision = {"approved": False}
                    ok = bool(decision.get("approved"))
                    if not ok:
                        actively_denied = (
                            decision.get("user_consent") is False
                            or decision.get("outcome") == "denied"
                            or str(decision.get("message") or "").startswith(
                                "BLOCKED (hardline)")
                        )
                        if not actively_denied and outer._cb is not None:
                            try:
                                ok = bool(outer._cb(
                                    f"[omp subagent approval]\n{title}"))
                            except Exception:
                                ok = False
                    value = "Approve" if ok else "Deny"
                elif kind == "confirm":
                    # Free-form confirm: route through the callback if the
                    # user can be asked; otherwise fail closed.
                    if outer._cb is not None:
                        try:
                            ok = bool(outer._cb(title))
                        except Exception:
                            ok = False
                    else:
                        ok = False
                    value = "Approve" if ok else "Deny"
                else:
                    ok = False
                    value = "Deny"
                payload = json.dumps({"value": value, "confirmed": ok}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *a):
                pass

        class UnixHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_UNIX
            def server_bind(self):
                # HTTPServer.server_bind() calls socket.getfqdn() — a
                # multi-SECOND DNS stall on resolver-less boxes (measured
                # ~3.5s here, paid by EVERY sync delegation batch) — and
                # unpacks a unix PATH as (host, port). Bind plainly and use
                # static names; nothing reads server_name on this socket.
                import socketserver
                socketserver.TCPServer.server_bind(self)
                self.server_name = "mercury-approval-bridge"
                self.server_port = 0
            def get_request(self):
                req, _ = self.socket.accept()
                return req, ("unix", 0)

        self._server = UnixHTTPServer(self._path, Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name="mercury-approval-bridge")

    def start(self) -> str:
        os.chmod(self._path, 0o600)
        self._thread.start()
        return self._path

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except Exception:
            pass

# C1 slice 2: RPC-first child transport. Approval-capable (prompt-tier
# decisions route into hermes' guard stack); the -p one-shot remains as
# fallback ONLY for RPC start failure (binary too old / not RPC-capable /
# vendored client import error). ``startup`` bounds the ready-frame wait so
# a non-RPC binary fails over fast instead of stalling the fan-out.
try:
    RPC_STARTUP_TIMEOUT = float(os.environ.get("HERMES_OMP_RPC_STARTUP", "20"))
except (TypeError, ValueError):
    RPC_STARTUP_TIMEOUT = 20.0


def _rpc_startup_timeout() -> float:
    """Ready-frame wait, re-read per batch so env changes take effect.

    Invalid values (e.g. HERMES_OMP_RPC_STARTUP=garbage) fall back to 20s
    instead of crashing the delegation engine at import or per batch.
    """
    try:
        return float(os.environ.get("HERMES_OMP_RPC_STARTUP", str(RPC_STARTUP_TIMEOUT)))
    except (TypeError, ValueError):
        return 20.0


def _rpc_disabled() -> bool:
    """Kill-switch: HERMES_OMP_TRANSPORT=oneshot forces the -p engine."""
    return os.environ.get("HERMES_OMP_TRANSPORT", "").strip().lower() == "oneshot"


def _isolate_worktree_enabled() -> bool:
    """Opt-in: HERMES_OMP_ISOLATE=1/true/yes/on enables; default OFF.

    OFF until --isolate-worktree stabilizes upstream (label collisions,
    stale binaries). Explicit opt-in only — empty/unset means disabled.
    """
    return os.environ.get("HERMES_OMP_ISOLATE", "").strip().lower() in {"1", "true", "yes", "on"}


_isolate_support_cache: Dict[str, bool] = {}


def _omp_supports_isolate_worktree(omp_path: str) -> bool:
    """Version gate: True when ``omp --help`` advertises --isolate-worktree.

    Cached per binary path. Any probe failure (missing binary, timeout,
    non-zero help) returns False — fail safe means omitting the flag.
    """
    if not omp_path:
        return False
    if omp_path in _isolate_support_cache:
        return _isolate_support_cache[omp_path]
    try:
        proc = subprocess.run(
            [omp_path, "--help"],
            capture_output=True, text=True, timeout=10,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ok = "isolate-worktree" in out
    except Exception:
        ok = False
    _isolate_support_cache[omp_path] = ok
    return ok


def _gate_isolate_label(omp_path: str, isolate_worktree: Optional[str]) -> Optional[str]:
    """Omit + warn when the omp binary predates --isolate-worktree."""
    if not isolate_worktree:
        return None
    try:
        if _omp_supports_isolate_worktree(omp_path):
            return isolate_worktree
    except Exception:
        pass
    logger.warning(
        "omp binary %s does not advertise --isolate-worktree "
        "(predates flag?) — omitting isolate label %r",
        omp_path, isolate_worktree)
    return None


def _isolate_slug(label: str) -> str:
    """Branch slug omp derives from a label (mirrors its sanitization)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label)


# Substrings (lowercased) marking an RPC start failure as isolate/usage
# related: retrying the one-shot fallback with the SAME label would fail
# the same way (stale binary: unknown flag exit 2; branch collision:
# already-exists exit 1). The fallback strips the label instead.
_ISOLATE_START_ERROR_HINTS = (
    "isolate",
    "unknown flag",
    "unknown option",
    "usage",
    "already exists",
    "worktree",
)


def _is_isolate_start_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return any(hint in msg for hint in _ISOLATE_START_ERROR_HINTS)


def _git_toplevel(workdir: Optional[str]) -> Optional[str]:
    """Return the git toplevel for *workdir*, or None when not in a work tree."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workdir or os.getcwd(),
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


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
                  profile_home: Optional[str] = None,
                  extra_env: Optional[Dict[str, str]] = None,
                  delegation_id: Optional[str] = None,
                  name: Optional[str] = None,
                  goal: Optional[str] = None,
                  owner_session_id: str = "",
                  base_env: Optional[Dict[str, str]] = None,
                  isolate_worktree: Optional[str] = None) -> Dict[str, Any]:
    """Run ONE omp child; return a result entry (old entry contract).

    C1 slice 2: prefer the RPC transport (approval routing live); fall
    back to the ``-p`` one-shot when the RPC child cannot START. A task
    that starts under RPC and then fails is a real failure — no silent
    re-run on the other transport (double-execution hazard for
    side-effecting tasks).

    M0A (matrix observatory §8.1): every live child registers in the
    steer/stop registry under ``<delegation_id>/<task_index>`` while it
    runs, and its result entry carries the task ``name`` so delegation
    records/completions stay name-addressable.

    status ∈ {completed, failed, interrupted}; exit_reason ∈ {completed,
    error, timeout, interrupted}; truncated is always False.
    """
    omp_path = _resolve_omp_binary()
    started = time.time()
    child_name = str(name or "").strip() or f"task-{task_index}"
    # Wave-mem profiler: stamp delegation identity on the child env so the
    # sampler can attribute RSS per wave. Self-gating ({} when off).
    _wave_overlay = _wave_mem_profiler.child_env_overlay(
        delegation_id or "", child_name, task_index)
    if _wave_overlay:
        extra_env = {**(extra_env or {}), **_wave_overlay}
    # M0A: registry meta — the steer/stop address of this child while it
    # runs. Registered/unregistered by whichever transport owns the run.
    meta = {
        "child_id": _live_child_id(delegation_id, task_index),
        "delegation_id": delegation_id,
        "task_index": task_index,
        "name": child_name,
        "goal": goal,
        "model": model,
        "owner_session_id": owner_session_id or "",
    }
    if omp_path is None:
        return {
            "task_index": task_index,
            "name": child_name,
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
    # Version gate: stale omp binaries predate --isolate-worktree (unknown
    # flag exit 2). Omit + warn instead of failing every child in the batch.
    isolate_worktree = _gate_isolate_label(omp_path, isolate_worktree)

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
            # Batch-shared base (linear-memory contract): the fan-out builds
            # os.environ + safety-net overrides ONCE; each child copies it
            # and overlays only its own extras. None = legacy direct-caller
            # path (compute inline, exactly as before).
            if base_env is None:
                rpc_env = os.environ.copy()
                rpc_env.update(_shared_env_overrides())
            else:
                rpc_env = dict(base_env)
            if extra_env:
                rpc_env.update(extra_env)
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
                    timeout=(float(timeout) if timeout is not None else None),
                    startup_timeout=_rpc_startup_timeout(),
                    batch_procs=batch_procs,
                    approval_callback=_parent_approval_callback(),
                    thinking_level=_delegate_thinking_level(),
                    isolate_worktree=isolate_worktree,
                    # M0A: live-child registry (steer/stop) for the run
                    child_started=lambda c: _register_live_child(
                        {**meta, "transport_kind": "rpc", "steerable": True}, c),
                    child_finished=lambda c: _unregister_live_child(
                        meta["child_id"], c),
                )
            except OmpRpcStartError as start_exc:
                # Isolate/usage start errors must NOT retry with the same
                # label: stale binary (unknown flag exit 2) or branch
                # collision (same slug exists, exit 1) would fail identically
                # on the fallback and double-spawn side effects. Strip it.
                fallback_label: Optional[str] = None
                if isolate_worktree and _is_isolate_start_error(start_exc):
                    logger.warning(
                        "C1: omp RPC start failed with isolate/usage error "
                        "(%s) — retrying one-shot WITHOUT --isolate-worktree "
                        "for task %d", start_exc, task_index)
                else:
                    logger.warning(
                        "C1: omp RPC start failed (%s) — falling back to -p "
                        "one-shot for task %d", start_exc, task_index)
                    fallback_label = isolate_worktree
                entry = _run_omp_one_shot(
                    task_index, prompt, model, omp_path, workdir,
                    timeout, fallback_chain, batch_procs, started,
                    profile_home=profile_home, extra_env=extra_env,
                    meta={**meta, "transport_kind": "oneshot-fallback"},
                    base_env=base_env,
                    isolate_worktree=fallback_label)
                if fallback_label and entry.get("status") == "completed":
                    entry["isolated_worktree"] = {"branch": f"omp-isolated/{_isolate_slug(fallback_label)}", "label": fallback_label}
                entry["transport"] = "oneshot-fallback"
                return entry
            except Exception as exc:  # after a good start: real failure
                return {
                    "task_index": task_index,
                    "name": child_name,
                    "status": "failed",
                    "summary": None,
                    "error": f"omp RPC child failed: {exc}",
                    "exit_reason": "error",
                    "truncated": False,
                    "model": model,
                    "duration_seconds": round(time.time() - started, 2),
                }
            entry["task_index"] = task_index
            entry["name"] = child_name
            entry["transport"] = "rpc"
            if isolate_worktree and entry.get("status") == "completed":
                _slug = _isolate_slug(isolate_worktree)
                entry["isolated_worktree"] = {"branch": f"omp-isolated/{_slug}", "label": isolate_worktree}
            return entry

    entry = _run_omp_one_shot(
        task_index, prompt, model, omp_path, workdir,
        timeout, fallback_chain, batch_procs, started,
        profile_home=profile_home, extra_env=extra_env,
        meta={**meta, "transport_kind": "oneshot"},
        base_env=base_env,
        isolate_worktree=isolate_worktree)
    if isolate_worktree and entry.get("status") == "completed":
        _slug = _isolate_slug(isolate_worktree)
        entry["isolated_worktree"] = {"branch": f"omp-isolated/{_slug}", "label": isolate_worktree}
    entry["transport"] = "oneshot"
    return entry


def _run_omp_one_shot(task_index: int, prompt: str, model: str, omp_path: str,
                      workdir: Optional[str], timeout: int,
                      fallback_chain: Optional[str],
                      batch_procs: Optional[List["subprocess.Popen"]],
                      started: float,
                      profile_home: Optional[str] = None,
                      extra_env: Optional[Dict[str, str]] = None,
                      meta: Optional[Dict[str, Any]] = None,
                      base_env: Optional[Dict[str, str]] = None,
                      isolate_worktree: Optional[str] = None) -> Dict[str, Any]:
    """The original ``omp --model m -p <prompt>`` one-shot path (B1).

    ``meta`` (M0A): live-child registry record — one-shot children are
    listed and stoppable (SIGKILL), but never steerable.
    """
    # Batch-shared base (linear-memory contract): copy the fan-out's base
    # and overlay this child's extras. None = legacy direct-caller path.
    if base_env is None:
        env = os.environ.copy()
        env.update(_shared_env_overrides())
    else:
        env = dict(base_env)
    if extra_env:
        env.update(extra_env)
    if profile_home:
        env["MERCURY_PROFILE_HOME"] = profile_home
    if fallback_chain:
        env["OMP_FALLBACK_CHAIN"] = fallback_chain
    # prompt as ONE argv element: verbatim by construction
    # MERCURY-OMP PATCH (user directive): thinking level is a config
    # parameter (models.delegate_thinking_level, default xhigh) — passed
    # explicitly, never agent-selected per spawn.
    cmd = [omp_path, "--model", model, "-p", prompt]
    _tl = _delegate_thinking_level()
    if _tl:
        cmd += ["--thinking", _tl]
    # --isolate-worktree (oh-my-pi#452): start the child in its own linked
    # worktree so parallel children never share a working copy. Omitted when
    # None (flag off, non-git workdir, or stale binary predating the flag).
    isolate_worktree = _gate_isolate_label(omp_path, isolate_worktree)
    if isolate_worktree:
        cmd += ["--isolate-worktree", isolate_worktree]

    proc = subprocess.Popen(
        cmd, cwd=workdir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # own process group → clean kill of omp's tree
    )
    _track_proc(proc, batch_procs)
    if meta is not None:
        _register_live_child(
            {**meta, "transport_kind": meta.get("transport_kind") or "oneshot",
             "steerable": False}, proc)
    entry: Dict[str, Any] = {
        "task_index": task_index,
        "name": (meta or {}).get("name") or f"task-{task_index}",
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
        if meta is not None:
            _unregister_live_child(meta["child_id"], proc)
    return entry


def _kill_live_children() -> None:
    """Legacy global kill — retained only for API compatibility; prefer the
    batch-scoped interrupt closure (see dispatch_omp_delegation)."""
    _kill_procs(_live_procs)


def _sync_run(tasks: List[Dict[str, Any]], env: Dict[str, str],
              workdir: Optional[str], timeout: int,
              max_workers: int,
              batch_procs: Optional[List["subprocess.Popen"]] = None,
              delegation_id: Optional[str] = None,
              owner_session_id: str = "",
              prof: Optional["_wave_mem_profiler.WaveProfiler"] = None) -> Dict[str, Any]:
    """Run all omp children; one entry per task.

    ``delegation_id``/``owner_session_id`` (M0A): registry spine so each
    child registers under ``<delegation_id>/<task_index>`` owned by the
    dispatching conversation (steer/stop addressing).

    Linear-memory contract: the child-process base env (os.environ + the
    .env safety net) is built ONCE here and shared read-only across the
    batch — workers copy it per child instead of each repeating the file
    reads, config parse, and token peek. No cap: ``max_workers`` still
    equals the task count (every task gets its own worker).
    """
    started = time.time()
    # HERMES-OMP PATCH (approval pass-through): one-shot children ask the
    # user through THIS parent. RPC children route approvals natively; the
    # bridge covers the -p fallback (and any headless mode).
    _bridge = None
    if "MERCURY_APPROVAL_SOCKET" not in env:
        try:
            _bridge = _ApprovalBridgeServer(_parent_approval_callback())
            env = dict(env)
            env["MERCURY_APPROVAL_SOCKET"] = _bridge.start()
        except Exception:
            _bridge = None
    try:
        base_env = _delegate_batch_base_env()
        return _sync_run_inner(tasks, env, workdir, timeout, max_workers,
                               batch_procs, delegation_id, owner_session_id,
                               base_env=base_env, prof=prof)
    finally:
        if _bridge is not None:
            _bridge.stop()


def _sync_run_inner(tasks: List[Dict[str, Any]], env: Dict[str, str],
              workdir: Optional[str], timeout: int,
              max_workers: int,
              batch_procs: Optional[List["subprocess.Popen"]] = None,
              delegation_id: Optional[str] = None,
              owner_session_id: str = "",
              base_env: Optional[Dict[str, str]] = None,
              prof: Optional["_wave_mem_profiler.WaveProfiler"] = None) -> Dict[str, Any]:
    # Direct-caller path (tests, diagnostics): no batch base supplied, so
    # build it once here — still exactly once per batch, never per child.
    if base_env is None:
        base_env = _delegate_batch_base_env()
    started = time.time()
    # --isolate-worktree labels (oh-my-pi#452): computed ONCE per batch, not
    # per child. None when the kill-switch is off or the workdir is not in a
    # git tree (silent degrade — matches the subagent_worktree contract).
    # Deduped on the SANITIZED slug (suffix -2, -3): distinct names can
    # sanitize identically ("foo bar" vs "foo-bar") and omp would fail the
    # second child with branch-exists exit 1.
    _isolate_repo = _git_toplevel(workdir) if _isolate_worktree_enabled() else None
    _seen_slugs: set = set()

    def _isolate_label(i: int, t: Dict[str, Any]) -> Optional[str]:
        if not _isolate_repo:
            return None
        _child = str(t.get("name") or "").strip() or f"task-{i}"
        raw = f"{_child}-{delegation_id}"
        slug = _isolate_slug(raw)
        if slug not in _seen_slugs:
            _seen_slugs.add(slug)
            return raw
        base = raw
        n = 2
        while True:
            cand = f"{base}-{n}"
            s = _isolate_slug(cand)
            if s not in _seen_slugs:
                _seen_slugs.add(s)
                return cand
            n += 1

    if len(tasks) == 1 or max_workers <= 1:
        _profile_home = env.get("MERCURY_PROFILE_HOME")
        _extra = {k: env[k] for k in ("MERCURY_APPROVAL_SOCKET",) if k in env}
        results = [
            _run_omp_task(i, t["prompt"], env["OMP_MODEL"], workdir, timeout,
                          env.get("OMP_FALLBACK_CHAIN"), batch_procs,
                          profile_home=_profile_home, extra_env=_extra or None,
                          delegation_id=delegation_id, name=t.get("name"),
                          goal=t.get("goal"),
                          owner_session_id=owner_session_id,
                          base_env=base_env,
                          isolate_worktree=_isolate_label(i, t))
            for i, t in enumerate(tasks)
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            _extra = {k: env[k] for k in ("MERCURY_APPROVAL_SOCKET",) if k in env}
            futures = [
                pool.submit(_run_omp_task, i, t["prompt"], env["OMP_MODEL"],
                            workdir, timeout, env.get("OMP_FALLBACK_CHAIN"),
                            batch_procs, profile_home=env.get("MERCURY_PROFILE_HOME"),
                            extra_env=_extra or None,
                            delegation_id=delegation_id, name=t.get("name"),
                            goal=t.get("goal"),
                            owner_session_id=owner_session_id,
                            base_env=base_env,
                            isolate_worktree=_isolate_label(i, t))
                for i, t in enumerate(tasks)
            ]
            results = [f.result() for f in futures]
    if prof is not None:
        for i, entry in enumerate(results):
            _wave_mem_profiler.note_result(
                prof, f"{delegation_id}/{i}",
                (entry or {}).get("summary") or (entry or {}).get("error"))
    return {
        "results": results,
        "total_duration_seconds": round(time.time() - started, 2),
        "engine": "omp",
    }


def dispatch_omp_delegation(parent_agent: Any, function_args: Dict[str, Any]) -> str:
    """B1 entry point — replaces Mercury-child spawn for delegate_task.

    Control actions (list/steer/stop) forward into the live-child registry
    (M0A, matrix observatory §8.1): RPC children are steered over their
    connection and stopped via graceful abort + SIGKILL fallback; one-shot
    children are listed/stoppable but not steerable.
    """
    from tools.registry import tool_error

    action = str(function_args.get("action") or "").strip().lower()
    if action in ("list", "steer", "stop"):
        return handle_omp_control_action(
            action,
            function_args.get("subagent_id"),
            function_args.get("message"),
            parent_agent,
        )

    # --- spawn path -----------------------------------------------------------
    from tools.delegate_tool import (
        _get_max_async_children,
        _get_max_concurrent_children,
        _resolve_workspace_hint,
        _strip_model_hidden_task_fields,
        normalize_delegation_names,
    )

    raw_tasks = _strip_model_hidden_task_fields(function_args.get("tasks"))
    task_dicts: List[Dict[str, Any]] = []
    if isinstance(raw_tasks, list) and raw_tasks:
        for t in raw_tasks:
            if not isinstance(t, dict):
                continue
            g = str(t.get("goal") or "").strip()
            if not g:
                continue
            task_dicts.append({
                "goal": g,
                "context": t.get("context"),
                "output_schema": t.get("output_schema"),
                "name": t.get("name"),
            })
    if not task_dicts:
        g = str(function_args.get("goal") or "").strip()
        if not g:
            return tool_error(
                "delegate_task needs task text: tasks[].goal (preferred) or "
                "the legacy top-level goal."
            )
        task_dicts.append({
            "goal": g,
            "context": function_args.get("context"),
            "output_schema": function_args.get("output_schema"),
            "name": None,  # legacy single-goal shape → fallback task-0
        })
    # M0A (§8.1 item 1): `name` is hard-required in the model-facing schema;
    # every other caller shape (legacy goal, cron, direct python) gets a
    # derived fallback here so nothing breaks.
    normalize_delegation_names(task_dicts)
    goals: List[Dict[str, Any]] = task_dicts

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
        {"prompt": _build_task_prompt(g["goal"], g["context"], g["output_schema"]),
         "name": g["name"], "goal": g["goal"]}
        for g in goals
    ]
    workdir = _resolve_workspace_hint(parent_agent)
    timeout = DEFAULT_TIMEOUT
    # HERMES-OMP PATCH (NO LIMITS, user directive 2026-09-05): no cap on
    # the number of concurrent subagents — every task gets its own worker.
    max_workers = max(1, len(tasks))
    is_subagent = getattr(parent_agent, "_delegate_depth", 0) > 0
    # M0A: registry spine — generated HERE so the id is known before the
    # runner starts (children register under <delegation_id>/<task_index>
    # the moment they spawn) and can be passed to the async registry.
    delegation_id = f"deleg_{uuid.uuid4().hex[:8]}"
    owner_session_id = str(getattr(parent_agent, "session_id", "") or "")
    # Wave-mem profiler (default off): when MERCURY_WAVE_MEM_PROFILE=1 (or
    # delegation.wave_mem_profile) a background sampler attributes
    # per-process RSS to parent/children/grandchildren for this wave and
    # spills to $MERCURY_HOME/logs/wave-mem/. None when off: zero overhead.
    _prof = _wave_mem_profiler.maybe_start(delegation_id, owner_session_id)

    def _stop_prof() -> None:
        _prof_path = _wave_mem_profiler.stop(_prof)
        if _prof_path is not None:
            logger.info("wave-mem profile for %s → %s", delegation_id, _prof_path)

    def _run_profiled_batch(
            batch_procs_arg: Optional[List["subprocess.Popen"]] = None,
    ) -> Dict[str, Any]:
        try:
            return _sync_run(tasks, env, workdir, timeout, max_workers,
                             batch_procs_arg,
                             delegation_id=delegation_id,
                             owner_session_id=owner_session_id,
                             prof=_prof)
        finally:
            _stop_prof()

    if is_subagent:
        # Orchestrator children need results within their own turn.
        return json.dumps(
            _run_profiled_batch(),
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
        result = _run_profiled_batch(batch_procs)
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
        runner=lambda: _run_profiled_batch(batch_procs),
        delegation_id=delegation_id,
        origin_ui_session_id=origin_ui_session_id,
        origin_session_id=origin_session_id,
        interrupt_fn=_interrupt_batch,
        max_async_children=_get_max_async_children(),
        names=[g["name"] for g in goals],
    )
    if dispatch.get("status") == "dispatched":
        n = len(tasks)
        note = (
            "omp is running the task in the background. Keep working; its "
            "result re-enters the conversation as a new message. Do not wait "
            "or poll. While it runs you can steer it (action='steer' + "
            "subagent_id + message) or stop it early (action='stop')."
            if n == 1 else
            f"{n} omp children are running in parallel in the background. "
            "Keep working; their consolidated results re-enter the "
            "conversation as a single message once ALL finish. Do not wait "
            "or poll. While they run you can steer or stop individual "
            "children (action='steer'/'stop' + subagent_id)."
        )
        return json.dumps({
            "status": "dispatched",
            "mode": "background",
            "engine": "omp",
            "count": n,
            "delegation_id": dispatch["delegation_id"],
            "model": model,
            "goals": [g["goal"] for g in goals],
            # M0A: name-addressable children — subagent_id for steer/stop.
            "children": [
                {
                    "subagent_id": f"{delegation_id}/{i}",
                    "name": g["name"],
                    "goal": g["goal"],
                }
                for i, g in enumerate(goals)
            ],
            "note": note,
        }, ensure_ascii=False)
    return tool_error(f"delegation rejected: {dispatch.get('error')}")
