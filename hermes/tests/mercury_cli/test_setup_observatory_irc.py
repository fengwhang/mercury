"""IRC observatory wizard tests (fresh behavior, fake provision module)."""

from __future__ import annotations

from unittest.mock import patch

import mercury_cli.setup as setup_mod


class _FakeObs:
    def __init__(self, status, **kw):
        self._status = dict(status)
        self.provision_calls: list[dict] = []
        self.bind_calls: list[str] = []
        self.validate_calls: list[str] = []
        self.converge_calls = 0
        self.unit_calls = 0
        self._kw = kw

    def status_summary(self):
        return dict(self._status)

    def provision_in_wizard(self, **kwargs):
        self.provision_calls.append(kwargs)
        self._status["provisioned"] = True

    def validate_server_name(self, value):
        self.validate_calls.append(value)
        return str(value).strip().lower()

    def verify_and_converge_gateway(self):
        self.converge_calls += 1
        return "converged-gateway (#mercury_gateway)"

    def ensure_observatory_unit(self):
        self.unit_calls += 1
        return "installed"

    def set_ircd_bind(self, ip):
        self.bind_calls.append(ip)
        return ip

    def current_listen_addresses(self):
        return self._kw.get("addrs", ["127.0.0.1", "127.0.0.1"])

    def detect_tailscale(self):
        return dict(
            self._kw.get(
                "ts", {"available": False, "up": False, "ip": None, "dns_name": None}
            )
        )


def _base_status(**kw):
    status = {
        "enabled": True,
        "provisioned": False,
        "server_name": "mercury",
        "agent": "127.0.0.1:6669",
        "bouncer": "127.0.0.1:6670",
        "unit": "active",
        "bouncer_password_set": True,
        "agent_password_set": True,
        "config_path": "/h/observatory/ircd.json",
    }
    status.update(kw)
    return status


def _patch_common(stack, fake, *, choice=1, yes_answers=None):
    stack.enter_context(
        patch.object(setup_mod, "_load_observatory_provision", return_value=fake)
    )
    stack.enter_context(patch.object(setup_mod, "prompt_choice", return_value=choice))
    stack.enter_context(patch.object(setup_mod, "_prompt_observatory_enabled_toggle"))
    stack.enter_context(patch.object(setup_mod, "_print_observatory_setup_card"))
    stack.enter_context(patch.object(setup_mod, "_offer_tailscale_bind"))
    stack.enter_context(patch.object(setup_mod, "_maybe_print_bind_mismatch_action"))
    stack.enter_context(
        patch.object(
            setup_mod,
            "_tailscale_status",
            return_value={
                "available": False,
                "up": False,
                "ip": None,
                "dns_name": None,
            },
        )
    )
    stack.enter_context(
        patch.object(setup_mod, "_offer_observatory_reset", return_value=False)
    )
    stack.enter_context(patch.object(setup_mod, "_offer_bouncer_password_rotate"))
    stack.enter_context(
        patch.object(setup_mod, "_prompt_server_label", return_value="mercury")
    )
    auto = stack.enter_context(
        patch.object(
            setup_mod,
            "_run_observatory_auto_steps",
            return_value={"unit": "installed", "gateway": "converged"},
        )
    )
    answers = list(yes_answers or [])

    def _yes_no(question, default=True):
        return answers.pop(0) if answers else default

    stack.enter_context(patch.object(setup_mod, "prompt_yes_no", _yes_no))
    return {"auto": auto}


def test_fresh_install_provisions_with_label():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status())
    with ExitStack() as stack:
        mocks = _patch_common(stack, fake, choice=0)
        setup_mod.setup_observatory({})
    assert fake.provision_calls == [{"server_name": "mercury"}]
    mocks["auto"].assert_called_once()


def test_skip_leaves_everything_alone():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status())
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=1)
        setup_mod.setup_observatory({})
    assert fake.provision_calls == []
    assert fake.unit_calls == 0


def test_reconfigure_no_keeps_everything():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status(provisioned=True))
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=1, yes_answers=[False])
        setup_mod.setup_observatory({})
    assert fake.provision_calls == []


def test_bind_offer_pins_bouncer(monkeypatch):
    fake = _FakeObs(_base_status(provisioned=True))
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda q, default=False: True)
    monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: None)
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (
            (_ for _ in ()).throw(AssertionError("should not restart in this test"))
            if False
            else None
        ),
    )

    # restart path: make subprocess.run raise so we take the warn branch
    def _boom(*a, **k):
        raise OSError("no systemctl here")

    monkeypatch.setattr(subprocess, "run", _boom)
    ts = {"available": True, "up": True, "ip": "100.64.0.1", "dns_name": None}
    setup_mod._offer_tailscale_bind(fake, ts)
    assert fake.bind_calls == ["100.64.0.1"]


def test_bind_mismatch_action_lines():
    ts_up = {"available": True, "up": True, "ip": "100.64.0.1", "dns_name": None}
    ts_down = {"available": False, "up": False, "ip": None, "dns_name": None}
    assert setup_mod._bind_mismatch_action_line(["127.0.0.1"], ts_up) is not None
    assert (
        setup_mod._bind_mismatch_action_line(["100.64.0.1", "127.0.0.1"], ts_up) is None
    )
    assert setup_mod._bind_mismatch_action_line(["127.0.0.1"], ts_down) is None
    assert setup_mod._bind_mismatch_action_line(None, ts_up) is None


def test_bouncer_port_parsing():
    assert setup_mod._bouncer_port("127.0.0.1:6670") == "6670"
    assert setup_mod._bouncer_port("") == "6670"


def test_headless_setup_provisions():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status())
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(setup_mod, "_load_observatory_provision", return_value=fake)
        )
        stack.enter_context(
            patch.object(
                setup_mod,
                "_run_observatory_auto_steps",
                return_value={"unit": "installed", "gateway": "ok"},
            )
        )
        setup_mod.run_headless_observatory_setup()
    assert fake.provision_calls == [{}]


def test_setup_card_mentions_bouncer_not_password(capsys):
    status = _base_status(provisioned=True)
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    out = capsys.readouterr().out
    assert "127.0.0.1:6670" in out
    assert "#mercury_gateway" in out
    assert "IRC_BOUNCER_PASSWORD" in out
