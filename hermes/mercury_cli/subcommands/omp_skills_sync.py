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
        help="Reconcile omp's 3-source skill union (engine root wins)",
        description=(
            "Present omp the union of three skill sources: its own engine-root "
            "skills (<omp agent dir>/skills, never touched), the shared library "
            "($MERCURY_HOME/skills), and the hermes engine tree "
            "($HERMES_HOME/skills) — category-nested trees become flat "
            "skills/<name> symlinks. Engine root wins name collisions; the "
            "shared library wins over the hermes engine tree. Idempotent: adds "
            "new skills, removes vanished/excluded ones, never overwrites "
            "omp's own skills. Also runs automatically at launcher boot, at "
            "install, and before every omp spawn (bridge.py --render-omp)."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="print the reconcile summary as JSON",
    )
    parser.set_defaults(func=cmd_omp_sync_skills)
