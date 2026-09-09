"""Vendored tuwunel binaries — pin set, tarball staging, offline provision.

The repo vendors the upstream .zst assets per-arch under
hermes/observatory/tuwunel-binaries/ (VERSION + SHA256SUMS-pinned);
scripts/make-dist.sh decompresses the matching-arch asset into each
per-arch tarball as tuwunel-binaries/tuwunel-<arch>; install.sh installs
it before provision --offline. This module covers:

* the pin set itself (VERSION >= MIN_VERSION, both assets present and
  hash-verified — the supply-chain gate);
* the REAL _stage_tuwunel_binary function from make-dist.sh (per-arch ELF
  machine bytes, executable bit, VERSION + verifiable SHA256SUMS;
  fail-hard on unknown arch / missing asset / tampered bytes);
* offline provision from a staged binary with a fetch that explodes
  (installed_version / _refresh_tuwunel_offline recognition);
* a live boot of the REAL vendored x86_64 binary on a throwaway home
  (owner bootstrap against a running tuwunel — proves the staged binary
  actually runs on this arch).
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory import provision, tuwunel  # noqa: E402
from observatory.config_gen import ObservatoryPaths  # noqa: E402

VENDORED_DIR = REPO_ROOT / "hermes" / "observatory" / "tuwunel-binaries"
MAKE_DIST = REPO_ROOT / "scripts" / "make-dist.sh"

ARCHES = {"x64": ("x86_64-v1", 62), "arm64": ("aarch64-v8", 183)}

NEEDS_SHELL_STAGE = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("zstd") is None
    or shutil.which("sha256sum") is None,
    reason="needs bash + zstd + sha256sum (the release-host staging toolchain)",
)
NEEDS_EXEC_X64 = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine().lower() not in ("x86_64", "amd64"),
    reason="needs linux x86_64 to execute the vendored binary",
)


def _version() -> str:
    return (VENDORED_DIR / "VERSION").read_text(encoding="utf-8").strip()


def _pins() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (VENDORED_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


def _extract_stage_func() -> str:
    lines = MAKE_DIST.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("_stage_tuwunel_binary() {")), None)
    assert start is not None, "staging function missing from make-dist.sh"
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1]) + "\n"


def _stage(arch: str, dest: Path, repo: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    script = _extract_stage_func() + f'\n_stage_tuwunel_binary "{arch}" "{dest}" "{repo}"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=300)


def _elf_machine(path: Path) -> int:
    head = path.read_bytes()[:20]
    assert head[:4] == b"\x7fELF", f"{path.name} is not an ELF binary"
    return head[18]


def _boom(url: str) -> bytes:
    raise AssertionError(f"network touched on the offline path: {url}")


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class TestPinSet:
    def test_version_meets_min_gate(self):
        ver = _version()
        assert ver, "VERSION is empty"
        tuwunel.check_min_version(f"v{ver}")  # raises below MIN_VERSION

    def test_both_arch_assets_present_and_pinned(self):
        pins = _pins()
        assert len(pins) >= 2, f"expected both arch pins, got: {sorted(pins)}"
        for arch, (asset_arch, _machine) in ARCHES.items():
            cands = sorted(VENDORED_DIR.glob(f"*-{asset_arch}-linux-gnu-tuwunel.zst"))
            assert len(cands) == 1, f"expected one {asset_arch} asset: {cands}"
            digest = hashlib.sha256(cands[0].read_bytes()).hexdigest()
            assert digest.lower() == pins[cands[0].name].lower(), \
                f"hash mismatch: {cands[0].name}"

    def test_decompressed_assets_are_correct_arch(self):
        # Reads .zst via the zstd CLI only when present; otherwise the
        # byte-level check is covered by TestMakeDistStaging.
        if shutil.which("zstd") is None:  # pragma: no cover
            pytest.skip("no zstd")
        import tempfile
        for _arch, (asset_arch, machine) in ARCHES.items():
            (zst,) = sorted(VENDORED_DIR.glob(f"*-{asset_arch}-linux-gnu-tuwunel.zst"))
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                subprocess.run(["zstd", "-d", "-c", str(zst)], stdout=tmp,
                               check=True, timeout=300)
            try:
                assert _elf_machine(Path(tmp.name)) == machine
            finally:
                Path(tmp.name).unlink(missing_ok=True)


@NEEDS_SHELL_STAGE
class TestMakeDistStaging:
    @pytest.mark.parametrize("arch", ["x64", "arm64"])
    def test_stage_produces_runnable_arch_binary(self, tmp_path, arch):
        dest = tmp_path / "stage"
        proc = _stage(arch, dest)
        assert proc.returncode == 0, proc.stderr
        binary = dest / "tuwunel-binaries" / f"tuwunel-{arch}"
        assert binary.is_file() and binary.stat().st_size > 0
        assert _elf_machine(binary) == ARCHES[arch][1]
        import os
        assert os.access(binary, os.X_OK), "staged binary must be executable"
        assert (dest / "tuwunel-binaries" / "VERSION").read_text(
            encoding="utf-8").strip() == _version()
        check = subprocess.run(["sha256sum", "-c", "SHA256SUMS"],
                               cwd=dest / "tuwunel-binaries",
                               capture_output=True, text=True, timeout=120)
        assert check.returncode == 0, check.stdout + check.stderr

    def test_stage_unknown_arch_fails(self, tmp_path):
        proc = _stage("riscv64", tmp_path / "stage")
        assert proc.returncode != 0

    def test_stage_missing_asset_fails(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "hermes" / "observatory" / "tuwunel-binaries"
        src.mkdir(parents=True)
        (src / "VERSION").write_text("1.9.0\n", encoding="utf-8")
        (src / "SHA256SUMS").write_text("", encoding="utf-8")
        proc = _stage("x64", tmp_path / "stage", repo)
        assert proc.returncode != 0

    def test_stage_tampered_asset_fails(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "hermes" / "observatory" / "tuwunel-binaries"
        src.mkdir(parents=True)
        (zst,) = sorted(VENDORED_DIR.glob("*-x86_64-v1-linux-gnu-tuwunel.zst"))
        raw = bytearray(zst.read_bytes())
        raw[-1] ^= 0xFF  # flip the last byte: pin must catch it
        (src / zst.name).write_bytes(bytes(raw))
        (src / "SHA256SUMS").write_text(
            (VENDORED_DIR / "SHA256SUMS").read_text(encoding="utf-8"),
            encoding="utf-8")
        (src / "VERSION").write_text("1.9.0\n", encoding="utf-8")
        proc = _stage("x64", tmp_path / "stage", repo)
        assert proc.returncode != 0
        assert "mismatch" in proc.stderr

    def test_stage_unpinned_asset_fails(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "hermes" / "observatory" / "tuwunel-binaries"
        src.mkdir(parents=True)
        (zst,) = sorted(VENDORED_DIR.glob("*-x86_64-v1-linux-gnu-tuwunel.zst"))
        shutil.copy(zst, src / zst.name)
        (src / "SHA256SUMS").write_text("", encoding="utf-8")  # pin absent
        (src / "VERSION").write_text("1.9.0\n", encoding="utf-8")
        proc = _stage("x64", tmp_path / "stage", repo)
        assert proc.returncode != 0
        assert "no SHA256SUMS pin" in proc.stderr


@NEEDS_SHELL_STAGE
class TestOfflineProvisionFromStaged:
    def test_staged_binary_provisions_offline_with_dead_fetch(self, tmp_path):
        """End of the virgin-install pipeline: staged binary + version file
        are all provision --offline needs (fetch explodes on any touch)."""
        dest = tmp_path / "stage"
        assert _stage("x64", dest).returncode == 0
        home = tmp_path / "home"
        paths = ObservatoryPaths(home)
        paths.bin_dir.mkdir(parents=True)
        shutil.copy(dest / "tuwunel-binaries" / "tuwunel-x64", paths.binary)
        paths.binary.chmod(0o755)
        shutil.copy(dest / "tuwunel-binaries" / "VERSION", paths.version_file)
        # Owner creds seeded: the owner step would boot the binary (covered
        # by TestLiveBootRealBinary); here the binary step is under test.
        paths.owner_credentials.write_text("{}\n", encoding="utf-8")

        assert tuwunel.installed_version(paths) == _version()
        summary = provision.provision(home, systemd=False, fetch=_boom)
        assert summary["tuwunel"]["action"] == "current"
        assert summary["tuwunel"]["version"] == _version()
        assert summary["tuwunel"]["offline"] is True


@NEEDS_SHELL_STAGE
@NEEDS_EXEC_X64
@pytest.mark.live_system_guard_bypass
class TestLiveBootRealBinary:
    def test_real_binary_boots_and_registers_owner(self, tmp_path):
        """The vendored binary actually runs: full provision boots tuwunel
        on a throwaway home and registers the owner (no mocks, no fetch)."""
        from observatory import config_gen

        dest = tmp_path / "stage"
        assert _stage("x64", dest).returncode == 0
        home = tmp_path / "home"
        paths = ObservatoryPaths(home)
        paths.bin_dir.mkdir(parents=True)
        shutil.copy(dest / "tuwunel-binaries" / "tuwunel-x64", paths.binary)
        paths.binary.chmod(0o755)
        shutil.copy(dest / "tuwunel-binaries" / "VERSION", paths.version_file)
        # Pre-write the closed config on an ephemeral free port: the test
        # must not depend on the default port (a live homeserver may own
        # it). ensure_config keeps this file; the owner bootstrap binds it.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not _port_free(port):  # pragma: no cover - vanishingly rare
            pytest.skip(f"port {port} occupied")
        paths.db_dir.mkdir(parents=True)
        paths.appservices_dir.mkdir(parents=True)
        paths.toml.write_text(config_gen.render_tuwunel_toml(
            database_path=str(paths.db_dir),
            appservice_dir=str(paths.appservices_dir),
            registration_token=config_gen.new_secret(32),
            port=port,
        ), encoding="utf-8")

        summary = provision.provision(home, systemd=False, fetch=_boom)
        assert summary["tuwunel"]["action"] == "current"
        assert summary["owner"] == "created"
        assert paths.owner_credentials.is_file()
