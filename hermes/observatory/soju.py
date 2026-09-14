"""soju bouncer frontend for the IRC observatory (provisioning).

Architecture: phones talk to soju (the reference bouncer Goguma is
built for: ``BOUNCER_NETID``, ``soju.im/*`` caps, background/push);
soju upstreams over localhost to our own ircd, which stays the agent
network (rooms, gateway bot, trace feeds, OPER DESTROY). The login
card's host/ports do not change — they terminate at soju instead of
the ircd directly.

Layout (all under ``$MERCURY_HOME/observatory``):
``soju.conf`` → listeners + db + tls; ``soju.db`` → users/networks;
``soju-admin`` → unix admin socket for sojuctl. Binaries ship in the
release tarball at ``hermes/observatory/soju-binaries/``
(``soju-<arch>``, ``sojuctl-<arch>``, ``sojudb-<arch>``), built
pure-Go (``-tags=moderncsqlite``), pinned to :data:`SOJU_VERSION_PIN`.

Users/networks cannot live in soju.conf — they are bootstrapped via
sojudb (users) and sojuctl/BouncerServ (networks), then the unit is
restarted (soju only picks up db changes on restart).
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: Pinned soju release (built with -tags=moderncsqlite, CGO_ENABLED=0).
SOJU_VERSION_PIN = "v0.10.1"
#: Single-owner login (the IRC nick stays the user's choice at login).
SOJU_USER = "owner"
#: systemd user unit for the bouncer.
SOJU_UNIT_NAME = "mercury-soju.service"
SOJU_UNIT_DESCRIPTION = "Mercury soju bouncer (observatory frontend)"
#: Binary dir inside the installed hermes tree.
SOJU_BIN_DIRNAME = "soju-binaries"

FILE_SOJU_CONF = "soju.conf"
FILE_SOJU_DB = "soju.db"
FILE_SOJU_ADMIN_SOCK = "soju-admin"


class SojuError(RuntimeError):
    pass


def soju_arch() -> str:
    """Release arch suffix for this host (matches the packed binaries)."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise SojuError(f"unsupported arch for soju binaries: {platform.machine()}")


def hermes_root() -> Path:
    return Path(__file__).resolve().parent.parent


def soju_bin(name: str, root: str | Path | None = None) -> Path:
    """Path to a packed soju binary (soju | sojuctl | sojudb)."""
    if name not in ("soju", "sojuctl", "sojudb"):
        raise SojuError(f"unknown soju binary: {name}")
    path = Path(root or hermes_root()) / "observatory" / SOJU_BIN_DIRNAME
    path = path / f"{name}-{soju_arch()}"
    if not path.is_file():
        raise SojuError(
            f"soju binary missing: {path} "
            f"(ships in release >= v0.0.63; dev checkouts: see scripts/build-soju.sh)"
        )
    return path


class SojuPaths:
    """Resolved soju layout under ``$MERCURY_HOME/observatory``."""

    def __init__(self, mercury_home: str | Path):
        self.root = Path(mercury_home).expanduser()
        self.dir = self.root / "observatory"
        self.conf = self.dir / FILE_SOJU_CONF
        self.db = self.dir / FILE_SOJU_DB
        self.admin_sock = self.dir / FILE_SOJU_ADMIN_SOCK


def render_soju_conf(
    *,
    bouncer_host: str,
    bouncer_port: int,
    tls_port: int,
    tls_cert: str,
    tls_key: str,
    server_name: str,
    db_path: str,
    admin_sock: str,
) -> str:
    """Render soju.conf (pure string templating, no I/O).

    soju owns the PUBLIC bouncer bind (plaintext + TLS); the ircd
    bouncer drops to localhost when ``soju_front`` is set in ircd.json.
    Plaintext off-localhost requires the ``irc+insecure://`` scheme or
    soju refuses to start.
    """
    return f"""\
# Managed by `mercury setup observatory` — hand edits are overwritten.
listen irc+insecure://{bouncer_host}:{bouncer_port}
listen ircs://{bouncer_host}:{tls_port}
listen unix+admin://{admin_sock}
hostname {server_name}
tls {tls_cert} {tls_key}
db sqlite3 {db_path}
"""


