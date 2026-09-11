"""Setup-auto on vendored crypto: `mercury setup observatory` runs everything.

Covers the orchestration provision.py + setup.py provide: fail-closed
vendored crypto install, sidecar unit, heal+converge, headless parity,
and idempotency. Fakes the network/unit layers; asserts printed lines
(the user's evidence) plus call counts.

FORBIDDEN paths (never re-add): podman builds, and any automatic
``observatory.e2ee: false`` write — crypto failure fails CLOSED.
"""
from __future__ import annotations

import builtins
import json
import sys
import types
from argparse import Namespace
from pathlib import Path

import mercury_cli.setup as setup_mod
import observatory.e2ee as e2ee_mod
from observatory import provision as provision_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hermes"))
from mercury_cli.config import load_config  # noqa: E402


PASSWORD = "s3cr3t-observatory-owner-password"
MXID = "@owner:mercury.local"
HOMESERVER_URL = "http://127.0.0.1:18008"
FAKE_WHEEL = Path("/fake/wheels/python_olm-3.2.16-cp313-cp313-linux_x86_64.whl")


def _status(**over) -> dict:
    from observatory.config_gen import HOMESERVER_UNIT_NAME

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
        "e2ee": True,
        "observatory_dir": "/nonexistent/observatory",
    }
    st.update(over)
    return st


def _write_credentials(home: Path) -> Path:
    obs = home / "observatory"
    obs.mkdir(parents=True, exist_ok=True)
    creds = obs / "owner-credentials.json"
    creds.write_text(json.dumps({
        "homeserver_url": HOMESERVER_URL, "user_id": MXID,
        "password": PASSWORD, "access_token": "", "device_id": "",
    }), encoding="utf-8")
    return creds


class _FakeProvision:
    def __init__(self, statuses, **kw):
        self._statuses = list(statuses)
        self._crypto = kw.get("crypto", "ready")
        self._sidecar = kw.get("sidecar", "installed")
        self._healed = kw.get("healed")
        self._tree = kw.get("tree", "converged-3")
        self._crypto_error = kw.get("crypto_error")
        self._sidecar_error = kw.get("sidecar_error")
        self._tree_error = kw.get("tree_error")
        self._provision_error = kw.get("provision_error")
        self._tailscale = dict(kw.get("tailscale") or
                               {"available": False, "up": False, "ip": None, "dns_name": None})
        self._bind_address = kw.get("bind_address")
        self.calls = {"provision": 0, "status": 0, "crypto": 0,
                      "sidecar": 0, "heal": 0, "tree": 0, "bind": 0}
        self.provision_kwargs: dict = {}
        self.rotated: list = []
        # Identity surface mirrors observatory.provision (real validators).
        self.validate_server_name = provision_mod.validate_server_name
        self.validate_owner_localpart = provision_mod.validate_owner_localpart
        self.validate_owner_password = provision_mod.validate_owner_password

    def read_owner_credentials(self, *a, **k):
        import json as _json
        from pathlib import Path as _Path
        try:
            path = (self._statuses[0] or {}).get("owner_credentials_path")
            if path:
                doc = _json.loads(_Path(path).read_text(encoding="utf-8"))
                if isinstance(doc, dict) and doc.get("user_id"):
                    return {"user_id": str(doc["user_id"])}
        except Exception:  # noqa: BLE001 — test double falls back to default
            pass
        return {"user_id": MXID}

    def rotate_owner_password(self, new_password, *a, **k):
        self.rotated.append(new_password)
        return "rotated"

    def status_summary(self, *a, **k):
        idx = min(self.calls["status"], len(self._statuses) - 1)
        self.calls["status"] += 1
        return dict(self._statuses[idx])

    def provision_in_wizard(self, *a, **k):
        self.provision_kwargs = dict(k)
        self.calls["provision"] += 1
        if self._provision_error is not None:
            raise self._provision_error
        print("→ Matrix Observatory provisioning (Tuwunel)")
        return {"tuwunel": {"action": "current", "version": "1.9.0"}}

    def ensure_crypto_stack(self, *a, **k):
        self.calls["crypto"] += 1
        if self._crypto_error is not None:
            raise self._crypto_error
        return self._crypto

    def ensure_sidecar_unit(self, *a, **k):
        self.calls["sidecar"] += 1
        if self._sidecar_error is not None:
            raise self._sidecar_error
        return self._sidecar

    def heal_owner_url(self, *a, **k):
        self.calls["heal"] += 1
        return self._healed

    def verify_and_converge_gateway(self, *a, **k):
        self.calls["tree"] += 1
        if self._tree_error is not None:
            raise self._tree_error
        return self._tree

    def detect_tailscale(self, *a, **k):
        return dict(self._tailscale)

    def current_bind_address(self, *a, **k):
        return self._bind_address


