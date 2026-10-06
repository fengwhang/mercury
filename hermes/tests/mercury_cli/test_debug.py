"""Tests for ``mercury debug`` CLI command and debug utilities."""

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mercury_home(tmp_path, monkeypatch):
    """Set up an isolated HERMES_HOME with minimal logs."""
    home = tmp_path / ".mercury"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Create log files
    logs_dir = home / "logs"
    logs_dir.mkdir()
    (logs_dir / "agent.log").write_text(
        "2026-04-12 17:00:00 INFO agent: session started\n"
        "2026-04-12 17:00:01 INFO tools.terminal: running ls\n"
        "2026-04-12 17:00:02 WARNING agent: high token usage\n"
    )
    (logs_dir / "errors.log").write_text(
        "2026-04-12 17:00:05 ERROR gateway.run: connection lost\n"
    )
    (logs_dir / "gateway.log").write_text(
        "2026-04-12 17:00:10 INFO gateway.run: started\n"
    )
    (logs_dir / "gui.log").write_text(
        "2026-04-12 17:00:12 INFO mercury_cli.web_server: dashboard request\n"
    )
    (logs_dir / "desktop.log").write_text(
        "2026-04-12 17:00:15 INFO desktop: backend spawned\n"
    )

    return home

def test_local_report_command_replaces_remote_share(mercury_home, capsys):
    import argparse
    from mercury_cli.debug import run_debug
    from mercury_cli.subcommands.debug import build_debug_parser

    parser = argparse.ArgumentParser()
    build_debug_parser(parser.add_subparsers(), cmd_debug=run_debug)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["debug", "share", "--yes"])
    assert exc.value.code == 2
    args = parser.parse_args(["debug", "report", "--lines", "2"])
    with patch("mercury_cli.dump.run_dump", side_effect=lambda a: print("local dump")):
        args.func(args)
    report = capsys.readouterr().out
    assert "local dump" in report
    assert "high token usage" in report
    assert "session started" not in report


# ---------------------------------------------------------------------------
# Log reading
# ---------------------------------------------------------------------------

class TestCaptureLogSnapshot:
    """Test _capture_log_snapshot for log reading and truncation."""




    def test_race_truncate_after_resolve_reports_empty(self, mercury_home, monkeypatch):
        """If the log is truncated between resolve and stat, say 'empty', not 'missing'."""
        log_path = mercury_home / "logs" / "agent.log"
        from mercury_cli import debug

        monkeypatch.setattr(debug, "_resolve_log_path", lambda _name: log_path)
        log_path.write_text("")

        snap = debug._capture_log_snapshot("agent", tail_lines=10)
        assert snap.path == log_path
        assert snap.full_text is None
        assert snap.tail_text == "(file empty)"


    def test_keeps_first_line_when_truncation_on_boundary(self, mercury_home):
        """When truncation lands on a line boundary, keep the first full line."""
        from mercury_cli.debug import _capture_log_snapshot

        # File must exceed the initial chunk_size (8192) used by the
        # backward-reading loop so the truncation path actually fires.
        line = "A" * 99 + "\n"  # 100 bytes per line
        num_lines = 200  # 20000 bytes
        (mercury_home / "logs" / "agent.log").write_text(line * num_lines)

        # max_bytes = 1000 = 100 * 10 → cut at byte 20000 - 1000 = 19000,
        # and byte 19000 - 1 is '\n'.  Boundary hit → keep all 10 lines.
        snap = _capture_log_snapshot("agent", tail_lines=5, max_bytes=1000)
        assert snap.full_text is not None
        assert "truncated" in snap.full_text
        raw = snap.full_text.split("\n", 1)[1]
        kept = [l for l in raw.strip().splitlines() if l.startswith("A")]
        assert len(kept) == 10


