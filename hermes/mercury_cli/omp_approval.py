"""Assess OMP commands with Hermes guards; leave human prompts to OMP's UI."""
from __future__ import annotations

import contextlib
import json
import os
import sys


def assess_command(command: str, session_key: str = "") -> dict:
    from tools import approval

    escalation = None

    def defer_to_owner(_command, description, **_kwargs):
        nonlocal escalation
        escalation = description
        return "deny"

    interactive = approval.set_hermes_interactive_context(True)
    session = approval.set_current_session_key(session_key or "omp:smart")
    try:
        result = approval.check_all_command_guards(
            command, env_type="local", approval_callback=defer_to_owner,
        )
        if escalation is not None:
            return {"policy": "prompt", "reason": escalation}
        if result.get("approved"):
            return {"policy": "allow"}
        return {"policy": "deny", "reason": result.get("message") or "Blocked by Mercury command guards"}
    finally:
        approval.reset_current_session_key(session)
        approval.reset_hermes_interactive_context(interactive)


def main() -> None:
    # This subprocess only assesses risk. It never owns a gateway prompt or
    # reads terminal input; the caller routes escalation to its existing UI.
    for key in ("HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_SESSION_PLATFORM",
                "HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION"):
        os.environ.pop(key, None)
    try:
        request = json.load(sys.stdin)
        command = request["command"]
        session = request.get("session", "")
        if not isinstance(command, str) or not command.strip() or not isinstance(session, str):
            raise ValueError("Invalid command assessment request")
        # Optional scanners/hooks may print diagnostics. Keep the IPC reply
        # separate from them, and never echo commands into protocol errors.
        with contextlib.redirect_stdout(sys.stderr):
            result = assess_command(command, session)
    except Exception:
        result = {"policy": "prompt", "reason": "Smart assessment unavailable; manual approval required"}
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
