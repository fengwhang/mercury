"""install.sh python-olm arch selection — guards + functional selector tests.

Root cause (v0.0.19 VM log): install.sh handed pip an UNFILTERED
``python_olm-*.whl`` glob expanding to BOTH arch wheels (x86_64 + aarch64),
so pip got two conflicting python-olm URLs — in the vendored step and in
the bundled-wheels step (make-dist stages both arches by design).

No shell unit harness exists (tests/install holds only the bubblewrap
e2e), so this module pairs static source guards (no raw olm glob may
reach a ``pip install`` line) with functional tests that extract the REAL
``_select_vendored_olm_wheel`` function from install.sh and run it under
bash with a faked ``uname -m``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

X64 = "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl"
ARM = "python_olm-3.2.16-cp313-cp313-linux_aarch64.whl"


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _extract_selector() -> str:
    """The real _select_vendored_olm_wheel function body from install.sh."""
    lines = _text().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("_select_vendored_olm_wheel() {")), None)
    assert start is not None, "selector function missing from install.sh"
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _run_selector(wheels: Path, machine: str) -> subprocess.CompletedProcess:
    """Run the extracted selector with a faked `uname -m`."""
    bindir = wheels.parent / "bin"
    bindir.mkdir(exist_ok=True)
    fake_uname = bindir / "uname"
    fake_uname.write_text(f'#!/bin/sh\necho "{machine}"\n', encoding="utf-8")
    fake_uname.chmod(fake_uname.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    script = _extract_selector() + f'\n_select_vendored_olm_wheel "{wheels}"\n'
    return subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=30)


def _wheels_dir(tmp_path: Path) -> Path:
    d = tmp_path / "wheels"
    d.mkdir()
    (d / X64).write_bytes(b"PK")
    (d / ARM).write_bytes(b"PK")
    (d / "mautrix-0.21.1-py3-none-any.whl").write_bytes(b"PK")
    return d


# --- source guards: no unfiltered olm glob may reach pip --------------------

def test_no_unfiltered_olm_glob_reaches_pip():
    offenders = [ln.strip() for ln in _text().splitlines()
                 if "pip install" in ln and "python_olm-*.whl" in ln]
    assert offenders == [], f"unfiltered olm glob reaches pip: {offenders}"


def test_no_raw_wheel_glob_on_any_pip_install_line():
    offenders = [ln.strip() for ln in _text().splitlines()
                 if "pip install" in ln and "*.whl" in ln]
    assert offenders == [], f"raw wheel glob passed to pip: {offenders}"


def test_vendored_step_installs_single_selected_path():
    body = _text()
    assert "_select_vendored_olm_wheel" in body
    assert '"$VENDORED_WHEEL"' in body  # quoted single path, never a glob


def test_bundled_step_skips_foreign_arch_with_notice():
    body = _text()
    assert "skipping foreign-arch bundled wheel" in body
    assert "python_olm-*.whl)" in body  # case filter, not a pip argument


def test_install_sh_still_parses():
    if shutil.which("bash") is None:
        pytest.skip("bash required")
    proc = subprocess.run(["bash", "-n", str(INSTALL_SH)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


# --- functional: the real selector under faked uname -m ---------------------

@pytest.mark.parametrize(("machine", "want"), [
    ("x86_64", X64), ("amd64", X64),
    ("aarch64", ARM), ("arm64", ARM),
])
def test_selector_picks_this_arch(tmp_path, machine, want):
    proc = _run_selector(_wheels_dir(tmp_path), machine)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()).name == want


def test_selector_unknown_arch_skips_silently(tmp_path):
    proc = _run_selector(_wheels_dir(tmp_path), "riscv64")
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


def test_selector_no_matching_wheel_fails(tmp_path):
    d = tmp_path / "wheels"
    d.mkdir()
    (d / ARM).write_bytes(b"PK")
    proc = _run_selector(d, "x86_64")
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