def _run_install(monkeypatch, capsys, fake, *, yes_no=(True,), texts=("", "")):
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    monkeypatch.setattr(setup_mod, "prompt_choice", lambda q, c, d=0, description=None: 0)
    remaining = list(yes_no)

    def fake_yes_no(question, default=True):
        assert remaining, f"unexpected prompt: {question!r}"
        return remaining.pop(0)

    monkeypatch.setattr(setup_mod, "prompt_yes_no", fake_yes_no)
    pending_texts = list(texts)

    def fake_prompt(question, default=None, password=False):
        # Mirrors setup.prompt: empty input selects the default.
        assert pending_texts, f"unexpected extra prompt: {question!r}"
        answer = pending_texts.pop(0)
        return answer if answer else (default or "")

    monkeypatch.setattr(setup_mod, "prompt", fake_prompt)
    setup_mod.setup_observatory(load_config())
    assert not pending_texts, f"unconsumed prompt answers: {pending_texts!r}"
    return capsys.readouterr().out, remaining


def _patch_vendored_platform(monkeypatch):
    """Force the linux-cp313 vendored-wheel branch deterministically."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    monkeypatch.setattr(provision_mod, "_vendored_olm_wheel", lambda: FAKE_WHEEL)


def test_wizard_install_runs_full_auto_path(monkeypatch, capsys, tmp_path):
    """Install choice: provision → crypto → sidecar → heal/converge, then card."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_status(), _status(provisioned=True, owner_credentials_exist=True,
                            owner_credentials_path=str(creds),
                            homeserver_reachable=True, unit_active=True)],
        healed=HOMESERVER_URL,
    )
    out, remaining = _run_install(monkeypatch, capsys, fake, yes_no=(True, True, True))
    assert fake.calls == {"provision": 1, "status": 3, "crypto": 1,
                          "sidecar": 1, "heal": 1, "tree": 1, "bind": 0}
    # fresh install: prompted identity (defaults) reaches provisioning
    assert fake.provision_kwargs == {
        "server_name": "mercury.local",
        "owner_localpart": "merc-owner",
        "owner_password": None,
    }
    assert "Sidecar unit installed" in out
    assert "Owner homeserver URL healed" in out
    assert "Gateway tree converged-3" in out
    assert "Matrix Observatory — first login (FluffyChat / Element X)" in out
    assert PASSWORD not in out
    assert remaining == []


def test_wizard_install_failed_crypto_warns_and_keeps_e2ee(monkeypatch, capsys, tmp_path):
    """Crypto unrestorable → fail-closed warning, no silent downgrade, card still prints."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_status(), _status(provisioned=True, owner_credentials_exist=True,
                            owner_credentials_path=str(creds))],
        crypto="failed: no vendored python-olm wheel for this machine",
    )
    out, _ = _run_install(monkeypatch, capsys, fake, yes_no=(True, True, True))
    assert "Crypto stack not ready" in out
    assert "E2EE stays ON" in out
    assert "e2ee:false" not in out and "e2ee: false" not in out
    assert "Matrix Observatory — first login (FluffyChat / Element X)" in out


def test_wizard_install_each_auto_failure_degrades_independently(monkeypatch, capsys, tmp_path):
    """Crypto/sidecar/tree errors warn but never kill the wizard or the card."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_status(), _status(provisioned=True, owner_credentials_exist=True,
                            owner_credentials_path=str(creds))],
        crypto_error=RuntimeError("boom-crypto"),
        sidecar_error=RuntimeError("boom-sidecar"),
        tree_error=RuntimeError("boom-tree"),
    )
    out, remaining = _run_install(monkeypatch, capsys, fake, yes_no=(True, True, True))
    assert "Crypto auto-setup skipped" in out
    assert "Sidecar unit install skipped" in out
    assert "Gateway tree converge skipped" in out
    assert "Matrix Observatory — first login (FluffyChat / Element X)" in out
    assert remaining == []