class TestMissingLogNote:
    """A missing log explains itself when the writer isn't this backend.

    `mercury debug report` runs on the backend, so a desktop connected to a
    remote/docker/SSH backend can never contribute desktop.log. Reporting a
    bare absence sends triage after a client-side bug it cannot see.
    """

    def test_backend_written_log_reports_plain_absence(self, mercury_home):
        from mercury_cli.debug import _capture_log_snapshot

        (mercury_home / "logs" / "agent.log").unlink()

        snap = _capture_log_snapshot("agent", tail_lines=10)
        assert snap.full_text is None
        assert snap.tail_text == "(file not found)"

    def test_client_written_log_names_its_writer_and_path(self, mercury_home):
        from mercury_cli.debug import _capture_log_snapshot

        (mercury_home / "logs" / "desktop.log").unlink()

        snap = _capture_log_snapshot("desktop", tail_lines=10)
        assert snap.full_text is None
        assert "not on this host" in snap.tail_text
        assert "Mercury Desktop" in snap.tail_text
        # The reader needs the path to collect by hand on the client machine.
        assert str(mercury_home / "logs" / "desktop.log") in snap.tail_text

    def test_present_client_log_is_captured_normally(self, mercury_home):
        """A local backend still reads desktop.log — the note is only for a miss."""
        from mercury_cli.debug import _capture_log_snapshot

        snap = _capture_log_snapshot("desktop", tail_lines=10)
        assert "backend spawned" in snap.tail_text
        assert "not on this host" not in snap.tail_text

    def test_empty_client_log_is_empty_not_absent(self, mercury_home):
        """An empty file means the app ran and logged nothing — a different fact."""
        from mercury_cli.debug import _capture_log_snapshot

        (mercury_home / "logs" / "desktop.log").write_text("")

        snap = _capture_log_snapshot("desktop", tail_lines=10)
        assert snap.tail_text == "(file empty)"

    def test_report_carries_the_note_for_a_remote_backend(self, mercury_home):
        """The local report — must explain it."""
        from mercury_cli.debug import collect_debug_report

        (mercury_home / "logs" / "desktop.log").unlink()

        report = collect_debug_report(log_lines=10, dump_text="dump\n")
        assert "--- desktop.log" in report
        assert "not on this host" in report




# ---------------------------------------------------------------------------
# Capture log redaction (force=True applies regardless of HERMES_REDACT_SECRETS)
# ---------------------------------------------------------------------------

# A vendor-prefixed token used across redaction tests. Long enough to clear
# the redactor's `floor` parameter so it actually masks rather than fully blanks.
_REDACT_FIXTURE_TOKEN = "sk-proj-A1B2C3D4E5F6G7H8I9J0aA"


