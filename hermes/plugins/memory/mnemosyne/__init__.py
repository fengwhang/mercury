"""Mnemosyne memory plugin — Hermes MemoryProvider interface.

Canonical plugin identifier: ``hermes-mnemosyne`` (NOT ``mnemosyne``).
Upstream enable (stock Hermes)::

    hermes plugins enable hermes-mnemosyne --no-allow-tool-override
    hermes config set memory.provider mnemosyne

Mercury equivalent: ``mercury memory setup`` → pick ``mnemosyne`` (the
memory-setup path never grants built-in-tool override, so the
``--no-allow-tool-override`` posture holds structurally), or
``mercury config set memory.provider mnemosyne``. Activation key is
``memory.provider: mnemosyne`` — the same string upstream uses.

Shared bank: hermes + omp share ONE SQLite file at
``~/.mercury/memories/mnemopi.db`` (explicit ``dbPath`` pin on both sides,
``scoping: global`` so no per-project sibling DBs split the bank). The omp
side selects it via ``memory.backend: mnemopi`` (its renamed backend id);
this provider selects it via ``memory.provider: mnemosyne``. Same file,
both engines.

Persistence model (Mercury rebuilds ``hermes/.venv`` on updates, like the
canonical doc's Docker case — the venv is replaceable, the repo is not):
this module is the persistent wrapper. It is in-tree repo code with NO
hard third-party imports (stdlib ``sqlite3`` only), so a venv rebuild can
never take the provider with it. Optional heavy deps
(``mnemosyne-memory[embeddings]`` via ``mnemosyne-hermes``) live in a
persistent side venv (``$MERCURY_HOME/venvs/mnemosyne``); the bootstrap
below prepends that side venv's ``site-packages`` to ``sys.path`` when it
exists — the same mechanism as canonical persistent side-venv wrapper
mode. Recall is FTS-first in every configuration: FTS5 + importance
ranking always works; embeddings only add signal when installed.

Concurrency with the TS side (``omp/packages/mnemopi/src/db.ts``
``enablePragmas``): every operation opens a short-lived connection with
``PRAGMA foreign_keys=ON``, ``PRAGMA busy_timeout=5000`` and
``PRAGMA journal_mode=WAL``, then closes it. No long-lived handles, so an
omp child and hermes can write the same bank without ``database is
locked`` corruption.

Canonical reference: mnemosyne `docs/hermes-integration.md` (e29909c).
Mercury default install profile is ``[embeddings]`` (local fastembed
vectors, single-user desktop); never ``[all]``/ctransformers unless the
user opts in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from tools.registry import tool_error

logger = logging.getLogger(__name__)

PROVIDER_NAME = "mnemosyne"
PLUGIN_IDENTIFIER = "hermes-mnemosyne"
SHARED_BANK_FILENAME = "mnemopi.db"
DEFAULT_BANK_NAME = "default"

_MNEMOSYNE_GLYPH = "🌀"

RECALL_SCHEMA = {
    "name": "mnemosyne_recall",
    "description": (
        "Search the shared Mnemosyne memory bank (working + episodic memory, "
        "FTS-first ranked by importance). Use before answering questions "
        "about the user, past decisions, or project conventions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 5, max: 20)."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "mnemosyne_remember",
    "description": (
        "Store a fact the user would expect you to remember into the shared "
        "Mnemosyne bank (visible to both hermes and omp)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Fact content to store."},
            "importance": {
                "type": "number",
                "description": "Importance 0..1 (default: 0.5).",
            },
        },
        "required": ["content"],
    },
}

# Small stopword set for FTS query shaping (mirrors the TS recall path's
# intent; the bank's FTS index holds the full content either way).
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "i", "in", "is", "it", "of", "on", "or", "that", "the",
    "this", "to", "was", "what", "when", "where", "with",
})


# ---------------------------------------------------------------------------
# Persistent side-venv bootstrap (wrapper mode)
# ---------------------------------------------------------------------------

def _side_venv_candidates() -> List[Path]:
    """Persistent homes for the optional heavy deps (outside hermes/.venv)."""
    homes: List[Path] = []
    for raw in (os.environ.get("MERCURY_HOME", ""), os.path.join(os.path.expanduser("~"), ".mercury")):
        if raw:
            homes.append(Path(raw) / "venvs" / "mnemosyne")
    env_venv = os.environ.get("MNEMOSYNE_VENV", "").strip()
    if env_venv:
        homes.append(Path(env_venv))
    return homes


def _bootstrap_side_venv() -> Optional[str]:
    """Prepend the persistent side venv's site-packages to sys.path.

    Best-effort: returns the site-packages dir when added, else None.
    Never raises — the stdlib FTS path works without it.
    """
    try:
        for venv in _side_venv_candidates():
            lib = venv / "lib"
            if not lib.is_dir():
                continue
            for site in sorted(lib.glob("python3*/site-packages")):
                if site.is_dir() and str(site) not in sys.path:
                    sys.path.insert(0, str(site))
                    return str(site)
    except Exception as exc:
        logger.debug("Mnemosyne side-venv bootstrap skipped: %s", exc)
    return None


_bootstrap_side_venv()


# ---------------------------------------------------------------------------
# Bank path resolution
# ---------------------------------------------------------------------------

def _mercury_home(hermes_home: str = "") -> str:
    """Resolve the Mercury home (~/.mercury) holding the shared bank."""
    env_home = os.environ.get("MERCURY_HOME", "").strip()
    if env_home:
        return env_home
    if hermes_home:
        # Under the launcher HERMES_HOME=$MERCURY_HOME/hermes.
        if Path(hermes_home).name == "hermes":
            return str(Path(hermes_home).parent)
        return hermes_home
    return os.path.join(os.path.expanduser("~"), ".mercury")


_ENSURED_BANKS: set = set()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def resolve_bank_path(hermes_home: str = "", config: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the ONE shared bank file both engines use."""
    env_db = os.environ.get("MNEMOSYNE_DB_PATH", "").strip()
    if env_db:
        return os.path.expanduser(env_db)
    cfg = config or {}
    mem_cfg = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
    mn_cfg = mem_cfg.get("mnemosyne", {}) if isinstance(mem_cfg, dict) else {}
    if isinstance(mn_cfg, dict):
        for key in ("db_path", "dbPath"):
            val = mn_cfg.get(key, "")
            if isinstance(val, str) and val.strip():
                return os.path.expanduser(val.strip())
    # Same pin the omp side renders (omp.mnemopi.dbPath in the unified file).
    # Raw read: the merged config view extracts only the hermes subtree.
    try:
        raw = _read_unified_raw()
        omp_node = raw.get("omp", {}) if isinstance(raw, dict) else {}
        mn_node = omp_node.get("mnemopi", {}) if isinstance(omp_node, dict) else {}
        omp_db = mn_node.get("dbPath", "") if isinstance(mn_node, dict) else ""
        if isinstance(omp_db, str) and omp_db.strip():
            return os.path.expanduser(omp_db.strip())
    except Exception:
        pass
    return os.path.join(_mercury_home(hermes_home), "memories", SHARED_BANK_FILENAME)


