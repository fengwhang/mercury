"""MERCURY-OMP PATCH (B1): tests for tools/omp_delegation.py.

Covers the delegate_task → omp rewrite: task collection, verbatim prompt
passing, entry contract, fail-hard paths, batch-scoped interrupts, and the
registry fallback wiring. Runs on stdlib unittest only (no pytest in this
container). Execute with the venv interpreter (deps: yaml):

  PYTHONPATH=<repo>/mercury /opt/mercury/.venv/bin/python -m unittest \
      tests.tools.test_omp_delegation -v
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

import pytest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[2]

# Fixture four-slot config (valid: distinct pairs, all slots set).
FIXTURE_CONFIG = """\
# fixture — mercury-omp B1 tests
models:
  default: "prov/main-model"
  fallback: "prov/main-fallback"
  delegate_default: "prov/delegate-model"
  delegate_fallback: "prov/delegate-fallback"
"""

# Fixture bridge output for --delegate.
BRIDGE_DELEGATE_OUT = "OMP_MODEL=prov/delegate-model\nOMP_FALLBACK_CHAIN=prov/delegate-fallback\n"


def _write_fake_omp(directory: Path, body: str) -> str:
    """Create an executable fake omp; returns its path."""
    p = directory / "omp"
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def _make_parent(depth: int = 0) -> MagicMock:
    parent = MagicMock()
    parent._delegate_depth = depth
    parent.session_id = "sess-b1-test"
    parent.base_url = "https://example.test/v1"
    parent.api_key = "***"
    parent.provider = "prov"
    parent.model = "prov/main-model"
    parent.platform = "cli"
    parent.cwd = str(REPO)
    return parent


class _EnvFixture:
    """Point the module at fixture bridge/config + fake omp binary."""

    def __init__(self, tmpdir: Path, bridge_exit=0, bridge_out=BRIDGE_DELEGATE_OUT,
                 bridge_err=""):
        self.tmpdir = tmpdir
        self.bridge = tmpdir / "bridge.py"
        self.bridge.write_text(
            "#!/usr/bin/env python3\n"
            f"import sys; sys.stdout.write({bridge_out!r});"
            f"sys.stderr.write({bridge_err!r}); sys.exit({bridge_exit})\n"
        )
        self.bridge.chmod(0o755)
        self.cfg = tmpdir / "config.yaml"
        self.cfg.write_text(FIXTURE_CONFIG)
        self.old = {}

    def __enter__(self):
        for k, v in (
            ("HERMES_OMP_BRIDGE", str(self.bridge)),
            ("HERMES_OMP_CONFIG", str(self.cfg)),
        ):
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        # reset the module's process-lifetime caches between tests
        import tools.omp_delegation as mod
        mod._env_cache.update(mtime=None, env=None, err=None)
        mod._rendered.clear()
        return self

    def __exit__(self, *exc):
        import tools.omp_delegation as mod
        mod._env_cache.update(mtime=None, env=None, err=None)
        mod._rendered.clear()
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class TestTaskCollectionAndValidation(unittest.TestCase):
    """Goal collection from tasks[]/legacy goal, and fail-hard gates."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.fx = _EnvFixture(self.tmpdir)
        self.fx.__enter__()
        self.addCleanup(self.fx.__exit__)
        self.addCleanup(self._tmp.cleanup)
        # fail the spawn early if collection passes unexpectedly
        self.fake_omp = _write_fake_omp(
            self.tmpdir, "#!/bin/sh\necho 'fake-omp-ran'\n")
        self._old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = self.fake_omp
        self.addCleanup(lambda: self._restore_bin())

    def _restore_bin(self):
        if self._old_bin is None:
            os.environ.pop("HERMES_OMP_BIN", None)
        else:
            os.environ["HERMES_OMP_BIN"] = self._old_bin

    def _dispatch(self, args, parent=None):
        import tools.omp_delegation as mod
        with mock.patch.object(mod, "_render_omp_config_once"):
            return mod.dispatch_omp_delegation(
                parent or _make_parent(), args)

    def test_no_task_text_is_tool_error(self):
        out = self._dispatch({})
        payload = json.loads(out)
        self.assertIn("error", payload)
        self.assertIn("task text", payload["error"].lower())

    def test_legacy_top_level_goal_accepted(self):
        # depth>0 → synchronous path → runs the fake omp, no registry needed
        out = self._dispatch({"goal": "hello legacy"},
                             parent=_make_parent(depth=1))
        payload = json.loads(out)
        self.assertEqual(payload["engine"], "omp")
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["summary"], "fake-omp-ran")

    def test_tasks_list_goals_collected(self):
        out = self._dispatch(
            {"tasks": [{"goal": "alpha"}, {"goal": "beta"}]},
            parent=_make_parent(depth=1))
        payload = json.loads(out)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual([r["task_index"] for r in payload["results"]], [0, 1])

    def test_goalless_task_entries_skipped(self):
        out = self._dispatch(
            {"tasks": [{"goal": "alpha"}, {"context": "orphan"}, {"goal": "beta"}]},
            parent=_make_parent(depth=1))
        payload = json.loads(out)
        self.assertEqual(len(payload["results"]), 2)

    def test_bridge_refusal_aborts(self):
        fx2 = _EnvFixture(self.tmpdir, bridge_exit=1, bridge_err="FATAL: nope\n")
        with fx2:
            out = self._dispatch({"goal": "x"}, parent=_make_parent(depth=1))
        self.assertIn("bridge refused", out)
        self.assertIn("FATAL: nope", out)

    def test_missing_binary_aborts_before_spawn(self):
        os.environ["HERMES_OMP_BIN"] = "/nonexistent/omp-b1-test"
        out = self._dispatch({"goal": "x"}, parent=_make_parent(depth=1))
        self.assertIn("omp binary not found", out)

    def test_control_actions_over_live_registry(self):
        # M0A: control actions now answer over the live-child registry —
        # empty list is honest, unknown steer/stop targets are errors
        # (no more blanket 'not supported').
        import tools.omp_delegation as mod
        # isolate from any leaked registry state
        with mock.patch.object(mod, "_live_children", {}):
            out = self._dispatch({"action": "list"})
            payload = json.loads(out)
            self.assertEqual(payload["engine"], "omp")
            self.assertEqual(payload["count"], 0)
            self.assertIn("subagents", payload)
            out = self._dispatch({"action": "steer", "subagent_id": "x",
                                  "message": "y"})
            self.assertIn("No live omp child 'x'", out)
            out = self._dispatch({"action": "stop", "subagent_id": "x"})
            self.assertIn("No live omp child 'x'", out)

    def test_acp_transport_args_stripped_before_prompt(self):
        # hidden ACP fields must never reach the omp prompt (strip invariant
        # moved inside the omp engine with B1)
        echo_omp = _write_fake_omp(
            self.tmpdir, "#!/bin/sh\nprintf '%s' \"$4\"\n")
        os.environ["HERMES_OMP_BIN"] = echo_omp
        out = self._dispatch(
            {"tasks": [{"goal": "visible-goal",
                        "acp_command": "codex",
                        "acp_args": ["--acp"]}]},
            parent=_make_parent(depth=1))
        payload = json.loads(out)
        summary = payload["results"][0]["summary"]
        self.assertIn("visible-goal", summary)
        self.assertNotIn("acp_command", summary)
        self.assertNotIn("--acp", summary)


