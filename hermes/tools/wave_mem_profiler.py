"""Wave-time memory profiler for REAL delegation waves.

Background: a live wave once held ~17GB with far fewer children than the
N x 0.7GB synthetic flat-rate model predicts. Candidate causes:

  (a) long-lived children accumulate context over a run;
  (b) grandchildren fan out INSIDE children (omp task.maxConcurrency);
  (c) tool-output payloads + full transcripts accumulate in the parent;
  (d) thread/memory leaks in the long-running gateway or RPC transport.

This module instruments REAL waves (never synthetic benchmarks):

  * sampler: background daemon thread, ~5s interval, per-process RSS from
    /proc tagged by delegation identity (MERCURY_WAVE_* env markers stamped
    on children at spawn) and tree depth (parent=0, child=1, grandchild>=2),
    plus parent-session context bytes (state.db, best-effort) and payload
    bytes (child results noted by the dispatch hook);
  * log: one timestamped JSONL under $MERCURY_HOME/logs/wave-mem/
    (never into the repo, never into session context);
  * report: ``python -m tools.wave_mem_profiler --report <log>`` attributes
    peak MB to parent context vs N children vs M grandchildren vs payloads.

Gating: ``MERCURY_WAVE_MEM_PROFILE=1`` (alias ``HERMES_WAVE_MEM_PROFILE``)
or ``delegation.wave_mem_profile: true`` in config.yaml. Default OFF:
``maybe_start()`` returns None after two getenv calls — no thread, no file,
no per-child env keys — and every other entry point no-ops on None.

Stdlib only. Linux /proc primary; on other platforms samples record
rss_mb=null (counts still work) instead of raising.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LOG_VERSION = 1
DEFAULT_INTERVAL_S = 5.0
LOG_SUBDIR = Path("logs") / "wave-mem"

ENV_FLAG = "MERCURY_WAVE_MEM_PROFILE"
ENV_FLAG_ALIAS = "HERMES_WAVE_MEM_PROFILE"
ENV_INTERVAL = "MERCURY_WAVE_MEM_PROFILE_INTERVAL"

# Env markers stamped on child processes at spawn (only when enabled).
WAVE_ID_KEY = "MERCURY_WAVE_ID"
WAVE_ROLE_KEY = "MERCURY_WAVE_ROLE"
WAVE_NAME_KEY = "MERCURY_WAVE_NAME"
WAVE_INDEX_KEY = "MERCURY_WAVE_INDEX"

_TRUTHY = {"1", "true", "yes", "on"}


def _flag_on(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def is_enabled() -> bool:
    """Fast-path gate: env only, no imports, no I/O. Hot-path safe."""
    return _flag_on(os.environ.get(ENV_FLAG)) or _flag_on(
        os.environ.get(ENV_FLAG_ALIAS)
    )


def profiling_enabled() -> bool:
    """Full gate: env var wins, else ``delegation.wave_mem_profile`` config.

    The config read is lazy and guarded — any failure means OFF. When the
    env flag is set the config is never touched.
    """
    if is_enabled():
        return True
    try:
        from tools.delegate_tool import _load_config  # lazy: no import cost off-path

        cfg = _load_config()
        return bool(isinstance(cfg, dict) and cfg.get("wave_mem_profile"))
    except Exception:
        return False


def sample_interval() -> float:
    try:
        return max(1.0, float(os.environ.get(ENV_INTERVAL, "") or DEFAULT_INTERVAL_S))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S


def mercury_home() -> Path:
    val = os.environ.get("MERCURY_HOME", "").strip()
    if val:
        return Path(val)
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        # HERMES_HOME is the engine-private dir ($MERCURY_HOME/hermes);
        # its parent IS the Mercury home in launcher layouts.
        p = Path(val)
        return p.parent if p.name == "hermes" else p
    return Path.home() / ".mercury"


def log_dir() -> Path:
    return mercury_home() / LOG_SUBDIR


def child_env_overlay(wave_id: str, name: str, index: int) -> Dict[str, str]:
    """Identity markers for one child env. {} when profiling is off.

    Called per child on the spawn path, so it must stay trivial: one
    cached gate check, no I/O. Uses is_enabled() (env-only), NOT the
    config fallback — the dispatch-site maybe_start() already resolved
    the full gate, and per-child config reads would break the zero-
    overhead contract.
    """
    if not is_enabled():
        return {}
    return {
        WAVE_ID_KEY: wave_id,
        WAVE_ROLE_KEY: "child",
        WAVE_NAME_KEY: str(name or "")[:64],
        WAVE_INDEX_KEY: str(index),
    }


# --- /proc scanning (stdlib, Linux) -------------------------------------------

ProcInfo = Dict[str, Any]
ProcTable = Dict[int, ProcInfo]


def _read_proc_table() -> ProcTable:
    """pid -> {ppid, comm, rss_mb}. Best-effort; races with exit → skip."""
    table: ProcTable = {}
    try:
        page = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        page = 4096
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return table
    for pid_s in pids:
        pid = int(pid_s)
        try:
            with open(f"/proc/{pid_s}/stat", "r", encoding="utf-8",
                      errors="replace") as fh:
                stat = fh.read()
            # comm is parenthesized and may contain spaces/parens.
            rparen = stat.rfind(")")
            if rparen < 0:
                continue
            comm = stat[stat.find("(") + 1:rparen]
            rest = stat[rparen + 2:].split()
            ppid = int(rest[1]) if len(rest) > 1 else 0
            rss_mb: Optional[float] = None
            try:
                with open(f"/proc/{pid_s}/statm", "r", encoding="utf-8") as fh:
                    rss_pages = int(fh.read().split()[1])
                rss_mb = round(rss_pages * page / (1024 * 1024), 2)
            except (OSError, IndexError, ValueError):
                pass
            table[pid] = {"ppid": ppid, "comm": comm, "rss_mb": rss_mb}
        except (OSError, ValueError, IndexError):
            continue
    return table


def _proc_environ(pid: int, keys: List[str]) -> Dict[str, str]:
    """Read selected env keys of one process. Root-owned procs → {}."""
    out: Dict[str, str] = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read(65536)
    except OSError:
        return out
    want = set(keys)
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        k, _, v = item.partition(b"=")
        try:
            ks = k.decode("utf-8", errors="replace")
        except Exception:
            continue
        if ks in want:
            try:
                out[ks] = v.decode("utf-8", errors="replace")
            except Exception:
                continue
            if len(out) == len(want):
                break
    return out


def _proc_cmdline(pid: int, limit: int = 160) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read(4096)
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()[:limit]
    except OSError:
        return ""


def attribute_tree(
    table: ProcTable,
    parent_pid: int,
    wave_id: str = "",
    env_reader: Optional[Callable[[int], Dict[str, str]]] = None,
) -> Dict[str, List[ProcInfo]]:
    """Split a /proc table into parent/children/grandchildren/detached.

    Depth = process-tree distance from ``parent_pid`` (BFS over ppids):
    0 → parent, 1 → child, >=2 → grandchild. A live process carrying a
    matching MERCURY_WAVE_ID but unreachable from the parent (reparented,
    double-forked) lands in ``detached`` instead of being dropped.

    ``env_reader`` injects ``{WAVE_ID_KEY: ...}`` per pid (tests); default
    reads /proc/<pid>/environ, but ONLY for in-tree pids plus comm-matching
    candidates, so the per-tick cost stays bounded.
    """
    children_of: Dict[int, List[int]] = {}
    for pid, info in table.items():
        children_of.setdefault(info["ppid"], []).append(pid)

    depth: Dict[int, int] = {parent_pid: 0}
    queue = [parent_pid]
    while queue:
        cur = queue.pop(0)
        for kid in children_of.get(cur, []):
            if kid not in depth:
                depth[kid] = depth[cur] + 1
                queue.append(kid)

    buckets: Dict[str, List[ProcInfo]] = {
        "parent": [], "children": [], "grandchildren": [], "detached": [],
    }
    read_env = env_reader or (
        lambda pid: _proc_environ(pid, [WAVE_ID_KEY, WAVE_NAME_KEY, WAVE_INDEX_KEY])
    )
    # Candidate detached: wave-marked but outside the tree. Only worth the
    # environ read when the comm looks like an agent runtime.
    suspect_comms = ("omp", "node", "bun", "python", "python3", "hermes")
    for pid, info in table.items():
        if pid in depth:
            d = depth[pid]
            env = read_env(pid)
            rec = {
                "pid": pid, "ppid": info["ppid"], "comm": info["comm"],
                "rss_mb": info["rss_mb"], "depth": d,
                "child_id": env.get(WAVE_INDEX_KEY, ""),
                "name": env.get(WAVE_NAME_KEY, ""),
                "cmd": _proc_cmdline(pid) if env_reader is None else "",
            }
            if d == 0:
                buckets["parent"].append(rec)
            elif d == 1:
                buckets["children"].append(rec)
            else:
                buckets["grandchildren"].append(rec)
        elif wave_id and info["comm"] in suspect_comms:
            env = read_env(pid)
            if env.get(WAVE_ID_KEY) == wave_id:
                buckets["detached"].append({
                    "pid": pid, "ppid": info["ppid"], "comm": info["comm"],
                    "rss_mb": info["rss_mb"], "depth": -1,
                    "child_id": env.get(WAVE_INDEX_KEY, ""),
                    "name": env.get(WAVE_NAME_KEY, ""),
                    "cmd": _proc_cmdline(pid) if env_reader is None else "",
                })
    return buckets


# --- parent context + session sizes (all best-effort) --------------------------

def parent_context_bytes(session_id: str, hermes_home: Optional[Path] = None) -> Optional[int]:
    """Byte size of one session's rows in state.db. None when unknowable.

    Read-only open, short timeout, dynamic schema probe (table with a
    session-ish column + a text-ish column) — never raises.
    """
    if not session_id:
        return None
    try:
        base = hermes_home or _hermes_home()
        db = base / "state.db"
        if not db.exists():
            return None
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for tbl in tables:
                if "message" not in tbl.lower():
                    continue
                cols = [(r[1], (r[2] or "").upper())
                        for r in con.execute(f"PRAGMA table_info({tbl})")]
                names = [c for c, _ in cols]
                sess_col = next(
                    (c for c in names
                     if c.lower() in ("session_id", "sessionid", "session",
                                      "conversation_id", "key")),
                    None,
                )
                if sess_col is None:
                    continue
                text_cols = [c for c, t in cols
                             if t in ("TEXT", "CLOB", "") and c != sess_col]
                if not text_cols:
                    continue
                expr = " + ".join(f"length({c})" for c in text_cols)
                row = con.execute(
                    f"SELECT sum({expr}) FROM {tbl} WHERE {sess_col}=?",
                    (session_id,),
                ).fetchone()
                if row and row[0]:
                    return int(row[0])
        finally:
            con.close()
    except Exception:
        pass
    return None


def _hermes_home() -> Path:
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    mh = os.environ.get("MERCURY_HOME", "").strip()
    if mh:
        return Path(mh) / "hermes"
    return Path.home() / ".mercury"


def omp_sessions_bytes(budget_s: float = 1.5) -> Optional[int]:
    """Total bytes under $PI_CODING_AGENT_DIR/sessions. None if unavailable.

    Time-boxed walk — aborts to None rather than stalling a sample tick.
    """
    root = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if not root:
        return None
    deadline = time.monotonic() + budget_s
    total = 0
    try:
        sessions = Path(root) / "sessions"
        if not sessions.is_dir():
            return None
        for dirpath, _dirs, files in os.walk(sessions):
            for fn in files:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                except OSError:
                    continue
            if time.monotonic() > deadline:
                return None
        return total
    except Exception:
        return None


# --- sampler -------------------------------------------------------------------

class WaveProfiler:
    """Background RSS sampler for one delegation wave. None-safe outside."""

    def __init__(self, wave_id: str, parent_pid: Optional[int] = None,
                 parent_session_id: str = "", interval_s: float = 5.0,
                 out_path: Optional[Path] = None):
        self.wave_id = wave_id
        self.parent_pid = parent_pid or os.getpid()
        self.parent_session_id = parent_session_id or ""
        self.interval_s = interval_s
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self._t0 = time.monotonic()
        if out_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = "".join(c if (c.isalnum() or c in "-_") else "_"
                           for c in wave_id)[:48] or "wave"
            out_path = log_dir() / f"wave-{safe}-{stamp}.jsonl"
        self.out_path = Path(out_path)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._payload_bytes = 0
        self._payload_lock = threading.Lock()
        self._grandchild_notes: Dict[str, int] = {}
        self._result_notes = 0

    @property
    def path(self) -> Path:
        return self.out_path

    def start(self) -> "WaveProfiler":
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "header", "v": LOG_VERSION, "wave_id": self.wave_id,
                "parent_pid": self.parent_pid,
                "parent_session_id": self.parent_session_id,
                "interval_s": self.interval_s,
                "started_utc": self.started_utc,
                "mercury_home": str(mercury_home()),
            }) + "\n")
        self._thread = threading.Thread(
            target=self._loop, name=f"wave-mem-{self.wave_id[:16]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def note_result(self, child_id: str, summary: Any) -> None:
        """Record one child result's payload bytes (hypothesis c)."""
        try:
            n = len(summary.encode("utf-8")) if isinstance(summary, str) \
                else len(json.dumps(summary or "", ensure_ascii=False).encode("utf-8"))
        except Exception:
            n = 0
        with self._payload_lock:
            self._payload_bytes += n
            self._result_notes += 1
        try:
            with open(self.out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "result", "t": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": round(time.monotonic() - self._t0, 1),
                    "child_id": str(child_id), "bytes": n,
                }) + "\n")
        except OSError:
            pass

    def note_grandchildren(self, child_id: str, count: int) -> None:
        """Optional: live in-process subagent count per child (hypothesis b).

        omp grandchildren live INSIDE the child process, so RSS cannot split
        them — the count (via RPC get_subagents) is the attribution signal.
        """
        try:
            self._grandchild_notes[str(child_id)] = int(count)
        except (TypeError, ValueError):
            pass

    def sample_once(self) -> Dict[str, Any]:
        table = _read_proc_table()
        buckets = attribute_tree(table, self.parent_pid, self.wave_id)
        ctx_b = parent_context_bytes(self.parent_session_id)
        sess_b = omp_sessions_bytes()
        with self._payload_lock:
            payload_b = self._payload_bytes
        elapsed = round(time.monotonic() - self._t0, 1)

        def _sum(recs: List[ProcInfo]) -> float:
            return round(sum(r["rss_mb"] for r in recs
                             if r["rss_mb"] is not None), 2)

        slim = lambda recs: [  # noqa: E731 — log-shaped projection
            {"pid": r["pid"], "rss_mb": r["rss_mb"], "depth": r["depth"],
             "child_id": r["child_id"], "name": r["name"], "cmd": r["cmd"]}
            for r in sorted(recs, key=lambda r: -(r["rss_mb"] or 0))
        ]
        sample = {
            "type": "sample",
            "t": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": elapsed,
            "parent": {
                "pid": self.parent_pid,
                "rss_mb": next((r["rss_mb"] for r in buckets["parent"]), None),
                "ctx_mb": round(ctx_b / 1048576, 2) if ctx_b is not None else None,
            },
            "children": slim(buckets["children"]),
            "grandchildren": slim(buckets["grandchildren"]),
            "detached": slim(buckets["detached"]),
            "rollup": {
                "parent_rss_mb": _sum(buckets["parent"]),
                "children_rss_mb": _sum(buckets["children"]),
                "grandchildren_rss_mb": _sum(buckets["grandchildren"]),
                "detached_rss_mb": _sum(buckets["detached"]),
                "n_children": len(buckets["children"]),
                "n_grandchildren": len(buckets["grandchildren"]),
                "n_detached": len(buckets["detached"]),
                "grandchild_notes": dict(self._grandchild_notes),
                "payload_mb": round(payload_b / 1048576, 3),
                "n_results": self._result_notes,
                "omp_sessions_mb": (round(sess_b / 1048576, 2)
                                    if sess_b is not None else None),
            },
        }
        return sample

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                sample = self.sample_once()
            except Exception:
                continue
            try:
                with open(self.out_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(sample) + "\n")
            except OSError:
                continue

    def stop(self) -> Optional[Path]:
        self._stop.set()
        # Final sample captures the just-finished state (freed children gone).
        try:
            sample = self.sample_once()
            sample["type"] = "sample"
            final = True
        except Exception:
            sample, final = {}, False
        try:
            with open(self.out_path, "a", encoding="utf-8") as fh:
                if final:
                    fh.write(json.dumps(sample) + "\n")
                fh.write(json.dumps({
                    "type": "footer",
                    "ended_utc": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": round(time.monotonic() - self._t0, 1),
                }) + "\n")
        except OSError:
            return None
        return self.out_path


