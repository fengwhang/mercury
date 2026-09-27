"""``mercury observatory`` — IRC observatory status, rooms, doctor, restart.

``status`` prints the provisioned network (listeners, unit, gateway
channel); ``rooms`` lists live agent rooms from state.db; ``restart``
freshens the chat surface without the setup wizard: daemon + gateway
onto the code on disk, then verifies the bot joined its room.
"""
from __future__ import annotations

import argparse
import sys


def _open_state(home: str | None):
    """Open the observatory state DB, or return None when unprovisioned."""
    try:
        from observatory.provision import _mercury_home
        from observatory.state import ObservatoryState, default_state_db_path

        db = default_state_db_path(_mercury_home(home))
        if not db.is_file():
            return None
        return ObservatoryState(db)
    except Exception:
        return None


def _cmd_status(args) -> int:
    try:
        from observatory.provision import status_summary
    except Exception as exc:
        print(f"observatory unavailable: {exc}", file=sys.stderr)
        return 1
    try:
        status = status_summary(getattr(args, "home", None))
    except Exception as exc:
        print(f"status failed: {exc}", file=sys.stderr)
        return 1
    for key in ("enabled", "provisioned", "server_name", "agent", "server",
                "unit", "server_password_set", "agent_password_set", "config_path"):
        print(f"{key}: {status.get(key)}")
    return 0


def _cmd_rooms(args) -> int:
    state = _open_state(getattr(args, "home", None))
    if state is None:
        print("observatory not provisioned (no state.db).", file=sys.stderr)
        return 1
    try:
        rows = state.get_live()
    except Exception as exc:
        print(f"rooms failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            state.close()
        except Exception:
            pass
    if not rows:
        print("no live rooms.")
        return 0
    for row in rows:
        print(f"{row.get('room_id') or '(no room)'}  "
              f"{row.get('engine')}  {row.get('name')}  [{row.get('node_id')}]")
    return 0


def cmd_observatory(args) -> int:
    action = getattr(args, "observatory_action", None) or "status"
    if action == "rooms":
        return _cmd_rooms(args)
    if action == "doctor":
        return _cmd_doctor(args)
    if action == "restart":
        return _cmd_restart(args)
    return _cmd_status(args)


def _restart_gateway_now() -> int:
    """Restart the gateway service so the bot rebuilds on current code."""
    try:
        import shutil
        import subprocess

        from mercury_constants import mercury_command

        bin_ = shutil.which(mercury_command())
        if bin_ is None:
            print("gateway: command not found on PATH", file=sys.stderr)
            return 1
        proc = subprocess.run(
            [bin_, "gateway", "restart"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:
        print(f"gateway: restart failed ({exc})", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        print("gateway: restart failed" + (f" ({tail[0]})" if tail else ""),
              file=sys.stderr)
        return 1
    print("gateway: restarted")
    return 0


def _print_doctor() -> None:
    """Dump the full chat-path diagnosis (the answer, in the same output)."""
    try:
        from observatory.doctor import run_doctor

        for ok, label, detail in run_doctor():
            print(f"[{'ok' if ok else 'FAIL'}] {label}: {detail}")
    except Exception as exc:
        print(f"doctor unavailable: {exc}", file=sys.stderr)


def _cmd_restart(args) -> int:
    """Freshen the chat surface: daemon + gateway, then verify the bot.

    A service unit bakes an interpreter path at render time, so a plain
    restart re-runs whatever tree was current when the unit was last
    written — updates then never reach the running daemon. This
    RE-RENDERS the unit from this install first (ensuring also restarts
    it), so "restart" always means "restart onto the code now on disk".
    When the bot does not come back, the full diagnosis prints here.
    """
    try:
        from observatory.provision import ensure_observatory_unit

        unit_result = str(ensure_observatory_unit())
    except Exception as exc:
        print(f"observatory unavailable: {exc}", file=sys.stderr)
        return 1
    if unit_result == "installed":
        print("daemon: restarted onto current code")
    elif unit_result == "skipped":
        print("daemon: skipped (no systemd on this host)")
    else:
        print(f"daemon: FAILED ({unit_result})", file=sys.stderr)
        return 1
    if _restart_gateway_now() != 0:
        return 1
    try:
        from mercury_cli.setup import _verify_gateway_bot

        ok, detail = _verify_gateway_bot(tries=6, wait=10, stable_samples=3)
    except Exception as exc:
        print(f"bot check unavailable: {exc}", file=sys.stderr)
        return 0
    if ok:
        print(f"bot: {detail}")
        return 0
    print(f"bot: {detail}", file=sys.stderr)
    _print_doctor()
    return 1


def _cmd_doctor(args) -> int:
    try:
        from observatory.doctor import run_doctor
    except Exception as exc:
        print(f"doctor unavailable: {exc}", file=sys.stderr)
        return 1
    home = getattr(args, "home", None)
    if home:
        import os as _os
        _os.environ["MERCURY_HOME"] = home
    results = run_doctor(home)
    failed = 0
    for ok, label, detail in results:
        mark = "ok" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {label}: {detail}")
    return 1 if failed else 0


def build_observatory_parser(subparsers) -> None:
    """Attach the ``observatory`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "observatory",
        help="IRC observatory status and rooms",
    )
    subs = parser.add_subparsers(dest="observatory_action")
    p_status = subs.add_parser("status", help="Show observatory status")
    p_status.add_argument("--home", default=None, help="Mercury home override")
    p_rooms = subs.add_parser("rooms", help="List live agent rooms")
    p_rooms.add_argument("--home", default=None, help="Mercury home override")
    p_doctor = subs.add_parser("doctor", help="Diagnose user-to-agent chat path")
    p_doctor.add_argument("--home", default=None, help="Mercury home override")
    p_restart = subs.add_parser(
        "restart",
        help="Restart daemon + gateway onto current code, verify the bot",
    )
    parser.set_defaults(func=cmd_observatory)
