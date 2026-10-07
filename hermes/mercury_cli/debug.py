"""Local Mercury diagnostics: report to stdout or a local file."""

import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mercury_constants import get_hermes_home

_MAX_LOG_BYTES = 512_000
_EMAIL_ADDRESS_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9._%+-])"
)


# ---------------------------------------------------------------------------
# Log file reading
# ---------------------------------------------------------------------------


@dataclass
class LogSnapshot:
    """Single-read snapshot of a log file used by local diagnostics."""

    path: Optional[Path]
    tail_text: str
    full_text: Optional[str]


def _primary_log_path(log_name: str) -> Optional[Path]:
    """Where *log_name* would live if present. Doesn't check existence."""
    from mercury_cli.logs import LOG_FILES

    filename = LOG_FILES.get(log_name)
    return (get_hermes_home() / "logs" / filename) if filename else None


# Logs written by a client process rather than by this backend. When the
# desktop app talks to a remote/docker/SSH backend, `mercury debug report` runs
# on the *backend* and can never see them — a bare "(file not found)" then
# reads as "the app logged nothing" and sends triage down a dead end, which is
# exactly the wrong answer when the client is the thing being debugged.
_CLIENT_SIDE_LOGS = {
    "desktop": (
        "written by Mercury Desktop on the machine running the app, not by this "
        "backend. If the desktop connects to a remote/docker/SSH backend, collect "
        "it on that client machine"
    ),
}


def _missing_log_note(log_name: str) -> str:
    """Explain a missing log instead of stating a bare absence.

    For a client-side log the absence is expected on a remote backend, so the
    note names the writer and the path to collect by hand.
    """
    reason = _CLIENT_SIDE_LOGS.get(log_name)
    if reason is None:
        return "(file not found)"

    primary = _primary_log_path(log_name)
    where = f" — expected at {primary}" if primary else ""
    return f"(not on this host: {reason}{where})"


def _resolve_log_path(log_name: str) -> Optional[Path]:
    """Find the log file for *log_name*, falling back to the .1 rotation.

    Returns the first non-empty candidate (primary, then .1), or None.
    Callers distinguish 'empty primary' from 'truly missing' via
    :func:`_primary_log_path`.
    """
    primary = _primary_log_path(log_name)
    if primary is None:
        return None

    if primary.exists() and primary.stat().st_size > 0:
        return primary

    rotated = primary.parent / f"{primary.name}.1"
    if rotated.exists() and rotated.stat().st_size > 0:
        return rotated

    return None


def _redact_log_text(text: str) -> str:
    """Run ``redact_sensitive_text`` with ``force=True`` over locally exported text.

    Uses ``force=True`` so redaction fires regardless of the operator's
    ``security.redact_secrets`` setting. The local on-disk log file is
    not modified; only the in-memory copy used in the local report is sanitized. Returns the redacted text (or the original
    when empty / non-string).
    """
    if not text:
        return text
    from agent.redact import redact_sensitive_text

    text = redact_sensitive_text(text, force=True)
    return _EMAIL_ADDRESS_RE.sub("[REDACTED_EMAIL]", text)


def _capture_log_snapshot(
    log_name: str,
    *,
    tail_lines: int,
    max_bytes: int = _MAX_LOG_BYTES,
    redact: bool = True,
) -> LogSnapshot:
    """Capture a log once and derive summary/full-log views from it.

    The report tail and exported full log must come from the same file
    snapshot. Otherwise a rotation/truncate between reads can make the report
    look newer than the exported ``agent.log``.

    When ``redact`` is True (the default), both ``tail_text`` and
    ``full_text`` are run through ``_redact_log_text`` so the snapshot
    returned is safe for local export. The on-disk log file is never modified.
    Internal callers can request original log content with ``redact=False``.
    """
    log_path = _resolve_log_path(log_name)
    if log_path is None:
        primary = _primary_log_path(log_name)
        tail = (
            "(file empty)"
            if primary and primary.exists()
            else _missing_log_note(log_name)
        )
        return LogSnapshot(path=None, tail_text=tail, full_text=None)

    try:
        size = log_path.stat().st_size
        if size == 0:
            # race: file was truncated between _resolve_log_path and stat
            return LogSnapshot(path=log_path, tail_text="(file empty)", full_text=None)

        with open(log_path, "rb") as f:
            if size <= max_bytes:
                raw = f.read()
                truncated = False
            else:
                # Read from the end until we have enough bytes for the
                # full log export and enough newline context to render the
                # summary tail from the same snapshot.
                chunk_size = 8192
                pos = size
                chunks: list[bytes] = []
                total = 0
                newline_count = 0

                while pos > 0 and (total < max_bytes or newline_count <= tail_lines + 1) and total < max_bytes * 2:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    chunks.insert(0, chunk)
                    total += len(chunk)
                    newline_count += chunk.count(b"\n")
                    chunk_size = min(chunk_size * 2, 65536)

                raw = b"".join(chunks)
                truncated = pos > 0

        full_raw = raw
        if truncated and len(full_raw) > max_bytes:
            cut = len(full_raw) - max_bytes
            # Check whether the cut lands exactly on a line boundary.  If the
            # byte just before the cut position is a newline the first retained
            # byte starts a complete line and we should keep it.  Only drop a
            # partial first line when we're genuinely mid-line.
            on_boundary = cut > 0 and full_raw[cut - 1 : cut] == b"\n"
            full_raw = full_raw[cut:]
            if not on_boundary and b"\n" in full_raw:
                full_raw = full_raw.split(b"\n", 1)[1]

        all_text = raw.decode("utf-8", errors="replace")
        tail_text = "".join(all_text.splitlines(keepends=True)[-tail_lines:]).rstrip("\n")

        full_text = full_raw.decode("utf-8", errors="replace")
        if truncated:
            full_text = f"[... truncated — showing last ~{max_bytes // 1024}KB ...]\n{full_text}"

        if redact:
            tail_text = _redact_log_text(tail_text)
            full_text = _redact_log_text(full_text)

        return LogSnapshot(path=log_path, tail_text=tail_text, full_text=full_text)
    except Exception as exc:
        return LogSnapshot(path=log_path, tail_text=f"(error reading: {exc})", full_text=None)


