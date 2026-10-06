"""Exercise Mercury's distribution installer with local archives and no services."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import shlex
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _archive(tmp_path: Path) -> Path:
    tree = tmp_path / "payload" / "mercury"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin/mercury").write_text("#!/bin/bash\nexit 0\n")
    (tree / "hermes/ui-tui/dist").mkdir(parents=True)
    (tree / "hermes/ui-tui/dist/entry.js").write_text("// built\n")
    archive = tmp_path / "mercury.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tree, arcname="mercury")
    Path(str(archive) + ".sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  mercury.tar.gz\n")
    return archive


def _installer(tmp_path: Path, arguments: list[str], tail: str, *, downloads=False):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "MERCURY_INHERIT_FROM": str(home / "absent")}
    if downloads:
        archive = _archive(tmp_path)
        tools = tmp_path / "tools"
        tools.mkdir()
        curl = tools / "curl"
        curl.write_text(
            "#!/bin/bash\nset -eu\n"
            "printf '%s\\n' \"$*\" >> \"$DOWNLOAD_LOG\"\n"
            "output=; url=\n"
            "while [ $# -gt 0 ]; do\n"
            "  case $1 in -o) output=$2; shift 2;; http*) url=$1; shift;; *) shift;; esac\n"
            "done\n"
            "[ -n \"$output\" ] || exit 0\n"
            "case $url in *.sha256) cp \"$ARCHIVE.sha256\" \"$output\";; *) cp \"$ARCHIVE\" \"$output\";; esac\n")
        curl.chmod(0o755)
        env.update(PATH=str(tools) + os.pathsep + env["PATH"],
                   ARCHIVE=str(archive), DOWNLOAD_LOG=str(tmp_path / "downloads.log"))
    result = subprocess.run(
        ["bash", "-c", 'source "$1" "${@:2}"; ' + tail,
         "installer-test", str(ROOT / "install.sh"), *arguments],
        env=env, capture_output=True, text=True, timeout=30)
    return result


@pytest.mark.parametrize("tag,libc,suffix", [
    ("v0.4.4-nightly", "glibc", "x64"),
    ("v0.4.4-nightly", "musl", "musl-x64"),
    ("v0.4.4", "glibc", "x64"),
])
def test_tag_selects_published_asset_basename(tmp_path, tag, libc, suffix):
    result = _installer(tmp_path, ["--channel", "nightly", tag],
                        f'LIBC={libc}; uname() {{ echo x86_64; }}; '
                        'select_omp_binary() { :; }; fetch_tarball', downloads=True)
    assert result.returncode == 0, result.stdout + result.stderr
    requests = (tmp_path / "downloads.log").read_text()
    expected = f"/download/{tag}/mercury-{tag[1:]}-{suffix}.tar.gz"
    assert expected in requests
    assert expected + ".sha256" in requests


@pytest.mark.parametrize("channel,name", [("stable", "mercury"), ("nightly", "mercury-nightly")])
def test_explicit_channel_selects_isolated_default_home_and_command(tmp_path, channel, name):
    result = _installer(tmp_path, ["--channel", channel],
                        'printf "\\nselected=%s|%s|%s|%s\\n" '
                        '"$MERCURY_HOME" "$INSTALL_ROOT" "$MANAGED_BIN" "$MERCURY_CMD"')
    assert result.returncode == 0, result.stderr
    home = tmp_path / "home" / f".{name}"
    assert f"selected={home}|{home}/mercury-agent|{home}/bin|{name}" in result.stdout


def test_supported_python_creation_failure_never_uses_unconstrained_python(tmp_path):
    uv = tmp_path / "uv"
    calls = tmp_path / "uv-calls"
    uv.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        "case \"$*\" in *'--python >=3.11,<3.14'*) exit 23;; esac\n"
        "exit 0\n")
    uv.chmod(0o755)
    result = _installer(
        tmp_path, [],
        f'UV_CMD={shlex.quote(str(uv))}; '
        'mkdir -p "$INSTALL_ROOT/hermes"; cd "$INSTALL_ROOT"; setup_venv')
    assert result.returncode != 0
    assert "supported Python" in result.stdout + result.stderr
    assert len(calls.read_text().splitlines()) == 1