class TestPromptVerbatim(unittest.TestCase):
    """The prompt must reach omp as ONE argv element, verbatim."""

    def test_prompt_single_argv_element(self):
        import tools.omp_delegation as mod
        captured = {}

        def fake_run(task_index, prompt, model, workdir, timeout,
                     fallback_chain, batch_procs=None,
                     profile_home=None, extra_env=None,
                     delegation_id=None, name=None, goal=None,
                     owner_session_id="", base_env=None,
                     isolate_worktree=None):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["fallback"] = fallback_chain
            captured["name"] = name
            captured["isolate_worktree"] = isolate_worktree
            return {"task_index": task_index, "status": "completed",
                    "name": name, "summary": "s", "exit_reason": "completed"}

        with mock.patch.object(mod, "_run_omp_task", side_effect=fake_run):
            res = mod._sync_run(
                [{"prompt": "line1\nline2; rm -rf / --faked"}],
                {"OMP_MODEL": "m", "OMP_FALLBACK_CHAIN": "f"},
                None, 60, 1)
        self.assertEqual(captured["prompt"], "line1\nline2; rm -rf / --faked")
        self.assertEqual(captured["model"], "m")
        self.assertEqual(captured["fallback"], "f")
        # tasks without a name pass None through (fallback is the runner's job)
        self.assertIsNone(captured["name"])
        self.assertEqual(res["engine"], "omp")
        self.assertEqual(res["results"][0]["summary"], "s")

    def test_output_schema_appended(self):
        import tools.omp_delegation as mod
        prompt = mod._build_task_prompt(
            "goal text", "ctx text",
            {"type": "object", "properties": {"answer": {"type": "string"}}})
        self.assertIn("goal text", prompt)
        self.assertIn("ctx text", prompt)
        self.assertIn("JSON Schema", prompt)
        self.assertIn('"answer"', prompt)


