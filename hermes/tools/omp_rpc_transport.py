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


def _approve_command_select(client: Any, request: Any) -> None:
    """Answer an Approve/Deny select via the hermes guard stack."""
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
        return
    approved = hermes_approval_decision(command)
    logger.info("C1: omp approval %s: %r", "APPROVED" if approved else "DENIED", command[:120])
    client.send_ui_value(request.id, "Approve" if approved else "Deny")


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
                _approve_command_select(client, request)
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
                 thinking_level: Optional[str] = None):
        self._omp_path = omp_path
        self._model = model
        # MERCURY-OMP PATCH (user directive): thinking level is a CONFIG
        # parameter (models.delegate_thinking_level, default xhigh) — never
        # agent-selected per spawn.
        self._thinking_level = thinking_level
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
    """
    child = OmpRpcChild(
        omp_path=omp_path, model=model, workdir=workdir,
        # NO LIMITS: None task timeout -> no approval clock either.
        env=env,
        thinking_level=thinking_level,
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
        return child.run_task(prompt, timeout=timeout)
    finally:
        child.stop()
        if batch_procs is not None:
            try:
                batch_procs.remove(child)
            except ValueError:
                pass
