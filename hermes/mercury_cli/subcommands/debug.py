"""Parser for local Mercury diagnostics."""

from __future__ import annotations

from typing import Callable


def build_debug_parser(subparsers, *, cmd_debug: Callable) -> None:
    """Attach the local ``debug report`` command."""
    debug_parser = subparsers.add_parser(
        "debug",
        help="Generate local diagnostics",
        description="Generate a redacted local report of system info and recent logs.",
    )
    debug_sub = debug_parser.add_subparsers(dest="debug_command")
    report_parser = debug_sub.add_parser("report", help="Print a local debug report")
    report_parser.add_argument(
        "--lines", type=int, default=200,
        help="Number of recent log lines per file (default: 200)",
    )
    report_parser.add_argument(
        "--output", metavar="PATH",
        help="Write the report to a local file instead of stdout",
    )
    debug_parser.set_defaults(func=cmd_debug)
