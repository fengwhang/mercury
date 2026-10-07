"""Tests for stuck-session loop detection (#7536).

When a session is active across 3+ consecutive gateway restarts (the agent
gets stuck, gateway restarts, same session gets stuck again), the session
is auto-suspended on startup so the user gets a clean slate.
"""

import json
from unittest.mock import MagicMock

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def runner_with_home(tmp_path, monkeypatch):
    """Create a runner with a writable HERMES_HOME."""
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    return runner, tmp_path


class TestStuckLoopDetection:

    def test_increment_creates_file(self, runner_with_home):
        runner, home = runner_with_home
        runner._increment_restart_failure_counts({"session:a", "session:b"})
        path = home / runner._STUCK_LOOP_FILE
        assert path.exists()
        counts = json.loads(path.read_text())
        assert counts["session:a"] == 1
        assert counts["session:b"] == 1


    def test_suspend_at_threshold(self, runner_with_home):
        runner, home = runner_with_home
        # Simulate 3 restarts with session:a active each time
        for _ in range(3):
            runner._increment_restart_failure_counts({"session:a"})

        # Create a mock session entry
        mock_entry = MagicMock()
        mock_entry.suspended = False
        runner.session_store._entries = {"session:a": mock_entry}
        runner.session_store._save = MagicMock()

        suspended = runner._suspend_stuck_loop_sessions()
        assert suspended == 1
        assert mock_entry.suspended is True

    def test_no_suspend_below_threshold(self, runner_with_home):
        runner, home = runner_with_home
        runner._increment_restart_failure_counts({"session:a"})
        runner._increment_restart_failure_counts({"session:a"})
        # Only 2 restarts — below threshold of 3

        mock_entry = MagicMock()
        mock_entry.suspended = False
        runner.session_store._entries = {"session:a": mock_entry}

        suspended = runner._suspend_stuck_loop_sessions()
        assert suspended == 0
        assert mock_entry.suspended is False


class TestPlannedRestartsDoNotCount:
    """A planned restart is the operator's/updater's doing, not the session's.

    Regression cover for the bug where three unrelated planned gateway
    restarts auto-suspended a healthy session and force-wiped its session_id,
    orphaning an in-flight subagent's transcript so the work could not be
    resumed.
    """

    def test_planned_restart_does_not_increment(self, runner_with_home):
        runner, home = runner_with_home
        runner._restart_requested = True
        runner._increment_restart_failure_counts({"session:a"})
        path = home / runner._STUCK_LOOP_FILE
        assert not path.exists(), "planned restart must not record a failure count"

    def test_repeated_planned_restarts_never_suspend(self, runner_with_home):
        """The reported bug: N planned restarts with an active session must
        leave the session resumable, not auto-suspended."""
        runner, home = runner_with_home
        mock_entry = MagicMock()
        mock_entry.suspended = False
        runner.session_store._entries = {"session:a": mock_entry}
        runner.session_store._save = MagicMock()

        for _ in range(5):
            runner._restart_requested = True
            runner._increment_restart_failure_counts({"session:a"})

        assert runner._suspend_stuck_loop_sessions() == 0
        assert mock_entry.suspended is False

    def test_unplanned_shutdown_still_counts_and_suspends(self, runner_with_home):
        """Genuine stuck loops must still be broken at the threshold."""
        runner, home = runner_with_home
        runner._restart_requested = False
        for _ in range(3):
            runner._increment_restart_failure_counts({"session:a"})

        mock_entry = MagicMock()
        mock_entry.suspended = False
        runner.session_store._entries = {"session:a": mock_entry}
        runner.session_store._save = MagicMock()

        assert runner._suspend_stuck_loop_sessions() == 1
        assert mock_entry.suspended is True

    def test_mixed_restart_kinds_count_only_unplanned(self, runner_with_home):
        """Only session-caused shutdowns accumulate toward the threshold."""
        runner, home = runner_with_home
        for planned in (True, False, True, False, True):
            runner._restart_requested = planned
            runner._increment_restart_failure_counts({"session:a"})

        counts = json.loads((home / runner._STUCK_LOOP_FILE).read_text())
        assert counts["session:a"] == 2

    def test_false_by_default_when_flag_absent(self, runner_with_home):
        """Runners built without the attribute must still record failures
        (fail-safe toward the original stuck-loop protection)."""
        runner, home = runner_with_home
        if hasattr(runner, "_restart_requested"):
            delattr(runner, "_restart_requested")
        runner._increment_restart_failure_counts({"session:a"})
        counts = json.loads((home / runner._STUCK_LOOP_FILE).read_text())
        assert counts["session:a"] == 1


