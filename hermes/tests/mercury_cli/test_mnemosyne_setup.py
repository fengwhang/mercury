"""Setup-path tests for the shared-bank default (no manual plugin step).

Fresh installs get memory.provider=mnemosyne (silent config write, pip
re-verify attempted); explicit user backends are never clobbered (silent
keep, byte-identical file). Covers the wizard default selection and the
mnemosyne-hermes repair probe (status/install equivalents) with fakes —
no network, no 800MB download.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import mercury_cli.memory_setup as memory_setup
from mercury_cli.memory_setup import (
    _wizard_default_index,
    ensure_mnemosyne_default,
)


def _write_config(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_ensure_sets_default_on_fresh_config(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, "models:\n  default: prov/m-1\n")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))

    assert ensure_mnemosyne_default(install=False, verbose=True) == "mnemosyne"
    text = cfg.read_text(encoding="utf-8")
    assert "provider: mnemosyne" in text
    assert capsys.readouterr().out != ""  # announces the fresh default


def test_ensure_keeps_explicit_backend_silent(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, "hermes:\n  memory:\n    provider: honcho\n")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    before = cfg.read_text(encoding="utf-8")

    assert ensure_mnemosyne_default(install=False) == "honcho"
    assert cfg.read_text(encoding="utf-8") == before
    assert capsys.readouterr().out == ""


def test_ensure_already_mnemosyne_is_silent_keep(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, "hermes:\n  memory:\n    provider: mnemosyne\n")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    before = cfg.read_text(encoding="utf-8")

    assert ensure_mnemosyne_default(install=False) == "mnemosyne"
    assert cfg.read_text(encoding="utf-8") == before
    assert capsys.readouterr().out == ""


def test_wizard_default_selects_mnemosyne_when_fresh():
    names = ["honcho", "mnemosyne", "holographic"]
    assert _wizard_default_index(names, "", len(names)) == 1
    assert _wizard_default_index(names, "built-in", len(names)) == 1


def test_wizard_default_keeps_current_on_reruns():
    names = ["honcho", "mnemosyne", "holographic"]
    assert _wizard_default_index(names, "honcho", len(names)) == 0
    assert _wizard_default_index(names, "mnemosyne", len(names)) == 1
    # Unknown non-empty backend: fall back to built-in, still offering mnemosyne.
    assert _wizard_default_index(names, "weird", len(names)) == 3

def test_install_probe_noops_when_package_present(tmp_path, monkeypatch):
    """Venv-rebuild repair: present package -> no install attempt, no network."""
    monkeypatch.setitem(sys.modules, "mnemosyne_hermes", types.ModuleType("mnemosyne_hermes"))
    calls: list = []

    class _ok:
        ok, blocked, stderr = True, False, ""

    monkeypatch.setattr(
        "tools.lazy_deps.install_specs",
        lambda specs, timeout=300: (calls.append(list(specs)), _ok())[1],
    )
    memory_setup._install_dependencies("mnemosyne")
    assert calls == []


def test_install_probe_reinstalls_when_missing(tmp_path, monkeypatch):
    """Missing package (rebuilt venv) -> reinstall mnemosyne-hermes via pip."""
    monkeypatch.delitem(sys.modules, "mnemosyne_hermes", raising=False)
    # Force the import probe to fail even if the real package exists here.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mnemosyne_hermes":
            raise ImportError("No module named mnemosyne_hermes")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    calls: list = []

    class _ok:
        ok, blocked, stderr = True, False, ""

    monkeypatch.setattr(
        "tools.lazy_deps.install_specs",
        lambda specs, timeout=300: (calls.append(list(specs)), _ok())[1],
    )
    memory_setup._install_dependencies("mnemosyne")
    assert calls == [["mnemosyne-hermes"]]


def _preflight(cfg_text, tmp_path, monkeypatch, **env):
    from plugins.memory.mnemosyne import preflight_shared_bank

    cfg = tmp_path / "config.yaml"
    cfg.write_text(cfg_text, encoding="utf-8")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.delenv("MNEMOSYNE_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("MNEMOSYNE_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("MNEMOSYNE_DB_PATH", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return preflight_shared_bank()


def test_preflight_unified_on_fresh_config(tmp_path, monkeypatch):
    report = _preflight("models:\n  default: prov/m-1\n", tmp_path, monkeypatch)
    assert report["ok"] is True
    assert {name: layer["status"] for name, layer in report["layers"].items()} == {
        "bank_file": "ok", "bank_name": "ok", "scoping": "ok", "backend": "ok",
        "embedding": "ok", "schema": "ok", "canary": "ok",
    }
    # Canary cleans up after itself.
    import sqlite3

    conn = sqlite3.connect(report["bank_path"])
    try:
        leftovers = conn.execute(
            "SELECT COUNT(*) FROM working_memory WHERE source = 'preflight'").fetchone()[0]
    finally:
        conn.close()
    assert leftovers == 0


def test_preflight_fails_loud_on_split_scoping(tmp_path, monkeypatch):
    report = _preflight(
        "models:\n  default: prov/m-1\n"
        "omp:\n  memory:\n    backend: mnemopi\n"
        "  mnemopi:\n    bank: default\n    scoping: per-project\n",
        tmp_path, monkeypatch)
    assert report["ok"] is False
    assert report["layers"]["scoping"]["status"] == "fail"
    assert "per-project" in report["layers"]["scoping"]["detail"]


def test_preflight_fails_on_backend_divergence(tmp_path, monkeypatch):
    report = _preflight(
        "models:\n  default: prov/m-1\nomp:\n  memory:\n    backend: off\n",
        tmp_path, monkeypatch)
    assert report["ok"] is False
    assert report["layers"]["backend"]["status"] == "fail"


def test_preflight_fails_on_vector_space_mismatch(tmp_path, monkeypatch):
    report = _preflight(
        "models:\n  default: prov/m-1\n"
        "omp:\n  memory:\n    backend: mnemopi\n"
        "  mnemopi:\n    bank: default\n    scoping: global\n"
        "    noEmbeddings: false\n    embeddingVariant: en\n",
        tmp_path, monkeypatch,
        MNEMOSYNE_EMBEDDING_MODEL="intfloat/multilingual-e5-large")
    assert report["ok"] is False
    layer = report["layers"]["embedding"]
    assert layer["status"] == "fail"
    assert "768" in layer["detail"] and "1024" in layer["detail"]


def test_preflight_warns_on_one_sided_embeddings(tmp_path, monkeypatch):
    report = _preflight(
        "models:\n  default: prov/m-1\n"
        "omp:\n  memory:\n    backend: mnemopi\n"
        "  mnemopi:\n    bank: default\n    scoping: global\n"
        "    noEmbeddings: false\n    embeddingVariant: en\n",
        tmp_path, monkeypatch)
    assert report["ok"] is True
    assert report["layers"]["embedding"]["status"] == "warn"


def test_preflight_honors_omp_dbpath_pin(tmp_path, monkeypatch):
    custom = tmp_path / "custom.db"
    report = _preflight(
        "models:\n  default: prov/m-1\n"
        "omp:\n  memory:\n    backend: mnemopi\n"
        f"  mnemopi:\n    dbPath: '{custom}'\n    bank: default\n    scoping: global\n",
        tmp_path, monkeypatch)
    assert report["ok"] is True
    assert report["bank_path"] == str(custom)

def test_ensure_stamps_both_sides_unified_fts_on_empty_config(tmp_path, monkeypatch, capsys):
    """Fresh install (empty config) lands unified: mnemopi + FTS-only both sides."""
    from plugins.memory.mnemosyne import format_preflight, preflight_shared_bank

    cfg = tmp_path / "config.yaml"
    _write_config(cfg, "models:\n  default: prov/m-1\n")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.delenv("MNEMOSYNE_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("MNEMOSYNE_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("MNEMOSYNE_DB_PATH", raising=False)

    assert ensure_mnemosyne_default(install=False, verbose=True) == "mnemosyne"
    out = capsys.readouterr().out
    assert "local mnemosyne" in out
    text = cfg.read_text(encoding="utf-8")
    assert "provider: mnemosyne" in text
    assert "backend: mnemopi" in text
    assert "noEmbeddings: true" in text
    assert "scoping: global" in text
    assert "autoRecall: true" in text
    assert "autoRetain: true" in text

    report = preflight_shared_bank()
    assert report["ok"] is True
    assert report["layers"]["backend"]["status"] == "ok"
    assert report["layers"]["embedding"]["status"] == "ok"
    assert "both FTS-only" in report["layers"]["embedding"]["detail"]
    assert "local mnemosyne bank UNIFIED" in format_preflight(report)


def test_preflight_vm_shape_fails_loud_then_passes_after_render(tmp_path, monkeypatch):
    """VM fresh v0.0.41 shape: partial omp block reads as off + embeddings-on.

    The omp block exists (approvals/models stamped first) but carries no
    memory section, so the effective settings leak via the omp schema
    defaults: backend off, embeddings ON (BAAI/bge-base-en-v1.5 768d) while
    hermes writes FTS-only. Preflight must fail loud pre-fix and pass
    post-fix once the render pins the unified defaults.
    """
    from plugins.memory.mnemosyne import format_preflight, preflight_shared_bank

    vm_shape = (
        "models:\n  default: prov/m-1\n"
        "omp:\n  setupVersion: 2\n  tools:\n    approvalMode: \"write\"\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(vm_shape, encoding="utf-8")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.delenv("MNEMOSYNE_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("MNEMOSYNE_EMBEDDING_DIM", raising=False)
    monkeypatch.delenv("MNEMOSYNE_DB_PATH", raising=False)

    report = preflight_shared_bank()
    assert report["ok"] is False
    assert report["layers"]["bank_file"]["status"] == "ok"
    assert report["layers"]["bank_name"]["status"] == "ok"
    assert report["layers"]["scoping"]["status"] == "ok"
    assert report["layers"]["backend"]["status"] == "fail"
    assert report["layers"]["backend"]["detail"] == "omp memory.backend='off': engines diverge; set mnemopi to unify"
    assert report["layers"]["embedding"]["status"] == "warn"
    assert "BAAI/bge-base-en-v1.5" in report["layers"]["embedding"]["detail"]
    assert "768d" in report["layers"]["embedding"]["detail"]
    assert "FTS-only" in report["layers"]["embedding"]["detail"]
    assert "local mnemosyne bank NOT unified" in format_preflight(report)

    # Post-fix: the ONE ensure pass stamps BOTH sides (hermes provider +
    # omp backend/mnemopi FTS-only), so the same preflight now passes.
    assert ensure_mnemosyne_default(install=False) == "mnemosyne"
    fixed = preflight_shared_bank()
    assert fixed["ok"] is True
    assert fixed["layers"]["backend"]["status"] == "ok"
    assert fixed["layers"]["embedding"]["status"] == "ok"
    assert "local mnemosyne bank UNIFIED" in format_preflight(fixed)
    text = cfg.read_text(encoding="utf-8")
    assert "backend: mnemopi" in text
    assert "noEmbeddings: true" in text


def test_ensure_never_clobbers_explicit_off(tmp_path, monkeypatch, capsys):
    """Explicit omp backend off survives the fresh-default pass."""
    cfg = tmp_path / "config.yaml"
    _write_config(cfg, "models:\n  default: prov/m-1\nomp:\n  memory:\n    backend: off\n")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))

    from mercury_cli.memory_setup import _ensure_omp_mnemopi_defaults

    assert _ensure_omp_mnemopi_defaults() is False
    text = cfg.read_text(encoding="utf-8")
    assert "backend: off" in text
    assert "backend: mnemopi" not in text
    assert "mnemopi:" not in text
