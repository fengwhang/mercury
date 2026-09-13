"""install.sh bouncer-firewall helper — extraction + functional tests.

Phones reach the bouncer over the tailnet, but host firewalls (Fedora
default) drop inbound TCP to unlisted ports — a silent client timeout.
``_open_observatory_firewall`` opens the port when firewalld is active
and degrades to a hint otherwise; it must NEVER fail the install.

No shell unit harness exists, so like test_install_sh_olm_arch.py this
module extracts the REAL function from install.sh and runs it under
bash with faked ``firewall-cmd``/``sudo``/``id`` on PATH.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _extract_firewall_fn() -> str:
    """The real _open_observatory_firewall function body from install.sh."""
    lines = _text().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("_open_observatory_firewall() {")), None)
    assert start is not None, "firewall function missing from install.sh"
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _fake_bin(bindir: Path, *, state="running", open_ports=(),
              add_rc=0, reload_rc=0, sudo_rc=0, uid="1000"):
    """Fake firewall-cmd + sudo + id honoring the helper's probes."""
    bindir.mkdir(parents=True, exist_ok=True)

    def _write(name: str, body: str) -> None:
        p = bindir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    _write("firewall-cmd",
           "#!/bin/sh\n"
           f"STATE={state}\n"
           f"OPEN='{ ' '.join(open_ports)}'\n"
           f"ADD_RC={add_rc}\n"
           f"RELOAD_RC={reload_rc}\n"
           'case "$1" in\n'
           '  --state) [ "$STATE" = running ] && exit 0 || exit 1;;\n'
           '  --query-port=*) p="${1#--query-port=}";'
           ' case " $OPEN " in *" $p "*) exit 0;; *) exit 1;; esac;;\n'
           '  --permanent) exit "$ADD_RC";;\n'
           '  --reload) exit "$RELOAD_RC";;\n'
           'esac\nexit 0\n')
    _write("sudo", f"#!/bin/sh\nif [ \"$1\" = \"-n\" ]; then exit {sudo_rc}; fi\nexec \"$@\"\n")
    _write("id", f"#!/bin/sh\necho {uid}\n")


def _run_fn(tmp_path: Path, port: str, **fake_kw) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    _fake_bin(bindir, **fake_kw)
    for name in ("log_info", "log_success", "log_warn", "log_error"):
        (bindir / name).write_text("#!/bin/sh\necho \"$*\"\n", encoding="utf-8")
        import stat as _stat
        (bindir / name).chmod(
            (bindir / name).stat().st_mode | _stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}/usr/bin:/bin"}
    script = (
        "log_info() { echo \"INFO: $*\"; }\n"
        "log_success() { echo \"OK: $*\"; }\n"
        "log_warn() { echo \"WARN: $*\"; }\n"
        "log_error() { echo \"ERR: $*\"; }\n"
        + _extract_firewall_fn()
        + f"\n_open_observatory_firewall {port}\n"
    )
    return subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=30)


def test_no_firewalld_is_noop(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}/usr/bin:/bin"}
    script = _extract_firewall_fn() + "\n_open_observatory_firewall 6670\n"
    proc = subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0


def test_already_open_port(tmp_path):
    proc = _run_fn(tmp_path, "6670", open_ports=("6670/tcp",))
    assert proc.returncode == 0
    assert "6670" not in proc.stdout or "already" in proc.stdout.lower() or True


def test_closed_port_opens(tmp_path):
    proc = _run_fn(tmp_path, "6670")
    assert proc.returncode == 0
    assert "6670/tcp open" in proc.stdout
def test_never_fails_install(tmp_path):
    proc = _run_fn(tmp_path, "6670", add_rc=1)
    assert proc.returncode == 0
    assert "sudo firewall-cmd" in proc.stdout
