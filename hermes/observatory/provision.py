"""Idempotent IRC observatory provisioner (replaces the tuwunel stack).

Provisions, in order: ``ircd.json`` config → bouncer/agent passwords
(mirrored in ``$MERCURY_HOME/.env``) → gateway state row → systemd
user unit. Fail-hard like every other provision step; never touches
the network (no downloads — the daemon is stdlib-only).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from observatory.config_gen import (
    HISTORY_LIMIT_DEFAULT,
    IRCD_ADDRESS,
    IRCD_AGENT_PORT_DEFAULT,
    IRCD_BOUNCER_PORT_DEFAULT,
    IRCD_TLS_PORT_DEFAULT,
    OBSERVATORY_UNIT_NAME,
    SERVER_NAME_DEFAULT,
    ObservatoryPaths,
    new_secret,
    render_observatory_unit,
)

logger = logging.getLogger(__name__)


class ProvisionError(RuntimeError):
    pass


#: Gateway node identity (mirrors the old sidecar seam so state.db readers
#: never fork the gateway row).
GATEWAY_NODE_ID = "gw"
GATEWAY_NODE_NAME = "gateway agent"

#: state.db meta key projecting the live server name into shared state.
SERVER_NAME_META_KEY = "server_name"

#: $MERCURY_HOME/.env keys for the IRC listeners (0600; the setup card
#: points here — NEVER printed to the terminal).
ENV_BOUNCER_PASSWORD = "IRC_BOUNCER_PASSWORD"
ENV_AGENT_PASSWORD = "IRC_AGENT_PASSWORD"

#: Strength floor for user-chosen bouncer passwords (generated ones carry
#: ~192 bits and bypass this).
BOUNCER_PASSWORD_MIN_LENGTH = 8

_SERVER_NAME_RE = r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?"


def _mercury_home(mercury_home: str | Path | None = None) -> Path:
    """Resolve the Mercury home: explicit arg > $MERCURY_HOME > the engine's
    profile-aware home (get_hermes_home(); ``…/hermes`` → its parent, the
    same derivation mercury_cli/gateway.py uses for the env trio) >
    ~/.mercury."""
    if mercury_home:
        return Path(mercury_home).expanduser()
    env = os.environ.get("MERCURY_HOME")
    if env:
        return Path(env).expanduser()
    try:
        from mercury_constants import get_hermes_home

        hermes_home = Path(get_hermes_home()).expanduser()
        if hermes_home.name == "hermes":
            return hermes_home.parent
        return hermes_home
    except Exception:
        return Path.home() / ".mercury"


# --- validation ------------------------------------------------------------


def validate_server_name(value: str) -> str:
    """IRC network label: lowercase letters/digits/_/-, normalized."""
    clean = str(value or "").strip().lower()
    if not clean or not re.fullmatch(_SERVER_NAME_RE, clean):
        raise ValueError(
            f"invalid server name {value!r} — use lowercase letters, "
            "digits, _ or - (e.g. 'mercury')"
        )
    return clean


def validate_bouncer_password(value: str) -> str:
    clean = str(value or "")
    if len(clean) < BOUNCER_PASSWORD_MIN_LENGTH:
        raise ValueError(
            "bouncer password must be at least "
            f"{BOUNCER_PASSWORD_MIN_LENGTH} characters"
        )
    return clean


def generate_password(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


# --- .env mirror -------------------------------------------------------------


def _quote_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+-]+", value or ""):
        return value
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _env_line_defines_key(line: str, key: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    return stripped == key or stripped.startswith(key + "=")


def _upsert_env_key(env_path: Path, key: str, value: str) -> None:
    try:
        lines = (
            env_path.read_text(encoding="utf-8").splitlines()
            if env_path.is_file()
            else []
        )
    except Exception:
        lines = []
    entry = f"{key}={_quote_env_value(value)}"
    replaced = False
    out: list[str] = []
    for line in lines:
        if _env_line_defines_key(line, key):
            if not replaced:
                out.append(entry)
                replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(entry)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except Exception:
        pass


def mirror_irc_env(
    mercury_home: str | Path | None, bouncer_password: str, agent_password: str
) -> Path:
    """Mirror both listener passwords into $MERCURY_HOME/.env (0600)."""
    env_path = _mercury_home(mercury_home) / ".env"
    _upsert_env_key(env_path, ENV_BOUNCER_PASSWORD, bouncer_password)
    _upsert_env_key(env_path, ENV_AGENT_PASSWORD, agent_password)
    return env_path


def read_irc_passwords(mercury_home: str | Path | None = None) -> dict[str, str]:
    """Passwords from env/.env (never generated here — provisioning owns that)."""
    try:
        from mercury_cli.config import get_env_value

        bouncer = str(get_env_value(ENV_BOUNCER_PASSWORD) or "")
        agent = str(get_env_value(ENV_AGENT_PASSWORD) or "")
    except Exception:
        bouncer = os.environ.get(ENV_BOUNCER_PASSWORD, "")
        agent = os.environ.get(ENV_AGENT_PASSWORD, "")
    return {"bouncer": bouncer, "agent": agent}


# --- config ------------------------------------------------------------------


def default_config(*, server_name: str = SERVER_NAME_DEFAULT) -> dict[str, Any]:
    return {
        "server_name": server_name,
        "agent_host": IRCD_ADDRESS,
        "agent_port": IRCD_AGENT_PORT_DEFAULT,
        "bouncer_host": IRCD_ADDRESS,
        "bouncer_port": IRCD_BOUNCER_PORT_DEFAULT,
        "tls_port": IRCD_TLS_PORT_DEFAULT,
        "history_limit": HISTORY_LIMIT_DEFAULT,
    }


def read_config(mercury_home: str | Path | None = None) -> dict[str, Any] | None:
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    try:
        if not paths.config_file.is_file():
            return None
        return json.loads(paths.config_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def ensure_config(
    paths: ObservatoryPaths,
    *,
    server_name: str | None = None,
    agent_host: str | None = None,
    agent_port: int | None = None,
    bouncer_host: str | None = None,
    bouncer_port: int | None = None,
    tls_port: int | None = None,
    history_limit: int | None = None,
) -> dict[str, Any]:
    """Idempotent ircd.json: stored values win unless explicitly passed
    (explicit disagreement with a STORED value fails hard — never
    silently re-pin a live network under running agents). Fresh installs
    (no readable config yet — missing, empty, or corrupt file) accept
    explicit values outright: there is nothing live to protect, and the
    defaults must never masquerade as stored values in the error."""
    current = read_config(paths.root.parent)
    had_config = isinstance(current, dict)
    cfg = default_config() if not had_config else dict(current)
    explicit = {
        "server_name": (
            validate_server_name(server_name) if server_name is not None else None
        ),
        "agent_host": agent_host,
        "agent_port": agent_port,
        "bouncer_host": bouncer_host,
        "bouncer_port": bouncer_port,
        "tls_port": tls_port,
        "history_limit": history_limit,
    }
    changed: list[str] = []
    for key, value in explicit.items():
        if value is None:
            cfg.setdefault(key, default_config()[key])
            continue
        if had_config and key in cfg and cfg[key] != value:
            raise ProvisionError(
                f"observatory {key} is {cfg[key]!r} in {paths.config_file} "
                f"but {value!r} was requested — hand-edit the file (and "
                "restart the unit) instead of re-provisioning under live agents"
            )
        if cfg.get(key) != value:
            changed.append(key)
        cfg[key] = value
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    action = (
        "wrote"
        if not isinstance(current, dict)
        else ("updated" if changed else "current")
    )
    return {"action": action, "path": str(paths.config_file), "config": cfg}


def ensure_passwords(mercury_home: str | Path | None = None) -> dict[str, Any]:
    """Generate missing listener passwords and mirror them into .env."""
    have = read_irc_passwords(mercury_home)
    made: list[str] = []
    if not have["bouncer"]:
        have["bouncer"] = generate_password()
        made.append("bouncer")
    if not have["agent"]:
        have["agent"] = generate_password()
        made.append("agent")
    if made:
        mirror_irc_env(mercury_home, have["bouncer"], have["agent"])
    return {"action": "generated" if made else "current", "made": made}


#: Env var carrying a user-chosen bouncer password into provisioning
#: (install.sh passes the environment through; never an argv flag —
#: secrets stay out of ps output). Validated like a wizard-typed one.
ENV_CHOSEN_BOUNCER_PASSWORD = "OBSERVATORY_BOUNCER_PASSWORD"


def set_bouncer_password(
    mercury_home: str | Path | None, password: str
) -> dict[str, Any]:
    """Set the bouncer password to a chosen value (min 8 chars).

    The agent password is kept as-is (generated when missing); both are
    mirrored to .env. The caller MUST restart the daemon afterwards — a
    live daemon keeps the old password in memory until then, which is
    exactly the ".env says X, daemon rejects X" desync.
    """
    clean = validate_bouncer_password(password)
    have = read_irc_passwords(mercury_home)
    agent = have.get("agent") or generate_password()
    mirror_irc_env(mercury_home, clean, agent)
    return {"action": "set", "agent": "kept" if have.get("agent") else "generated"}

# --- TLS certificate -----------------------------------------------------------


def ensure_tls_cert(mercury_home: str | Path | None = None) -> dict[str, Any]:
    """Idempotent self-signed CA + server cert for the TLS bouncer.

    Strict clients (Goguma-style: TLS default, no plaintext toggle) need
    TLS even on a tailnet. The CA is generated once and kept (clients
    trust it once); the server cert covers the network label,
    localhost, and the current tailnet hostnames/IPs. Regeneration only
    happens on explicit reset (which deletes ``tls/``). Returns
    ``{"action": "generated"|"current", "sans": [...]}``. Never raises
    for a missing ``cryptography`` install — TLS just stays unavailable.
    """
    from observatory.config_gen import SERVER_NAME_DEFAULT as _default_name

    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    if (paths.tls_ca.is_file() and paths.tls_cert.is_file()
            and paths.tls_key.is_file()):
        return {"action": "current", "sans": []}
    try:
        import datetime as _dt

        from cryptography import x509 as _x509
        from cryptography.hazmat.primitives import hashes as _hashes
        from cryptography.hazmat.primitives import serialization as _ser
        from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
        from cryptography.x509.oid import NameOID as _oid
    except Exception as exc:
        logger.debug("observatory: TLS unavailable (%s)", exc)
        return {"action": "unavailable", "sans": []}
    cfg = read_config(home) or {}
    server_name = str(cfg.get("server_name") or _default_name)
    sans: list[str] = [server_name, "localhost"]
    try:
        ts = detect_tailscale()
        for cand in (ts.get("dns_name"), ts.get("ip")):
            if isinstance(cand, str) and cand.strip() and cand.strip() not in sans:
                sans.append(cand.strip())
    except Exception:
        pass
    now = _dt.datetime.now(_dt.timezone.utc)
    expiry = now + _dt.timedelta(days=825)
    ca_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _x509.Name([_x509.NameAttribute(_oid.COMMON_NAME, f"{server_name} observatory CA")])
    ca_cert = (
        _x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(_x509.random_serial_number())
        .not_valid_before(now).not_valid_after(expiry)
        .add_extension(_x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            _x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False)
        .add_extension(
            _x509.KeyUsage(digital_signature=False, content_commitment=False,
                           key_encipherment=False, data_encipherment=False,
                           key_agreement=False, key_cert_sign=True, crl_sign=True,
                           encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, _hashes.SHA256())
    )
    srv_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san_list: list = []
    for name in sans:
        try:
            import ipaddress as _ip

            san_list.append(_x509.IPAddress(_ip.ip_address(name)))
        except ValueError:
            san_list.append(_x509.DNSName(name))
    srv_cert = (
        _x509.CertificateBuilder()
        .subject_name(_x509.Name([_x509.NameAttribute(_oid.COMMON_NAME, server_name)]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key()).serial_number(_x509.random_serial_number())
        .not_valid_before(now).not_valid_after(expiry)
        .add_extension(_x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(
            _x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()),
            critical=False)
        .add_extension(
            _x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False)
        .add_extension(
            _x509.KeyUsage(digital_signature=True, content_commitment=False,
                           key_encipherment=True, data_encipherment=False,
                           key_agreement=False, key_cert_sign=False, crl_sign=False,
                           encipher_only=False, decipher_only=False), critical=True)
        .add_extension(
            _x509.ExtendedKeyUsage([_x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False)
        .sign(ca_key, _hashes.SHA256())
    )
    try:
        paths.tls_dir.mkdir(parents=True, exist_ok=True)
        paths.tls_ca.write_bytes(ca_cert.public_bytes(_ser.Encoding.PEM))
        paths.tls_cert.write_bytes(srv_cert.public_bytes(_ser.Encoding.PEM))
        paths.tls_key.write_bytes(srv_key.private_bytes(
            _ser.Encoding.PEM, _ser.PrivateFormat.TraditionalOpenSSL,
            _ser.NoEncryption()))
        for p in (paths.tls_ca, paths.tls_cert, paths.tls_key):
            try:
                p.chmod(0o600)
            except Exception:
                pass
    except Exception as exc:
        raise ProvisionError(f"TLS cert write failed: {exc}") from exc
    return {"action": "generated", "sans": sans}


# --- gateway row ---------------------------------------------------------------


def ensure_gateway_node_in_state(state: Any, *, server_name: str) -> str:
    """Idempotent gateway row + server_name meta projection (shared-state
    seam for provision/boot/gateway paths). Returns the gateway nick."""
    from observatory.rooms import agent_nick, gateway_channel

    clean = validate_server_name(server_name)
    nick = agent_nick(f"{clean}_gateway")
    channel = gateway_channel(clean)
    try:
        row = state.get(GATEWAY_NODE_ID)
        updates: dict[str, Any] = {}
        if str((row or {}).get("mxid") or "") != nick:
            updates["mxid"] = nick
        if str((row or {}).get("room_id") or "") != channel:
            try:
                state.set_room_id(GATEWAY_NODE_ID, channel)
            except Exception:
                pass
        if updates:
            try:
                with state.locked() as db:
                    with db:
                        for col, val in updates.items():
                            db.execute(
                                f"UPDATE nodes SET {col} = ? WHERE node_id = ?",
                                (val, GATEWAY_NODE_ID),
                            )
            except Exception:
                pass
        try:
            state.set_meta(SERVER_NAME_META_KEY, clean)
        except Exception:
            pass
        return nick
    except Exception as exc:
        from observatory.state import StateError

        if not isinstance(exc, StateError):
            raise
        state.add_node(
            GATEWAY_NODE_ID,
            engine="hermes",
            name=GATEWAY_NODE_NAME,
            slug="gateway",
            mxid=nick,
            session_ref="session:gateway",
            parent_node_id=None,
            extra={"kind": "gateway"},
        )
        try:
            state.set_room_id(GATEWAY_NODE_ID, channel)
        except Exception:
            pass
        try:
            state.set_meta(SERVER_NAME_META_KEY, clean)
        except Exception:
            pass
        return nick


def live_server_name(mercury_home: str | Path | None = None) -> str | None:
    """Live network label from ircd.json, or None when unprovisioned."""
    cfg = read_config(mercury_home)
    if not isinstance(cfg, dict):
        return None
    name = str(cfg.get("server_name") or "").strip()
    return name or None


# --- systemd -------------------------------------------------------------------


def _systemctl_available() -> bool:
    try:
        return shutil.which("systemctl") is not None
    except Exception:
        return False


def _run_systemctl(
    args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


def _unit_is_active(unit_name: str) -> bool:
    try:
        proc = _run_systemctl(["is-active", unit_name], check=False)
        return (proc.stdout or "").strip() == "active"
    except Exception:
        return False


def _gateway_python_bin() -> str:
    import sys

    return sys.executable


def ensure_observatory_unit(
    mercury_home: str | Path | None = None,
    *,
    python_bin: str | None = None,
    hermes_root: str | None = None,
) -> str:
    """Install/enable/start the ircd unit. Never raises for missing
    systemd (containers/CI) — returns "skipped"."""
    if not _systemctl_available():
        return "skipped"
    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    if hermes_root is None:
        hermes_root = str(Path(__file__).resolve().parent.parent)
    unit_text = render_observatory_unit(
        python_bin=python_bin or _gateway_python_bin(),
        hermes_root=hermes_root,
        mercury_home=str(home),
        log_dir=str(paths.logs_dir),
    )
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / OBSERVATORY_UNIT_NAME).write_text(unit_text, encoding="utf-8")
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        _run_systemctl(["daemon-reload"], check=False)
        _run_systemctl(["enable", OBSERVATORY_UNIT_NAME], check=False)
        proc = _run_systemctl(["restart", OBSERVATORY_UNIT_NAME], check=False)
        if proc.returncode != 0:
            return f"installed (start failed: {(proc.stderr or '').strip()[:200]})"
        return "installed"
    except Exception as exc:
        return f"installed (start skipped: {exc})"


#: Back-compat alias for setup paths importing the old name.
ensure_sidecar_unit = ensure_observatory_unit


def unit_status() -> str:
    if not _systemctl_available():
        return "no-systemd"
    try:
        proc = _run_systemctl(["is-active", OBSERVATORY_UNIT_NAME], check=False)
        state = (proc.stdout or "").strip()
        return state or "unknown"
    except Exception:
        return "unknown"


# --- tailscale -------------------------------------------------------------------


def detect_tailscale() -> dict:
    """Best-effort Tailscale tailnet detection for the wizard.

    Detect-and-assist only: ``shutil.which('tailscale')`` + read-only
    ``tailscale status`` / ``tailscale ip -4`` / ``tailscale status --json``
    probes. NEVER installs, NEVER authenticates, NEVER raises — every
    failure degrades to ``up=False``. Shape: ``{"available", "up", "ip",
    "dns_name"}`` where ``ip`` is the tailnet IPv4 and ``dns_name`` the
    MagicDNS name (trailing dot stripped).
    """
    down = {"available": False, "up": False, "ip": None, "dns_name": None}
    try:
        if shutil.which("tailscale") is None:
            return dict(down)
    except Exception:  # noqa: BLE001 — display probe, never raises
        return dict(down)
    found: dict = {"available": True, "up": False, "ip": None, "dns_name": None}
    try:
        proc = subprocess.run(
            ["tailscale", "status"], capture_output=True, text=True, timeout=10
        )
    except Exception:  # noqa: BLE001
        return found
    if proc.returncode != 0:
        return found
    found["up"] = True
    try:
        ip_proc = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10
        )
        if ip_proc.returncode == 0:
            for line in (ip_proc.stdout or "").splitlines():
                cand = line.strip()
                if cand:
                    found["ip"] = cand
                    break
    except Exception:  # noqa: BLE001
        pass
    try:
        js_proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if js_proc.returncode == 0 and js_proc.stdout:
            data = json.loads(js_proc.stdout)
            self_node = data.get("Self") if isinstance(data, dict) else None
            if isinstance(self_node, dict):
                dns = self_node.get("DNSName")
                if isinstance(dns, str) and dns.strip():
                    found["dns_name"] = dns.strip().rstrip(".")
                if found["ip"] is None:
                    ips = self_node.get("TailscaleIPs")
                    if isinstance(ips, list):
                        for cand_ip in ips:
                            text = str(cand_ip).strip()
                            if text and ":" not in text:
                                found["ip"] = text
                                break
    except Exception:  # noqa: BLE001
        pass
    return found


def current_listen_addresses(mercury_home: str | Path | None = None) -> list[str]:
    """Configured [agent_host, bouncer_host] (deduped, for the bind trap check)."""
    cfg = read_config(mercury_home) or {}
    addrs = [
        str(cfg.get("agent_host") or IRCD_ADDRESS),
        str(cfg.get("bouncer_host") or IRCD_ADDRESS),
    ]
    out: list[str] = []
    for addr in addrs:
        if addr not in out:
            out.append(addr)
    return out


def set_ircd_bind(
    ip: str, mercury_home: str | Path | None = None, *, listener: str = "bouncer"
) -> str:
    """Pin one listener to the tailnet IP (localhost retained on the
    other listener by default — pass listener="agent" to pin the agent
    side instead, or "both"). Never starts/stops the daemon: restart
    the unit for the new bind to take effect. Refuses loopback."""
    if not isinstance(ip, str) or not ip.strip():
        raise ProvisionError("set_ircd_bind needs a non-empty tailnet IP")
    target = ip.strip()
    try:
        import ipaddress as _ipaddress

        _ipaddress.ip_address(target)
    except Exception as exc:
        raise ProvisionError(
            f"refusing to bind to {target!r}: not an IP address ({exc})"
        ) from exc
    if target in ("127.0.0.1", "::1", "localhost"):
        raise ProvisionError(
            f"refusing to bind loopback {target!r} — localhost is already bound"
        )
    if listener not in ("bouncer", "agent", "both"):
        raise ProvisionError(f"listener must be bouncer|agent|both, got {listener!r}")
    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    cfg = read_config(home)
    if not isinstance(cfg, dict):
        raise ProvisionError(
            f"observatory not provisioned (ircd.json missing at {paths.config_file}) — "
            "run 'mercury setup observatory' install first"
        )
    if listener in ("bouncer", "both"):
        cfg["bouncer_host"] = target
    if listener in ("agent", "both"):
        cfg["agent_host"] = target
    paths.config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return target


# --- top-level flows ---------------------------------------------------------------


def provision(
    mercury_home: str | Path | None = None,
    *,
    server_name: str | None = None,
    agent_host: str | None = None,
    agent_port: int | None = None,
    bouncer_host: str | None = None,
    bouncer_port: int | None = None,
    history_limit: int | None = None,
    systemd: bool = True,
) -> dict:
    """Run every provisioning step (config → passwords → TLS cert →
    gateway row → unit). Returns a summary dict; raises ProvisionError
    on failure."""
    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    summary = {
        "config": ensure_config(
            paths,
            server_name=server_name,
            agent_host=agent_host,
            agent_port=agent_port,
            bouncer_host=bouncer_host,
            bouncer_port=bouncer_port,
            history_limit=history_limit,
        ),
        "passwords": ensure_passwords(home),
        "tls": ensure_tls_cert(home),
    }
    chosen = os.environ.get(ENV_CHOSEN_BOUNCER_PASSWORD)
    if chosen:
        # Explicit choice wins over generated/current (install.sh
        # passes the env through; the wizard has its own prompt).
        try:
            summary["chosen_password"] = set_bouncer_password(home, chosen)
        except ValueError as exc:
            raise ProvisionError(
                f"{ENV_CHOSEN_BOUNCER_PASSWORD} invalid: {exc}"
            ) from exc
    live = live_server_name(home)
    if not live:
        raise ProvisionError("ircd.json pins no server_name (unprovisioned identity)")
    from observatory.state import ObservatoryState, default_state_db_path

    gw_state = ObservatoryState(default_state_db_path(home))
    try:
        summary["gateway"] = ensure_gateway_node_in_state(gw_state, server_name=live)
    finally:
        try:
            gw_state.close()
        except Exception:
            pass
    summary["unit"] = (
        ensure_observatory_unit(home) if systemd else "skipped (--no-systemd)"
    )
    return summary


def provision_in_wizard(mercury_home: str | Path | None = None, **kwargs: Any) -> dict:
    """Wizard entry: :func:`provision` with wizard-friendly errors."""
    return provision(mercury_home, **kwargs)


def verify_and_converge_gateway(mercury_home: str | Path | None = None) -> str:
    """Ensure the gateway row exists; return its channel (never raises)."""
    try:
        live = live_server_name(mercury_home) or SERVER_NAME_DEFAULT
        from observatory.state import ObservatoryState, default_state_db_path

        home = _mercury_home(mercury_home)
        state = ObservatoryState(default_state_db_path(home))
        try:
            ensure_gateway_node_in_state(state, server_name=live)
        finally:
            try:
                state.close()
            except Exception:
                pass
        from observatory.rooms import gateway_channel

        return f"converged-gateway ({gateway_channel(live)})"
    except Exception as exc:
        return f"skipped-error ({exc})"


def _safe_port(value: Any, default: int) -> int:
    """int(value) or default — status surfaces never raise on bad config."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return default