class TestEntryContract(unittest.TestCase):
    """Result entries keep the old engine's contract keys."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.bin = _write_fake_omp(self.tmpdir, "#!/bin/sh\necho out; echo err >&2; exit 3\n")
        self._old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = self.bin
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_bin is None:
            os.environ.pop("HERMES_OMP_BIN", None)
        else:
            os.environ["HERMES_OMP_BIN"] = self._old_bin

    def test_failed_child_carries_stdout_and_stderr(self):
        import tools.omp_delegation as mod
        entry = mod._run_omp_task(0, "p", "m", None, 60, None)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["exit_reason"], "error")
        self.assertFalse(entry["truncated"])
        self.assertIn("out", entry["error"])
        self.assertIn("err", entry["error"])
        self.assertEqual(entry["task_index"], 0)
        self.assertEqual(entry["model"], "m")

    @pytest.mark.live_system_guard_bypass
    def test_timeout_kills_group(self):
        import tools.omp_delegation as mod
        bin2 = _write_fake_omp(
            self.tmpdir, "#!/bin/sh\nsleep 300 & wait\n")
        os.environ["HERMES_OMP_BIN"] = bin2
        entry = mod._run_omp_task(0, "p", "m", None, 2, None)
        self.assertEqual(entry["exit_reason"], "timeout")
        self.assertIn("timed out", entry["error"])


class TestBackgroundDispatch(unittest.TestCase):
    """Top-level delegation goes through the async registry seam."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.fx = _EnvFixture(self.tmpdir)
        self.fx.__enter__()
        self.addCleanup(self.fx.__exit__)
        self.addCleanup(self._tmp.cleanup)
        self.bin = _write_fake_omp(self.tmpdir, "#!/bin/sh\necho bg-ok\n")
        self._old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = self.bin
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_bin is None:
            os.environ.pop("HERMES_OMP_BIN", None)
        else:
            os.environ["HERMES_OMP_BIN"] = self._old_bin

    def _dispatch(self, args, parent=None):
        import tools.omp_delegation as mod
        with mock.patch.object(mod, "_render_omp_config_once"):
            return mod.dispatch_omp_delegation(
                parent or _make_parent(depth=0), args)

    def test_dispatched_via_async_registry(self):
        import tools.omp_delegation as mod
        captured = {}

        def fake_dispatch(**kwargs):
            captured.update(kwargs)
            return {"status": "dispatched", "delegation_id": "d-b1"}

        with mock.patch("tools.async_delegation.dispatch_async_delegation_batch",
                        side_effect=fake_dispatch), \
             mock.patch("tools.approval.get_current_session_key",
                        return_value="agent:main:test"), \
             mock.patch("gateway.session_context.async_delivery_supported",
                        return_value=True), \
             mock.patch("gateway.session_context.get_session_env",
                        return_value=""):
            out = self._dispatch({"tasks": [{"goal": "g1"}]})
        payload = json.loads(out)
        self.assertEqual(payload["status"], "dispatched")
        self.assertEqual(payload["engine"], "omp")
        self.assertEqual(payload["delegation_id"], "d-b1")
        self.assertEqual(captured["goals"], ["g1"])
        self.assertEqual(captured["model"], "prov/delegate-model")
        self.assertEqual(captured["session_key"], "agent:main:test")
        # runner must be runnable and produce the omp result payload
        runner_result = captured["runner"]()
        self.assertEqual(runner_result["engine"], "omp")
        self.assertEqual(runner_result["results"][0]["summary"], "bg-ok")
        # interrupt closure must be per-batch (callable), not the global kill
        self.assertTrue(callable(captured["interrupt_fn"]))
        captured["interrupt_fn"]()  # must not raise (no live procs)

    def test_finite_session_falls_back_to_sync(self):
        import tools.omp_delegation as mod
        with mock.patch("gateway.session_context.async_delivery_supported",
                        return_value=False), \
             mock.patch("tools.async_delegation._current_origin_session_id",
                        return_value=""):
            out = self._dispatch({"tasks": [{"goal": "g1"}]})
        payload = json.loads(out)
        self.assertEqual(payload["engine"], "omp")
        self.assertEqual(payload["results"][0]["summary"], "bg-ok")
        self.assertIn("SYNCHRONOUSLY", payload["note"])

    def test_registry_rejection_is_tool_error(self):
        import tools.omp_delegation as mod
        with mock.patch("tools.async_delegation.dispatch_async_delegation_batch",
                        return_value={"status": "rejected", "error": "capacity"}), \
             mock.patch("tools.approval.get_current_session_key",
                        return_value="agent:main:test"), \
             mock.patch("gateway.session_context.async_delivery_supported",
                        return_value=True), \
             mock.patch("gateway.session_context.get_session_env",
                        return_value=""):
            out = self._dispatch({"tasks": [{"goal": "g1"}]})
        self.assertIn("capacity", out)


