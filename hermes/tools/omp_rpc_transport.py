"""MERCURY-OMP PATCH (C1): omp child transport — RPC mode with approval
routing into the Mercury (hermes-side) approval pipeline.

Problem this closes (TODO Track C): under unified approvals, omp children
run at a real approval mode (manual->always-ask, smart->write — NOT yolo).
When an omp child hits a prompt-tier decision (exec command under smart,
everything under manual), the one-shot ``-p`` path has no interactive UI
and FAILS CLOSED ("requires approval but no interactive UI available") —
delegation bricks instead of prompting.

Mechanism (all verified against the vendored omp 18.1.6 tree + docs/rpc.md):
  - omp ``--mode rpc`` exposes the approval gate as an
    ``extension_ui_request`` frame: the wrapper calls
    ``uiContext.select(formatApprovalPrompt(tool, args), ["Approve","Deny"])``
    (extensibility/extensions/wrapper.ts), rpc-mode serializes it as
    ``{type:"extension_ui_request", id, method:"select", message:<prompt>}``.
  - The bash tool's approval details line is ``Command: <cmd>`` (bash.ts
    formatApprovalDetails), so the shell command is recoverable host-side.
  - The host answers ``{type:"extension_ui_response", id, value:"Approve"|"Deny"}``.

Transport: the vendored ``omp_rpc`` Python client (omp/python/omp-rpc,
stdlib-only) — it owns process lifecycle, request correlation, v2 frame
negotiation, and the UI-request queue. We deliberately do NOT use
``install_headless_ui``'s listener path for approvals: listeners run
inline on the client's stdout reader thread, and a human approval can take
minutes — that would stall every other frame. Instead a dedicated handler
thread drains ``next_ui_request()`` and answers from the queue.

Routing: a ``select`` with options [Approve, Deny] whose message carries
``Command:`` goes to hermes' ``check_all_command_guards`` — the SAME
hardline floor, deny rules, smart-approval guardian, and (via the
registered callback) human prompt the user's own terminal commands face.
Everything else (passive notify/status, login selects) is answered
fail-closed deny so the child never wedges on an unattended dialog.

The one-shot ``-p`` path remains the default engine; this module is the
approval-capable upgrade seam (dispatch_omp_delegation opts in per-task).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Repositorio layout: this file is <repo>/hermes/tools/omp_rpc_transport.py
# (repo root two levels up).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_OMP_RPC_SRC = os.path.join(_REPO_ROOT, "omp", "python", "omp-rpc", "src")

_COMMAND_LINE_RE = re.compile(r"^Command:\s*(.+)$", re.MULTILINE | re.DOTALL)

# How long the dedicated UI thread waits between queue polls (seconds).
_UI_POLL_INTERVAL = 0.5
# MERCURY-OMP PATCH (matrix observatory §8.2): subagent frame subscription
# levels the omp RPC server accepts (RpcSubagentSubscriptionLevel).
_SUBAGENT_SUBSCRIPTION_LEVELS = ("off", "progress", "events")
_isolate_support_cache: Dict[str, bool] = {}


def _omp_supports_isolate_worktree(omp_path: str) -> bool:
    """Version gate: True when ``omp --help`` advertises --isolate-worktree.

    Cached per binary path. Any probe failure returns False — fail safe
    means omitting the flag (stale binaries predate it: unknown flag exit 2).
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



def _import_omp_rpc():
    """Import the vendored omp_rpc package (stdlib-only) by path."""
    if _OMP_RPC_SRC not in sys.path:
        sys.path.insert(0, _OMP_RPC_SRC)
    from omp_rpc import RpcClient  # noqa: WPS433 (runtime import by design)

    return RpcClient


def extract_command_from_prompt(message: str) -> Optional[str]:
    """Recover the shell command from omp's approval prompt text.

    The wrapper formats prompts as ``Allow tool: <name>\\n[Reason: ...\\n]
    Command: <cmd>`` (approval.ts formatApprovalPrompt + bash.ts
    formatApprovalDetails). Only the ``Command:`` line is authoritative;
    everything else is presentation.
    """
    match = _COMMAND_LINE_RE.search(message or "")
    if not match:
        return None
    command = match.group(1).strip()
    return command or None