def status_summary(mercury_home: str | Path | None = None) -> dict:
    """Machine-readable observatory status for setup/status surfaces."""
    home = _mercury_home(mercury_home)
    cfg = read_config(home)
    passwords = read_irc_passwords(home)
    enabled = True
    try:
        from mercury_cli.config import cfg_get, load_config

        enabled = bool(cfg_get(load_config(), "observatory", "enabled", default=True))
    except Exception:
        pass
    return {
        "enabled": enabled,
        "provisioned": isinstance(cfg, dict),
        "bouncer": f"{(cfg or {}).get('bouncer_host', IRCD_ADDRESS)}:"
        f"{(cfg or {}).get('bouncer_port', IRCD_BOUNCER_PORT_DEFAULT)}",
        "tls_port": _safe_port((cfg or {}).get("tls_port"), IRCD_TLS_PORT_DEFAULT),
        "tls_ready": bool(
            (ObservatoryPaths(home).tls_cert.is_file())
            and (ObservatoryPaths(home).tls_key.is_file())),
        "bouncer_password_set": bool(passwords["bouncer"]),
        "agent_password_set": bool(passwords["agent"]),
        "config_path": str(ObservatoryPaths(home).config_file),
    }


def reset_observatory_data(mercury_home: str | Path | None = None) -> list[str]:
    """Delete IRC observatory data (config + history + agent tree + soju
    backlog). The unit files survive (re-provision rewrites config;
    callers must restart both daemons — live memory outlives the files).
    Returns what was removed."""
    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    removed: list[str] = []
    soju_db = paths.root / "soju.db"
    soju_admin = paths.root / "soju-admin"
    for target in (
        paths.config_file,
        paths.history_db,
        paths.root / "state.db",
        paths.root / "state.db-wal",
        paths.root / "state.db-shm",
        paths.root / "omp-sessions",
        paths.tls_dir,
        soju_db,
        soju_admin,
    ):
        try:
            if target.is_dir() and not target.is_symlink():
                import shutil as _shutil

                _shutil.rmtree(target)
                removed.append(str(target))
            elif target.exists() or target.is_symlink():
                target.unlink()
                removed.append(str(target))
        except Exception:
            pass
    return removed


