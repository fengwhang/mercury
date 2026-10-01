"""mLounge frontend for the MIRC observatory (replaces soju).

One mLounge instance per human user (``only one required per user``):
it stays connected to every mercury MIRC daemon on the tailnet as a regular
MIRC client (persistent, backlog included) and serves its web UI to the
user's browser. Adding mercury networks happens in mLounge UI —
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

MLOUNGE_PORT_DEFAULT = 9000
MLOUNGE_UNIT_NAME = "mercury-lounge.service"  # Installed unit compatibility.
MLOUNGE_UNIT_DESCRIPTION = "Mercury mLounge frontend (observatory UI)"
MLOUNGE_DIRNAME = "lounge"  # Preserve existing accounts and state layout.
FILE_MLOUNGE_CONFIG = "config.js"
NPM_PREFIX_DIRNAME = "npm"
#: npm cache lives here too — ~/.npm must stay untouched so rm -rf
#: ~/.mercury truly removes every lounge trace.
NPM_CACHE_DIRNAME = "npm-cache"
#: Vendored fork base (third_party/mlounge). The fork carries the Mercury
#: release version (package.json ``version`` + ``mercuryFork: true``
#: marker); the bundle-text patch below only applies to legacy upstream
#: installs.
#: Minified channel-join opener in the 4.5.2 bundle: the frontend opens
#: EVERY channel join, ignoring the server's shouldOpen flag (queries
#: excepted). Patched to respect it, so auto-joined agent rooms land in
#: the sidebar without stealing focus from the current chat.
FRONTEND_JOIN_OPEN = "e.chan.type===`query`&&!e.shouldOpen"
FRONTEND_JOIN_OPEN_FIXED = "!e.shouldOpen"


def patch_mlounge_frontend_text(js: str) -> tuple:
    """No-focus-steal transform for the bundle JS (pure, idempotent).

    Returns (text, changed). Absent pattern means already-patched or
    version drift — either way returns unchanged (caller goes loud).
    """
    if FRONTEND_JOIN_OPEN not in js:
        return js, False
    return js.replace(FRONTEND_JOIN_OPEN, FRONTEND_JOIN_OPEN_FIXED), True


def patch_mlounge_frontend(paths: "MLoungePaths") -> dict:
    """Apply the no-focus patch to the installed bundle (never raises).

    Runs on every provision (fresh installs patch before first start;
    live ones pick it up on browser reload via changed mtime). A state
    file records patched bundles (name + size + mtime): "current" means
    WE patched this exact file before; "pattern-missing" means the
    opener is gone under an unfamiliar shape (version drift — setup
    surfaces it loudly). Returns {"action": ...}.
    """
    try:
        import json as _json

        try:
            _ver = _json.loads(
                (paths.dir / "pkg" / "package.json").read_text(
                    encoding="utf-8")).get("version", "")
        except Exception:
            _ver = ""
        if "mercury" in str(_ver):
            return {"action": "skipped",
                    "reason": "fork bundle (no-focus fix lives in source)"}
        assets = paths.dir / "pkg" / "public" / "assets"
        state_file = paths.dir / "frontend-patch.json"
        try:
            state = _json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        if not assets.is_dir():
            return {"action": "skipped", "reason": "no installed bundle yet"}
        changed_any = False
        saw_bundles = False
        for bundle in sorted(assets.glob("index-*.js")):
            try:
                st = bundle.stat()
                key = f"{bundle.name}:{st.st_size}:{int(st.st_mtime)}"
            except Exception:
                continue
            saw_bundles = True
            if state.get(key) == "patched":
                continue
            try:
                text = bundle.read_text(encoding="utf-8")
            except Exception:
                continue
            if FRONTEND_JOIN_OPEN not in text:
                try:
                    state[key] = "pattern-missing"
                except Exception:
                    pass
                continue
            patched, changed = patch_mlounge_frontend_text(text)
            if changed:
                bundle.write_text(patched, encoding="utf-8")
                changed_any = True
                try:
                    nst = bundle.stat()
                    state[f"{bundle.name}:{nst.st_size}:{int(nst.st_mtime)}"] = "patched"
                except Exception:
                    pass
        try:
            state_file.write_text(_json.dumps(state, indent=2) + "\n",
                                  encoding="utf-8")
        except Exception:
            pass
        if not saw_bundles:
            return {"action": "skipped", "reason": "no bundle found"}
        if changed_any:
            return {"action": "patched"}
        if any(v == "pattern-missing" for v in state.values()):
            return {"action": "pattern-missing"}
        return {"action": "current"}
    except Exception as exc:
        return {"action": "failed", "reason": str(exc)[:200]}


def mlounge_npm_cache(mercury_home: str | Path | None = None) -> Path:
    """Cache dir for our npm invocations (under the observatory)."""
    from observatory.provision import _mercury_home  # local import: no cycle

    return (Path(_mercury_home(mercury_home)) / "observatory"
            / MLOUNGE_DIRNAME / NPM_CACHE_DIRNAME)


class MLoungeError(RuntimeError):
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
        raise MLoungeError(f"mLounge exec failed: {exc}") from exc


def _systemctl_available() -> bool:
    try:
        return shutil.which("systemctl") is not None
    except Exception:
        return False


class MLoungePaths:
    """Resolved mlounge layout under ``$MERCURY_HOME/observatory``."""

    def __init__(self, mercury_home: str | Path):
        self.root = Path(mercury_home).expanduser()
        self.dir = self.root / "observatory" / MLOUNGE_DIRNAME
        self.home = self.dir / "home"
        # config.js lives IN the home: that is the only file the server
        # reads ($THELOUNGE_HOME/config.js). A config anywhere else is
        # decoration — including our own pre-0.131 dir/config.js, which
        # ensure removes when it finds it.
        self.conf = self.home / FILE_MLOUNGE_CONFIG


def mlounge_prefix(mercury_home: str | Path | None = None) -> Path:
    """Our isolated npm prefix (never the system global dirs)."""
    from observatory.provision import _mercury_home  # local import: no cycle

    return Path(_mercury_home(mercury_home)) / "observatory" / MLOUNGE_DIRNAME / NPM_PREFIX_DIRNAME


def mlounge_bin(mercury_home: str | Path | None = None) -> Path:
    """Path to the ``mlounge`` binary (our npm prefix first)."""
    prefix = mlounge_prefix(mercury_home)
    for executable in ("mlounge", "thelounge"):
        ours = prefix / "bin" / executable
        if ours.is_file():
            return ours
    # Existing external deployments can still use the legacy executable.
    for executable in ("mlounge", "thelounge"):
        found = shutil.which(executable)
        if found:
            return Path(found)
    raise MLoungeError("mLounge binary not found — run mercury setup observatory")


def ensure_node() -> str:
    """Make sure Node.js + npm exist, installing via the system package
    manager when missing (needs sudo — the wizard offers first).

    Fresh computers have no Node: without this the mLounge layer can
    never provision itself. Raises MLoungeError with the manual command
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
    raise MLoungeError(
        "Node.js not found and automatic install failed — install it "
        "by hand (Fedora: sudo dnf install -y nodejs npm), then re-run setup")


