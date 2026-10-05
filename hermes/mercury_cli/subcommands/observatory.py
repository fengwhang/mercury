"""``mercury observatory`` — MIRC status, login, rooms, doctor, restart.

``status`` prints the provisioned network (listeners, unit, gateway
channel); ``login`` reprints setup's web login card;
``rooms`` lists live agent rooms from state.db; ``restart``
freshens the chat surface without the setup wizard: daemon + gateway
onto the code on disk, then verifies the bot joined its room.
"""
from __future__ import annotations

import argparse
import os
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


def _cmd_login(args) -> int:
    """Print the existing setup card without provisioning or changing state."""
    from mercury_constants import mercury_command

    try:
        from observatory import provision
        from mercury_cli.setup import (
            _print_observatory_setup_card, _tailscale_status, print_header,
        )

        home = getattr(args, "home", None)
        # Avoid bootstrapping config directories for an unconfigured install.
        status = (
            provision.status_summary(home)
            if provision.read_config(home) is not None else {}
        )
        if not status.get("provisioned"):
            print(
                f"observatory not provisioned — run {mercury_command()} setup observatory.",
                file=sys.stderr,
            )
            return 1
        print_header("Save this — Observatory login")
        _print_observatory_setup_card(
            status, _tailscale_status(provision), mercury_home=home,
        )
    except Exception as exc:
        print(f"observatory login unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_observatory(args) -> int:
    action = getattr(args, "observatory_action", None) or "status"
    if action == "login":
        return _cmd_login(args)
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
        print("gateway: restarting (active sessions will resume)", flush=True)
        proc = subprocess.run(
            [bin_, "gateway", "restart", "--quick"],
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
    """Freshen the chat surface: daemon + mlounge fork + gateway, then verify.

    A service unit bakes an interpreter path at render time, so a plain
    restart re-runs whatever tree was current when the unit was last
    written — updates then never reach the running daemon. This
    RE-RENDERS the unit from this install first (ensuring also restarts
    it), so "restart" always means "restart onto the code now on disk".
    When the bot does not come back, the full diagnosis prints here.
    """
    import time

    args._observatory_restart_epoch = time.time()
    try:
        from observatory.restart import prepare_room_cleanup
        from observatory.provision import ensure_observatory_unit

        cleanup = prepare_room_cleanup(getattr(args, "home", None))
        print(f"rooms: expiring {cleanup['expired_agents']} descendant/dead agent(s); "
              "keeping level-0 sessions and the configured gateway", flush=True)
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
    try:
        from observatory import mlounge as mlounge_mod

        configured = bool(mlounge_mod.status_mlounge().get("configured"))
    except Exception:  # noqa: BLE001 — a broken status read skips, never kills
        configured = False
    if configured:
        try:
            mlounge_result = mlounge_mod.refresh_mlounge_fork()
        except Exception as exc:  # noqa: BLE001 — refresh never kills restart
            mlounge_result = f"skipped-error: {exc}"
        print(f"mLounge: {mlounge_result}")
    else:
        print("mLounge: not installed, skipping")
    if _restart_gateway_now() != 0:
        return 1
    try:
        from mercury_cli.setup import _verify_gateway_bot

        ok, detail = _verify_gateway_bot(tries=20, wait=1, stable_samples=2)
    except Exception as exc:
        print(f"bot check unavailable: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"bot: {detail}", file=sys.stderr)
        _print_doctor()
        return 1
    print(f"bot: {detail}")
    return _verify_fleet(args)


def _verify_fleet(args) -> int:
    """Check resync results, room presence, and gateway dispatch transport.

    A gateway restart alone is not a fleet respawn — spawned agents only
    come back via boot resync (channel JOINs, identity reconnects, omp
    child rebuilds). This waits for a resync newer than the restart,
    then reads the current roster and checks every room with a probe client.
    Missing identities get a bounded shared grace period for reconnects.
    A private transport challenge also proves the
    gateway nick belongs to a receiver, not a send-only clone. This is NOT
    provider health; no model turn is run. Nonzero on any failed check.
    """
    import json as _json
    import time as _time

    start = getattr(args, "_observatory_restart_epoch", None) or _time.time()
    home = getattr(args, "home", None)
    state = _open_state(home)
    if state is None:
        print("fleet: observatory not provisioned (no state.db).", file=sys.stderr)
        return 1
    marker: dict = {}
    for _ in range(24):
        try:
            marker = _json.loads(state.get_meta("last-resync") or "{}")
        except Exception:
            marker = {}
        if isinstance(marker, dict) and float(marker.get("epoch") or 0) >= start:
            break
        _time.sleep(5)
    else:
        marker = marker if isinstance(marker, dict) else {}
    if float((marker or {}).get("epoch") or 0) < start:
        print("fleet: FAIL — no resync completed since the restart "
              "(bot never reconnected?)", file=sys.stderr)
        return 1
    for failure in (marker or {}).get("failed") or []:
        print(f"fleet: resync reported: {failure}")
    try:
        live = list(state.get_live())
    except Exception as exc:
        print(f"fleet: cannot read live agents ({exc})", file=sys.stderr)
        return 1
    if not live:
        print("fleet: no live agents (gateway row missing?)", file=sys.stderr)
        return 1
    try:
        from observatory.doctor import _Probe
        from observatory.provision import read_config, read_mirc_passwords
    except Exception as exc:
        print(f"fleet: probe unavailable ({exc})", file=sys.stderr)
        return 1
    try:
        cfg = read_config(home) or {}
        pw = read_mirc_passwords(home) or {}
        host = str(cfg.get("server_host") or "127.0.0.1")
        port = int(cfg.get("server_port") or 6670)
        secret = str(pw.get("server") or "")
    except Exception as exc:
        print(f"fleet: cannot read ircd config ({exc})", file=sys.stderr)
        return 1
    failures = len((marker or {}).get("failed") or [])
    probe = _Probe(host, port, f"mercury-fleet-{os.getpid() % 10000}", secret)
    try:
        if not probe.connect():
            print(f"fleet: FAIL — probe could not register on {host}:{port}",
                  file=sys.stderr)
            return 1
        pending = {row["node_id"]: row for row in live}
        observations = {}
        present = set()
        deadline = _time.monotonic() + 5.0
        while pending:
            # A delegated task can complete during restart verification. Use
            # the current roster, rather than fail an already-ended session.
            try:
                current = {row["node_id"]: row for row in state.get_live()}
            except Exception as exc:
                print(f"fleet: cannot read live agents ({exc})", file=sys.stderr)
                return 1
            for node_id in list(pending):
                if node_id not in current:
                    pending.pop(node_id)
                    continue
                row = pending[node_id] = current[node_id]
                channel = str(row.get("room_id") or "")
                nick = str(row.get("mxid") or "")
                name = str(row.get("name") or node_id)
                if not channel or not nick:
                    observations[node_id] = "no channel/nick recorded"
                    continue
                try:
                    members = probe.names(channel, timeout=max(
                        0.1, min(1.0, deadline - _time.monotonic())))
                except Exception as exc:
                    observations[node_id] = f"probe error ({exc})"
                    continue
                if members is None:
                    observations[node_id] = "membership probe timed out or JOIN rejected"
                elif nick.lower() in {str(m).lower() for m in members}:
                    print(f"fleet: presence ok {name} ({channel}) — {nick} present")
                    present.add(node_id)
                    pending.pop(node_id)
                else:
                    observations[node_id] = (
                        f"{nick} NOT present (members: {', '.join(members) or 'none'})")
            remaining = deadline - _time.monotonic()
            if not pending or remaining <= 0:
                break
            _time.sleep(min(0.25, remaining))
        for node_id, row in pending.items():
            channel = str((row or {}).get("room_id") or "")
            name = str((row or {}).get("name") or (row or {}).get("node_id"))
            print(f"fleet: FAIL {name} ({channel}): {observations[node_id]}")
            failures += 1
        gateway_nick = next((str(row.get("mxid") or "") for row in live
                             if row.get("node_id") == "gw"
                             or (row.get("extra") or {}).get("kind") == "gateway"), "")
        if gateway_nick and probe.gateway_roundtrip(gateway_nick):
            print("fleet: gateway dispatch transport round-trip ok (provider not tested)")
        else:
            print("fleet: FAIL — gateway nick does not answer dispatch probe; "
                  "presence alone is not a working agent", file=sys.stderr)
            failures += 1
    finally:
        try:
            probe.close()
        except Exception:
            pass
    if failures:
        print(f"fleet: {failures} check(s) failed", file=sys.stderr)
        return 1
    print(f"fleet: all {len(present)} live agent(s) present; gateway transport verified "
          "(provider replies not tested)")
    return 0


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
        help="MIRC observatory status, login, and rooms",
    )
    subs = parser.add_subparsers(dest="observatory_action")
    p_status = subs.add_parser("status", help="Show observatory status")
    p_status.add_argument("--home", default=None, help="Mercury home override")
    p_login = subs.add_parser("login", help="Show the web login card from setup")
    p_login.add_argument("--home", default=None, help="Mercury home override")
    p_rooms = subs.add_parser("rooms", help="List live agent rooms")
    p_rooms.add_argument("--home", default=None, help="Mercury home override")
    p_doctor = subs.add_parser("doctor", help="Diagnose user-to-agent chat path")
    p_doctor.add_argument("--home", default=None, help="Mercury home override")
    p_restart = subs.add_parser(
        "restart",
        help="Restart daemon + gateway onto current code, verify the bot",
    )
    parser.set_defaults(func=cmd_observatory)
