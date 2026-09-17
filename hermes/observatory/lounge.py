"""The Lounge frontend for the IRC observatory (replaces soju).

One Lounge instance per human user (``only one required per user``):
it stays connected to every mercury ircd on the tailnet as a regular
IRC client (persistent, backlog included) and serves its web UI to the
user's browser. Adding mercury networks happens in The Lounge UI —
this module only installs it, binds it, and keeps its unit running.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LOUNGE_PORT_DEFAULT = 9000
LOUNGE_UNIT_NAME = "mercury-lounge.service"
LOUNGE_UNIT_DESCRIPTION = "Mercury The Lounge frontend (observatory UI)"
LOUNGE_DIRNAME = "lounge"
FILE_LOUNGE_CONFIG = "config.js"


class LoungeError(RuntimeError):
    pass


def _run(args: list[str], *, input_text: Optional[str] = None,
         timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, input=input_text, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise LoungeError(f"lounge exec failed: {exc}") from exc


def _systemctl_available() -> bool:
    try:
        return shutil.which("systemctl") is not None
    except Exception:
        return False


class LoungePaths:
    """Resolved lounge layout under ``$MERCURY_HOME/observatory``."""

    def __init__(self, mercury_home: str | Path):
        self.root = Path(mercury_home).expanduser()
        self.dir = self.root / "observatory" / LOUNGE_DIRNAME
        self.conf = self.dir / FILE_LOUNGE_CONFIG
        self.home = self.dir / "home"


def lounge_bin() -> Path:
    """Path to the ``thelounge`` binary (npm global install)."""
    found = shutil.which("thelounge")
    if found:
        return Path(found)
    raise LoungeError(
        "thelounge binary not found — install it with: "
        "npm install -g thelounge (needs Node.js 18+) "
        "https://thelounge.chat/docs/installation")


def ensure_lounge_installed() -> str:
    """Make sure ``thelounge`` exists, installing via npm if asked-for.

    Never installs unprompted: raises with the exact command when the
    binary is missing so the wizard can offer it.
    """
    try:
        return str(lounge_bin())
    except LoungeError:
        pass
    npm = shutil.which("npm")
    if npm is None:
        raise LoungeError(
            "thelounge not installed and npm not found — install Node.js, "
            "then run: npm install -g thelounge")
    out = _run([npm, "install", "-g", "thelounge"], timeout=600)
    if out.returncode != 0:
        raise LoungeError(
            "npm install -g thelounge failed "
            f"(may need sudo): {(out.stderr or out.stdout).strip()}")
    return str(lounge_bin())


def render_lounge_config(*, host: str, port: int) -> str:
    """Render config.js (pure string templating, no I/O)."""
    return f"""// Managed by `mercury setup observatory` — hand edits are overwritten.
module.exports = {{
	host: "{host}",
	port: {int(port)},
	public: false,
	theme: "default",
}};
"""


def ensure_lounge_config(paths: LoungePaths, *, host: str, port: int) -> dict:
    """Write config.js when it differs. Returns {"action": ...}."""
    try:
        paths.dir.mkdir(parents=True, exist_ok=True)
        paths.home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise LoungeError(f"lounge dir create failed: {exc}") from exc
    rendered = render_lounge_config(host=host, port=port)
    try:
        current = paths.conf.read_text(encoding="utf-8") if paths.conf.is_file() else None
    except Exception:
        current = None
    if current == rendered:
        return {"action": "current", "path": str(paths.conf)}
    try:
        paths.conf.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        raise LoungeError(f"lounge config write failed: {exc}") from exc
    return {"action": "wrote" if current is None else "updated",
            "path": str(paths.conf)}


def lounge_users(paths: LoungePaths) -> list[str]:
    """Usernames with a stored Lounge login."""
    try:
        users_dir = paths.home / "users"
        if not users_dir.is_dir():
            return []
        return sorted(p.stem for p in users_dir.glob("*.json"))
    except Exception:
        return []


def ensure_lounge_user(paths: LoungePaths, username: str,
                       password: Optional[str]) -> dict:
    """Create the Lounge login (password via stdin, never argv).

    Returns {"action": created|current}. Raises LoungeError with manual
    instructions when creation needs an interactive terminal.
    """
    if username in lounge_users(paths):
        return {"action": "current"}
    if not password:
        raise LoungeError(
            f"lounge user {username!r} missing and no password given — "
            f"create it manually: thelounge --home {paths.home} add {username}")
    import os as _os

    full_env = dict(_os.environ)
    full_env["THELOUNGE_HOME"] = str(paths.home)
    try:
        proc = subprocess.run(
            [str(lounge_bin()), "add", username],
            input=password + "\n" + password + "\n",
            capture_output=True, text=True, timeout=120, env=full_env)
    except FileNotFoundError as exc:
        raise LoungeError(f"lounge add failed: {exc}") from exc
    if username not in lounge_users(paths):
        raise LoungeError(
            f"thelounge add {username!r} did not stick "
            f"({(proc.stderr or proc.stdout).strip() or proc.returncode}) — "
            f"create it manually: THELOUNGE_HOME={paths.home} "
            f"thelounge add {username}")
    return {"action": "created"}


def render_lounge_unit(*, lounge_bin: str, home: str) -> str:
    """Render the lounge systemd USER unit (pure string templating)."""
    return f"""\
