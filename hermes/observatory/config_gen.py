"""Pure generators for Matrix Observatory provisioning artifacts.

Spec: docs/design/matrix-observatory.md §2 component 1 + D16. Every function
here is pure (inputs -> string; no I/O, no clock, no randomness beyond the
explicit token arguments). All filesystem work lives in
``observatory.provision``; the contract tests in ``tests/observatory`` cover
only this module's output.

Config keys verified against tuwunel-example.toml (v1.9.0) and a live boot:
``server_name`` / ``database_path`` / ``address`` / ``port`` /
``allow_registration`` / ``registration_token`` / ``allow_federation`` /
``appservice_dir`` — all under ``[global]``.
"""
from __future__ import annotations

import secrets
from pathlib import Path

# --- fixed contract constants (spec §2, D3/D4) -------------------------------

#: MXID suffix. Non-federating (D2), so this is a label, not a DNS promise;
#: immutable once the database exists, hence a stable constant.
SERVER_NAME_DEFAULT = "mercury.local"

#: Localhost only (D2). Mobile clients arrive over Tailscale/VPN.
HOMESERVER_ADDRESS = "127.0.0.1"
#: Distinct from Tuwunel's 8008 default so a pre-existing homeserver on the
#: box is never collided with.
HOMESERVER_PORT_DEFAULT = 18008

#: Appservice (sidecar) listen port; the registration YAML points the
#: homeserver here. Served by the bundled sidecar daemon
#: (mercury-observatory.service).
APPSERVICE_PORT_DEFAULT = 18090
APPSERVICE_ID = "merc-observatory"
APPSERVICE_SENDER_LOCALPART = "merc-bot"
#: Anchored + exclusive: ALL observatory virtual users carry the reserved
#: ``merc_`` prefix (spec §4 resolution), so the appservice owns the namespace.
APPSERVICE_NAMESPACE_REGEX = "^@merc_.*$"
APPSERVICE_REGISTRATION_FILENAME = "merc-observatory.yaml"

#: First registered user becomes admin (owner bootstrap, spec §4).
OWNER_LOCALPART_DEFAULT = "merc-owner"

HOMESERVER_UNIT_NAME = "mercury-observatory-homeserver.service"
HOMESERVER_UNIT_DESCRIPTION = "Mercury Observatory homeserver (Tuwunel)"

#: Sidecar daemon unit — installed by the setup Install paths (via the
#: auto steps; LOUD on post-wipe reprovision since the wipe destroys the
#: unit file) and by the explicit repair path
#: (``mercury setup observatory --install-sidecar`` via
#: ``provision.ensure_sidecar_unit``), never by provision() itself: the
#: daemon boots provision(), so auto-installing there would restart its
#: own unit mid-boot.
SIDECAR_UNIT_NAME = "mercury-observatory.service"
SIDECAR_UNIT_DESCRIPTION = "Mercury Observatory sidecar (appservice daemon)"

# Directory layout under $MERCURY_HOME/observatory
DIR_BIN = "bin"
DIR_DB = "tuwunel-db"
DIR_APPSERVICES = "appservices"
DIR_LOGS = "logs"
FILE_TOML = "tuwunel.toml"
FILE_VERSION = "tuwunel.version"
FILE_OWNER_CREDENTIALS = "owner-credentials.json"


def new_secret(nbytes: int = 32) -> str:
    """URL-safe random secret (registration token, as/hs tokens, owner password).

    Cryptographically equivalent to the spec's ``openssl rand``; kept in one
    place so every artifact draws from the same generator.
    """
    return secrets.token_urlsafe(nbytes)


def render_tuwunel_toml(
    *,
    database_path: str,
    appservice_dir: str,
    registration_token: str,
    server_name: str = SERVER_NAME_DEFAULT,
    address: str = HOMESERVER_ADDRESS,
    port: int = HOMESERVER_PORT_DEFAULT,
    allow_registration: bool = False,
    allow_federation: bool = False,
) -> str:
    """Render the homeserver config (final, CLOSED form).

    ``allow_registration = false`` keeps the server closed — verified live:
    token registration 403s ("Registration has been disabled") in this state.
    The owner bootstrap flips a temporary copy to ``true`` (see
    ``provision.ensure_owner_account``); this file never carries it.
    """
    if not registration_token:
        raise ValueError("registration_token must be a non-empty secret")
    return f"""\
# Mercury Matrix Observatory — Tuwunel homeserver config.
# Generated ONCE at provision time; hand-editable and never overwritten
# (delete it to re-provision). Keys: tuwunel-example.toml upstream
# (github.com/matrix-construct/tuwunel).

[global]
# MXID suffix (@user:server_name). Immutable without a database wipe.
server_name = "{server_name}"

# RocksDB lives under the Mercury home, never the engine tree.
database_path = "{database_path}"

# Localhost only (spec D2): mobile clients reach the server over
# Tailscale/VPN, never the open internet. No per-user rate limiter exists
# upstream — do NOT expose this beyond the VPN.
address = "{address}"
port = {port}

# Closed server (spec D2): no federation, no registration. The token below
# matters only during owner bootstrap, while a temporary config has
# allow_registration = true; with this file's false it is inert.
allow_federation = {str(allow_federation).lower()}
allow_registration = {str(allow_registration).lower()}
registration_token = "{registration_token}"

# Appservice registration YAMLs (sidecar) are dropped here;
# tuwunel loads them at startup.
appservice_dir = "{appservice_dir}"

# RocksDB WAL preallocation (fallocate) balloons archived WALs to their
# full preallocated size on CoW filesystems (btrfs: 602-byte logical WALs
# kept 20-70MB physical each) — disabling it only turns off preallocation
# and is safe on all filesystems, so it ships unconditionally.
rocksdb_allow_fallocate = false
"""


