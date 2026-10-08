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
import struct
import json
import hashlib
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
    interp = b"/lib/ld-linux-aarch64.so.1\0" if magic == 183 else b"/lib64/ld-linux-x86-64.so.2\0"
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, magic)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    payload = bytes(header) + struct.pack("<IIQQQQQQ", 3, 0, 120, 0, 0, len(interp), len(interp), 1) + interp
    if baked_version is not None:
        payload += f"omp/{baked_version}-mercury".encode()
    omp_bin = omp / "omp"
    omp_bin.write_bytes(payload)
    omp_bin.chmod(0o755)
    ui = repo / "hermes" / "ui-tui" / "dist"
    ui.mkdir(parents=True)
    (ui / "entry.js").write_text("// tui\n", encoding="utf-8")

    shutil.copy2(REPO_ROOT / "hermes/mercury_cli/elf.py", cli / "elf.py")
    natives = repo / "omp/packages/natives"
    natives.mkdir(parents=True)
    (natives / "package.json").write_text('{\n  "version": "18.1.6"\n}\n')
    addon_dir = natives / "native"
    addon_dir.mkdir()
    addon_header = bytearray(header)
    (addon_dir / f"pi_natives.linux-{'arm64' if magic == 183 else 'x64-baseline'}.node").write_bytes(
        bytes(addon_header) + struct.pack("<IIQQQQQQ", 1, 0, 0, 0, 0, 120, 120, 1) + b"__piNativesV18_1_6")
    source = repo / "third_party/mlounge"
    source.mkdir(parents=True)
    package = source / "package.json"
    package.write_text('{"version":"1.0.0"}')
    first = f"{hashlib.sha256(package.read_bytes()).hexdigest()}  ./package.json\n".encode()
    source_sha = hashlib.sha256(first).hexdigest()
    bundle = repo / "dist/mlounge-fork/tree"
    bundle.mkdir(parents=True)
    shutil.copy2(package, bundle / "package.json")
    (bundle / ".mercury-fork-build.json").write_text(json.dumps({"source_sha":source_sha}))
    (repo / "PINS.txt").write_text("hermes test\nomp test\n")

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


def test_nix_loader_refuses_pack_before_writing_archive(tmp_path):
    repo = _sandbox_repo(tmp_path, VERSION)
    binary = repo / "omp/packages/coding-agent/dist/omp"
    data = binary.read_bytes()
    interpreter = b"/nix/store/build-host-glibc/lib/ld-linux-x86-64.so.2\0"
    changed = bytearray(data[:120])
    struct.pack_into("<QQ", changed, 64 + 32, len(interpreter), len(interpreter))
    binary.write_bytes(changed + interpreter + f"omp/{VERSION}-mercury".encode())
    proc = _run_make_dist(repo, {})
    assert proc.returncode != 0
    assert "Non-portable ELF" in proc.stderr
    assert not list((repo / "dist").glob("*.tar.gz"))


def test_modern_only_x64_addon_refuses_pack(tmp_path):
    repo = _sandbox_repo(tmp_path, VERSION)
    natives = repo / "omp/packages/natives/native"
    baseline = natives / "pi_natives.linux-x64-baseline.node"
    if not baseline.exists():
        pytest.skip("x64 baseline regression")
    baseline.rename(natives / "pi_natives.linux-x64-modern.node")
    proc = _run_make_dist(repo, {})
    assert proc.returncode != 0
    assert "baseline" in proc.stderr
    assert not list((repo / "dist").glob("*.tar.gz"))


def test_mlounge_build_preserves_committed_frontend_bytes(tmp_path, monkeypatch):
    """Prepared dependencies build offline without rewriting archived source."""
    repo = _sandbox_repo(tmp_path, VERSION)
    build = repo / "scripts/build-mlounge-fork.sh"
    shutil.copy2(REPO_ROOT / "scripts/build-mlounge-fork.sh", build)
    source = repo / "third_party/mlounge"
    (source / "package.json").write_text('{"version":"1.0.0","mercuryFork":true}')
    (source / "yarn.lock").write_text("# committed dependency graph\n")
    committed = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.iterdir()
    }
    (source / "node_modules").mkdir()
    (source / "node_modules" / "prepared-fixture").write_text("cached dependency closure\n")

    # Run the real generating script; all acquisition commands fail if reached.
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "node").write_text("#!/bin/bash\nexit 0\n")
    (tools / "npx").write_text("#!/bin/bash\nprintf 'unexpected npx\\n' >&2\nexit 91\n")
    (tools / "npm").write_text(
        "#!/bin/bash\nset -eu\n"
        'if [ "$1" = install ] || [ "$1" = ci ]; then\n'
        "  printf 'unexpected dependency acquisition\\n' >&2\n"
        "  exit 92\n"
        "fi\n"
        '[ -f node_modules/prepared-fixture ]\n'
        '[ "$*" = "run build" ]\n'
        "mkdir -p dist/server/plugins/inputs dist/server/plugins/irc-events public/assets\n"
        "printf '// built\\n' > dist/server/index.js\n"
        "printf 'draft/multiline\\n' > dist/server/plugins/inputs/msg.js\n"
        "printf 'draft/multiline\\n' > dist/server/plugins/irc-events/message.js\n"
        "printf '// built\\n' > public/assets/index-test.js\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        ["bash", str(build)], cwd=repo, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = repo / "dist/mlounge-fork/tree"
    for name, digest in committed.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
        assert hashlib.sha256((payload / name).read_bytes()).hexdigest() == digest, name
    assert not (payload / "package-lock.json").exists()
    assert not (payload / "node_modules").exists()

    shutil.rmtree(source / "node_modules")
    missing = subprocess.run(
        ["bash", str(build)], cwd=repo, capture_output=True, text=True, timeout=30
    )
    assert missing.returncode != 0
    assert "prepared frozen mLounge dependencies missing" in missing.stderr
    for name, digest in committed.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
