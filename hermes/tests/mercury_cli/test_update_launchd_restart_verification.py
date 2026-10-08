"""Passive launchd supervision proofs and platform-specific recovery guidance."""

from __future__ import annotations


import pytest

import mercury_cli.gateway as gateway_cli
from mercury_cli.update_cmd import _warn_incomplete_gateway_fleet_restart

LABEL = "ai.mercury.gateway"


class _FakeClock:
    """Monotonic clock that only advances when the code under test sleeps.

    Keeps the poll loop's wall-clock budget honest without spending it: a real
    20s verification timeout would otherwise make this file the slowest in the
    suite, and shortening the timeout would stop testing the throttle window.
    """

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(gateway_cli.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(gateway_cli.time, "sleep", fake.sleep)
    return fake


@pytest.fixture(autouse=True)
def _no_detached_fallback(monkeypatch):
    """Default every test to "launchd can manage this domain"."""
    monkeypatch.setattr(
        gateway_cli, "_launchd_unsupported_marker_exists", lambda: False
    )


def _supervision_returning(*results):
    """Fake ``_launchctl_label_supervising_process`` yielding ``results`` in order.

    The final value repeats, so a test can say "False twenty times, then True
    from then on".
    """
    seq = list(results)
    calls = []

    def probe(label):
        calls.append(label)
        return seq[min(len(calls) - 1, len(seq) - 1)]

    probe.calls = calls
    return probe


class TestWaitForLaunchdGatewaySupervision:
    def test_returns_true_when_already_supervised(self, monkeypatch, clock):
        """The common case must not cost a single sleep."""
        monkeypatch.setattr(
            gateway_cli,
            "_launchctl_label_supervising_process",
            _supervision_returning(True),
        )

        assert gateway_cli.wait_for_launchd_gateway_supervision(label=LABEL) is True
        assert clock.slept == []

    def test_waits_out_the_launchd_respawn_throttle(self, monkeypatch, clock):
        """A pid that only appears after ~10s is a SUCCESS, not a failure.

        launchd will not relaunch a KeepAlive job more than about once per 10
        seconds, so a gateway that exits promptly leaves the label registered
        with no pid for most of that window.  A one-shot check - or any budget
        shorter than the throttle - would report a perfectly healthy restart as
        a silent failure, which is a worse bug than the one being fixed.
        """
        # 0.5s poll interval: 20 misses is ~10s of throttle, then the pid lands.
        probe = _supervision_returning(*([False] * 20 + [True]))
        monkeypatch.setattr(
            gateway_cli, "_launchctl_label_supervising_process", probe
        )

        assert gateway_cli.wait_for_launchd_gateway_supervision(label=LABEL) is True
        assert sum(clock.slept) == pytest.approx(10.0)

    def test_gives_up_at_the_deadline(self, monkeypatch, clock):
        """A job that never comes back must fail, and must fail bounded."""
        probe = _supervision_returning(False)
        monkeypatch.setattr(
            gateway_cli, "_launchctl_label_supervising_process", probe
        )

        assert (
            gateway_cli.wait_for_launchd_gateway_supervision(
                label=LABEL, timeout=20.0
            )
            is False
        )
        assert sum(clock.slept) <= 20.0
        # The deadline is enforced by wall clock, not by a probe count.
        assert len(probe.calls) == 41

    def test_detached_fallback_is_not_a_failure(self, monkeypatch, clock):
        """On a host where launchd cannot manage the domain, no pid is correct.

        ``_launchd_fallback_to_detached`` is a legitimate outcome (macOS 26+
        unmanageable domains); the gateway runs unsupervised by design there.
        Reporting that as an incomplete update would fail every update on those
        hosts.
        """
        monkeypatch.setattr(
            gateway_cli, "_launchd_unsupported_marker_exists", lambda: True
        )
        probe = _supervision_returning(False)
        monkeypatch.setattr(
            gateway_cli, "_launchctl_label_supervising_process", probe
        )

        assert gateway_cli.wait_for_launchd_gateway_supervision(label=LABEL) is True
        assert probe.calls == []








class TestIncompleteFleetWarningIsPlatformCorrect:
    def test_macos_recovery_instructions_are_launchctl(self, monkeypatch, capsys):
        """A launchd label must not be handed systemctl commands."""
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)

        _warn_incomplete_gateway_fleet_restart([LABEL])

        out = capsys.readouterr().out
        assert "launchctl bootstrap" in out
        assert "systemctl" not in out

    def test_linux_recovery_instructions_are_unchanged(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        _warn_incomplete_gateway_fleet_restart(["mercury-gateway.service"])

        out = capsys.readouterr().out
        assert "systemctl --user restart <unit>" in out
        assert "launchctl" not in out