def observatory_enabled(config: Any = None) -> bool:
    """Default ON; ``observatory.enabled: false`` freezes the rooms."""
    try:
        if isinstance(config, dict):
            obs = config.get("observatory")
            if isinstance(obs, dict) and "enabled" in obs:
                return bool(obs.get("enabled"))
        from mercury_cli.config import cfg_get, load_config

        return bool(cfg_get(load_config(), "observatory", "enabled", default=True))
    except Exception:
        return True


def provision_if_missing(mercury_home: str | Path | None = None) -> bool | None:
    """First-time provision for installs predating the observatory.

    Returns True when it provisioned now, None when already provisioned
    or disabled. Raises ProvisionError on failure (callers warn, never
    block the update).
    """
    try:
        if not observatory_enabled():
            return None
    except Exception:
        pass
    home = _mercury_home(mercury_home)
    if read_config(home) is not None:
        return None
    provision(home)
    return True


def refresh_for_update(mercury_home: str | Path | None = None) -> str:
    """Update-tail refresh: keep config + unit current (stdlib daemon —
    nothing to download). Returns the receipt line ("" when disabled)."""
    try:
        if not observatory_enabled():
            return ""
    except Exception:
        pass
    home = _mercury_home(mercury_home)
    if read_config(home) is None:
        return ""
    paths = ObservatoryPaths(home)
    ensure_config(paths)
    unit = ensure_observatory_unit(home)
    return f"observatory current (unit {unit})"


