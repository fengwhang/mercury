"""Launch a full Observatory restart outside the gateway being restarted."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from uuid import uuid4

# Let the gateway send its acknowledgement before the MIRC socket closes.
_RESTART_HELPER = (
    "import os,sys,time; time.sleep(2); "
    "os.environ.pop('_HERMES_GATEWAY', None); "
    "os.execvpe(sys.argv[1], sys.argv[1:], os.environ)"
)


def prepare_room_cleanup(mercury_home=None) -> dict:
    """Journal restart expiry before either service can restore old rooms.

    Root sessions are durable and will resume. Descendants cannot resume their
    old task transports, so an explicit Observatory restart ends them. Unknown
    cached frontend rooms are rejected by MIRC's managed-room policy.
    """
    import json
    import time

    from observatory.provision import (
        GATEWAY_NODE_ID, ensure_gateway_node_in_state, live_server_name,
    )
    from observatory.rooms import gateway_channel
    from observatory.spawn import ExitRecord, PURGE_JOURNAL_KEY, read_purge_journal
    from observatory.state import (
        CLOSED_ROOMS_META_KEY, MANAGED_ROOMS_META_KEY, ObservatoryState,
        default_state_db_path,
    )

    server = live_server_name(mercury_home)
    if not server:
        raise RuntimeError("Observatory is not provisioned (no MIRC network name)")
    gateway = gateway_channel(server).lower()
    with ObservatoryState(default_state_db_path(mercury_home)) as state:
        ensure_gateway_node_in_state(state, server_name=server)
        with state.locked() as db, db:
            rows = db.execute(
                "SELECT node_id, parent_node_id, depth, status, room_id, extra_json FROM nodes "
                "ORDER BY depth, created_epoch, node_id"
            ).fetchall()
            entries = read_purge_journal(state)
            protected = {row["node_id"] for row in rows if row["node_id"] == GATEWAY_NODE_ID
                         or (row["status"] == "live" and row["depth"] == 0
                             and row["parent_node_id"] is None
                             and json.loads(row["extra_json"]).get("kind") != "delegate")}
            protected_channels = {gateway} | {row["room_id"].lower() for row in rows
                                               if row["node_id"] in protected and row["room_id"]}
            # Old journals must not annihilate a protected room or a newly
            # registered root that reused an expired name.
            for entry in entries:
                entry["rows"] = [row for row in entry.get("rows", []) if row["node_id"] not in protected]
                entry["channels"] = [channel for channel in entry.get("channels", [])
                                     if channel.lower() not in protected_channels]
            pending = {row["node_id"] for entry in entries for row in entry.get("rows", [])}
            # Older delegation bugs left kind=delegate rows at depth zero.
            # They are not explicit root sessions and must not be resurrected.
            removed = [dict(row) for row in rows
                       if row["node_id"] not in pending and row["node_id"] not in protected]
            channels = sorted({str(row["room_id"]) for row in removed
                               if row["room_id"] and row["room_id"].lower() != gateway})
            prior = db.execute("SELECT value FROM meta WHERE key = ?", (CLOSED_ROOMS_META_KEY,)).fetchone()
            closed = set(json.loads(prior[0])) if prior else set()
            closed.update(channel.lower() for channel in channels)
            closed.discard(gateway)
            if removed:
                record = ExitRecord(
                    journal_id=f"pj-restart-{uuid4().hex}", node_id="restart-cleanup",
                    status="restart", summary=None, created_epoch=time.time(),
                    rows=removed, channels=channels,
                )
                entries.append(record.to_entry())
                db.executemany("UPDATE nodes SET status = 'dead', died_epoch = ? WHERE node_id = ?",
                               [(time.time(), row["node_id"]) for row in removed])
            # Even a previously tombstoned gateway is protected and made live.
            db.execute("UPDATE nodes SET status = 'live', died_epoch = NULL WHERE node_id = ?",
                       (GATEWAY_NODE_ID,))
            for key, value in ((CLOSED_ROOMS_META_KEY, json.dumps(sorted(closed))),
                               (PURGE_JOURNAL_KEY, json.dumps(entries)),
                               (MANAGED_ROOMS_META_KEY, "true")):
                db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    return {"expired_agents": len(removed), "channels": channels, "gateway": gateway}


def quick_restart_handler(runner, loop):
    """Control-socket handler; marshal a session-preserving restart to the loop."""
    def request(params=None):
        done = threading.Event()
        accepted = []

        def on_loop():
            try:
                accepted.append(runner.request_restart(
                    detached=False, via_service=True, after_turn_timeout=0.0,
                    trigger="control:restart-observatory",
                    actor=(params or {}).get("_authenticated_actor"),
                    request_id=(params or {}).get("request_id")))
            finally:
                done.set()

        loop.call_soon_threadsafe(on_loop)
        done.wait(timeout=5)
        return {
            "pid": os.getpid(), "restarting": bool(accepted and accepted[0]),
            "already_stopping": bool(accepted and not accepted[0]),
        }

    return request


def launch_observatory_restart(command: list[str], *, request_id: str | None = None) -> None:
    """Reuse the CLI's daemon/frontend/gateway restart, with mLounge optional.

    A detached child still belongs to a systemd gateway's cgroup. A transient
    user unit gives the restart helper its own cgroup so it survives stopping
    the gateway. Other hosts use the normal detached-process mechanism.
    """
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
    env.pop("_HERMES_GATEWAY", None)
    if request_id:
        env["MERCURY_RESTART_REQUEST_ID"] = request_id
    helper = [sys.executable, "-c", _RESTART_HELPER, *command, "observatory", "restart"]
    if os.environ.get("INVOCATION_ID"):
        systemd_run = shutil.which("systemd-run")
        if not systemd_run:
            raise RuntimeError("systemd-run is required to restart Observatory from this gateway service")
        # Pass only launch/profile settings. Provider credentials are loaded
        # by the CLI from the selected profile, never placed in unit arguments.
        inherited = (
            "PATH", "PYTHONPATH", "MERCURY_HOME", "MERCURY_CONFIG", "MERCURY_CMD",
            "MERCURY_CHANNEL", "MERCURY_REPO", "MERCURY_PYTHON", "HERMES_HOME",
            "PI_CODING_AGENT_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
            "MERCURY_RESTART_REQUEST_ID",
        )
        argv = [systemd_run, "--user", "--collect", "--quiet", "--property=Type=exec",
                f"--unit=mercury-observatory-restart-{uuid4().hex}"]
        argv.extend(f"--setenv={key}={env[key]}" for key in inherited if key in env)
        result = subprocess.run([*argv, "--", *helper], env=env, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(f"Observatory restart launcher failed (exit {result.returncode})")
        return
    if sys.platform == "win32":
        from mercury_cli._subprocess_compat import windows_detach_popen_kwargs

        detach = windows_detach_popen_kwargs()
    else:
        detach = {"start_new_session": True}
    subprocess.Popen(helper, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
