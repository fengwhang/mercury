"""``mercury observatory`` — Matrix observatory device-trust operations.

Today: ``trust-device`` approves pending device rotations. A rotation goes
pending when a known device ID presents changed keys (Element/Element X
identity reset): the sidecar keeps refusing it (fail closed, never
auto-trusted) and records old/new fingerprints + first-seen time. Approval
drops the old record, marks the new keys VERIFIED, and forces Megolm
rotation on the next share.
"""

from __future__ import annotations

import argparse
import sys


def _open_state(home: str | None):
    """Open the observatory state DB, or return None when unprovisioned.

    Never creates the DB: constructing ObservatoryState would mkdir + touch
    an empty state.db as a side effect, so existence is checked first.
    """
    from observatory.provision import _mercury_home
    from observatory.state import ObservatoryState, default_state_db_path

    db = default_state_db_path(_mercury_home(home))
    if not db.exists():
        return None
    return ObservatoryState(db)


def _close_state(state) -> None:
    try:
        state.close()
    except Exception:  # noqa: BLE001 — teardown must not raise
        pass


def _print_pending(rec: dict, *, show_user: bool = False) -> None:
    from observatory.e2ee import trust_device_command

    user = str(rec.get("user_id") or "")
    device = str(rec.get("device_id") or "")
    cmd = trust_device_command(device)
    if show_user:
        cmd += f" --user {user}"
    print(f"- {user}/{device}")
    print(f"  old identity: {rec.get('old_identity_key')}")
    print(f"  old signing:  {rec.get('old_signing_key')}")
    print(f"  new identity: {rec.get('new_identity_key')}")
    print(f"  new signing:  {rec.get('new_signing_key')}")
    print(f"  first seen:   {rec.get('first_seen')}")
    print(f"  approve:      {cmd}")


def _cmd_list(state, args) -> int:
    from observatory.e2ee import list_pending_trusts

    pendings = list_pending_trusts(state)
    if args.device:
        pendings = [r for r in pendings if str(r.get("device_id")) == args.device]
        if args.user:
            pendings = [r for r in pendings if str(r.get("user_id")) == args.user]
        if not pendings:
            print(f"No pending device rotation for device '{args.device}'.",
                  file=sys.stderr)
            return 1
    if not pendings:
        print("No pending device rotations.")
        return 0
    multi_user = len({str(r.get("user_id")) for r in pendings}) > 1
    print(f"Pending device rotations ({len(pendings)}):")
    for rec in pendings:
        _print_pending(rec, show_user=multi_user or bool(args.user))
    return 0


def _confirm(question: str) -> bool:
    """Explicit yes/no approval. Non-TTY stdin never approves (use --yes)."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _cmd_approve(state, args, rec: dict) -> int:
    from observatory.e2ee import approve_pending_trust

    user = str(rec.get("user_id") or "")
    device = str(rec.get("device_id") or "")
    if not args.yes:
        _print_pending(rec)
        if not _confirm(f"Trust the new keys for {user}/{device}?"):
            print("Skipped (no changes made).")
            return 2
    try:
        approval = approve_pending_trust(state, user_id=user, device_id=device)
    except Exception as exc:  # noqa: BLE001 — E2EEError carries the remedy
        print(f"Could not approve {user}/{device}: {exc}", file=sys.stderr)
        return 1
    print(f"Approved {user}/{device} (approved at {approval.get('approved_at')}).")
    print("Next share drops the old device record, encrypts to the new "
          "verified keys, and rotates the Megolm session.")
    return 0


def cmd_observatory(args) -> int:
    action = getattr(args, "observatory_action", None) or "trust-device"
    state = _open_state(getattr(args, "home", None))
    if state is None:
        print("Observatory state not found — run `mercury setup observatory` "
              "first.", file=sys.stderr)
        return 1
    try:
        if action == "trust-device" and not getattr(args, "device", None):
            return _cmd_list(state, args)
        if action == "trust-device":
            from observatory.e2ee import list_pending_trusts

            pendings = [r for r in list_pending_trusts(state)
                        if str(r.get("device_id")) == args.device]
            if getattr(args, "user", None):
                pendings = [r for r in pendings
                            if str(r.get("user_id")) == args.user]
            if not pendings:
                print(f"No pending device rotation for device "
                      f"'{args.device}'.", file=sys.stderr)
                return 1
            if len(pendings) > 1:
                print(f"Device '{args.device}' is pending for multiple users; "
                      f"re-run with --user to disambiguate:", file=sys.stderr)
                for rec in pendings:
                    print(f"  {rec.get('user_id')}/{rec.get('device_id')}",
                          file=sys.stderr)
                return 1
            return _cmd_approve(state, args, pendings[0])
        print(f"Unknown observatory action '{action}'.", file=sys.stderr)
        return 2
    finally:
        _close_state(state)


def build_observatory_parser(subparsers) -> None:
    """Attach the ``observatory`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "observatory",
        help="Matrix observatory device trust (pending rotation approvals)",
        description=("Inspect and approve pending Matrix device rotations. "
                     "Rotations stay refused until explicitly approved here."),
    )
    subs = parser.add_subparsers(dest="observatory_action")
    p_trust = subs.add_parser(
        "trust-device",
        help="List pending device rotations; approve one with --device",
        description=("List pending device rotations (same device ID, changed "
                      "keys after an Element/Element X identity reset) and "
                      "approve one explicitly. Approval drops the old record, "
                      "marks the new keys verified, and forces Megolm "
                      "rotation on the next share."),
    )
    p_trust.add_argument(
        "--device",
        default=None,
        help="Approve the pending rotation for this device ID "
             "(omit to only list pendings)",
    )
    p_trust.add_argument(
        "--user",
        default=None,
        help="Disambiguate when several users pend the same device ID",
    )
    p_trust.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Approve without the interactive confirmation prompt",
    )
    p_trust.add_argument(
        "--home",
        default=None,
        help="Mercury home (default: $MERCURY_HOME or ~/.mercury)",
    )
    parser.set_defaults(func=cmd_observatory)
