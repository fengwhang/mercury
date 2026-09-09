"""make-dist.sh baked-version gate — a release must not ship a stale omp binary.

The omp binary bakes MERCURY_VERSION at COMPILE time (compile-binary.ts
resolveMercuryVersion reads hermes/mercury_cli/__init__.py __version__).
v0.0.19 shipped omp/0.0.18 because the build ran BEFORE the __version__
bump (build -> bump -> pack). make-dist.sh therefore fail-hards: the
binary's baked user-agent (extracted via `strings`) must contain
omp/<VERSION>-mercury, and MERCURY_VERSION must agree with __version__.

Runs the REAL script in a sandbox git repo (make-dist stages `git archive
HEAD` + injects gitignored prebuilt artifacts).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAKE_DIST = REPO_ROOT / "scripts" / "make-dist.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for make-dist sandbox")

VERSION = "9.9.9"
STALE_VERSION = "9.9.8"


def _sandbox_repo(tmp_path: Path, baked_version: str | None) -> Path:
    """Minimal committed repo + a fixture omp binary baking `baked_version`.

    `baked_version=None` writes a binary with no user-agent string at all.
    """
    repo = tmp_path / "repo"
    obs = repo / "hermes" / "observatory"
    obs.mkdir(parents=True)
    (obs / "provision.py").write_text("# observatory\n", encoding="utf-8")
    cli = repo / "hermes" / "mercury_cli"
    cli.mkdir(parents=True)
    (cli / "__init__.py").write_text(f'__version__ = "{VERSION}"\n', encoding="utf-8")
    (repo / "scripts").mkdir()
    shutil.copy2(MAKE_DIST, repo / "scripts" / "make-dist.sh")
    # gitignored prebuilt injected by the release host
    omp = repo / "omp" / "packages" / "coding-agent" / "dist"
    omp.mkdir(parents=True)
    machine = platform.machine().lower()
    magic = 183 if machine in ("aarch64", "arm64") else 62
    payload = b"\x7fELF" + b"\x00" * 14 + bytes([magic]) + b"\x00" * 8
    if baked_version is not None:
        payload += f"omp/{baked_version}-mercury".encode()
    omp_bin = omp / "omp"
    omp_bin.write_bytes(payload)
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

    env = {**os.environ, "MERCURY_VERSION": VERSION, **extra_env}
    if "MERCURY_SKIP_OBS_WHEELS" not in extra_env:
        env.pop("MERCURY_SKIP_OBS_WHEELS", None)
    if "MERCURY_WHEELS_DIR" not in extra_env:
        env.pop("MERCURY_WHEELS_DIR", None)
    return subprocess.run(
        ["bash", str(repo / "scripts" / "make-dist.sh")],
        cwd=repo, env=env, capture_output=True, text=True, timeout=120,
    )


def test_matching_binary_packs(tmp_path):
    repo = _sandbox_repo(tmp_path, VERSION)
    proc = _run_make_dist(repo, {"MERCURY_SKIP_OBS_WHEELS": "1"})
    assert proc.returncode == 0, proc.stderr
    assert f"omp binary version OK (omp/{VERSION}-mercury)" in proc.stdout + proc.stderr
    assert (repo / "dist" / f"mercury-{VERSION}-x64.tar.gz").exists() or \
           (repo / "dist" / f"mercury-{VERSION}-arm64.tar.gz").exists()


def test_stale_binary_refuses_pack(tmp_path):
    """Fixture: binary with the PREVIOUS release's user-agent (v0.0.19 lesson)."""
    repo = _sandbox_repo(tmp_path, STALE_VERSION)
    proc = _run_make_dist(repo, {"MERCURY_SKIP_OBS_WHEELS": "1"})
    assert proc.returncode != 0, "stale baked version must fail the pack"
    assert "FATAL" in proc.stderr
    assert "baked version mismatch" in proc.stderr
    assert f"omp/{STALE_VERSION}-mercury" in proc.stderr  # names the stale build
    assert not (repo / "dist" / f"mercury-{VERSION}-x64.tar.gz").exists()


def test_binary_without_user_agent_refuses_pack(tmp_path):
    repo = _sandbox_repo(tmp_path, None)
    proc = _run_make_dist(repo, {"MERCURY_SKIP_OBS_WHEELS": "1"})
    assert proc.returncode != 0, "binary with no user-agent must fail the pack"
    assert "FATAL" in proc.stderr
    assert "baked version mismatch" in proc.stderr


def test_env_override_drift_refuses_pack(tmp_path):
    """MERCURY_VERSION disagreeing with __version__ is a build-order violation."""
    repo = _sandbox_repo(tmp_path, VERSION)
    proc = _run_make_dist(repo, {"MERCURY_VERSION": "9.9.10", "MERCURY_SKIP_OBS_WHEELS": "1"})
    assert proc.returncode != 0, "env/file version drift must fail the pack"
    assert "FATAL" in proc.stderr
    assert "MERCURY_VERSION" in proc.stderr


def test_stale_arm64_binary_refuses_pack(tmp_path):
    """Per-arch gate: a stale second binary fails even when x64 is fresh."""
    repo = _sandbox_repo(tmp_path, VERSION)
    arm64 = repo / "omp" / "packages" / "coding-agent" / "dist" / "omp-linux-arm64"
    arm64.write_bytes(b"\x7fELF" + b"\x00" * 32 + f"omp/{STALE_VERSION}-mercury".encode())
    arm64.chmod(0o755)
    proc = _run_make_dist(repo, {"MERCURY_SKIP_OBS_WHEELS": "1"})
    assert proc.returncode != 0, "stale arm64 binary must fail the pack"
    assert "FATAL" in proc.stderr
    assert "aarch64" in proc.stderr
