"""``mercury observatory`` — IRC observatory status and room listing.

``status`` prints the provisioned network (listeners, unit, gateway
channel); ``rooms`` lists live agent rooms from state.db.
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
    for key in ("enabled", "provisioned", "server_name", "agent", "bouncer",
                "unit", "bouncer_password_set", "agent_password_set", "config_path"):
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
    return _cmd_status(args)


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
    parser.set_defaults(func=cmd_observatory)