# --- hook API (what omp_delegation.py calls) ------------------------------------

def maybe_start(wave_id: str, parent_session_id: str = "",
                parent_pid: Optional[int] = None) -> Optional[WaveProfiler]:
    """Start profiling one wave, or None when disabled (default).

    Disabled cost: two getenv calls. No thread, no file, no dir.
    """
    if not profiling_enabled():
        return None
    try:
        return WaveProfiler(
            wave_id=wave_id,
            parent_pid=parent_pid,
            parent_session_id=parent_session_id,
            interval_s=sample_interval(),
        ).start()
    except Exception:
        return None


def stop(prof: Optional[WaveProfiler]) -> Optional[Path]:
    if prof is None:
        return None
    try:
        return prof.stop()
    except Exception:
        return None


def note_result(prof: Optional[WaveProfiler], child_id: str,
                summary: Any) -> None:
    if prof is None:
        return
    try:
        prof.note_result(child_id, summary)
    except Exception:
        pass


# --- log parsing + report -------------------------------------------------------

def iter_records(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def summarize(path: Path) -> Dict[str, Any]:
    """Peak-MB attribution per level across one wave log."""
    header: Dict[str, Any] = {}
    peaks = {"parent_rss_mb": 0.0, "children_rss_mb": 0.0,
             "grandchildren_rss_mb": 0.0, "detached_rss_mb": 0.0,
             "parent_ctx_mb": 0.0, "payload_mb": 0.0}
    peak_n = {"n_children": 0, "n_grandchildren": 0, "n_detached": 0}
    child_peak: Dict[str, float] = {}
    child_first_half: List[float] = []
    child_second_half: List[float] = []
    first_parent_rss: Optional[float] = None
    last_parent_rss: Optional[float] = None
    n_samples = 0
    footer: Dict[str, Any] = {}
    for rec in iter_records(Path(path)):
        rtype = rec.get("type")
        if rtype == "header":
            header = rec
        elif rtype == "sample":
            n_samples += 1
            roll = rec.get("rollup", {})
            for k in peaks:
                v = roll.get(k)
                if isinstance(v, (int, float)):
                    peaks[k] = max(peaks[k], float(v))
            for k in peak_n:
                v = roll.get(k)
                if isinstance(v, int):
                    peak_n[k] = max(peak_n[k], v)
            prss = rec.get("parent", {}).get("rss_mb")
            if isinstance(prss, (int, float)):
                if first_parent_rss is None:
                    first_parent_rss = float(prss)
                last_parent_rss = float(prss)
            half = child_first_half if n_samples % 2 == 1 else child_second_half
            for c in rec.get("children", []):
                rss = c.get("rss_mb")
                if isinstance(rss, (int, float)):
                    key = str(c.get("child_id") or c.get("pid"))
                    child_peak[key] = max(child_peak.get(key, 0.0), float(rss))
                    half.append(float(rss))
        elif rtype == "result":
            b = rec.get("bytes")
            if isinstance(b, (int, float)):
                peaks["payload_mb"] = max(
                    peaks["payload_mb"], float(b) / 1048576)
                # payload peak = cumulative; recompute from rollup instead —
                # handled below via max of rollup payload_mb (already above).
        elif rtype == "footer":
            footer = rec
    total_peak = round(peaks["parent_rss_mb"] + peaks["children_rss_mb"]
                       + peaks["grandchildren_rss_mb"] + peaks["detached_rss_mb"], 2)
    per_child_max = round(max(child_peak.values()), 2) if child_peak else 0.0
    first_avg = (sum(child_first_half) / len(child_first_half)
                 if child_first_half else 0.0)
    second_avg = (sum(child_second_half) / len(child_second_half)
                  if child_second_half else 0.0)
    return {
        "wave_id": header.get("wave_id", ""),
        "n_samples": n_samples,
        "elapsed_s": footer.get("elapsed_s"),
        "peak": {k: round(v, 2) for k, v in peaks.items()},
        "peak_counts": peak_n,
        "total_proc_peak_mb": total_peak,
        "per_child_max_mb": per_child_max,
        "child_rss_first_half_avg_mb": round(first_avg, 2),
        "child_rss_second_half_avg_mb": round(second_avg, 2),
        "child_rss_growth_mb": round(second_avg - first_avg, 2),
        "parent_rss_first_mb": first_parent_rss,
        "parent_rss_last_mb": last_parent_rss,
    }


def format_report(summary: Dict[str, Any]) -> str:
    p = summary["peak"]
    c = summary["peak_counts"]
    lines = [
        f"wave {summary.get('wave_id') or '?'}: "
        f"{summary['n_samples']} samples",
        f"  parent RSS peak      : {p['parent_rss_mb']:>9.1f} MB"
        f"   (ctx peak {p['parent_ctx_mb']:.1f} MB in state.db)",
        f"  children RSS peak    : {p['children_rss_mb']:>9.1f} MB"
        f"   (max live {c['n_children']}, per-child max {summary['per_child_max_mb']:.0f} MB)",
        f"  grandchildren peak   : {p['grandchildren_rss_mb']:>9.1f} MB"
        f"   (max live {c['n_grandchildren']})",
        f"  detached wave procs  : {p['detached_rss_mb']:>9.1f} MB"
        f"   (max {c['n_detached']})",
        f"  payloads (results)   : {p['payload_mb']:>9.1f} MB cumulative",
        f"  total proc peak      : {summary['total_proc_peak_mb']:>9.1f} MB",
        f"  child RSS drift      : {summary['child_rss_growth_mb']:+.1f} MB"
        " (2nd-half avg − 1st-half avg; + supports hypothesis (a) accumulation)",
    ]
    shares = sorted(
        (("children", p["children_rss_mb"]),
         ("grandchildren", p["grandchildren_rss_mb"]),
         ("parent RSS", p["parent_rss_mb"]),
         ("parent ctx", p["parent_ctx_mb"]),
         ("payloads", p["payload_mb"])),
        key=lambda kv: -kv[1],
    )
    top = shares[0][0] if shares and shares[0][1] > 0 else "nothing measurable"
    hint = {
        "children": "(b?) many live children — check N x per-child vs flat 0.7GB",
        "grandchildren": "(b) grandchild fan-out inside children",
        "parent RSS": "(d?) gateway/parent growth — compare first vs last parent RSS",
        "parent ctx": "(c) parent transcript accumulation in state.db",
        "payloads": "(c) tool-output payloads re-entering as one message",
        "nothing measurable": "no samples captured (wave shorter than interval?)",
    }[top]
    lines.append(f"  largest contributor  : {top} {hint}")
    if (summary.get("parent_rss_first_mb") is not None
            and summary.get("parent_rss_last_mb") is not None):
        lines.append(
            f"  parent RSS first→last: {summary['parent_rss_first_mb']:.0f} → "
            f"{summary['parent_rss_last_mb']:.0f} MB")
    return "\n".join(lines)


def _cmd_report(args: argparse.Namespace) -> int:
    summary = summarize(Path(args.log))
    print(format_report(summary))
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Manual attach: sample an already-running wave parent pid."""
    prof = WaveProfiler(
        wave_id=args.wave_id, parent_pid=args.pid,
        parent_session_id=args.session or "",
        interval_s=args.interval, out_path=Path(args.out) if args.out else None,
    ).start()
    print(f"profiling wave {args.wave_id} (parent pid {args.pid}) → {prof.path}",
          flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        out = prof.stop()
    print(f"stopped → {out}")
    print(format_report(summarize(out)))
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    d = log_dir()
    if not d.is_dir():
        print(f"no logs ({d} missing)")
        return 0
    for p in sorted(d.glob("wave-*.jsonl"), key=lambda p: p.stat().st_mtime):
        print(f"{p.stat().st_size / 1048576:7.1f} MB  {p}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Wave-time memory profiler: sample or report.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="attribute peak MB per level from a log")
    r.add_argument("log", help="path to a wave-*.jsonl log")
    r.add_argument("--json", action="store_true",
                   help="also dump the summary dict as JSON")
    r.set_defaults(fn=_cmd_report)
    w = sub.add_parser("watch", help="manually profile a running wave parent")
    w.add_argument("--pid", type=int, required=True)
    w.add_argument("--wave-id", required=True)
    w.add_argument("--session", default="")
    w.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    w.add_argument("--out", default="")
    w.set_defaults(fn=_cmd_watch)
    li = sub.add_parser("list", help="list wave logs under the Mercury home")
    li.set_defaults(fn=_cmd_list)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