#: The fork ships inside the mercury distribution with client bundle +
#: server already built on the release host (like the omp binaries), so
#: user machines never compile. Install = copy the tree, npm install
#: runtime deps only, link the bin. No upstream download links anywhere.


def _is_fork_tree(root: Path) -> bool:
    """True when *root* is the mLounge fork: explicit marker plus built
    server output. Version alone proves nothing (upstream shares the
    name); the marker is stamped by scripts/bump-version.sh."""
    try:
        pkg = root / "package.json"
        if not pkg.is_file():
            return False
        if json.loads(pkg.read_text(encoding="utf-8")).get(
                "mercuryFork") is not True:
            return False
        if not (root / "dist" / "server" / "index.js").is_file():
            return False
    except Exception:
        return False
    return True


def _fork_source_tree() -> Path | None:
    """Vendored fork tree, release-built (never user-compiled).

    Returns third_party/mlounge only when it carries the Mercury fork
    marker (package.json ``mercuryFork: true``) AND built server output
    (dist/server/index.js) — i.e. a release-host build. Otherwise None
    (dev checkouts build it via scripts/build-mlounge-fork.sh; user
    machines must update mercury).
    """
    root = Path(__file__).resolve().parents[2] / "third_party" / "mlounge"
    return root if _is_fork_tree(root) else None


def _fork_tree_version(tree: Path) -> str:
    """Fork version from the vendored tree (single source of truth)."""
    try:
        return str(json.loads((tree / "package.json").read_text(
            encoding="utf-8")).get("version", ""))
    except Exception:
        return ""


