"""MERCURY-OMP PATCH: omp → mercury migration.

Mirrors the hermes migration UX (dry-run report, itemized selection,
non-destructive copies, automatic backups) for users coming from a stock
omp install. Source: ``~/.omp`` (or $OMP_SOURCE). Target: Mercury's omp
state root ($MERCURY_HOME/omp via PI_CODING_AGENT_DIR, default
``~/.mercury/omp``).

Migratable items:
  omp-sessions   ~/.omp/agent/sessions → session history (copy, non-destr.)
  omp-models     ~/.omp/agent/models.yml → custom model definitions
                 (id-preserving merge)
  omp-config     ~/.omp/agent/config.yml → non-model settings (theme,
                 providers.webSearchOrder, editor …) merged key-by-key;
                 model/approval keys are IGNORED — those belong to the
                 unified four-slot config, never imported
  omp-mcp        ~/.omp/agent/mcp.json → MCP server definitions (copy)
  omp-ssh        ~/.omp/agent/ssh.json → SSH connection definitions (copy)
  omp-themes     ~/.omp/agent/themes → custom themes (copy, no overwrite)

Run:  mercury migrate-omp [--dry-run] [--include ...] [--exclude ...]
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ITEMS = ("omp-sessions", "omp-models", "omp-config", "omp-mcp", "omp-ssh", "omp-themes")

# config.yml keys that mirror the unified models:/approvals: story —
# importing them would fight the bridge re-render; skip them explicitly.
_SKIP_CONFIG_KEYS = {
    "models", "approvalMode", "retry", "bash", "tools", "setupVersion",
}


def _source_root() -> Path:
    return Path(os.environ.get("OMP_SOURCE") or Path.home() / ".omp")


def _source_agent_dir() -> Path:
    return _source_root() / "agent"


def _target_root() -> Path:
    # Mercury's omp agent dir: PI_CODING_AGENT_DIR, default ~/.mercury/omp
    return Path(os.environ.get("PI_CODING_AGENT_DIR")
                or Path(os.environ.get("MERCURY_HOME") or Path.home() / ".mercury") / "omp")


def _backup(path: Path, backup_root: Path) -> None:
    if not path.exists():
        return
    backup_root.mkdir(parents=True, exist_ok=True)
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(path, backup_root / f"{path.name}.{stamp}.bak")


def _load_yaml_settings(path: Path) -> dict:
    """Minimal key-preserving YAML read for omp's config.yml (flat +
    one-level nesting is all omp writes there)."""
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dump_yaml(data: dict) -> str:
    try:
        import yaml
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    except Exception:
        return json.dumps(data, indent=2)


def plan(include: set[str] | None = None, exclude: set[str] | None = None) -> dict:
    """Dry-run: report what WOULD migrate. No writes."""
    src = _source_agent_dir()
    selected = set(ITEMS) if include is None else set(include) & set(ITEMS)
    selected -= (exclude or set())

    report: dict = {"source": str(src), "source_exists": src.is_dir(), "items": {}}
    if not src.is_dir():
        return report

    if "omp-sessions" in selected:
        s = src / "sessions"
        report["items"]["omp-sessions"] = {
            "source": str(s), "exists": s.is_dir(),
            "count": sum(1 for _ in s.rglob("*.jsonl")) if s.is_dir() else 0,
        }
    if "omp-models" in selected:
        s = src / "models.yml"
        data = _load_yaml_settings(s) if s.exists() else {}
        report["items"]["omp-models"] = {
            "source": str(s), "exists": s.exists(),
            "count": len(data.get("models", data)) if isinstance(data.get("models", data), (list, dict)) else 0,
        }
    if "omp-config" in selected:
        s = src / "config.yml"
        data = _load_yaml_settings(s) if s.exists() else {}
        keep = {k for k in data if k not in _SKIP_CONFIG_KEYS}
        report["items"]["omp-config"] = {
            "source": str(s), "exists": s.exists(),
            "keys": sorted(keep),
        }
    if "omp-mcp" in selected:
        s = src / "mcp.json"
        report["items"]["omp-mcp"] = {"source": str(s), "exists": s.exists()}
    if "omp-ssh" in selected:
        s = src / "ssh.json"
        report["items"]["omp-ssh"] = {"source": str(s), "exists": s.exists()}
    if "omp-themes" in selected:
        s = src / "themes"
        report["items"]["omp-themes"] = {
            "source": str(s), "exists": s.is_dir(),
            "count": len([f for f in s.iterdir() if f.suffix == ".json"]) if s.is_dir() else 0,
        }
    return report


def run(include: set[str] | None = None, exclude: set[str] | None = None,
        dry_run: bool = False) -> dict:
    """Execute the migration. Non-destructive: existing files never
    overwritten (backups taken before merges)."""
    src = _source_agent_dir()
    tgt = _target_root()
    results = {"source": str(src), "target": str(tgt), "dry_run": dry_run, "items": {}}
    if not src.is_dir():
        results["error"] = f"no omp installation found at {src}"
        return results

    selected = set(ITEMS) if include is None else set(include) & set(ITEMS)
    selected -= (exclude or set())
    backup_root = tgt.parent / "migration-backup"

    if dry_run:
        return plan(include, exclude)

    tgt.mkdir(parents=True, exist_ok=True)

    # --- sessions: copy *.jsonl non-destructively (skip existing names)
    if "omp-sessions" in selected:
        s = src / "sessions"
        copied = 0
        if s.is_dir():
            tdir = tgt / "sessions"
            tdir.mkdir(parents=True, exist_ok=True)
            for f in s.rglob("*.jsonl"):
                rel = f.relative_to(s)
                d = tdir / rel
                if not d.exists():
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, d)
                    copied += 1
        results["items"]["omp-sessions"] = f"copied {copied} session file(s)"

    # --- models.yml: id-preserving merge
    if "omp-models" in selected:
        s = src / "models.yml"
        if s.exists():
            src_models = _load_yaml_settings(s)
            tgt_path = tgt / "models.yml"
            tgt_models = _load_yaml_settings(tgt_path) if tgt_path.exists() else {}
            src_list = src_models.get("models", src_models if isinstance(src_models, list) else [])
            tgt_list = tgt_models.get("models", tgt_models if isinstance(tgt_models, list) else [])
            if isinstance(src_list, dict):
                src_list = list(src_list.values())
            if isinstance(tgt_list, dict):
                tgt_list = list(tgt_list.values())
            have = set()
            for m in tgt_list:
                if isinstance(m, dict):
                    mid = m.get("id") or m.get("name")
                    if mid:
                        have.add(str(mid))
            added = [m for m in src_list
                     if isinstance(m, dict) and str(m.get("id") or m.get("name") or "") not in have]
            if added:
                _backup(tgt_path, backup_root)
                merged = dict(tgt_models) if isinstance(tgt_models, dict) else {}
                merged["models"] = (tgt_list or []) + added
                tgt_path.write_text(_dump_yaml(merged), encoding="utf-8")
            results["items"]["omp-models"] = f"merged (+{len(added)} model(s))"
        else:
            results["items"]["omp-models"] = "skipped (absent)"

    # --- config.yml: key-by-key merge, bridge-owned keys skipped
    if "omp-config" in selected:
        s = src / "config.yml"
        if s.exists():
            src_cfg = _load_yaml_settings(s)
            tgt_path = tgt / "config.yml"
            tgt_cfg = _load_yaml_settings(tgt_path) if tgt_path.exists() else {}
            changed = False
            for k, v in src_cfg.items():
                if k in _SKIP_CONFIG_KEYS:
                    continue
                if k not in tgt_cfg or tgt_cfg.get(k) in (None, "", [], {}):
                    tgt_cfg[k] = v
                    changed = True
            if changed:
                _backup(tgt_path, backup_root)
                tgt_path.write_text(_dump_yaml(tgt_cfg), encoding="utf-8")
            results["items"]["omp-config"] = (
                f"merged ({sum(1 for k in src_cfg if k not in _SKIP_CONFIG_KEYS)} key(s) considered)"
            )
        else:
            results["items"]["omp-config"] = "skipped (absent)"

    # --- mcp.json / ssh.json: whole-file copy if absent
    for key, name in (("omp-mcp", "mcp.json"), ("omp-ssh", "ssh.json")):
        if key in selected:
            s = src / name
            if s.exists():
                d = tgt / name
                if not d.exists():
                    shutil.copy2(s, d)
                    results["items"][key] = "copied"
                else:
                    results["items"][key] = "kept existing (not overwritten)"
            else:
                results["items"][key] = "skipped (absent)"

    # --- themes: copy without overwrite
    if "omp-themes" in selected:
        s = src / "themes"
        copied = 0
        if s.is_dir():
            tdir = tgt / "themes"
            tdir.mkdir(parents=True, exist_ok=True)
            for f in s.iterdir():
                if f.is_file() and not (tdir / f.name).exists():
                    shutil.copy2(f, tdir / f.name)
                    copied += 1
        results["items"]["omp-themes"] = f"copied {copied} theme(s)"

    return results
