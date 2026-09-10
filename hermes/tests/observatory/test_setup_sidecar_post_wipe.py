"""Post-wipe sidecar reinstall (VM: wipe_observatory_data stops+deletes BOTH
units, but reprovision left no sidecar — unit file gone, nothing on 18090,
gateway Matrix-detached — and the wizard still reported success; the VM fix
was a manual --install-sidecar).

Law: a setup path that JUST wiped MUST reinstall+start the sidecar unit
itself, or fail loudly naming the exact retry command — never report a
detached stack as complete. Non-wipe installs keep the soft warning
(containers have no systemd; missing extras are advisory there).
"""

from __future__ import annotations

from pathlib import Path

import mercury_cli.setup as setup_mod
from mercury_cli.config import load_config
from observatory import provision as provision_mod
from observatory.config_gen import (
    HOMESERVER_PORT_DEFAULT,
    HOMESERVER_UNIT_NAME,
)

HOMESERVER_URL = f"http://127.0.0.1:{HOMESERVER_PORT_DEFAULT}"
_TS_ABSENT = {"available": False, "up": False, "ip": None, "dns_name": None}
RETRY_CMD = "mercury setup observatory --install-sidecar"


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
    def __init__(self, statuses, *, data_present=False, sidecar="installed",
                 sidecar_error=None, no_sidecar_installer=False):
        self._statuses = list(statuses)
        self._data_present = data_present
        self._sidecar = sidecar
        self._sidecar_error = sidecar_error
        self.calls = {"provision": 0, "status": 0, "wipe": 0,
                      "crypto": 0, "sidecar": 0, "heal": 0, "tree": 0}
        self.provision_kwargs: dict = {}
        self.wipe_modes: list = []
        if no_sidecar_installer:
            self.ensure_sidecar_unit = None  # type: ignore[assignment]
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

    def observatory_data_present(self, *a, **k):
        return self._data_present

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

    def detect_tailscale(self, *a, **k):
        return dict(_TS_ABSENT)

    def set_tuwunel_bind(self, ip, *a, **k):
        return ip

    def current_bind_address(self, *a, **k):
        return None

    def current_bind_addresses(self, *a, **k):
        return []


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


def _provisioned_statuses(tmp_path: Path):
    creds = tmp_path / "owner-credentials.json"
    creds.write_text('{"user_id": "@owner:mercury.local"}', encoding="utf-8")
    return [
        _status(),
        _status(provisioned=True, config_exists=True, binary_installed=True,
                owner_credentials_exist=True,
                owner_credentials_path=str(creds),
                homeserver_reachable=True, unit_active=True),
    ]


# ---------------------------------------------------------------------------
# re-run wipe branch (direct)
# ---------------------------------------------------------------------------


def test_rerun_wipe_sidecar_failure_is_loud_not_success(monkeypatch, capsys):
    """VM repro: wipe + reprovision where the sidecar reinstall throws must
    error loudly (naming the retry command) and must NOT print success."""
    fake = _FakeProvision([_status(provisioned=True)],
                          sidecar_error=RuntimeError("boom-sidecar"))
    rec = _Recorder(choices=[2], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    out = capsys.readouterr().out
    assert fake.wipe_modes == ["annihilate"]
    assert fake.calls["provision"] == 1
    assert "FAILED" in out
    assert RETRY_CMD in out
    assert "re-provisioned as @" not in out
    assert "NOT running" in out


def test_rerun_wipe_sidecar_success_reports_complete(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True)], sidecar="installed")
    rec = _Recorder(choices=[1], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    out = capsys.readouterr().out
    assert fake.wipe_modes == ["archive"]
    assert "re-provisioned as @merc-owner:mercury.local." in out
    assert "FAILED" not in out


def test_rerun_wipe_missing_installer_names_command(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True)], no_sidecar_installer=True)
    rec = _Recorder(choices=[2], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert RETRY_CMD in out
    assert "re-provisioned as @" not in out


# ---------------------------------------------------------------------------
# fresh install paths (full section)
# ---------------------------------------------------------------------------


def test_fresh_residual_wipe_sidecar_failure_is_loud(monkeypatch, capsys,
                                                     tmp_path):
    fake = _FakeProvision(_provisioned_statuses(tmp_path), data_present=True,
                          sidecar_error=RuntimeError("boom-sidecar"))
    rec = _Recorder(choices=[0, 1, 0], yes_no=[True, True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod.setup_observatory(load_config())
    out = capsys.readouterr().out
    assert fake.wipe_modes == ["archive"]
    assert "FAILED" in out
    assert RETRY_CMD in out
    assert "Observatory provisioning complete." not in out
    assert "NOT running" in out


def test_plain_install_sidecar_failure_stays_soft(monkeypatch, capsys, tmp_path):
    """Scope pin: a NON-wipe install keeps the advisory warning (containers
    have no systemd; the card still prints). Only post-wipe goes loud."""
    fake = _FakeProvision(_provisioned_statuses(tmp_path), data_present=False,
                          sidecar_error=RuntimeError("boom-sidecar"))
    rec = _Recorder(choices=[0, 0], yes_no=[True, True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod.setup_observatory(load_config())
    out = capsys.readouterr().out
    assert fake.wipe_modes == []
    assert "Sidecar unit install skipped" in out
    assert "FAILED" not in out
    assert "Observatory provisioning complete." in out