def main(argv: list[str] | None = None) -> int:
    """``python -m observatory.provision`` (install.sh entry point)."""
    import argparse

    parser = argparse.ArgumentParser(description="Provision the IRC observatory")
    parser.add_argument("--server-name", default=None)
    parser.add_argument("--agent-host", default=None)
    parser.add_argument("--agent-port", type=int, default=None)
    parser.add_argument("--bouncer-host", default=None)
    parser.add_argument("--bouncer-port", type=int, default=None)
    parser.add_argument("--no-systemd", action="store_true")
    parser.add_argument("--mercury-home", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        summary = provision(
            args.mercury_home,
            server_name=args.server_name,
            agent_host=args.agent_host,
            agent_port=args.agent_port,
            bouncer_host=args.bouncer_host,
            bouncer_port=args.bouncer_port,
            systemd=not args.no_systemd,
        )
    except ProvisionError as exc:
        print(f"observatory provisioning failed: {exc}")
        return 1
    print(f"observatory ready: {summary['config']['path']}")
    return 0


def observatory_data_present(mercury_home: str | Path | None = None) -> bool:
    """True when any IRC observatory data exists under the home."""
    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    candidates = (paths.config_file, paths.history_db,
                  paths.root / "state.db", paths.root / "omp-sessions")
    try:
        return any(p.exists() for p in candidates)
    except Exception:
        return False


def wipe_observatory_data(mercury_home: str | Path | None = None,
                          *, mode: str = "annihilate") -> dict:
    """Archive (timestamped move aside) or annihilate (delete) the
    observatory data root. The unit file is handled separately
    (:func:`_stop_and_remove_units`). Returns
    ``{"moved"|"deleted": [...], "units_removed": []}``."""
    import datetime as _dt
    import shutil as _shutil

    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    summary: dict = {"units_removed": []}
    if mode == "archive":
        moved: list[str] = []
        if paths.root.exists():
            dest = paths.root.parent / (
                f"observatory.archive.{_dt.datetime.now().strftime('%Y%m%dT%H%M%S')}")
            try:
                _shutil.move(str(paths.root), str(dest))
                moved.append(str(dest))
            except Exception as exc:
                raise ProvisionError(f"archive failed: {exc}") from exc
        summary["moved"] = moved
        return summary
    if mode != "annihilate":
        raise ProvisionError(f"unknown wipe mode {mode!r} (archive|annihilate)")
    deleted: list[str] = []
    for target in (paths.root,):
        try:
            if target.is_dir() and not target.is_symlink():
                _shutil.rmtree(target)
                deleted.append(str(target))
            elif target.exists() or target.is_symlink():
                target.unlink()
                deleted.append(str(target))
        except Exception as exc:
            raise ProvisionError(f"annihilate failed: {exc}") from exc
    summary["deleted"] = deleted
    return summary


def _stop_and_remove_units() -> list[str]:
    """Stop + disable + delete the observatory unit file. Never raises."""
    removed: list[str] = []
    if not _systemctl_available():
        return removed
    try:
        _run_systemctl(["stop", OBSERVATORY_UNIT_NAME], check=False)
        _run_systemctl(["disable", OBSERVATORY_UNIT_NAME], check=False)
    except Exception:
        pass
    try:
        unit_file = Path.home() / ".config" / "systemd" / "user" / OBSERVATORY_UNIT_NAME
        if unit_file.is_file():
            unit_file.unlink()
            removed.append(OBSERVATORY_UNIT_NAME)
        _run_systemctl(["daemon-reload"], check=False)
    except Exception:
        pass
    return removed


def _kill_stray_ircd() -> list[int]:
    """SIGTERM stray ircd processes (daemon started outside the unit).
    Never raises; returns killed PIDs."""
    killed: list[int] = []
    try:
        proc = subprocess.run(["pgrep", "-f", "observatory.ircd"],
                              capture_output=True, text=True, timeout=10)
    except Exception:
        return killed
    if proc.returncode != 0:
        return killed
    import os as _os
    import signal as _signal
    for line in (proc.stdout or "").splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == _os.getpid():
            continue
        try:
            _os.kill(pid, _signal.SIGTERM)
            killed.append(pid)
        except Exception:
            pass
    return killed


#: Back-compat alias (uninstall paths importing the old tuwunel-era name).
_kill_stray_tuwunel = _kill_stray_ircd


if __name__ == "__main__":
    raise SystemExit(main())
