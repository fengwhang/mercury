"""update_release bundled-wheels arch filter — installer selects correctly.

make-dist stages BOTH arch python-olm wheels into the tarball's wheels/
(see test_make_dist_wheels_gate). Handing pip two conflicting python-olm
URLs fails the install, so _install_bundled_wheels installs this-arch olm
+ every non-olm wheel and skips the foreign-arch olm with a notice (never
passed to pip). Selection mirrors observatory.provision._vendored_olm_wheel.
"""

from __future__ import annotations

import platform
from pathlib import Path

import observatory.provision as prov
import mercury_cli.update_release as ur

X64 = "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl"
ARM = "python_olm-3.2.16-cp313-cp313-linux_aarch64.whl"
MATRIX = "mautrix-0.21.1-py3-none-any.whl"
BOTH_PLUS_MATRIX = [X64, ARM, MATRIX]


def _stage(root: Path, names: list[str]) -> None:
    wheels = root / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    for name in names:
        (wheels / name).write_bytes(b"PK\x05\x06" + b"\x00" * 18)


def _isolate(monkeypatch, machine: str) -> list[list[str]]:
    """Pin arch + gates; record every _pip_install arg list. Returns calls."""
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(prov, "observatory_enabled", lambda: True)
    monkeypatch.setattr(prov, "_vendored_olm_wheel", lambda: None)
    calls: list[list[str]] = []

    def _fake_pip(venv: Path, args: list[str]) -> tuple[bool, str]:
        calls.append(list(args))
        return True, ""

    monkeypatch.setattr(ur, "_pip_install", _fake_pip)
    return calls


def _installed_names(calls: list[list[str]]) -> list[str]:
    assert len(calls) == 1
    return [Path(a).name for a in calls[0]]


def test_x64_host_installs_x64_olm_only(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, BOTH_PLUS_MATRIX)
    calls = _isolate(monkeypatch, "x86_64")
    ur._install_bundled_wheels(tmp_path, tmp_path / "venv")
    installed = _installed_names(calls)
    assert X64 in installed and MATRIX in installed
    assert ARM not in installed
    assert ARM in capsys.readouterr().out  # skipped with notice


def test_arm_host_installs_arm_olm_only(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, BOTH_PLUS_MATRIX)
    calls = _isolate(monkeypatch, "aarch64")
    ur._install_bundled_wheels(tmp_path, tmp_path / "venv")
    installed = _installed_names(calls)
    assert ARM in installed and MATRIX in installed
    assert X64 not in installed
    assert X64 in capsys.readouterr().out


def test_unknown_machine_skips_all_olm(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, BOTH_PLUS_MATRIX)
    calls = _isolate(monkeypatch, "riscv64")
    ur._install_bundled_wheels(tmp_path, tmp_path / "venv")
    assert _installed_names(calls) == [MATRIX]
    out = capsys.readouterr().out
    assert X64 in out and ARM in out


def test_split_unit_table(monkeypatch):
    whls = [Path(X64), Path(ARM), Path(MATRIX)]
    for machine, want in (("x86_64", X64), ("amd64", X64),
                          ("aarch64", ARM), ("arm64", ARM)):
        monkeypatch.setattr(platform, "machine", lambda m=machine: m)
        keep, skipped = ur._split_bundled_wheels(whls)
        assert [p.name for p in keep] == [want, MATRIX]
        assert [p.name for p in skipped] == [ARM if want == X64 else X64]
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    keep, skipped = ur._split_bundled_wheels(whls)
    assert [p.name for p in keep] == [MATRIX]
    assert sorted(p.name for p in skipped) == sorted([X64, ARM])


def test_no_olm_wheels_pass_through_untouched(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, [MATRIX])
    calls = _isolate(monkeypatch, "x86_64")
    ur._install_bundled_wheels(tmp_path, tmp_path / "venv")
    assert _installed_names(calls) == [MATRIX]
    assert "foreign arch" not in capsys.readouterr().out


def test_missing_wheels_dir_makes_no_pip_call(tmp_path, monkeypatch):
    calls = _isolate(monkeypatch, "x86_64")
    ur._install_bundled_wheels(tmp_path, tmp_path / "venv")
    assert calls == []
