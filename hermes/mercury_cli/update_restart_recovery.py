"""Recover update restarts from a fresh interpreter using idle-only admission.

The fresh process avoids the updater's stale module graph. It never invokes
administrative restart commands or signals. ``verified`` requires a live,
ready replacement PID; a deferred request is only ``relaunch_attempted``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from collections.abc import Iterable, Mapping

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SUPERVISOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def restart_profiles(
    profiles: Iterable[str],
    *,
    supervisors: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Ask each profile's gateway to restart when idle, without escalation."""
    from mercury_cli.gateway import request_automatic_gateway_restart
    from mercury_cli.profiles import _get_default_hermes_dir, get_profile_dir
    from mercury_cli.update_cmd import _wait_for_automatic_gateway_replacement

    result = {"verified": [], "relaunch_attempted": [], "failed": []}
    for profile in sorted(set(profiles)):
        try:
            home = _get_default_hermes_dir() if profile == "default" else get_profile_dir(profile)
            reply = request_automatic_gateway_restart(home=home, trigger="update-recovery")
            if not reply["restarting"]:
                result["failed"].append(profile)
            elif reply["deferred"]:
                result["relaunch_attempted"].append(profile)
            elif _wait_for_automatic_gateway_replacement(home, reply["pid"]):
                result["verified"].append(profile)
            else:
                result["relaunch_attempted"].append(profile)
        except Exception as exc:
            print(f"Automatic gateway recovery deferred for {profile}: {exc}", file=sys.stderr)
            result["failed"].append(profile)
    return result


def _parse_payload(stream) -> tuple[list[str], dict[str, str]]:
    payload = json.load(stream)
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        raise ValueError("recovery payload must contain a profiles list")
    if any(
        not isinstance(profile, str) or not _PROFILE_ID_RE.fullmatch(profile)
        for profile in profiles
    ):
        raise ValueError("recovery profiles contain an invalid profile id")
    raw_supervisors = payload.get("supervisors") if isinstance(payload, dict) else None
    supervisors: dict[str, str] = {}
    if raw_supervisors is not None:
        if not isinstance(raw_supervisors, dict) or any(
            not isinstance(profile, str)
            or not isinstance(supervisor, str)
            or not _PROFILE_ID_RE.fullmatch(profile)
            or not _SUPERVISOR_RE.fullmatch(supervisor)
            for profile, supervisor in raw_supervisors.items()
        ):
            raise ValueError("recovery supervisors map is invalid")
        supervisors = dict(raw_supervisors)
    return profiles, supervisors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if not args.stdin:
        parser.error("this command is an internal update-recovery entry point")

    try:
        profiles, supervisors = _parse_payload(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = restart_profiles(profiles, supervisors=supervisors)
    except (ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "verified": [],
                    "relaunch_attempted": [],
                    "failed": [],
                }
            )
        )
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
