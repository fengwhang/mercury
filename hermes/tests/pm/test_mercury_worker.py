"""Publish plugin dependency generations through PM's real isolated worker."""
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.pm._fixtures import _wheel


@pytest.fixture
def worker(tmp_path, monkeypatch):
    configured = os.environ.get("MERCURY_TEST_PM_PYTHON")
    if not configured:
        pytest.skip("set MERCURY_TEST_PM_PYTHON to a prepared isolated PM runtime")
    python = Path(configured)
    uv = shutil.which("uv")
    assert python.is_file() and uv
    result = subprocess.run([str(python), "-I", "-c",
        "import importlib.util; import truststore, packaging; from ruamel.yaml import YAML; "
        "assert importlib.util.find_spec('yaml') is None; "
        "assert importlib.util.find_spec('dotenv') is None"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("MERCURY_CONFIG", str(home / "config.yaml"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("MERCURY_PYTHON", sys.executable)
    from pm import paths
    client = importlib.import_module("pm.client")
    script = Path(client.__file__).with_name("worker.py")
    launch = (
        "import runpy,sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(script.parent.parent)!r}); import pm._uv; "
        f"pm._uv._toolchain = lambda **kwargs: (Path({uv!r}), Path({str(python)!r})); "
        f"runpy.run_path({str(script)!r}, run_name='__main__')")
    monkeypatch.setattr(client, "runtime_command", lambda *args, **kwargs:
        [str(python), "-I", "-B", "-c", launch])
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    project = tmp_path / "project"
    project.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "greeting_dep", "1.0")
    _wheel(wheels, "greeting_dep", "2.0")
    (project / "pyproject.toml").write_text(
        '[project]\nname="hermes-agent"\nversion="0"\nrequires-python=">=3.11,<3.14"\n'
        f'[tool.uv]\npackage=false\nno-index=true\nfind-links=[{json.dumps(str(wheels))}]\n')
    monkeypatch.setattr(paths, "repo_root", lambda: project)
    plugin = home / "hermes" / "plugins" / "example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text('name: example\npython_dependencies: ["greeting-dep==1.0"]\n')
    (home / "config.yaml").write_text(
        'hermes:\n  plugins:\n    enabled: [example]\n  security:\n    allow_lazy_installs: false\n'
        'models: {default: "nous/keep-this"}\nomp: {tools: {approvalMode: write}}\n')
    client.lock_project(project, offline=True, explicit=True)
    return client, project, home, plugin


def test_worker_publishes_upgrade_and_preserves_previous_on_failure(worker):
    from pm.environments import selected_venv
    from pm.package import InstallError

    client, project, home, plugin = worker
    config = (home / "config.yaml").read_bytes()
    original_prefix = sys.prefix
    client.sync_venv(explicit=True)
    previous = selected_venv(project)
    assert (previous / "lib").is_dir()
    def version(environment):
        executable = environment / "bin" / "python"
        completed = subprocess.run([str(executable), "-I", "-c",
            "import greeting_dep; print(greeting_dep.__version__)"],
            capture_output=True, text=True, timeout=10)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()
    assert version(previous) == "1.0"
    (plugin / "plugin.yaml").write_text('name: example\npython_dependencies: ["greeting-dep==2.0"]\n')
    client.sync_venv(explicit=True)
    upgraded = selected_venv(project)
    assert upgraded != previous
    assert version(upgraded) == "2.0"
    assert version(previous) == "1.0"
    (plugin / "plugin.yaml").write_text('name: example\npython_dependencies: ["greeting-dep==99.0"]\n')
    with pytest.raises(InstallError):
        client.sync_venv(explicit=True)
    assert selected_venv(project) == upgraded
    assert version(upgraded) == "2.0"
    assert sys.prefix == original_prefix
    assert (home / "config.yaml").read_bytes() == config


def test_worker_refuses_lazy_install_with_same_config_as_caller(worker):
    from pm.package import InstallError
    client, project, home, plugin = worker
    with pytest.raises(InstallError, match="lazy installs are disabled"):
        client.sync_venv()
    assert not (home / "tools" / "facts.json").exists()


def test_worker_rejects_unsupported_application_python_without_publishing(worker, monkeypatch):
    from pm.package import InstallError
    from pm.environments import committed_venv

    client, project, home, plugin = worker
    monkeypatch.setenv("MERCURY_PYTHON", os.environ["MERCURY_TEST_PM_PYTHON"])
    with pytest.raises(InstallError, match="application Python is unavailable or unsupported"):
        client.sync_venv(explicit=True)
    assert committed_venv(project) is None


def test_worker_detects_changed_project_manifest_before_reusing_generation(worker):
    from pm.client import venv_is_current

    client, project, home, plugin = worker
    client.sync_venv(explicit=True)
    assert venv_is_current()
    manifest = project / "pyproject.toml"
    manifest.write_text(manifest.read_text() + "\n# revised application inputs\n")
    assert not venv_is_current()
