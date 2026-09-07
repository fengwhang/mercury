"""MERCURY-OMP PATCH (skills bridge): expose the shared library to omp children.

Why: omp discovers user skills ONLY from its agent dir —
``<agentDir>/skills/<name>/SKILL.md``, ONE level deep, symlinked dirs
accepted (omp/packages/coding-agent/src/discovery/builtin.ts loadSkills →
helpers.ts scanSkillsFromDir). Mercury's shared library is category-nested
(``$MERCURY_HOME/skills/<category>/<name>/SKILL.md``), so omp's scan of the
shared root sees only categories (no SKILL.md) and omp children get zero
mercury skills on a machine without a ``~/.claude`` dir.

Fix: reconcile ``<omp agent dir>/skills`` to a FLAT symlink view of the
library — one ``<name>`` symlink per skill. Idempotent and incremental:
re-running adds new skills, replaces repointed links, removes links whose
source vanished or became excluded, and leaves everything else (including
anything the user put there) untouched.

Ownership rule: a symlink in the target dir is "managed" iff it points
inside the mercury skills root. Real dirs/files and symlinks to elsewhere
are the user's own omp skills — NEVER overwritten, only warned about.

omp agent-dir resolution mirrors omp/packages/utils/src/dirs.ts
(getConfigDirName / getConfigAgentDirName / DirResolver):
  - active profile (``OMP_PROFILE`` canonical, ``PI_PROFILE`` legacy
    fallback; an explicitly empty ``OMP_PROFILE`` selects the default
    profile) → ``<home>/<PI_CONFIG_DIR|.omp>/profiles/<profile>/agent``
  - else ``PI_CODING_AGENT_DIR`` (absolute, or resolved against cwd like
    Node's path.resolve) → that dir
  - else ``<home>/<PI_CONFIG_DIR|.omp>/agent``
Mercury's launcher forces ``PI_CODING_AGENT_DIR=$MERCURY_HOME/omp``, so the
bridge lands in the ONE state tree under mercury. (omp defines no
``OMP_AGENT_DIR``; ``XDG_*_HOME`` only redirects data/state/cache, never
the agent dir itself — verified against dirs.ts DirResolver.)

Excludes: conversation-side skills meaningless for a headless coding child.
The default list is REPLACED (not extended) by ``hermes.omp_skills_exclude``
in the unified config ($MERCURY_HOME/config.yaml). The unified file is read
directly with yaml — same resolution as mercury_cli/omp_sync.py and
tools/omp_delegation.py — because importing mercury_cli.config costs
seconds and the launcher calls this on every boot (must stay ~free).
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Conversation-side-only skills — the user's devices, messaging, and live
# meetings; noise (or actively wrong) for a headless coding child. All
# coding/research/web skills are kept. Names verified against the real
# $MERCURY_HOME/skills tree (category in parentheses).
DEFAULT_EXCLUDES: frozenset = frozenset({
    "computer-use",            # drives the user's desktop GUI (autonomous-ai-agents)
    "imessage",                # user's messaging inbox (apple)
    "apple-notes",             # personal Apple-device data (apple)
    "apple-reminders",         # personal Apple-device data (apple)
    "findmy",                  # device location lookup (apple)
    "teams-meeting-pipeline",  # live Teams meeting capture (productivity)
    "meeting-action-items",    # meeting transcript post-processing (productivity)
    "weekly-review-planning",  # user's weekly-review chat flow (productivity)
})

# omp profile grammar (omp dirs.ts PROFILE_NAME_RE): [a-z0-9][a-z0-9._-]{0,63}
_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def _normalize_profile_name(value: str) -> Optional[str]:
    """Mirror omp's profile normalization (lowercase, PROFILE_NAME_RE).

    Invalid values degrade to the default profile — omp's
    readProfileFromEnvSafe swallows normalizeProfileName's throw, and a bad
    env var must not break best-effort bridging either.
    """
    name = value.strip().lower()
    if name and _PROFILE_RE.fullmatch(name):
        return name
    return None


def _active_profile() -> Optional[str]:
    """OMP_PROFILE is canonical; PI_PROFILE is the legacy fallback.

    An explicitly EMPTY OMP_PROFILE selects the default profile rather than
    inheriting PI_PROFILE (omp resolveProfileEnv semantics).
    """
    omp = os.environ.get("OMP_PROFILE")
    if omp is not None:
        return _normalize_profile_name(omp)
    return _normalize_profile_name(os.environ.get("PI_PROFILE", ""))


def resolve_omp_agent_dir(home: Optional[Path] = None) -> Path:
    """The omp agent dir whose ``skills/`` the bridge manages.

    Mirrors omp dirs.ts precedence — see module docstring.
    """
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    config_dir = os.environ.get("PI_CONFIG_DIR") or ".omp"
    profile = _active_profile()
    if profile:
        return base / config_dir / "profiles" / profile / "agent"
    override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path.cwd() / p).resolve()
    return base / config_dir / "agent"


def resolve_mercury_skills_dir(home: Optional[Path] = None) -> Path:
    """Shared library root: MERCURY_SKILLS_DIR > $MERCURY_HOME/skills > ~/.mercury/skills."""
    env = os.environ.get("MERCURY_SKILLS_DIR", "").strip()
    if env:
        return Path(env)
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if mercury:
        return Path(mercury) / "skills"
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return base / ".mercury" / "skills"


def resolve_unified_config_path() -> Optional[Path]:
    """The unified config file — same resolution as omp_sync / omp_delegation."""
    value = os.environ.get("MERCURY_CONFIG", "").strip()
    if value:
        return Path(value)
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if mercury:
        return Path(mercury) / "config.yaml"
    return None


def load_excludes(config_path: Optional[Path] = None) -> Tuple[frozenset, str]:
    """Resolve the exclude set: config REPLACES defaults when present.

    Returns ``(excludes, source)`` with source ``"config"`` or
    ``"defaults"`` so callers can surface which set is in effect. Any read
    or shape problem degrades to defaults with a warning.
    """
    path = config_path if config_path is not None else resolve_unified_config_path()
    if path is not None and path.is_file():
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            raw = (data.get("hermes") or {}).get("omp_skills_exclude") if isinstance(data, dict) else None
            if isinstance(raw, str):
                return frozenset(n.strip() for n in raw.split(",") if n.strip()), "config"
            if isinstance(raw, list) and all(isinstance(n, str) for n in raw):
                return frozenset(n.strip() for n in raw if n.strip()), "config"
            if raw is not None:
                logger.warning(
                    "omp skills bridge: hermes.omp_skills_exclude must be a list of "
                    "skill names (got %s in %s); using defaults",
                    type(raw).__name__, path,
                )
        except Exception as exc:  # unparseable config must never break boot
            logger.warning("omp skills bridge: could not read %s (%s); using defaults", path, exc)
    return DEFAULT_EXCLUDES, "defaults"


def scan_mercury_skills(skills_root: Path) -> List[Tuple[str, str, Path]]:
    """All skills in the category-nested library as ``(category, name, dir)``.

    Deterministic: categories and skill dirs in sorted order, so collision
    resolution ("first category wins") is stable across runs and machines.
    """
    found: List[Tuple[str, str, Path]] = []
    try:
        categories = sorted(p for p in skills_root.iterdir()
                            if not p.name.startswith(".") and p.is_dir())
    except OSError:
        return found
    for cat in categories:
        try:
            children = sorted(p for p in cat.iterdir()
                              if not p.name.startswith(".") and p.is_dir()
                              and (p / "SKILL.md").is_file())
        except OSError:
            continue
        found.extend((cat.name, child.name, child) for child in children)
    return found


def _link_target(link: Path) -> Optional[Path]:
    """Absolute (unresolved) target of a symlink, or None if not a symlink."""
    try:
        target = Path(os.readlink(link))
    except OSError:
        return None
    return target if target.is_absolute() else link.parent / target


def _is_managed(link: Path, managed_root: Path) -> bool:
    """True when symlink *link* points inside *managed_root* (ours to edit/remove)."""
    target = _link_target(link)
    if target is None:
        return False
    try:
        target.resolve().relative_to(managed_root)
        return True
    except (ValueError, OSError):
        return False


def reconcile_omp_skills(
    *,
    mercury_skills_dir: Optional[Path] = None,
    omp_agent_dir: Optional[Path] = None,
    excludes: Optional[frozenset] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile ``<omp agent dir>/skills`` to the flat symlink view.

    Never raises for a missing/unusable source or target — returns a summary
    the CLI prints. Only managed symlinks (pointing inside the skills root)
    are created/replaced/removed; real entries and foreign symlinks are
    skipped with a warning.
    """
    skills_root = Path(mercury_skills_dir) if mercury_skills_dir is not None else resolve_mercury_skills_dir()
    agent_dir = Path(omp_agent_dir) if omp_agent_dir is not None else resolve_omp_agent_dir()
    if excludes is None:
        excludes, _exclude_source = load_excludes(config_path)

    summary: Dict[str, Any] = {
        "skills_dir": str(skills_root),
        "target_dir": str(agent_dir / "skills"),
        "excludes": sorted(excludes),
        "source_present": skills_root.is_dir(),
        "created": 0,
        "updated": 0,
        "removed": 0,
        "skipped_real": [],
        "skipped_foreign": [],
        "collisions": [],
        "failed": [],
    }

    # Desired flat namespace: first category in sorted order wins a name.
    desired: Dict[str, Path] = {}
    for category, name, skill_dir in scan_mercury_skills(skills_root):
        if name in excludes:
            continue
        if name in desired:
            summary["collisions"].append(name)
            logger.warning(
                "omp skills bridge: name collision on %r — %s/%s kept, %s/%s skipped",
                name, desired[name].parent.name, name, category, name,
            )
            continue
        desired[name] = skill_dir

    target_dir = agent_dir / "skills"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        summary["failed"].append(f"mkdir {target_dir}: {exc}")
        logger.warning("omp skills bridge: cannot create %s (%s)", target_dir, exc)
        return summary

    root_resolved = skills_root.resolve()

    # Create / refresh desired links.
    for name in sorted(desired):
        link = target_dir / name
        src = desired[name].resolve()
        if os.path.lexists(link):
            if not link.is_symlink():
                summary["skipped_real"].append(name)
                logger.warning(
                    "omp skills bridge: %s already exists (user's own skill) — not touched", link
                )
                continue
            current = _link_target(link)
            if current is not None and current.resolve() == src:
                continue  # already correct — leave untouched
            if not _is_managed(link, root_resolved):
                summary["skipped_foreign"].append(name)
                logger.warning(
                    "omp skills bridge: %s points outside the mercury library — not touched", link
                )
                continue
            try:
                link.unlink()
                os.symlink(src, link)
                summary["updated"] += 1
            except OSError as exc:
                summary["failed"].append(f"{name}: {exc}")
                logger.warning("omp skills bridge: could not repoint %s (%s)", link, exc)
            continue
        try:
            os.symlink(src, link)
            summary["created"] += 1
        except OSError as exc:
            summary["failed"].append(f"{name}: {exc}")
            logger.warning("omp skills bridge: could not link %s → %s (%s)", link, src, exc)

    # Remove managed links whose source vanished or became excluded.
    try:
        existing = sorted(target_dir.iterdir())
    except OSError:  # pragma: no cover - just created above
        return summary
    for entry in existing:
        if entry.name.startswith(".") or not entry.is_symlink():
            continue
        if entry.name in desired:
            continue
        if not _is_managed(entry, root_resolved):
            continue
        try:
            entry.unlink()
            summary["removed"] += 1
        except OSError as exc:
            summary["failed"].append(f"{entry.name}: {exc}")
            logger.warning("omp skills bridge: could not remove stale %s (%s)", entry, exc)

    return summary


def main(argv: Optional[List[str]] = None) -> int:
    """``mercury omp-sync-skills`` entry point (also ``python -m``-style via -c)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="mercury omp-sync-skills",
        description="Symlink the shared mercury skills library into omp's user skills dir "
                    "(flat, exclude-aware, idempotent).",
    )
    parser.add_argument("--json", action="store_true", help="print the reconcile summary as JSON")
    args = parser.parse_args(argv)

    summary = reconcile_omp_skills()
    if args.json:
        import json

        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if not summary["source_present"]:
        print(f"omp skills bridge: skills dir not found: {summary['skills_dir']}", file=sys.stderr)
        return 1
    print(f"omp skills dir : {summary['target_dir']}")
    print(f"mercury library: {summary['skills_dir']}")
    print(f"created={summary['created']} updated={summary['updated']} removed={summary['removed']}")
    if summary["excludes"]:
        print(f"excluded: {', '.join(summary['excludes'])}")
    for name in summary["collisions"]:
        print(f"collision: {name} — first category in sorted order wins")
    for name in summary["skipped_real"] + summary["skipped_foreign"]:
        print(f"skipped (not overwritten): {name}")
    for err in summary["failed"]:
        print(f"failed: {err}", file=sys.stderr)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
