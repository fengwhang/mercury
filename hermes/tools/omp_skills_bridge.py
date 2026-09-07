"""MERCURY-OMP PATCH (skills bridge): union all three mercury skill trees for omp.

The three skill sources (highest precedence first):
  1. ENGINE ROOT — ``<omp agent dir>/skills`` (flat, ``<name>/SKILL.md``),
                 omp-private native skills. Never touched by the bridge and
                 always wins a name collision (no link is placed over them).
  2. SHARED     — ``$MERCURY_HOME/skills`` (category-nested,
                 ``<category>/<name>/SKILL.md``), hermes' native layout and
                 the ONE user-facing library both engines read/write.
  3. HERMES ENGINE — ``$HERMES_HOME/skills`` (same category-nested layout;
                 under Mercury the launcher forces ``HERMES_HOME =
                 $MERCURY_HOME/hermes``). This is the tree the hermes engine
                 actually reads: bundled skills are seeded into it at startup
                 (tools/skills_sync), ``hermes skills install`` targets it,
                 and curator edits land there. Without bridging it, omp sees
                 only the shared-library subset — the engine tree carries the
                 full bundled set (research/, web/, software-development/,
                 mlops/, devops/…), so a coding child lost most of it.

Why a bridge at all: omp discovers user skills ONLY from its engine root —
``<agentDir>/skills/<name>/SKILL.md``, ONE level deep, symlinked dirs
accepted (omp/packages/coding-agent/src/discovery/builtin.ts loadSkills →
helpers.ts scanSkillsFromDir). omp's own mercury patch scans the shared
root too, but the compiled binary scans it the same flat way, so the
category-nested library yields nothing there. The bridge materializes the
shared library AND the hermes engine tree INTO the engine root as a flat
symlink view — one ``<name>`` symlink per skill — which the frozen binary
picks up natively.

Union semantics (ENGINE ROOT WINS, SHARED WINS OVER HERMES ENGINE):
  - omp sees ``engine-root skills ∪ shared-library skills ∪ hermes-engine
    skills``.
  - Real dirs/files in the engine root and symlinks pointing outside both
    bridged roots are omp's OWN skills — never created, replaced, or
    removed by the bridge, and they win any name collision with either
    bridged tree (no link is placed over them; the bridged copy is simply
    not materialized under that name).
  - A name present in BOTH bridged trees resolves to the SHARED library
    copy: the shared library is the user-facing ONE library (explicit
    installs, omp-managed, migrate target), the engine tree is seeded
    defaults — user intent outranks bundled defaults, mirroring how
    hermes' own sync honors user copies over bundled ones.
  - The ``omp-managed`` category (omp's auto-learned skills,
    ``$MERCURY_HOME/skills/omp-managed`` per omp/.../autolearn/
    managed-skills.ts getManagedSkillsDir) is NOT materialized from any
    source: omp already loads it through its dedicated managed-skills
    provider at the LOWEST skill priority, so an authored skill of the same
    name wins. Bridging it would promote learned skills to engine-root
    (highest) priority and invert that design.

Reconcile is idempotent and incremental: re-running adds new skills,
replaces repointed links, removes links whose source vanished or became
excluded (including any pre-fix ``omp-managed`` links — self-healing), and
leaves everything else untouched. It runs at launcher boot, at install,
and — via bridge.py ``--render-omp`` — before every omp spawn (delegate
RPC children, ``/omp``, cron omp_direct, post-setup sync), so a
long-running gateway converges without a reboot.

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

# Conversation-side-only skills — the user's devices, messaging, live
# meetings, and chat persona; noise (or actively wrong) for a headless
# coding child. All coding/research/web skills are kept. Names verified
# against BOTH real trees: $MERCURY_HOME/skills and $HERMES_HOME/skills
# (category in parentheses).
DEFAULT_EXCLUDES: frozenset = frozenset({
    "computer-use",               # drives the user's desktop GUI (autonomous-ai-agents)
    "imessage",                   # user's messaging inbox (apple)
    "apple-notes",                # personal Apple-device data (apple)
    "apple-reminders",            # personal Apple-device data (apple)
    "findmy",                     # device location lookup (apple)
    "openhue",                    # user's smart-home lights (smart-home)
    "simplex-chat",               # user's messaging bridge (devops)
    "simplex-chat-hermes-setup",  # user's messaging bridge (devops)
    "persona-authoring",          # chat persona/tone for the parent agent (communication)
    "teams-meeting-pipeline",     # live Teams meeting capture (productivity)
    "meeting-action-items",       # meeting transcript post-processing (productivity)
    "weekly-review-planning",     # user's weekly-review chat flow (productivity)
})

# omp profile grammar (omp dirs.ts PROFILE_NAME_RE): [a-z0-9][a-z0-9._-]{0,63}
_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# omp's auto-learned skills live in the SHARED library as their own
# category ($MERCURY_HOME/skills/omp-managed — omp autolearn/managed-skills.ts
# getManagedSkillsDir under MERCURY_HOME) but must NOT be materialized into
# the engine root: omp loads them through its dedicated managed-skills
# provider at the LOWEST skill priority (an authored same-name skill wins).
# Bridging them would promote learned skills to engine-root priority and
# invert omp's authored-beats-managed design.
MANAGED_SKILLS_CATEGORY = "omp-managed"


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


def resolve_hermes_engine_skills_dir(home: Optional[Path] = None) -> Path:
    """Hermes engine tree root: ``$MERCURY_HOME/hermes/skills`` >
    ``$HERMES_HOME/skills`` > ``~/.mercury/hermes/skills``.

    Under Mercury the launcher forces ``HERMES_HOME=$MERCURY_HOME/hermes``,
    so both env forms name the same tree — the one the hermes engine
    actually reads. ``MERCURY_HOME`` wins so an ambient stock-hermes
    ``HERMES_HOME`` can never leak a foreign install's tree once the
    mercury home is known (mirrors ``resolve_mercury_skills_dir``).
    """
    mercury = os.environ.get("MERCURY_HOME", "").strip()
    if mercury:
        return Path(mercury) / "hermes" / "skills"
    hermes = os.environ.get("HERMES_HOME", "").strip()
    if hermes:
        return Path(hermes) / "skills"
    base = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return base / ".mercury" / "hermes" / "skills"


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


def scan_category_skills(skills_root: Path) -> List[Tuple[str, str, Path]]:
    """Authored skills in a category-nested tree (shared library or hermes
    engine — both use hermes' native ``<category>/<name>/SKILL.md`` layout)
    as ``(category, name, dir)``.

    Deterministic: categories and skill dirs in sorted order, so collision
    resolution ("first category wins") is stable across runs and machines.
    The ``omp-managed`` category is EXCLUDED in every tree — see
    MANAGED_SKILLS_CATEGORY: learned skills keep their own low-priority omp
    provider instead of being promoted into the engine root.
    """
    found: List[Tuple[str, str, Path]] = []
    try:
        categories = sorted(p for p in skills_root.iterdir()
                            if not p.name.startswith(".") and p.is_dir()
                            and p.name != MANAGED_SKILLS_CATEGORY)
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


def _is_managed(link: Path, managed_roots) -> bool:
    """True when symlink *link* points inside any of *managed_roots*
    (resolved paths — ours to edit/remove). A link into EITHER bridged tree
    (shared library or hermes engine) is bridge-owned."""
    target = _link_target(link)
    if target is None:
        return False
    try:
        resolved = target.resolve()
    except OSError:
        return False
    for root in managed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def reconcile_omp_skills(
    *,
    mercury_skills_dir: Optional[Path] = None,
    hermes_skills_dir: Optional[Path] = None,
    omp_agent_dir: Optional[Path] = None,
    excludes: Optional[frozenset] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile the union view: engine-root ∪ shared library ∪ hermes engine.

    Materializes every non-excluded skill from BOTH bridged trees (shared
    library first, hermes engine tree filling remaining names) as a flat
    symlink in ``<omp agent dir>/skills`` — unless the engine root already
    owns that name (real dir/file or foreign symlink), in which case the
    ENGINE ROOT entry wins and is never touched. Never raises for a missing
    or unusable source or target — returns a summary the CLI prints. Only
    managed symlinks (pointing inside either bridged root) are
    created/replaced/removed; real entries and foreign symlinks are
    skipped with a warning.
    """
    skills_root = Path(mercury_skills_dir) if mercury_skills_dir is not None else resolve_mercury_skills_dir()
    hermes_root = Path(hermes_skills_dir) if hermes_skills_dir is not None else resolve_hermes_engine_skills_dir()
    agent_dir = Path(omp_agent_dir) if omp_agent_dir is not None else resolve_omp_agent_dir()
    if excludes is None:
        excludes, _exclude_source = load_excludes(config_path)

    summary: Dict[str, Any] = {
        "skills_dir": str(skills_root),
        "hermes_skills_dir": str(hermes_root),
        "target_dir": str(agent_dir / "skills"),
        "excludes": sorted(excludes),
        "source_present": skills_root.is_dir(),
        "hermes_source_present": hermes_root.is_dir(),
        "sources": {"shared": 0, "hermes": 0},
        "created": 0,
        "updated": 0,
        "removed": 0,
        "skipped_real": [],
        "skipped_foreign": [],
        "collisions": [],
        "failed": [],
    }

    # Desired flat namespace, three sources in precedence order:
    #   shared library (user-facing ONE library) → hermes engine tree fills
    #   gaps. Within one tree, first category in sorted order wins a name.
    # The omp engine root needs no entry here — real entries and foreign
    # symlinks are skipped at materialization time, so they always win.
    desired: Dict[str, Path] = {}
    origin: Dict[str, str] = {}
    collision_names: set = set()

    def _absorb(root: Path, source: str) -> None:
        for category, name, skill_dir in scan_category_skills(root):
            if name in excludes:
                continue
            prior = desired.get(name)
            if prior is not None:
                if prior == skill_dir:
                    continue  # same tree reached twice (roots may alias)
                if name not in collision_names:
                    collision_names.add(name)
                    summary["collisions"].append(name)
                logger.warning(
                    "omp skills bridge: name collision on %r — %s/%s kept, %s/%s skipped",
                    name, prior.parent.name, name, category, name,
                )
                continue
            desired[name] = skill_dir
            origin[name] = source

    _absorb(skills_root, "shared")
    _absorb(hermes_root, "hermes")
    summary["sources"] = {
        "shared": sum(1 for s in origin.values() if s == "shared"),
        "hermes": sum(1 for s in origin.values() if s == "hermes"),
    }

    target_dir = agent_dir / "skills"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        summary["failed"].append(f"mkdir {target_dir}: {exc}")
        logger.warning("omp skills bridge: cannot create %s (%s)", target_dir, exc)
        return summary

    managed_roots = {root.resolve() for root in (skills_root, hermes_root)}

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
            if not _is_managed(link, managed_roots):
                summary["skipped_foreign"].append(name)
                logger.warning(
                    "omp skills bridge: %s points outside the bridged mercury trees — not touched", link
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
        if not _is_managed(entry, managed_roots):
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
        description="Present omp the union of its engine-root skills, the shared mercury "
                    "library, and the hermes engine tree (flat symlinks, exclude-aware, "
                    "idempotent).",
    )
    parser.add_argument("--json", action="store_true", help="print the reconcile summary as JSON")
    args = parser.parse_args(argv)

    summary = reconcile_omp_skills()
    if args.json:
        import json

        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if not (summary["source_present"] or summary["hermes_source_present"]):
        print(
            f"omp skills bridge: no skills tree found: {summary['skills_dir']} "
            f"or {summary['hermes_skills_dir']}",
            file=sys.stderr,
        )
        return 1
    print(f"omp skills dir : {summary['target_dir']}")
    print(f"mercury library: {summary['skills_dir']}")
    print(f"hermes engine   : {summary['hermes_skills_dir']}")
    sources = summary["sources"]
    print(
        f"bridged={sources['shared'] + sources['hermes']} "
        f"(shared={sources['shared']}, hermes={sources['hermes']}) "
        f"created={summary['created']} updated={summary['updated']} removed={summary['removed']}"
    )
    if summary["excludes"]:
        print(f"excluded: {', '.join(summary['excludes'])}")
    for name in summary["collisions"]:
        print(f"collision: {name} — shared library wins over the hermes engine tree")
    for name in summary["skipped_real"] + summary["skipped_foreign"]:
        print(f"skipped (not overwritten): {name}")
    for err in summary["failed"]:
        print(f"failed: {err}", file=sys.stderr)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(main())
