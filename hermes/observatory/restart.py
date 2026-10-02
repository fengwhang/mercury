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


def quick_restart_handler(runner, loop):
    """Control-socket handler; marshal a session-preserving restart to the loop."""
    def request():
        done = threading.Event()
        accepted = []

        def on_loop():
            try:
                accepted.append(runner.request_restart(
                    detached=False, via_service=True, after_turn_timeout=0.0))
            finally:
                done.set()

        loop.call_soon_threadsafe(on_loop)
        done.wait(timeout=5)
        return {
            "pid": os.getpid(), "restarting": bool(accepted and accepted[0]),
            "already_stopping": bool(accepted and not accepted[0]),
        }

    return request


def launch_observatory_restart(command: list[str]) -> None:
    """Reuse the CLI's daemon/frontend/gateway restart, with mLounge optional.

    A detached child still belongs to a systemd gateway's cgroup. A transient
    user unit gives the restart helper its own cgroup so it survives stopping
    the gateway. Other hosts use the normal detached-process mechanism.
    """
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env(scrub_secrets=False, inherit_profile_home=True)
    env.pop("_HERMES_GATEWAY", None)
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
