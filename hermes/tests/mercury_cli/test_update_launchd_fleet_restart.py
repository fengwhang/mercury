"""Launchd fleet discovery, profile isolation, and passive replacement proofs."""

from __future__ import annotations

import subprocess
import sys

import pytest

import mercury_cli.gateway as gw
import mercury_cli.profiles
from mercury_cli.gateway import (
    _locate_launchd_gateway_service,
    _parse_launchd_pid_from_print_output,
    _probe_launchd_domain_for_label,
    launchd_gateway_labels_for_install,
)
from mercury_cli.update_cmd import (
    _warn_incomplete_gateway_fleet_restart,
)


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="launchd fleet restart is macOS-only; helpers use POSIX os.getuid",
)

UID = 501

PRINT_RUNNING = (
    "system/com.example = {\n"
    "\tactive count = 1\n"
    "\tstate = running\n"
    "\tpid = 4242\n"
    "\tprogram = /usr/bin/true\n"
    "}\n"
)
PRINT_LOADED_NOT_RUNNING = (
    "system/com.example = {\n"
    "\tactive count = 0\n"
    "\tstate = not running\n"
    "\tprogram = /usr/bin/true\n"
    "}\n"
)


@pytest.fixture(autouse=True)
def _fixed_uid(monkeypatch):
    monkeypatch.setattr(gw.os, "getuid", lambda: UID)


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class _Profile:
    def __init__(self, name, is_default=False):
        self.name = name
        self.is_default = is_default


class TestLaunchdGatewayLabelsForInstall:
    def test_labels_derive_from_this_installs_profiles(self, monkeypatch):
        """The fleet is THIS install's profiles, root first — never a glob of
        the shared per-user LaunchAgents dir. A sandboxed HERMES_HOME (tests,
        side-by-side installs) must not enumerate — and restart — another
        install's services, and the hermetic test suite must not see the dev
        machine's real fleet."""
        monkeypatch.setattr(
            mercury_cli.profiles,
            "list_profiles",
            lambda: [
                _Profile("tfl-wiki"),
                _Profile("default", is_default=True),
                _Profile("merit-ops"),
                _Profile("Bad Name!"),  # cannot map to a service suffix — skipped
            ],
        )
        assert launchd_gateway_labels_for_install() == [
            "ai.mercury.gateway",
            "ai.mercury.gateway-merit-ops",
            "ai.mercury.gateway-tfl-wiki",
        ]

    def test_no_profiles_means_no_fleet(self, monkeypatch):
        monkeypatch.setattr(mercury_cli.profiles, "list_profiles", lambda: [])
        assert launchd_gateway_labels_for_install() == []


class TestParseLaunchdPidFromPrintOutput:
    def test_running_service_pid(self):
        assert _parse_launchd_pid_from_print_output(PRINT_RUNNING) == 4242

    def test_loaded_but_not_running_has_no_pid(self):
        assert _parse_launchd_pid_from_print_output(PRINT_LOADED_NOT_RUNNING) is None


class TestLocateLaunchdGatewayService:
    def test_domains_resolve_per_label_not_from_cache(self, monkeypatch):
        """The #41403 review defect: sibling domains are independent."""
        gui_loaded = {"ai.mercury.gateway-a"}

        def fake_run(cmd, **kwargs):
            assert cmd[:2] == ["launchctl", "print"]
            domain, _, label = cmd[2].rpartition("/")
            in_gui = domain == f"gui/{UID}" and label in gui_loaded
            in_user = domain == f"user/{UID}" and label not in gui_loaded
            if in_gui or in_user:
                return _completed(0, PRINT_RUNNING)
            return _completed(113)

        monkeypatch.setattr(gw.subprocess, "run", fake_run)
        # Simulate a prior current-profile resolution having populated the
        # process-wide cache — per-label lookups must not consult it.
        monkeypatch.setattr(gw, "_resolved_launchd_domain", f"gui/{UID}")

        assert _locate_launchd_gateway_service("ai.mercury.gateway-a") == (
            f"gui/{UID}",
            4242,
        )
        assert _locate_launchd_gateway_service("ai.mercury.gateway-b") == (
            f"user/{UID}",
            4242,
        )

    def test_loaded_without_live_process(self, monkeypatch):
        monkeypatch.setattr(
            gw.subprocess,
            "run",
            lambda *a, **k: _completed(0, PRINT_LOADED_NOT_RUNNING),
        )
        assert _locate_launchd_gateway_service("ai.mercury.gateway-x") == (
            f"gui/{UID}",
            None,
        )

    def test_not_loaded_in_either_domain(self, monkeypatch):
        monkeypatch.setattr(gw.subprocess, "run", lambda *a, **k: _completed(113))
        assert _locate_launchd_gateway_service("ai.mercury.gateway-x") == (None, None)

    def test_timeout_propagates_to_caller(self, monkeypatch):
        """A wedged launchctl must surface as a failure, not read as
        'unloaded' — the update path owns per-label failure accounting."""

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

        monkeypatch.setattr(gw.subprocess, "run", fake_run)
        with pytest.raises(subprocess.TimeoutExpired):
            _locate_launchd_gateway_service("ai.mercury.gateway-x")


