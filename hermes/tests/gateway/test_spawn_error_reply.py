"""_spawn_error_reply: one line, actionable, never a dump."""

from __future__ import annotations

from gateway.slash_commands import _spawn_error_reply as reply


def test_natives_failure_gives_remediation() -> None:
    err = RuntimeError(
        "Cannot find module '/x/pi_natives.linux-x64.node'\n"
        "Require stack:\n- /x\ncurl stuff\nmore lines")
    out = reply("spawnomp", err)
    assert out.count("\n") == 0
    assert "rm -rf ~/.omp/natives" in out


def test_generic_failure_first_line_capped() -> None:
    err = ValueError("short problem\nsecond line\nthird")
    out = reply("spawn", err)
    assert out == "✗ /spawn failed: short problem (full error in the gateway log)"
    assert reply("spawn", RuntimeError("x" * 500)).count("\n") == 0
    assert len(reply("spawn", RuntimeError("x" * 500))) < 400
