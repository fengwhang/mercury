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
        "server": "127.0.0.1:6670",
        "unit": "active",
        "server_password_set": True,
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
    stack.enter_context(patch.object(setup_mod, "_offer_server_password_rotate"))
    stack.enter_context(patch.object(setup_mod, "_prompt_server_label", return_value="mercury"))
    stack.enter_context(patch.object(setup_mod, "_wire_gateway_irc_env"))
    stack.enter_context(patch.object(setup_mod, "_offer_agent_bind"))
    stack.enter_context(patch.object(setup_mod, "_offer_lounge"))
    stack.enter_context(patch.object(setup_mod, "_offer_lounge_password_reset"))
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


def test_bind_offer_pins_server(monkeypatch):
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


def test_server_port_parsing():
    assert setup_mod._server_port("127.0.0.1:6670") == "6670"
    assert setup_mod._server_port("") == "6670"


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


def test_setup_card_mentions_server_not_password(capsys):
    status = _base_status(provisioned=True)
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    out = capsys.readouterr().out
    assert "127.0.0.1:6670" in out
    assert "#mercury_gateway" in out
    assert "IRC_CLIENT_PASSWORD" in out
    assert "server address" in out
    assert "client address" not in out
    assert "The Lounge" in out


def test_setup_card_lounge_login(monkeypatch, capsys):
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": True, "users": ["owner"],
                         "host": "100.9.9.9", "port": 9000,
                         "unit": "active", "binary": "/b"})
    status = _base_status(provisioned=True)
    status["agent"] = "127.0.0.1:6669"
    status["server_name"] = "vm"
    setup_mod._print_observatory_setup_card(
        status, dict(available=True, up=True, ip="100.9.9.9",
                     dns_name=None)
    )
    out = capsys.readouterr().out
    assert "http://100.9.9.9:9000" in out
    assert "user 'owner'" in out
    assert "pre-added" in out
    assert "#vm_gateway" in out


def test_setup_card_lounge_missing(monkeypatch, capsys):
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": False, "users": [],
                         "host": "", "port": 0, "external": False,
                         "unit": "inactive", "binary": ""})
    status = _base_status(provisioned=True)
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    assert "not installed" in capsys.readouterr().out


def test_setup_card_lounge_external(monkeypatch, capsys):
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": False, "users": [],
                         "host": "127.0.0.1", "port": 9000,
                         "external": True, "unit": "unknown",
                         "binary": ""})
    status = _base_status(provisioned=True)
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    out = capsys.readouterr().out
    assert "runs outside" in out
    assert "http://127.0.0.1:9000" in out


def test_setup_card_existing_lounge_block(capsys):
    status = _base_status(provisioned=True)
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    out = capsys.readouterr().out
    assert "another Lounge" in out
    assert "#mercury_gateway" in out


def test_verify_daemon_listening_live_and_dead():
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    live_port = srv.getsockname()[1]
    try:
        ok, detail = setup_mod._verify_daemon_listening(
            {"agent": f"127.0.0.1:{live_port}", "server": "127.0.0.1:1"}
        )
        assert ok is False
        assert f"127.0.0.1:{live_port}" not in detail
        assert "127.0.0.1:1" in detail
        ok, _ = setup_mod._verify_daemon_listening(
            {"agent": f"127.0.0.1:{live_port}", "server": f"127.0.0.1:{live_port}"}
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
        {"agent": "127.0.0.1:6669", "server": "127.0.0.1:6670"})
    assert ok is True
    assert calls["n"] >= 3  # retried past the dead window


def test_verify_gives_up_with_both_down(monkeypatch):
    import mercury_cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_probe_tcp", lambda addr: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok, detail = setup_mod._verify_daemon_listening(
        {"agent": "127.0.0.1:6669", "server": "127.0.0.1:6670"}, retries=2)
    assert ok is False
    assert "systemctl --user restart" in detail


def test_rotate_offer_with_chosen_password_restarts(monkeypatch, tmp_path):
    """Choosing your own password sets it and restarts the daemon."""
    import observatory.provision as provision_mod

    calls = {}
    monkeypatch.setattr(
        provision_mod, "set_server_password",
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
        def validate_server_password(self, value):
            return provision_mod.validate_server_password(value)

    setup_mod._offer_server_password_rotate(_Obs())
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
        lambda home: {"server": "old", "agent": "ag"},
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

    setup_mod._offer_server_password_rotate(_Obs())
    assert "systemctl --user restart" in capsys.readouterr().out


def test_password_offer_runs_on_fresh_install():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status())
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=0)
        offer = stack.enter_context(
            patch.object(setup_mod, "_offer_server_password_rotate")
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
            patch.object(setup_mod, "_offer_server_password_rotate")
        )
        setup_mod.setup_observatory({})
    assert offer.call_count == 1