class TestCaptureLogSnapshotRedaction:
    """Pin local-export redaction at the _capture_log_snapshot boundary."""

    @pytest.fixture
    def mercury_home_with_secret(self, tmp_path, monkeypatch):
        """Isolated HERMES_HOME whose agent.log contains a vendor-prefixed token."""
        home = tmp_path / ".mercury"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        # Baseline fixture: no explicit env-var opinion. With the post-#17691
        # default of ON, the default-path tests below exercise the
        # secure-default behaviour. The `force=True` regression test
        # setenvs to "false" inline to prove force=True works even when
        # the runtime flag is disabled.
        monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)

        logs_dir = home / "logs"
        logs_dir.mkdir()
        (logs_dir / "agent.log").write_text(
            f"2026-04-12 17:00:00 INFO config: api_key={_REDACT_FIXTURE_TOKEN} loaded\n"
        )
        (logs_dir / "errors.log").write_text("")
        (logs_dir / "gateway.log").write_text("")
        return home

    def test_default_redacts_tail_and_full_text(self, mercury_home_with_secret):
        from mercury_cli.debug import _capture_log_snapshot

        snap = _capture_log_snapshot("agent", tail_lines=10)

        # Both views the local export uses must be sanitized.
        assert _REDACT_FIXTURE_TOKEN not in snap.tail_text
        assert snap.full_text is not None
        assert _REDACT_FIXTURE_TOKEN not in snap.full_text

    def test_redact_false_passes_through(self, mercury_home_with_secret):
        from mercury_cli.debug import _capture_log_snapshot

        snap = _capture_log_snapshot("agent", tail_lines=10, redact=False)

        # Original token survives when the caller opts out.
        assert _REDACT_FIXTURE_TOKEN in snap.tail_text
        assert _REDACT_FIXTURE_TOKEN in (snap.full_text or "")

    def test_force_true_works_when_redaction_disabled(
        self, mercury_home_with_secret, monkeypatch
    ):
        """Regression test: redact_sensitive_text short-circuits without force=True.

        If a future refactor drops `force=True` from `_redact_log_text`, this
        test fails immediately. Without `force=True`, the redactor returns the
        input unchanged when HERMES_REDACT_SECRETS=false, and the export-time
        redaction feature ships silently broken for users who opted out of
        runtime redaction (e.g. developers working on the redactor itself).
        """

        # Force the runtime flag off so we're exercising the force=True path,
        # not the default-on path.
        monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")

        from mercury_cli.debug import _capture_log_snapshot

        assert os.environ.get("HERMES_REDACT_SECRETS", "") == "false"

        snap = _capture_log_snapshot("agent", tail_lines=10)

        assert _REDACT_FIXTURE_TOKEN not in snap.tail_text
        assert snap.full_text is not None
        assert _REDACT_FIXTURE_TOKEN not in snap.full_text

    def test_default_redacts_email_addresses_for_local_export(
        self, mercury_home_with_secret
    ):
        from mercury_cli.debug import _capture_log_snapshot

        log_path = mercury_home_with_secret / "logs" / "agent.log"
        log_path.write_text(
            "2026-04-12 17:00:00 INFO gateway.run: "
            "inbound message: platform=bluebubbles "
            "user=person@example.com chat=iMessage;-;person@example.com msg='hello'\n"
        )

        snap = _capture_log_snapshot("agent", tail_lines=10)

        assert "person@example.com" not in snap.tail_text
        assert "[REDACTED_EMAIL]" in snap.tail_text
        assert snap.full_text is not None
        assert "person@example.com" not in snap.full_text

    def test_no_redact_preserves_email_addresses(self, mercury_home_with_secret):
        from mercury_cli.debug import _capture_log_snapshot

        log_path = mercury_home_with_secret / "logs" / "agent.log"
        log_path.write_text(
            "2026-04-12 17:00:00 INFO gateway.run: "
            "inbound message: platform=bluebubbles "
            "user=person@example.com chat=iMessage;-;person@example.com msg='hello'\n"
        )

        snap = _capture_log_snapshot("agent", tail_lines=10, redact=False)

        assert "person@example.com" in snap.tail_text
        assert "person@example.com" in (snap.full_text or "")

    def test_capture_default_log_snapshots_threads_redact(
        self, mercury_home_with_secret
    ):
        from mercury_cli.debug import _capture_default_log_snapshots

        snaps = _capture_default_log_snapshots(50)

        # Default threads redact=True to all three captured logs.
        assert _REDACT_FIXTURE_TOKEN not in snaps["agent"].tail_text
        assert _REDACT_FIXTURE_TOKEN not in (snaps["agent"].full_text or "")

    def test_capture_default_log_snapshots_no_redact_passes_through(
        self, mercury_home_with_secret
    ):
        from mercury_cli.debug import _capture_default_log_snapshots

        snaps = _capture_default_log_snapshots(50, redact=False)

        assert _REDACT_FIXTURE_TOKEN in snaps["agent"].tail_text
        assert _REDACT_FIXTURE_TOKEN in (snaps["agent"].full_text or "")


# ---------------------------------------------------------------------------
# Debug report collection
# ---------------------------------------------------------------------------

class TestCollectDebugReport:
    """Test the debug report builder."""

    def test_report_includes_dump_output(self, mercury_home):
        from mercury_cli.debug import collect_debug_report

        with patch("mercury_cli.dump.run_dump") as mock_dump:
            mock_dump.side_effect = lambda args: print(
                "--- mercury dump ---\nversion: 0.8.0\n--- end dump ---"
            )
            report = collect_debug_report(log_lines=50)

        assert "--- mercury dump ---" in report
        assert "version: 0.8.0" in report




def test_report_exports_redacted_local_file(mercury_home, capsys):
    from types import SimpleNamespace
    from mercury_cli.debug import run_debug_report
    output = mercury_home / "report.txt"
    token = _REDACT_FIXTURE_TOKEN
    (mercury_home / "logs" / "agent.log").write_text(f"api_key={token}\n")
    with patch("mercury_cli.dump.run_dump", side_effect=lambda a: print("local dump")):
        run_debug_report(SimpleNamespace(lines=10, output=str(output)))
    assert "local dump" in output.read_text()
    assert token not in output.read_text()
    assert "saved locally" in capsys.readouterr().out
    assert token in (mercury_home / "logs" / "agent.log").read_text()


def test_cli_slash_debug_is_local(mercury_home, capsys):
    from mercury_cli.cli_commands_mixin import CLICommandsMixin
    with patch("mercury_cli.dump.run_dump", side_effect=lambda a: print("local dump")):
        CLICommandsMixin._handle_debug_command(object(), "/debug")
    assert "local dump" in capsys.readouterr().out


def test_cli_slash_debug_rejects_removed_destination(capsys):
    from mercury_cli.cli_commands_mixin import CLICommandsMixin
    CLICommandsMixin._handle_debug_command(object(), "/debug nous")
    assert "Usage: /debug" in capsys.readouterr().out