def _installed_fork_version(final: Path) -> str | None:
    """Fork version of the installed tree, or None when absent."""
    try:
        pkg = final / "package.json"
        if not pkg.is_file():
            return None
        return str(json.loads(pkg.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        return None


def _fork_fingerprint(tree: Path) -> dict | None:
    """``{fork_version, source_sha}`` from a tree's build record, else None.

    The record is written by scripts/build-mlounge-fork.sh and travels
    with the tree (shipped payload and installed copy alike)."""
    try:
        data = json.loads((tree / ".mercury-fork-build.json").read_text(
            encoding="utf-8"))
        if isinstance(data, dict) and data.get("source_sha"):
            return data
    except Exception:
        pass
    return None


def fork_staleness(mercury_home: str | Path | None = None) -> str:
    """Two-tier freshness for every reinstall path: version numbers first,
    then shasums. Returns ``"current"`` (nothing to do), ``"missing"``
    (no usable install), ``"stale-version"`` (release moved on),
    ``"stale-content"`` (same version, different sources — the
    forgotten-bump class), or ``"no-shipped"`` (no vendored tree here).
    Never raises."""
    try:
        shipped = _fork_source_tree()
        if shipped is None:
            return "no-shipped"
        want = _fork_tree_version(shipped)
        prefix = mlounge_prefix(mercury_home)
        final = prefix.parent / "pkg"
        have = _installed_fork_version(final)
        if have is None:
            return "missing"
        try:
            existing_bin = mlounge_bin(mercury_home)
        except MLoungeError:
            existing_bin = None
        if existing_bin is None or not (
                final / "node_modules" / "irc-framework").is_dir():
            return "missing"
        if have != want:
            return "stale-version"
        shipped_fp = _fork_fingerprint(shipped)
        installed_fp = _fork_fingerprint(final)
        if (shipped_fp is not None and installed_fp is not None
                and shipped_fp.get("source_sha")
                != installed_fp.get("source_sha")):
            return "stale-content"
        return "current"
    except Exception:
        return "missing"

def ensure_mlounge_installed(mercury_home: str | Path | None = None) -> str:
    """Install the vendored Mercury fork (never upstream, never compile).

    The fork ships inside the mercury distribution with its client bundle
    + server already built on the release host (like the omp binaries).
    Install here = copy the tree, ``npm install --omit=dev`` for runtime
    deps only, link the bin. Reinstalls on :func:`fork_staleness` —
    missing install, version drift, or content drift (same version,
    different sources) — which is also how a hand-installed upstream
    gets replaced by the fork. Raises with the exact state when the
    shipped tree is missing so the wizard can offer it (update mercury).
    """
    if fork_staleness(mercury_home) == "current":
        return str(mlounge_bin(mercury_home))
    shipped = _fork_source_tree()
    if shipped is None:
        raise MLoungeError(
            "no vendored mLounge fork found — update mercury to a release "
            "that ships third_party/mlounge built (dist/server/index.js)")
    prefix = mlounge_prefix(mercury_home)
    final = prefix.parent / "pkg"
    npm = shutil.which("npm")
    if npm is None:
        raise MLoungeError(
            "mLounge fork needs Node.js + npm on PATH — install Node.js, "
            "then re-run setup")
    import os as _os

    node = shutil.which("node") or ""
    path = _os.pathsep.join(
        [str(prefix / "bin"),
         str(Path(npm).parent), str(Path(node).parent) if node else "",
         _os.environ.get("PATH", "")])
    env = {"PATH": path}
    try:
        if final.exists() or final.is_symlink():
            if final.is_dir() and not final.is_symlink():
                shutil.rmtree(final)
            else:
                final.unlink()
        shutil.copytree(
            shipped, final,
            ignore=shutil.ignore_patterns("node_modules", ".git"))
    except Exception as exc:
        raise MLoungeError(f"mLounge fork stage failed: {exc}") from exc
    out = _run(
        [npm, "install", "--prefix", str(final), "--omit=dev", "--no-audit",
         "--no-fund", "--cache", str(mlounge_npm_cache(mercury_home))],
        extra_env=env, timeout=900)
    if out.returncode != 0:
        raise MLoungeError(
            "mLounge dependency install failed: "
            f"{(out.stderr or out.stdout).strip()[-3000:]}")
    link = prefix / "bin" / "mlounge"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(final / "index.js")
        import stat as _stat

        mode = final.joinpath("index.js").stat().st_mode
        final.joinpath("index.js").chmod(
            mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    except Exception as exc:
        raise MLoungeError(f"mLounge bin link failed: {exc}") from exc
    try:
        return str(mlounge_bin(mercury_home))
    except MLoungeError:
        raise MLoungeError(
            "mLounge install finished but no binary resolves") from None


def refresh_mlounge_fork(mercury_home: str | Path | None = None) -> str:
    """Reinstall the mLounge when :func:`fork_staleness` says the installed
    fork lags the shipped tree, restarting the service so the new bundle
    actually serves. The fast path (current) touches nothing — no
    reinstall, no bounce. Never raises; restart/setup print the status.
    Returns ``"current"``, ``"reinstalled"`` (missing install or version
    drift), ``"reinstalled-content"`` (same version, different sources —
    the forgotten-bump class), ``"reinstalled-no-restart: ..."`` (new
    code on disk, service bounce failed), or ``"skipped-..."``.

    FORK AUTHORS: scripts/bump-version.sh restamps the fork version on
    EVERY release, even untouched ones — the version tier only works
    because of that. The shasum tier covers the rest."""
    try:
        stale = fork_staleness(mercury_home)
        if stale == "current":
            return "current"
        if stale == "no-shipped":
            return "skipped-no-shipped-fork"
        ensure_mlounge_installed(mercury_home)
    except MLoungeError as exc:
        return f"skipped-error: {exc}"
    except Exception as exc:  # noqa: BLE001 — refresh never kills its caller
        return f"skipped-error: {exc}"
    try:
        restart_mlounge()
    except Exception as exc:  # noqa: BLE001 — code is vended; report the bounce
        return f"reinstalled-no-restart: {exc}"
    return "reinstalled-content" if stale == "stale-content" else "reinstalled"


def fork_versions(mercury_home: str | Path | None = None) -> tuple[str | None, str | None]:
    """(installed, shipped) fork versions for status surfaces. Never raises;
    None means absent (no install) or unknown (no shipped tree)."""
    try:
        prefix = mlounge_prefix(mercury_home)
        have = _installed_fork_version(prefix.parent / "pkg")
    except Exception:
        have = None
    try:
        shipped = _fork_source_tree()
        want = _fork_tree_version(shipped) if shipped is not None else None
        if want == "":
            want = None
    except Exception:
        want = None
    return have, want

def render_mlounge_config(*, host: str, port: int) -> str:
    """Render config.js (pure string templating, no I/O).

    File uploads on (drag-and-drop or the upload dialog in the web UI,
    stored under the mLounge home so reset wipes them). maxFileSize -1
    = unlimited per the official docs (code accepts <1; -1 is the
    documented value, so a future exact-check can't turn 0 into
    "0 KB allowed"). Uploads never expire server-side — prune by hand.
    """
    return f"""// Managed by `mercury setup observatory` — hand edits are overwritten.
module.exports = {{
	host: "{host}",
	port: {int(port)},
	public: false,
	theme: "default",
	fileUpload: {{
		enable: true,
		maxFileSize: -1,
	}},
}};
"""


def ensure_mlounge_config(paths: MLoungePaths, *, host: str, port: int) -> dict:
    """Write config.js when it differs. Returns {"action": ...}."""
    try:
        paths.dir.mkdir(parents=True, exist_ok=True)
        paths.home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise MLoungeError(f"mLounge dir create failed: {exc}") from exc
    try:
        stale = paths.dir / FILE_MLOUNGE_CONFIG
        if stale != paths.conf and stale.is_file():
            stale.unlink()
    except Exception:
        pass
    rendered = render_mlounge_config(host=host, port=port)
    try:
        current = paths.conf.read_text(encoding="utf-8") if paths.conf.is_file() else None
    except Exception:
        current = None
    if current == rendered:
        return {"action": "current", "path": str(paths.conf)}
    try:
        paths.conf.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        raise MLoungeError(f"mLounge config write failed: {exc}") from exc
    return {"action": "wrote" if current is None else "updated",
            "path": str(paths.conf)}


def mlounge_users(paths: MLoungePaths) -> list[str]:
    """Usernames with a stored mLounge login."""
    try:
        users_dir = paths.home / "users"
        if not users_dir.is_dir():
            return []
        return sorted(p.stem for p in users_dir.glob("*.json"))
    except Exception:
        return []


def ensure_mlounge_user(paths: MLoungePaths, username: str,
                       password: Optional[str]) -> dict:
    """Create the mLounge login via ``add --password`` (non-interactive).

    Piping answers to the interactive prompts does NOT work (the
    prompt sequence eats them and the user silently never sticks —
    verified live). ``--password`` is the supported path; the momentary
    argv exposure is confined to the single-user box. The unit's PATH
    carries node for the thelounge shebang; same here via full_env.
    Returns {"action": created|current}. Raises MLoungeError with manual
    instructions on failure.
    """
    if username in mlounge_users(paths):
        return {"action": "current"}
    try:
        (paths.home / "users").mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise MLoungeError(f"mLounge users dir create failed: {exc}") from exc
    if not password:
        raise MLoungeError(
            f"mLounge user {username!r} missing and no password given — "
            f"create it manually: thelounge --home {paths.home} add {username}")
    import os as _os

    full_env = dict(_os.environ)
    full_env["MLOUNGE_HOME"] = str(paths.home)
    full_env["THELOUNGE_HOME"] = str(paths.home)  # Legacy deployments.
    node = shutil.which("node") or ""
    if node:
        full_env["PATH"] = _os.pathsep.join(
            [str(Path(node).parent), full_env.get("PATH", "")])
    try:
        proc = subprocess.run(
            [str(mlounge_bin()), "add", "--password", password,
             "--save-logs", username],
            capture_output=True, text=True, timeout=120, env=full_env)
    except FileNotFoundError as exc:
        raise MLoungeError(f"mLounge add failed: {exc}") from exc
    if username not in mlounge_users(paths):
        raise MLoungeError(
            f"thelounge add {username!r} did not stick "
            f"({(proc.stderr or proc.stdout).strip() or proc.returncode}) — "
            f"create it manually: MLOUNGE_HOME={paths.home} "
            f"thelounge add {username}")
    return {"action": "created"}


def render_mlounge_unit(*, mlounge_bin: str, home: str, path_extra: str = "") -> str:
    """Render the mlounge systemd USER unit (pure string templating)."""
    import os as _os

    _path = _os.pathsep.join(
        [p for p in (path_extra, "/usr/local/bin:/usr/bin:/bin") if p])
    return f"""\
[Unit]
Description={MLOUNGE_UNIT_DESCRIPTION}
After=network-online.target mercury-observatory.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=MLOUNGE_HOME={home}
Environment=THELOUNGE_HOME={home}
Environment=PATH={_path}
ExecStart={mlounge_bin} start
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


def ensure_mlounge_unit(paths: MLoungePaths, *, unit: str) -> str:
    """Install/enable/start the mlounge unit. Never raises for missing
    systemd (containers/CI) — returns "skipped"."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / MLOUNGE_UNIT_NAME).write_text(unit, encoding="utf-8")
    except Exception as exc:
        raise MLoungeError(f"mLounge unit write failed: {exc}") from exc
    if not _systemctl_available():
        return "skipped"
    for args in (
        ["daemon-reload"],
        ["enable", MLOUNGE_UNIT_NAME],
        ["start", MLOUNGE_UNIT_NAME],
    ):
        out = _run(["systemctl", "--user", *args])
        if out.returncode != 0:
            raise MLoungeError(
                f"systemctl --user {' '.join(args)} failed: "
                f"{(out.stderr or out.stdout).strip()}")
    return "installed"


def restart_mlounge() -> None:
    out = _run(["systemctl", "--user", "restart", MLOUNGE_UNIT_NAME])
    if out.returncode != 0:
        raise MLoungeError(
            f"mLounge restart failed: {(out.stderr or out.stdout).strip()}")


def mlounge_unit_active() -> bool:
    try:
        out = _run(["systemctl", "--user", "is-active", MLOUNGE_UNIT_NAME])
        return (out.stdout or "").strip() == "active"
    except Exception:
        return False


def ensure_mlounge_network(
    paths: MLoungePaths,
    username: str,
    *,
    net_name: str,
    host: str,
    port: int,
    server_password: str,
    nick: str,
    channel: str,
) -> dict:
    """Pre-seed this mercury server as a mLounge network for ``username``.

    Single-install story: after setup the user opens :9000, logs in,
    and the gateway channel is already there — no manual "add network"
    step. Edits ``users/<username>.json`` (written by ``thelounge add``)
    in place, replacing any same-named network. The caller restarts the
    unit afterwards so a running mLounge picks it up. Without a server
    password the entry could never log in — skipped, never half-written.
    """
    import uuid as _uuid

    if not server_password:
        return {"action": "skipped", "reason": "no server password"}
    users_file = paths.home / "users" / f"{username}.json"
    try:
        data = json.loads(users_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MLoungeError(f"mLounge user file unreadable: {exc}") from exc
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
    for n in networks:
        if isinstance(n, dict) and n.get("name") == net_name:
            if all(n.get(k) == entry[k] for k in (
                    "host", "port", "password", "nick", "username")) and [
                    c.get("name") for c in (n.get("channels") or [])
                    if isinstance(c, dict)] == [channel]:
                return {"action": "current", "network": net_name}
    kept = [n for n in networks
            if not (isinstance(n, dict) and n.get("name") == net_name)]
    kept.append(entry)
    data["networks"] = kept
    try:
        users_file.write_text(json.dumps(data, indent=2) + "\n",
                              encoding="utf-8")
    except Exception as exc:
        raise MLoungeError(f"mLounge network seed failed: {exc}") from exc
    return {"action": "seeded", "network": net_name, "channel": channel}


def reset_mlounge_password(paths: MLoungePaths, username: str,
                          password: str) -> dict:
    """Reset a mLounge LOGIN password non-interactively.

    ``mlounge reset --password`` with MLOUNGE_HOME pointed at our
    home (verified against the real CLI). Raises MLoungeError on
    failure so the wizard surfaces it instead of silently stranding
    the user at the login page.
    """
    if not password:
        raise MLoungeError("refusing to reset to an empty password")
    users_file = paths.home / "users" / f"{username}.json"
    if not users_file.is_file():
        raise MLoungeError(f"mLounge user {username!r} does not exist")
    import os as _os

    _env: dict = {"MLOUNGE_HOME": str(paths.home), "THELOUNGE_HOME": str(paths.home)}
    _node = shutil.which("node") or ""
    if _node:
        _env["PATH"] = _os.pathsep.join(
            [str(Path(_node).parent), _os.environ.get("PATH", "")])
    out = _run(
        [str(mlounge_bin()), "reset", "--password", password, username],
        extra_env=_env, timeout=60)
    if out.returncode != 0:
        raise MLoungeError(
            "thelounge reset failed: "
            f"{(out.stderr or out.stdout).strip()}")
    if username not in mlounge_users(paths):
        raise MLoungeError(
            f"thelounge reset did not stick for {username!r}")
    return {"action": "reset", "user": username}


def provision_mlounge(
    mercury_home: str | Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = MLOUNGE_PORT_DEFAULT,
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
    """Full mLounge layer: node → binary → config → unit → user → network.

    ``host`` is the WEB UI bind (127.0.0.1 or the tailnet IP). The
    ``uplink_*`` fields pre-seed this mercury server as a mLounge
    network (same-box localhost uplink): after one setup the gateway
    channel is already in the browser, no manual add-network step.
    The unit restarts last so a running mLounge picks up the seed.
    """
    from observatory.provision import _mercury_home  # local import: no cycle

    _ = hermes_root
    home = _mercury_home(mercury_home)
    summary: dict = {"node": str(ensure_node())}
    summary["bin"] = str(ensure_mlounge_installed())
    spaths = MLoungePaths(home)
    # Sidebar without focus-steal: patch before first start (fresh) or
    # live (browser picks it up on reload). Loud on drift — a silently
    # unpatched bundle reintroduces the yank on every spawn.
    summary["frontend"] = patch_mlounge_frontend(spaths)
    # Was the unit already answering? A running server only picks up
    # user/network changes on restart (a fresh start loads them).
    was_active = mlounge_unit_active()
    summary["config"] = ensure_mlounge_config(spaths, host=host, port=int(port))
    import os as _os2

    _node = shutil.which("node") or ""
    # `add` only CREATES: an existing login silently keeps its old
    # password, so a freshly-typed password would never take effect
    # (the "correct password rejected" trap). A given password ALWAYS
    # wins — reset onto the existing account.
    summary["user"] = ensure_mlounge_user(spaths, username, password)
    if summary["user"].get("action") == "current" and password:
        summary["user"] = reset_mlounge_password(spaths, username, password)
    if uplink_name and uplink_channel:
        summary["network"] = ensure_mlounge_network(
            spaths, username,
            net_name=uplink_name, host=uplink_host, port=int(uplink_port),
            server_password=uplink_password,
            nick=uplink_nick or username, channel=uplink_channel)
    summary["unit"] = ensure_mlounge_unit(
        spaths,
        unit=render_mlounge_unit(
            mlounge_bin=summary["bin"], home=str(spaths.home),
            path_extra=_os2.pathsep.join(
                [str(mlounge_prefix(home) / "bin"),
                 str(Path(_node).parent)] if _node else
                [str(mlounge_prefix(home) / "bin")])))
    if was_active:
        restart_mlounge()
    return summary


#: Denied staging sources for agent-uploaded files (mirrors the
#: gateway MEDIA pipeline denylist in gateway/platforms/base.py: system
#: dirs, credential homes, and secret-looking basenames). An agent that
#: could stage /etc/passwd or a token file would turn the share link
#: into an exfil channel.
UPLOAD_DENIED_PREFIXES = ("/etc", "/proc", "/sys", "/dev", "/root",
                          "/boot", "/var/log")
UPLOAD_DENIED_HOME_PARTS = (".ssh", ".aws", ".gnupg", ".kube", ".docker",
                            ".config", ".azure", ".gcloud")
UPLOAD_DENIED_BASENAMES = (".env",)


def stage_mlounge_upload(mercury_home: str | Path | None,
                        src: str | Path) -> dict:
    """Stage a local file as a mLounge upload; return link parts.

    Layout mirrors the server's own scheme (``uploads/<2hex>/<16hex>``,
    original name only in the URL), so the file is served verbatim
    without touching the upload API. Returns {"url_path", "filename"}.
    Raises MLoungeError on missing/non-file/denied sources.
    """
    import secrets as _secrets
    import shutil as _shutil
    import urllib.parse as _urlparse

    from observatory.provision import _mercury_home  # local import: no cycle

    raw = str(src or "")
    if not raw:
        raise MLoungeError("no file path given")
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except Exception:
        raise MLoungeError(f"file not found: {raw[:200]}") from None
    if not resolved.is_file():
        raise MLoungeError(f"not a regular file: {raw[:200]}")
    # Check both the requested spelling and the physical target: protected
    # directories themselves can be symlinks (including /etc on NixOS).
    requested = Path(raw).expanduser().absolute()
    protected = [Path(prefix) for prefix in UPLOAD_DENIED_PREFIXES]
    protected += [Path.home() / name for name in UPLOAD_DENIED_HOME_PARTS]
    for boundary in protected:
        physical_boundary = boundary.resolve()
        if (requested.is_relative_to(boundary)
                or resolved.is_relative_to(boundary)
                or resolved.is_relative_to(physical_boundary)):
            raise MLoungeError(f"refusing protected path: {raw[:200]}")
    lowered = resolved.name.lower()
    if lowered in UPLOAD_DENIED_BASENAMES or lowered.endswith((".key", ".pem")):
        raise MLoungeError(f"refusing secret-looking file: {raw[:200]}")
    try:
        from observatory.provision import _mercury_home as _mh

        mhome = str(Path(_mh(mercury_home)).expanduser().resolve())
        uploads = str((MLoungePaths(_mh(mercury_home)).home / "uploads"))
        if str(resolved).startswith(mhome + "/") and not str(resolved).startswith(uploads + "/"):
            raise MLoungeError(f"refusing mercury-home file: {raw[:200]}")
    except MLoungeError:
        raise
    except Exception:
        pass
    token = _secrets.token_hex(8)
    dest_dir = (MLoungePaths(_mercury_home(mercury_home)).home / "uploads"
                / token[:2])
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / token
        _shutil.copyfile(resolved, dest)
    except Exception as exc:
        raise MLoungeError(f"upload stage failed: {exc}") from exc
    name = resolved.name[:128] or "file"
    return {"url_path": f"uploads/{token}/{_urlparse.quote(name)}",
            "filename": name}


def check_upload_serves(url: str, timeout: float = 5.0) -> bool:
    """Best-effort GET check that a staged link actually serves.

    Catches the embarrassing post-a-dead-link case (server mid-restart,
    wrong bind). Never raises; a failed check means "don't post".
    """
    try:
        import urllib.request as _urlopen

        with _urlopen.urlopen(url, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:
        return False


def mlounge_base_url(mercury_home: str | Path | None = None) -> str:
    """Public base URL of this box's mLounge (for staged file links)."""
    st = status_mlounge(mercury_home)
    host = str(st.get("host") or "127.0.0.1")
    try:
        port = int(st.get("port") or MLOUNGE_PORT_DEFAULT)
    except (TypeError, ValueError):
        port = MLOUNGE_PORT_DEFAULT
    return f"http://{host}:{port}"


def mlounge_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """True when something answers on the mLounge web-UI port (never raises)."""
    import socket as _socket

    try:
        with _socket.create_connection((str(host), int(port)),
                                       timeout=timeout):
            return True
    except Exception:
        return False


def _local_port_answers(port: int, timeout: float = 0.5) -> bool:
    """True when our own box answers on ``port`` (any local address).

    Localhost alone misses tailnet-bound services (a mLounge listening
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
    return any(mlounge_port_open(host, port, timeout=timeout)
               for host in candidates)


def status_mlounge(mercury_home: str | Path | None = None) -> dict:
    """Best-effort mLounge status for setup/status surfaces (never raises)."""
    from observatory.provision import _mercury_home  # local import: no cycle

    try:
        home = _mercury_home(mercury_home)
        spaths = MLoungePaths(home)
        conf = str(spaths.conf) if spaths.conf.is_file() else ""
        try:
            binary = str(mlounge_bin())
        except MLoungeError:
            binary = ""
        host, port = _read_mlounge_bind(spaths)
        configured = bool(conf)
        return {
            "configured": configured,
            "binary": binary,
            "users": mlounge_users(spaths),
            "unit": "active" if mlounge_unit_active() else "inactive",
            "host": host,
            "port": port,
            # A mLounge the user runs themselves (container, another
            # box's install): our config is absent but the port answers
            # on some local address (localhost OR tailnet-bound).
            "external": (not configured) and _local_port_answers(
                MLOUNGE_PORT_DEFAULT),
        }
    except Exception:
        return {"configured": False, "binary": "", "users": [],
                "unit": "unknown", "host": "", "port": 0,
                "external": False}


def _read_mlounge_bind(spaths: "MLoungePaths") -> tuple:
    """Bound web-UI host/port parsed from config.js (best effort)."""
    import re as _re

    try:
        conf = spaths.conf.read_text(encoding="utf-8", errors="replace")
        host = _re.search(r'host:\s*"([^"]+)"', conf)
        port = _re.search(r"port:\s*(\d+)", conf)
        return (host.group(1) if host else "127.0.0.1",
                int(port.group(1)) if port else MLOUNGE_PORT_DEFAULT)
    except Exception:
        return "127.0.0.1", MLOUNGE_PORT_DEFAULT

# Compatibility aliases for existing extensions; implementation uses the fork names.
LOUNGE_PORT_DEFAULT = MLOUNGE_PORT_DEFAULT
LOUNGE_UNIT_NAME = MLOUNGE_UNIT_NAME
LOUNGE_UNIT_DESCRIPTION = MLOUNGE_UNIT_DESCRIPTION
LOUNGE_DIRNAME = MLOUNGE_DIRNAME
FILE_LOUNGE_CONFIG = FILE_MLOUNGE_CONFIG
patch_lounge_frontend_text = patch_mlounge_frontend_text
patch_lounge_frontend = patch_mlounge_frontend
lounge_npm_cache = mlounge_npm_cache
LoungeError = MLoungeError
LoungePaths = MLoungePaths
lounge_prefix = mlounge_prefix
lounge_bin = mlounge_bin
ensure_lounge_installed = ensure_mlounge_installed
refresh_lounge_fork = refresh_mlounge_fork
render_lounge_config = render_mlounge_config
ensure_lounge_config = ensure_mlounge_config
lounge_users = mlounge_users
ensure_lounge_user = ensure_mlounge_user
render_lounge_unit = render_mlounge_unit
ensure_lounge_unit = ensure_mlounge_unit
restart_lounge = restart_mlounge
lounge_unit_active = mlounge_unit_active
ensure_lounge_network = ensure_mlounge_network
reset_lounge_password = reset_mlounge_password
provision_lounge = provision_mlounge
stage_lounge_upload = stage_mlounge_upload
lounge_base_url = mlounge_base_url
lounge_port_open = mlounge_port_open
status_lounge = status_mlounge
_read_lounge_bind = _read_mlounge_bind
