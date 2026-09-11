"""Tests for the mnemosyne shared-bank provider (hermes side of ONE bank).

Recall/retain round-trip on a temp bank, importance ranking, episodic FTS,
concurrent-open WAL smoke, tool surface, and the status probe
(mnemosyne-hermes status + hermes memory status equivalents).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

import pytest

from plugins.memory.mnemosyne import (
    MnemosyneMemoryProvider,
    ensure_schema,
    mnemosyne_status_summary,
    open_bank,
    recall_rows,
    resolve_bank_path,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("MNEMOSYNE_DB_PATH", "")
    return tmp_path


@pytest.fixture
def provider(home):
    p = MnemosyneMemoryProvider()
    assert p.name == "mnemosyne"
    assert p.is_available()
    p.initialize("test-session", mercury_home=str(home), agent_context="primary")
    yield p
    p.shutdown()


def bank_file(home) -> str:
    return os.path.join(str(home), "memories", "mnemopi.db")


def test_resolve_bank_path_defaults_to_shared_file(home):
    assert resolve_bank_path() == bank_file(home)


def test_initialize_creates_shared_bank(provider, home):
    assert os.path.isfile(bank_file(home))
    assert "Mnemosyne" in provider.system_prompt_block()


def test_recall_retain_round_trip(provider):
    provider.on_memory_write("add", "memory", "The user prefers dark mode in the editor")
    out = provider.prefetch("editor dark mode preference")
    assert "dark mode" in out
    status = provider.recall_status()
    assert status is not None and status.count >= 1


def test_trivial_prompt_recalls_nothing(provider):
    provider.on_memory_write("add", "memory", "The user prefers dark mode")
    assert provider.prefetch("hi") == ""
    assert provider.recall_status() is None


def test_remove_mirrors_delete(provider):
    provider.on_memory_write("add", "memory", "temporary parking code 7788")
    assert "7788" in provider.prefetch("parking code")
    provider.on_memory_write("remove", "memory", "temporary parking code 7788")
    assert "7788" not in provider.prefetch("parking code")


def test_importance_ranking_identical_content(provider, home):
    conn = open_bank(bank_file(home))
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO working_memory (id, content, source, timestamp, session_id, importance, scope)"
            " VALUES ('low-imp', 'quokka census Oak Street', 't', '2026-09-11T00:00:00.000Z', 's', 0.1, 'global')"
        )
        conn.execute(
            "INSERT INTO working_memory (id, content, source, timestamp, session_id, importance, scope)"
            " VALUES ('high-imp', 'quokka census Oak Street', 't', '2026-09-11T00:00:00.000Z', 's', 0.9, 'global')"
        )
        conn.commit()
        hits = recall_rows(conn, "quokka census Oak", limit=5)
    finally:
        conn.close()
    assert [h["id"] for h in hits[:2]] == ["high-imp", "low-imp"]


def test_episodic_fts_recall(provider, home):
    conn = open_bank(bank_file(home))
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO episodic_memory (id, content, source, timestamp, session_id, importance, scope)"
            " VALUES ('ep1', 'Sprint review moved to Thursday standup', 't',"
            " '2026-09-11T00:00:00.000Z', 's', 0.6, 'global')"
        )
        conn.commit()
    finally:
        conn.close()
    out = provider.prefetch("sprint review Thursday")
    assert "Thursday" in out


def test_tool_recall_and_remember(provider):
    stored = json.loads(provider.handle_tool_call("mnemosyne_remember", {"content": "Gate code is 4410"}))
    assert stored["stored"] is True
    found = json.loads(provider.handle_tool_call("mnemosyne_recall", {"query": "gate code"}))
    assert any("4410" in r["content"] for r in found["results"])
    assert "Unknown tool" in provider.handle_tool_call("nope", {})


def test_tool_schemas_never_override_builtin(provider):
    names = [t["name"] for t in provider.get_tool_schemas()]
    assert names == ["mnemosyne_recall", "mnemosyne_remember"]
    assert not (set(names) & {"recall", "retain", "reflect", "memory"})


def test_cron_context_pauses_mirror_writes(home):
    p = MnemosyneMemoryProvider()
    p.initialize("cron-run", mercury_home=str(home), agent_context="cron")
    p.on_memory_write("add", "memory", "cron system prompt detail 9911")
    assert "9911" not in p.prefetch("cron system prompt 9911")


def test_concurrent_open_smoke(provider):
    errors: list = []

    def writer(n):
        try:
            for i in range(15):
                provider.on_memory_write("add", "memory", f"concurrent deploy note {n}-{i} kubernetes")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(15):
                provider.prefetch("kubernetes deploy")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert "kubernetes" in provider.prefetch("concurrent deploy kubernetes")


def test_ts_side_interop_same_file(provider, home):
    """Rows written with TS-shaped columns are recalled by the provider."""
    conn = sqlite3.connect(bank_file(home), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO working_memory (id, content, embed_text, source, timestamp,"
            " session_id, importance, memory_type, trust_tier, scope)"
            " VALUES ('ts-row', 'deploy freeze every Friday', 'deploy freeze Friday',"
            " 'conversation', '2026-09-11T00:00:00.000Z', 'default', 0.7,"
            " 'decision', 'STATED', 'global')"
        )
        conn.commit()
    finally:
        conn.close()
    assert "Friday" in provider.prefetch("deploy freeze Friday")


def test_status_summary_reports_bank_and_graceful_package_probe(provider, home):
    summary = mnemosyne_status_summary()
    assert summary["provider"] == "mnemosyne"
    assert summary["plugin"] == "hermes-mnemosyne"
    assert summary["bank_path"] == bank_file(home)
    assert summary["bank_exists"] is True
    assert summary["bank_bytes"] > 0
    assert summary["fts5_available"] is True
    assert isinstance(summary["package_present"], bool)
    assert isinstance(summary["embeddings_present"], bool)


def test_backup_paths_declares_bank_inside_mercury_home(provider, home):
    assert provider.backup_paths() == [bank_file(home)]


def test_backup_paths_empty_without_init(home):
    assert MnemosyneMemoryProvider().backup_paths() == []


def test_config_schema_has_no_secrets(provider):
    schema = provider.get_config_schema()
    assert {f["key"] for f in schema} == {"db_path", "bank"}
    assert not any(f.get("secret") for f in schema)
    assert provider.save_config({}, str(home)) is None
