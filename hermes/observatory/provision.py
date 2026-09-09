"""Idempotent, fail-hard provisioning orchestrator for the Matrix Observatory.

Spec: docs/design/matrix-observatory.md §2 component 1 + §4 onboarding +
D16. What 'provision' means here, in order:

1. ``_refresh_tuwunel_offline`` — trust the installed binary (>= 1.8.1
   gate) at ``$MERCURY_HOME/observatory/bin/tuwunel`` with its version
   file. Boot/provision NEVER touches the network; the online
   latest-stable check lives ONLY behind explicit user actions
   (install.sh's CLI call, `mercury update`'s ``refresh_for_update`` /
   ``provision_if_missing``).
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
   land in ``owner-credentials.json`` (0600) and are mirrored into
   ``$MERCURY_HOME/.env`` as ``MATRIX_OBS_OWNER_*`` (0600) — the
   setup card's sources. Skipped entirely when the credentials file exists
   (the 'exists' path only fills .env keys a pre-mirror install missed).
5. ``ensure_systemd_unit`` — user unit ``mercury-observatory-homeserver.service``
   (ExecStart the binary with the toml, Restart=on-failure, logs under
   ``$MERCURY_HOME/observatory/logs``), mirroring how the gateway unit is
   generated/installed (write, daemon-reload, enable). Best-effort ONLY
   when systemd is absent (containers/CI) — everything else fails hard.

Entry points:
  ``python -m observatory.provision``          — install.sh (explicit
      install with network: online unless ``--offline``).
  ``observatory.provision.refresh_for_update`` — `mercury update` (D16).
  ``observatory.provision.provision_in_wizard`` — the setup wizard's
      'Matrix Observatory' section (same steps/summary, never exits).
  ``observatory.provision.status_summary``     — wizard status card input
      (booleans/paths only, never secrets).

The sidecar unit (``mercury-observatory.service``) is installed ONLY by
the explicit repair path ``ensure_sidecar_unit`` (``mercury setup
observatory --install-sidecar``) — never by provision() itself, because
the sidecar daemon boots provision() and auto-installing would restart
its own unit mid-boot.
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
    SIDECAR_UNIT_NAME,
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


def _bound_base_url(paths: ObservatoryPaths) -> str:
    """Homeserver URL from the CLOSED tuwunel.toml bind (address + port).

    The sidecar and render_live compute this same URL at boot; the
    credentials file and status displays must agree with it (a stale
    127.0.0.1 URL strands Tailscale phones after a bind change).
    Falls back to the localhost default when unprovisioned or
    unreadable — never raises."""
    try:
        cfg = _load_toml(paths.toml).get("global", {})
        port = int(cfg.get("port", config_gen.HOMESERVER_PORT_DEFAULT))
        address = cfg.get("address", config_gen.HOMESERVER_ADDRESS)
        if isinstance(address, list):
            address = address[0] if address else config_gen.HOMESERVER_ADDRESS
        text = str(address or "").strip() or config_gen.HOMESERVER_ADDRESS
        return paths.homeserver_url(address=text, port=port)
    except Exception:  # noqa: BLE001 — display/sync probe, never raises
        return paths.homeserver_url()


def sync_owner_homeserver_url(paths: ObservatoryPaths) -> str:
    """Rewrite owner-credentials.json homeserver_url to the toml bind.

    Returns the bound URL. No-op when the credentials file is absent;
    leaves a corrupt/unparseable file untouched (never destroys
    secrets); preserves every other key and keeps 0600. Best-effort by
    design — callers (re-provision heal, the bind offer) must never fail
    the outer step when the sync cannot complete."""
    bound = _bound_base_url(paths)
    try:
        raw = paths.owner_credentials.read_text(encoding="utf-8")
    except OSError:
        return bound
    try:
        doc = json.loads(raw)
    except ValueError:
        return bound
    if not isinstance(doc, dict):
        return bound
    if doc.get("homeserver_url") == bound:
        return bound
    doc["homeserver_url"] = bound
    try:
        paths.owner_credentials.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        paths.owner_credentials.chmod(0o600)
    except OSError:
        pass
    return bound


#: $MERCURY_HOME/.env keys mirroring the observatory owner credentials, so
#: the FluffyChat password survives outside owner-credentials.json (the setup
#: card points here; NEVER printed to the terminal).
ENV_OWNER_USER_ID = "MATRIX_OBS_OWNER_USER_ID"
ENV_OWNER_PASSWORD = "MATRIX_OBS_OWNER_PASSWORD"


def _quote_env_value(value: str) -> str:
    """Quote a .env value only when it carries dotenv-special characters.

    Same rule as mercury_cli.config._quote_env_value (kept local so this
    module stays importable from install.sh without the CLI package).
    """
    if value == "":
        return value
    needs_quoting = (
        "#" in value
        or '"' in value
        or "'" in value
        or value != value.strip()
        or any(c.isspace() for c in value)
    )
    if not needs_quoting:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _env_line_defines_key(line: str, key: str) -> bool:
    """True when a .env line assigns *key* (plain or ``export``-prefixed)."""
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    name, sep, _ = stripped.partition("=")
    return bool(sep) and name.strip() == key


def mirror_owner_env(mercury_home: str | Path | None, user_id: str,
                     password: str, *, only_missing: bool = False) -> None:
    """Mirror the owner credentials into ``$MERCURY_HOME/.env`` (0600).

    Upserts :data:`ENV_OWNER_USER_ID` / :data:`ENV_OWNER_PASSWORD`: existing
    assignments are replaced in place, missing ones appended (creating the
    file when absent). With ``only_missing=True`` pre-existing values are
    left untouched — the idempotent heal path for installs provisioned
    before the mirror existed. NEVER logs the values.
    """
    home = _mercury_home(mercury_home)
    env_path = home / ".env"
    try:
        home.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        wanted = {ENV_OWNER_USER_ID: user_id, ENV_OWNER_PASSWORD: password}
        pending = dict(wanted)
        for i, line in enumerate(lines):
            for key in list(pending):
                if _env_line_defines_key(line, key):
                    if only_missing:
                        del pending[key]
                    else:
                        lines[i] = f"{key}={_quote_env_value(pending.pop(key))}\n"
                    break
        if lines and pending and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        for key, value in pending.items():
            lines.append(f"{key}={_quote_env_value(value)}\n")
        env_path.write_text("".join(lines), encoding="utf-8")
        env_path.chmod(0o600)
    except OSError as exc:
        raise ProvisionError(f"could not mirror owner credentials to {env_path}: {exc}") from exc


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


def _heal_owner_env_best_effort(paths: ObservatoryPaths) -> None:
    """Fill .env owner keys missing on pre-mirror installs; never raises.

    The 'exists' path means provisioning already succeeded, so a broken
    mirror must not fail the run — and a present value is never overwritten
    (the credentials file stays the source of truth).
    """
    try:
        creds = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
        mirror_owner_env(
            paths.root.parent,
            str(creds.get("user_id") or ""),
            str(creds.get("password") or ""),
            only_missing=True,
        )
    except Exception:  # noqa: BLE001 — heal-only; provisioning already done
        pass


def ensure_owner_account(paths: ObservatoryPaths,
                         owner_localpart: str = OWNER_LOCALPART_DEFAULT) -> str:
    """Bootstrap the owner (admin) account; 'exists' when already provisioned.

    Boots the binary against a THROWAWAY bootstrap config (identical to the
    closed one except allow_registration = true) on the same database and
    port, registers via the registration token, then tears both down. The
    running systemd unit, if any, is stopped first and restarted by the
    caller's unit step.

    Fresh credentials are mirrored into ``$MERCURY_HOME/.env`` as
    ``MATRIX_OBS_OWNER_USER_ID`` / ``MATRIX_OBS_OWNER_PASSWORD`` (0600);
    the 'exists' path only fills keys a pre-mirror install never wrote.
    """
    if paths.owner_credentials.exists():
        _heal_owner_env_best_effort(paths)
        sync_owner_homeserver_url(paths)
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
                            "Shown by the onboarding setup card (mercury setup observatory).",
                },
                indent=2,
            ) + "\n",
        )
        # Same password also lives in $MERCURY_HOME/.env (0600) for FluffyChat
        # paste-in. Fail-hard like every other step here — never log it.
        mirror_owner_env(paths.root.parent, str(body["user_id"]), password)
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


def ensure_sidecar_unit(mercury_home: str | Path | None = None,
                        python_bin: str | None = None,
                        hermes_root: str | Path | None = None) -> str:
    """Install/refresh the sidecar user unit; returns 'installed',
    'refreshed', 'started', or 'skipped' (no systemd — containers/CI;
    caller surfaces a hint).

    Repair path for ``mercury setup observatory --install-sidecar`` —
    deliberately NOT part of provision() (see module docstring). The
    render lives in sidecar_main (lazy import: that module imports this
    one, so a top-level import would cycle)."""
    if not _systemctl_available():
        return "skipped"
    from observatory.sidecar_main import render_sidecar_unit

    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    if hermes_root is None:
        hermes_root = Path(__file__).resolve().parent.parent
    if python_bin is None:
        python_bin = sys.executable
    unit = render_sidecar_unit(
        python_bin=str(python_bin),
        hermes_root=str(hermes_root),
        mercury_home=str(home),
        log_dir=str(paths.logs_dir),
    )
    unit_path = Path.home() / ".config" / "systemd" / "user" / SIDECAR_UNIT_NAME
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    existing = unit_path.read_text() if unit_path.exists() else ""
    changed = existing != unit
    if changed:
        unit_path.write_text(unit, encoding="utf-8")
    _run_systemctl(["daemon-reload"])
    _run_systemctl(["enable", SIDECAR_UNIT_NAME])
    _run_systemctl(["restart", SIDECAR_UNIT_NAME])
    return "refreshed" if changed and existing else "installed" if changed else "started"

# --- vendored crypto stack ---------------------------------------------------------
# python-olm 3.2.16 publishes no cp313 wheel on PyPI (cp310–cp312 only),
# and mautrix 0.21.1 hard-requires the compiled ``olm`` extension — so the
# repo vendors per-arch cp313 wheels under hermes/observatory/wheels/
# (checked in, SHA256SUMS-pinned). setup installs from there: no compiler,
# no container runtime, and no network needed for the compiled piece.
# observatory/scripts/build_python_olm_wheel.sh remains as a DOCUMENTED
# MANUAL fallback for rebuilding the wheels — it is never auto-invoked by
# any setup/install/update path (user directive 2026-09-08).

#: python-olm version pinned for the observatory E2EE stack.
PYTHON_OLM_VERSION = "3.2.16"

#: Pure-python crypto-stack companions, mirroring pyproject ``[matrix]``.
_CRYPTO_PY_DEPS = ("mautrix[encryption]==0.21.1", "aiosqlite==0.22.1")


def _vendored_wheels_dir() -> Path:
    """Absolute path of the checked-in crypto wheel set."""
    return Path(__file__).resolve().parent / "wheels"


def _host_olm_arch() -> str | None:
    """This host's python-olm wheel arch tag (``x86_64`` | ``aarch64``).

    Single source of truth for the uname-machine mapping, shared by the
    vendored-wheel selector below, the ``mercury update`` bundled-wheels
    filter, and install.sh ``_select_vendored_olm_wheel`` (same mapping in
    shell). None when the machine is unknown — callers then install no
    olm wheel rather than handing pip a conflicting set."""
    import platform as _platform

    return {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "aarch64", "arm64": "aarch64"}.get(
        _platform.machine().lower())


def _vendored_olm_wheel() -> Path | None:
    """Vendored python-olm wheel for THIS interpreter + platform, or None.

    Only cp313-on-linux needs vendoring (older interpreters resolve
    python-olm from the package index; other platforms have no wheel and
    no supported path). None also when the expected file is absent from
    the checkout (e.g. a partial tree)."""
    if sys.platform != "linux" or sys.version_info[:2] != (3, 13):
        return None
    arch = _host_olm_arch()
    if arch is None:
        return None
    cand = (_vendored_wheels_dir()
            / f"python_olm-{PYTHON_OLM_VERSION}-cp313-cp313-linux_{arch}.whl")
    return cand if cand.is_file() else None


def _read_wheel_hashes(wheels_dir: Path) -> dict[str, str]:
    """Parse wheels/SHA256SUMS (sha256sum format); {} when absent."""
    out: dict[str, str] = {}
    try:
        text = (wheels_dir / "SHA256SUMS").read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        out[name] = digest
    return out


def _verified_vendored_wheel(cand: Path) -> Path:
    """Hash-verify the vendored wheel against SHA256SUMS.

    Raises ProvisionError on a missing pin, an unreadable file, or any
    mismatch — an unverified wheel is never installed."""
    import hashlib

    pinned = _read_wheel_hashes(cand.parent).get(cand.name)
    if not pinned:
        raise ProvisionError(
            f"vendored wheel {cand.name} has no SHA256SUMS pin — refusing to install")
    try:
        digest = hashlib.sha256(cand.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProvisionError(
            f"vendored wheel {cand.name} unreadable ({exc}) — refusing to install") from exc
    if digest.lower() != pinned.lower():
        raise ProvisionError(
            f"vendored wheel {cand.name} hash mismatch (file {digest[:16]}… "
            f"!= pin {pinned[:16]}…) — refusing to install")
    return cand


def set_observatory_e2ee(enabled: bool, mercury_home: str | Path | None = None) -> None:
    """Write ``observatory.e2ee`` in ``$MERCURY_HOME/config.yaml`` (creates
    the mapping when absent, preserves every other key). Raises
    ProvisionError when the file cannot be read/written. Manual operator
    use only — the automatic crypto path NEVER calls this (it fails
    CLOSED instead of downgrading to plaintext)."""
    home = _mercury_home(mercury_home)
    cfg_path = home / "config.yaml"
    try:
        import yaml  # type: ignore[import-untyped]
    except Exception as exc:
        raise ProvisionError(f"cannot set observatory.e2ee (pyyaml missing): {exc}") from exc
    try:
        if cfg_path.is_file():
            doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(doc, dict):
                doc = {}
        else:
            doc = {}
        obs = doc.get("observatory")
        if not isinstance(obs, dict):
            obs = {}
            doc["observatory"] = obs
        obs["e2ee"] = bool(enabled)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    except ProvisionError:
        raise
    except Exception as exc:
        raise ProvisionError(f"could not write observatory.e2ee to {cfg_path}: {exc}") from exc


def _crypto_pip_install(python_bin: str, args: list[str]) -> tuple[bool, str]:
    """Install into python_bin's env: uv first, then pip.

    Local copy of the update_release runner — this module stays importable
    from install.sh without the CLI package, so it cannot import it."""
    def _run(cmd: list[str]) -> tuple[bool, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except (OSError, ValueError) as exc:
            return False, str(exc)
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        return proc.returncode == 0, detail[-600:]

    uv_bin = shutil.which("uv")
    if uv_bin:
        ok, detail = _run([uv_bin, "pip", "install", "--python", python_bin, *args])
        if ok:
            return True, ""
    else:
        ok, detail = False, "uv not found"
    if not ok:
        ok, detail = _run([python_bin, "-m", "pip", "install", *args])
    return ok, detail


def _crypto_fail_closed(reason: str) -> str:
    """Fail CLOSED: E2EE stays on, nothing is written to config.yaml.

    Prints an actionable line (what failed + the exact retry command) and
    returns ``"failed: <reason>"``. Never raises and NEVER touches
    ``observatory.e2ee`` — a missing/unimportable stack must never silently
    downgrade rooms to plaintext."""
    print(f"  ✗ crypto stack unrestorable ({reason}) — E2EE stays ON; "
          f"rooms will fail at sidecar boot until the stack imports. "
          f"Retry with: mercury setup observatory --install-sidecar")
    return f"failed: {reason}"


def ensure_crypto_stack(mercury_home: str | Path | None = None) -> str:
    """Ensure the compiled crypto stack from the vendored wheels, or fail CLOSED.

    Returns one of ``"ready"`` (stack already imports),
    ``"disabled-already"`` (``observatory.e2ee: false`` set EXPLICITLY by
    the operator — nothing to do), ``"installed"`` (vendored/index wheel
    restored the stack), or ``"failed: <reason>"`` (no usable wheel, hash
    mismatch, or the install failed — E2EE stays ON, config.yaml untouched,
    retry command printed). NEVER raises and NEVER crashes the wizard:
    every failure degrades to the fail-closed status. Installs into
    ``sys.executable``'s environment (the same venv that runs the
    sidecar); needs no compiler and no container runtime."""
    try:
        from observatory.e2ee import e2ee_available, e2ee_enabled
    except Exception:  # noqa: BLE001 — import shape failure = unavailable
        e2ee_available = lambda: False  # type: ignore[assignment]  # noqa: E731
        def e2ee_enabled(_home=None) -> bool:  # type: ignore[misc]  # noqa: E306
            return True
    try:
        if not bool(e2ee_enabled(mercury_home)):
            return "disabled-already"
    except Exception:  # noqa: BLE001 — unreadable config means default-on
        pass
    try:
        if bool(e2ee_available()):
            return "ready"
    except Exception:  # noqa: BLE001 — probe failure = unavailable
        pass
    python_bin = sys.executable
    if sys.platform == "linux" and sys.version_info[:2] == (3, 13):
        try:
            cand = _vendored_olm_wheel()
        except Exception as exc:  # noqa: BLE001 — selection failure = no wheel
            return _crypto_fail_closed(f"wheel selection failed ({exc})")
        if cand is None:
            return _crypto_fail_closed(
                "no vendored python-olm wheel for this machine in "
                "hermes/observatory/wheels")
        try:
            wheel = _verified_vendored_wheel(cand)
        except ProvisionError as exc:
            return _crypto_fail_closed(str(exc))
        print(f"  → crypto stack missing — installing vendored {wheel.name} …")
        ok, detail = _crypto_pip_install(
            python_bin, ["-q", str(wheel), *_CRYPTO_PY_DEPS])
        if not ok:
            tail = detail.strip().replace("\n", " ")
            return _crypto_fail_closed(
                f"vendored install failed{': ' + tail[-300:] if tail else ''}")
    else:
        print("  → crypto stack missing — installing python-olm from the index …")
        ok, detail = _crypto_pip_install(
            python_bin, ["-q", f"python-olm=={PYTHON_OLM_VERSION}", *_CRYPTO_PY_DEPS])
        if not ok:
            tail = detail.strip().replace("\n", " ")
            return _crypto_fail_closed(
                f"index install failed{': ' + tail[-300:] if tail else ''}")
    sys.modules.pop("olm", None)
    try:
        import olm  # noqa: F401
    except Exception:  # noqa: BLE001 — installed but still unimportable
        return _crypto_fail_closed(
            "python-olm installed but does not import")
    print("  ✓ crypto stack ready (python-olm installed, no build needed).")
    return "installed"


#
# --- setup-auto orchestration (heal / converge) ----------------------------------
# `mercury setup observatory` calls these automatically after provisioning so
# the gateway room appears with zero manual follow-ups. Every helper here is
# best-effort and NEVER raises: failures degrade to a printed line + a
# "deferred" status string the wizard surfaces. Idempotent by
# construction (re-runs are no-ops when already converged).
#

def heal_owner_url(mercury_home: str | Path | None = None) -> str | None:
    """Rewrite owner-credentials.json homeserver_url to the toml bind.

    Best-effort wrapper over :func:`sync_owner_homeserver_url` for the
    setup wizard: returns the bound URL, or None when unprovisioned /
    unreadable. NEVER raises."""
    try:
        paths = ObservatoryPaths(_mercury_home(mercury_home))
        return sync_owner_homeserver_url(paths)
    except Exception:  # noqa: BLE001 — heal is best-effort
        return None


def verify_and_converge_gateway(mercury_home: str | Path | None = None) -> str:
    """Heal the owner URL, ensure the gateway node, verify its ghost and
    converge the space tree so the gateway room exists without further
    commands. Best-effort: returns ``"converged-N"`` (N applied intents),
    ``"verified-ghost-only"``, or ``"deferred: <reason>"`` (sidecar boot
    retries on its next start). NEVER raises."""
    healed = heal_owner_url(mercury_home)
    home = _mercury_home(mercury_home)
    paths = ObservatoryPaths(home)
    if not paths.toml.is_file() or not paths.owner_credentials.is_file():
        return "deferred: unprovisioned (tuwunel.toml or owner-credentials.json missing)"
    try:
        from observatory.state import ObservatoryState, StateError
    except Exception as exc:  # noqa: BLE001
        return f"deferred: state store unavailable ({exc})"
    try:
        state = ObservatoryState(paths.root / "state.db")
    except Exception as exc:  # noqa: BLE001
        return f"deferred: could not open state.db ({exc})"
    try:
        try:
            gateway_mxid = str(state.get("gw")["mxid"])
        except StateError:
            from observatory.identity import assign_slug, virtual_mxid
            cfg = _load_toml(paths.toml).get("global", {})
            server_name = str(cfg.get("server_name", config_gen.SERVER_NAME_DEFAULT))
            slug = assign_slug("gateway agent", state)
            gateway_mxid = virtual_mxid(slug, server_name=server_name)
            state.add_node(
                "gw", engine="hermes", name="gateway agent", slug=slug,
                mxid=gateway_mxid, session_ref="session:gateway",
                parent_node_id=None, extra={"kind": "gateway"},
            )
    except Exception as exc:  # noqa: BLE001
        try:
            state.close()
        except Exception:  # noqa: BLE001
            pass
        return f"deferred: gateway node ensure failed ({exc})"
    _ = healed
    bound = _bound_base_url(paths)
    if not _homeserver_reachable(bound):
        try:
            state.close()
        except Exception:  # noqa: BLE001
            pass
        return ("deferred: homeserver not reachable "
                "(sidecar converges the tree on its next start)")
    try:
        import asyncio as _asyncio
        from observatory.appservice import as_token_from_registration
        from observatory.matrix_client import MatrixClient
        from observatory.renderer import IntentExecutor, Renderer
        try:
            from observatory.e2ee import e2ee_enabled as _flag
            want_e2ee = bool(_flag(home))
        except Exception:  # noqa: BLE001
            want_e2ee = True
        doc = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
        owner_mxid, admin_token = str(doc["user_id"]), str(doc.get("access_token") or "")
        cfg = _load_toml(paths.toml).get("global", {})
        server_name = str(cfg.get("server_name", config_gen.SERVER_NAME_DEFAULT))
        as_token = as_token_from_registration(paths.appservice_registration)

        async def _converge() -> str:
            client = MatrixClient(bound, as_token, server_name=server_name,
                                  admin_token=admin_token)
            for row in state.get_live():
                localpart = str(row["mxid"]).lstrip("@").split(":", 1)[0]
                try:
                    await client.register_virtual_user(localpart)
                except Exception:  # noqa: BLE001 — best-effort per ghost
                    pass
            try:
                profile = await client.get_profile(gateway_mxid)
                ghost_ok = bool(profile)
            except Exception:  # noqa: BLE001
                ghost_ok = False
            if not ghost_ok:
                return "verified-ghost-only"
            executor: object = IntentExecutor(client, state, owner_mxid=owner_mxid,
                                             server_name=server_name)
            if want_e2ee:
                try:
                    from observatory import e2ee as _e2ee
                    if _e2ee.e2ee_available():
                        mgr = _e2ee.E2EEManager(
                            client, state,
                            crypto_dir=_e2ee.crypto_dir_for(home),
                            owner_mxid=owner_mxid, gateway_mxid=gateway_mxid)
                        await mgr.start(enabled=True)
                        executor = _e2ee.EncryptedIntentExecutor(
                            client, state, owner_mxid=owner_mxid,
                            server_name=server_name, e2ee=mgr)
                except Exception:  # noqa: BLE001 — plaintext converge beats no converge
                    pass
            renderer = Renderer(state, gateway_node_id="gw",
                                server_name=server_name, owner_mxid=owner_mxid,
                                executor=executor)  # type: ignore[arg-type]
            import socket as _socket
            applied = await renderer.apply_plan(
                renderer.build_plan(host=_socket.gethostname()))
            return f"converged-{len(applied)}"

        result = _asyncio.run(_converge())
        try:
            state.close()
        except Exception:  # noqa: BLE001
            pass
        return result
    except Exception as exc:  # noqa: BLE001 — sidecar boot retries
        try:
            state.close()
        except Exception:  # noqa: BLE001
            pass
        return f"deferred: converge failed ({exc} — sidecar retries on start)"


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

    Boot law: the binary step NEVER touches the network by default — it
    trusts the installed ``tuwunel.version`` file behind the same >=
    MIN_VERSION gate as the online path (``_refresh_tuwunel_offline``).
    A missing or stale binary fails hard with an actionable message (run
    ``mercury update`` with network, or install tuwunel manually) — boot
    never fetches implicitly, so a GitHub outage/rate-limit cannot crash
    it when the binary is already installed and current.

    ``offline``: None/True = offline (default); False = online
    latest-stable check+upgrade via the GitHub release API. False is
    reserved for explicit user actions with network (install.sh's CLI
    call, the `mercury update` first-time-provision gate) — boot/sidecar
    paths never pass it. ``fetch`` is only used on the online path.
    """
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    online = offline is False
    if online:
        action, version = tuwunel.refresh_tuwunel(paths, fetch=fetch)
    else:
        action, version = _refresh_tuwunel_offline(paths)
    summary = {
        "tuwunel": {"action": action, "version": version, "binary": str(paths.binary),
                    "offline": not online},
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
    """Boot-path binary step: trust the installed binary, never the network.

    No network of any kind: reads ``installed_version`` (binary + version
    file must BOTH exist), enforces the same >= MIN_VERSION gate as the
    online path, and reports ``("current", v)``. Raises TuwunelError with
    an actionable message when there is nothing usable to trust — boot
    never downloads, so the message tells the operator exactly how to fix
    it (``mercury update`` with network, or a manual binary install).
    """
    current = tuwunel.installed_version(paths)
    if current is None:
        raise tuwunel.TuwunelError(
            "no tuwunel binary/version file installed under "
            f"{paths.bin_dir} — boot never fetches: run `mercury update` "
            "with network access, or install the tuwunel binary manually, "
            "then restart"
        )
    try:
        tuwunel.check_min_version(f"v{current}")
    except tuwunel.TuwunelError:
        raise tuwunel.TuwunelError(
            f"installed tuwunel v{current} is below the minimum "
            f"{tuwunel.MIN_VERSION} — boot never upgrades itself: run "
            "`mercury update` with network access, or install a current "
            "tuwunel binary manually, then restart"
        ) from None
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
        return bool(obs.get("e2ee", True)) if isinstance(obs, dict) else True
    except Exception:
        return True


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
    - ``homeserver_url`` / ``homeserver_reachable`` — toml-bound URL + liveness
    - ``unit_active`` / ``unit_name`` — systemd user unit state
    - ``enabled``                 — observatory.enabled config gate (default on)
    - ``e2ee``                    — observatory.e2ee flag
    - ``observatory_dir``         — $MERCURY_HOME/observatory
    """
    paths = ObservatoryPaths(_mercury_home(mercury_home))
    config_exists = paths.toml.is_file()
    creds_exist = paths.owner_credentials.is_file()
    bound = _bound_base_url(paths)
    return {
        "provisioned": config_exists and creds_exist,
        "config_exists": config_exists,
        "binary_installed": paths.binary.is_file(),
        "owner_credentials_exist": creds_exist,
        "owner_credentials_path": str(paths.owner_credentials),
        "homeserver_url": bound,
        "homeserver_reachable": _homeserver_reachable(bound),
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
    sync_owner_homeserver_url(paths)
    return target


def current_bind_address(mercury_home: str | Path | None = None) -> str | None:
    """Current tuwunel ``address`` (first entry when bound to a list).

    None when unprovisioned or unreadable — never raises. The wizard uses
    this to detect the localhost-only trap (tailnet up, toml still on
    127.0.0.1, so phones cannot reach the homeserver).
    """
    try:
        paths = ObservatoryPaths(_mercury_home(mercury_home))
        if not paths.toml.is_file():
            return None
        address = _load_toml(paths.toml).get("global", {}).get("address")
        if isinstance(address, list):
            address = address[0] if address else None
        text = str(address or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 — display probe, never raises
        return None


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
    the ``python -m observatory.provision`` CLI. Boot-law offline: trusts
    the binary install.sh already fetched (a missing/stale binary raises
    TuwunelError with the `mercury update` remediation). Raises
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
        help="trust the installed tuwunel.version file instead of querying "
             "the GitHub release API (the library default is offline; this "
             "CLI stays online for explicit installs such as install.sh)",
    )
    args = parser.parse_args(argv)

    print("→ Matrix Observatory provisioning (Tuwunel)")
    try:
        summary = provision(
            args.mercury_home,
            args.registration_token,
            systemd=not args.no_systemd,
            owner_localpart=args.owner_localpart,
            offline=True if args.offline else False,
        )
    except (tuwunel.TuwunelError, ProvisionError) as exc:
        print(f"✗ observatory provisioning failed: {exc}")
        return 1

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
