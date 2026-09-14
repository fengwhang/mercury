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
    stack.enter_context(patch.object(setup_mod, "_prompt_server_label", return_value="mercury"))
    stack.enter_context(patch.object(setup_mod, "_wire_gateway_irc_env"))
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
    stack.enter_context(
        patch.object(
            setup_mod, "_verify_daemon_listening", return_value=(True, "mocked")
        )
    )
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


def test_verify_daemon_listening_live_and_dead():
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    live_port = srv.getsockname()[1]
    try:
        ok, detail = setup_mod._verify_daemon_listening(
            {"agent": f"127.0.0.1:{live_port}", "bouncer": "127.0.0.1:1"}
        )
        assert ok is False
        assert f"127.0.0.1:{live_port}" not in detail
        assert "127.0.0.1:1" in detail
        ok, _ = setup_mod._verify_daemon_listening(
            {"agent": f"127.0.0.1:{live_port}", "bouncer": f"127.0.0.1:{live_port}"}
        )
        assert ok is True
    finally:
        srv.close()


def test_verify_daemon_skips_empty_status():
    import mercury_cli.setup as setup_mod

    ok, detail = setup_mod._verify_daemon_listening({})
    assert ok is True
    assert "skipped" in detail


def test_auto_unit_partial_result_is_failure(capsys):
    import mercury_cli.setup as setup_mod

    class _PartialObs(_FakeObs):
        def ensure_observatory_unit(self):
            return "installed (start failed: boom)"

    assert setup_mod._auto_ensure_unit(_PartialObs(_base_status())) == "skipped-error"
    out = capsys.readouterr().out
    assert "NOT running" in out


def _fake_firewall_bin(tmp_path, *, state="running", open_ports=(), add_rc=0, reload_rc=0):
    """A fake firewall-cmd honoring --state/--query-port/--add-port/--reload."""
    bindir = tmp_path / "fwbin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "firewall-cmd"
    script.write_text(
        "#!/bin/sh\n"
        f"STATE={state}\n"
        f"OPEN='{ ' '.join(open_ports)}'\n"
        f"ADD_RC={add_rc}\n"
        f"RELOAD_RC={reload_rc}\n"
        'case "$1" in\n'
        '  --state) [ "$STATE" = running ] && exit 0 || exit 1;;\n'
        '  --query-port=*) p="${1#--query-port=}"; case " $OPEN " in *" $p "*) exit 0;; *) exit 1;; esac;;\n'
        '  --permanent) exit "$ADD_RC";;\n'
        '  --reload) exit "$RELOAD_RC";;\n'
        'esac\nexit 0\n',
        encoding="utf-8",
    )
    import stat as _stat

    script.chmod(script.stat().st_mode | _stat.S_IXUSR)
    return bindir


def _no_sudo(monkeypatch):
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda name: None if name in ("firewall-cmd", "sudo") else real_which(name),
    )


def test_firewall_unavailable_without_binary(monkeypatch, tmp_path):
    import mercury_cli.setup as setup_mod

    _no_sudo(monkeypatch)
    assert setup_mod._ensure_firewall_port(6670) == "unavailable"


def test_firewall_already_open(monkeypatch, tmp_path):
    import os

    import mercury_cli.setup as setup_mod

    bindir = _fake_firewall_bin(tmp_path, open_ports=("6670/tcp",))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert setup_mod._ensure_firewall_port(6670) == "already-open"


