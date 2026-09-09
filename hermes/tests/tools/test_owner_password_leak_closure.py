"""Regression tests for the Matrix owner-password leak closure.

Law: the owner password / access_token in
``$MERCURY_HOME/observatory/owner-credentials.json`` (0600) — plus the
sibling plaintext-secret files under ``observatory/`` (tuwunel.toml,
tuwunel-bootstrap.toml, appservice registration YAML) — must never reach
model context through file tools. Every model-facing file-reading tool
refuses these reads (read_file, patch replace + V4A, search_files
content) with a location-only error; wizard surfaces (provision
status_summary, setup card) carry booleans/paths only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

SENTINEL_PASSWORD = "obs-pw-SENTINEL-7c3a9e1f5b2d"
SENTINEL_TOKEN = "syt-SENTINEL-4f8c2a6e0b1d3f5a"
SENTINEL_REG_TOKEN = "reg-SENTINEL-1a2b3c4d5e6f"

_TS_ABSENT = {"available": False, "up": False, "ip": None, "dns_name": None}


@pytest.fixture
def fake_mercury(tmp_path, monkeypatch):
    """Fake $MERCURY_HOME with planted observatory secrets."""
    home = tmp_path / "fake-mercury"
    obs = home / "observatory"
    (obs / "appservices").mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    import mercury_constants
    monkeypatch.setattr(mercury_constants, "get_default_hermes_root", lambda: home)
    import agent.file_safety as fs
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: home)
    creds = obs / "owner-credentials.json"
    creds.write_text(
        json.dumps(
            {
                "homeserver_url": "http://127.0.0.1:18008",
                "user_id": "@merc-owner:mercury.local",
                "password": SENTINEL_PASSWORD,
                "access_token": SENTINEL_TOKEN,
                "device_id": "DEV1",
            }
        ),
        encoding="utf-8",
    )
    (obs / "tuwunel.toml").write_text(
        '[global]\nregistration_token = "%s"\n' % SENTINEL_REG_TOKEN,
        encoding="utf-8",
    )
    (obs / "appservices" / "merc-observatory.yaml").write_text(
        'as_token: "as-%s"\nhs_token: "hs-%s"\n' % (SENTINEL_TOKEN, SENTINEL_TOKEN),
        encoding="utf-8",
    )
    return {"home": home, "obs": obs, "creds": creds}


def _assert_location_only(result: dict) -> None:
    assert result.get("error"), f"secret read must be refused: {result}"
    assert "access denied" in result["error"].lower()
    blob = json.dumps(result)
    assert SENTINEL_PASSWORD not in blob
    assert SENTINEL_TOKEN not in blob
    assert SENTINEL_REG_TOKEN not in blob


class TestReadRefusesObservatorySecrets:
    def test_read_owner_credentials_refused(self, fake_mercury):
        from tools.file_tools import read_file_tool
        _assert_location_only(json.loads(read_file_tool(str(fake_mercury["creds"]))))

    def test_read_tuwunel_toml_refused(self, fake_mercury):
        from tools.file_tools import read_file_tool
        _assert_location_only(
            json.loads(read_file_tool(str(fake_mercury["obs"] / "tuwunel.toml")))
        )

    def test_read_appservice_registration_refused(self, fake_mercury):
        from tools.file_tools import read_file_tool
        _assert_location_only(
            json.loads(
                read_file_tool(
                    str(fake_mercury["obs"] / "appservices" / "merc-observatory.yaml")
                )
            )
        )

    def test_read_observatory_log_still_allowed(self, fake_mercury):
        """Exact-file deny must not swallow non-secret observatory files."""
        from tools.file_tools import read_file_tool
        log = fake_mercury["obs"] / "logs" / "homeserver.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("server started\n", encoding="utf-8")
        result = json.loads(read_file_tool(str(log)))
        assert not result.get("error"), f"non-secret log must stay readable: {result}"


class TestPatchRefusesObservatorySecrets:
    def test_patch_replace_refuses_owner_credentials(self, fake_mercury):
        from tools.file_tools import patch_tool
        original = fake_mercury["creds"].read_text(encoding="utf-8")
        result = json.loads(
            patch_tool(
                mode="replace",
                path=str(fake_mercury["creds"]),
                old_string="user_id",
                new_string="user_id=X",
            )
        )
        _assert_location_only(result)
        assert fake_mercury["creds"].read_text(encoding="utf-8") == original

    def test_patch_v4a_refuses_owner_credentials(self, fake_mercury):
        from tools.file_tools import patch_tool
        original = fake_mercury["creds"].read_text(encoding="utf-8")
        v4a = (
            "*** Begin Patch\n"
            f"*** Update File: {fake_mercury['creds']}\n"
            "@@\n"
            f'-"password": "{SENTINEL_PASSWORD}"\n'
            '+"password": "hacked"\n'
            "*** End Patch\n"
        )
        result = json.loads(patch_tool(mode="patch", patch=v4a))
        _assert_location_only(result)
        assert fake_mercury["creds"].read_text(encoding="utf-8") == original


class TestSearchRefusesObservatorySecrets:
    def test_direct_search_refused(self, fake_mercury):
        from tools.file_tools import search_tool
        direct = json.loads(
            search_tool(
                pattern="SENTINEL",
                path=str(fake_mercury["creds"]),
                output_mode="content",
            )
        )
        assert direct.get("error"), f"direct creds search must be refused: {direct}"
        assert "access denied" in direct["error"].lower()
        assert SENTINEL_PASSWORD not in json.dumps(direct)

    def test_dir_search_filters_credential_matches(self, fake_mercury, tmp_path):
        from tools.file_tools import search_tool
        (tmp_path / "notes.txt").write_text("nothing secret here\n", encoding="utf-8")
        result = json.loads(
            search_tool(
                pattern="SENTINEL",
                path=str(fake_mercury["obs"]),
                output_mode="content",
            )
        )
        blob = json.dumps(result)
        assert SENTINEL_PASSWORD not in blob
        assert SENTINEL_TOKEN not in blob
        assert SENTINEL_REG_TOKEN not in blob


class TestWizardSurfacesCarryNoValues:
    def test_status_summary_has_no_secret_values(self, fake_mercury):
        from observatory import provision as provision_mod
        summary = provision_mod.status_summary(fake_mercury["home"])
        blob = json.dumps(summary)
        assert SENTINEL_PASSWORD not in blob
        assert SENTINEL_TOKEN not in blob
        assert SENTINEL_REG_TOKEN not in blob
        assert summary["owner_credentials_exist"] is True

    def test_setup_card_prints_no_secret_values(self, fake_mercury, capsys):
        import mercury_cli.setup as setup_mod
        status = {
            "owner_credentials_path": str(fake_mercury["creds"]),
            "homeserver_url": "http://127.0.0.1:18008",
        }
        setup_mod._print_observatory_setup_card(status, dict(_TS_ABSENT))
        out = capsys.readouterr().out
        assert SENTINEL_PASSWORD not in out
        assert SENTINEL_TOKEN not in out
        # Location / variable names may remain; values must not.
        assert "MATRIX_OBS_OWNER_PASSWORD" in out
        assert str(fake_mercury["creds"]) in out
