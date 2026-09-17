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
NPM_PREFIX_DIRNAME = "npm"


class LoungeError(RuntimeError):
    pass


def _run(args: list[str], *, input_text: Optional[str] = None,
         timeout: int = 120,
         extra_env: Optional[dict] = None) -> subprocess.CompletedProcess[str]:
    try:
        import os as _os

        env = None
        if extra_env:
            env = dict(_os.environ)
            env.update(extra_env)
        return subprocess.run(
            args, input=input_text, capture_output=True, text=True,
            timeout=timeout, env=env)
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


def lounge_prefix(mercury_home: str | Path | None = None) -> Path:
    """Our isolated npm prefix (never the system global dirs)."""
    from observatory.provision import _mercury_home  # local import: no cycle

    return Path(_mercury_home(mercury_home)) / "observatory" / LOUNGE_DIRNAME / NPM_PREFIX_DIRNAME


def lounge_bin(mercury_home: str | Path | None = None) -> Path:
    """Path to the ``thelounge`` binary (our npm prefix first)."""
    try:
        ours = lounge_prefix(mercury_home) / "bin" / "thelounge"
        if ours.is_file():
            return ours
    except Exception:
        pass
    found = shutil.which("thelounge")
    if found:
        return Path(found)
    raise LoungeError(
        "thelounge binary not found — install it with: "
        "npm install -g thelounge (needs Node.js 18+) "
        "https://thelounge.chat/docs/installation")


def ensure_node() -> str:
    """Make sure Node.js + npm exist, installing via the system package
    manager when missing (needs sudo — the wizard offers first).

    Fresh computers have no Node: without this the Lounge layer can
    never provision itself. Raises LoungeError with the manual command
    when every install path fails.
    """
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node and npm:
        return str(node)
    for args in (
        ["sudo", "dnf", "install", "-y", "nodejs", "npm"],
        ["sudo", "apt-get", "install", "-y", "nodejs", "npm"],
    ):
        try:
            out = _run(args, timeout=600)
        except Exception:
            continue
        if (out is not None and getattr(out, "returncode", 1) == 0
                and shutil.which("node") and shutil.which("npm")):
            return str(shutil.which("node"))
    raise LoungeError(
        "Node.js not found and automatic install failed — install it "
        "by hand (Fedora: sudo dnf install -y nodejs npm), then re-run setup")


def ensure_lounge_installed(mercury_home: str | Path | None = None) -> str:
    """Make sure ``thelounge`` exists, installing via npm if asked-for.

    Installs into our own prefix (never system globals — no sudo, no
    PATH dependence). Uses ``--ignore-scripts``: thelounge's git-pinned
    irc-framework builds BROWSER bundles in its prepare step
    (``babel: command not found`` — npm never links git-dep devDeps for
    preparation), but the server runs from ``src/`` directly
    (``main: src/``), so the skipped build is browser-only and the
    installed server is complete (verified live: HTTP 200).
    Lifecycle scripts still inherit a PATH containing node+npm, which
    bare ``npm install -g`` lacks when node lives outside PATH.
    Never installs unprompted: raises with the exact command when the
    binary is missing so the wizard can offer it.
    """
    try:
        return str(lounge_bin(mercury_home))
    except LoungeError:
        pass
    npm = shutil.which("npm")
    if npm is None:
        raise LoungeError(
            "thelounge not installed and npm not found — install Node.js, "
            "then run: npm install -g thelounge")
    import os as _os

    prefix = lounge_prefix(mercury_home)
    node = shutil.which("node") or ""
    # The prefix bin comes FIRST (and need not exist yet): thelounge's
    # git-pinned irc-framework builds itself via `npm-run-all` in a
    # nested prepare script that inherits this PATH. Without a
    # pre-seeded npm-run-all on it, the whole install dies with 127.
    path = _os.pathsep.join(
        [str(prefix / "bin"),
         str(Path(npm).parent), str(Path(node).parent)]
        + [_os.environ.get("PATH", "")])
    env = {"PATH": path}
    out = _run(
        [npm, "install", "-g", "--prefix", str(prefix),
         "--ignore-scripts", "thelounge"],
        extra_env=env, timeout=900)
    if out.returncode != 0:
        raise LoungeError(
            "npm install -g thelounge failed: "
            f"{(out.stderr or out.stdout).strip()[-3000:]}")
    try:
        return str(lounge_bin(mercury_home))
    except LoungeError:
        raise LoungeError(
            "npm install reported success but the thelounge binary "
            f"is missing under {prefix}") from None


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
    try:
        (paths.home / "users").mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise LoungeError(f"lounge users dir create failed: {exc}") from exc
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


