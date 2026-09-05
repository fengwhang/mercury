"""MERCURY-OMP PATCH: hermes → mercury migration.

Mirrors the OpenClaw migration UX (dry-run report, itemized selection,
non-destructive copies, automatic backups) for users coming from a stock
hermes install. Source: ``~/.hermes`` (or $HERMES_SOURCE). Target: the
Mercury home ($MERCURY_HOME, default ``~/.mercury``).

Migratable items:
  soul          SOUL.md (merged, entry-level)
  memory        MEMORY.md (merged, entry-level)
  user-profile  USER.md (merged, entry-level)
  config        config.yaml hermes: subtree (model/provider/approvals keys)
  env           .env API keys (merged into ~/.mercury/.env — the ONE shared env)
  skills        ~/.hermes/skills → shared library ~/.mercury/config-skills
  sessions      sessions/ history (copy, non-destructive)
  cron          cron/jobs.json scheduled jobs (id-preserving)
  auth          auth.json OAuth provider logins (copy)

Run:  mercury migrate-hermes [--dry-run] [--include ...] [--exclude ...]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ITEMS = ("soul", "memory", "user-profile", "config", "env", "skills", "sessions", "cron", "auth")
MD_MAP = {"soul": "SOUL.md", "memory": "MEMORY.md", "user-profile": "USER.md"}


def _source_root() -> Path:
    return Path(os.environ.get("HERMES_SOURCE") or Path.home() / ".hermes")


def _target_root() -> Path:
    return Path(os.environ.get("MERCURY_HOME") or Path.home() / ".mercury")


def _target_config_dir() -> Path:
    return _target_root() / "config"


def _backup(path: Path, backup_root: Path) -> Path | None:
    if not path.exists():
        return None
    backup_root.mkdir(parents=True, exist_ok=True)
    rel = path.relative_to(backup_root.anchor)
    dest = backup_root / f"{path.name}.{_stamp()}.bak"
    shutil.copy2(path, dest)
    return dest


def _stamp() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _merge_md(source: Path, target: Path, limit: int = 500) -> tuple[str, int]:
    """Entry-level merge: source entries appended when not already present."""
    def entries(text: str) -> list[str]:
        out, buf = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("§") and buf:
                out.append("\n".join(buf).strip())
                buf = [line]
            elif not line.strip() and buf:
                out.append("\n".join(buf).strip())
                buf = []
            else:
                buf.append(line)
        return [e for e in out if e]

    src = entries(source.read_text(encoding="utf-8")) if source.exists() else []
    tgt = entries(target.read_text(encoding="utf-8")) if target.exists() else []
    tgt_set = {t.replace("\n", " ") for t in tgt}
    added = 0
    result = list(tgt)
    for e in src[:limit]:
        if e.replace("\n", " ") not in tgt_set:
            result.append(e)
            added += 1
    if result:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n".join(result) + "\n", encoding="utf-8")
    return ("merged", added)


def plan(include: set[str] | None = None, exclude: set[str] | None = None) -> dict:
    """Dry-run: report what WOULD migrate, item by item. No writes."""
    src = _source_root()
    tgt = _target_root()
    selected = set(ITEMS) if include is None else set(include) & set(ITEMS)
    selected -= (exclude or set())

    report: dict = {"source": str(src), "source_exists": src.is_dir(), "items": {}}
    if not src.is_dir():
        return report

    cfg_dir = _target_config_dir()
    # markdown memories
    for key, name in MD_MAP.items():
        s = src / name
        t = cfg_dir / name
        if key in selected:
            report["items"][key] = {
                "source": str(s), "target": str(t),
                "exists": s.exists(),
                "would_add": 0 if not s.exists() else len(
                    [e for e in (s.read_text(encoding="utf-8").split("\n\n") if s.exists() else []) if e.strip()]
                ),
            }
    # env
    if "env" in selected:
        s = src / ".env"
        report["items"]["env"] = {"source": str(s), "exists": s.exists()}
    # config
    if "config" in selected:
        s = src / "config.yaml"
        report["items"]["config"] = {"source": str(s), "exists": s.exists()}
    # skills
    if "skills" in selected:
        s = src / "skills"
        report["items"]["skills"] = {"source": str(s), "exists": s.is_dir(),
                                     "count": len(list(s.iterdir())) if s.is_dir() else 0}
    # sessions
    if "sessions" in selected:
        s = src / "sessions"
        n = len(list(s.glob("*.json"))) if s.is_dir() else 0
        report["items"]["sessions"] = {"source": str(s), "exists": s.is_dir(), "count": n}
    # cron
    if "cron" in selected:
        s = src / "cron" / "jobs.json"
        n = 0
        if s.exists():
            try:
                n = len(json.loads(s.read_text(encoding="utf-8")).get("jobs", []))
            except Exception:
                pass
        report["items"]["cron"] = {"source": str(s), "exists": s.exists(), "count": n}
    # auth
    if "auth" in selected:
        s = src / "auth.json"
        report["items"]["auth"] = {"source": str(s), "exists": s.exists()}
    return report


def run(include: set[str] | None = None, exclude: set[str] | None = None, dry_run: bool = False) -> dict:
    """Execute the migration. Returns a per-item result report."""
    if dry_run:
        return {"dry_run": True, **plan(include, exclude)}

    src = _source_root()
    tgt = _target_root()
    backup_root = tgt / "migration-backup" / _stamp()
    results: dict = {"source": str(src), "items": {}}

    selected = set(ITEMS) if include is None else set(include) & set(ITEMS)
    selected -= (exclude or set())
    cfg_dir = _target_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (tgt / "hermes").mkdir(parents=True, exist_ok=True)

    for key, name in MD_MAP.items():
        if key not in selected:
            continue
        s, t = src / name, cfg_dir / name
        if not s.exists():
            results["items"][key] = "skipped (absent)"
            continue
        _backup(t, backup_root)
        status, added = _merge_md(s, t)
        results["items"][key] = f"{status} (+{added} entries)"

    if "env" in selected:
        s, t = src / ".env", tgt / ".env"
        if s.exists():
            _backup(t, backup_root)
            existing = t.read_text(encoding="utf-8") if t.exists() else ""
            have = {ln.split("=", 1)[0] for ln in existing.splitlines() if "=" in ln}
            new = [ln for ln in s.read_text(encoding="utf-8").splitlines()
                   if "=" in ln and ln.split("=", 1)[0] not in have]
            with open(t, "a", encoding="utf-8") as f:
                for ln in new:
                    f.write(ln + "\n")
            os.chmod(t, 0o600)
            results["items"]["env"] = f"merged (+{len(new)} keys)"
        else:
            results["items"]["env"] = "skipped (absent)"

    if "config" in selected:
        s, t = src / "config.yaml", tgt / "config.yaml"
        if s.exists():
            _backup(t, backup_root)
            import yaml
            whole = yaml.safe_load(t.read_text(encoding="utf-8")) if t.exists() else {}
            whole = whole if isinstance(whole, dict) else {}
            sub = yaml.safe_load(s.read_text(encoding="utf-8")) or {}
            sub = sub if isinstance(sub, dict) else {}
            # model/provider → hermes: subtree (models: slots derive via omp-sync)
            model = sub.get("model") or {}
            if isinstance(model, dict) and model.get("default"):
                whole.setdefault("hermes", {})["model"] = model
            # approvals
            ap = sub.get("approvals")
            if isinstance(ap, dict):
                whole["approvals"] = ap
            t.write_text(yaml.safe_dump(whole, sort_keys=False), encoding="utf-8")
            results["items"]["config"] = "hermes: subtree written (model + approvals)"
        else:
            results["items"]["config"] = "skipped (absent)"

    if "skills" in selected:
        s = src / "skills"
        if s.is_dir():
            dest = tgt / "skills"
            copied = 0
            for child in s.iterdir():
                d = dest / child.name
                if d.exists():
                    continue
                if child.is_dir():
                    shutil.copytree(child, d, symlinks=True)
                else:
                    shutil.copy2(child, d)
                copied += 1
            results["items"]["skills"] = f"copied {copied} new skill(s)"
        else:
            results["items"]["skills"] = "skipped (absent)"

    if "sessions" in selected:
        s = src / "sessions"
        if s.is_dir():
            dest = tgt / "hermes" / "sessions"
            dest.mkdir(parents=True, exist_ok=True)
            copied = sum(
                1
                for f in s.glob("*.json")
                if not (dest / f.name).exists() and not shutil.copy2(f, dest / f.name)
            )
            results["items"]["sessions"] = f"copied {copied} session(s)"
        else:
            results["items"]["sessions"] = "skipped (absent)"

    if "cron" in selected:
        s = src / "cron" / "jobs.json"
        if s.exists():
            dest = tgt / "hermes" / "cron" / "jobs.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            _backup(dest, backup_root)
            try:
                data = json.loads(s.read_text(encoding="utf-8"))
                existing = json.loads(dest.read_text(encoding="utf-8")) if dest.exists() else {"jobs": []}
                have = {j.get("id") for j in existing.get("jobs", [])}
                added = [j for j in data.get("jobs", []) if j.get("id") not in have]
                existing["jobs"].extend(added)
                dest.write_text(json.dumps(existing, indent=2), encoding="utf-8")
                results["items"]["cron"] = f"merged (+{len(added)} job(s))"
            except Exception as exc:
                results["items"]["cron"] = f"error: {exc}"
        else:
            results["items"]["cron"] = "skipped (absent)"

    if "auth" in selected:
        s = src / "auth.json"
        if s.exists():
            dest = tgt / "hermes" / "auth.json"
            if not dest.exists():
                shutil.copy2(s, dest)
                os.chmod(dest, 0o600)
                results["items"]["auth"] = "copied (OAuth logins)"
            else:
                results["items"]["auth"] = "kept existing"
        else:
            results["items"]["auth"] = "skipped (absent)"

    results["backup"] = str(backup_root) if backup_root.exists() else None
    return results
