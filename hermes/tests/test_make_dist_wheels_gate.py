"""make-dist.sh observatory wheels gate — a release must not ship broken E2EE.

python-olm has no cp313 wheel on PyPI; the release tarball therefore
bundles the crypto stack under wheels/ (staged from $MERCURY_WHEELS_DIR or
hermes/observatory/scripts/dist). The gate inside build_one:

* observatory code in the archive + NO wheels found + MERCURY_SKIP_OBS_WHEELS
  unset -> LOUD FAILURE (exit 1) — the release host cannot silently cut a
  release that bricks e2ee on existing installs;
* MERCURY_SKIP_OBS_WHEELS=1 -> builds with a warning;
* wheels staged -> they land in the tarball under mercury/wheels/.

Runs the REAL script in a sandbox git repo (make-dist stages `git archive
HEAD` + injects gitignored prebuilt artifacts).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKE_DIST = REPO_ROOT / "scripts" / "make-dist.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for make-dist sandbox")


def _sandbox_repo(tmp_path: Path) -> Path:
    """Minimal committed repo + the gitignored prebuilts make-dist expects."""
    repo = tmp_path / "repo"
    obs = repo / "hermes" / "observatory"
    obs.mkdir(parents=True)
    (obs / "provision.py").write_text("# observatory\n", encoding="utf-8")
    cli = repo / "hermes" / "mercury_cli"
    cli.mkdir(parents=True)
    (cli / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    (repo / "scripts").mkdir()
    shutil.copy2(MAKE_DIST, repo / "scripts" / "make-dist.sh")
    # gitignored prebuilts injected by the release host
    omp = repo / "omp" / "packages" / "coding-agent" / "dist"
    omp.mkdir(parents=True)
    machine = platform.machine().lower()
    magic = 183 if machine in ("aarch64", "arm64") else 62
    omp_bin = omp / "omp"
    omp_bin.write_bytes(b"\x7fELF" + b"\x00" * 14 + bytes([magic]) + b"\x00" * 8)
    omp_bin.chmod(0o755)
    ui = repo / "hermes" / "ui-tui" / "dist"
    ui.mkdir(parents=True)
    (ui / "entry.js").write_text("// tui\n", encoding="utf-8")

    git = ["git", "-c", "user.email=t@test", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q"], cwd=repo, check=True)
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-qm", "sandbox"], cwd=repo, check=True)
    return repo


def _run_make_dist(repo: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "MERCURY_VERSION": "9.9.9", **extra_env}
    if "MERCURY_SKIP_OBS_WHEELS" not in extra_env:
        env.pop("MERCURY_SKIP_OBS_WHEELS", None)
    if "MERCURY_WHEELS_DIR" not in extra_env:
        env.pop("MERCURY_WHEELS_DIR", None)
    return subprocess.run(
        ["bash", str(repo / "scripts" / "make-dist.sh")],
        cwd=repo, env=env, capture_output=True, text=True, timeout=120,
    )


def _x64_host() -> None:
    if platform.machine().lower() not in ("x86_64", "amd64"):
        pytest.skip("sandbox only prebuilt the x64 omp binary")


def test_archive_with_observatory_and_no_wheels_fails_loudly(tmp_path):
    repo = _sandbox_repo(tmp_path)
    proc = _run_make_dist(repo, {})
    assert proc.returncode != 0, "missing crypto-stack wheels must fail the release"
    assert "FATAL" in proc.stderr
    assert "observatory" in proc.stderr
    assert "MERCURY_SKIP_OBS_WHEELS" in proc.stderr  # remediation is printed


def test_skip_env_var_builds_with_warning(tmp_path):
    repo = _sandbox_repo(tmp_path)
    proc = _run_make_dist(repo, {"MERCURY_SKIP_OBS_WHEELS": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and "wheels" in proc.stderr
    assert (repo / "dist" / "mercury-9.9.9-x64.tar.gz").exists() or \
           (repo / "dist" / "mercury-9.9.9-arm64.tar.gz").exists()


def test_staged_wheels_land_in_tarball(tmp_path):
    repo = _sandbox_repo(tmp_path)
    wheels = repo / "dist" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl").write_bytes(b"PK")
    (wheels / "mautrix-0.21.1-py3-none-any.whl").write_bytes(b"PK")
    proc = _run_make_dist(repo, {})
    assert proc.returncode == 0, proc.stderr
    _x64_host()
    with tarfile.open(repo / "dist" / "mercury-9.9.9-x64.tar.gz", "r:gz") as tf:
        names = tf.getnames()
    assert "mercury/wheels/python_olm-3.2.16-cp313-cp313-linux_x86_64.whl" in names
    assert "mercury/wheels/mautrix-0.21.1-py3-none-any.whl" in names


def test_e2ee_script_dist_wheels_also_bundle(tmp_path):
    """Wheels freshly built by build_python_olm_wheel.sh (its output dir)
    are picked up even without explicit staging."""
    repo = _sandbox_repo(tmp_path)
    script_dist = repo / "hermes" / "observatory" / "scripts" / "dist"
    script_dist.mkdir(parents=True)
    (script_dist / "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl").write_bytes(b"PK")
    proc = _run_make_dist(repo, {})
    assert proc.returncode == 0, proc.stderr
    _x64_host()
    with tarfile.open(repo / "dist" / "mercury-9.9.9-x64.tar.gz", "r:gz") as tf:
        assert "mercury/wheels/python_olm-3.2.16-cp313-cp313-linux_x86_64.whl" in tf.getnames()


def test_staged_dir_wins_on_name_clash(tmp_path):
    repo = _sandbox_repo(tmp_path)
    script_dist = repo / "hermes" / "observatory" / "scripts" / "dist"
    script_dist.mkdir(parents=True)
    (script_dist / "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl").write_bytes(b"OLD")
    staged = repo / "dist" / "wheels"
    staged.mkdir(parents=True)
    (staged / "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl").write_bytes(b"NEW-STAGED")
    proc = _run_make_dist(repo, {})
    assert proc.returncode == 0, proc.stderr
    _x64_host()
    with tarfile.open(repo / "dist" / "mercury-9.9.9-x64.tar.gz", "r:gz") as tf:
        member = "mercury/wheels/python_olm-3.2.16-cp313-cp313-linux_x86_64.whl"
        assert tf.extractfile(member).read() == b"NEW-STAGED"


def test_both_arch_olm_wheels_stage_together(tmp_path):
    """Both-arch olm wheels stay staged in the tarball; arch selection is
    the INSTALLER's job (install.sh arch filter + update_release
    _split_bundled_wheels), not the packager's. Regression: trimming the
    tarball to one arch would re-break the other arch's offline install."""
    repo = _sandbox_repo(tmp_path)
    wheels = repo / "dist" / "wheels"
    wheels.mkdir(parents=True)
    (wheels / "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl").write_bytes(b"PK-X64")
    (wheels / "python_olm-3.2.16-cp313-cp313-linux_aarch64.whl").write_bytes(b"PK-ARM")
    (wheels / "mautrix-0.21.1-py3-none-any.whl").write_bytes(b"PK")
    proc = _run_make_dist(repo, {})
    assert proc.returncode == 0, proc.stderr
    with tarfile.open(repo / "dist" / "mercury-9.9.9-x64.tar.gz", "r:gz") as tf:
        names = tf.getnames()
    assert "mercury/wheels/python_olm-3.2.16-cp313-cp313-linux_x86_64.whl" in names
    assert "mercury/wheels/python_olm-3.2.16-cp313-cp313-linux_aarch64.whl" in names
    assert "mercury/wheels/mautrix-0.21.1-py3-none-any.whl" in names
