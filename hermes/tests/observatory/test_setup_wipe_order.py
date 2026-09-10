"""Wipe-before-identity ordering (VM: setup asked identity BEFORE the
wipe/archive/keep question, so values typed against the pre-wipe state were
dropped or failed against it).

Ordering law: the wipe/archive/keep question comes FIRST; identity prompts
apply to the post-wipe state. Nothing collected pre-wipe can be silently
dropped — with wipe-first ordering there is nothing pre-wipe to drop.
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
    """observatory.provision stand-in with wipe + presence tracking."""

    def __init__(self, statuses, *, data_present=False):
        self._statuses = list(statuses)
        self._data_present = data_present
        self.calls = {"provision": 0, "status": 0, "wipe": 0,
                      "crypto": 0, "sidecar": 0, "heal": 0, "tree": 0}
        self.provision_kwargs: dict = {}
        self.wipe_modes: list = []
        self.validate_server_name = provision_mod.validate_server_name
        self.validate_owner_localpart = provision_mod.validate_owner_localpart
        self.validate_owner_password = provision_mod.validate_owner_password

    # -- provision surface --
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

    def rotate_owner_password(self, new_password, *a, **k):
        return "rotated"

    # -- auto steps --
    def ensure_crypto_stack(self, *a, **k):
        self.calls["crypto"] += 1
        return "ready"

    def ensure_sidecar_unit(self, *a, **k):
        self.calls["sidecar"] += 1
        return "installed"

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
    """Scripted prompt answers that record question order."""

    def __init__(self, *, choices, yes_no, texts):
        self.choices = list(choices)
        self.yes_no = list(yes_no)
        self.texts = list(texts)
        self.events: list = []

    def prompt_choice(self, question, options, default=0, description=None):
        self.events.append(("choice", question))
        assert self.choices, f"unexpected extra prompt_choice: {question!r}"
        return self.choices.pop(0)

    def prompt_yes_no(self, question, default=True):
        self.events.append(("yes_no", question))
        assert self.yes_no, f"unexpected extra prompt_yes_no: {question!r}"
        return self.yes_no.pop(0)

    def prompt(self, question, default=None, password=False):
        self.events.append(("prompt", question))
        assert self.texts, f"unexpected extra prompt: {question!r}"
        answer = self.texts.pop(0)
        return answer if answer else (default or "")

    def wipe_index(self):
        for i, (kind, q) in enumerate(self.events):
            if kind == "choice" and "what should happen" in q:
                return i
        return None

    def first_identity_index(self):
        for i, (kind, q) in enumerate(self.events):
            if kind == "prompt" and (
                "Homeserver name" in q or "Owner username" in q
            ):
                return i
        return None


def _install(monkeypatch, fake, rec):
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    monkeypatch.setattr(setup_mod, "prompt_choice", rec.prompt_choice)
    monkeypatch.setattr(setup_mod, "prompt_yes_no", rec.prompt_yes_no)
    monkeypatch.setattr(setup_mod, "prompt", rec.prompt)


# ---------------------------------------------------------------------------
# provisioned re-run: wipe FIRST, identity after
# ---------------------------------------------------------------------------


def test_rerun_annihilate_wipes_before_identity_prompts(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True)])
    rec = _Recorder(choices=[2], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    out_status = setup_mod._run_observatory_provisioned_rerun(
        fake, _status(provisioned=True))
    assert fake.wipe_modes == ["annihilate"]
    wi, ii = rec.wipe_index(), rec.first_identity_index()
    assert wi is not None and ii is not None and wi < ii, (
        f"wipe question must precede identity prompts: {rec.events!r}")
    # Post-wipe identity (defaults) reaches provisioning — nothing pre-wipe
    # was collected, so nothing could be dropped.
    assert fake.provision_kwargs == {
        "server_name": "mercury.local",
        "owner_localpart": "merc-owner",
        "owner_password": None,
    }
    assert fake.calls["provision"] == 1
    assert out_status is not None


def test_rerun_archive_wipes_before_identity_prompts(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True)])
    rec = _Recorder(choices=[1], yes_no=[True], texts=["", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    assert fake.wipe_modes == ["archive"]
    assert rec.wipe_index() < rec.first_identity_index()
    assert fake.provision_kwargs["server_name"] == "mercury.local"


def test_rerun_keep_runs_identity_flow_without_wipe(monkeypatch, capsys):
    fake = _FakeProvision([_status(provisioned=True)])
    # Keep, then per-field keep-everything ("", "" + empty password).
    rec = _Recorder(choices=[0], yes_no=[], texts=["", "", ""])
    _install(monkeypatch, fake, rec)
    setup_mod._run_observatory_provisioned_rerun(fake, _status(provisioned=True))
    assert fake.wipe_modes == []
    assert fake.calls["provision"] == 1
    # Unchanged identity repairs with the stored identity (no new values).
    assert fake.provision_kwargs == {}


# ---------------------------------------------------------------------------
# fresh install with residual data: wipe FIRST, identity after
# ---------------------------------------------------------------------------


def _run_section(monkeypatch, capsys, fake, rec):
    _install(monkeypatch, fake, rec)
    config = load_config()
    setup_mod.setup_observatory(config)
    return capsys.readouterr().out


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


def test_fresh_residual_archive_wipes_before_identity(monkeypatch, capsys,
                                                      tmp_path):
    fake = _FakeProvision(_provisioned_statuses(tmp_path), data_present=True)
    # Install, then Archive; identity defaults; generate pw; keep enabled.
    rec = _Recorder(choices=[0, 1, 0], yes_no=[True, True], texts=["", ""])
    _run_section(monkeypatch, capsys, fake, rec)
    assert fake.wipe_modes == ["archive"]
    wi, ii = rec.wipe_index(), rec.first_identity_index()
    assert wi is not None and ii is not None and wi < ii, (
        f"wipe question must precede identity prompts: {rec.events!r}")
    assert fake.provision_kwargs == {
        "server_name": "mercury.local",
        "owner_localpart": "merc-owner",
        "owner_password": None,
    }


def test_fresh_no_residual_asks_no_wipe_question(monkeypatch, capsys, tmp_path):
    fake = _FakeProvision(_provisioned_statuses(tmp_path), data_present=False)
    rec = _Recorder(choices=[0, 0], yes_no=[True, True], texts=["", ""])
    _run_section(monkeypatch, capsys, fake, rec)
    assert rec.wipe_index() is None
    assert fake.wipe_modes == []
    assert fake.calls["provision"] == 1


def test_fresh_residual_keep_provisions_with_typed_identity(monkeypatch, capsys,
                                                           tmp_path):
    fake = _FakeProvision(_provisioned_statuses(tmp_path), data_present=True)
    rec = _Recorder(choices=[0, 0, 0], yes_no=[True, True], texts=["", ""])
    _run_section(monkeypatch, capsys, fake, rec)
    assert fake.wipe_modes == []
    assert rec.wipe_index() < rec.first_identity_index()
    assert fake.provision_kwargs["server_name"] == "mercury.local"