def test_wizard_install_idempotent_double_run(monkeypatch, capsys, tmp_path):
    """Two Install runs: every auto step re-runs cleanly (idempotent)."""
    creds = _write_credentials(tmp_path)
    st = _status(provisioned=True, owner_credentials_exist=True,
                 owner_credentials_path=str(creds))
    fake = _FakeProvision([st, st, st])
    out, remaining = _run_install(
        monkeypatch, capsys, fake, yes_no=(True, True), texts=("", "", ""))
    assert remaining == []
    out, remaining = _run_install(
        monkeypatch, capsys, fake, yes_no=(True, True), texts=("", "", ""))
    assert remaining == []
    # re-runs offer the triple (empty keeps) and pass no identity onwards.
    assert fake.provision_kwargs == {}
    assert fake.rotated == []
    assert fake.calls["provision"] == 2
    assert fake.calls["crypto"] == 2
    assert fake.calls["sidecar"] == 2
    assert fake.calls["tree"] == 2


def test_repair_path_runs_auto_steps(monkeypatch, capsys):
    """--install-sidecar alias runs the same auto steps (no prompts)."""
    fake = _FakeProvision([_status(provisioned=True)], healed=HOMESERVER_URL)
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    setup_mod._run_observatory_sidecar_repair()
    out = capsys.readouterr().out
    assert fake.calls["provision"] == 1
    assert fake.calls["crypto"] == 1
    assert fake.calls["sidecar"] == 1
    assert fake.calls["tree"] == 1
    assert "Observatory provisioning complete." in out


def test_headless_runs_auto_with_zero_prompts(monkeypatch, capsys, tmp_path):
    """Headless setup: provision + auto + card, prompt fns never called."""
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision(
        [_status(provisioned=True, owner_credentials_exist=True,
                 owner_credentials_path=str(creds),
                 homeserver_reachable=True)],
        healed=HOMESERVER_URL,
    )
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)

    def _no_prompt(*a, **k):
        raise AssertionError("headless path must not prompt")

    monkeypatch.setattr(setup_mod, "prompt_choice", _no_prompt)
    monkeypatch.setattr(setup_mod, "prompt_yes_no", _no_prompt)
    setup_mod.run_headless_observatory_setup()
    out = capsys.readouterr().out
    assert fake.calls["provision"] == 1
    assert fake.calls["crypto"] == 1
    assert fake.calls["sidecar"] == 1
    assert fake.calls["tree"] == 1
    assert "Matrix Observatory — first login (FluffyChat / Element X)" in out
    assert PASSWORD not in out


def test_headless_dispatch_via_run_setup_wizard(monkeypatch, capsys, tmp_path):
    """`mercury setup observatory` headless dispatches to the auto path."""
    creds = _write_credentials(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake = _FakeProvision(
        [_status(provisioned=True, owner_credentials_exist=True,
                 owner_credentials_path=str(creds))],
    )
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
    args = Namespace(non_interactive=True, section="observatory", reset=False,
                     portal=False, quick=False, reconfigure=False,
                     install_sidecar=False)
    setup_mod.run_setup_wizard(args)
    out = capsys.readouterr().out
    assert fake.calls["provision"] == 1
    assert "Matrix Observatory — first login (FluffyChat / Element X)" in out


# --- provision.ensure_crypto_stack (vendored, fail-closed) ---------------------


def _write_home_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_crypto_ready_when_stack_present(tmp_path, monkeypatch):
    _write_home_config(tmp_path, "observatory:\n  e2ee: true\n")
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)
    assert provision_mod.ensure_crypto_stack(tmp_path) == "ready"
    assert "e2ee: true" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_crypto_disabled_already_is_noop(tmp_path, monkeypatch):
    """Explicit operator opt-out: no install attempted, no write."""
    _write_home_config(tmp_path, "observatory:\n  e2ee: false\n")
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: False)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)
    called = []
    monkeypatch.setattr(
        provision_mod, "_crypto_pip_install",
        lambda *a, **k: called.append(True) or (True, ""))
    assert provision_mod.ensure_crypto_stack(tmp_path) == "disabled-already"
    assert called == []


