"""omp children stay under $MERCURY_HOME/omp (never ~/.omp)."""

from __future__ import annotations

import os

from tools.omp_delegation import ensure_omp_home_env


def test_helper_pins_both_dirs(monkeypatch) -> None:
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("OMP_WORKTREE_DIR", raising=False)
    env: dict = {}
    out = ensure_omp_home_env(env, "/h/mercury")
    assert out == {
        "PI_CODING_AGENT_DIR": "/h/mercury/omp",
        "OMP_WORKTREE_DIR": "/h/mercury/omp/wt",
    }
    assert env is out


def test_helper_never_overrides_explicit() -> None:
    env = {"PI_CODING_AGENT_DIR": "/custom", "OMP_WORKTREE_DIR": "/custom/wt"}
    assert ensure_omp_home_env(env, "/h/mercury") is env
    assert env["PI_CODING_AGENT_DIR"] == "/custom"


def test_helper_falls_back_to_home(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MERCURY_HOME", raising=False)
    monkeypatch.setattr(os, "environ", {}, raising=False)
    import pathlib

    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    out = ensure_omp_home_env({})
    assert out["PI_CODING_AGENT_DIR"] == str(tmp_path / ".mercury" / "omp")


def test_child_pins_env_without_mutating_caller() -> None:
    from tools.omp_rpc_transport import OmpRpcChild

    caller = {"MERCURY_HOME": "/h/mercury", "FOO": "bar"}
    child = OmpRpcChild(omp_path="/bin/false", model="m", env=caller)
    assert child._env["PI_CODING_AGENT_DIR"] == "/h/mercury/omp"
    assert child._env["OMP_WORKTREE_DIR"] == "/h/mercury/omp/wt"
    assert child._env["FOO"] == "bar"
    assert "PI_CODING_AGENT_DIR" not in caller


def test_child_honors_explicit_home() -> None:
    from tools.omp_rpc_transport import OmpRpcChild

    caller = {"PI_CODING_AGENT_DIR": "/custom"}
    child = OmpRpcChild(omp_path="/bin/false", model="m", env=caller)
    assert child._env["PI_CODING_AGENT_DIR"] == "/custom"
