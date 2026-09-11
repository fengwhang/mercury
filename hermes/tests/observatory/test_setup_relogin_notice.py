"""Post-wipe re-login notice (VM 2026-09-11: Element X holds the OLD session
against the new server identity after annihilate + re-provision).

Pins: every wipe+reprovision path prints the loud DEAD-BY-CONSTRUCTION
re-login block (fresh MXID, fresh password, exactly ONE identity
verification), and the first-login card names FluffyChat AND Element X.
"""

from __future__ import annotations

import mercury_cli.setup as setup_mod
from observatory import provision as provision_mod
from observatory.config_gen import (
    HOMESERVER_PORT_DEFAULT,
    HOMESERVER_UNIT_NAME,
)

HOMESERVER_URL = f"http://127.0.0.1:{HOMESERVER_PORT_DEFAULT}"
_TS_ABSENT = {"available": False, "up": False, "ip": None, "dns_name": None}
MXID = "@merc-owner:mercury.local"


def _status(**over) -> dict:
    st = {
        "provisioned": False,
        "config_exists": False,
        "binary_installed": False,
        "owner_credentials_exist": False,
        "owner_credentials_path": "/nonexistent/owner-credentials.json",
        "homeserver_url": HOMESERVER_URL,
        "homeserver_reachable": False,
        "unit_active": False,
        "unit_name": HOMESERVER_UNIT_NAME,
        "enabled": True,
        "e2ee": False,
        "observatory_dir": "/nonexistent/observatory",
    }
    st.update(over)
    return st


class _FakeProvision:
    def __init__(self, statuses, *, sidecar="installed", sidecar_error=None):
        self._statuses = list(statuses)
        self._sidecar = sidecar
        self._sidecar_error = sidecar_error
        self.calls = {"provision": 0, "status": 0, "wipe": 0,
                      "crypto": 0, "sidecar": 0, "heal": 0, "tree": 0}
        self.provision_kwargs: dict = {}
        self.wipe_modes: list = []
        self.validate_server_name = provision_mod.validate_server_name
        self.validate_owner_localpart = provision_mod.validate_owner_localpart
        self.validate_owner_password = provision_mod.validate_owner_password

    def status_summary(self, *a, **k):
        idx = min(self.calls["status"], len(self._statuses) - 1)
        self.calls["status"] += 1
        return dict(self._statuses[idx])

    def provision_in_wizard(self, *a, **k):
        self.calls["provision"] += 1
        self.provision_kwargs = dict(k)
        return {}

    def wipe_observatory_data(self, *, mode, **k):
        self.calls["wipe"] += 1
        self.wipe_modes.append(mode)
        return {"mode": mode, "moved": ["tuwunel.toml"], "deleted": [],
                "units_removed": [], "env_stripped": []}

    def read_owner_credentials(self, *a, **k):
        return {"user_id": "@owner:mercury.local"}

    def ensure_crypto_stack(self, *a, **k):
        self.calls["crypto"] += 1
        return "ready"

    def ensure_sidecar_unit(self, *a, **k):
        self.calls["sidecar"] += 1
        if self._sidecar_error is not None:
            raise self._sidecar_error
        return self._sidecar

    def heal_owner_url(self, *a, **k):
        self.calls["heal"] += 1
        return None

    def verify_and_converge_gateway(self, *a, **k):
        self.calls["tree"] += 1
        return "converged-3"


class _Recorder:
    def __init__(self, *, choices, yes_no, texts):
        self.choices = list(choices)
        self.yes_no = list(yes_no)
        self.texts = list(texts)

    def prompt_choice(self, question, options, default=0, description=None):
        assert self.choices, f"unexpected extra prompt_choice: {question!r}"
        return self.choices.pop(0)

    def prompt_yes_no(self, question, default=True):
        assert self.yes_no, f"unexpected extra prompt_yes_no: {question!r}"
        return self.yes_no.pop(0)

    def prompt(self, question, default=None, password=False):
        assert self.texts, f"unexpected extra prompt: {question!r}"
        answer = self.texts.pop(0)
        return answer if answer else (default or "")


def _install(monkeypatch, fake, rec):
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    monkeypatch.setattr(setup_mod, "prompt_choice", rec.prompt_choice)
    monkeypatch.setattr(setup_mod, "prompt_yes_no", rec.prompt_yes_no)
    monkeypatch.setattr(setup_mod, "prompt", rec.prompt)


def _assert_notice(out: str, mxid: str = MXID) -> None:
    assert "DEAD BY CONSTRUCTION" in out
    assert "Element / FluffyChat" in out
    assert "log out / remove the old account" in out
    assert mxid in out
    assert "FRESH password" in out
    assert "MATRIX_OBS_OWNER_PASSWORD" in out
    assert "exactly ONE identity verification" in out
    assert "SECOND reset prompt" in out
    assert "something else is wrong" in out
    assert "mercury setup observatory" in out


def test_relogin_notice_pins_required_phrases(capsys):
    """Direct pin: the notice block states the dead session + fresh login."""
    setup_mod._print_observatory_relogin_notice(MXID)
    _assert_notice(capsys.readouterr().out)


def test_rerun_annihilate_prints_relogin_notice(monkeypatch, capsys):
    """Wipe-first re-run (annihilate) prints the notice with the fresh MXID."""
    fake = _FakeProvision([_status(provisioned=True)])
    rec = _Recorder(choices=[2], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    out = capsys.readouterr().out
    assert fake.wipe_modes == ["annihilate"]
    assert "re-provisioned as @merc-owner:mercury.local." in out
    _assert_notice(out, "@merc-owner:mercury.local")


def test_rerun_wipe_sidecar_failure_still_prints_notice(monkeypatch, capsys):
    """Sidecar reinstall failure still prints the notice — the session is
    dead regardless of the detached mirror."""
    fake = _FakeProvision([_status(provisioned=True)],
                          sidecar_error=RuntimeError("boom-sidecar"))
    rec = _Recorder(choices=[2], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    out = capsys.readouterr().out
    assert "NOT running" in out
    _assert_notice(out, "@merc-owner:mercury.local")


def test_card_names_fluffychat_and_element_x(capsys, tmp_path):
    """First-login card generalizes off FluffyChat-only wording."""
    creds = tmp_path / "owner-credentials.json"
    creds.write_text('{"user_id": "@owner:mercury.local"}', encoding="utf-8")
    setup_mod._print_observatory_setup_card(
        {
            "owner_credentials_path": str(creds),
            "homeserver_url": HOMESERVER_URL,
            "e2ee": False,
        },
        dict(_TS_ABSENT),
    )
    out = capsys.readouterr().out
    assert "Matrix Observatory — first login (FluffyChat / Element X)" in out
    assert "in FluffyChat / Element X:" in out
    assert "paste it into FluffyChat / Element X" in out
    assert "after an identity reset in Element/Element X:" in out
    assert "mercury observatory trust-device" in out
