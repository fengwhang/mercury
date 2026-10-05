"""Dependency publication must survive failure and keep live generations."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mercury_cli import runtime_state
from pm.environments import install_state_dir, runtime_facts_path


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("MERCURY_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = tmp_path / "project"
    root.mkdir()
    return root, home


def journal(project, previous, after, committed=False):
    root, home = project
    facts = runtime_facts_path(root)
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_bytes(b'{"generation": "old"}')
    (home / "config.yaml").write_bytes(after)
    row = {"config": str(home / "config.yaml"),
           "previous": base64.b64encode(previous).decode(),
           "config_after": hashlib.sha256(after).hexdigest(),
           "facts_before": hashlib.sha256(facts.read_bytes()).hexdigest(),
           "committed": committed}
    path = facts.parent / "publication.json"
    path.write_text(json.dumps(row))
    return path


def test_uncommitted_publication_rolls_back_atomically(project):
    root, home = project
    previous = b'models: {default: "nous/original"}\n'
    path = journal(project, previous, b'models: {default: "nous/new"}\n')
    with runtime_state.runtime_lock(root):
        runtime_state.recover_publication(root)
    assert (home / "config.yaml").read_bytes() == previous
    assert not path.exists()


def test_committed_publication_is_kept(project):
    root, home = project
    after = b'models: {default: "nous/new"}\n'
    path = journal(project, b'models: {default: "nous/original"}\n', after)
    with runtime_state.runtime_lock(root):
        runtime_state.finish_publication(root)
    assert (home / "config.yaml").read_bytes() == after
    assert not path.exists()


def test_recovery_refuses_to_clobber_concurrent_user_edit(project):
    root, home = project
    path = journal(project, b"before", b"after")
    (home / "config.yaml").write_bytes(b"user's newer edit")
    with pytest.raises(RuntimeError, match="config changed"):
        runtime_state.recover_publication(root)
    assert path.exists()
    assert (home / "config.yaml").read_bytes() == b"user's newer edit"


def test_kernel_lease_is_live_until_child_exits(project):
    root, home = project
    generation = install_state_dir(root) / "environments" / "old"
    generation.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    script = (
        "from pathlib import Path; import sys; "
        "from mercury_cli.runtime_state import lease_directory; "
        "lease_directory(Path(sys.argv[1])); print('leased', flush=True); sys.stdin.read()")
    child = subprocess.Popen([sys.executable, "-c", script, str(generation)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
    try:
        assert child.stdout.readline().strip() == "leased"
        assert runtime_state.leases_held(generation)
        # Simulate abrupt process death: kernel locks must release without atexit.
        child.kill()
        child.wait(timeout=3)
        assert not runtime_state.leases_held(generation)
        assert list((generation / ".leases").iterdir()) == []
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)


def test_release_is_idempotent(project):
    root, home = project
    generation = home / "generation"
    generation.mkdir()
    (generation / ".lease-managed").touch()
    release = runtime_state.lease_directory(generation)
    assert runtime_state.leases_held(generation)
    release()
    release()
    assert not runtime_state.leases_held(generation)


def test_manifest_gate_uses_engine_api_and_mercury_product_versions():
    from pm.plugin_declarations import manifest_version_error

    assert manifest_version_error({"requires_hermes": ">=0.21.0,<0.22", "manifest_version": 2}, "test") is None
    assert "Hermes API" in manifest_version_error({"requires_hermes": ">=0.22"}, "test")
    assert "Mercury" in manifest_version_error({"requires_mercury": ">=99"}, "test")
    assert "supports up to 2" in manifest_version_error({"manifest_version": 3}, "test")
