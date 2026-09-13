"""Regression tests for the IRC observatory secret closure.

Law: the listener passwords ``IRC_BOUNCER_PASSWORD`` /
``IRC_AGENT_PASSWORD`` in ``$MERCURY_HOME/.env`` (0600) must never reach
model context through file tools. Every model-facing file-reading tool
refuses these reads (read_file, patch replace + V4A, search_files
content) with a location-only error; wizard surfaces (provision
status_summary, setup card) carry booleans/paths only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

SENTINEL_BOUNCER = "bounce-SENTINEL-7c3a9e1f5b2d"
SENTINEL_AGENT = "agent-SENTINEL-4f8c2a6e0b1d3f5a"

_TS_ABSENT = {"available": False, "up": False, "ip": None, "dns_name": None}


@pytest.fixture
def fake_mercury(tmp_path, monkeypatch):
    """Fake $MERCURY_HOME with planted IRC secrets."""
    home = tmp_path / "fake-mercury"
    obs = home / "observatory"
    obs.mkdir(parents=True)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    import mercury_constants
    monkeypatch.setattr(mercury_constants, "get_default_hermes_root", lambda: home)
    import agent.file_safety as fs
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: home)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: home)
    env = home / ".env"
    env.write_text(
        f"IRC_BOUNCER_PASSWORD={SENTINEL_BOUNCER}\n"
        f"IRC_AGENT_PASSWORD={SENTINEL_AGENT}\n",
        encoding="utf-8",
    )
    return {"home": home, "obs": obs, "env": env}


def _assert_location_only(result: dict) -> None:
    assert result.get("error"), f"secret read must be refused: {result}"
    assert "access denied" in result["error"].lower()
    blob = json.dumps(result)
    assert SENTINEL_BOUNCER not in blob
    assert SENTINEL_AGENT not in blob


class TestReadRefusesObservatorySecrets:
    def test_read_env_refused(self, fake_mercury):
        from tools.file_tools import read_file_tool
        _assert_location_only(json.loads(read_file_tool(str(fake_mercury["env"]))))

    def test_read_observatory_log_still_allowed(self, fake_mercury):
        """Exact-file deny must not swallow non-secret observatory files."""
        from tools.file_tools import read_file_tool
        log = fake_mercury["obs"] / "logs" / "ircd.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("client connected\n", encoding="utf-8")
        result = json.loads(read_file_tool(str(log)))
        assert not result.get("error"), f"non-secret log must stay readable: {result}"

    def test_read_ircd_config_allowed(self, fake_mercury):
        """ircd.json carries no secrets (passwords ride env only)."""
        from tools.file_tools import read_file_tool
        cfg = fake_mercury["obs"] / "ircd.json"
        cfg.write_text('{"server_name": "mercury"}\n', encoding="utf-8")
        result = json.loads(read_file_tool(str(cfg)))
        assert not result.get("error"), f"config must stay readable: {result}"


class TestPatchRefusesObservatorySecrets:
    def test_patch_replace_refuses_env(self, fake_mercury):
        from tools.file_tools import patch_tool
        original = fake_mercury["env"].read_text(encoding="utf-8")
        result = json.loads(
            patch_tool(
                mode="replace",
                path=str(fake_mercury["env"]),
                old_string="IRC_BOUNCER_PASSWORD",
                new_string="IRC_BOUNCER_PASSWORD=X",
            )
        )
        _assert_location_only(result)
        assert fake_mercury["env"].read_text(encoding="utf-8") == original

    def test_patch_v4a_refuses_env(self, fake_mercury):
        from tools.file_tools import patch_tool
        original = fake_mercury["env"].read_text(encoding="utf-8")
        v4a = (
            "*** Begin Patch\n"
            f"*** Update File: {fake_mercury['env']}\n"
            "@@\n"
            f"-IRC_BOUNCER_PASSWORD={SENTINEL_BOUNCER}\n"
            "+IRC_BOUNCER_PASSWORD=hacked\n"
            "*** End Patch\n"
        )
        result = json.loads(patch_tool(mode="patch", patch=v4a))
        _assert_location_only(result)
        assert fake_mercury["env"].read_text(encoding="utf-8") == original


class TestSearchRefusesObservatorySecrets:
    def test_direct_search_refused(self, fake_mercury):
        from tools.file_tools import search_tool
        direct = json.loads(
            search_tool(
                pattern="SENTINEL",
                path=str(fake_mercury["env"]),
                output_mode="content",
            )
        )
        assert direct.get("error"), f"direct creds search must be refused: {direct}"
        assert "access denied" in direct["error"].lower()
        assert SENTINEL_BOUNCER not in json.dumps(direct)

    def test_dir_search_filters_credential_matches(self, fake_mercury, tmp_path):
        from tools.file_tools import search_tool
        (tmp_path / "notes.txt").write_text("nothing secret here\n", encoding="utf-8")
        result = json.loads(
            search_tool(
                pattern="SENTINEL",
                path=str(fake_mercury["home"]),
                output_mode="content",
            )
        )
        blob = json.dumps(result)
        assert SENTINEL_BOUNCER not in blob
        assert SENTINEL_AGENT not in blob


class TestWizardSurfacesCarryNoValues:
    def test_status_summary_has_no_secret_values(self, fake_mercury, monkeypatch):
        from observatory import provision as provision_mod
        monkeypatch.setenv("IRC_BOUNCER_PASSWORD", SENTINEL_BOUNCER)
        monkeypatch.setenv("IRC_AGENT_PASSWORD", SENTINEL_AGENT)
        summary = provision_mod.status_summary(fake_mercury["home"])
        blob = json.dumps(summary)
        assert SENTINEL_BOUNCER not in blob
        assert SENTINEL_AGENT not in blob
        assert summary["bouncer_password_set"] is True

    def test_setup_card_prints_no_secret_values(self, fake_mercury, capsys):
        import mercury_cli.setup as setup_mod
        status = {
            "server_name": "mercury",
            "bouncer": "127.0.0.1:6670",
        }
        setup_mod._print_observatory_setup_card(status, dict(_TS_ABSENT))
        out = capsys.readouterr().out
        assert SENTINEL_BOUNCER not in out
        assert SENTINEL_AGENT not in out
        # Location / variable names may remain; values must not.
        assert "IRC_BOUNCER_PASSWORD" in out
