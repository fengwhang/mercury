"""``mercury omp-sync-skills`` subcommand parser.

Extracted from the god-file decomposition pattern (see subcommands/__init__).
Handler injected to avoid importing ``main``; the reconcile logic itself
lives in ``tools/omp_skills_bridge``.
"""

from __future__ import annotations

from typing import Callable


def build_omp_sync_skills_parser(subparsers, *, cmd_omp_sync_skills: Callable) -> None:
    """Attach the ``omp-sync-skills`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "omp-sync-skills",
        help="Symlink the shared skills library into omp's user skills dir",
        description=(
            "Reconcile <omp agent dir>/skills to a flat symlink view of the mercury "
            "library ($MERCURY_HOME/skills/<category>/<name> → skills/<name>). "
            "Idempotent: adds new skills, removes vanished/excluded ones, never "
            "overwrites the user's own omp skills."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print the reconcile summary as JSON",
    )
    parser.set_defaults(func=cmd_omp_sync_skills)
