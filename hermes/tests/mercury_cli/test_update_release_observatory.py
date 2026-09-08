"""update_from_release observatory tail — update-completeness contracts.

Verified behaviors (existing-install upgrade path for the Matrix
observatory), through the REAL update_from_release with network, download,
subprocess, and observatory hooks mocked:

1. Ordering: venv refresh -> bundled wheels -> [matrix] extra -> config
   migration -> first-time provision -> tuwunel refresh. In particular
   `provision_if_missing` runs BEFORE `refresh_for_update` (a binary swap
   alone leaves a pre-observatory install without toml/appservice/owner/
   unit), and wheels install only AFTER the venv refresh.
2. First-time success prints the login-card line.
3. Already provisioned (provision_if_missing -> None): no first-time line,
   refresh still runs.
4. Provision failure WARNS, never blocks (update returns 0).
5. Bundled-wheels install failure warns only (update returns 0).
6. Observatory disabled: no wheels/matrix-extra installs, no first-time
   provision, silent.
"""

from __future__ import annotations

import platform
import subprocess
import tarfile
from pathlib import Path

import pytest

import mercury_cli.update_release as ur
import observatory.provision as prov
import mercury_cli.update_cmd as update_cmd

FAKE_UV = "/usr/bin/fake-uv"


def _fixture_tarball(dest: Path, *, wheels: list[str] | None) -> None:
    """A minimal but layout-correct mercury release tarball."""
    staging = dest.parent / "fixture-src"
    (staging / "bin").mkdir(parents=True)
    (staging / "bin" / "mercury").write_text("#!/bin/sh\n", encoding="utf-8")
    omp = staging / "omp" / "packages" / "coding-agent" / "dist"
    omp.mkdir(parents=True)
    # ELF magic byte 18: 62 = x86_64, 183 = AArch64 (arch guard reads it)
    machine = platform.machine().lower()
    want = 183 if machine in ("aarch64", "arm64") else 62
    (omp / "omp").write_bytes(b"\x7fELF" + b"\x00" * 14 + bytes([want]) + b"\x00" * 8)
    tools = staging / "hermes" / "tools"
    tools.mkdir(parents=True)
    (tools / "omp_delegation.py").write_text("# delegation\n", encoding="utf-8")
    if wheels:
        wdir = staging / "wheels"
        wdir.mkdir()
        for name in wheels:
            (wdir / name).write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(staging, arcname="mercury")


WHEELS = [
    "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl",
    "mautrix-0.21.1-py3-none-any.whl",
]