def test_repair_path_offers_agent_bind():
    from contextlib import ExitStack

    fake = _FakeObs(_base_status(provisioned=True))
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=0, yes_answers=[True])
        offer = stack.enter_context(
            patch.object(setup_mod, "_offer_agent_bind")
        )
        setup_mod.setup_observatory({})
    assert offer.call_count == 1
    assert offer.call_args[0][1] == "mercury"


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


def test_tail_offers_lounge_layer(monkeypatch):
    from contextlib import ExitStack
    from unittest.mock import patch

    fake = _FakeObs(_base_status(provisioned=True))
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=1, yes_answers=[True])
        offer = stack.enter_context(
            patch.object(setup_mod, "_offer_lounge"))
        setup_mod.setup_observatory({})
    assert offer.call_count == 1


def test_wire_gateway_sets_allow_all(monkeypatch):
    """Observatory wiring disables the per-nick gate (D7 perimeter)."""
    import observatory.provision as provision_mod

    saved = {}
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(
        setup_mod, "save_env_value", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(
        setup_mod, "_restart_gateway", lambda *a, **k: True)
    monkeypatch.setattr(
        provision_mod, "_mercury_home", lambda home: "/h")
    monkeypatch.setattr(
        provision_mod, "read_config",
        lambda home: {"server_name": "vm", "agent_host": "127.0.0.1",
                      "agent_port": 6669})
    monkeypatch.setattr(
        provision_mod, "read_irc_passwords",
        lambda home: {"server": "b", "agent": "a"})
    assert setup_mod._wire_gateway_irc_env("vm") is True
    assert saved["IRC_ALLOW_ALL_USERS"] == "true"
    assert saved["IRC_CHANNEL"] == "#vm_gateway"
    assert saved["IRC_NICKNAME"] == "vm_gateway"


def test_provisioned_flow_ends_with_gateway_restart():
    """The gateway restarts AFTER the soju/ircd converge (final step)."""
    from contextlib import ExitStack
    from unittest.mock import patch

    fake = _FakeObs(_base_status(provisioned=True))
    with ExitStack() as stack:
        _patch_common(stack, fake, choice=0, yes_answers=[True])
        restart = stack.enter_context(
            patch.object(setup_mod, "_restart_gateway")
        )
        setup_mod.setup_observatory({})
    assert restart.call_count == 1
    assert "final step" in restart.call_args[0][0]


def test_offer_agent_bind_localhost_wires_env(tmp_path, monkeypatch) -> None:
    """Localhost choice pins 127.0.0.1 and wires the gateway env."""
    import json as _json

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "ircd.json").write_text(_json.dumps({"server_name": "vm"}))
    saved = {}
    monkeypatch.setattr(setup_mod, "prompt_choice", lambda *a, **k: 0)
    monkeypatch.setattr(setup_mod, "save_env_value",
                        lambda k, v: saved.__setitem__(k, v))
    import observatory.provision as provision_mod
    monkeypatch.setattr(provision_mod, "read_irc_passwords",
                        lambda home: {"server": "b", "agent": "a"})
    setup_mod._offer_agent_bind(None, "vm", {"up": False})
    cfg = _json.loads((obs / "ircd.json").read_text())
    assert cfg["agent_host"] == "127.0.0.1"
    assert saved["IRC_SERVER"] == "127.0.0.1"
    assert saved["IRC_NICKNAME"] == "vm_gateway"


def test_offer_agent_bind_tailscale_pins_ip(tmp_path, monkeypatch) -> None:
    """Tailscale choice pins the tailnet IP for the whole agent side."""
    import json as _json

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "ircd.json").write_text(_json.dumps({"server_name": "vm"}))
    saved = {}
    monkeypatch.setattr(setup_mod, "prompt_choice", lambda *a, **k: 1)
    monkeypatch.setattr(setup_mod, "save_env_value",
                        lambda k, v: saved.__setitem__(k, v))
    import observatory.provision as provision_mod
    monkeypatch.setattr(provision_mod, "read_irc_passwords",
                        lambda home: {"server": "b", "agent": "a"})
    setup_mod._offer_agent_bind(
        None, "vm", {"up": True, "ip": "100.9.9.9", "dns_name": None})
    cfg = _json.loads((obs / "ircd.json").read_text())
    assert cfg["agent_host"] == "100.9.9.9"
    assert saved["IRC_SERVER"] == "100.9.9.9"