def test_firewall_opens_closed_port_as_root(monkeypatch, tmp_path):
    import os

    import mercury_cli.setup as setup_mod

    bindir = _fake_firewall_bin(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert setup_mod._ensure_firewall_port("6670") == "open"


def test_firewall_failure_degrades(monkeypatch, tmp_path):
    import os

    import mercury_cli.setup as setup_mod

    bindir = _fake_firewall_bin(tmp_path, add_rc=1)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert setup_mod._ensure_firewall_port(6670) == "failed"


def test_firewall_bad_port():
    import mercury_cli.setup as setup_mod

    assert setup_mod._ensure_firewall_port("nope") == "unavailable"


def test_verify_retries_restart_window(monkeypatch):
    import mercury_cli.setup as setup_mod

    calls = {"n": 0}

    def _flaky(addr):
        calls["n"] += 1
        return calls["n"] >= 3  # down, down, then up

    monkeypatch.setattr(setup_mod, "_probe_tcp", _flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok, detail = setup_mod._verify_daemon_listening(
        {"agent": "127.0.0.1:6669", "bouncer": "127.0.0.1:6670"})
    assert ok is True
    assert calls["n"] >= 3  # retried past the dead window


def test_verify_gives_up_with_both_down(monkeypatch):
    import mercury_cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_probe_tcp", lambda addr: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok, detail = setup_mod._verify_daemon_listening(
        {"agent": "127.0.0.1:6669", "bouncer": "127.0.0.1:6670"}, retries=2)
    assert ok is False
    assert "systemctl --user restart" in detail


def test_rotate_offer_with_chosen_password_restarts(monkeypatch, tmp_path):
    """Choosing your own password sets it and restarts the daemon."""
    import observatory.provision as provision_mod

    calls = {}
    monkeypatch.setattr(
        provision_mod, "set_bouncer_password",
        lambda home, pw: calls.setdefault("set", (str(home), pw)),
    )
    monkeypatch.setattr(
        provision_mod, "_mercury_home", lambda home: tmp_path / "mercury"
    )
    answers = iter([True, True, True])  # rotate, custom, restart now
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no", lambda *a, **k: next(answers)
    )
    monkeypatch.setattr(
        setup_mod, "_prompt_validated", lambda *a, **k: "my-chosen-pw"
    )
    ran = []
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: ran.append(a[0])
    )

    class _Obs:
        def validate_bouncer_password(self, value):
            return provision_mod.validate_bouncer_password(value)

    setup_mod._offer_bouncer_password_rotate(_Obs())
    assert calls["set"] == (str(tmp_path / "mercury"), "my-chosen-pw")
    assert ran and ran[0][:3] == ["systemctl", "--user", "restart"]


def test_rotate_offer_random_without_restart_prints_manual(monkeypatch, capsys):
    """Random rotate without restart leaves a manual command (no crash)."""
    import observatory.provision as provision_mod

    monkeypatch.setattr(
        provision_mod, "mirror_irc_env", lambda *a: None
    )
    monkeypatch.setattr(
        provision_mod, "read_irc_passwords",
        lambda home: {"bouncer": "old", "agent": "ag"},
    )
    monkeypatch.setattr(
        provision_mod, "generate_password", lambda *a: "new-random-pw"
    )
    answers = iter([True, False, False])  # rotate, generated, no restart
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no", lambda *a, **k: next(answers)
    )

    class _Obs:
        pass

    setup_mod._offer_bouncer_password_rotate(_Obs())
    assert "systemctl --user restart" in capsys.readouterr().out


def test_password_offer_runs_on_fresh_install():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status())
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=0)
        offer = stack.enter_context(
            patch.object(setup_mod, "_offer_bouncer_password_rotate")
        )
        setup_mod.setup_observatory({})
    assert fake.provision_calls == [{"server_name": "mercury"}]
    assert offer.call_count == 1


def test_password_offer_runs_on_reset_path():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status(provisioned=True))
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=0, yes_answers=[True])
        stack.enter_context(
            patch.object(setup_mod, "_offer_observatory_reset", return_value=True)
        )
        offer = stack.enter_context(
            patch.object(setup_mod, "_offer_bouncer_password_rotate")
        )
        setup_mod.setup_observatory({})
    assert offer.call_count == 1


def test_repair_path_wires_gateway():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status(provisioned=True))
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=0, yes_answers=[True])
        wire = stack.enter_context(
            patch.object(setup_mod, "_wire_gateway_irc_env")
        )
        setup_mod.setup_observatory({})
    assert wire.call_count == 1
    assert wire.call_args[0][0] == "mercury"


def test_restart_gateway_runs_mercury_restart(monkeypatch):
    ran = []
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/mercury")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: ran.append(a[0])
    )
    assert setup_mod._restart_gateway("test reason") is True
    assert ran == [["/bin/mercury", "gateway", "restart"]]


def test_restart_gateway_decline_prints_manual(monkeypatch, capsys):
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: False)
    assert setup_mod._restart_gateway("test reason") is False
    assert "mercury gateway restart" in capsys.readouterr().out