class TestProbeLaunchdDomainForLabel:
    def test_unloaded_label_falls_back_to_managername(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["launchctl", "print"]:
                raise subprocess.CalledProcessError(113, cmd)
            if cmd == ["launchctl", "managername"]:
                return _completed(0, "Aqua\n")
            raise AssertionError(f"unexpected command {cmd}")

        monkeypatch.setattr(gw.subprocess, "run", fake_run)
        assert _probe_launchd_domain_for_label("ai.mercury.gateway-x") == f"gui/{UID}"

    def test_unloaded_label_defaults_to_user_domain(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["launchctl", "print"]:
                raise subprocess.CalledProcessError(113, cmd)
            if cmd == ["launchctl", "managername"]:
                return _completed(0, "Background\n")
            raise AssertionError(f"unexpected command {cmd}")

        monkeypatch.setattr(gw.subprocess, "run", fake_run)
        assert _probe_launchd_domain_for_label("ai.mercury.gateway-x") == f"user/{UID}"


class TestGetServicePidsScoping:
    def _wire(self, monkeypatch):
        monkeypatch.setattr(gw, "is_macos", lambda: True)
        monkeypatch.setattr(gw, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gw, "get_launchd_label", lambda: "ai.mercury.gateway")
        monkeypatch.setattr(
            gw,
            "launchd_gateway_labels_for_install",
            lambda: ["ai.mercury.gateway", "ai.mercury.gateway-a", "ai.mercury.gateway-b"],
        )
        located = {
            "ai.mercury.gateway": (f"gui/{UID}", 100),
            "ai.mercury.gateway-a": (f"gui/{UID}", 200),
            "ai.mercury.gateway-b": (None, None),  # not bootstrapped
        }
        monkeypatch.setattr(
            gw, "_locate_launchd_gateway_service", lambda label: located[label]
        )

    def test_all_profiles_returns_every_gateway_service_pid(self, monkeypatch):
        """The update sweep's exclude-set must protect ALL freshly-restarted
        services, not only the invoking profile's (else the sweep SIGTERMs
        gateways launchd just respawned)."""
        self._wire(monkeypatch)
        assert gw._get_service_pids(all_profiles=True) == {100, 200}

    def test_default_stays_scoped_to_current_profile(self, monkeypatch):
        """Regression guard: default-scope callers (gateway status, cron,
        stop_profile_gateway's orphan reaper) must NOT start seeing sibling
        service PIDs — the reaper SIGTERM/SIGKILLs what they feed it."""
        self._wire(monkeypatch)
        assert gw._get_service_pids() == {100}

    def test_find_gateway_pids_passes_profile_scope_through(self, monkeypatch):
        calls: list[bool] = []
        monkeypatch.setattr(
            gw,
            "_get_service_pids",
            lambda all_profiles=False: (calls.append(all_profiles), set())[1],
        )
        monkeypatch.setattr(gw, "_scan_gateway_pids", lambda *a, **k: [])
        monkeypatch.setattr(gw, "supports_systemd_services", lambda: True)

        gw.find_gateway_pids(all_profiles=False)
        gw.find_gateway_pids(all_profiles=True)
        assert calls == [False, True]






class TestWaitForLaunchdServicePid:
    def test_returns_true_once_pid_changes(self, monkeypatch):
        pids = iter([200, 200, 4242])
        monkeypatch.setattr(
            gw,
            "_launchd_print_service_pid",
            lambda domain, label: (True, next(pids)),
        )
        monkeypatch.setattr(gw.time, "sleep", lambda _s: None)
        assert gw._wait_for_launchd_service_pid(
            "ai.mercury.gateway-x", old_pid=200, timeout=5.0, domain=f"gui/{UID}"
        )

    def test_returns_false_when_pid_never_changes(self, monkeypatch):
        clock = iter(float(i) for i in range(100))
        monkeypatch.setattr(gw.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(gw.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            gw,
            "_launchd_print_service_pid",
            lambda domain, label: (True, 200),
        )
        assert not gw._wait_for_launchd_service_pid(
            "ai.mercury.gateway-x", old_pid=200, timeout=3.0, domain=f"gui/{UID}"
        )


class TestIncompleteWarningMentionsLaunchctl:
    def test_launchd_labels_get_launchctl_hint(self, capsys):
        _warn_incomplete_gateway_fleet_restart(["ai.mercury.gateway-merit-ops"])
        out = capsys.readouterr().out
        assert "Update incomplete" in out
        assert "launchctl kickstart -k" in out

    def test_systemd_units_keep_systemctl_hint(self, capsys):
        _warn_incomplete_gateway_fleet_restart(["mercury-gateway-coder"])
        out = capsys.readouterr().out
        assert "systemctl" in out
        assert "launchctl" not in out
