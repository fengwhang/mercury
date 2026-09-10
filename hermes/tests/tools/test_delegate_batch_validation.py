#!/usr/bin/env python3
"""Batch input validation for delegate_task(tasks=[...]).

Guards against the model wasting a whole fan-out on malformed batches:
exact-duplicate goals, placeholder goals ('TODO', 'task N', unexpanded
template markers, too-short), and 1-task batches that should have used
the single `goal` form.

All checks are BATCH-ONLY — the single-goal form is deliberately exempt
(short goals like goal="test" are valid there and widely used).

Inspired by: MoonshotAI/kimi-code agent-swarm.md validation rules (MIT)
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task


def _make_mock_parent(depth=0):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _call(tasks):
    return json.loads(delegate_task(tasks=tasks, parent_agent=_make_mock_parent()))


def _omp_results(*summaries):
    """Patch the omp dispatch to return a canned completed-results payload.

    Validation must pass through to the engine; the engine itself is stubbed
    (omp reality: delegate_task validates, omp executes)."""
    import tools.omp_delegation as omp

    entries = [
        {
            "task_index": i,
            "status": "completed",
            "summary": summary,
            "api_calls": 1,
            "duration_seconds": 1.0,
        }
        for i, summary in enumerate(summaries)
    ]
    payload = json.dumps({"results": entries, "total_duration_seconds": 2.0})
    return patch.object(omp, "dispatch_omp_delegation", return_value=payload)


GOOD_A = "Refactor the login handler to use the new session helper"
GOOD_B = "Write regression tests for the session expiry watcher"


class TestBatchDuplicateGoalsAllowed(unittest.TestCase):
    """Identical-goal fan-outs are legitimate (best-of-N / ensemble sampling).

    The original gate from #81141 rejected duplicates; the post-merge audit
    downgraded that -- duplicates must pass validation.
    """

    def test_exact_duplicate_goals_accepted(self):
        with _omp_results("ok", "ok"):
            result = _call([{"goal": GOOD_A}, {"goal": GOOD_A}])
        self.assertNotIn("error", result)
        self.assertEqual(len(result["results"]), 2)

    def test_case_whitespace_variant_duplicates_accepted(self):
        with _omp_results("ok", "ok"):
            result = _call([{"goal": GOOD_A}, {"goal": "  " + GOOD_A.upper() + "  "}])
        self.assertNotIn("error", result)
        self.assertEqual(len(result["results"]), 2)


class TestBatchPlaceholderGoals(unittest.TestCase):
    def test_bare_todo_rejected_case_insensitive(self):
        for todo in ("TODO", "todo", "ToDo"):
            result = _call([{"goal": GOOD_A}, {"goal": todo}])
            self.assertIn("error", result, todo)

    def test_task_n_placeholder_rejected(self):
        # 'Task 123456789' is >10 chars, so only the bare-'task N' shape
        # check can reject it — proves the pattern check exists.
        result = _call([{"goal": GOOD_A}, {"goal": "Task 123456789"}])
        self.assertIn("error", result)
        self.assertIn("placeholder", result["error"].lower())

    def test_unexpanded_angle_template_marker_rejected(self):
        result = _call([{"goal": GOOD_A}, {"goal": "Implement <feature_name> end to end"}])
        self.assertIn("error", result)
        self.assertIn("template", result["error"].lower())

    def test_unexpanded_brace_template_marker_rejected(self):
        result = _call([{"goal": GOOD_A}, {"goal": "Summarize {file_path} for the report"}])
        self.assertIn("error", result)
        self.assertIn("template", result["error"].lower())

    def test_code_shaped_brackets_not_rejected(self):
        """Generics, HTML tags, JSON snippets, glob braces, and f-string-style
        single-word placeholders are legitimate goal content — the narrow
        marker regex (post-merge audit of #81141) must not fire on them."""
        code_goals = [
            "Refactor the parser to return Vec<T> instead of raw pointers",
            "Fix the Result<String> error propagation in the config loader",
            "Render the sidebar inside a <div> wrapper with flex layout",
            'Update the fixture to emit {"key": 1} for the happy path',
            "Add a glob rule matching src/{a,b}/*.py to the lint config",
            "Rewrite the loop so {i} interpolates via f-strings correctly",
        ]
        for bad_free_goal in code_goals:
            with _omp_results("ok", "ok"):
                result = _call([{"goal": GOOD_A}, {"goal": bad_free_goal}])
            self.assertNotIn("error", result, bad_free_goal)

    def test_multiword_placeholder_shapes_still_rejected(self):
        for marker_goal in (
            "Deploy the service to <target environment> when ready",
            "Backfill rows for {customer id} in the billing table",
            "Ship <FEATURE-NAME> behind the beta flag",
        ):
            result = _call([{"goal": GOOD_A}, {"goal": marker_goal}])
            self.assertIn("error", result, marker_goal)
            self.assertIn("template", result["error"].lower())

    def test_too_short_goal_rejected(self):
        result = _call([{"goal": GOOD_A}, {"goal": "fix bug"}])
        self.assertIn("error", result)

    def test_placeholder_error_is_actionable(self):
        result = _call([{"goal": GOOD_A}, {"goal": "TODO"}])
        self.assertIn("error", result)
        # Error must tell the model HOW to fix the call.
        self.assertIn("specific", result["error"].lower())


class TestSingleTaskBatch(unittest.TestCase):
    def test_one_task_batch_is_valid_single_task_shape(self):
        """A one-entry tasks[] array is the canonical single-task call (the
        advertised interface is tasks-only), so it must NOT be rejected —
        and short goals are legitimate for a single task."""
        with _omp_results("done"):
            result = _call([{"goal": GOOD_A}])
        self.assertNotIn("error", result)


class TestValidBatchStillRuns(unittest.TestCase):
    def test_two_distinct_goals_pass_validation(self):
        with _omp_results("A done", "B done"):
            result = _call([{"goal": GOOD_A}, {"goal": GOOD_B}])
        self.assertNotIn("error", result)
        self.assertEqual(len(result["results"]), 2)

    def test_single_goal_form_unaffected_by_batch_checks(self):
        # goal="test" is short — must NOT trip the batch-only length check.
        parent = _make_mock_parent()
        with _omp_results("ok"):
            result = json.loads(delegate_task(goal="test", parent_agent=parent))
        self.assertNotIn("error", result)


if __name__ == "__main__":
    unittest.main()
