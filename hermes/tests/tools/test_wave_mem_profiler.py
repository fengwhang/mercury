"""Tests for tools/wave_mem_profiler.py + its dispatch wiring.

Stdlib unittest only. Execute with the repo interpreter:

  PYTHONPATH=<repo>/hermes /opt/hermes/.venv/bin/python -m unittest \\
      tests.tools.test_wave_mem_profiler -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import wave_mem_profiler as wmp  # noqa: E402

_SCRUB_KEYS = (
    "MERCURY_WAVE_MEM_PROFILE",
    "HERMES_WAVE_MEM_PROFILE",
    "MERCURY_WAVE_MEM_PROFILE_INTERVAL",
    "MERCURY_HOME",
    "HERMES_HOME",
    "PI_CODING_AGENT_DIR",
)


def _scrub_env():
    old = {k: os.environ.get(k) for k in _SCRUB_KEYS}
    for k in _SCRUB_KEYS:
        os.environ.pop(k, None)
    return old


def _restore_env(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _synthetic_table():
    # parent(100) -> child a(200), child b(300) -> grandchild g1(400),
    # g2(500); grandchild g3(600) under g1 (depth 3); stray(700) detached
    # wave-marked; unrelated(800) ignored.
    def rec(ppid, comm, rss):
        return {"ppid": ppid, "comm": comm, "rss_mb": rss}

    return {
        100: rec(1, "hermes", 500.0),
        200: rec(100, "omp", 700.0),
        300: rec(100, "omp", 700.0),
        400: rec(200, "node", 300.0),
        500: rec(200, "node", 300.0),
        600: rec(400, "python3", 100.0),
        700: rec(1, "omp", 250.0),
        800: rec(1, "bash", 5.0),
    }


def _env_reader(pid):
    marks = {
        200: {"MERCURY_WAVE_ID": "deleg_test", "MERCURY_WAVE_NAME": "a",
              "MERCURY_WAVE_INDEX": "0"},
        300: {"MERCURY_WAVE_ID": "deleg_test", "MERCURY_WAVE_NAME": "b",
              "MERCURY_WAVE_INDEX": "1"},
        700: {"MERCURY_WAVE_ID": "deleg_test", "MERCURY_WAVE_NAME": "zz",
              "MERCURY_WAVE_INDEX": "9"},
    }
    return marks.get(pid, {})


class TestAttributeTree(unittest.TestCase):
    def test_multilevel_attribution(self):
        buckets = wmp.attribute_tree(
            _synthetic_table(), 100, "deleg_test", env_reader=_env_reader)
        self.assertEqual([r["pid"] for r in buckets["parent"]], [100])
        self.assertEqual(
            sorted(r["pid"] for r in buckets["children"]), [200, 300])
        self.assertEqual(
            sorted(r["pid"] for r in buckets["grandchildren"]), [400, 500, 600])
        # depth preserved past level 2
        by_pid = {r["pid"]: r for r in buckets["grandchildren"]}
        self.assertEqual(by_pid[600]["depth"], 3)
        # wave-marked but outside the tree -> detached, not dropped
        self.assertEqual([r["pid"] for r in buckets["detached"]], [700])
        # unrelated proc ignored everywhere
        all_pids = [r["pid"] for rs in buckets.values() for r in rs]
        self.assertNotIn(800, all_pids)

    def test_identity_markers_surface(self):
        buckets = wmp.attribute_tree(
            _synthetic_table(), 100, "deleg_test", env_reader=_env_reader)
        by_pid = {r["pid"]: r for r in buckets["children"]}
        self.assertEqual(by_pid[200]["name"], "a")
        self.assertEqual(by_pid[200]["child_id"], "0")
        self.assertEqual(buckets["detached"][0]["child_id"], "9")


class TestSampleSummarizeRoundTrip(unittest.TestCase):
    def _write_log(self, path):
        prof = wmp.WaveProfiler(
            wave_id="deleg_test", parent_pid=100, parent_session_id="",
            interval_s=1000.0, out_path=path)
        tables = [_synthetic_table(), _synthetic_table()]
        # second tick: children grew (hypothesis a signal)
        for pid in (200, 300):
            tables[1][pid] = dict(tables[1][pid], rss_mb=1400.0)

        def _fake_environ(pid, keys):
            full = _env_reader(pid)
            return {k: full[k] for k in keys if k in full}

        with patch.object(wmp, "_read_proc_table", side_effect=tables), \
                patch.object(wmp, "_proc_environ", side_effect=_fake_environ), \
                patch.object(wmp, "_proc_cmdline", return_value="omp child"):
            s1 = prof.sample_once()
            s2 = prof.sample_once()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "header", "v": 1,
                                 "wave_id": "deleg_test"}) + "\n")
            fh.write(json.dumps(s1) + "\n")
            fh.write(json.dumps(s2) + "\n")
            fh.write(json.dumps({"type": "footer", "ended_utc": "t",
                                 "elapsed_s": 10.0}) + "\n")
        return s1, s2

    def test_log_format_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave-deleg_test.jsonl"
            self._write_log(path)
            records = list(wmp.iter_records(path))
            self.assertEqual(
                [r["type"] for r in records], ["header", "sample", "sample", "footer"])
            for r in records:
                self.assertIn("type", r)
            roll = records[1]["rollup"]
            for key in ("parent_rss_mb", "children_rss_mb",
                        "grandchildren_rss_mb", "detached_rss_mb",
                        "n_children", "n_grandchildren", "payload_mb"):
                self.assertIn(key, roll)

    def test_peaks_attribute_per_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave-deleg_test.jsonl"
            self._write_log(path)
            summary = wmp.summarize(path)
        self.assertEqual(summary["wave_id"], "deleg_test")
        self.assertEqual(summary["n_samples"], 2)
        self.assertAlmostEqual(summary["peak"]["parent_rss_mb"], 500.0)
        # children peaked on the grown tick
        self.assertAlmostEqual(summary["peak"]["children_rss_mb"], 2800.0)
        self.assertAlmostEqual(
            summary["peak"]["grandchildren_rss_mb"], 700.0)
        self.assertAlmostEqual(summary["peak"]["detached_rss_mb"], 250.0)
        self.assertEqual(summary["peak_counts"]["n_children"], 2)
        self.assertEqual(summary["peak_counts"]["n_grandchildren"], 3)
        self.assertAlmostEqual(summary["per_child_max_mb"], 1400.0)
        # growth signal: 2nd-half child avg above 1st-half
        self.assertGreater(summary["child_rss_growth_mb"], 0)

    def test_report_names_each_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave-deleg_test.jsonl"
            self._write_log(path)
            summary = wmp.summarize(path)
        text = wmp.format_report(summary)
        for token in ("parent RSS peak", "children RSS peak",
                      "grandchildren peak", "payloads", "total proc peak",
                      "largest contributor"):
            self.assertIn(token, text)

    def test_report_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wave-deleg_test.jsonl"
            self._write_log(path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = wmp.main(["report", str(path)])
        self.assertEqual(rc, 0)
        self.assertIn("children RSS peak", buf.getvalue())


class TestDefaultOff(unittest.TestCase):
    def setUp(self):
        self._old = _scrub_env()
        self.addCleanup(lambda: _restore_env(self._old))

    def test_gate_off_by_default(self):
        self.assertFalse(wmp.is_enabled())
        self.assertFalse(wmp.profiling_enabled())

    def test_maybe_start_returns_none_without_side_effects(self):
        before = threading.active_count()
        prof = wmp.maybe_start("deleg_x", "sess-x")
        self.assertIsNone(prof)
        self.assertEqual(threading.active_count(), before)

    def test_overlay_empty_and_notes_noop(self):
        self.assertEqual(wmp.child_env_overlay("deleg_x", "a", 0), {})
        wmp.note_result(None, "deleg_x/0", "anything")  # must not raise
        self.assertIsNone(wmp.stop(None))

    def test_no_log_dir_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MERCURY_HOME"] = tmp
            wmp.maybe_start("deleg_x", "sess-x")
            self.assertFalse((Path(tmp) / "logs" / "wave-mem").exists())


class TestEnabledPath(unittest.TestCase):
    def setUp(self):
        self._old = _scrub_env()
        self.addCleanup(lambda: _restore_env(self._old))

    def test_env_flag_stamps_identity(self):
        os.environ["MERCURY_WAVE_MEM_PROFILE"] = "1"
        self.assertTrue(wmp.is_enabled())
        overlay = wmp.child_env_overlay("deleg_abc", "my-child", 2)
        self.assertEqual(overlay, {
            "MERCURY_WAVE_ID": "deleg_abc",
            "MERCURY_WAVE_ROLE": "child",
            "MERCURY_WAVE_NAME": "my-child",
            "MERCURY_WAVE_INDEX": "2",
        })

    def test_alias_flag_counts(self):
        os.environ["HERMES_WAVE_MEM_PROFILE"] = "yes"
        self.assertTrue(wmp.is_enabled())

    def test_config_fallback_enables(self):
        with patch("tools.delegate_tool._load_config",
                   return_value={"wave_mem_profile": True}):
            self.assertTrue(wmp.profiling_enabled())

    def test_start_stop_lifecycle(self):
        os.environ["MERCURY_WAVE_MEM_PROFILE"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MERCURY_HOME"] = tmp
            prof = wmp.maybe_start("deleg_live", "")
            self.assertIsNotNone(prof)
            prof.note_result("deleg_live/0", "hello" * 200000)  # ~1MB payload
            out = wmp.stop(prof)
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())
            kinds = [r["type"] for r in wmp.iter_records(out)]
            self.assertEqual(kinds[0], "header")
            self.assertEqual(kinds[-1], "footer")
            self.assertIn("result", kinds)
            summary = wmp.summarize(out)
            self.assertGreater(summary["peak"]["payload_mb"], 0)


class TestDispatchWiring(unittest.TestCase):
    """Default-off dispatch creates no profiler artifacts."""

    def setUp(self):
        self._old = _scrub_env()
        self.addCleanup(lambda: _restore_env(self._old))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        # fixture bridge + config, mirroring test_omp_delegation
        bridge = self.tmpdir / "bridge.py"
        bridge.write_text(
            "#!/usr/bin/env python3\nimport sys; "
            "sys.stdout.write('OMP_MODEL=prov/delegate-model\\n"
            "OMP_FALLBACK_CHAIN=prov/delegate-fallback\\n'); sys.exit(0)\n")
        bridge.chmod(0o755)
        cfg = self.tmpdir / "config.yaml"
        cfg.write_text(
            'models:\n  default: "prov/main-model"\n'
            '  fallback: "prov/main-fallback"\n'
            '  delegate_default: "prov/delegate-model"\n'
            '  delegate_fallback: "prov/delegate-fallback"\n')
        omp = self.tmpdir / "omp"
        omp.write_text("#!/bin/sh\necho 'fake-omp-ran'\n")
        omp.chmod(0o755)
        self._env_old = {k: os.environ.get(k) for k in (
            "HERMES_OMP_BRIDGE", "HERMES_OMP_CONFIG", "HERMES_OMP_BIN",
            "HERMES_OMP_TRANSPORT")}
        os.environ["HERMES_OMP_BRIDGE"] = str(bridge)
        os.environ["HERMES_OMP_CONFIG"] = str(cfg)
        os.environ["HERMES_OMP_BIN"] = str(omp)
        os.environ["HERMES_OMP_TRANSPORT"] = "oneshot"
        os.environ["MERCURY_HOME"] = str(self.tmpdir / "mh")
        self.addCleanup(self._restore_env2)
        import tools.omp_delegation as mod
        mod._env_cache.update(mtime=None, env=None, err=None)
        mod._rendered.clear()
        self.addCleanup(
            lambda: mod._env_cache.update(mtime=None, env=None, err=None))
        self.addCleanup(mod._rendered.clear)

    def _restore_env2(self):
        for k, v in self._env_old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_sync_dispatch_off_creates_no_log(self):
        import tools.omp_delegation as mod
        from unittest import mock
        parent = MagicMock()
        parent._delegate_depth = 1
        parent.session_id = "sess-prof-test"
        with mock.patch.object(mod, "_render_omp_config_once"):
            out = mod.dispatch_omp_delegation(parent, {"goal": "hi"})
        payload = json.loads(out)
        self.assertEqual(payload["results"][0]["summary"], "fake-omp-ran")
        self.assertFalse(
            (self.tmpdir / "mh" / "logs" / "wave-mem").exists())

    def test_sync_dispatch_on_stamps_child_env(self):
        os.environ["MERCURY_WAVE_MEM_PROFILE"] = "1"
        os.environ["MERCURY_WAVE_MEM_PROFILE_INTERVAL"] = "1000"
        import tools.omp_delegation as mod
        from unittest import mock
        parent = MagicMock()
        parent._delegate_depth = 1
        parent.session_id = "sess-prof-test"
        seen = {}
        real_popen = mod.subprocess.Popen

        class SpyPopen(real_popen):
            def __init__(self, *a, **k):
                seen.update((k.get("env") or {}))
                super().__init__(*a, **k)

        with mock.patch.object(mod, "_render_omp_config_once"), \
                mock.patch.object(mod.subprocess, "Popen", SpyPopen):
            out = mod.dispatch_omp_delegation(parent, {"goal": "hi"})
        payload = json.loads(out)
        self.assertEqual(payload["results"][0]["summary"], "fake-omp-ran")
        self.assertEqual(seen.get("MERCURY_WAVE_ROLE"), "child")
        self.assertTrue(seen.get("MERCURY_WAVE_ID", "").startswith("deleg_"))
        logs = list((self.tmpdir / "mh" / "logs" / "wave-mem").glob("wave-*.jsonl"))
        self.assertEqual(len(logs), 1)
        summary = wmp.summarize(logs[0])
        self.assertGreaterEqual(summary["n_samples"], 0)


if __name__ == "__main__":
    unittest.main()
