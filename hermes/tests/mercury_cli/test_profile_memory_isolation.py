"""Real profile creation and SQLite writes must never leak into sibling banks."""
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from mercury_cli import profiles
from mercury_cli.memory_settings import profile_bank_path
from mercury_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.memory.mnemosyne import MnemosyneMemoryProvider, resolve_bank_path


@pytest.fixture(params=[".mercury", ".mercury-nightly"])
def installation(tmp_path, monkeypatch, request):
    root = tmp_path / request.param
    (root / "hermes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key, value in {"HOME": tmp_path, "MERCURY_HOME": root,
                       "HERMES_HOME": root / "hermes", "MERCURY_CONFIG": root / "config.yaml",
                       "MNEMOSYNE_DB_PATH": ""}.items():
        monkeypatch.setenv(key, str(value))
    bank = root / "memories" / "mnemopi.db"
    (root / "config.yaml").write_text(yaml.safe_dump({
        "models": {"default": "nous/chat", "delegate_model": "nous/code"},
        "hermes": {"memory": {"provider": "mnemosyne", "mnemosyne": {"db_path": str(bank)}}},
        "omp": {"memory": {"backend": "mnemopi"}, "mnemopi": {"dbPath": str(bank), "noEmbeddings": True}},
    }))
    return root


def create(name, **kwargs):
    return profiles.create_profile(name, no_alias=True, no_skills=not bool(kwargs), **kwargs)


def provider(home):
    p = MnemosyneMemoryProvider()
    p.initialize("memory-isolation", mercury_home=str(home))
    return p


def remember(p, text):
    result = p.handle_tool_call("mnemosyne_remember", {"content": text})
    assert not json.loads(result).get("error"), result


def recall(p):
    return json.loads(p.handle_tool_call("mnemosyne_recall", {"query": "fruit"}))["results"]


def test_existing_profile_global_pins_and_contexts_are_isolated(installation):
    root = installation
    main = provider(root / "hermes")
    remember(main, "fruit default pomegranate")
    homes = [create("alpha"), create("beta")]
    # Reproduce old profiles copied with absolute main-bank settings.
    old = (root / "config.yaml").read_text()
    for home in homes:
        (home / "config.yaml").write_text(old)

    def worker(home):
        token = set_hermes_home_override(str(home))
        try:
            p = provider(home)
            remember(p, "fruit " + home.name)
            assert resolve_bank_path() == str(home / "memories" / "mnemopi.db")
            assert p.backup_paths() == [resolve_bank_path()]
            result = recall(p)
            p.shutdown()
            return [hit["content"] for hit in result]
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, homes))
    assert results == [["fruit alpha"], ["fruit beta"]]
    assert [hit["content"] for hit in recall(main)] == ["fruit default pomegranate"]
    main.shutdown()


def test_create_clone_and_full_snapshot_use_independent_banks(installation):
    root = installation
    source = create("source")
    raw = yaml.safe_load((source / "config.yaml").read_text())
    expected = str(source / "memories" / "mnemopi.db")
    assert raw["hermes"]["memory"]["mnemosyne"]["db_path"] == expected
    assert raw["omp"]["mnemopi"]["dbPath"] == expected
    p = provider(source)
    remember(p, "fruit source persimmon")
    normal = create("normal", clone_from="source", clone_config=True)
    full = create("full", clone_from="source", clone_all=True)
    blank = provider(normal)
    snapshot = provider(full)
    assert recall(blank) == []
    assert [hit["content"] for hit in recall(snapshot)] == ["fruit source persimmon"]
    remember(snapshot, "fruit snapshot pineapple")
    assert [hit["content"] for hit in recall(p)] == ["fruit source persimmon"]
    assert resolve_bank_path(str(root / "hermes")) == str(root / "memories" / "mnemopi.db")
    for item in (p, blank, snapshot):
        item.shutdown()


def test_import_and_rename_rebase_bank_without_losing_memories(installation, tmp_path):
    source = create("source")
    p = provider(source)
    remember(p, "fruit exported mango")
    p.shutdown()
    archive = profiles.export_profile("source", str(tmp_path / "profile.tar.gz"))
    restored = profiles.import_profile(str(archive), name="restored")
    p = provider(restored)
    assert [hit["content"] for hit in recall(p)] == ["fruit exported mango"]
    p.shutdown()
    renamed = profiles.rename_profile("restored", "renamed")
    raw = yaml.safe_load((renamed / "config.yaml").read_text())
    assert raw["omp"]["mnemopi"]["dbPath"] == str(renamed / "memories" / "mnemopi.db")
    p = provider(renamed)
    assert [hit["content"] for hit in recall(p)] == ["fruit exported mango"]
    p.shutdown()


def test_outside_overrides_cannot_join_another_profile_bank(installation, monkeypatch):
    root = installation
    home = create("private")
    monkeypatch.setenv("MNEMOSYNE_DB_PATH", str(root / "memories" / "mnemopi.db"))
    assert resolve_bank_path(str(home)) == str(home / "memories" / "mnemopi.db")
    assert profile_bank_path("memories/custom.db", home=home) == str(home / "memories" / "custom.db")
    # Default-profile custom storage remains supported.
    assert profile_bank_path("/tmp/custom-memory.db", home=root) == "/tmp/custom-memory.db"


def test_setup_and_bridge_use_selected_profile_not_ambient_main(installation):
    import os
    import subprocess
    import sys
    from mercury_cli.memory_setup import _ensure_omp_mnemopi_defaults

    root = installation
    home = create("setup-test")
    main_config = (root / "config.yaml").read_text()
    (home / "config.yaml").write_text(main_config)
    token = set_hermes_home_override(str(home))
    try:
        assert _ensure_omp_mnemopi_defaults()
    finally:
        reset_hermes_home_override(token)
    assert yaml.safe_load((home / "config.yaml").read_text())["omp"]["mnemopi"]["dbPath"] == str(home / "memories" / "mnemopi.db")
    (home / "config.yaml").write_text(main_config)
    bridge = Path(__file__).resolve().parents[3] / "bridge" / "bridge.py"
    result = subprocess.run([sys.executable, str(bridge), "--render-omp"],
                            env={**os.environ, "HERMES_OMP_CONFIG": str(home / "config.yaml")},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load((home / "config.yaml").read_text())["omp"]["mnemopi"]["dbPath"] == str(home / "memories" / "mnemopi.db")
    assert (root / "config.yaml").read_text() == main_config


def test_full_clone_snapshots_live_wal_and_detaches_config_symlink(installation):
    source = create("source-wal")
    config = source / "config.yaml"
    original = config.read_text()
    backing = installation / "source-settings.yaml"
    backing.write_text(original)
    config.unlink()
    config.symlink_to(backing)
    p = provider(source)
    # Holding a real connection keeps committed changes in the live WAL.
    connection = p._connect()
    try:
        remember(p, "fruit source papaya")
        assert Path(p._bank_path + "-wal").stat().st_size > 0
        full = create("wal-copy", clone_from="source-wal", clone_all=True)
        assert not (full / "config.yaml").is_symlink()
        assert backing.read_text() == original
        clone = provider(full)
        assert [hit["content"] for hit in recall(clone)] == ["fruit source papaya"]
        clone.shutdown()
    finally:
        connection.close()
        p.shutdown()
