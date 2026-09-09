"""Wizard prompt flows for the observatory identity triple.

Fresh installs prompt server-name / localpart / password (validated in a
loop, safe defaults) and hand the answers to provisioning; re-runs keep
stored credentials unless the user explicitly opts into a rotation.
Reuses the section fakes from test_setup_wizard (real validators).
"""

from __future__ import annotations

import pytest

import mercury_cli.setup as setup_mod
from tests.observatory.test_setup_wizard import (
    _FakeProvision,
    _run_section,
    _status,
    _write_credentials,
)


def _provisioned_status(creds) -> dict:
    return _status(
        provisioned=True,
        config_exists=True,
        binary_installed=True,
        owner_credentials_exist=True,
        owner_credentials_path=str(creds),
        homeserver_reachable=True,
        unit_active=True,
    )


def test_fresh_install_custom_identity_passes_through(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[False, True],  # custom password, toggle keep
        texts=["Example.COM ", "Custom.User", "my-very-strong-password"],
    )
    assert fake.provision_kwargs == {
        "server_name": "example.com",
        "owner_localpart": "custom.user",
        "owner_password": "my-very-strong-password",
    }
    assert "@custom.user:example.com" in out
    assert "my-very-strong-password" not in out
    assert remaining == []


def test_validation_loop_reprompts_until_valid(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True, True],
        texts=["bad name!", "mercury.local", "merc_bot", "merc-owner"],
    )
    assert "invalid server name" in out
    assert "must not start with 'merc_'" in out
    assert fake.provision_kwargs["server_name"] == "mercury.local"
    assert fake.provision_kwargs["owner_localpart"] == "merc-owner"
    assert remaining == []


def test_weak_custom_password_reprompts(monkeypatch, capsys):
    fake = _FakeProvision([_status()])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[False, True],
        texts=["", "", "short", "long-enough-password-1"],
    )
    assert "at least 12 characters" in out
    assert fake.provision_kwargs["owner_password"] == "long-enough-password-1"
    assert remaining == []


def test_provisioned_rerun_keeps_credentials_by_default(monkeypatch, capsys, tmp_path):
    """Keep-data re-run with empty answers keeps everything, honestly."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],  # toggle keep
        texts=["", "", ""],  # server keep, username keep, password keep
    )
    assert fake.provision_kwargs == {}
    assert fake.rotated == []
    assert "identity unchanged (kept existing data)" in out
    assert remaining == []

def test_provisioned_rerun_rotate_accepted(monkeypatch, capsys, tmp_path):
    """A typed password on the re-run triple rotates (never silently kept)."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],  # toggle keep
        texts=["", "", "rotated-password-99"],
    )
    assert fake.rotated == ["rotated-password-99"]
    assert "Owner password rotated" in out
    assert "rotated-password-99" not in out
    assert "identity unchanged" not in out
    assert remaining == []