def test_lounge_offer_skipped_when_answering(monkeypatch, capsys) -> None:
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(lounge_mod, "status_lounge",
                        lambda *a, **k: {"configured": False})
    monkeypatch.setattr(lounge_mod, "lounge_port_open",
                        lambda *a, **k: True)
    setup_mod._offer_lounge(None, None)
    assert "already answers" in capsys.readouterr().out


def test_setup_card_lounge_first(monkeypatch, capsys):
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": True, "users": ["owner"],
                         "host": "127.0.0.1", "port": 9000,
                         "unit": "active", "binary": "/b"})
    status = _base_status(provisioned=True)
    status["server_name"] = "ace"
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    out = capsys.readouterr().out
    assert out.index("http://127.0.0.1:9000") < out.index("server address")
    assert "pre-added" in out


def test_setup_card_live_server_name(monkeypatch, capsys):
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": False, "users": [],
                         "host": "", "port": 0, "external": False,
                         "unit": "inactive", "binary": ""})
    status = _base_status(provisioned=True)
    status["server_name"] = "ace"
    setup_mod._print_observatory_setup_card(
        status, dict(available=False, up=False, ip=None, dns_name=None)
    )
    out = capsys.readouterr().out
    assert "#ace_gateway" in out
    assert "#mercury_gateway" not in out


def test_lounge_offer_resumes_partial_install(monkeypatch, capsys) -> None:
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": True, "users": []})
    monkeypatch.setattr(lounge_mod, "_local_port_answers",
                        lambda *a, **k: False)
    asked = []
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no",
        lambda q, default=True: asked.append(q) or False)
    setup_mod._offer_lounge(None, None)
    assert asked, "partial install must re-offer (username/password below)"
    assert "Skipped" in capsys.readouterr().out


def test_lounge_offer_skipped_when_users_exist(monkeypatch, capsys) -> None:
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": True, "users": ["owner"]})
    asked = []
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no",
        lambda q, default=True: asked.append(q) or False)
    setup_mod._offer_lounge(None, None)
    assert not asked
    assert "keeping it" in capsys.readouterr().out


def test_password_reset_skipped_after_fresh_creation(monkeypatch) -> None:
    setup_mod._JUST_CREATED_LOUNGE_USER = "owner"
    asked = []
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no",
        lambda q, default=True: asked.append(q) or False)
    try:
        setup_mod._offer_lounge_password_reset(None)
    finally:
        setup_mod._JUST_CREATED_LOUNGE_USER = None
    assert not asked
    assert setup_mod._JUST_CREATED_LOUNGE_USER is None


def test_lounge_offer_seeds_live_server_bind(monkeypatch) -> None:
    """Pre-seeded uplink uses the live server_host, never localhost."""
    import observatory.lounge as lounge_mod
    from observatory import provision as provision_mod

    monkeypatch.setattr(
        lounge_mod, "status_lounge",
        lambda *a, **k: {"configured": False, "users": []})
    monkeypatch.setattr(lounge_mod, "_local_port_answers",
                        lambda *a, **k: False)
    monkeypatch.setattr(provision_mod, "_mercury_home", lambda home: "/h")
    monkeypatch.setattr(
        provision_mod, "read_config",
        lambda home: {"server_name": "vm", "server_host": "100.9.9.9",
                      "server_port": 6670})
    monkeypatch.setattr(
        provision_mod, "read_irc_passwords",
        lambda home: {"server": "pw", "agent": "pw2"})
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(setup_mod, "prompt_choice", lambda *a, **k: 0)
    monkeypatch.setattr(setup_mod, "prompt", lambda *a, **k: "owner")
    monkeypatch.setattr(setup_mod, "_ensure_firewall_port", lambda *a: None)
    seen = {}
    monkeypatch.setattr(
        lounge_mod, "provision_lounge",
        lambda **kw: seen.update(kw) or {"user": {"action": "created"}})
    setup_mod._offer_lounge(None, {"up": False, "ip": None})
    assert seen["uplink_host"] == "100.9.9.9"
    assert seen["uplink_port"] == 6670
    assert seen["uplink_channel"] == "#vm_gateway"