def render_soju_unit(*, soju_bin: str, config_path: str) -> str:
    """Render the soju systemd USER unit (pure string templating)."""
    return f"""\
[Unit]
Description={SOJU_UNIT_DESCRIPTION}
After=network-online.target mercury-observatory.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={soju_bin} -config {config_path}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


def _write_text(path: Path, content: str) -> str:
    """Write content; return 'wrote' | 'updated' | 'current'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return "current"
        except Exception:
            pass
        action = "updated"
    else:
        action = "wrote"
    path.write_text(content, encoding="utf-8")
    return action


def _run(args: list[str], *, input_text: str | None = None,
         timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, input=input_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SojuError(f"soju exec failed: {exc}") from exc


def _systemctl_available() -> bool:
    try:
        from observatory.provision import _systemctl_available as _avail

        return bool(_avail())
    except Exception:
        return False


def soju_unit_active() -> bool:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", SOJU_UNIT_NAME],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    return out.stdout.strip() == "active"


def ensure_soju_config(paths: SojuPaths, *, conf: str) -> dict:
    return {"action": _write_text(paths.conf, conf), "path": str(paths.conf)}


def ensure_soju_unit(paths: SojuPaths, *, unit: str) -> str:
    """Install/enable/start the soju unit. Never raises for missing
    systemd (containers/CI) — returns "skipped"."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / SOJU_UNIT_NAME).write_text(unit, encoding="utf-8")
    except Exception as exc:
        raise SojuError(f"soju unit write failed: {exc}") from exc
    if not _systemctl_available():
        return "skipped"
    for args in (
        ["daemon-reload"],
        ["enable", SOJU_UNIT_NAME],
        ["start", SOJU_UNIT_NAME],
    ):
        out = _run(["systemctl", "--user", *args])
        if out.returncode != 0:
            raise SojuError(
                f"systemctl --user {' '.join(args)} failed: "
                f"{(out.stderr or out.stdout).strip()}")
    return "installed"


def restart_soju() -> None:
    out = _run(["systemctl", "--user", "restart", SOJU_UNIT_NAME])
    if out.returncode != 0:
        raise SojuError(
            f"soju restart failed: {(out.stderr or out.stdout).strip()}")


def _sojuctl(conf: str, *words: str) -> subprocess.CompletedProcess[str]:
    return _run([str(soju_bin("sojuctl")), "-config", conf, *words])


def soju_user_exists(conf: str, username: str) -> bool:
    out = _sojuctl(conf, "user", "status", username)
    return out.returncode == 0


def ensure_soju_user(paths: SojuPaths, username: str,
                     password: str | None) -> dict:
    """Create the owner login (or rotate its password when given).

    sojudb reads the password from stdin (never argv — ps-visible).
    Returns {"action": created|password-changed|current}; caller must
    restart soju when the db changed.
    """
    conf = str(paths.conf)
    if soju_user_exists(conf, username):
        if not password:
            return {"action": "current"}
        out = _run(
            [str(soju_bin("sojudb")), "-config", conf,
             "change-password", username],
            input_text=password + "\n")
        if out.returncode != 0:
            raise SojuError(
                f"sojudb change-password failed: "
                f"{(out.stderr or out.stdout).strip()}")
        return {"action": "password-changed"}
    if not password:
        raise SojuError(
            f"soju user {username!r} missing and no password given "
            "(run setup with a bouncer password)")
    out = _run(
        [str(soju_bin("sojudb")), "-config", conf,
         "create-user", username, "-admin"],
        input_text=password + "\n")
    if out.returncode != 0:
        raise SojuError(
            f"sojudb create-user failed: {(out.stderr or out.stdout).strip()}")
    return {"action": "created"}


def soju_networks(conf: str, username: str) -> str:
    # Network commands need a user session even on the admin socket.
    out = _sojuctl(conf, "user", "run", username, "network", "status")
    if out.returncode != 0:
        raise SojuError(
            f"sojuctl network status failed: {(out.stderr or out.stdout).strip()}")
    return out.stdout


def ensure_soju_network(paths: SojuPaths, *, name: str, addr: str,
                        nick: str, username: str, password: str) -> dict:
    """Create or converge the upstream network (our own ircd).

    ``addr`` must use the ``irc+insecure://`` scheme for plaintext
    localhost — bare hostnames default to TLS upstream and fail.
    Returns {"action": created|converged|current}; caller restarts
    soju when not current (create/update reconnect upstream).
    """
    conf = str(paths.conf)
    run = ("user", "run", SOJU_USER)
    try:
        status = soju_networks(conf, SOJU_USER)
    except SojuError:
        status = ""
    exists = any(
        line.split(" (", 1)[0].strip() == name
        for line in status.splitlines() if line.strip())
    if exists:
        out = _sojuctl(
            conf, *run, "network", "update", name,
            "-addr", addr, "-nick", nick, "-username", username,
            "-pass", password)
        if out.returncode != 0:
            raise SojuError(
                f"sojuctl network update failed: "
                f"{(out.stderr or out.stdout).strip()}")
        return {"action": "converged"}
    out = _sojuctl(
        conf, *run, "network", "create",
        "-addr", addr, "-name", name, "-nick", nick,
        "-username", username, "-pass", password)
    if out.returncode != 0:
        raise SojuError(
            f"sojuctl network create failed: {(out.stderr or out.stdout).strip()}")
    return {"action": "created"}


def set_soju_front(mercury_home: str | Path, on: bool) -> bool:
    """Flip the ``soju_front`` flag in ircd.json (True: soju owns the
    public bouncer bind; the ircd bouncer drops to localhost).

    Returns True when the flag changed."""
    from observatory.provision import ObservatoryPaths  # local import: no cycle

    paths = ObservatoryPaths(mercury_home)
    try:
        cfg = json.loads(paths.config_file.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return False
    except Exception:
        return False
    if bool(cfg.get("soju_front")) == bool(on):
        return False
    cfg["soju_front"] = bool(on)
    paths.config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return True


def _wait_admin_sock(sock: str, timeout: int = 20) -> None:
    import socket as _socket
    import time as _time

    end = _time.time() + max(1, timeout)
    while _time.time() < end:
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(sock)
            s.close()
            return
        except Exception:
            _time.sleep(0.5)
    raise SojuError(f"soju admin socket never appeared: {sock}")


def _restart_ircd_unit() -> None:
    from observatory.config_gen import OBSERVATORY_UNIT_NAME

    out = _run(["systemctl", "--user", "restart", OBSERVATORY_UNIT_NAME])
    if out.returncode != 0:
        raise SojuError(
            f"ircd restart failed: {(out.stderr or out.stdout).strip()}")


def provision_soju(
    mercury_home: str | Path | None = None,
    *,
    bouncer_password: str | None = None,
    hermes_root: str | Path | None = None,
) -> dict:
    """Full soju layer: conf → soju_front → unit → user → network.

    Reads bind/ports/server_name/TLS paths from the live ircd.json
    (single source of truth — the tailscale offer keeps writing it).
    ``bouncer_password`` seeds/changes the downstream login; None
    leaves an existing login untouched.
    """
    from observatory.provision import (
        ObservatoryPaths,
        _mercury_home,
        live_server_name,
        read_config,
        read_irc_passwords,
    )

    home = _mercury_home(mercury_home)
    cfg = read_config(home) or {}
    server = live_server_name(home) or "mercury"
    bouncer_host = str(cfg.get("bouncer_host") or "127.0.0.1")
    bouncer_port = int(cfg.get("bouncer_port") or 6670)
    tls_port = int(cfg.get("tls_port") or 6697)
    opaths = ObservatoryPaths(home)
    tls_cert = str(opaths.tls_cert)
    tls_key = str(opaths.tls_key)
    if not (opaths.tls_cert.is_file() and opaths.tls_key.is_file()):
        raise SojuError("TLS cert missing — run the ircd provisioning first")
    # Binaries resolve BEFORE any state change (fail loud, change nothing).
    bins = {n: str(soju_bin(n, hermes_root)) for n in ("soju", "sojuctl", "sojudb")}
    logger.info("ircd: soju binaries %s", bins["soju"])

    spaths = SojuPaths(home)
    summary: dict = {"bins": bins}
    summary["config"] = ensure_soju_config(
        spaths,
        conf=render_soju_conf(
            bouncer_host=bouncer_host, bouncer_port=bouncer_port,
            tls_port=tls_port, tls_cert=tls_cert, tls_key=tls_key,
            server_name=server, db_path=str(spaths.db),
            admin_sock=str(spaths.admin_sock)))
    summary["front"] = (
        "enabled" if set_soju_front(home, True) else "current")
    if summary["front"] == "enabled" and _systemctl_available():
        # Port handoff: the ircd held the public bind until now; it must
        # drop to localhost BEFORE soju starts or the bind collides.
        _restart_ircd_unit()
        summary["ircd_handoff"] = True
    summary["unit"] = ensure_soju_unit(
        spaths,
        unit=render_soju_unit(
            soju_bin=bins["soju"], config_path=str(spaths.conf)))
    have = read_irc_passwords(home)
    downstream = bouncer_password or have.get("bouncer") or ""
    summary["user"] = ensure_soju_user(spaths, SOJU_USER, downstream or None)
    changed = changed or summary["user"]["action"] != "current"
    if summary["user"]["action"] != "current" and _systemctl_available():
        # sojudb writes land only on restart (fresh users are invisible
        # to the running daemon until then).
        restart_soju()
        _wait_admin_sock(str(spaths.admin_sock))
        summary["user_restarted"] = True

    upstream_pass = have.get("bouncer") or ""
    if not upstream_pass:
        raise SojuError("no bouncer password in .env — run the ircd provisioning first")
    summary["network"] = ensure_soju_network(
        spaths, name=server, addr=f"irc+insecure://127.0.0.1:{bouncer_port}",
        nick=SOJU_USER, username=SOJU_USER, password=upstream_pass)
    changed = changed or summary["network"]["action"] != "current"

    if changed and soju_unit_active():
        restart_soju()
        summary["restarted"] = True
    return summary


def status_soju(mercury_home: str | Path | None = None) -> dict:
    """Best-effort soju status for setup/status surfaces (never raises)."""
    from observatory.provision import _mercury_home  # local import: no cycle

    try:
        home = _mercury_home(mercury_home)
        spaths = SojuPaths(home)
        conf = str(spaths.conf) if spaths.conf.is_file() else ""
        up = ""
        if conf:
            try:
                up = soju_networks(conf, SOJU_USER)
            except SojuError:
                up = ""
        connected = "connected" in up.lower()
        return {
            "configured": bool(conf),
            "unit": "active" if soju_unit_active() else "inactive",
            "upstream_connected": connected,
        }
    except Exception:
        return {"configured": False, "unit": "unknown", "upstream_connected": False}


def main(argv: list[str] | None = None) -> int:
    """``python -m observatory.soju`` (install.sh entry point)."""
    import argparse

    parser = argparse.ArgumentParser(description="Provision the soju bouncer layer")
    parser.add_argument("--mercury-home", default=None)
    parser.add_argument("--hermes-root", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        summary = provision_soju(
            args.mercury_home, hermes_root=args.hermes_root)
    except SojuError as exc:
        print(f"soju provisioning failed: {exc}")
        return 1
    unit = summary.get("unit")
    print(f"soju ready: {summary['config']['path']} (unit {unit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