def test_rotate_failure_degrades_and_wizard_continues(monkeypatch, capsys, tmp_path):
    """A failed rotation errors loudly — the typed password is never silent."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])

    def boom(new_password, *a, **k):
        raise RuntimeError("homeserver refused")

    monkeypatch.setattr(fake, "rotate_owner_password", boom)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["", "", "rotated-password-99"],
    )
    assert "Password rotation failed: homeserver refused" in out
    assert "mercury setup observatory" in out
    assert "identity unchanged" not in out
    assert remaining == []


def test_headless_setup_never_prompts(monkeypatch, capsys, tmp_path):
    """The non-interactive path provisions with defaults and asks nothing."""
    from observatory import provision as provision_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen: dict = {}

    class HeadlessProvision:
        def provision_in_wizard(self, *a, **k):
            seen.update(k)
            print("→ Matrix Observatory provisioning (Tuwunel)")
            return {}

        def status_summary(self, *a, **k):
            return {
                "provisioned": True,
                "homeserver_reachable": False,
                "homeserver_url": "http://127.0.0.1:18008",
                "unit_active": False,
                "unit_name": "mercury-observatory-homeserver.service",
                "enabled": True,
                "owner_credentials_path": str(tmp_path / "creds.json"),
            }

        def __getattr__(self, name):
            if name.startswith("ensure_") or name in (
                    "heal_owner_url", "verify_and_converge_gateway"):
                return lambda *a, **k: "skipped-test"
            raise AttributeError(name)

    fake = HeadlessProvision()
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)

    def no_prompt(*a, **k):
        raise AssertionError("headless path must not prompt")

    for helper in ("prompt", "prompt_choice", "prompt_yes_no"):
        monkeypatch.setattr(setup_mod, helper, no_prompt)
    setup_mod.run_headless_observatory_setup()
    assert seen == {}


def test_fresh_password_matching_username_reprompts(monkeypatch, capsys):
    """The triple prompt wires the username into password validation."""
    fake = _FakeProvision([_status()])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[False, True],  # custom password, toggle keep
        texts=["mybox.lan", "alicealice12", "alicealice12",
               "totally-different-password-1"],
    )
    assert "must not be the username itself" in out
    assert fake.provision_kwargs == {
        "server_name": "mybox.lan",
        "owner_localpart": "alicealice12",
        "owner_password": "totally-different-password-1",
    }
    assert remaining == []


def test_rotate_password_matching_stored_username_reprompts(
        monkeypatch, capsys, tmp_path):
    """Re-run password validates against the kept username, not just the floor."""
    creds = _write_credentials(tmp_path, user_id="@alicealice12:mercury.local")
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],  # toggle keep
        texts=["", "", "alicealice12", "rotated-password-99"],
    )
    assert "must not be the username itself" in out
    assert fake.rotated == ["rotated-password-99"]
    assert remaining == []


def test_rotate_without_credential_reader_keeps_length_floor(
        monkeypatch, capsys, tmp_path):
    """No credential reader (third-party double) degrades to the old floor."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    monkeypatch.setattr(fake, "read_owner_credentials", None)
    assert getattr(fake, "read_owner_credentials", None) is None
    assert setup_mod._rotate_owner_localpart(fake) is None
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["", "", "short", "rotated-password-99"],
    )
    assert "at least 12 characters" in out
    assert fake.rotated == ["rotated-password-99"]
    assert remaining == []

def test_rerun_offers_full_triple_with_keep_defaults(monkeypatch, capsys, tmp_path):
    """Keep-data re-run offers server + username + password (not password-only)."""
    creds = _write_credentials(tmp_path, user_id="@keepme:mercury.local")
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["", "", ""],
    )
    assert "Provisioned identity: @keepme:mercury.local" in out
    assert "Owner account will be @keepme:mercury.local." in out
    assert "identity unchanged (kept existing data)" in out
    assert fake.provision_kwargs == {}
    assert fake.rotated == []
    assert remaining == []


def test_rerun_server_change_errors_loudly_without_forking(monkeypatch, capsys, tmp_path):
    """A typed server-name change errors (immutable) — never silently kept."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["newbox.lan", "", ""],
    )
    assert "immutable once provisioned" in out
    assert "delete owner-credentials.json and tuwunel-db" in out
    assert fake.rotated == []
    # Repair still runs with the stored identity (never forks to the typed one).
    assert fake.provision_kwargs == {}
    assert remaining == []


def test_rerun_username_change_errors_loudly_without_forking(monkeypatch, capsys, tmp_path):
    """A typed username change errors (immutable) — never silently kept."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["", "brand-new-owner", ""],
    )
    assert "immutable once provisioned" in out
    assert fake.rotated == []
    assert fake.provision_kwargs == {}
    assert remaining == []


def test_rerun_typed_password_never_silently_drops(monkeypatch, capsys, tmp_path):
    """Typed-but-unapplied is impossible: a password either rotates or errors."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["", "", "typed-password-12345"],
    )
    applied = fake.rotated == ["typed-password-12345"]
    errored = ("Password rotation failed" in out
               or "not applied" in out)
    assert applied or errored
    assert not (fake.rotated == [] and "identity unchanged" in out), (
        "typed password was silently dropped as keep-everything")
    assert "typed-password-12345" not in out
    assert remaining == []


def test_rerun_password_failure_is_loud_not_silent_keep(monkeypatch, capsys, tmp_path):
    """Rotate failure on a typed password errors — never prints keep-everything."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])

    def boom(new_password, *a, **k):
        raise RuntimeError("login probe refused the new password")

    monkeypatch.setattr(fake, "rotate_owner_password", boom)
    out, _config, remaining = _run_section(
        monkeypatch, capsys, fake, choice=0,
        yes_no=[True],
        texts=["", "", "typed-password-12345"],
    )
    assert "Password rotation failed: login probe refused the new password" in out
    assert "identity unchanged" not in out
    assert remaining == []