def looks_like_approval_select(options: Optional[tuple], method: str) -> bool:
    """True when a UI request is the wrapper's Approve/Deny approval gate.

    The wrapper always selects between exactly ``["Approve", "Deny"]``
    (wrapper.ts approval gate); other selects (login flows, model pickers)
    have different option sets and are NOT routed to the command pipeline.
    """
    if method != "select":
        return False
    return tuple(options or ()) == ("Approve", "Deny")


def hermes_approval_decision(
    command: str,
    session_key: Optional[str] = None,
) -> bool:
    """Run one command through hermes' full guard stack; True = approve.

    This is the SAME stack the user's own terminal tool calls face:
    hardline floor, sudo-stdin guard, user deny rules, dangerous-command
    detection, tirith, smart-approval guardian, and the interactive
    approval callback when one is registered on this thread (the
    established pattern: delegate_tool/run_agent install it before
    spawning children — see approval.py _prompt_dangerous_approval_inner).

    ``session_key`` scopes approval persistence; when None the ambient
    session key is used (delegation threads inherit the parent's).
    """
    try:
        from tools.approval import (
            check_all_command_guards,
            get_current_session_key,
            set_current_session_key,
        )
    except ImportError:
        logger.exception("C1: tools.approval unavailable — failing closed")
        return False

    token = None
    if session_key:
        token = set_current_session_key(session_key)
    try:
        decision = check_all_command_guards(command, env_type="container")
        return bool(decision.get("approved"))
    except Exception:
        logger.exception("C1: guard stack raised — failing closed: %r", command[:120])
        return False
    finally:
        if token is not None:
            from tools.approval import reset_current_session_key

            reset_current_session_key(token)


def _deny(client: Any, request_id: str) -> None:
    client.cancel_ui_request(request_id)


def _approve_command_select(client: Any, request: Any) -> bool:
    """Answer an Approve/Deny select via the hermes guard stack (True=approve)."""
    # requestRpcSelect (rpc-mode.ts) serializes the prompt as the TITLE:
    # the wrapper calls select(safetyPrompt, ["Approve","Deny"]) and only
    # {method, title, options} go on the wire — there is no message field
    # for selects. Check both defensively.
    prompt_text = (getattr(request, "title", None)
                   or getattr(request, "message", None) or "")
    command = extract_command_from_prompt(prompt_text)
    if command is None:
        logger.warning(
            "C1: approval select without a parsable Command: line — denying "
            "(message head: %r)",
            (request.message or "")[:120],
        )
        _deny(client, request.id)
        return False
    approved = hermes_approval_decision(command)
    logger.info("C1: omp approval %s: %r", "APPROVED" if approved else "DENIED", command[:120])
    client.send_ui_value(request.id, "Approve" if approved else "Deny")
    return bool(approved)


# M4b (matrix observatory §5/D10): optional approval-frame observer. The
# observatory sidecar registers ONE process-global callback to mirror omp
# RPC approval gates into the child's Matrix room while the guard stack
# below still owns the decision. Purely additive: with no callback
# registered (the default) this module behaves exactly as before.
ApprovalFrameHook = Callable[[str, str, str, tuple], None]
_approval_frame_hook: Optional[ApprovalFrameHook] = None
ApprovalSettleHook = Callable[[str, bool], None]
_approval_settle_hook: Optional[ApprovalSettleHook] = None
def set_approval_frame_hook(cb: Optional[ApprovalFrameHook]) -> Optional[ApprovalFrameHook]:
    """Register/replace (``None`` unregisters) the approval-frame observer.

    The hook is called with ``(request_id, method, title, options)``
    whenever the responder thread sees a UI request that
    ``looks_like_approval_select`` matches, BEFORE the guard stack is
    consulted — unless the guard would auto-decide without a human (see
    :func:`tools.approval.guard_requires_human_approval`), in which case
    the mirror is skipped so the room never carries a prompt no /approve
    could resolve. Observational only: return values are ignored,
    exceptions are swallowed, and the guard decision below is unchanged.
    Returns the previously registered hook (for restore-on-unregister).
    """
    global _approval_frame_hook
    previous = _approval_frame_hook
    _approval_frame_hook = cb
    return previous