class _Harness:
    """Drives update_from_release against a fake installed tree."""

    def __init__(self, tmp_path: Path, monkeypatch, *, wheels: list[str] | None,
                 enabled: bool, provision_result, provision_raises=None,
                 wheels_rc=0):
        self.events: list[str] = []
        self.wheels_rc = wheels_rc
        self.root = tmp_path / "install-root"
        (self.root / "hermes" / ".venv" / "bin").mkdir(parents=True)

        tarball = tmp_path / "rel.tar.gz"
        _fixture_tarball(tarball, wheels=wheels)

        rel = {
            "tag_name": "v9.9.9",
            "assets": [{
                "name": "mercury-9.9.9-x64.tar.gz",
                "browser_download_url": "https://example.invalid/mercury-9.9.9-x64.tar.gz",
            }],
        }
        monkeypatch.setattr(ur, "_latest_release", lambda *a, **k: rel)
        monkeypatch.setattr(ur, "_installed_version", lambda: "0.0.1")
        monkeypatch.setattr(ur, "_project_root", lambda: self.root)

        def _download(url, dest, *a, **k):
            Path(dest).write_bytes(tarball.read_bytes())
        monkeypatch.setattr(ur, "_download", _download)

        # deterministic uv: both the venv refresh and the new pip helpers
        # resolve uv through shutil.which
        monkeypatch.setattr(ur.shutil, "which", lambda name: FAKE_UV if name == "uv" else None)

        harness = self

        def _fake_run(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if any(str(c).endswith(".whl") for c in cmd):
                harness.events.append("wheels-install")
                rc = harness.wheels_rc
            elif "[matrix]" in joined:
                harness.events.append("matrix-extra")
                rc = 0
            elif "-e" in joined and "install" in joined:
                harness.events.append("venv-refresh")
                rc = 0
            else:
                rc = 0
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom" if rc else "")
        monkeypatch.setattr(ur.subprocess, "run", _fake_run)

        def _migrate(*a, **k):
            self.events.append("config-migration")
        monkeypatch.setattr(update_cmd, "_check_and_apply_config_migration", _migrate)

        monkeypatch.setattr(prov, "observatory_enabled", lambda: enabled)

        def _provision_if_missing(*a, **k):
            self.events.append("provision-if-missing")
            if provision_raises is not None:
                raise provision_raises
            return provision_result
        monkeypatch.setattr(prov, "provision_if_missing", _provision_if_missing)

        def _refresh(*a, **k):
            self.events.append("refresh-for-update")
            return None
        monkeypatch.setattr(prov, "refresh_for_update", _refresh)

    def run(self) -> int:
        return ur.update_from_release(assume_yes=True)


def _harness(tmp_path, monkeypatch, **kw):
    defaults = dict(wheels=WHEELS, enabled=True, provision_result={"tuwunel": {}})
    defaults.update(kw)
    return _Harness(tmp_path, monkeypatch, **defaults)


def test_tail_order_and_first_time_line(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch)
    assert h.run() == 0
    out = capsys.readouterr().out
    assert "observatory provisioned for the first time — run mercury setup for the login card" in out
    ev = h.events
    assert ev.index("venv-refresh") < ev.index("wheels-install"), "wheels must install after venv refresh"
    assert ev.index("wheels-install") < ev.index("matrix-extra")
    assert ev.index("matrix-extra") < ev.index("provision-if-missing"), "deps before the observatory tail"
    assert ev.index("provision-if-missing") < ev.index("refresh-for-update"), "provision BEFORE refresh"
    assert "refresh-for-update" in ev


def test_already_provisioned_no_first_time_line(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch, provision_result=None)
    assert h.run() == 0
    out = capsys.readouterr().out
    assert "provisioned for the first time" not in out
    assert "provision-if-missing" in h.events
    assert "refresh-for-update" in h.events


def test_provision_failure_warns_not_blocks(tmp_path, monkeypatch, capsys):
    h = _harness(
        tmp_path, monkeypatch,
        provision_raises=prov.ProvisionError("github unreachable"),
    )
    assert h.run() == 0  # never blocks the update
    out = capsys.readouterr().out
    assert "observatory first-time provision skipped" in out
    assert "github unreachable" in out
    assert "refresh-for-update" in h.events  # refresh still attempted


def test_wheels_failure_warns_only(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch, wheels_rc=1)
    assert h.run() == 0
    out = capsys.readouterr().out
    assert "bundled-wheels install failed" in out
    assert "manual fix" in out
    # uv attempt fails -> the pip fallback also lands as a wheels install
    assert h.events.count("wheels-install") == 2


def test_disabled_install_skips_deps_and_provision(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch, wheels=None, enabled=False, provision_result=None)
    assert h.run() == 0
    out = capsys.readouterr().out
    assert "wheels-install" not in h.events
    assert "matrix-extra" not in h.events
    assert "provisioned for the first time" not in out
    # the tail hooks still ran (their own disabled-gates returned None)
    assert "provision-if-missing" in h.events
    assert "refresh-for-update" in h.events


def test_no_wheels_dir_skips_wheel_install_silently(tmp_path, monkeypatch, capsys):
    h = _harness(tmp_path, monkeypatch, wheels=None)
    assert h.run() == 0
    assert "wheels-install" not in h.events
    assert "matrix-extra" in h.events  # enabled install still gets the extra
    assert "bundled" not in capsys.readouterr().out