class TestRegistryFallback(unittest.TestCase):
    """delegate_tool's registry handler routes to the omp engine."""

    def test_handler_routes_to_omp_dispatch(self):
        from tools import delegate_tool
        captured = {}

        def fake_dispatch(parent, args):
            captured["parent"] = parent
            captured["args"] = args
            return '{"status": "dispatched", "engine": "omp"}'

        with mock.patch("tools.omp_delegation.dispatch_omp_delegation",
                        side_effect=fake_dispatch):
            handler = delegate_tool.registry._tools["delegate_task"]["delegation"]["delegate_task"]["handler"] \
                if False else None
        # registry internals differ by version; call the fallback fn directly
        with mock.patch("tools.omp_delegation.dispatch_omp_delegation",
                        side_effect=fake_dispatch):
            out = delegate_tool._omp_registry_fallback(
                {"goal": "rg"}, {"parent_agent": "PA"})
        self.assertEqual(captured["parent"], "PA")
        self.assertEqual(captured["args"], {"goal": "rg"})
        self.assertIn("omp", out)

    def test_handler_failure_is_visible_not_silent(self):
        from tools import delegate_tool
        with mock.patch("tools.omp_delegation.dispatch_omp_delegation",
                        side_effect=RuntimeError("boom")):
            out = delegate_tool._omp_registry_fallback(
                {"goal": "rg"}, {"parent_agent": None})
        payload = json.loads(out)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("boom", payload["error"])