def test_converge_repoints_drifted_uplink(tmp_path, monkeypatch) -> None:
    import json as _json
    import observatory.lounge as lounge_mod
    from observatory import provision as provision_mod

    home = tmp_path / "mercury"
    monkeypatch.setattr(provision_mod, "_mercury_home", lambda h: home)
    monkeypatch.setattr(
        provision_mod, "read_config",
        lambda h: {"server_name": "vm", "server_host": "100.9.9.9",
                   "server_port": 6670})
    monkeypatch.setattr(
        provision_mod, "read_irc_passwords",
        lambda h: {"server": "pw", "agent": "pw2"})
    users = lounge_mod.LoungePaths(home).home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": [{
        "name": "vm", "host": "127.0.0.1", "port": 6670,
        "password": "pw", "nick": "owner", "username": "owner",
        "channels": [{"name": "#vm_gateway", "muted": False,
                      "key": ""}]}]}))
    monkeypatch.setattr(lounge_mod, "lounge_unit_active", lambda: True)
    restarted = []
    monkeypatch.setattr(lounge_mod, "restart_lounge",
                        lambda: restarted.append(True))
    setup_mod._converge_lounge_uplink()
    data = _json.loads((users / "owner.json").read_text())
    assert data["networks"][0]["host"] == "100.9.9.9"
    assert restarted == [True]


def test_rotate_reseeds_lounge_uplink(tmp_path, monkeypatch) -> None:
    from observatory import provision as provision_mod

    home = tmp_path / "mercury"
    (home / "observatory").mkdir(parents=True)
    monkeypatch.setattr(provision_mod, "_mercury_home", lambda h: home)
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
    monkeypatch.setattr(
        setup_mod, "_prompt_validated", lambda *a, **k: "custom-password-123")
    monkeypatch.setattr(
        setup_mod, "_restart_observatory_unit", lambda *a, **k: True)
    converged = []
    monkeypatch.setattr(
        setup_mod, "_converge_lounge_uplink",
        lambda: converged.append(True))
    import types as _types
    setup_mod._offer_server_password_rotate(
        _types.SimpleNamespace(
            validate_server_password=lambda p: p))
    assert converged == [True]
    assert "custom-password-123" in (home / ".env").read_text()


def test_converge_failure_is_loud(monkeypatch, capsys) -> None:
    from observatory import provision as provision_mod

    def _boom(home=None):
        raise RuntimeError("no home")

    monkeypatch.setattr(provision_mod, "_mercury_home", _boom)
    setup_mod._converge_lounge_uplink()
    assert "converge failed" in capsys.readouterr().out


def test_converge_applies_config_template_drift(tmp_path, monkeypatch) -> None:
    import json as _json
    import observatory.lounge as lounge_mod
    from observatory import provision as provision_mod

    home = tmp_path / "mercury"
    monkeypatch.setattr(provision_mod, "_mercury_home", lambda h: home)
    monkeypatch.setattr(
        provision_mod, "read_config",
        lambda h: {"server_name": "vm", "server_host": "127.0.0.1",
                   "server_port": 6670})
    monkeypatch.setattr(
        provision_mod, "read_irc_passwords",
        lambda h: {"server": "pw", "agent": "pw2"})
    paths = lounge_mod.LoungePaths(home)
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text(_json.dumps({"networks": [{
        "name": "vm", "host": "127.0.0.1", "port": 6670,
        "password": "pw", "nick": "owner", "username": "owner",
        "channels": [{"name": "#vm_gateway", "muted": False,
                      "key": ""}]}]}))
    # stale template: config without fileUpload
    paths.dir.mkdir(parents=True, exist_ok=True)
    (paths.dir / "config.js").write_text(
        'module.exports = {\n\thost: "127.0.0.1",\n\tport: 9000,\n};\n')
    monkeypatch.setattr(lounge_mod, "lounge_unit_active", lambda: True)
    restarted = []
    monkeypatch.setattr(lounge_mod, "restart_lounge",
                        lambda: restarted.append(True))
    setup_mod._converge_lounge_uplink()
    assert "fileUpload" in (paths.dir / "config.js").read_text()
    assert restarted == [True]