def set_approval_settle_hook(cb: Optional[ApprovalSettleHook]) -> Optional[ApprovalSettleHook]:
    """Register/replace (``None`` unregisters) the approval-settle observer.

    Called with ``(request_id, approved)`` AFTER the guard stack answers an
    approval select, so an observational mirror can settle the Matrix
    pending it created to the guard outcome instead of leaving it stale
    for a later /approve to hit the late path. Observational only.
    """
    global _approval_settle_hook
    previous = _approval_settle_hook
    _approval_settle_hook = cb
    return previous


def _mirror_wanted(request: Any) -> bool:
    """True when an approval select deserves a Matrix mirror (M4b).

    False when the guard would auto-decide without a human — mirroring
    then only creates a stale pending. Errors fail OPEN (mirror) so a
    real prompt is never skipped by a broken pre-check.
    """
    try:
        from tools.approval import guard_requires_human_approval
    except Exception:
        return True
    try:
        text = str(getattr(request, "title", None) or getattr(request, "message", None) or "")
        command = extract_command_from_prompt(text)
        if command is None:
            return True
        return bool(guard_requires_human_approval(command))
    except Exception:
        return True


def _notify_approval_frame(request: Any) -> None:
    """Fire the M4b hook for one approval select — never affects the decision."""
    hook = _approval_frame_hook
    if hook is None:
        return
    try:
        hook(
            str(getattr(request, "id", "") or ""),
            str(getattr(request, "method", "") or ""),
            str(getattr(request, "title", None) or getattr(request, "message", None) or ""),
            tuple(getattr(request, "options", None) or ()),
        )
    except Exception:
        logger.exception("M4b: approval-frame hook raised (ignored)")


def _notify_approval_settled(request_id: str, approved: bool) -> None:
    """Fire the M4b settle hook — never affects the decision."""
    hook = _approval_settle_hook
    if hook is None:
        return
    try:
        hook(str(request_id or ""), bool(approved))
    except Exception:
        logger.exception("M4b: approval-settle hook raised (ignored)")


def serve_approvals(client: Any, stop: threading.Event,
                    approval_callback: Optional[Callable] = None) -> None:
    """Dedicated approval-responder thread body.

    Drains ``next_ui_request`` off the client's queue (listeners stay
    untouched — they run inline on the reader thread and must not block on
    a human) and answers each request:
      - approval selects (Approve/Deny + Command:) -> hermes guard stack
      - everything else                          -> fail-closed cancel

    ``approval_callback``: the PARENT turn's thread-local callback (e.g.
    the gateway's chat prompt), re-registered on THIS thread so
    check_all_command_guards can surface a human prompt in the user's
    chat. Hermes stores it per-thread (_callback_tls), so without this
    copy a gateway-registered callback would be invisible here and
    dangerous-but-approvable commands would silently take the
    unattended-default path instead of asking the user.
    """
    if approval_callback is not None:
        try:
            from tools.terminal_tool import set_approval_callback

            set_approval_callback(approval_callback)
        except Exception:
            logger.exception("C1: could not install approval callback on "
                             "the responder thread")
    while not stop.is_set():
        try:
            request = client.next_ui_request(timeout=_UI_POLL_INTERVAL)
        except Exception:
            continue  # queue timeout — loop and re-check stop
        try:
            if looks_like_approval_select(request.options, request.method):
                # M4b mirror only when a human could answer it; auto-decided
                # guards resolve in ms and a mirrored prompt would go stale.
                mirrored = False
                if _mirror_wanted(request):
                    _notify_approval_frame(request)
                    mirrored = True
                approved = _approve_command_select(client, request)
                if mirrored:
                    _notify_approval_settled(str(getattr(request, "id", "") or ""), approved)
            elif request.method in ("cancel",) or request.is_passive():
                continue  # passive frames are not answered
            else:
                # Unrouted dialog (login, editor, generic confirm) in an
                # unattended child: deny so the child errors visibly
                # instead of hanging forever.
                logger.warning(
                    "C1: unrouted UI request (method=%s) denied fail-closed",
                    request.method,
                )
                _deny(client, request.id)
        except Exception:
            logger.exception("C1: UI request handler error (request %s)", request.id)


