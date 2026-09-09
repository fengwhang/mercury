"""Regression tests: parallel-delegation memory safety.

Covers the dominant terms of the parallel-wave memory blowup (~17GB seen):
(a) per-child RSS x N — the omp path fanned out with NO task-count cap
    (max_workers = len(tasks)) and no memory awareness;
(b) parent context accumulation — omp batch results re-entered verbatim;
(c) result payloads — no trim/spill on the omp path (the Mercury-child path
    already had _apply_summary_budget; now both do).

Serial (N=1) behavior is pinned unchanged.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import tools.delegate_tool as dt
import tools.omp_delegation as od


def _big_summary(n=100_000):
    return "HEAD_MARKER\n" + ("X" * n) + "\nTAIL_MARKER"


class _FakeCompressor:
    def __init__(self, context_length, max_tokens):
        self.context_length = context_length
        self.max_tokens = max_tokens


class _FakeParent:
    def __init__(self, context_length=200_000, used_tokens=10_000, max_tokens=8_000):
        self.context_compressor = _FakeCompressor(context_length, max_tokens)
        self.session_prompt_tokens = used_tokens


def _stub_run_factory(summaries=None, delay=0.0, counter=None):
    def _stub(task_index, *args, **kwargs):
        if counter is not None:
            counter["active"] += 1
            counter["peak"] = max(counter["peak"], counter["active"])
        try:
            if delay:
                time.sleep(delay)
            summary = summaries[task_index] if summaries else "ok-%d" % task_index
            return {"task_index": task_index, "status": "completed",
                    "summary": summary, "exit_reason": "completed",
                    "truncated": False}
        finally:
            if counter is not None:
                counter["active"] -= 1
    return _stub


class TestEffectiveCap(unittest.TestCase):
    def test_unknown_memory_leaves_configured_cap(self):
        with mock.patch.object(dt, "_memory_stats_mb", return_value=None):
            self.assertEqual(dt._effective_concurrency_cap(10), 10)

    def test_disabled_cap_leaves_configured_cap(self):
        with mock.patch.object(dt, "_memory_stats_mb",
                               return_value=(16384, 12000)), \
             mock.patch.object(dt, "_memory_cap_enabled", return_value=False):
            self.assertEqual(dt._effective_concurrency_cap(10), 10)

    def test_tight_memory_clamps_cap(self):
        # 8GB box, 5GB free: reserve=max(2048, 2048)=2048 → (5000-2048)//450=6
        with mock.patch.object(dt, "_memory_stats_mb", return_value=(8192, 5000)):
            self.assertEqual(dt._memory_concurrency_cap(), 6)
            self.assertEqual(dt._effective_concurrency_cap(10), 6)

    def test_never_blocks_serial(self):
        # Nearly OOM still allows one child — the N=1 path never blocks.
        with mock.patch.object(dt, "_memory_stats_mb", return_value=(4096, 100)):
            self.assertEqual(dt._memory_concurrency_cap(), 1)
            self.assertEqual(dt._effective_concurrency_cap(10), 1)
            self.assertEqual(dt._effective_concurrency_cap(1), 1)

    def test_configured_cap_still_rules_when_lower(self):
        # Plenty of RAM: min() keeps the user's configured value, never above.
        with mock.patch.object(dt, "_memory_stats_mb",
                               return_value=(65536, 60000)):
            self.assertEqual(dt._effective_concurrency_cap(3), 3)

    def test_env_disable(self):
        with mock.patch.object(dt, "_memory_stats_mb",
                               return_value=(8192, 5000)):
            with mock.patch.dict(os.environ, {"HERMES_DELEGATION_MEMORY_CAP": "0"}):
                # _memory_cap_enabled reads real config (default True) then env.
                with mock.patch.object(dt, "_load_config", return_value={}):
                    self.assertFalse(dt._memory_cap_enabled())
                    self.assertIsNone(dt._memory_concurrency_cap())

    def test_configured_cap_untouched(self):
        # _get_max_concurrent_children keeps exact legacy semantics.
        with mock.patch.object(dt, "_load_config",
                               return_value={"max_concurrent_children": 50}):
            self.assertEqual(dt._get_max_concurrent_children(), 50)


class TestOmpCapRespected(unittest.TestCase):
    def test_dispatch_rejects_over_cap_before_env(self):
        # Validation runs before env/binary resolution: hermetic, no omp needed.
        tasks = [{"goal": "g%d" % i, "name": "t-%d" % i} for i in range(3)]
        with mock.patch.object(dt, "_get_max_concurrent_children", return_value=2):
            out = od.dispatch_omp_delegation(_FakeParent(), {"tasks": tasks})
        err = json.loads(out)
        self.assertIn("Too many tasks", err.get("error", ""))
        self.assertIn("max_concurrent_children", err.get("error", ""))

    def test_dispatch_accepts_at_cap(self):
        # At cap the call proceeds past validation. _omp_delegate_env is
        # stubbed to an error so the test never touches real env/binary
        # resolution; reaching that stage proves acceptance.
        tasks = [{"goal": "g%d" % i, "name": "t-%d" % i} for i in range(2)]
        with mock.patch.object(dt, "_get_max_concurrent_children", return_value=2), \
             mock.patch.object(od, "_omp_delegate_env", return_value=({}, "STUB_ENV")):
            out = od.dispatch_omp_delegation(_FakeParent(), {"tasks": tasks})
        self.assertNotIn("Too many tasks", out)
        self.assertIn("STUB_ENV", out)

    def test_pool_width_bounded_by_max_workers(self):
        counter = {"active": 0, "peak": 0}
        tasks = [{"prompt": "p%d" % i} for i in range(6)]
        with mock.patch.object(od, "_run_omp_task",
                               side_effect=_stub_run_factory(delay=0.2, counter=counter)):
            res = od._sync_run(tasks, {"OMP_MODEL": "m"}, None, 60, 2,
                               parent_agent=None)
        self.assertEqual(len(res["results"]), 6)
        self.assertLessEqual(counter["peak"], 2)


class TestOmpSpill(unittest.TestCase):
    def test_large_results_truncated_with_retrievable_spill(self):
        big = _big_summary()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ,
                                 {"HERMES_HOME": os.path.join(td, ".mercury")}):
                with mock.patch.object(od, "_run_omp_task",
                                       side_effect=_stub_run_factory([big, big])):
                    res = od._sync_run(
                        [{"prompt": "a"}, {"prompt": "b"}],
                        {"OMP_MODEL": "m"}, None, 60, 2, parent_agent=None)
                    for r in res["results"]:
                        self.assertTrue(r["summary_truncated"])
                        self.assertLess(len(r["summary"]), len(big))
                        self.assertIn("HEAD_MARKER", r["summary"])
                        self.assertIn("TAIL_MARKER", r["summary"])
                        path = r.get("summary_full_path")
                        self.assertTrue(path and os.path.exists(path))
                        with open(path, encoding="utf-8") as fh:
                            self.assertEqual(fh.read(), big)
                        self.assertIn(os.path.join("cache", "delegation"), path)

    def test_parent_context_bounded_with_n_children(self):
        n = 10
        big = _big_summary()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ,
                                 {"HERMES_HOME": os.path.join(td, ".mercury")}):
                with mock.patch.object(od, "_run_omp_task",
                                       side_effect=_stub_run_factory([big] * n)):
                    res = od._sync_run(
                        [{"prompt": "p%d" % i} for i in range(n)],
                        {"OMP_MODEL": "m"}, None, 60, n, parent_agent=None)
                    # Static ceiling (parent unknown): each in-context summary ≤ cap+footer.
                    per_child = dt.DEFAULT_MAX_SUMMARY_CHARS + 2000  # footer slack
                    total = sum(len(r["summary"]) for r in res["results"])
                    self.assertLessEqual(total, n * per_child)
                    self.assertLess(total, n * len(big))  # strictly better than verbatim N×100KB
                    self.assertTrue(all(r["summary_truncated"] for r in res["results"]))


class TestSerialUnchanged(unittest.TestCase):
    def test_single_small_result_verbatim(self):
        with mock.patch.object(od, "_run_omp_task",
                               side_effect=_stub_run_factory(["s"])):
            res = od._sync_run([{"prompt": "only"}], {"OMP_MODEL": "m"},
                               None, 60, 1, parent_agent=None)
        self.assertEqual(res["results"][0]["summary"], "s")
        self.assertNotIn("summary_truncated", res["results"][0])
        self.assertNotIn("summary_full_path", res["results"][0])
        self.assertEqual(res["engine"], "omp")

    def test_single_task_takes_direct_path(self):
        # N=1 runs inline (no pool): peak concurrency stays 1 even at width 1.
        counter = {"active": 0, "peak": 0}
        with mock.patch.object(od, "_run_omp_task",
                               side_effect=_stub_run_factory(["s"], counter=counter)):
            od._sync_run_inner([{"prompt": "only"}], {"OMP_MODEL": "m"},
                               None, 60, 1)
        self.assertEqual(counter["peak"], 1)


if __name__ == "__main__":
    unittest.main()
