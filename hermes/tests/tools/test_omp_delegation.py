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

    def test_control_actions_answer_honestly(self):
        out = self._dispatch({"action": "list"})
        payload = json.loads(out)
        self.assertEqual(payload["engine"], "omp")
        out = self._dispatch({"action": "steer", "subagent_id": "x",
                              "message": "y"})
        self.assertIn("not supported", out)
        out = self._dispatch({"action": "stop", "subagent_id": "x"})
        self.assertIn("not supported", out)

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
                     fallback_chain, batch_procs=None):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["fallback"] = fallback_chain
            return {"task_index": task_index, "status": "completed",
                    "summary": "s", "exit_reason": "completed"}

        with mock.patch.object(mod, "_run_omp_task", side_effect=fake_run):
            res = mod._sync_run(
                [{"prompt": "line1\nline2; rm -rf / --faked"}],
                {"OMP_MODEL": "m", "OMP_FALLBACK_CHAIN": "f"},
                None, 60, 1)
        self.assertEqual(captured["prompt"], "line1\nline2; rm -rf / --faked")
        self.assertEqual(captured["model"], "m")
        self.assertEqual(captured["fallback"], "f")
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
        old = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = bin_path
        try:
            tasks = [{"prompt": f"t{i}"} for i in range(4)]
            t0 = time.monotonic()
            res = mod._sync_run(tasks, {"OMP_MODEL": "m"}, None, 60, 4)
            dt = time.monotonic() - t0
        finally:
            if old is None:
                os.environ.pop("HERMES_OMP_BIN", None)
            else:
                os.environ["HERMES_OMP_BIN"] = old
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
        self.assertNotIn("transport", entry)  # one-shot never stamped

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

        def fake_oneshot(*a):
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


if __name__ == "__main__":
    unittest.main()
