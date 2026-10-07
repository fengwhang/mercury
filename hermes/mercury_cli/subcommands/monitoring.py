"""``mercury monitoring`` subcommand parser.

Gateway monitoring inspection. ``status`` reads gateway and cron health from
local runtime state without sending reports.

The handler is injected to avoid importing ``main`` (mirrors the insights
subcommand).
"""

from __future__ import annotations

from typing import Callable


def build_monitoring_parser(subparsers, *, cmd_monitoring: Callable) -> None:
    """Attach the ``monitoring`` subcommand (with actions) to ``subparsers``."""
    p = subparsers.add_parser(
        "monitoring",
        help="Inspect local gateway and cron health",
        description=(
            "Gateway monitoring: local service health metrics and diagnostics. "
            "No remote telemetry or reporting."
        ),
    )
    sub = p.add_subparsers(dest="monitoring_action")

    sub.add_parser(
        "status",
        help="Show local gateway, platform, and cron health",
    )

    p.set_defaults(func=cmd_monitoring)