class OmpRpcControlError(Exception):
    """A control-plane call (steer/abort/observe) hit a dead or unborn
    child connection. Callers treat this as 'connection lost' and fall
    back to process-level signals."""


class OmpRpcChild:
    """One delegated omp child driven over RPC with approval routing.

    Minimal per-task lifecycle: start, prompt, wait for the final
    assistant text, stop. Reuses the vendored RpcClient for transport;
    Mercury contributes only the approval seam. The client spawns omp
    with ``start_new_session=True`` so the whole child tree dies with it.
    """

    def __init__(self, *, omp_path: str, model: str, workdir: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None,
                 approval_timeout: float = 600.0,
                 command_override: Optional[list] = None,
                 approval_callback: Optional[Callable] = None,
                 startup_timeout: float = 60.0,
                 thinking_level: Optional[str] = None,
                 isolate_worktree: Optional[str] = None):
        self._omp_path = omp_path
        self._model = model
        # MERCURY-OMP PATCH (user directive): thinking level is a CONFIG
        # parameter (models.delegate_thinking_level, default xhigh) — never
        # agent-selected per spawn.
        self._thinking_level = thinking_level
        # --isolate-worktree label (oh-my-pi#452): None = flag omitted.
        self._isolate_worktree = isolate_worktree
        self._workdir = workdir
        self._env = env or {}
        self._approval_timeout = approval_timeout
        self._command_override = command_override
        self._approval_callback = approval_callback
        self._startup_timeout = startup_timeout
        self._client: Any = None
        self._ui_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def proc(self):
        """The child's Popen handle (None before start).

        Exposed so the delegation engine can register the child in its
        batch-scoped interrupt lists — RpcClient spawns with
        start_new_session=True, so killpg on this pid takes the whole
        child tree, same contract as the -p one-shot path.
        """
        if self._client is None:
            return None
        return getattr(self._client, "_process", None)

    @property
    def pid(self):
        """Popen-duck-compatible pid (None before the process spawns)."""
        proc = self.proc
        return getattr(proc, "pid", None)

    def kill(self) -> None:
        """Popen-duck-compatible kill — tears the RPC child down."""
        self.stop()

    def start(self) -> None:
        RpcClient = _import_omp_rpc()
        # command= REPLACES the whole argv (no --mode rpc appended): the fake
        # server in tests IS the RPC server; the real omp path passes
        # [omp, --mode, rpc, --model, m] itself.
        argv = self._command_override or [
            self._omp_path, "--mode", "rpc", "--model", self._model]
        if self._thinking_level and not self._command_override:
            argv += ["--thinking", self._thinking_level]
        # Same guard as --thinking: test fakes via command_override keep full
        # control of the argv and never see this flag. Version-gated: stale
        # omp binaries predate --isolate-worktree, so omit + warn instead of
        # failing start with unknown-flag exit 2.
        if self._isolate_worktree and not self._command_override:
            try:
                supported = _omp_supports_isolate_worktree(self._omp_path)
            except Exception:
                supported = False
            if supported:
                argv += ["--isolate-worktree", self._isolate_worktree]
            else:
                logger.warning(
                    "omp binary %s does not advertise --isolate-worktree "
                    "(predates flag?) — omitting isolate label %r",
                    self._omp_path, self._isolate_worktree)
        self._client = RpcClient(
            command=list(argv),
            cwd=self._workdir,
            env=self._env,
            startup_timeout=self._startup_timeout,
        )
        self._client.start()
        self._ui_thread = threading.Thread(
            target=serve_approvals,
            args=(self._client, self._stop, self._approval_callback),
            name="omp-c1-approvals", daemon=True,
        )
        self._ui_thread.start()

    def run_task(self, prompt: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Run one task; return {status, summary|error, exit_reason}."""
        if self._client is None:
            return {
                "status": "failed", "summary": None,
                "error": "omp RPC child not started",
                "exit_reason": "error", "truncated": False,
                "model": self._model, "duration_seconds": 0.0,
            }
        import time as _time

        started = _time.time()
        try:
            turn = self._client.prompt_and_wait(prompt, timeout=timeout)
            text = turn.require_assistant_text()
            return {
                "status": "completed", "summary": text or "(omp returned no output)",
                "exit_reason": "completed", "truncated": False,
                "model": self._model,
                "duration_seconds": round(_time.time() - started, 2),
            }
        except Exception as exc:
            # Timeout parity with the -p one-shot entry contract: a task that
            # started but did not finish in time reports exit_reason=timeout,
            # not error (delegation consumers key off this).
            _import_omp_rpc()  # idempotent sys.path insert (done in start())
            from omp_rpc import RpcTimeoutError

            reason = "timeout" if isinstance(exc, RpcTimeoutError) else "error"
            return {
                "status": "failed", "summary": None,
                "error": f"omp RPC child failed: {exc}",
                "exit_reason": reason, "truncated": False,
                "model": self._model,
                "duration_seconds": round(_time.time() - started, 2),
            }

    # ------------------------------------------------------------------
    # MERCURY-OMP PATCH (matrix observatory §8.1/§8.2): live control +
    # subagent-observer surface. The delegated task IS this child's main
    # omp session, so steer/abort address the main session; the
    # subagent_* methods address omp's IN-PROCESS subagents (children of
    # this child — grandchildren of the delegating agent), matching the
    # server commands in omp rpc-types.ts.
    # ------------------------------------------------------------------

    def _require_client(self) -> Any:
        client = self._client
        if client is None:
            raise OmpRpcControlError("omp RPC child not started")
        proc = getattr(client, "_process", None)
        if proc is None or proc.poll() is not None:
            raise OmpRpcControlError("omp RPC child connection lost")
        return client

    def steer(self, text: str) -> None:
        """Queue steering text into the child's MAIN session mid-run.

        omp's steer semantics: the text is injected as a steering prompt at
        the next safe boundary — the in-flight tool call is never cut.
        """
        self._require_client().steer(text)

    def abort(self, reason: Optional[str] = None) -> None:
        """Ask the child's main session to stop at its next boundary.

        ``reason`` is host-side bookkeeping only — the wire ``abort``
        command carries no reason field (rpc-types.ts); subagent_abort
        does, and takes it verbatim.
        """
        client = self._require_client()
        if reason:
            logger.info("C1: aborting omp RPC child (reason: %s)", reason[:200])
        client.abort()

    def get_subagents(self) -> Dict[str, Any]:
        """Snapshot of omp's in-process AgentRegistry (AgentRef records)."""
        return dict(self._require_client().request_raw("get_subagents"))

    def set_subagent_subscription(self, level: str) -> Dict[str, Any]:
        """Set subagent frame subscription: 'off' | 'progress' | 'events'."""
        if level not in _SUBAGENT_SUBSCRIPTION_LEVELS:
            raise ValueError(
                f"subscription level must be one of "
                f"{_SUBAGENT_SUBSCRIPTION_LEVELS}, got {level!r}"
            )
        return dict(self._require_client().request_raw(
            "set_subagent_subscription", level=level))

    def get_subagent_messages(
        self,
        subagent_id: Optional[str] = None,
        session_file: Optional[str] = None,
        from_byte: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Read a subagent transcript by registry id or session file path.

        Returns ``{sessionFile, fromByte, nextByte, reset, entries,
        messages}`` — poll with ``fromByte=nextByte`` for incremental
        tailing (server contract: rpc-types.ts RpcSubagentMessagesResult).
        """
        if not subagent_id and not session_file:
            raise ValueError(
                "get_subagent_messages needs subagent_id or session_file"
            )
        return dict(self._require_client().request_raw(
            "get_subagent_messages",
            subagentId=subagent_id,
            sessionFile=session_file,
            fromByte=from_byte,
        ))

    def subagent_steer(self, subagent_id: str, text: str) -> None:
        """Steer one of omp's in-process subagents by registry id.

        Wire shape ``{subagentId, text}`` (rpc-types.ts, HERMES-OMP PATCH
        matrix observatory §8.2). Works while the subagent is live;
        parked isolated subagents cannot be revived (server-side rule).
        """
        if not str(subagent_id or "").strip():
            raise ValueError("subagent_steer needs a subagent_id")
        if not str(text or "").strip():
            raise ValueError("subagent_steer needs non-empty text")
        self._require_client().request_raw(
            "subagent_steer",
            subagentId=str(subagent_id),
            text=str(text),
        )

    def subagent_abort(
        self, subagent_id: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Abort one of omp's in-process subagents by registry id."""
        if not str(subagent_id or "").strip():
            raise ValueError("subagent_abort needs a subagent_id")
        return dict(self._require_client().request_raw(
            "subagent_abort",
            subagentId=str(subagent_id),
            reason=(str(reason) if reason else None),
        ))

    def stop(self) -> None:
        self._stop.set()
        if self._ui_thread is not None:
            self._ui_thread.join(timeout=5.0)
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                logger.exception("C1: client stop raised (ignored)")


class OmpRpcStartError(Exception):
    """The RPC child could not START (no ready frame, bad binary, or the
    vendored omp_rpc client failed to import).

    Raised by run_omp_task_rpc before any task was sent, so the delegation
    engine can fail over to the ``-p`` one-shot without double-execution
    risk: no prompt has reached the child at this point."""


def run_omp_task_rpc(
    omp_path: str,
    model: str,
    prompt: str,
    workdir: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 1800.0,
    command_override: Optional[list] = None,
    startup_timeout: float = 60.0,
    batch_procs: Optional[list] = None,
    rpc_procs: Optional[list] = None,
    approval_callback: Optional[Callable] = None,
    thinking_level: Optional[str] = None,
    isolate_worktree: Optional[str] = None,
    child_started: Optional[Callable[["OmpRpcChild"], None]] = None,
    child_finished: Optional[Callable[["OmpRpcChild"], None]] = None,
) -> Dict[str, Any]:
    """One-shot helper: full RPC child lifecycle around a single task.

    Mirrors _run_omp_task's entry contract so dispatch_omp_delegation can
    adopt it per-task without restructuring the fan-out. ``command_override``
    replaces the spawned argv entirely (test/diagnostic seam).
    ``startup_timeout`` bounds how long we wait for the RPC ready frame —
    the dispatch path keeps this SHORT (binary that doesn't speak RPC must
    fail over to the -p one-shot quickly, not stall the fan-out).
    ``batch_procs`` receives the OmpRpcChild itself (has .pid → killpg-
    compatible with the engine's Popen-based interrupt lists);
    ``rpc_procs`` optionally receives the inner Popen.
    ``child_started``/``child_finished`` (matrix observatory §8.1): the
    delegation engine registers the live OmpRpcChild in its steer/stop
    registry the moment the RPC connection is up (and deregisters it when
    the run ends), so control actions can reach the child mid-task.
    """
    child = OmpRpcChild(
        omp_path=omp_path, model=model, workdir=workdir,
        # NO LIMITS: None task timeout -> no approval clock either.
        env=env,
        thinking_level=thinking_level,
        isolate_worktree=isolate_worktree,
        approval_timeout=(min(timeout, 600.0) if timeout is not None else None),
        command_override=command_override,
        startup_timeout=startup_timeout,
        approval_callback=approval_callback,
    )
    # batch_procs tracking happens ONLY after start() returns: the engine's
    # interrupt path does killpg(getpgid(p.pid)) and a None pid would resolve
    # to OUR process group — suicide, not interrupt.
    try:
        try:
            child.start()
        except Exception as exc:
            # Start failure only (no ready frame / bad binary / vendored
            # client import error). Task-time failures return entries, they
            # do not raise — so this is unambiguously "could not start".
            raise OmpRpcStartError(f"{type(exc).__name__}: {exc}") from exc
        if batch_procs is not None:
            batch_procs.append(child)
        if rpc_procs is not None and child.proc is not None:
            rpc_procs.append(child.proc)
        if child_started is not None:
            try:
                child_started(child)
            except Exception:
                logger.exception("C1: child_started hook raised (ignored)")
        return child.run_task(prompt, timeout=timeout)
    finally:
        child.stop()
        if child_finished is not None:
            try:
                child_finished(child)
            except Exception:
                logger.exception("C1: child_finished hook raised (ignored)")
        if batch_procs is not None:
            try:
                batch_procs.remove(child)
            except ValueError:
                pass
