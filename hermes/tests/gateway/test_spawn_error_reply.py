"""_spawn_error_reply: one line, actionable, never a dump."""

from __future__ import annotations

from gateway.slash_commands import _spawn_error_reply as reply


def test_natives_failure_gives_remediation() -> None:
    err = RuntimeError(
        "Cannot find module '/x/pi_natives.linux-x64.node'\n"
        "Require stack:\n- /x\ncurl stuff\nmore lines")
    out = reply("spawnomp", err)
    assert out.count("\n") == 0
    assert "rm -rf $MERCURY_HOME/.local/share/omp/natives" in out
    assert "~/.omp" not in out


def test_generic_failure_first_line_capped() -> None:
    err = ValueError("short problem\nsecond line\nthird")
    out = reply("spawn", err)
    assert out == "✗ /spawn failed: short problem (full error in the gateway log)"
    assert reply("spawn", RuntimeError("x" * 500)).count("\n") == 0
    assert len(reply("spawn", RuntimeError("x" * 500))) < 400


class _StubAdapter:
    def __init__(self, channel="", extra=()):
        self.channel = channel
        self.extra_channels = set(extra)


class _StubSelf:
    def __init__(self, adapter=None):
        from gateway.config import Platform
        self.adapters = {Platform("irc"): adapter} if adapter is not None else {}


def test_live_channels_reports_joined_rooms() -> None:
    from gateway.slash_commands import GatewaySlashCommandsMixin as mixin

    got = mixin._observatory_live_channels(
        _StubSelf(_StubAdapter("#vm_gateway", ("#vm_ace",))))
    assert got == {"#vm_gateway", "#vm_ace"}


def test_live_channels_empty_without_adapter() -> None:
    from gateway.slash_commands import GatewaySlashCommandsMixin as mixin

    assert mixin._observatory_live_channels(_StubSelf()) == set()