def _capture_default_log_snapshots(
    log_lines: int, *, redact: bool = True
) -> dict[str, LogSnapshot]:
    """Capture all logs used by local diagnostics exactly once.

    ``redact`` is forwarded to each ``_capture_log_snapshot`` call so all
    captured logs share the same redaction policy for a given run.
    """
    errors_lines = min(log_lines, 100)
    return {
        "agent": _capture_log_snapshot(
            "agent", tail_lines=log_lines, redact=redact
        ),
        "errors": _capture_log_snapshot(
            "errors", tail_lines=errors_lines, redact=redact
        ),
        "gateway": _capture_log_snapshot(
            "gateway", tail_lines=errors_lines, redact=redact
        ),
        "gui": _capture_log_snapshot(
            "gui", tail_lines=errors_lines, redact=redact
        ),
        "desktop": _capture_log_snapshot(
            "desktop", tail_lines=errors_lines, redact=redact
        ),
    }


# ---------------------------------------------------------------------------
# Debug report collection
# ---------------------------------------------------------------------------

def _capture_dump() -> str:
    """Run ``mercury dump`` and return its stdout as a string."""
    from mercury_cli.dump import run_dump

    class _FakeArgs:
        show_keys = False

    old_stdout = sys.stdout
    sys.stdout = capture = io.StringIO()
    try:
        run_dump(_FakeArgs())
    except SystemExit:
        pass
    finally:
        sys.stdout = old_stdout

    return capture.getvalue()


def collect_debug_report(
    *,
    log_lines: int = 200,
    dump_text: str = "",
    log_snapshots: Optional[dict[str, LogSnapshot]] = None,
) -> str:
    """Build the summary debug report: system dump + log tails.

    Parameters
    ----------
    log_lines
        Number of recent lines to include per log file.
    dump_text
        Pre-captured dump output.  If empty, ``mercury dump`` is run
        internally.

    Returns the report as a plain-text string for local display or export.
    """
    buf = io.StringIO()

    if not dump_text:
        dump_text = _capture_dump()
    buf.write(dump_text)

    if log_snapshots is None:
        log_snapshots = _capture_default_log_snapshots(log_lines)

    # ── Recent log tails (summary only) ──────────────────────────────────
    buf.write("\n\n")
    buf.write(f"--- agent.log (last {log_lines} lines) ---\n")
    buf.write(log_snapshots["agent"].tail_text)
    buf.write("\n\n")

    errors_lines = min(log_lines, 100)
    buf.write(f"--- errors.log (last {errors_lines} lines) ---\n")
    buf.write(log_snapshots["errors"].tail_text)
    buf.write("\n\n")

    buf.write(f"--- gateway.log (last {errors_lines} lines) ---\n")
    buf.write(log_snapshots["gateway"].tail_text)
    buf.write("\n\n")

    buf.write(f"--- gui.log (last {errors_lines} lines) ---\n")
    buf.write(log_snapshots["gui"].tail_text)
    buf.write("\n\n")

    buf.write(f"--- desktop.log (last {errors_lines} lines) ---\n")
    buf.write(log_snapshots["desktop"].tail_text)
    buf.write("\n")

    return buf.getvalue()


def run_debug_report(args):
    """Generate a redacted report without contacting a reporting service."""
    report = collect_debug_report(log_lines=args.lines)
    if args.output:
        Path(args.output).expanduser().write_text(report, encoding="utf-8")
        print(f"Debug report saved locally: {args.output}")
    else:
        print(report, end="" if report.endswith("\n") else "\n")


def run_debug(args):
    """Route local diagnostic commands."""
    if getattr(args, "debug_command", None) == "report":
        run_debug_report(args)
    else:
        print("Usage: mercury debug report [--lines N] [--output PATH]")
