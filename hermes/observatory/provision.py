"""Idempotent, fail-hard provisioning orchestrator for the Matrix Observatory.

Spec: docs/design/matrix-observatory.md §2 component 1 + §4 onboarding +
D16. What 'provision' means here, in order:

1. ``refresh_tuwunel``  — ensure the latest stable binary (>= 1.8.1 gate)
   at ``$MERCURY_HOME/observatory/bin/tuwunel`` with its version file.
2. ``ensure_config``    — write ``tuwunel.toml`` ONCE (closed form:
   localhost, no federation, no registration, registration_token). Never
   overwritten; delete the file to re-provision.
3. ``ensure_appservice_registration`` — write the sidecar registration YAML
   ONCE (random as/hs tokens, exclusive ``^@merc_.*$`` namespace).
4. ``ensure_owner_account`` — first registered user becomes admin. The
   closed config cannot register anyone (verified live: 403 "Registration
   has been disabled"), so this boots a THROWAWAY config with
   ``allow_registration = true`` on the SAME database/port, registers the
   owner via ``m.login.registration_token``, then removes it. Credentials
   land in ``owner-credentials.json`` (0600) — the Phase 3 setup card's
   source. Skipped entirely when the credentials file already exists.
5. ``ensure_systemd_unit`` — user unit ``mercury-observatory-homeserver.service``
   (ExecStart the binary with the toml, Restart=on-failure, logs under
   ``$MERCURY_HOME/observatory/logs``), mirroring how the gateway unit is
   generated/installed (write, daemon-reload, enable). Best-effort ONLY
   when systemd is absent (containers/CI) — everything else fails hard.

Entry points:
  ``python -m observatory.provision``          — install.sh + the future
      first-gateway-start hook (same command, same idempotence).
  ``observatory.provision.refresh_for_update`` — `mercury update` (D16).
  ``observatory.provision.provision_in_wizard`` — the setup wizard's
      'Matrix Observatory' section (same steps/summary, never exits).
  ``observatory.provision.status_summary``     — wizard status card input
      (booleans/paths only, never secrets).

The sidecar unit (``mercury-observatory.service``) is deliberately NOT
created — the sidecar package lands in Phase 3.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from observatory import config_gen
from observatory.config_gen import (
    APPSERVICE_PORT_DEFAULT,
    HOMESERVER_UNIT_NAME,
    ObservatoryPaths,
    OWNER_LOCALPART_DEFAULT,
)
from observatory import tuwunel

#: The registration-token UIAA flow (verified live against tuwunel 1.9.0).
_REGISTRATION_AUTH_TYPE = "m.login.registration_token"


class ProvisionError(RuntimeError):
    """Fail-hard provisioning error with a clear, actionable message."""


# --- helpers -------------------------------------------------------------------

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


def _write_secret_file(path: Path, content: str) -> None:
    """Write a secrets-bearing file (toml, registration YAML, credentials)
    with 0600 — same law as $MERCURY_HOME/.env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def _load_toml(path: Path) -> dict:
    import tomllib

    with open(path, "rb") as f:
        return tomllib.load(f)


