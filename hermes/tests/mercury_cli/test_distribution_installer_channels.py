"""Execute installer channel selection with installation dependencies stubbed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def installer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state = home / ".mercury"
    state.mkdir()
    (state / "channel").write_text("nightly\n")
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home),
        "MERCURY_HOME": str(state), "MERCURY_CHANNEL": "nightly",
        "MERCURY_BIN_DIR": str(home / ".local/bin"),
        "INSTALLER_UNDER_TEST": str(ROOT / "install.sh"),
    }
    harness = tmp_path / "exercise.sh"
    harness.write_text(r'''#!/usr/bin/env bash
set -euo pipefail
source "$INSTALLER_UNDER_TEST" "$@"
for step in detect_system install_uv fetch_tarball setup_venv smoke_test \
    install_system_packages install_browser_use_cli install_observatory \
    run_setup_wizard sweep_stray_omp_logs maybe_start_gateway print_success \
    ensure_path_configured; do
    eval "$step() { :; }"
done
# Protect host-global stale launcher paths while executing real setup_path.
rm() {
    local path
    for path in "$@"; do
        case "$path" in
            -*) ;;
            "$HOME"/*) command rm -f -- "$path" ;;
            *) return 0 ;;
        esac
    done
}
if [[ "${FAIL_OPTIONAL:-}" == yes ]]; then
    install_system_packages() { return 77; }
fi
main
printf 'parsed-tag:%s\n' "${TAG_ARG:-}"
''')
    return harness, env, state


def run_installer(installer, *args, fail=False):
    harness, env, _ = installer
    if fail:
        env = dict(env, FAIL_OPTIONAL="yes")
    return subprocess.run(["bash", str(harness), *args], env=env,
                          capture_output=True, text=True, timeout=15)


def assert_channel(installer, selected):
    _, env, state = installer
    assert (state / "channel").read_text().strip() == selected
    shim = Path(env["MERCURY_BIN_DIR"]) / "mercury"
    # Execute the shim against a tiny fixture distribution launcher.
    launcher = state / "mercury-agent/bin/mercury"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$MERCURY_CHANNEL"\n')
    launcher.chmod(0o755)
    result = subprocess.run([str(shim)], env=env, capture_output=True,
                            text=True, timeout=15, check=True)
    assert result.stdout.strip() == selected


def test_default_stable_ignores_inherited_nightly_and_old_marker(installer):
    result = run_installer(installer)
    assert result.returncode == 0, result.stderr
    assert_channel(installer, "stable")


@pytest.mark.parametrize("selected", ["stable", "nightly"])
def test_explicit_channel_matches_saved_marker_and_launcher(installer, selected):
    result = run_installer(installer, "--channel", selected)
    assert result.returncode == 0, result.stderr
    assert_channel(installer, selected)


def test_optional_failure_cannot_leave_launcher_and_marker_disagreeing(installer):
    result = run_installer(installer, fail=True)
    assert result.returncode == 77, result.stderr
    assert_channel(installer, "stable")


def test_invalid_channel_fails_before_modifying_installation(installer):
    result = run_installer(installer, "--channel", "bogus")
    assert result.returncode != 0
    assert "invalid --channel" in result.stdout
    _, env, state = installer
    assert (state / "channel").read_text() == "nightly\n"
    assert not Path(env["MERCURY_BIN_DIR"]).exists()


def test_prerelease_tag_is_accepted(installer):
    result = run_installer(installer, "--channel", "nightly", "v0.3.4-nightly")
    assert result.returncode == 0, result.stderr
    assert "parsed-tag:v0.3.4-nightly" in result.stdout
    assert_channel(installer, "nightly")


@pytest.mark.parametrize("remote", [False, True])
def test_nightly_wrapper_selects_explicit_channel_and_prerelease_tag(tmp_path, remote):
    wrapper = tmp_path / "install-nightly.sh"
    shutil.copy2(ROOT / "install-nightly.sh", wrapper)
    stub = '''#!/usr/bin/env bash
python3 -c 'import json, os, sys; print(json.dumps({"args":sys.argv[1:], "home":os.environ["MERCURY_HOME"], "command":os.environ["MERCURY_CMD"]}))' "$@"
'''
    mocks = tmp_path / "bin"
    mocks.mkdir()
    curl = mocks / "curl"
    curl.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
if any("api.github.com" in arg for arg in sys.argv):
    print(json.dumps([{"tag_name":"v0.3.4", "prerelease":False}, {"tag_name":"v0.3.4-nightly", "prerelease":True}]))
else:
    print(Path(__file__).with_name("install-stub").read_text())
''')
    curl.chmod(0o755)
    (mocks / "install-stub").write_text(stub)
    if not remote:
        (tmp_path / "install.sh").write_text(stub)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": str(mocks) + ":" + os.environ["PATH"], "HOME": str(home),
           "MERCURY_CHANNEL": "stable"}
    result = subprocess.run(["bash", str(wrapper), "--skip-setup"], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.splitlines()[-1])
    assert output["args"] == ["--channel", "nightly", "v0.3.4-nightly", "--skip-setup"]
    assert output["command"] == "mercury-nightly"
    assert output["home"] == str(home / ".mercury-nightly")
