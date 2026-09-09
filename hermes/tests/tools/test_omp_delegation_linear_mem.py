"""Linear per-subagent memory: regression tests for the omp fan-out.

Covers the agent/delegation-linear-mem fix (batch-shared child env, no
concurrency caps):

* the .env safety net is computed ONCE per batch, not once per child;
* every task still spawns (no reject-at-cap), with one worker per task;
* per-child assembly heap stays linear within tolerance across N;
* the safety net survives a missing .env (NameError regression).

Stdlib unittest only. Run via scripts/run_tests.sh (per-file isolation).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import tracemalloc
import unittest
import unittest.mock as mock
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BRIDGE_DELEGATE_OUT = "OMP_MODEL=prov/delegate-model\nOMP_FALLBACK_CHAIN=prov/delegate-fallback\n"

FIXTURE_CONFIG = """\
models:
  default: "prov/main-model"
  fallback: "prov/main-fallback"
  delegate_default: "prov/delegate-model"
  delegate_fallback: "prov/delegate-fallback"
"""


def _write_fake_omp(directory: Path, body: str) -> str:
    p = directory / "omp"
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def _make_parent(depth: int = 0) -> MagicMock:
    parent = MagicMock()
    parent._delegate_depth = depth
    parent.session_id = "sess-linear-mem-test"
    parent.base_url = "https://example.test/v1"
    parent.api_key = "***"
    parent.provider = "prov"
    parent.model = "prov/main-model"
    parent.platform = "cli"
    parent.cwd = str(REPO)
    return parent


class _EnvFixture:
    """Point the module at fixture bridge/config + scrubbed homes."""

    def __init__(self, tmpdir: Path):
        self.tmpdir = tmpdir
        self.bridge = tmpdir / "bridge.py"
        self.bridge.write_text(
            "#!/usr/bin/env python3\n"
            f"import sys; sys.stdout.write({BRIDGE_DELEGATE_OUT!r});"
            "sys.stderr.write(''); sys.exit(0)\n"
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


class TestSharedOverridesMissingDotenv(unittest.TestCase):
    """_shared_env_overrides() with MERCURY_HOME set but no .env file."""

    def test_missing_dotenv_returns_empty(self):
        import tools.omp_delegation as mod
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"MERCURY_HOME": home}):
                with mock.patch.object(
                    mod, "_nous_search_env_overrides", return_value={}
                ):
                    # Regression: `overrides` was referenced before
                    # assignment on this path (NameError).
                    self.assertEqual(mod._shared_env_overrides(), {})

    def test_unset_home_returns_empty(self):
        import tools.omp_delegation as mod
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MERCURY_HOME", None)
            self.assertEqual(mod._shared_env_overrides(), {})


class TestBatchBaseEnvComputedOnce(unittest.TestCase):
    """An 8-child fan-out performs the shared env work exactly once."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.fx = _EnvFixture(self.tmpdir)
        self.fx.__enter__()
        self.addCleanup(self.fx.__exit__)
        self.addCleanup(self._tmp.cleanup)
        self.fake_omp = _write_fake_omp(
            self.tmpdir, "#!/bin/sh\necho child-ok\n")
        self._old_bin = os.environ.get("HERMES_OMP_BIN")
        self._old_transport = os.environ.get("HERMES_OMP_TRANSPORT")
        os.environ["HERMES_OMP_BIN"] = self.fake_omp
        # One-shot transport: exercise the real spawn path per child.
        os.environ["HERMES_OMP_TRANSPORT"] = "oneshot"
        # Probe .env key the safety net must deliver to every child.
        mercury_home = self.tmpdir / "mhome"
        mercury_home.mkdir()
        (mercury_home / ".env").write_text("LINMEM_PROBE_TAG=probe-123\n")
        self._old_mhome = os.environ.get("MERCURY_HOME")
        os.environ["MERCURY_HOME"] = str(mercury_home)
        os.environ.pop("LINMEM_PROBE_TAG", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, val in (
            ("HERMES_OMP_BIN", self._old_bin),
            ("HERMES_OMP_TRANSPORT", self._old_transport),
            ("MERCURY_HOME", self._old_mhome),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_eight_children_one_shared_computation(self):
        import tools.omp_delegation as mod
        real = mod._shared_env_overrides
        calls = []

        def counting():
            calls.append(1)
            return real()

        n = 8
        tasks = [{"prompt": f"task {i}", "name": f"t{i}", "goal": f"g{i}"}
                 for i in range(n)]
        with mock.patch.object(
            mod, "_nous_search_env_overrides", return_value={}
        ), mock.patch.object(
            mod, "_shared_env_overrides", side_effect=counting
        ):
            res = mod._sync_run(
                tasks, {"OMP_MODEL": "m"}, None, 60, n)
        self.assertEqual(len(res["results"]), n)
        for entry in res["results"]:
            self.assertEqual(entry["status"], "completed")
        # The expensive shared work (.env read + parse) ran ONCE for the
        # whole batch — not once per child (2x per child before the fix:
        # _run_omp_task AND _run_omp_one_shot each recomputed it).
        self.assertEqual(len(calls), 1)

    def test_child_envs_carry_overrides_and_stay_isolated(self):
        import tools.omp_delegation as mod
        seen = []

        real_popen = mod.subprocess.Popen

        def spy_popen(cmd, **kw):
            seen.append((list(cmd), dict(kw.get("env") or {})))
            return real_popen(cmd, **kw)

        tasks = [{"prompt": f"task {i}", "name": f"t{i}", "goal": f"g{i}"}
                 for i in range(3)]
        with mock.patch.object(
            mod, "_nous_search_env_overrides", return_value={}
        ), mock.patch.object(
            mod.subprocess, "Popen", side_effect=spy_popen
        ):
            mod._sync_run(tasks, {"OMP_MODEL": "m"}, None, 60, 3)
        child_envs = [env for _, env in seen
                      if env.get("LINMEM_PROBE_TAG") == "probe-123"]
        self.assertEqual(len(child_envs), 3)
        for env in child_envs:
            self.assertEqual(env.get("LINMEM_PROBE_TAG"), "probe-123")
        # Distinct dicts per child: mutating one must not leak into others.
        child_envs[0]["LINMEM_MUTANT"] = "x"
        self.assertNotIn("LINMEM_MUTANT", child_envs[1])
        self.assertNotIn("LINMEM_MUTANT", child_envs[2])
        self.assertIsNot(child_envs[0], child_envs[1])
        # The bridge validation ran exactly ONCE despite 3 racing worker
        # threads (double-checked lock); before the fix each thread that
        # arrived early spawned its own bridge subprocess.
        bridge_runs = [cmd for cmd, _ in seen
                       if len(cmd) > 1 and str(cmd[1]).endswith("bridge.py")]
        self.assertEqual(len(bridge_runs), 1)


class TestNoTaskCountCap(unittest.TestCase):
    """Task counts above max_concurrent_children are NOT rejected.

    Reject-at-cap is a bug, not a feature: every task gets its own
    worker (max_workers == N), however large N is.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.fx = _EnvFixture(self.tmpdir)
        self.fx.__enter__()
        self.addCleanup(self.fx.__exit__)
        self.addCleanup(self._tmp.cleanup)
        self.fake_omp = _write_fake_omp(
            self.tmpdir, "#!/bin/sh\necho child-ok\n")
        self._old_bin = os.environ.get("HERMES_OMP_BIN")
        os.environ["HERMES_OMP_BIN"] = self.fake_omp
        self.addCleanup(self._restore_bin)

    def _restore_bin(self):
        if self._old_bin is None:
            os.environ.pop("HERMES_OMP_BIN", None)
        else:
            os.environ["HERMES_OMP_BIN"] = self._old_bin

    def test_twelve_tasks_all_run_with_twelve_workers(self):
        import tools.omp_delegation as mod
        captured = {}

        def fake_sync(tasks, env, workdir, timeout, max_workers, *a, **kw):
            captured["n"] = len(tasks)
            captured["max_workers"] = max_workers
            return {
                "results": [
                    {"task_index": i, "status": "completed",
                     "summary": "s", "exit_reason": "completed",
                     "truncated": False}
                    for i in range(len(tasks))
                ],
                "total_duration_seconds": 0.01,
                "engine": "omp",
            }

        n = 12  # above the default max_concurrent_children=10
        args = {"tasks": [
            {"name": f"t{i}", "goal": f"goal number {i} does some work"}
            for i in range(n)
        ]}
        with mock.patch.object(mod, "_render_omp_config_once"), \
             mock.patch.object(mod, "_sync_run", side_effect=fake_sync):
            out = mod.dispatch_omp_delegation(_make_parent(depth=1), args)
        payload = json.loads(out)
        self.assertEqual(payload["engine"], "omp")
        self.assertEqual(len(payload["results"]), n)
        self.assertEqual(captured["n"], n)
        self.assertEqual(captured["max_workers"], n)


class TestPerChildAssemblyLinear(unittest.TestCase):
    """Per-child fan-out assembly heap stays linear across N.

    Exercises the REAL assembly ops (one batch base env + one prompt +
    one shallow env copy per task, envs held live as the runner holds
    them) under tracemalloc. A per-child duplication regression (e.g. a
    file read or config parse moving back inside the per-task loop)
    shows up as superlinear growth here.
    """

    def _assembly_bytes(self, n):
        import tools.omp_delegation as mod
        tracemalloc.start()
        try:
            base = mod._delegate_batch_base_env()
            held = []
            for i in range(n):
                prompt = mod._build_task_prompt(
                    f"goal number {i} does some work",
                    "shared context fixture", None)
                env = dict(base)
                env["MERCURY_APPROVAL_SOCKET"] = f"sock-{i}"
                held.append((prompt, env))
            current, _ = tracemalloc.get_traced_memory()
            return current
        finally:
            tracemalloc.stop()

    def test_linear_within_tolerance(self):
        totals = {n: self._assembly_bytes(n) for n in (1, 2, 4, 8)}
        per = {n: totals[n] / n for n in totals}
        # Absolute: a task's assembly state is small (prompt + env copy).
        for n, p in per.items():
            self.assertLess(
                p, 64 * 1024,
                f"per-child assembly heap too large at N={n}: {p:.0f}B")
        # Linear: per-child mean must not grow with N (generous 3x
        # tolerance for allocator/GC noise; a real per-child duplication
        # would scale the MEAN with N, not sit inside noise).
        self.assertLessEqual(
            per[8], per[1] * 3,
            f"superlinear per-child growth: N=1: {per[1]:.0f}B, "
            f"N=8: {per[8]:.0f}B")


if __name__ == "__main__":
    unittest.main()