class TestParallelFanout(unittest.TestCase):
    """N tasks actually run in parallel (bounded by max_workers)."""

    def test_parallel_execution_and_order(self):
        import tools.omp_delegation as mod
        bin_path = _write_fake_omp(
            Path(tempfile.mkdtemp()), "#!/bin/sh\necho \"task-$1\"\nsleep 0.3\n")
        # fake omp echoes argv; prompt is task text
        bin_path = _write_fake_omp(
            Path(bin_path).parent,
            "#!/bin/sh\nsleep 0.3\necho done\n")
        old_bin = os.environ.get("HERMES_OMP_BIN")
        # Force the -p one-shot engine: this test measures ONE-SHOT
        # parallelism; a non-RPC binary would add a per-task RPC
        # probe+failover (~0.7s each) to the wall clock without testing
        # anything new (failover is covered in TestRpcTransportSelection).
        old_transport = os.environ.get("HERMES_OMP_TRANSPORT")
        os.environ["HERMES_OMP_BIN"] = bin_path
        os.environ["HERMES_OMP_TRANSPORT"] = "oneshot"
        try:
            tasks = [{"prompt": f"t{i}"} for i in range(4)]
            t0 = time.monotonic()
            res = mod._sync_run(tasks, {"OMP_MODEL": "m"}, None, 60, 4)
            dt = time.monotonic() - t0
        finally:
            for key, val in (("HERMES_OMP_BIN", old_bin),
                             ("HERMES_OMP_TRANSPORT", old_transport)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        self.assertEqual(len(res["results"]), 4)
        self.assertEqual([r["task_index"] for r in res["results"]], [0, 1, 2, 3])
        # 4 tasks × 0.3s at width 4 ≈ 0.3s total; serial would be ≥ 1.2s
        self.assertLess(dt, 1.0, f"fan-out did not run in parallel (took {dt:.2f}s)")


class TestRpcTransportSelection(unittest.TestCase):
    """C1 slice 2: _run_omp_task prefers RPC; -p is fallback, not default.

    Fake omp binaries that speak no RPC exercise the REAL failover path
    (vendored client start fails → OmpRpcStartError → one-shot runs).
    The RPC-success path is covered in test_omp_rpc_transport against the
    fake RPC server; here we pin the engine-side contract.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # non-RPC binary: prints nothing RPC-shaped and exits
        self.bin = _write_fake_omp(self.tmpdir, "#!/bin/sh\necho plain-out\n")
        self._old = (
            os.environ.get("HERMES_OMP_BIN"),
            os.environ.get("HERMES_OMP_TRANSPORT"),
            os.environ.get("HERMES_OMP_RPC_STARTUP"),
        )
        os.environ["HERMES_OMP_BIN"] = self.bin
        os.environ["HERMES_OMP_RPC_STARTUP"] = "2"  # fail over fast in tests
        self.addCleanup(self._restore)

    def _restore(self):
        bin_, transport, startup = self._old
        for key, val in (
            ("HERMES_OMP_BIN", bin_),
            ("HERMES_OMP_TRANSPORT", transport),
            ("HERMES_OMP_RPC_STARTUP", startup),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_non_rpc_binary_falls_back_to_oneshot(self):
        import tools.omp_delegation as mod
        entry = mod._run_omp_task(0, "p", "m", None, 30, None)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["summary"], "plain-out")
        self.assertEqual(entry["transport"], "oneshot-fallback")

    def test_kill_switch_forces_oneshot(self):
        import tools.omp_delegation as mod
        os.environ["HERMES_OMP_TRANSPORT"] = "oneshot"
        entry = mod._run_omp_task(0, "p", "m", None, 30, None)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["transport"], "oneshot")  # direct one-shot stamps transport

    def test_failed_oneshot_after_rpc_start_failure_keeps_error(self):
        import tools.omp_delegation as mod
        bad = _write_fake_omp(self.tmpdir, "#!/bin/sh\necho out; echo err >&2; exit 3\n")
        os.environ["HERMES_OMP_BIN"] = bad
        entry = mod._run_omp_task(0, "p", "m", None, 30, None)
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["exit_reason"], "error")
        self.assertEqual(entry["transport"], "oneshot-fallback")
        self.assertIn("out", entry["error"])

    def test_rpc_success_stamps_transport(self):
        import tools.omp_delegation as mod
        from tools import omp_rpc_transport
        real = omp_rpc_transport.run_omp_task_rpc
        with mock.patch.object(
                omp_rpc_transport, "run_omp_task_rpc",
                side_effect=lambda **kw: {
                    "status": "completed", "summary": "rpc-ok",
                    "exit_reason": "completed", "truncated": False,
                    "model": kw.get("model", "m"),
                    "duration_seconds": 0.01,
                }), \
             mock.patch.object(mod, "RPC_STARTUP_TIMEOUT", 2.0):
            try:
                entry = mod._run_omp_task(0, "p", "m", None, 30, "f")
            finally:
                omp_rpc_transport.run_omp_task_rpc = real
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["summary"], "rpc-ok")
        self.assertEqual(entry["transport"], "rpc")
        self.assertEqual(entry["task_index"], 0)

    def test_rpc_start_failure_no_double_execution(self):
        """Fallback fires ONLY on start failure — task never sent twice."""
        import tools.omp_delegation as mod
        from tools import omp_rpc_transport
        calls = {"rpc": 0, "oneshot": 0}

        def fake_rpc(**kw):
            calls["rpc"] += 1
            raise omp_rpc_transport.OmpRpcStartError("RpcTimeoutError: no ready frame")

        def fake_oneshot(*a, **kw):
            calls["oneshot"] += 1
            return {"task_index": a[0], "status": "completed",
                    "summary": "one", "exit_reason": "completed",
                    "truncated": False, "model": "m",
                    "duration_seconds": 0.01}

        real_rpc = omp_rpc_transport.run_omp_task_rpc
        real_os = mod._run_omp_one_shot
        omp_rpc_transport.run_omp_task_rpc = fake_rpc
        mod._run_omp_one_shot = fake_oneshot
        try:
            entry = mod._run_omp_task(7, "p", "m", None, 30, None)
        finally:
            omp_rpc_transport.run_omp_task_rpc = real_rpc
            mod._run_omp_one_shot = real_os
        self.assertEqual(calls, {"rpc": 1, "oneshot": 1})
        self.assertEqual(entry["transport"], "oneshot-fallback")
        self.assertEqual(entry["task_index"], 7)

    def test_killprocs_skips_none_pid(self):
        """Interrupt never resolves a None pid to our own process group."""
        import tools.omp_delegation as mod

        class _Unspawned:
            pid = None

            def kill(self):
                raise AssertionError("kill() on unspawned child must not fire")

        # must not raise / must not kill US
        mod._kill_procs([_Unspawned()])



class TestDelegationNames(unittest.TestCase):
    """M0A §8.1 item 1: `name` is schema-required and flows everywhere."""

    def test_model_schema_hard_requires_name(self):
        from tools.delegate_tool import (
            DELEGATE_TASK_SCHEMA,
            _build_dynamic_schema_overrides,
        )
        for schema in (DELEGATE_TASK_SCHEMA, _build_dynamic_schema_overrides()):
            items = schema["parameters"]["properties"]["tasks"]["items"]
            self.assertEqual(items["required"], ["goal", "name"])
            self.assertEqual(items["properties"]["name"]["type"], "string")
        # the dynamic description text mandates the name too
        dyn = _build_dynamic_schema_overrides()
        self.assertIn("name", dyn["parameters"]["properties"]["tasks"]["description"])
        self.assertIn("name", dyn["description"])

    def test_normalize_fallback_and_collapsing(self):
        from tools.delegate_tool import normalize_delegation_names
        tasks = normalize_delegation_names([
            {"goal": "a"},                       # no name → task-0
            {"goal": "b", "name": "  fix \n auth "},  # whitespace collapsed
            {"goal": "c", "name": ""},           # blank → task-2
        ])
        self.assertEqual([t["name"] for t in tasks],
                         ["task-1", "fix auth", "task-3"])

    def test_names_flow_to_dispatch_response_and_registry_id(self):
        import tools.omp_delegation as mod
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmpdir = Path(self._tmp.name)
        fx = _EnvFixture(tmpdir)
        fx.__enter__()
        self.addCleanup(fx.__exit__)
        bin_ = _write_fake_omp(tmpdir, "#!/bin/sh\necho bg-ok\n")
        old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = bin_
        self.addCleanup(lambda: os.environ.pop("HERMES_OMP_BIN", None)
                        if old_bin is None else os.environ.__setitem__(
                            "HERMES_OMP_BIN", old_bin))
        captured = {}

        def fake_dispatch(**kwargs):
            captured.update(kwargs)
            return {"status": "dispatched",
                    "delegation_id": kwargs["delegation_id"]}

        with mock.patch("tools.async_delegation.dispatch_async_delegation_batch",
                        side_effect=fake_dispatch), \
             mock.patch("tools.approval.get_current_session_key",
                        return_value="agent:main:test"), \
             mock.patch("gateway.session_context.async_delivery_supported",
                        return_value=True), \
             mock.patch("gateway.session_context.get_session_env",
                        return_value=""), \
             mock.patch.object(mod, "_render_omp_config_once"):
            out = mod.dispatch_omp_delegation(
                _make_parent(depth=0),
                {"tasks": [{"goal": "g1", "name": "auth-refactor"},
                           {"goal": "g2", "name": "修复测试"}]})
        payload = json.loads(out)
        self.assertEqual(payload["status"], "dispatched")
        children = payload["children"]
        self.assertEqual([c["name"] for c in children],
                         ["auth-refactor", "修复测试"])
        self.assertEqual([c["subagent_id"] for c in children],
                         [f"{payload['delegation_id']}/0",
                          f"{payload['delegation_id']}/1"])
        # our pre-generated id is the one the async registry received
        self.assertEqual(captured["delegation_id"], payload["delegation_id"])

    def test_missing_names_get_derived_fallback_sync(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmpdir = Path(self._tmp.name)
        fx = _EnvFixture(tmpdir)
        fx.__enter__()
        self.addCleanup(fx.__exit__)
        bin_ = _write_fake_omp(tmpdir, "#!/bin/sh\necho ok\n")
        old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = bin_
        self.addCleanup(lambda: os.environ.pop("HERMES_OMP_BIN", None)
                        if old_bin is None else os.environ.__setitem__(
                            "HERMES_OMP_BIN", old_bin))
        import tools.omp_delegation as mod
        with mock.patch.object(mod, "_render_omp_config_once"), \
             mock.patch.object(mod, "_rpc_disabled", return_value=True):
            out = mod.dispatch_omp_delegation(
                _make_parent(depth=1),
                {"tasks": [{"goal": "alpha"}, {"goal": "beta", "name": ""}]})
        payload = json.loads(out)
        self.assertEqual([r["name"] for r in payload["results"]],
                         ["task-1", "task-2"])
        with mock.patch.object(mod, "_render_omp_config_once"), \
             mock.patch.object(mod, "_rpc_disabled", return_value=True):
            out = mod.dispatch_omp_delegation(
                _make_parent(depth=1), {"goal": "legacy"})
        payload = json.loads(out)
        self.assertEqual(payload["results"][0]["name"], "task-1")


class _FakeRpcTransport:
    """RPC-transport duck for control-plane tests (steer/abort/kills)."""

    def __init__(self, fail_abort=False, pid=None):
        self.steer_calls = []
        self.abort_calls = []
        self.fail_abort = fail_abort
        self.pid = pid
        self.killed = False

    def steer(self, text):
        self.steer_calls.append(text)

    def abort(self, reason=None):
        self.abort_calls.append(reason)
        if self.fail_abort:
            raise RuntimeError("connection lost")

    def kill(self):
        self.killed = True


class TestSteerStopForwarding(unittest.TestCase):
    """M0A §8.1 item 2: steer/stop forward to live children."""

    def setUp(self):
        import tools.omp_delegation as mod
        self.mod = mod
        # each test starts from an empty registry
        self._real_registry = dict(mod._live_children)
        mod._live_children.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        self.mod._live_children.clear()
        self.mod._live_children.update(self._real_registry)

    def _register(self, child_id, transport, *, owner="sess-b1-test",
                  steerable=True, name="probe", batch="deleg_aaa", index=0):
        self.mod._register_live_child({
            "child_id": child_id,
            "delegation_id": batch,
            "task_index": index,
            "name": name,
            "goal": "probe goal",
            "model": "prov/m",
            "owner_session_id": owner,
            "transport_kind": "rpc" if steerable else "oneshot",
            "steerable": steerable,
        }, transport)
        self.addCleanup(lambda: self.mod._unregister_live_child(
            child_id, transport))
        return transport

    def _dispatch_control(self, args, parent=None):
        with mock.patch.object(self.mod, "_render_omp_config_once"):
            return self.mod.dispatch_omp_delegation(
                parent or _make_parent(), args)

    def test_steer_forwards_over_rpc(self):
        transport = self._register("deleg_aaa/0", _FakeRpcTransport())
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_aaa/0",
             "message": "switch to plan B"})
        payload = json.loads(out)
        self.assertEqual(payload["action"], "steer")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(transport.steer_calls, ["switch to plan B"])

    def test_steer_resolves_bare_delegation_id_single_child(self):
        transport = self._register("deleg_bbb/0", _FakeRpcTransport(),
                                   batch="deleg_bbb")
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_bbb",
             "message": "go"})
        self.assertEqual(json.loads(out)["status"], "queued")
        self.assertEqual(transport.steer_calls, ["go"])

    def test_steer_ambiguous_batch_is_rejected_with_candidates(self):
        self._register("deleg_ccc/0", _FakeRpcTransport(), batch="deleg_ccc")
        self._register("deleg_ccc/1", _FakeRpcTransport(), batch="deleg_ccc",
                       index=1)
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_ccc",
             "message": "go"})
        self.assertIn("multiple live children", out)
        self.assertIn("deleg_ccc/0", out)
        self.assertIn("deleg_ccc/1", out)

    def test_steer_oneshot_child_rejected(self):
        self._register("deleg_ddd/0", _FakeRpcTransport(),
                       steerable=False, batch="deleg_ddd")
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_ddd/0",
             "message": "go"})
        self.assertIn("one-shot transport", out)

    def test_steer_requires_message(self):
        self._register("deleg_eee/0", _FakeRpcTransport(), batch="deleg_eee")
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_eee/0", "message": "  "})
        self.assertIn("non-empty 'message'", out)

    def test_steer_transport_failure_is_error(self):
        class _Boom:
            def steer(self, text):
                raise RuntimeError("RpcProcessExitError: child died")
        self._register("deleg_fff/0", _Boom(), batch="deleg_fff")
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_fff/0",
             "message": "go"})
        self.assertIn("child died", out)

    @pytest.mark.live_system_guard_bypass
    def test_stop_rpc_aborts_gracefully(self):
        import subprocess as sp
        sleeper = sp.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        transport = _FakeRpcTransport(pid=sleeper.pid)
        self._register("deleg_ggg/0", transport, batch="deleg_ggg")
        out = self._dispatch_control(
            {"action": "stop", "subagent_id": "deleg_ggg/0"})
        payload = json.loads(out)
        self.assertEqual(payload["status"], "interrupt_requested")
        self.assertEqual(len(transport.abort_calls), 1)
        # graceful: the process itself is still alive (abort, not SIGKILL)
        self.assertIsNone(sleeper.poll())

    @pytest.mark.live_system_guard_bypass
    def test_stop_connection_loss_falls_back_to_sigkill(self):
        import subprocess as sp
        sleeper = sp.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        transport = _FakeRpcTransport(fail_abort=True, pid=sleeper.pid)
        self._register("deleg_hhh/0", transport, batch="deleg_hhh")
        out = self._dispatch_control(
            {"action": "stop", "subagent_id": "deleg_hhh/0"})
        self.assertEqual(json.loads(out)["status"], "interrupt_requested")
        # abort raised → process group SIGKILLed as the fallback
        deadline = time.time() + 5
        while time.time() < deadline and sleeper.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(sleeper.poll())

    @pytest.mark.live_system_guard_bypass
    def test_stop_oneshot_kills_process(self):
        import subprocess as sp
        sleeper = sp.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        self._register("deleg_iii/0", sleeper, steerable=False,
                       batch="deleg_iii")
        out = self._dispatch_control(
            {"action": "stop", "subagent_id": "deleg_iii/0"})
        self.assertEqual(json.loads(out)["status"], "interrupt_requested")
        deadline = time.time() + 5
        while time.time() < deadline and sleeper.poll() is None:
            time.sleep(0.05)
        self.assertIsNotNone(sleeper.poll())

    def test_list_shows_children_with_names(self):
        self._register("deleg_jjj/0", _FakeRpcTransport(), batch="deleg_jjj",
                       name="auth-refactor")
        self._register("deleg_jjj/1", _FakeRpcTransport(), batch="deleg_jjj",
                       name="docs-sweep", index=1)
        out = self._dispatch_control({"action": "list"})
        payload = json.loads(out)
        self.assertEqual(payload["count"], 2)
        by_id = {e["subagent_id"]: e for e in payload["subagents"]}
        self.assertEqual(by_id["deleg_jjj/0"]["name"], "auth-refactor")
        self.assertEqual(by_id["deleg_jjj/1"]["name"], "docs-sweep")
        self.assertTrue(by_id["deleg_jjj/0"]["steerable"])

    def test_ownership_mismatch_denied(self):
        self._register("deleg_kkk/0", _FakeRpcTransport(),
                       owner="sess-OTHER", batch="deleg_kkk")
        out = self._dispatch_control(
            {"action": "steer", "subagent_id": "deleg_kkk/0",
             "message": "go"})
        self.assertIn("spawn tree", out)
        # ...and the child was NOT steered
        # (checked implicitly: no queued status)
        self.assertNotIn("queued", out)

    def test_delegate_tool_control_fallthrough(self):
        """delegate_tool._handle_control_action reaches the omp registry."""
        from tools import delegate_tool
        transport = self._register("deleg_lll/0", _FakeRpcTransport(),
                                   batch="deleg_lll")
        with mock.patch.object(delegate_tool, "_active_subagents", {}):
            out = delegate_tool._handle_control_action(
                "steer", "deleg_lll/0", "redirect now", _make_parent())
        payload = json.loads(out)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(transport.steer_calls, ["redirect now"])
        # list merges omp children even with an empty hermes registry
        with mock.patch.object(delegate_tool, "_active_subagents", {}):
            out = delegate_tool._handle_control_action(
                "list", None, None, _make_parent())
        payload = json.loads(out)
        ids = [e["subagent_id"] for e in payload["subagents"]]
        self.assertIn("deleg_lll/0", ids)

    def test_registry_empties_after_sync_run(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmpdir = Path(self._tmp.name)
        bin_ = _write_fake_omp(tmpdir, "#!/bin/sh\necho done\n")
        old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = bin_
        old_transport = os.environ.get("HERMES_OMP_TRANSPORT")
        os.environ["HERMES_OMP_TRANSPORT"] = "oneshot"
        self.addCleanup(lambda: (
            os.environ.pop("HERMES_OMP_BIN", None) if old_bin is None
            else os.environ.__setitem__("HERMES_OMP_BIN", old_bin),
            os.environ.pop("HERMES_OMP_TRANSPORT", None) if old_transport is None
            else os.environ.__setitem__("HERMES_OMP_TRANSPORT", old_transport)))
        with mock.patch.object(self.mod, "_rpc_disabled", return_value=True):
            self.mod._sync_run(
                [{"prompt": "p", "name": "probe", "goal": "probe"}],
                {"OMP_MODEL": "m"}, None, 30, 1,
                delegation_id="deleg_mmm", owner_session_id="sess-b1-test")
        self.assertEqual(self.mod._live_children, {})

    def test_approval_bridge_bind_never_calls_getfqdn(self):
        """HTTPServer.server_bind's getfqdn() is a multi-second DNS stall
        on resolver-less boxes — the bridge must bind without it."""
        import socket as _socket
        with mock.patch.object(_socket, "getfqdn",
                               side_effect=AssertionError("getfqdn called")):
            bridge = self.mod._ApprovalBridgeServer(None)
        try:
            path = bridge.start()
            self.assertTrue(os.path.exists(path))
            self.assertEqual(bridge._server.server_name,
                             "mercury-approval-bridge")
        finally:
            bridge.stop()

if __name__ == "__main__":
    unittest.main()
