"""Install-time offline provisioning: install.sh never asks GitHub.

Regression cover for the v0.0.21 live-VM failure: install.sh provisioned
the observatory ONLINE (``python -m observatory.provision`` without
``--offline``), so a GitHub 403 rate-limit on the releases API failed the
whole install (exit 1) — even with a usable binary already installed.

Laws under test:
* install.sh passes ``--offline`` to the provision CLI (source guard) and
  the CLI maps the flag through to ``provision(offline=...)`` — install
  makes zero GitHub requests;
* the explicit online path (``offline=False``: bare CLI, `mercury update`
  first-time provision) degrades on fetch failure when a usable binary is
  installed — warns and continues (action ``"kept"``) instead of raising;
* with nothing usable installed (missing/stale binary) the original fetch
  error still raises — fail-hard is preserved;
* the update path (``refresh_for_update``) still checks latest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory import provision, tuwunel  # noqa: E402
from observatory.config_gen import ObservatoryPaths  # noqa: E402

INSTALL_SH = REPO_ROOT / "install.sh"


def _rate_limited(url: str) -> bytes:
    raise tuwunel.TuwunelError(
        f"fetch failed: {url} (HTTP Error 403: API rate limit exceeded "
        "for unauthenticated requests)"
    )


@pytest.fixture
def seeded_home(tmp_path: Path) -> Path:
    """A pre-fetched home: binary + version file + existing owner creds
    (registration boots a server — these tests must skip that step)."""
    paths = ObservatoryPaths(tmp_path)
    paths.bin_dir.mkdir(parents=True)
    paths.binary.write_bytes(b"#!/bin/sh\n# fake pre-fetched tuwunel\n")
    paths.version_file.write_text("1.9.0\n", encoding="utf-8")
    paths.owner_credentials.write_text("{}\n", encoding="utf-8")
    return tmp_path


# --- install.sh passes --offline (source guard) ------------------------------


def test_install_sh_provision_call_passes_offline():
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert "_obs_args+=(--offline)" in body


def test_install_sh_still_invokes_provision_cli():
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert "-m observatory.provision" in body


def test_cli_maps_offline_flag_through(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def _fake(*args, **kwargs):
        seen.update(kwargs)
        return {
            "tuwunel": {"action": "current", "version": "1.9.0",
                        "binary": "x", "offline": kwargs.get("offline")},
            "config": "kept",
            "appservice": "kept",
            "owner": "exists",
            "unit": "skipped (--no-systemd)",
        }

    monkeypatch.setattr(provision, "provision", _fake)
    assert provision.main(
        ["--mercury-home", str(tmp_path), "--no-systemd", "--offline"]
    ) == 0
    assert seen.get("offline") is True


def test_cli_default_stays_online(tmp_path: Path, monkeypatch):
    """The bare CLI keeps its explicit online path (install.sh opts out
    via --offline; `mercury update` never goes through main at all)."""
    seen: dict = {}

    def _fake(*args, **kwargs):
        seen.update(kwargs)
        return {
            "tuwunel": {"action": "current", "version": "1.9.0",
                        "binary": "x", "offline": kwargs.get("offline")},
            "config": "kept",
            "appservice": "kept",
            "owner": "exists",
            "unit": "skipped (--no-systemd)",
        }

    monkeypatch.setattr(provision, "provision", _fake)
    assert provision.main(
        ["--mercury-home", str(tmp_path), "--no-systemd"]
    ) == 0
    assert seen.get("offline") is False


# --- online-path fetch failure degrades when a usable binary exists ----------


class TestOnlineDegradeKeepsUsableBinary:
    def test_403_with_installed_binary_keeps_and_warns(
        self, seeded_home: Path, capsys
    ):
        """The live-VM repro on the explicit online path: 403 rate-limit
        with a current binary installed provisions successfully."""
        summary = provision.provision(
            seeded_home, systemd=False, offline=False, fetch=_rate_limited
        )
        assert summary["tuwunel"]["action"] == "kept"
        assert summary["tuwunel"]["version"] == "1.9.0"
        assert summary["tuwunel"]["offline"] is False
        # provisioning CONTINUED past the binary step (log + continue)
        assert summary["config"] == "created"
        assert summary["appservice"] == "created"
        assert summary["owner"] == "exists"
        out = capsys.readouterr().out
        assert "keeping installed v1.9.0" in out
        assert "mercury update" in out

    def test_403_without_binary_still_raises(self, tmp_path: Path):
        with pytest.raises(tuwunel.TuwunelError, match="403"):
            provision.provision(
                tmp_path, systemd=False, offline=False, fetch=_rate_limited
            )

    def test_403_with_stale_binary_still_raises(self, seeded_home: Path):
        paths = ObservatoryPaths(seeded_home)
        paths.version_file.write_text("1.8.0\n", encoding="utf-8")
        with pytest.raises(tuwunel.TuwunelError, match="403"):
            provision.provision(
                seeded_home, systemd=False, offline=False, fetch=_rate_limited
            )

    def test_online_success_path_warns_nothing(
        self, seeded_home: Path, capsys
    ):
        def _fetch(url: str) -> bytes:
            return json.dumps({"tag_name": "v1.9.0", "assets": []}).encode()

        summary = provision.provision(
            seeded_home, systemd=False, offline=False, fetch=_fetch
        )
        assert summary["tuwunel"]["action"] == "current"
        assert summary["tuwunel"]["offline"] is False
        assert "keeping installed" not in capsys.readouterr().out


# --- update path still fetches latest ----------------------------------------


class TestUpdatePathStillFetches:
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
