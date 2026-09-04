"""MERCURY-OMP PATCH (C1 slice 2b): RPC-first omp_direct cron transport.

Closes the TODO Track C gap: omp_direct cron jobs spawned via the ``-p``
one-shot inherit omp's unattended failure mode — a prompt-tier approval
gate fails closed ("requires approval but no interactive UI available")
instead of routing to Mercury's guard stack. This module runs the SAME
RPC transport the delegation engine adopted (C1 slice 2a,
tools/omp_rpc_transport) so omp_direct jobs get identical approval
routing: hardline floor → deny rules → smart guardian, with the
unattended-default path when no human callback exists (cron has no chat
surface mid-run — safe commands auto-approve under smart mode,
dangerous ones deny).

Fallback contract (mirrors tools/omp_delegation._run_omp_task):

  - ``None`` → the caller must fall back to the ``-p`` one-shot. Returned
    when the kill-switch (HERMES_OMP_TRANSPORT=oneshot) is set, the
    transport module cannot be imported, or the RPC child cannot START
    (OmpRpcStartError — raised strictly BEFORE any prompt is sent, so
    failover carries no double-execution hazard).
  - a scheduler 4-tuple ``(ok, doc, output, err_code)`` → the RPC child
    STARTED; the result is FINAL. A task that started under RPC and then
    failed is a real failure and is never re-run on the one-shot (same
    policy as the delegation engine — side-effecting tasks must not run
    twice).

MERCURY-OMP PATCH: this module exists only in the mercury distribution.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Must equal cron.scheduler.SILENT_MARKER ("[SILENT]") — the delivery
# suppressor keys on this exact string. Kept local to avoid importing the
# 8k-line scheduler from a helper it calls.
SILENT = "[SILENT]"

# run_task() in tools/omp_rpc_transport substitutes this exact sentinel
# when the child completes with no assistant text (it is Mercury's own
# string, not model output — safe to match).
_NO_OUTPUT_SENTINEL = "(omp returned no output)"

# Same knobs the delegation engine uses — one switch per distribution,
# not per spawn site.
_KILL_SWITCH = "HERMES_OMP_TRANSPORT"
_STARTUP_KNOB = "HERMES_OMP_RPC_STARTUP"
_STARTUP_DEFAULT = 20.0


def omp_direct_rpc_attempt(
    *,
    omp_bin: str,
    model: str,
    prompt: str,
    env: Optional[Dict[str, str]] = None,
    workdir: Optional[str] = None,
    timeout: float = 1800.0,
    job_id: str = "",
    job_name: str = "",
    now_iso: str = "",
    command_override: Optional[list] = None,
) -> Optional[Tuple[bool, str, str, Optional[str]]]:
    """Run one omp_direct task over the RPC transport.

    Returns the scheduler result tuple, or ``None`` when the caller must
    fall back to the ``-p`` one-shot (see module docstring). Never raises
    for task-time outcomes — a started task's failure is reported as a
    failed tuple, not an exception.
    """
    if os.environ.get(_KILL_SWITCH, "").strip().lower() == "oneshot":
        return None

    try:
        from tools.omp_rpc_transport import OmpRpcStartError, run_omp_task_rpc
    except Exception:
        logger.warning(
            "omp_direct: omp_rpc_transport unavailable — one-shot fallback",
            exc_info=True,
        )
        return None

    try:
        entry = run_omp_task_rpc(
            omp_path=omp_bin,
            model=model,
            prompt=prompt,
            workdir=workdir,
            env=env,
            timeout=float(timeout),
            startup_timeout=float(os.environ.get(_STARTUP_KNOB, _STARTUP_DEFAULT)),
            command_override=command_override,
        )
    except OmpRpcStartError as exc:
        # Start failure only — no prompt reached the child, so falling
        # back to the one-shot cannot double-execute the task.
        logger.warning(
            "omp_direct: RPC start failed (%s) — one-shot fallback", exc
        )
        return None

    exit_reason = entry.get("exit_reason")
    model_label = entry.get("model") or model

    if entry.get("status") == "completed":
        summary = (entry.get("summary") or "").strip()
        if not summary or summary == _NO_OUTPUT_SENTINEL:
            return (
                True,
                f"# Cron Job: {job_name}\n\n"
                f"**Mode:** omp_direct (rpc)\n\n(omp produced no output)\n",
                SILENT,
                None,
            )
        doc = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {now_iso}\n"
            f"**Mode:** omp_direct (rpc · model {model_label})\n\n"
            f"---\n\n"
            f"{summary}\n"
        )
        return (True, doc, summary, None)

    # Started-but-failed: final result, no one-shot retry (double-exec
    # hazard for side-effecting tasks).
    err = (entry.get("error") or f"omp RPC child failed ({exit_reason})").strip()
    err_clipped = err[:1500]
    doc = (
        f"# Cron Job: {job_name}\n\n"
        f"**Mode:** omp_direct (rpc {exit_reason})\n\n"
        f"```\n{err_clipped}\n```\n"
    )
    alert = f"omp rpc {exit_reason}: {err}"[:1500]
    code = "omp_timeout" if exit_reason == "timeout" else "omp_rpc_error"
    return (False, doc, alert, code)