def test_crypto_delegates_to_vendored_install(monkeypatch):
    """Missing stack → the vendored wheel (hash-verified) reaches the
    installer with the matrix companions, then reports installed."""
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
    calls: list = []

    def _fake_install(python_bin, args):
        calls.append((python_bin, list(args)))
        return True, ""
    monkeypatch.setattr(provision_mod, "_crypto_pip_install", _fake_install)
    monkeypatch.setattr(
        provision_mod, "_verified_vendored_wheel", lambda cand: cand)
    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "olm":
            return types.ModuleType("olm")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _fake_import)
    _patch_vendored_platform(monkeypatch)
    assert provision_mod.ensure_crypto_stack() == "installed"
    assert len(calls) == 1
    python_bin, args = calls[0]
    assert python_bin == sys.executable
    wheel_args = [a for a in args if a.endswith(".whl")]
    assert wheel_args == [str(FAKE_WHEEL)]


def test_crypto_install_failure_fails_closed(tmp_path, monkeypatch, capsys):
    """Installer error: fail CLOSED — config.yaml untouched, retry printed."""
    _write_home_config(tmp_path, "observatory:\n  e2ee: true\nmodel:\n  provider: x\n")
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
    monkeypatch.setattr(
        provision_mod, "_crypto_pip_install", lambda *a, **k: (False, "boom"))
    result = provision_mod.ensure_crypto_stack(tmp_path)
    assert result.startswith("failed:")
    out = capsys.readouterr().out
    assert "E2EE stays ON" in out
    assert "--install-sidecar" in out
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "e2ee: true" in text  # never downgraded
    assert "provider: x" in text


def test_crypto_missing_wheel_fails_closed_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
    monkeypatch.setattr(provision_mod, "_vendored_olm_wheel", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))
    home = tmp_path / "home"
    assert provision_mod.ensure_crypto_stack(home).startswith("failed:")
    assert "E2EE stays ON" in capsys.readouterr().out
    assert not (home / "config.yaml").exists()


def test_crypto_hash_mismatch_fails_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)

    def _raise(cand):
        raise provision_mod.ProvisionError("hash mismatch")
    monkeypatch.setattr(provision_mod, "_verified_vendored_wheel", _raise)
    _patch_vendored_platform(monkeypatch)
    home = tmp_path / "home"
    assert provision_mod.ensure_crypto_stack(home).startswith("failed:")
    assert "E2EE stays ON" in capsys.readouterr().out
    assert not (home / "config.yaml").exists()


def test_crypto_never_raises_without_config(tmp_path, monkeypatch, capsys):
    """Missing config.yaml + missing stack → fail-closed, no crash, no write."""
    monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
    monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
    monkeypatch.setattr(
        provision_mod, "_crypto_pip_install", lambda *a, **k: (False, "boom"))
    home = tmp_path / "home"
    assert provision_mod.ensure_crypto_stack(home).startswith("failed:")
    assert "E2EE stays ON" in capsys.readouterr().out
    assert not (home / "config.yaml").exists()


def test_set_e2ee_preserves_other_keys(tmp_path):
    _write_home_config(
        tmp_path, "model:\n  provider: openrouter\nobservatory:\n  enabled: true\n")
    provision_mod.set_observatory_e2ee(False, tmp_path)
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "e2ee: false" in text
    assert "provider: openrouter" in text
    assert "enabled: true" in text
    provision_mod.set_observatory_e2ee(True, tmp_path)
    assert "e2ee: true" in (tmp_path / "config.yaml").read_text(encoding="utf-8")


def test_heal_unprovisioned_returns_default_url_without_creating_creds(tmp_path):
    url = provision_mod.heal_owner_url(tmp_path)
    assert url == "http://127.0.0.1:18008"
    assert not (tmp_path / "observatory" / "owner-credentials.json").exists()


def test_converge_unprovisioned_defers(tmp_path):
    assert provision_mod.verify_and_converge_gateway(tmp_path).startswith("deferred:")
