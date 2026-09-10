"""update_from_release must never swap the LIVE checkout under pytest.

RED regression test for the mercury_cli stomper family
(agent/mercury-cli-stomper-probe): the full hermes/tests/mercury_cli/ suite
is destructive when run from inside a git worktree — twice it deleted files
across the running worktree (committed test files included) and dropped
stray ``tuwunel-binaries/`` + ``wheels/`` dirs at the repo root.

Signature analysis (see REPORT.md): that is exactly what
``mercury_cli.update_release._swap_tree`` does when ``dst`` is the live
checkout — ``shutil.rmtree`` per tarball top-level entry (``bin/``,
``hermes/`` — which contains these very test files — ``omp/``), then copies
the tarball stubs over them, planting the tarball-only ``wheels/`` and
``tuwunel-binaries/`` dirs (scripts/make-dist.sh stages both at tarball top
level). Unlike the marker/recovery paths (``main._pytest_owns_live_checkout``,
``_early_recovery._pytest_owns_live_checkout``), ``update_release`` has NO
pytest-live-checkout guard, and ``_project_root()`` resolves to the live
checkout in-suite (``bin/mercury`` + ``hermes/`` both exist there).

These tests prove the hole WITHOUT stomping: every destructive op is
replaced by a tripwire/recorder, the fixture tarball lives in ``tmp_path``,
and the only live-checkout contact is read-only root resolution.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import mercury_cli.update_release as ur

# Install-root level: update_release roots at the dir containing bin/ +
# hermes/ (repo/worktree root), one level above main.PROJECT_ROOT (hermes/).
CHECKOUT_ROOT = Path(ur.__file__).resolve().parents[2]


def _fixture_tarball(dest: Path) -> None:
    """Minimal layout-correct release tarball (tmp_path only)."""
    staging = dest.parent / "fixture-src"
    (staging / "bin").mkdir(parents=True)
    (staging / "bin" / "mercury").write_text("#!/bin/sh\n", encoding="utf-8")
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(staging, arcname="mercury")


def test_update_release_module_has_live_checkout_guard(tmp_path):
    """The release updater must expose the same live-checkout predicate as
    main/_early_recovery, evaluated at install-root level."""
    guard = getattr(ur, "_pytest_owns_live_checkout", None)
    assert callable(guard), (
        "mercury_cli.update_release has no _pytest_owns_live_checkout guard: "
        "update_from_release can swap the live checkout under pytest"
    )
    assert guard(CHECKOUT_ROOT) is True
    assert guard(tmp_path) is False


def test_update_from_release_refuses_live_checkout_without_swapping(
    tmp_path, monkeypatch
):
    """Driving update_from_release at the live checkout under pytest must
    refuse BEFORE _swap_tree — never rmtree the live tree.

    Every destructive op is a tripwire; the live checkout is only resolved
    (read-only) via the mocked _project_root. RED on current code: the swap
    tripwire fires.
    """
    tarball = tmp_path / "rel.tar.gz"
    _fixture_tarball(tarball)

    rel = {
        "tag_name": "v9.9.9",
        "assets": [
            {
                "name": "mercury-9.9.9-x64.tar.gz",
                "browser_download_url": "https://example.invalid/x.tar.gz",
            }
        ],
    }
    monkeypatch.setattr(ur, "_latest_release", lambda *a, **k: rel)
    monkeypatch.setattr(ur, "_installed_version", lambda: "0.0.1")
    monkeypatch.setattr(ur, "_project_root", lambda: CHECKOUT_ROOT)
    monkeypatch.setattr(
        ur, "_download", lambda url, dest, *a, **k: Path(dest).write_bytes(tarball.read_bytes())
    )

    touched: list = []

    def _tripwire_swap(src, dst):
        touched.append(("swap", str(dst)))
        raise AssertionError(f"STOMP: _swap_tree called on live checkout dst={dst}")

    def _tripwire_build_id(root, build_id):
        touched.append(("build-id", str(root)))
        raise AssertionError(f"STOMP: _record_build_id wrote live root {root}")

    def _boom_run(cmd, **kwargs):
        raise AssertionError(f"STOMP: subprocess escaped mocks: {cmd!r}")

    monkeypatch.setattr(ur, "_swap_tree", _tripwire_swap)
    monkeypatch.setattr(ur, "_record_build_id", _tripwire_build_id)
    monkeypatch.setattr(ur.subprocess, "run", _boom_run)

    try:
        rc = ur.update_from_release(assume_yes=True)
    except SystemExit as exc:
        rc = exc.code

    assert touched == [], f"update_from_release touched the live checkout: {touched}"
    assert rc != 0, "live-checkout refusal must be non-zero"