def ensure_lounge_network(
    paths: LoungePaths,
    username: str,
    *,
    net_name: str,
    host: str,
    port: int,
    server_password: str,
    nick: str,
    channel: str,
) -> dict:
    """Pre-seed this mercury server as a Lounge network for ``username``.

    Single-install story: after setup the user opens :9000, logs in,
    and the gateway channel is already there — no manual "add network"
    step. Edits ``users/<username>.json`` (written by ``thelounge add``)
    in place, replacing any same-named network. The caller restarts the
    unit afterwards so a running Lounge picks it up. Without a server
    password the entry could never log in — skipped, never half-written.
    """
    import uuid as _uuid

    if not server_password:
        return {"action": "skipped", "reason": "no server password"}
    users_file = paths.home / "users" / f"{username}.json"
    try:
        data = json.loads(users_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LoungeError(f"lounge user file unreadable: {exc}") from exc
    networks = data.get("networks")
    if not isinstance(networks, list):
        networks = []
    entry = {
        "name": net_name,
        "host": host,
        "port": int(port),
        "tls": False,
        "rejectUnauthorized": False,
        "nick": nick,
        "username": nick,
        "realname": nick,
        "password": server_password,
        "sasl": "",
        "saslAccount": "",
        "saslPassword": "",
        "leaveMessage": "",
        "awayMessage": "",
        "userDisconnected": False,
        "commands": [],
        "ignoreList": [],
        "proxyEnabled": False,
        "proxyHost": "",
        "proxyPort": 1080,
        "proxyUsername": "",
        "proxyPassword": "",
        "channels": [{"name": channel, "muted": False, "key": ""}],
        "uuid": _uuid.uuid4().hex,
    }
    kept = [n for n in networks
            if not (isinstance(n, dict) and n.get("name") == net_name)]
    kept.append(entry)
    data["networks"] = kept
    try:
        users_file.write_text(json.dumps(data, indent=2) + "\n",
                              encoding="utf-8")
    except Exception as exc:
        raise LoungeError(f"lounge network seed failed: {exc}") from exc
    return {"action": "seeded", "network": net_name, "channel": channel}


def reset_lounge_password(paths: LoungePaths, username: str,
                          password: str) -> dict:
    """Reset a Lounge LOGIN password non-interactively.

    ``thelounge reset --password`` with THELOUNGE_HOME pointed at our
    home (verified against the real CLI). Raises LoungeError on
    failure so the wizard surfaces it instead of silently stranding
    the user at the login page.
    """
    if not password:
        raise LoungeError("refusing to reset to an empty password")
    users_file = paths.home / "users" / f"{username}.json"
    if not users_file.is_file():
        raise LoungeError(f"lounge user {username!r} does not exist")
    out = _run(
        [str(lounge_bin()), "reset", "--password", password, username],
        extra_env={"THELOUNGE_HOME": str(paths.home)}, timeout=60)
    if out.returncode != 0:
        raise LoungeError(
            "thelounge reset failed: "
            f"{(out.stderr or out.stdout).strip()}")
    if username not in lounge_users(paths):
        raise LoungeError(
            f"thelounge reset did not stick for {username!r}")
    return {"action": "reset", "user": username}


def provision_lounge(
    mercury_home: str | Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = LOUNGE_PORT_DEFAULT,
    username: str = "owner",
    password: Optional[str] = None,
    hermes_root: str | Path | None = None,
    uplink_host: str = "127.0.0.1",
    uplink_port: int = 6670,
    uplink_password: str = "",
    uplink_name: str = "",
    uplink_nick: str = "",
    uplink_channel: str = "",
) -> dict:
    """Full Lounge layer: node → binary → config → unit → user → network.

    ``host`` is the WEB UI bind (127.0.0.1 or the tailnet IP). The
    ``uplink_*`` fields pre-seed this mercury server as a Lounge
    network (same-box localhost uplink): after one setup the gateway
    channel is already in the browser, no manual add-network step.
    The unit restarts last so a running Lounge picks up the seed.
    """
    from observatory.provision import _mercury_home  # local import: no cycle

    _ = hermes_root
    home = _mercury_home(mercury_home)
    summary: dict = {"node": str(ensure_node())}
    summary["bin"] = str(ensure_lounge_installed())
    spaths = LoungePaths(home)
    summary["config"] = ensure_lounge_config(spaths, host=host, port=int(port))
    summary["unit"] = ensure_lounge_unit(
        spaths,
        unit=render_lounge_unit(
            lounge_bin=summary["bin"], home=str(spaths.home)))
    summary["user"] = ensure_lounge_user(spaths, username, password)
    if uplink_name and uplink_channel:
        summary["network"] = ensure_lounge_network(
            spaths, username,
            net_name=uplink_name, host=uplink_host, port=int(uplink_port),
            server_password=uplink_password,
            nick=uplink_nick or username, channel=uplink_channel)
        if lounge_unit_active():
            restart_lounge()
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