def _http_json(method: str, url: str, payload: dict | None = None,
               token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        raise ProvisionError(f"{method} {url} failed: {exc}") from exc


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _run_systemctl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """`systemctl --user <args>`; raises ProvisionError with stderr on check failure."""
    cmd = ["systemctl", "--user", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:  # pragma: no cover - raced away
        raise ProvisionError("systemctl disappeared mid-provision") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ProvisionError(f"`{' '.join(cmd)}` failed: {detail}")
    return proc


# --- steps ---------------------------------------------------------------------

def ensure_config(paths: ObservatoryPaths, registration_token: str | None = None) -> str:
    """Write the closed tuwunel.toml if absent; NEVER overwrite. Returns
    'kept' when an existing file was preserved, 'created' otherwise."""
    if paths.toml.exists():
        return "kept"
    token = registration_token or config_gen.new_secret(32)
    content = config_gen.render_tuwunel_toml(
        database_path=str(paths.db_dir),
        appservice_dir=str(paths.appservices_dir),
        registration_token=token,
    )
    _write_secret_file(paths.toml, content)
    return "created"


def ensure_appservice_registration(paths: ObservatoryPaths) -> str:
    """Write the sidecar registration YAML if absent; NEVER overwrite
    (regenerating would invalidate the homeserver's hs_token trust)."""
    if paths.appservice_registration.exists():
        return "kept"
    content = config_gen.render_appservice_registration_yaml(
        url=f"http://127.0.0.1:{APPSERVICE_PORT_DEFAULT}",
        as_token=config_gen.new_secret(32),
        hs_token=config_gen.new_secret(32),
    )
    _write_secret_file(paths.appservice_registration, content)
    return "created"


def _wait_for_homeserver(base_url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = _http_json("GET", f"{base_url}/_matrix/client/versions")
            if status == 200:
                return
        except ProvisionError:
            pass
        time.sleep(0.5)
    raise ProvisionError(
        f"homeserver did not become ready within {timeout:.0f}s at {base_url}"
    )


def ensure_owner_account(paths: ObservatoryPaths,
                         owner_localpart: str = OWNER_LOCALPART_DEFAULT) -> str:
    """Bootstrap the owner (admin) account; 'exists' when already provisioned.

    Boots the binary against a THROWAWAY bootstrap config (identical to the
    closed one except allow_registration = true) on the same database and
    port, registers via the registration token, then tears both down. The
    running systemd unit, if any, is stopped first and restarted by the
    caller's unit step.
    """
    if paths.owner_credentials.exists():
        return "exists"
    if not paths.binary.is_file():
        raise ProvisionError(
            f"tuwunel binary missing at {paths.binary} — cannot bootstrap the owner"
        )

    cfg = _load_toml(paths.toml).get("global", {})
    token = str(cfg.get("registration_token") or "")
    if not token:
        raise ProvisionError(
            f"{paths.toml} has no registration_token — delete the file to re-provision"
        )
    port = int(cfg.get("port", config_gen.HOMESERVER_PORT_DEFAULT))
    address = cfg.get("address", config_gen.HOMESERVER_ADDRESS)
    if isinstance(address, list):  # toml allows vector bindings
        address = address[0]
    base_url = paths.homeserver_url(address=address, port=port)

    # port must be free: stop the unit if one is running (idempotent)
    if _systemctl_available():
        _run_systemctl(["stop", HOMESERVER_UNIT_NAME], check=False)

    bootstrap = config_gen.render_tuwunel_toml(
        database_path=str(paths.db_dir),
        appservice_dir=str(paths.appservices_dir),
        registration_token=token,
        server_name=str(cfg.get("server_name", config_gen.SERVER_NAME_DEFAULT)),
        address=address,
        port=port,
        allow_registration=True,  # bootstrap ONLY; deleted below
    )
    _write_secret_file(paths.bootstrap_toml, bootstrap)
    password = config_gen.new_secret(24)

    proc = subprocess.Popen(
        [str(paths.binary), "-c", str(paths.bootstrap_toml)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_homeserver(base_url)
        status, body = _http_json(
            "POST",
            f"{base_url}/_matrix/client/v3/register",
            payload={
                "username": owner_localpart,
                "password": password,
                "auth": {"type": _REGISTRATION_AUTH_TYPE, "token": token},
            },
        )
        if status != 200 or "user_id" not in body:
            raise ProvisionError(
                f"owner registration failed (HTTP {status}): "
                f"{json.dumps(body)[:300]}"
            )
        _write_secret_file(
            paths.owner_credentials,
            json.dumps(
                {
                    "homeserver_url": base_url,
                    "user_id": body["user_id"],
                    "password": password,
                    "access_token": body.get("access_token", ""),
                    "device_id": body.get("device_id", ""),
                    "note": "first registered user = server admin (owner). "
                            "Shown by the Phase 3 onboarding setup card.",
                },
                indent=2,
            ) + "\n",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn server
            proc.kill()
            proc.wait()
        paths.bootstrap_toml.unlink(missing_ok=True)
    return "created"


def ensure_systemd_unit(paths: ObservatoryPaths) -> str:
    """Install/refresh the user unit; returns 'installed', 'refreshed', or
    'skipped' (no systemd — containers/CI; caller surfaces a hint)."""
    if not _systemctl_available():
        return "skipped"
    unit = config_gen.render_homeserver_unit(
        exec_path=str(paths.binary),
        config_path=str(paths.toml),
        log_dir=str(paths.logs_dir),
    )
    unit_path = Path.home() / ".config" / "systemd" / "user" / HOMESERVER_UNIT_NAME
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    existing = unit_path.read_text() if unit_path.exists() else ""
    changed = existing != unit
    if changed:
        unit_path.write_text(unit, encoding="utf-8")
    _run_systemctl(["daemon-reload"])
    _run_systemctl(["enable", HOMESERVER_UNIT_NAME])
    _run_systemctl(["restart", HOMESERVER_UNIT_NAME])
    return "refreshed" if changed and existing else "installed" if changed else "started"


# --- orchestration ---------------------------------------------------------------

def provision(mercury_home: str | Path | None = None,
              registration_token: str | None = None, *,
              fetch: tuwunel.Fetch | None = None,
              systemd: bool = True,
              owner_localpart: str = OWNER_LOCALPART_DEFAULT,
              offline: bool | None = None) -> dict:
    """Run every provisioning step (order matters: binary -> config ->
    appservice -> owner -> unit). Returns a summary dict; raises
    TuwunelError/ProvisionError on failure (installer fail-hard law).

    ``offline``: None resolves from config (``observatory.offline``, default
    False); True makes the binary step trust the installed version file and
    skip the GitHub release API entirely; False is the normal online path.
    """
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    if offline is None:
        offline = observatory_offline()
    if offline:
        action, version = _refresh_tuwunel_offline(paths)
    else:
        action, version = tuwunel.refresh_tuwunel(paths, fetch=fetch)
    summary = {
        "tuwunel": {"action": action, "version": version, "binary": str(paths.binary),
                    "offline": offline},
        "config": ensure_config(paths, registration_token),
        "appservice": ensure_appservice_registration(paths),
        "owner": ensure_owner_account(paths, owner_localpart),
        "unit": ensure_systemd_unit(paths) if systemd else "skipped (--no-systemd)",
    }
    return summary


def observatory_enabled() -> bool:
    """Config gate (D1): observatory.enabled in config.yaml, default ON.
    Unreadable/missing config never silently disables the observatory."""
    try:
        from mercury_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return True
    obs = cfg.get("observatory")
    if not isinstance(obs, dict):
        return True
    return bool(obs.get("enabled", True))

def observatory_offline() -> bool:
    """Config gate: ``observatory.offline`` in config.yaml, default OFF.

    Offline = trust the installed ``tuwunel.version`` file and NEVER query
    the GitHub release API (E2E sandbox homes / air-gapped hosts where the
    binary was fetched once with network). A CLI ``--offline`` overrides
    this to True; nothing here reads an environment secret.
    """
    try:
        from mercury_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return False
    obs = cfg.get("observatory")
    if not isinstance(obs, dict):
        return False
    return bool(obs.get("offline", False))


def _refresh_tuwunel_offline(paths: ObservatoryPaths) -> tuple[str, str]:
    """Offline fetch-step skip: the version file says current, trust it.

    No network of any kind: reads ``installed_version`` (binary + version
    file must BOTH exist), enforces the same >= MIN_VERSION gate as the
    online path, and reports ``("current", v)``. Raises TuwunelError when
    there is nothing usable to trust — offline never downloads.
    """
    current = tuwunel.installed_version(paths)
    if current is None:
        raise tuwunel.TuwunelError(
            "offline provisioning requested but no tuwunel binary/version "
            f"file is installed under {paths.bin_dir} — fetch once with "
            "network (drop --offline), then offline runs skip the fetch"
        )
    tuwunel.check_min_version(f"v{current}")
    return "current", current



def refresh_for_update() -> str | None:
    """D16 hook for `mercury update` (wired in cli_commands_mixin).

    No-op (silent) when the observatory is disabled in config. Otherwise:
    refresh the binary to latest stable (>= 1.8.1 gate), then restart the
    homeserver unit IF it exists. Returns a one-line description for the
    caller to print, or None when nothing applied.
    """
    if not observatory_enabled():
        return None
    if observatory_offline():
        # Trust the installed binary; `mercury update` cannot query for a
        # newer release without network, so the refresh step is a no-op.
        return None
    paths = ObservatoryPaths(_mercury_home())
    action, version = tuwunel.refresh_tuwunel(paths)
    line = f"observatory: tuwunel {action} v{version}"
    unit_path = Path.home() / ".config" / "systemd" / "user" / HOMESERVER_UNIT_NAME
    if unit_path.exists():
        _run_systemctl(["restart", HOMESERVER_UNIT_NAME])
        line += f"; restarted {HOMESERVER_UNIT_NAME}"
    return line

def provision_if_missing(mercury_home: str | Path | None = None,
                         *, offline: bool | None = None) -> dict | None:
    """First-time provision gate for `mercury update` (update-completeness).

    An existing install that predates the observatory has NO
    ``tuwunel.version`` file — ``refresh_for_update`` alone would only ever
    swap the binary on a tree that was never provisioned (no tuwunel.toml,
    appservice registration, owner account, or systemd unit). This gate
    runs the full idempotent :func:`provision` exactly once, on the first
    update that lands on such a tree:

    * observatory disabled in config → silent skip (None);
    * ``tuwunel.version`` already present → skip (None) — the update tail's
      ``refresh_for_update`` owns the binary refresh from there on;
    * offline (config gate or explicit) with nothing installed → silent
      skip (None): provisioning would hard-fail with nothing to trust and
      no network to fetch it;
    * otherwise → ``provision()``; returns its summary dict, or raises
      TuwunelError/ProvisionError on failure (callers WARN, never block).

    Same law as :func:`observatory_enabled`: unreadable config never
    silently disables the first-time provision (config default governs).
    """
    if not observatory_enabled():
        return None
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    if paths.version_file.is_file():
        return None
    if offline is None:
        offline = observatory_offline()
    if offline:
        return None
    return provision(mercury_home, offline=False)

# --- setup-wizard status (setup.py 'Matrix Observatory' section) ----------------

def _homeserver_reachable(base_url: str, timeout: float = 2.0) -> bool:
    """Liveness probe for status displays: GET ``/_matrix/client/versions``.
    Best-effort by design — refused/timeout/DNS all mean 'not reachable';
    never raises."""
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/_matrix/client/versions"
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 — display probe, not a health contract
        return False


def _unit_active() -> bool:
    """``systemctl --user is-active --quiet`` probe for status displays.
    False whenever systemd is missing or the unit is not running; never
    raises (containers/CI have no systemd user session at all)."""
    if not _systemctl_available():
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", HOMESERVER_UNIT_NAME],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def observatory_e2ee_flag(mercury_home: str | Path | None = None) -> bool:
    """``observatory.e2ee`` config value for status displays.

    Delegates to ``observatory.e2ee.e2ee_enabled`` when importable (single
    source of truth — the default is owned there and may be flipped);
    otherwise falls back to the same direct ``$MERCURY_HOME/config.yaml``
    read shape. No secrets involved."""
    try:
        from observatory.e2ee import e2ee_enabled

        return bool(e2ee_enabled(mercury_home))
    except Exception:
        pass
    try:
        import yaml

        cfg_path = _mercury_home(mercury_home) / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        obs = doc.get("observatory") if isinstance(doc, dict) else None
        return bool(obs.get("e2ee", False)) if isinstance(obs, dict) else False
    except Exception:
        return False


def status_summary(mercury_home: str | Path | None = None) -> dict:
    """Wizard-facing observatory status. Booleans, paths and the URL only —
    NEVER secrets (no owner password, no registration/as/hs tokens). Every
    probe degrades to False instead of raising so callers can render this
    unconditionally. Fields:

    - ``provisioned``             — closed tuwunel.toml + owner credentials exist
    - ``config_exists``           — tuwunel.toml present (written once)
    - ``binary_installed``        — tuwunel binary present
    - ``owner_credentials_exist`` / ``owner_credentials_path`` — the 0600
      credentials file (the password stays inside it)
    - ``homeserver_url`` / ``homeserver_reachable`` — localhost URL + liveness
    - ``unit_active`` / ``unit_name`` — systemd user unit state
    - ``enabled``                 — observatory.enabled config gate (default on)
    - ``e2ee``                    — observatory.e2ee flag
    - ``observatory_dir``         — $MERCURY_HOME/observatory
    """
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    config_exists = paths.toml.is_file()
    creds_exist = paths.owner_credentials.is_file()
    return {
        "provisioned": config_exists and creds_exist,
        "config_exists": config_exists,
        "binary_installed": paths.binary.is_file(),
        "owner_credentials_exist": creds_exist,
        "owner_credentials_path": str(paths.owner_credentials),
        "homeserver_url": paths.homeserver_url(),
        "homeserver_reachable": _homeserver_reachable(paths.homeserver_url()),
        "unit_active": _unit_active(),
        "unit_name": HOMESERVER_UNIT_NAME,
        "enabled": observatory_enabled(),
        "e2ee": observatory_e2ee_flag(mercury_home),
        "observatory_dir": str(paths.root),
    }

def detect_tailscale() -> dict:
    """Best-effort Tailscale tailnet detection for the wizard phone card.

    Detect-and-assist only: ``shutil.which('tailscale')`` + read-only
    ``tailscale status`` / ``tailscale ip -4`` / ``tailscale status --json``
    probes. NEVER installs, NEVER authenticates, NEVER raises — every
    failure degrades to ``up=False`` (absent/down) so callers render
    unconditionally. Shape: ``{"available", "up", "ip", "dns_name"}``
    where ``ip`` is the tailnet IPv4 and ``dns_name`` the MagicDNS name
    (trailing dot stripped) when the daemon reports them.
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
            capture_output=True, text=True, timeout=10,
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


def tailscale_phone_url(detection: dict | None, port: int = config_gen.HOMESERVER_PORT_DEFAULT) -> str | None:
    """Phone homeserver URL for the wizard card: MagicDNS preferred,
    tailnet IPv4 fallback. None unless the tailnet is up with a host.
    Pure function — never touches the network."""
    try:
        if not isinstance(detection, dict) or not detection.get("up"):
            return None
        host = detection.get("dns_name") or detection.get("ip")
        if not host or not str(host).strip():
            return None
        return f"http://{str(host).strip()}:{int(port)}"
    except Exception:  # noqa: BLE001
        return None


def set_tuwunel_bind(ip: str, mercury_home: str | Path | None = None) -> str:
    """Bind the homeserver to one interface address (Tailscale-only offer).

    Rewrites the ``address = "..."`` line in the EXISTING tuwunel.toml and
    returns the new address. Fails with a ProvisionError carrying guidance
    when unprovisioned (missing tuwunel.toml) or when no address line is
    found — never creates config. Never starts/stops the server itself:
    the caller must restart ``mercury-observatory-homeserver.service``
    for the new bind to take effect.
    """
    if not isinstance(ip, str) or not ip.strip():
        raise ProvisionError("set_tuwunel_bind needs a non-empty tailnet IP")
    target = ip.strip()
    try:
        import ipaddress as _ipaddress

        _ipaddress.ip_address(target)
    except Exception as exc:
        raise ProvisionError(f"refusing to bind to {target!r}: not an IP address ({exc})") from exc
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    if not paths.toml.is_file():
        raise ProvisionError(
            f"observatory not provisioned (tuwunel.toml missing at {paths.toml}) — "
            "run 'mercury setup observatory' install first; refusing to create "
            "config via bind"
        )
    try:
        text = paths.toml.read_text(encoding="utf-8")
    except Exception as exc:
        raise ProvisionError(f"could not read {paths.toml}: {exc}") from exc
    import re as _re

    pattern = _re.compile(r'(?m)^address\s*=\s*".*"\s*$')
    if not pattern.search(text):
        raise ProvisionError(
            f"could not find the address line in {paths.toml} — hand-edit "
            '`address = "..."` under [global] instead'
        )
    paths.toml.write_text(
        pattern.sub(f'address = "{target}"', text, count=1), encoding="utf-8"
    )
    try:
        paths.toml.chmod(0o600)
    except Exception:  # noqa: BLE001 — perms best-effort on odd filesystems
        pass
    return target


def _print_summary(summary: dict) -> None:
    """Render the provision summary — CLI and wizard share these EXACT
    lines (byte-compatible with the pre-helper CLI output)."""
    tw = summary["tuwunel"]
    print(f"  ✓ tuwunel: {tw['action']} v{tw['version']} → {tw['binary']}")
    print(f"  ✓ config: {summary['config']} (tuwunel.toml)")
    print(f"  ✓ appservice registration: {summary['appservice']}")
    print(f"  ✓ owner account: {summary['owner']}")
    if summary["unit"] == "skipped":
        print("  ⚠ systemd user unit skipped (systemd unavailable or --no-systemd);")
        print("    start manually: "
              f"{tw['binary']} -c <mercury-home>/observatory/tuwunel.toml")
    else:
        print(f"  ✓ systemd unit: {summary['unit']} ({HOMESERVER_UNIT_NAME})")


def provision_in_wizard(mercury_home: str | Path | None = None) -> dict:
    """In-process provisioning entry for the setup wizard's 'Matrix
    Observatory' section: same steps, same summary, same output lines as
    the ``python -m observatory.provision`` CLI. Raises
    TuwunelError/ProvisionError on failure — the wizard section catches
    and shows the message — and never ``sys.exit()``s: the wizard must
    survive a failed install."""
    print("→ Matrix Observatory provisioning (Tuwunel)")
    summary = provision(mercury_home)
    _print_summary(summary)
    return summary


# --- CLI --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m observatory.provision",
        description="Provision the Mercury Matrix Observatory homeserver (idempotent).",
    )
    parser.add_argument(
        "--mercury-home", default=None,
        help="Mercury home (default: $MERCURY_HOME or ~/.mercury)",
    )
    parser.add_argument(
        "--registration-token", default=None,
        help="Registration token for the generated tuwunel.toml (install.sh "
             "passes `openssl rand` output; a fresh secret is drawn when omitted)",
    )
    parser.add_argument(
        "--owner-localpart", default=OWNER_LOCALPART_DEFAULT,
        help=f"owner account localpart (default: {OWNER_LOCALPART_DEFAULT})",
    )
    parser.add_argument(
        "--no-systemd", action="store_true",
        help="skip the systemd user unit (containers/CI/dry-runs)",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="skip the GitHub release query: trust the installed "
             "tuwunel.version file (config default: observatory.offline)",
    )
    args = parser.parse_args(argv)

    print("→ Matrix Observatory provisioning (Tuwunel)")
    try:
        summary = provision(
            args.mercury_home,
            args.registration_token,
            systemd=not args.no_systemd,
            owner_localpart=args.owner_localpart,
            offline=True if args.offline else None,
        )
    except (tuwunel.TuwunelError, ProvisionError) as exc:
        print(f"✗ observatory provisioning failed: {exc}")
        return 1

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
