"""Pure generators for IRC observatory provisioning artifacts.

The observatory is a small stdlib asyncio IRC network (``ircd``): one
agent listener for the gateway/agents, one server listener for the
user's IRC client, on the same channel state with SQLite-backed
history replay. No homeserver, no crypto stack, no appservice.
"""

from __future__ import annotations

import secrets
from pathlib import Path

#: IRC network name. Non-federating, so this is a label, not a DNS
#: promise; shown in numerics and the gateway channel name.
SERVER_NAME_DEFAULT = "mercury"

#: Localhost only by default. Phones/clients arrive over Tailscale/VPN
#: (provision can pin the server listener to the tailnet IP).
IRCD_ADDRESS = "127.0.0.1"
#: Distinct from common IRC defaults (6667) so a pre-existing ircd on
#: the box is never collided with.
IRCD_AGENT_PORT_DEFAULT = 6669
IRCD_SERVER_PORT_DEFAULT = 6670
#: Pre-rename alias (old installs import this name).
IRCD_BOUNCER_PORT_DEFAULT = IRCD_SERVER_PORT_DEFAULT
#: TLS server for strict clients (Goguma-style): same rooms, IRCv3 + TLS.
IRCD_TLS_PORT_DEFAULT = 6697

#: History replay depth for the server listener.
HISTORY_LIMIT_DEFAULT = 200

#: The ONE systemd user unit (provision installs it; never provision()
#: itself — the gateway boots provision(), so auto-installing there
#: would restart its own unit mid-boot).
OBSERVATORY_UNIT_NAME = "mercury-observatory.service"
#: Back-compat alias (setup/setup-repair paths import this name).
SIDECAR_UNIT_NAME = OBSERVATORY_UNIT_NAME
OBSERVATORY_UNIT_DESCRIPTION = "Mercury Observatory IRC network (ircd)"

#: Gateway agent nick base: ``<server>_gateway`` (rooms.agent_nick).
GATEWAY_NICK_SUFFIX = "_gateway"

# Directory layout under $MERCURY_HOME/observatory
DIR_LOGS = "logs"
DIR_TLS = "tls"
FILE_CONFIG = "ircd.json"
FILE_HISTORY_DB = "irc-history.db"
FILE_TLS_CA = "ca.crt"
FILE_TLS_CERT = "server.crt"
FILE_TLS_KEY = "server.key"


def new_secret(nbytes: int = 32) -> str:
    """URL-safe random secret (server/agent/oper passwords)."""
    return secrets.token_urlsafe(nbytes)


def render_observatory_unit(
    *,
    python_bin: str,
    hermes_root: str,
    mercury_home: str,
    log_dir: str,
    description: str = OBSERVATORY_UNIT_DESCRIPTION,
    mercury_config: str | None = None,
    venv_dir: str | None = None,
    sane_path: str | None = None,
) -> str:
    """Render the ircd systemd USER unit (pure string templating, no I/O).

    The unit passes only ``--state-dir``: the daemon reads hosts, ports,
    and limits live from ``ircd.json`` on every start, so bind edits
    take effect on plain restart — the unit never goes stale.

    Env mirrors ``mercury-gateway.service``: the daemon MUST see the
    unified ``MERCURY_CONFIG`` so paths resolve identically inside and
    outside a login shell. Passwords ride the environment file
    (``EnvironmentFile=``), never the ExecStart line (ps-visible).
    """
    if mercury_config is None:
        mercury_config = f"{mercury_home}/config.yaml"
    if venv_dir is None:
        _pb = Path(python_bin)
        venv_dir = str(_pb.parent.parent if _pb.parent.name == "bin" else _pb.parent)
    if sane_path is None:
        sane_path = (
            f"{venv_dir}/bin:/usr/local/sbin:/usr/local/bin"
            ":/usr/sbin:/usr/bin:/sbin:/bin"
        )
    state_dir = f"{mercury_home}/observatory"
    env_file = f"{mercury_home}/.env"
    return f"""\
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=MERCURY_HOME={mercury_home}
Environment=MERCURY_CONFIG={mercury_config}
Environment=PYTHONPATH={hermes_root}
Environment=PATH={sane_path}
EnvironmentFile=-{env_file}
ExecStart={python_bin} -m observatory.ircd --state-dir {state_dir}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:{log_dir}/ircd.log
StandardError=append:{log_dir}/ircd.log

[Install]
WantedBy=default.target
"""


class ObservatoryPaths:
    """Resolved filesystem layout under ``$MERCURY_HOME/observatory``."""

    def __init__(self, mercury_home: str | Path):
        self.root = Path(mercury_home) / "observatory"
        self.logs_dir = self.root / DIR_LOGS
        self.config_file = self.root / FILE_CONFIG
        self.history_db = self.root / FILE_HISTORY_DB
        self.tls_dir = self.root / DIR_TLS
        self.tls_ca = self.tls_dir / FILE_TLS_CA
        self.tls_cert = self.tls_dir / FILE_TLS_CERT
        self.tls_key = self.tls_dir / FILE_TLS_KEY

    def server_url(
        self, *, address: str = IRCD_ADDRESS, port: int = IRCD_SERVER_PORT_DEFAULT
    ) -> str:
        return f"irc://{address}:{port}"