[Unit]
Description={LOUNGE_UNIT_DESCRIPTION}
After=network-online.target mercury-observatory.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=THELOUNGE_HOME={home}
ExecStart={lounge_bin} start
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


def ensure_lounge_unit(paths: LoungePaths, *, unit: str) -> str:
    """Install/enable/start the lounge unit. Never raises for missing
    systemd (containers/CI) — returns "skipped"."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / LOUNGE_UNIT_NAME).write_text(unit, encoding="utf-8")
    except Exception as exc:
        raise LoungeError(f"lounge unit write failed: {exc}") from exc
    if not _systemctl_available():
        return "skipped"
    for args in (
        ["daemon-reload"],
        ["enable", LOUNGE_UNIT_NAME],
        ["start", LOUNGE_UNIT_NAME],
    ):
        out = _run(["systemctl", "--user", *args])
        if out.returncode != 0:
            raise LoungeError(
                f"systemctl --user {' '.join(args)} failed: "
                f"{(out.stderr or out.stdout).strip()}")
    return "installed"


def restart_lounge() -> None:
    out = _run(["systemctl", "--user", "restart", LOUNGE_UNIT_NAME])
    if out.returncode != 0:
        raise LoungeError(
            f"lounge restart failed: {(out.stderr or out.stdout).strip()}")


def lounge_unit_active() -> bool:
    try:
        out = _run(["systemctl", "--user", "is-active", LOUNGE_UNIT_NAME])
        return (out.stdout or "").strip() == "active"
    except Exception:
        return False


def provision_lounge(
    mercury_home: str | Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = LOUNGE_PORT_DEFAULT,
    username: str = "owner",
    password: Optional[str] = None,
    hermes_root: str | Path | None = None,
) -> dict:
    """Full Lounge layer: binary → config → unit → user.

    ``host`` is the WEB UI bind (127.0.0.1 or the tailnet IP).
    Mercury networks themselves are added in The Lounge UI.
    """
    from observatory.provision import _mercury_home  # local import: no cycle

    _ = hermes_root
    home = _mercury_home(mercury_home)
    summary: dict = {"bin": str(ensure_lounge_installed())}
    spaths = LoungePaths(home)
    summary["config"] = ensure_lounge_config(spaths, host=host, port=int(port))
    summary["unit"] = ensure_lounge_unit(
        spaths,
        unit=render_lounge_unit(
            lounge_bin=summary["bin"], home=str(spaths.home)))
    summary["user"] = ensure_lounge_user(spaths, username, password)
    return summary


def lounge_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when something answers on the Lounge web-UI port (never raises)."""
    import socket as _socket

    try:
        with _socket.create_connection((str(host), int(port)),
                                       timeout=timeout):
            return True
    except Exception:
        return False


def _local_port_answers(port: int, timeout: float = 0.5) -> bool:
    """True when our own box answers on ``port`` (any local address).

    Localhost alone misses tailnet-bound services (a Lounge listening
    only on the tailnet IP). Probing every local address covers both
    without importing provisioning state. Never raises."""
    import socket as _socket

    candidates: set[str] = {"127.0.0.1"}
    try:
        for _fam, _typ, _proto, _canon, sockaddr in _socket.getaddrinfo(
                _socket.gethostname(), int(port),
                type=_socket.SOCK_STREAM):
            host = sockaddr[0] if isinstance(sockaddr, tuple) else ""
            if host and not str(host).startswith("127."):
                candidates.add(str(host))
    except Exception:
        pass
    return any(lounge_port_open(host, port, timeout=timeout)
               for host in candidates)


def status_lounge(mercury_home: str | Path | None = None) -> dict:
    """Best-effort Lounge status for setup/status surfaces (never raises)."""
    from observatory.provision import _mercury_home  # local import: no cycle

    try:
        home = _mercury_home(mercury_home)
        spaths = LoungePaths(home)
        conf = str(spaths.conf) if spaths.conf.is_file() else ""
        try:
            binary = str(lounge_bin())
        except LoungeError:
            binary = ""
        host, port = _read_lounge_bind(spaths)
        configured = bool(conf)
        return {
            "configured": configured,
            "binary": binary,
            "users": lounge_users(spaths),
            "unit": "active" if lounge_unit_active() else "inactive",
            "host": host,
            "port": port,
            # A Lounge the user runs themselves (container, another
            # box's install): our config is absent but the port answers
            # on some local address (localhost OR tailnet-bound).
            "external": (not configured) and _local_port_answers(
                LOUNGE_PORT_DEFAULT),
        }
    except Exception:
        return {"configured": False, "binary": "", "users": [],
                "unit": "unknown", "host": "", "port": 0,
                "external": False}


def _read_lounge_bind(spaths: "LoungePaths") -> tuple:
    """Bound web-UI host/port parsed from config.js (best effort)."""
    import re as _re

    try:
        conf = spaths.conf.read_text(encoding="utf-8", errors="replace")
        host = _re.search(r'host:\s*"([^"]+)"', conf)
        port = _re.search(r"port:\s*(\d+)", conf)
        return (host.group(1) if host else "127.0.0.1",
                int(port.group(1)) if port else LOUNGE_PORT_DEFAULT)
    except Exception:
        return "127.0.0.1", LOUNGE_PORT_DEFAULT
