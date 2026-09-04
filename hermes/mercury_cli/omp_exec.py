"""mercury-omp hybrid: native /omp command executor.

Routes the user's task text VERBATIM to omp one-shot (`omp --model <delegate
model> -p <task>`) with the task passed as a single argv element — no LLM in
the path, no rephrasing possible by construction.

Model + fallback come from the distribution's config.yaml (four slots,
fail-hard) via bridge/bridge.py --delegate. One model, no role routing
(patched omp tree forces every subagent to the session model).

MERCURY-OMP PATCH: this module exists only in the mercury-omp distribution.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from mercury_cli.slash_exec import CommandContext, CommandReply

# Distribution layout: <repo>/mercury/ is this tree; repo root one level up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = _REPO_ROOT / "bridge" / "bridge.py"
OMP_BIN = os.environ.get("HERMES_OMP_BIN", "omp")
DEFAULT_TIMEOUT = int(os.environ.get("HERMES_OMP_TIMEOUT", "1800"))


def _render_delegate_env() -> tuple[dict[str, str], str | None]:
    """Run bridge.py --delegate; parse OMP_* lines.

    Returns (env dict, error). Fail-hard: a refusing bridge aborts /omp with
    the actionable FATAL lines shown verbatim — never run without a valid
    four-slot config, never substitute defaults.
    """
    if not BRIDGE.exists():
        return {}, f"bridge not found at {BRIDGE} (mercury-omp layout broken?)"
    try:
        out = subprocess.run(
            [sys.executable, str(BRIDGE), "--delegate"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {}, "bridge timed out (30s) while validating model config"
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip()
        return {}, f"model config invalid — bridge refused:\n{detail}"
    env: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if line.startswith("OMP_") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    if "OMP_MODEL" not in env:
        return {}, "bridge produced no OMP_MODEL — refusing to run"
    return env, None


def _exec_omp(ctx: CommandContext) -> CommandReply:
    """Core /omp executor — deterministic verbatim routing to omp."""
    task = ctx.args.strip()
    if not task:
        return CommandReply(
            "Usage: /omp <task>\n"
            "Routes the task text verbatim to omp one-shot (single-model\n"
            "fan-out, no role routing). Model from the distribution config.",
            format="markdown",
        )

    env_map, err = _render_delegate_env()
    if err:
        return CommandReply(f"❌ /omp aborted: {err}")

    model = env_map["OMP_MODEL"]
    omp_path = shutil.which(OMP_BIN)
    if omp_path is None:
        return CommandReply(
            f"❌ omp binary not found ({OMP_BIN}). Build the patched tree:\n"
            f"  cd {_REPO_ROOT / 'omp'} && bun install && bun run build",
        )

    # task as ONE argv element: verbatim by construction
    cmd = [omp_path, "--model", model, "-p", task]
    env = os.environ.copy()
    if "OMP_FALLBACK_CHAIN" in env_map:
        env["OMP_FALLBACK_CHAIN"] = env_map["OMP_FALLBACK_CHAIN"]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT, env=env
        )
    except subprocess.TimeoutExpired:
        return CommandReply(
            f"⏱️ /omp timed out after {DEFAULT_TIMEOUT}s — task may still be running."
        )

    body = proc.stdout.strip() or "(no stdout)"
    if proc.returncode != 0 and proc.stderr.strip():
        body += f"\n\n[stderr]\n{proc.stderr.strip()}"
    tail = "" if proc.returncode == 0 else f"\n\n[exit {proc.returncode}]"
    header = f"— omp · model {model} · {os.getcwd()}\n\n"
    return CommandReply(header + body + tail)
