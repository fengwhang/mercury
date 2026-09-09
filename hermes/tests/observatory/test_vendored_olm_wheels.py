"""Vendored python-olm cp313 wheels — resolution, hash pin, offline install.

python-olm 3.2.16 publishes no cp313 wheel on PyPI, so the repo vendors
per-arch wheels under hermes/observatory/wheels/ (SHA256SUMS-pinned) and
every auto path (setup, headless, install.sh, update) consumes them —
never a container build, never a host compiler.
"""
from __future__ import annotations

import hashlib
import inspect
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import mercury_cli.setup as setup_mod
import observatory.e2ee as e2ee_mod
from observatory import provision as provision_mod

WHEELS_DIR = Path(provision_mod.__file__).resolve().parent / "wheels"
REPO_ROOT = Path(provision_mod.__file__).resolve().parent.parent.parent

HOST_VENDORED = sys.platform == "linux" and sys.version_info[:2] == (3, 13)
NEEDS_HOST_WHEEL = pytest.mark.skipif(
    not HOST_VENDORED, reason="needs linux cp313 (the only vendored configuration)")


def _pins() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (WHEELS_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1]] = parts[0]
    return out


class TestWheelSet:
    def test_both_arch_wheels_present_and_pinned(self):
        pins = _pins()
        for arch in ("x86_64", "aarch64"):
            name = f"python_olm-{provision_mod.PYTHON_OLM_VERSION}-cp313-cp313-linux_{arch}.whl"
            assert name in pins, f"{name} missing from SHA256SUMS"
            path = WHEELS_DIR / name
            assert path.is_file(), f"{name} not checked in"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest.lower() == pins[name].lower(), f"{name} hash mismatch"

    def test_arch_wheels_share_layout(self):
        names = {}
        for arch in ("x86_64", "aarch64"):
            name = f"python_olm-{provision_mod.PYTHON_OLM_VERSION}-cp313-cp313-linux_{arch}.whl"
            with zipfile.ZipFile(WHEELS_DIR / name) as zf:
                names[arch] = sorted(zf.namelist())
        assert names["x86_64"] == names["aarch64"]
        assert any(n.endswith(".abi3.so") for n in names["x86_64"])

    @pytest.mark.skipif(shutil.which("readelf") is None, reason="readelf required")
    def test_aarch64_wheel_is_aarch64_elf(self, tmp_path):
        name = f"python_olm-{provision_mod.PYTHON_OLM_VERSION}-cp313-cp313-linux_aarch64.whl"
        with zipfile.ZipFile(WHEELS_DIR / name) as zf:
            so = next(n for n in zf.namelist() if n.endswith(".so"))
            target = tmp_path / "a64.so"
            target.write_bytes(zf.read(so))
        head = subprocess.run(
            ["readelf", "-h", str(target)], capture_output=True, text=True)
        assert head.returncode == 0
        assert "AArch64" in head.stdout
        dyn = subprocess.run(
            ["readelf", "-d", str(target)], capture_output=True, text=True)
        assert "NEEDED" in dyn.stdout and "libc.so.6" in dyn.stdout

    @NEEDS_HOST_WHEEL
    def test_this_host_wheel_resolves_and_verifies(self):
        cand = provision_mod._vendored_olm_wheel()
        assert cand is not None and cand.is_file()
        assert provision_mod._verified_vendored_wheel(cand) == cand

    def test_tampered_wheel_refused(self, tmp_path):
        (tmp_path / "SHA256SUMS").write_text(
            (WHEELS_DIR / "SHA256SUMS").read_text(encoding="utf-8"), encoding="utf-8")
        real = next(WHEELS_DIR.glob("python_olm-*.whl"))
        bad = tmp_path / real.name
        data = bytearray(real.read_bytes())
        data[len(data) // 2] ^= 0xFF
        bad.write_bytes(bytes(data))
        with pytest.raises(provision_mod.ProvisionError):
            provision_mod._verified_vendored_wheel(bad)

    def test_missing_pin_refused(self, tmp_path):
        wheel = tmp_path / "python_olm-3.2.16-cp313-cp313-linux_x86_64.whl"
        wheel.write_bytes(b"PK")
        with pytest.raises(provision_mod.ProvisionError):
            provision_mod._verified_vendored_wheel(wheel)


class TestOfflineInstall:
    @NEEDS_HOST_WHEEL
    @pytest.mark.skipif(shutil.which("uv") is None, reason="uv required")
    def test_offline_install_from_vendored_dir(self, tmp_path):
        """The compiled piece installs with no network: fresh venv with
        system site packages (cffi visible), then a --offline install of
        python-olm from a copy of the vendored dir, then a real
        encrypt/decrypt round trip in the new venv."""
        wheels_copy = tmp_path / "wheels"
        shutil.copytree(WHEELS_DIR, wheels_copy)
        venv = tmp_path / "venv"
        r = subprocess.run(
            ["uv", "venv", "--system-site-packages", "--python", sys.executable, str(venv)],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-500:]
        vpy = str(venv / "bin" / "python")
        r = subprocess.run(
            ["uv", "pip", "install", "--python", vpy, "--offline",
             "--find-links", str(wheels_copy), "python-olm==3.2.16"],
            capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
        probe = (
            "from olm import Account, OutboundSession, InboundSession;"
            "a1, a2 = Account(), Account();"
            "a2.generate_one_time_keys(1);"
            "otk = list(a2.one_time_keys['curve25519'].values())[0];"
            "s1 = OutboundSession(a1, a2.identity_keys['curve25519'], otk);"
            "m = s1.encrypt('vendored-offline');"
            "s2 = InboundSession(a2, m);"
            "assert s2.decrypt(m) == 'vendored-offline';"
            "print('OFFLINE-ROUNDTRIP-OK')"
        )
        r = subprocess.run([vpy, "-c", probe], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
        assert "OFFLINE-ROUNDTRIP-OK" in r.stdout


class TestEnsureCryptoStack:
    def test_disabled_returns_early(self, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: False)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)

        def _boom(*a, **k):
            raise AssertionError("no install when e2ee is off")
        monkeypatch.setattr(provision_mod, "_crypto_pip_install", _boom)
        assert provision_mod.ensure_crypto_stack() == "disabled-already"

    def test_ready_when_stack_present(self, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: True)
        assert provision_mod.ensure_crypto_stack() == "ready"

    @NEEDS_HOST_WHEEL
    def test_installs_vendored_wheel(self, monkeypatch):
        """The vendored wheel path (not the index) reaches the installer
        with the matrix companions, then reports installed."""
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
        calls: list = []

        def _fake_install(python_bin, args):
            calls.append((python_bin, list(args)))
            return True, ""
        monkeypatch.setattr(provision_mod, "_crypto_pip_install", _fake_install)
        assert provision_mod.ensure_crypto_stack() == "installed"
        assert len(calls) == 1
        python_bin, args = calls[0]
        assert python_bin == sys.executable
        wheel_args = [a for a in args if a.endswith(".whl")]
        assert len(wheel_args) == 1
        assert Path(wheel_args[0]).parent == WHEELS_DIR
        assert "mautrix[encryption]==0.21.1" in args
        assert "aiosqlite==0.22.1" in args

    def test_install_failure_falls_back_and_disables(self, tmp_path, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
        monkeypatch.setattr(
            provision_mod, "_crypto_pip_install", lambda *a, **k: (False, "boom"))
        home = tmp_path / "home"
        assert provision_mod.ensure_crypto_stack(home) == "fallback-disabled"
        assert "e2ee: false" in (home / "config.yaml").read_text(encoding="utf-8")

    def test_missing_wheel_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)
        monkeypatch.setattr(provision_mod, "_vendored_olm_wheel", lambda: None)
        home = tmp_path / "home"
        assert provision_mod.ensure_crypto_stack(home) == "fallback-disabled"
        assert "e2ee: false" in (home / "config.yaml").read_text(encoding="utf-8")

    def test_unverified_wheel_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(e2ee_mod, "e2ee_enabled", lambda home=None: True)
        monkeypatch.setattr(e2ee_mod, "e2ee_available", lambda: False)

        def _raise(cand):
            raise provision_mod.ProvisionError("hash mismatch")
        monkeypatch.setattr(provision_mod, "_verified_vendored_wheel", _raise)
        home = tmp_path / "home"
        assert provision_mod.ensure_crypto_stack(home) == "fallback-disabled"
        assert "e2ee: false" in (home / "config.yaml").read_text(encoding="utf-8")

    def test_never_raises(self, tmp_path, monkeypatch):
        """Every probe/helper exploding still degrades to the fallback —
        the wizard must survive."""
        monkeypatch.setattr(
            e2ee_mod, "e2ee_enabled",
            lambda home=None: (_ for _ in ()).throw(RuntimeError("cfg")))
        monkeypatch.setattr(
            e2ee_mod, "e2ee_available",
            lambda: (_ for _ in ()).throw(RuntimeError("probe")))
        monkeypatch.setattr(
            provision_mod, "_vendored_olm_wheel",
            lambda: (_ for _ in ()).throw(RuntimeError("sel")))
        home = tmp_path / "home"
        assert provision_mod.ensure_crypto_stack(home) == "fallback-disabled"

    def test_set_e2ee_preserves_other_keys(self, tmp_path):
        home = tmp_path / "home"
        cfg = home / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("observatory:\n  enabled: true\n  offline: true\nother: 1\n",
                       encoding="utf-8")
        provision_mod.set_observatory_e2ee(False, home)
        text = cfg.read_text(encoding="utf-8")
        assert "e2ee: false" in text
        assert "enabled: true" in text and "other: 1" in text
        provision_mod.set_observatory_e2ee(True, home)
        assert "e2ee: true" in cfg.read_text(encoding="utf-8")


class TestSourceGuard:
    """The auto path never references a container build: setup consumes
    the checked-in wheels, and the podman script is manual-only."""

    @staticmethod
    def _auto_sources() -> dict[str, str]:
        fns = {
            "provision.ensure_crypto_stack": provision_mod.ensure_crypto_stack,
            "provision._vendored_olm_wheel": provision_mod._vendored_olm_wheel,
            "provision._verified_vendored_wheel": provision_mod._verified_vendored_wheel,
            "provision._crypto_pip_install": provision_mod._crypto_pip_install,
            "provision.set_observatory_e2ee": provision_mod.set_observatory_e2ee,
            "setup._auto_ensure_crypto": setup_mod._auto_ensure_crypto,
            "setup.run_headless_observatory_setup": setup_mod.run_headless_observatory_setup,
        }
        return {name: inspect.getsource(fn) for name, fn in fns.items()}

    def test_no_container_refs_in_auto_functions(self):
        for name, src in self._auto_sources().items():
            lowered = src.lower()
            assert "podman" not in lowered, name
            assert "docker" not in lowered, name
            assert "build_python_olm" not in lowered, name

    def test_install_sh_never_invokes_container_or_build_script(self):
        text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        assert "podman" not in text.lower()
        lines = text.splitlines()
        in_obs = False
        for line in lines:
            if line.startswith("install_observatory()"):
                in_obs = True
            elif in_obs and line == "}":
                break
            if in_obs and "build_python_olm_wheel.sh" in line:
                stripped = line.strip()
                assert stripped.startswith("#") or "log_warn" in stripped, line

    def test_make_dist_never_invokes_build_script(self):
        text = (REPO_ROOT / "scripts" / "make-dist.sh").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "build_python_olm_wheel.sh" in line:
                stripped = line.strip()
                # comments + gate-message hints only — never executed
                assert stripped.startswith("#") or stripped.startswith("echo"), line


class TestSetupAutoCrypto:
    def test_delegates_to_provision(self):
        assert setup_mod._auto_ensure_crypto(SimpleNamespace(
            ensure_crypto_stack=lambda: "installed")) == "installed"

    def test_missing_helper_degrades_silently(self):
        assert setup_mod._auto_ensure_crypto(SimpleNamespace()) == "skipped-unavailable"

    def test_helper_error_degrades(self):
        def _boom():
            raise RuntimeError("nope")
        assert setup_mod._auto_ensure_crypto(SimpleNamespace(
            ensure_crypto_stack=_boom)) == "skipped-error"

    def test_headless_setup_runs_crypto_then_card(self, monkeypatch):
        seen: dict = {}
        fake = SimpleNamespace(
            provision_in_wizard=lambda *a, **k: seen.setdefault("provisioned", True),
            status_summary=lambda *a, **k: {
                "provisioned": True, "homeserver_reachable": False,
                "homeserver_url": "http://127.0.0.1:8008", "unit_active": False,
                "unit_name": "u", "enabled": True,
                "owner_credentials_exist": False,
            },
            ensure_crypto_stack=lambda *a, **k: seen.setdefault("crypto", "installed"),
        )
        monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
        monkeypatch.setattr(setup_mod, "_tailscale_status", lambda obs: {"available": False})
        monkeypatch.setattr(setup_mod, "_print_observatory_setup_card",
                            lambda status, ts: seen.setdefault("card", True))
        monkeypatch.setattr(setup_mod, "_maybe_print_bind_mismatch_action",
                            lambda obs, ts: None)
        setup_mod.run_headless_observatory_setup()
        assert seen.get("provisioned") is True
        assert seen.get("crypto") == "installed"
        assert seen.get("card") is True

    def test_wizard_install_runs_crypto(self, monkeypatch, capsys):
        seen: dict = {}
        fake = SimpleNamespace(
            status_summary=lambda *a, **k: {
                "provisioned": False, "homeserver_reachable": False,
                "homeserver_url": "http://127.0.0.1:8008", "unit_active": False,
                "unit_name": "u", "enabled": True,
            },
            provision_in_wizard=lambda *a, **k: seen.setdefault("provisioned", True),
            ensure_crypto_stack=lambda *a, **k: seen.setdefault("crypto", "installed"),
        )
        monkeypatch.setattr(setup_mod, "_load_observatory_provision", lambda: fake)
        monkeypatch.setattr(setup_mod, "prompt_choice", lambda *a, **k: 0)
        monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **k: True)
        setup_mod.setup_observatory({})
        assert seen.get("provisioned") is True
        assert seen.get("crypto") == "installed"
