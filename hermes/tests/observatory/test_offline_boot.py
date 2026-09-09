"""Boot-law offline tests: provision() never touches the network by default.

Regression cover for the sidecar crash loop: boot called
``provision.provision()`` -> ``tuwunel.refresh_tuwunel()`` ->
``latest_stable_release()`` unconditionally, so a GitHub 403 rate-limit
propagated uncaught and systemd restarted the sidecar every 5s forever —
even with a current binary already installed.

Law under test:
* ``provision()`` default (no ``offline`` arg, no config) trusts the
  installed binary + min-version gate and NEVER calls ``fetch``;
* a GitHub 403 (or any fetch failure) cannot break boot when the binary
  is installed and current;
* the ONE explicit online path — ``mercury update`` /
  ``refresh_for_update()`` (and the ``offline=False`` escape hatch it and
  install.sh use) — still checks latest;
* missing/stale binaries fail hard with an actionable message (run
  ``mercury update`` with network, or install tuwunel manually).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory import provision, tuwunel  # noqa: E402
from observatory.config_gen import ObservatoryPaths  # noqa: E402


def _boom(url: str) -> bytes:
    raise AssertionError(f"network touched on the boot path: {url}")


def _rate_limited(url: str) -> bytes:
    raise tuwunel.TuwunelError(
        f"fetch failed: {url} (HTTP Error 403: API rate limit exceeded "
        "for unauthenticated requests)"
    )


@pytest.fixture
def seeded_home(tmp_path: Path) -> Path:
    """A pre-fetched home: binary + version file + existing owner creds
    (registration boots a server — boot-law tests must skip that step)."""
    paths = ObservatoryPaths(tmp_path)
    paths.bin_dir.mkdir(parents=True)
    paths.binary.write_bytes(b"#!/bin/sh\n# fake pre-fetched tuwunel\n")
    paths.version_file.write_text("1.9.0\n", encoding="utf-8")
    paths.owner_credentials.write_text("{}\n", encoding="utf-8")
    return tmp_path


class TestBootMakesZeroNetworkCalls:
    def test_default_provision_never_calls_fetch(self, seeded_home: Path):
        summary = provision.provision(seeded_home, systemd=False, fetch=_boom)
        assert summary["tuwunel"]["action"] == "current"
        assert summary["tuwunel"]["version"] == "1.9.0"
        assert summary["tuwunel"]["offline"] is True

    def test_default_boot_survives_github_403(self, seeded_home: Path):
        """The crash-loop repro: 403 rate-limit with a current binary
        installed must still boot from the installed binary."""
        summary = provision.provision(
            seeded_home, systemd=False, fetch=_rate_limited
        )
        assert summary["tuwunel"]["action"] == "current"
        assert summary["tuwunel"]["version"] == "1.9.0"


class TestExplicitOnlinePathStillFetches:
    def test_refresh_for_update_checks_latest(
        self, seeded_home: Path, monkeypatch
    ):
        seen: list[str] = []

        def _fetch(url: str) -> bytes:
            seen.append(url)
            return json.dumps({"tag_name": "v1.9.0", "assets": []}).encode()

        monkeypatch.setenv("MERCURY_HOME", str(seeded_home))
        monkeypatch.setattr(provision, "observatory_enabled", lambda: True)
        monkeypatch.setattr(provision, "observatory_offline", lambda: False)
        monkeypatch.setattr(tuwunel, "_default_fetch", _fetch)
        line = provision.refresh_for_update()
        assert line is not None and "tuwunel current v1.9.0" in line
        assert seen == [tuwunel.RELEASES_LATEST_API]

    def test_explicit_offline_false_still_fetches(self, seeded_home: Path):
        seen: list[str] = []

        def _fetch(url: str) -> bytes:
            seen.append(url)
            return json.dumps({"tag_name": "v1.9.0", "assets": []}).encode()

        summary = provision.provision(
            seeded_home, systemd=False, offline=False, fetch=_fetch
        )
        assert summary["tuwunel"]["action"] == "current"
        assert summary["tuwunel"]["offline"] is False
        assert seen == [tuwunel.RELEASES_LATEST_API]


class TestActionableFailure:
    def test_missing_binary_says_mercury_update(self, tmp_path: Path):
        with pytest.raises(tuwunel.TuwunelError, match="mercury update"):
            provision.provision(tmp_path, systemd=False, fetch=_boom)

    def test_stale_binary_says_mercury_update(self, seeded_home: Path):
        paths = ObservatoryPaths(seeded_home)
        paths.version_file.write_text("1.8.0\n", encoding="utf-8")
        with pytest.raises(tuwunel.TuwunelError, match="mercury update"):
            provision.provision(seeded_home, systemd=False, fetch=_boom)

    def test_stale_binary_names_minimum(self, seeded_home: Path):
        paths = ObservatoryPaths(seeded_home)
        paths.version_file.write_text("1.8.0\n", encoding="utf-8")
        with pytest.raises(tuwunel.TuwunelError, match=tuwunel.MIN_VERSION):
            provision.provision(seeded_home, systemd=False, fetch=_boom)