# ---------------------------------------------------------------------------
# SQLite access (mirrors omp db.ts enablePragmas + BeamMemory schema)
# ---------------------------------------------------------------------------

def open_bank(path: str) -> sqlite3.Connection:
    """Open the shared bank with the TS side's concurrency pragmas."""
    expanded = os.path.expanduser(path)
    parent = os.path.dirname(expanded)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(expanded, timeout=5.0, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error as exc:
        logger.debug("Mnemosyne pragma setup failed on %s: %s", expanded, exc)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create BeamMemory tables/FTS/triggers when missing (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS working_memory (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            embed_text TEXT DEFAULT NULL,
            source TEXT,
            timestamp TEXT,
            session_id TEXT DEFAULT 'default',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT,
            veracity TEXT DEFAULT 'unknown',
            memory_type TEXT DEFAULT 'unknown',
            consolidated_at TEXT,
            recall_count INTEGER DEFAULT 0,
            last_recalled TIMESTAMP DEFAULT NULL,
            valid_until TIMESTAMP DEFAULT NULL,
            superseded_by TEXT DEFAULT NULL,
            scope TEXT DEFAULT 'global',
            author_id TEXT DEFAULT NULL,
            author_type TEXT DEFAULT NULL,
            channel_id TEXT DEFAULT NULL,
            trust_tier TEXT DEFAULT 'STATED',
            validator TEXT DEFAULT NULL,
            validated_at TIMESTAMP DEFAULT NULL,
            validation_count INTEGER DEFAULT 0,
            event_date TEXT DEFAULT NULL,
            event_date_precision TEXT DEFAULT 'unknown',
            temporal_tags TEXT DEFAULT '[]',
            corrected_by INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodic_memory (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            source TEXT,
            timestamp TEXT,
            session_id TEXT DEFAULT 'default',
            importance REAL DEFAULT 0.5,
            metadata_json TEXT,
            summary_of TEXT DEFAULT '',
            veracity TEXT DEFAULT 'unknown',
            tier INTEGER DEFAULT 1,
            degraded_at TEXT,
            memory_type TEXT DEFAULT 'unknown',
            binary_vector BLOB,
            recall_count INTEGER DEFAULT 0,
            last_recalled TIMESTAMP DEFAULT NULL,
            valid_until TIMESTAMP DEFAULT NULL,
            superseded_by TEXT DEFAULT NULL,
            scope TEXT DEFAULT 'global',
            author_id TEXT DEFAULT NULL,
            author_type TEXT DEFAULT NULL,
            channel_id TEXT DEFAULT NULL,
            trust_tier TEXT DEFAULT 'STATED',
            validator TEXT DEFAULT NULL,
            validated_at TIMESTAMP DEFAULT NULL,
            validation_count INTEGER DEFAULT 0,
            event_date TEXT DEFAULT NULL,
            event_date_precision TEXT DEFAULT 'unknown',
            temporal_tags TEXT DEFAULT '[]',
            corrected_by INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_episodes USING fts5("
        "content, content='episodic_memory', content_rowid='rowid')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_working USING fts5("
        "id UNINDEXED, content)"
    )
    # Convergent migrations FIRST: trigger bodies reference these columns at
    # INSERT time, so they must exist before any trigger fires — whichever
    # engine (or mnemosyne version) created the bank. embed_text is an
    # omp-vendored column real-core 3.15.1 lacks; adding it is safe (all
    # sides use explicit column lists, never SELECT * positionally).
    # Mirrors real-core _add_column_if_missing (duplicate-safe both ways).
    _add_column_if_missing(conn, "working_memory", "embed_text", "TEXT DEFAULT NULL")
    for _table in ("working_memory", "episodic_memory"):
        _add_column_if_missing(conn, _table, "scope", "TEXT DEFAULT 'global'")
        _add_column_if_missing(conn, _table, "valid_until", "TIMESTAMP DEFAULT NULL")
        _add_column_if_missing(conn, _table, "superseded_by", "TEXT DEFAULT NULL")
        _add_column_if_missing(conn, _table, "memory_type", "TEXT DEFAULT 'unknown'")
        _add_column_if_missing(conn, _table, "veracity", "TEXT DEFAULT 'unknown'")
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS em_ai AFTER INSERT ON episodic_memory BEGIN
            INSERT INTO fts_episodes(rowid, content) VALUES (new.rowid, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS em_ad AFTER DELETE ON episodic_memory BEGIN
            INSERT INTO fts_episodes(fts_episodes, rowid, content)
            VALUES ('delete', old.rowid, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS em_au AFTER UPDATE ON episodic_memory BEGIN
            INSERT INTO fts_episodes(fts_episodes, rowid, content)
            VALUES ('delete', old.rowid, old.content);
            INSERT INTO fts_episodes(rowid, content) VALUES (new.rowid, new.content);
        END;
        DROP TRIGGER IF EXISTS wm_ai;
        CREATE TRIGGER IF NOT EXISTS wm_ai AFTER INSERT ON working_memory BEGIN
            INSERT INTO fts_working(id, content)
            VALUES (new.id, COALESCE(new.embed_text, new.content));
        END;
        CREATE TRIGGER IF NOT EXISTS wm_ad AFTER DELETE ON working_memory BEGIN
            DELETE FROM fts_working WHERE id = old.id;
        END;
        DROP TRIGGER IF EXISTS wm_au;
        CREATE TRIGGER IF NOT EXISTS wm_au AFTER UPDATE OF content, embed_text ON working_memory BEGIN
            DELETE FROM fts_working WHERE id = old.id;
            INSERT INTO fts_working(id, content)
            VALUES (new.id, COALESCE(new.embed_text, new.content));
        END;
        """
    )
def ensure_bank(path: str) -> str:
    """Open + converge the bank once per path per process (DDL cached)."""
    expanded = os.path.expanduser(path)
    if expanded in _ENSURED_BANKS:
        return expanded
    conn = open_bank(expanded)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    _ENSURED_BANKS.add(expanded)
    return expanded


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fts_match(query: str) -> str:
    """Shape a user query into an FTS5 OR-of-phrases MATCH string."""
    tokens = [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if t not in _STOP_WORDS]
    if not tokens:
        tokens = re.findall(r"[a-z0-9_]+", query.lower())
    phrases = ['"' + t.replace('"', '""') + '"' for t in tokens[:12]]
    return " OR ".join(phrases) if phrases else '""'


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _normalize_ranks(rows: List[tuple], key_idx: int, rank_idx: int) -> Dict[Any, float]:
    if not rows:
        return {}
    ranks = [float(r[rank_idx]) for r in rows]
    lo, hi = min(ranks), max(ranks)
    span = (hi - lo) or 1.0
    return {r[key_idx]: 1.0 - (float(r[rank_idx]) - lo) / span for r in rows}


def _clamp01(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def recall_rows(conn: sqlite3.Connection, query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """FTS-first recall across working + episodic memory (TS rank semantics)."""
    limit = max(1, min(int(limit or 5), 20))
    now = _utc_now()
    hits: Dict[str, Dict[str, Any]] = {}
    fts_norm: Dict[str, float] = {}

    def _fetch(table: str, ids: List[str]) -> None:
        if not ids:
            return
        cols = "id, content, source, timestamp, session_id, importance, memory_type"
        chunk = 500
        for off in range(0, len(ids), chunk):
            part = ids[off:off + chunk]
            q = f"SELECT {cols} FROM {table} WHERE id IN ({','.join('?' * len(part))})"
            for row in conn.execute(q, part).fetchall():
                rid = row[0]
                if rid not in hits:
                    hits[rid] = {
                        "id": rid, "content": row[1] or "", "source": row[2] or "",
                        "timestamp": row[3] or "", "session_id": row[4] or "",
                        "importance": _clamp01(row[5]), "memory_type": row[6] or "",
                        "tier": "working" if table == "working_memory" else "episodic",
                    }

    try:
        if _table_exists(conn, "fts_working") and _table_exists(conn, "working_memory"):
            wrows = conn.execute(
                "SELECT f.id, f.rank FROM fts_working f "
                "WHERE f.fts_working MATCH ? "
                "AND EXISTS (SELECT 1 FROM working_memory w WHERE w.id = f.id "
                "AND w.superseded_by IS NULL "
                "AND (w.valid_until IS NULL OR w.valid_until > ?)) "
                "ORDER BY f.rank, f.id LIMIT ?",
                (_fts_match(query), now, limit * 4),
            ).fetchall()
            fts_norm.update(_normalize_ranks(wrows, 0, 1))
            _fetch("working_memory", [r[0] for r in wrows])
        if _table_exists(conn, "fts_episodes") and _table_exists(conn, "episodic_memory"):
            erows = conn.execute(
                "SELECT f.rowid, f.rank FROM fts_episodes f "
                "WHERE f.fts_episodes MATCH ? "
                "AND EXISTS (SELECT 1 FROM episodic_memory e WHERE e.rowid = f.rowid "
                "AND e.superseded_by IS NULL "
                "AND (e.valid_until IS NULL OR e.valid_until > ?)) "
                "ORDER BY f.rank, f.rowid LIMIT ?",
                (_fts_match(query), now, limit * 4),
            ).fetchall()
            enorm = _normalize_ranks(erows, 0, 1)
            if erows:
                eids = conn.execute(
                    f"SELECT id FROM episodic_memory WHERE rowid IN "
                    f"({','.join('?' * len(erows))})",
                    [r[0] for r in erows],
                ).fetchall()
                rowid_to_id = {r[0]: e[0] for r, e in zip(erows, eids)}
                for r in erows:
                    eid = rowid_to_id.get(r[0])
                    if eid and eid not in fts_norm:
                        fts_norm[eid] = enorm.get(r[0], 0.0)
                _fetch("episodic_memory", [e[0] for e in eids])
    except sqlite3.OperationalError as exc:
        # Missing FTS tables or malformed MATCH: fall back to LIKE.
        logger.debug("Mnemosyne FTS recall fell back to LIKE: %s", exc)
        hits.clear()
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for table in ("working_memory", "episodic_memory"):
            try:
                for row in conn.execute(
                    f"SELECT id, content, source, timestamp, session_id, importance, memory_type "
                    f"FROM {table} WHERE content LIKE ? ESCAPE '\\' "
                    f"AND superseded_by IS NULL AND (valid_until IS NULL OR valid_until > ?) "
                    f"ORDER BY importance DESC LIMIT ?",
                    (like, now, limit * 4),
                ).fetchall():
                    if row[0] not in hits:
                        hits[row[0]] = {
                            "id": row[0], "content": row[1] or "", "source": row[2] or "",
                            "timestamp": row[3] or "", "session_id": row[4] or "",
                            "importance": _clamp01(row[5]), "memory_type": row[6] or "",
                            "tier": "working" if table == "working_memory" else "episodic",
                        }
            except sqlite3.Error:
                continue

    scored = []
    for rid, hit in hits.items():
        norm = fts_norm.get(rid, 0.0)
        scored.append((0.7 * norm + 0.3 * hit["importance"], hit))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [hit for _, hit in scored[:limit]]


def _clip(text: str, limit: int = 500) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def insert_working(
    conn: sqlite3.Connection,
    content: str,
    *,
    source: str = "hermes",
    session_id: str = "default",
    importance: float = 0.5,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert one working-memory row (FTS trigger mirrors it)."""
    mid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO working_memory (id, content, source, timestamp, session_id, "
        "importance, metadata_json, scope) VALUES (?, ?, ?, ?, ?, ?, ?, 'global')",
        (
            mid, content, source, _utc_now(), session_id or "default",
            _clamp01(importance),
            json.dumps(metadata or {}) if metadata else None,
        ),
    )
    conn.commit()
    return mid


# ---------------------------------------------------------------------------
# Status probe (mnemosyne-hermes status + hermes memory status equivalents)
# ---------------------------------------------------------------------------

def mnemosyne_status_summary(bank_path: str = "") -> Dict[str, Any]:
    """Read-only health probe. Never raises, never imports heavy deps."""
    import importlib.util

    path = os.path.expanduser(bank_path) if bank_path else resolve_bank_path()
    summary: Dict[str, Any] = {
        "provider": PROVIDER_NAME,
        "plugin": PLUGIN_IDENTIFIER,
        "bank_path": path,
        "bank_exists": os.path.isfile(path),
        "bank_bytes": 0,
        "fts5_available": False,
        "package_present": importlib.util.find_spec("mnemosyne_hermes") is not None,
        "embeddings_present": importlib.util.find_spec("fastembed") is not None,
        "side_venv": None,
    }
    for venv in _side_venv_candidates():
        if (venv / "bin" / "python").exists() or (venv / "Scripts" / "python.exe").exists():
            summary["side_venv"] = str(venv)
            break
    if summary["bank_exists"]:
        try:
            summary["bank_bytes"] = os.path.getsize(path)
        except OSError:
            pass
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(content)")
            summary["fts5_available"] = True
        finally:
            probe.close()
    except sqlite3.Error:
        summary["fts5_available"] = False
    return summary


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class MnemosyneMemoryProvider(MemoryProvider):
    """Shared-bank memory: hermes reads/writes the omp bank file directly."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = dict(config or {})
        self._bank_path = ""
        self._session_id = "default"
        self._mercury_home = ""
        self._writes_paused = False
        self._last_count = 0

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        try:
            import sqlite3  # noqa: F401
            return True
        except ImportError:
            return False

    def unavailable_reason(self) -> str:
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or "default"
        mercury_home = str(kwargs.get("mercury_home", "") or "")
        if not mercury_home:
            try:
                from mercury_constants import get_hermes_home

                mercury_home = str(get_hermes_home())
            except Exception:
                mercury_home = ""
        self._mercury_home = _mercury_home(mercury_home)
        self._bank_path = resolve_bank_path(mercury_home, self._config)
        # Cron/flush contexts must not let system prompts pollute the bank.
        self._writes_paused = kwargs.get("agent_context", "primary") in ("cron", "flush")
        try:
            ensure_bank(self._bank_path)
        except Exception as exc:
            logger.debug("Mnemosyne bank init degraded (%s): %s", self._bank_path, exc)

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            path = self._bank_path or resolve_bank_path()
            if path not in _ENSURED_BANKS:
                ensure_bank(path)
            return open_bank(path)
        except Exception as exc:
            logger.debug("Mnemosyne connect failed: %s", exc)
            return None

    def system_prompt_block(self) -> str:
        conn = self._connect()
        if conn is None:
            return ""
        try:
            total = conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]
        except Exception:
            conn.close()
            return ""
        conn.close()
        if not total:
            return (
                "# Mnemosyne Memory\n"
                "Active on the shared bank (hermes + omp). Empty — proactively "
                "remember facts the user would expect you to retain with "
                "mnemosyne_remember."
            )
        return (
            "# Mnemosyne Memory\n"
            f"Active on the shared bank (hermes + omp): {total} working memories. "
            "Use mnemosyne_recall before answering about the user, past "
            "decisions, or project conventions."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or is_trivial_prompt(query):
            self._last_count = 0
            return ""
        conn = self._connect()
        if conn is None:
            self._last_count = 0
            return ""
        try:
            hits = recall_rows(conn, query, limit=5)
        except Exception as exc:
            logger.debug("Mnemosyne prefetch failed: %s", exc)
            hits = []
        finally:
            conn.close()
        self._last_count = len(hits)
        if not hits:
            return ""
        lines = [f"- {_clip(str(h.get('content', '')))}" for h in hits]
        return "## Mnemosyne Memory\n" + "\n".join(lines)

    def recall_status(self) -> Optional[RecallStatus]:
        if self._last_count <= 0:
            return None
        return RecallStatus(
            provider_label="mnemosyne", count=self._last_count, glyph=_MNEMOSYNE_GLYPH
        )

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        # Retention flows through on_memory_write (built-in memory mirror),
        # never raw turn capture — same contract as the holographic provider.
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Namespaced mnemosyne_* tools: never override built-in memory tools
        # (canonical --no-allow-tool-override posture).
        return [RECALL_SCHEMA, REMEMBER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "mnemosyne_recall":
            query = str((args or {}).get("query", "") or "")
            if not query:
                return tool_error("query is required")
            try:
                top_k = min(max(int((args or {}).get("top_k", 5)), 1), 20)
            except (TypeError, ValueError):
                top_k = 5
            conn = self._connect()
            if conn is None:
                return tool_error("shared bank unavailable")
            try:
                hits = recall_rows(conn, query, limit=top_k)
            except Exception as exc:
                return tool_error(str(exc))
            finally:
                conn.close()
            return json.dumps({"results": [
                {"id": h["id"], "content": h["content"], "importance": h["importance"],
                 "tier": h["tier"], "timestamp": h["timestamp"]}
                for h in hits
            ]})
        if tool_name == "mnemosyne_remember":
            content = str((args or {}).get("content", "") or "").strip()
            if not content:
                return tool_error("content is required")
            try:
                importance = _clamp01((args or {}).get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            mid = self._remember(content, importance=importance, source="tool")
            if not mid:
                return tool_error("shared bank unavailable")
            return json.dumps({"id": mid, "stored": True})
        return tool_error(f"Unknown tool: {tool_name}")

    def _remember(
        self,
        content: str,
        *,
        importance: float = 0.5,
        source: str = "hermes",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        if self._writes_paused or not content:
            return ""
        conn = self._connect()
        if conn is None:
            return ""
        try:
            ensure_schema(conn)
            return insert_working(
                conn, content, source=source,
                session_id=self._session_id, importance=importance,
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("Mnemosyne remember failed: %s", exc)
            return ""
        finally:
            conn.close()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes into shared working memory."""
        if self._writes_paused or not content:
            return
        try:
            if action in ("add", "replace"):
                self._remember(
                    content, source="hermes",
                    metadata={"target": target, **(metadata or {})},
                )
            elif action == "remove":
                conn = self._connect()
                if conn is None:
                    return
                try:
                    conn.execute(
                        "DELETE FROM working_memory WHERE content = ? OR id = ?",
                        (content, content),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            logger.debug("Mnemosyne memory_write mirror failed: %s", exc)

    def shutdown(self) -> None:
        # Short-lived connections per operation: nothing to flush.
        self._last_count = 0

    def get_config_schema(self) -> List[Dict[str, Any]]:
        default_db = os.path.join(_mercury_home(), "memories", SHARED_BANK_FILENAME)
        return [
            {
                "key": "db_path",
                "description": "Shared SQLite bank path (same file omp uses)",
                "default": self._config.get("db_path", default_db),
            },
            {
                "key": "bank",
                "description": "Shared bank name (global scoping: one bank)",
                "default": self._config.get("bank", DEFAULT_BANK_NAME),
            },
        ]

    def save_config(self, values: Dict[str, Any], mercury_home: str) -> None:
        # config.yaml is canonical (memory_setup persists memory.mnemosyne
        # itself); no native file to write, no env vars to set.
        return None

    def backup_paths(self) -> List[str]:
        # The bank lives at $MERCURY_HOME/memories (outside $HERMES_HOME),
        # so backup/import must capture it explicitly. No init, no network.
        try:
            path = resolve_bank_path()
        except Exception:
            return []
        return [path] if os.path.isfile(path) else []

# ---------------------------------------------------------------------------
# Shared-bank preflight (unification gate for setup)
# ---------------------------------------------------------------------------

# Omp embedding variants (coding-agent settings-schema mnemopi.*).
OMP_VARIANT_MODELS = {
    "en": ("BAAI/bge-base-en-v1.5", 768),
    "multilingual": ("intfloat/multilingual-e5-large", 1024),
}
# Hermes real-lib default (mnemosyne/core/embeddings.py _DEFAULT_MODEL).
HERMES_DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
# Known model -> dims (subset of the real dims table + omp variants).
# Unknown models resolve via MNEMOSYNE_EMBEDDING_DIM, else loud-unknown.
KNOWN_EMBEDDING_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "intfloat/multilingual-e5-small": 384,
    "intfloat/multilingual-e5-base": 768,
    "intfloat/multilingual-e5-large": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "openai/text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}

_REQUIRED_TABLES = ("working_memory", "episodic_memory", "fts_working", "fts_episodes")
_REQUIRED_WORKING_COLUMNS = ("id", "content", "scope", "valid_until", "superseded_by")


def _unified_config_path() -> str:
    explicit = os.environ.get("MERCURY_CONFIG", "").strip()
    if explicit:
        return explicit
    home = os.environ.get("MERCURY_HOME", "").strip()
    if home:
        return os.path.join(home, "config.yaml")
    try:
        from mercury_constants import get_hermes_home

        return str(Path(get_hermes_home()) / "config.yaml")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mercury", "config.yaml")


def _read_unified_raw() -> Dict[str, Any]:
    """Parse the unified config file raw (omp: subtree included)."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return {}
    try:
        import yaml as _yaml

        with open(_unified_config_path(), encoding="utf-8") as fh:
            data = _yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _omp_effective_settings(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Effective omp memory settings (explicit, else bridge/schema defaults)."""
    raw = raw if isinstance(raw, dict) else _read_unified_raw()
    omp = raw.get("omp", {}) if isinstance(raw, dict) else {}
    if not isinstance(omp, dict):
        omp = {}
    memory = omp.get("memory", {}) if isinstance(omp.get("memory"), dict) else {}
    mn = omp.get("mnemopi", {}) if isinstance(omp.get("mnemopi"), dict) else {}
    has_omp_block = "omp" in raw if isinstance(raw, dict) else False

    def _str(key: str) -> str:
        val = mn.get(key, "")
        return val.strip().strip(chr(39) + chr(34)) if isinstance(val, str) else ""

    def _bool(key: str) -> Optional[bool]:
        val = mn.get(key, None)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            low = val.strip().lower()
            if low in ("true", "yes", "on", "1"):
                return True
            if low in ("false", "no", "off", "0"):
                return False
        return None

    backend = memory.get("backend", "") if isinstance(memory, dict) else ""
    if isinstance(backend, bool):
        # YAML 1.1: `off`/`no` parse to False, `on`/`yes` to True.
        backend = "off" if not backend else "on"
    elif isinstance(backend, str):
        backend = backend.strip().strip(chr(39) + chr(34))
    else:
        backend = ""
    if backend == "mnemosyne":
        backend = "mnemopi"
    if not backend:
        # Omp schema default is off (bridge pins mnemopi explicitly).
        backend = "off"
    if not has_omp_block:
        # Bridge has not rendered yet: it will pin the shared-bank default.
        return {
            "backend": "mnemopi", "bank": "default", "scoping": "global",
            "noEmbeddings": True, "embeddingModel": "", "embeddingVariant": "en",
            "dbPath": "", "will_default": True,
        }
    variant = _str("embeddingVariant") or "en"
    variant_model, variant_dims = OMP_VARIANT_MODELS.get(variant, OMP_VARIANT_MODELS["en"])
    model = _str("embeddingModel") or variant_model
    no_emb = _bool("noEmbeddings")
    return {
        "backend": backend,
        "bank": _str("bank") or "default",
        "scoping": _str("scoping") or "global",
        # Omp schema default is embeddings ON; only an explicit true is FTS-only.
        "noEmbeddings": no_emb if no_emb is not None else False,
        "embeddingModel": model,
        "embeddingDims": _dims_for(model),
        "embeddingVariant": variant,
        "dbPath": _str("dbPath"),
        "will_default": False,
    }


def _dims_for(model: str) -> Optional[int]:
    """Known dims for a model, else env override, else None (loud-unknown)."""
    if not model:
        return None
    if model in KNOWN_EMBEDDING_DIMS:
        return KNOWN_EMBEDDING_DIMS[model]
    env_dim = os.environ.get("MNEMOSYNE_EMBEDDING_DIM", "").strip()
    if env_dim:
        try:
            return int(env_dim)
        except (TypeError, ValueError):
            pass
    return None


def _hermes_embedding_posture(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Hermes-side embedding posture (this provider never writes vectors)."""
    model = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "").strip()
    explicit = bool(model)
    if not model and isinstance(config, dict):
        mem = config.get("memory", {})
        mn = mem.get("mnemosyne", {}) if isinstance(mem, dict) else {}
        if isinstance(mn, dict):
            for key in ("embedding_model", "embeddingModel"):
                val = mn.get(key, "")
                if isinstance(val, str) and val.strip():
                    model = val.strip()
                    explicit = True
                    break
    model = model or HERMES_DEFAULT_EMBEDDING_MODEL
    return {"model": model, "dims": _dims_for(model), "explicit": explicit}


def preflight_shared_bank(
    hermes_home: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verify the shared bank is ONE recall universe. Never raises.

    Layers: bank_file (openable + converged) -> bank_name (identical,
    ``default``) -> scoping (``global`` both sides) -> backend (omp
    ``mnemopi``) -> embedding (same model+dims, or both FTS-only; loud
    on divergent vector spaces) -> schema (both engines' columns) ->
    canary (write one side, read back fresh = WAL visibility).

    Returns ``{"ok": bool, "bank_path": str, "layers": {name: {"status":
    "ok"|"warn"|"fail", "detail": str}}}``. ``ok`` is False on any fail.
    """
    layers: Dict[str, Dict[str, str]] = {}

    def _record(name: str, status: str, detail: str) -> None:
        layers[name] = {"status": status, "detail": detail}

    bank_path = resolve_bank_path(hermes_home, config)
    try:
        created = not os.path.isfile(bank_path)
        ensure_bank(bank_path)
        size = os.path.getsize(bank_path)
        _record("bank_file", "ok", "%s (%d bytes%s)" % (bank_path, size, ", created" if created else ""))
    except Exception as exc:
        _record("bank_file", "fail", f"cannot open/converge {bank_path}: {exc}")
        return {"ok": False, "bank_path": bank_path, "layers": layers}

    omp = _omp_effective_settings()
    if omp["bank"] != "default":
        _record("bank_name", "fail",
                f"omp mnemopi.bank={omp['bank']!r} is not 'default': sibling banks split recall")
    else:
        _record("bank_name", "ok", "bank 'default' on the pinned file")
    if omp["scoping"] != "global":
        _record("scoping", "fail",
                f"omp mnemopi.scoping={omp['scoping']!r}: per-project banks split recall; use global")
    else:
        _record("scoping", "ok", "global scoping both sides")
    if omp["backend"] != "mnemopi":
        _record("backend", "fail",
                f"omp memory.backend={omp['backend']!r}: engines diverge; set mnemopi to unify")
    else:
        _record("backend", "ok", "omp memory.backend=mnemopi")

    hermes_emb = _hermes_embedding_posture(config)
    omp_embeds = not omp["noEmbeddings"]
    if not omp_embeds and not hermes_emb["explicit"]:
        _record("embedding", "ok", "both FTS-only (no vectors either side)")
    elif omp_embeds and not hermes_emb["explicit"]:
        _record("embedding", "warn",
                f"omp embeds with {omp['embeddingModel']} ({omp['embeddingDims']}d) while hermes "
                "writes FTS-only: FTS denominator shared, vectors omp-local")
    elif not omp_embeds and hermes_emb["explicit"]:
        _record("embedding", "warn",
                f"hermes model {hermes_emb['model']} configured while omp is FTS-only: "
                "vectors hermes-local until omp enables the same model")
    else:
        same_model = omp["embeddingModel"] == hermes_emb["model"]
        same_dims = omp["embeddingDims"] is not None and omp["embeddingDims"] == hermes_emb["dims"]
        if same_model and same_dims:
            _record("embedding", "ok",
                    f"shared vectors: {omp['embeddingModel']} ({omp['embeddingDims']}d) both sides; "
                    "hermes provider rows are FTS-only (vectors backfill via real-package paths)")
        else:
            _record("embedding", "fail",
                    f"vector-space mismatch: omp {omp['embeddingModel']} ({omp['embeddingDims']}d) vs "
                    f"hermes {hermes_emb['model']} ({hermes_emb['dims']}d): vectors written by one "
                    "side are garbage to the other; align models or set both FTS-only")

    try:
        conn = open_bank(bank_path)
        try:
            tables = {row[0] for row in
                      conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')").fetchall()}
            missing_tables = [t for t in _REQUIRED_TABLES if t not in tables]
            cols = {row[1] for row in conn.execute("PRAGMA table_info(working_memory)").fetchall()}
            missing_cols = [c for c in _REQUIRED_WORKING_COLUMNS if c not in cols]
            if missing_tables or missing_cols:
                _record("schema", "fail",
                        f"missing tables={missing_tables} columns={missing_cols}")
            else:
                _record("schema", "ok", "BeamMemory tables + cross-engine columns present")
        finally:
            conn.close()
    except Exception as exc:
        _record("schema", "fail", f"schema probe failed: {exc}")

    try:
        marker = f"mnemosyne-preflight-canary {uuid.uuid4().hex}"
        writer = open_bank(bank_path)
        try:
            ensure_schema(writer)
            mid = insert_working(writer, marker, source="preflight",
                                 session_id="preflight", importance=0.0)
        finally:
            writer.close()
        reader = open_bank(bank_path)
        try:
            hits = recall_rows(reader, marker, limit=5)
            seen = any(h["id"] == mid for h in hits)
        finally:
            reader.close()
        cleaner = open_bank(bank_path)
        try:
            cleaner.execute("DELETE FROM working_memory WHERE id = ?", (mid,))
            cleaner.commit()
        finally:
            cleaner.close()
        if seen:
            _record("canary", "ok", "write/read-back across fresh connections (WAL visible)")
        else:
            _record("canary", "fail", "canary row not recallable across connections")
    except Exception as exc:
        _record("canary", "fail", f"canary round-trip failed: {exc}")

    ok = all(layer["status"] != "fail" for layer in layers.values())
    return {"ok": ok, "bank_path": bank_path, "layers": layers}


def format_preflight(report: Dict[str, Any]) -> str:
    """One loud block: unified-or-failure per layer (for setup output)."""
    head = "local mnemosyne bank UNIFIED" if report.get("ok") else "local mnemosyne bank NOT unified"
    lines = [f"{head}: {report.get('bank_path', '')}"]
    for name, layer in (report.get("layers") or {}).items():
        lines.append(f"  [{layer.get('status', '?').upper()}] {name}: {layer.get('detail', '')}")
    return "\n".join(lines)
