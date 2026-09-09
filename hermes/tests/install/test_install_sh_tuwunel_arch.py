"""install.sh vendored tuwunel binary — guards + functional selector/installer tests.

Virgin installs provision --offline (zero GitHub requests) but the tarball
used to ship NO tuwunel binary — only the fetcher — so there was nothing
to trust and provision failed with 'run mercury update'. The tarball now
stages the matching-arch raw binary under tuwunel-binaries/ (see
 _stage_tuwunel_binary in scripts/make-dist.sh); install.sh hash-verifies
and installs it BEFORE provision --offline.

No shell unit harness exists, so this module pairs static source guards
(install runs before the provision call; never downgrades; sets the
executable bit) with functional tests that extract the REAL
_select/_verify/_install_vendored_tuwunel functions from install.sh and
run them under bash with a faked `uname -m` (same pattern as
test_install_sh_olm_arch.py).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
VENDORED_DIR = REPO_ROOT / "hermes" / "observatory" / "tuwunel-binaries"


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _extract_tuwunel_funcs() -> str:
    """The real _select/_verify/_install_vendored_tuwunel functions."""
    lines = _text().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("_select_vendored_tuwunel() {")), None)
    assert start is not None, "tuwunel selector missing from install.sh"
    stop = next((i for i, ln in enumerate(lines)
                 if ln.startswith("install_observatory() {")), None)
    assert stop is not None and stop > start
    end = max(i for i in range(start, stop) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


_STUBS = (
    "log_info() { echo \"INFO: $1\"; }\n"
    "log_warn() { echo \"WARN: $1\" >&2; }\n"
    "log_success() { echo \"OK: $1\"; }\n"
    "log_error() { echo \"ERR: $1\" >&2; }\n"
)


def _run_bash(script: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=60)


def _fake_uname_env(tmp_path: Path, machine: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake_uname = bindir / "uname"
    fake_uname.write_text(f'#!/bin/sh\necho "{machine}"\n', encoding="utf-8")
    fake_uname.chmod(fake_uname.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}


def _staged_dir(tmp_path: Path) -> Path:
    d = tmp_path / "staged"
    d.mkdir()
    (d / "tuwunel-x64").write_bytes(b"\x7fELF-fake-x64")
    (d / "tuwunel-arm64").write_bytes(b"\x7fELF-fake-arm64")
    (d / "VERSION").write_text("1.9.0\n", encoding="utf-8")
    pins = "\n".join(
        f"{hashlib.sha256((d / n).read_bytes()).hexdigest()}  {n}"
        for n in ("tuwunel-x64", "tuwunel-arm64")
    ) + "\n"
    (d / "SHA256SUMS").write_text(pins, encoding="utf-8")
    return d


# --- source guards -----------------------------------------------------------


def test_install_runs_before_provision_offline():
    body = _text()
    assert "_install_vendored_tuwunel" in body
    assert body.index("_install_vendored_tuwunel") < body.index("-m observatory.provision")


def test_install_never_downgrades_and_sets_executable():
    body = _text()
    assert "already installed" in body and "keeping it" in body
    assert "sort -V" in body  # version comparison against the installed binary
    assert "chmod 755" in body


def test_staged_hash_checked_with_venv_python_not_sha256sum():
    funcs = _extract_tuwunel_funcs()
    assert "_verify_vendored_tuwunel" in funcs
    assert "SHA256SUMS" in funcs
    # sha256sum is absent on macOS without coreutils (same law as the wheels
    # verifier); the tuwunel functions must never INVOKE it (mentions in
    # comments are fine).
    invocations = [ln for ln in funcs.splitlines()
                   if "sha256sum" in ln and not ln.strip().startswith("#")]
    assert invocations == [], f"sha256sum invoked: {invocations}"


def test_install_sh_still_parses():
    if shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("no bash")
    proc = subprocess.run(["bash", "-n", str(INSTALL_SH)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


# --- functional: the real selector under faked uname -m ----------------------


@pytest.mark.parametrize(("machine", "want"), [
    ("x86_64", "tuwunel-x64"),
    ("amd64", "tuwunel-x64"),
    ("aarch64", "tuwunel-arm64"),
    ("arm64", "tuwunel-arm64"),
])
def test_selector_picks_this_arch(tmp_path, machine, want):
    proc = _run_bash(
        _extract_tuwunel_funcs() + f'\n_select_vendored_tuwunel "{_staged_dir(tmp_path)}"\n',
        _fake_uname_env(tmp_path, machine),
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()).name == want


def test_selector_unknown_arch_selects_nothing(tmp_path):
    proc = _run_bash(
        _extract_tuwunel_funcs() + f'\n_select_vendored_tuwunel "{_staged_dir(tmp_path)}"\n',
        _fake_uname_env(tmp_path, "riscv64"),
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


def test_selector_missing_binary_selects_nothing(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    proc = _run_bash(
        _extract_tuwunel_funcs() + f'\n_select_vendored_tuwunel "{d}"\n',
        _fake_uname_env(tmp_path, "x86_64"),
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


# --- functional: the real installer ------------------------------------------


def _run_install(staged: Path, fallback: Path, home: Path,
                 machine: str, tmp_path: Path) -> subprocess.CompletedProcess:
    script = (
        _STUBS + _extract_tuwunel_funcs()
        + f'\n_install_vendored_tuwunel "{staged}" "{fallback}" "{sys.executable}"\n'
    )
    env = _fake_uname_env(tmp_path, machine)
    env["MERCURY_HOME"] = str(home)
    return _run_bash(script, env)


def test_install_places_binary_and_version(tmp_path):
    staged = _staged_dir(tmp_path)
    home = tmp_path / "home"
    proc = _run_install(staged, tmp_path / "nofallback", home, "x86_64", tmp_path)
    assert proc.returncode == 0, proc.stderr
    binary = home / "observatory" / "bin" / "tuwunel"
    version = home / "observatory" / "bin" / "tuwunel.version"
    assert binary.read_bytes() == b"\x7fELF-fake-x64"
    assert version.read_text(encoding="utf-8") == "1.9.0\n"
    assert os.access(binary, os.X_OK)


def test_install_never_downgrades_newer_binary(tmp_path):
    staged = _staged_dir(tmp_path)
    home = tmp_path / "home"
    dest = home / "observatory" / "bin"
    dest.mkdir(parents=True)
    (dest / "tuwunel").write_bytes(b"\x7fELF-newer")
    (dest / "tuwunel.version").write_text("9.9.9\n", encoding="utf-8")
    proc = _run_install(staged, tmp_path / "nofallback", home, "x86_64", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (dest / "tuwunel").read_bytes() == b"\x7fELF-newer"
    assert (dest / "tuwunel.version").read_text(encoding="utf-8") == "9.9.9\n"
    assert "keeping it" in proc.stdout


def test_install_upgrades_stale_binary(tmp_path):
    staged = _staged_dir(tmp_path)
    home = tmp_path / "home"
    dest = home / "observatory" / "bin"
    dest.mkdir(parents=True)
    (dest / "tuwunel").write_bytes(b"\x7fELF-stale")
    (dest / "tuwunel.version").write_text("1.8.1\n", encoding="utf-8")
    proc = _run_install(staged, tmp_path / "nofallback", home, "x86_64", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (dest / "tuwunel").read_bytes() == b"\x7fELF-fake-x64"
    assert (dest / "tuwunel.version").read_text(encoding="utf-8") == "1.9.0\n"


def test_install_hash_mismatch_warns_without_install(tmp_path):
    staged = _staged_dir(tmp_path)
    (staged / "tuwunel-x64").write_bytes(b"\x7fELF-tampered")
    home = tmp_path / "home"
    proc = _run_install(staged, tmp_path / "nofallback", home, "x86_64", tmp_path)
    assert proc.returncode == 0, proc.stderr  # warn-not-die
    assert not (home / "observatory" / "bin" / "tuwunel").exists()
    assert "hash check" in proc.stderr


def test_install_unknown_arch_warns_without_install(tmp_path):
    staged = _staged_dir(tmp_path)
    home = tmp_path / "home"
    proc = _run_install(staged, tmp_path / "nofallback", home, "riscv64", tmp_path)
    assert proc.returncode == 0, proc.stderr  # warn-not-die; provision fails hard later
    assert not (home / "observatory" / "bin" / "tuwunel").exists()