def render_appservice_registration_yaml(
    *,
    url: str,
    as_token: str,
    hs_token: str,
    registration_id: str = APPSERVICE_ID,
    sender_localpart: str = APPSERVICE_SENDER_LOCALPART,
    namespace_regex: str = APPSERVICE_NAMESPACE_REGEX,
    exclusive: bool = True,
) -> str:
    """Render the appservice registration YAML (spec §2 component 2, D3).

    Shape verified live against tuwunel 1.9.0: a file with exactly this
    layout in ``appservice_dir`` is picked up at startup and the as_token
    masquerades virtual users via ``?user_id=``.
    """
    return f"""\
# Mercury Observatory appservice registration (generated; the homeserver
# reads this from appservice_dir at startup). The sidecar serves
# {url} and authenticates with as_token; hs_token authenticates the
# homeserver's transactions TO the sidecar.
id: {registration_id}
url: {url}
as_token: "{as_token}"
hs_token: "{hs_token}"
sender_localpart: {sender_localpart}
# Virtual users are never throttled server-side and only exist appservice-side.
rate_limited: false
namespaces:
  users:
    - regex: "{namespace_regex}"
      exclusive: {str(exclusive).lower()}
"""


def render_homeserver_unit(
    *,
    exec_path: str,
    config_path: str,
    log_dir: str,
    description: str = HOMESERVER_UNIT_DESCRIPTION,
    restart: str = "on-failure",
) -> str:
    """Render the systemd USER unit (mirrors mercury-gateway.service shape:
    ~/.config/systemd/user/<name>, WantedBy=default.target).

    Logs append under $MERCURY_HOME/observatory/logs (spec: logs live in the
    Mercury home, not the journal-only gateway pattern, so they survive
    journal rotation and are trivially tailable).
    """
    return f"""\
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={exec_path} -c {config_path}
Restart={restart}
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=90
StandardOutput=append:{log_dir}/homeserver.log
StandardError=append:{log_dir}/homeserver.log

[Install]
WantedBy=default.target
"""


def render_sidecar_unit(
    *,
    python_bin: str,
    hermes_root: str,
    mercury_home: str,
    log_dir: str,
    description: str = SIDECAR_UNIT_DESCRIPTION,
    hermes_home: str | None = None,
    mercury_config: str | None = None,
    agent_dir: str | None = None,
    omp_bin: str | None = None,
    venv_dir: str | None = None,
    sane_path: str | None = None,
) -> str:
    """Render the sidecar systemd USER unit from the checked-in template
    (``observatory/templates/mercury-observatory.service``). Mirrors
    :func:`render_homeserver_unit`'s law: pure string templating, no
    I/O beyond reading the template — importable WITHOUT the matrix
    extra (aiohttp), so ``provision.ensure_sidecar_unit`` can install
    the unit file even when the crypto stack is missing.

    Env mirrors ``mercury-gateway.service`` (``mercury_cli.gateway``): the
    sidecar MUST see the unified ``MERCURY_CONFIG`` (never the
    onboarding-only ``HERMES_HOME/config.yaml``) so approvals.mode and
    model.default resolve identically inside and outside a login shell.
    Every new pin is an optional override defaulting to the layout
    derived from ``mercury_home``/``hermes_root``/``python_bin``.
    """
    if hermes_home is None:
        hermes_home = f"{mercury_home}/hermes"
    if mercury_config is None:
        mercury_config = f"{mercury_home}/config.yaml"
    if agent_dir is None:
        agent_dir = f"{mercury_home}/omp"
    if omp_bin is None:
        omp_bin = str(
            Path(hermes_root).parent / "omp" / "packages" / "coding-agent" / "dist" / "omp"
        )
    if venv_dir is None:
        _pb = Path(python_bin)
        venv_dir = str(_pb.parent.parent if _pb.parent.name == "bin" else _pb.parent)
    if sane_path is None:
        sane_path = (
            f"{venv_dir}/bin:/usr/local/sbin:/usr/local/bin"
            ":/usr/sbin:/usr/bin:/sbin:/bin"
        )
    template = (Path(__file__).parent / "templates" / "mercury-observatory.service").read_text(encoding="utf-8")
    return template.format(
        description=description,
        python_bin=python_bin,
        hermes_root=hermes_root,
        mercury_home=mercury_home,
        log_dir=log_dir,
        hermes_home=hermes_home,
        mercury_config=mercury_config,
        agent_dir=agent_dir,
        omp_bin=omp_bin,
        venv_dir=venv_dir,
        sane_path=sane_path,
    )


class ObservatoryPaths:
    """Resolved filesystem layout under ``$MERCURY_HOME/observatory``."""

    def __init__(self, mercury_home: str | Path):
        self.root = Path(mercury_home) / "observatory"
        self.bin_dir = self.root / DIR_BIN
        self.db_dir = self.root / DIR_DB
        self.appservices_dir = self.root / DIR_APPSERVICES
        self.logs_dir = self.root / DIR_LOGS
        self.toml = self.root / FILE_TOML
        self.version_file = self.bin_dir / FILE_VERSION
        self.binary = self.bin_dir / "tuwunel"
        self.appservice_registration = self.appservices_dir / APPSERVICE_REGISTRATION_FILENAME
        self.owner_credentials = self.root / FILE_OWNER_CREDENTIALS
        self.bootstrap_toml = self.root / "tuwunel-bootstrap.toml"

    def homeserver_url(self, *, address: str = HOMESERVER_ADDRESS,
                       port: int = HOMESERVER_PORT_DEFAULT) -> str:
        return f"http://{address}:{port}"
