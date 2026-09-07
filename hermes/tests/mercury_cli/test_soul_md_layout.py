"""Regression tests: SOUL.md must seed to (and be read from) the layout-correct
location — never a second top-level copy.

Bug: with MERCURY_HOME and HERMES_HOME both unset, ``ensure_hermes_home()``
resolved home to the platform-default ``~/.mercury`` (the Mercury root) but
``_ensure_default_soul_md()`` still seeded ``home/SOUL.md`` top-level. The
canonical location under the Mercury layout is ``<root>/config/SOUL.md`` —
the first candidate ``agent.prompt_builder.load_soul_md`` reads and the path
install.sh / bin/mercury seed — so users got a stray ``~/.mercury/SOUL.md``
next to the real ``~/.mercury/config/SOUL.md``. ``mercury doctor`` had the
same top-level assumption and could create the stray via ``--fix``.

Pinned behavior (``mercury_cli.config.soul_md_locations`` is the shared
source of truth for both writers):

- Mercury layout ($MERCURY_HOME set, home == platform-default root, or home
  carrying a config/ dir): seed/read ``<root>/config/SOUL.md`` only.
- Pure legacy hermes homes (HERMES_HOME at a non-mercury dir, no config/
  subdir, no $MERCURY_HOME): stock top-level ``home/SOUL.md`` — zero
  regressions.
- Per-profile homes: SOUL.md stays at the profile root by design (seeded by
  ``mercury profile create``).
- A pre-layout top-level stray is migrated into config/ exactly once (mirror
  of install.sh); when both exist it is left alone and never read.
"""

import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

from mercury_cli import config as cfg

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins the POSIX platform-default home name (.mercury) via HOME",
)


def _scrub_home_env(monkeypatch, tmp_path):
    """Reproduce the bug's launch conditions: both home env vars unset,
    HOME pointed at the test tmpdir so the platform-default Mercury root
    resolves inside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("MERCURY_HOME", raising=False)


class TestMercuryLayoutSeeding:
    @_POSIX_ONLY
    def test_platform_default_home_seeds_config_soul_md_only(self, tmp_path, monkeypatch):
        # The exact bug: env-scrubbed resolve lands home on the
        # platform-default ~/.mercury — the Mercury root — so SOUL.md must
        # land in config/, never top-level.
        _scrub_home_env(monkeypatch, tmp_path)
        cfg.ensure_hermes_home()

        seeded = tmp_path / ".mercury" / "config" / "SOUL.md"
        assert seeded.is_file()
        assert seeded.read_text(encoding="utf-8").strip() != ""
        assert not (tmp_path / ".mercury" / "SOUL.md").exists()

    def test_mercury_home_env_seeds_shared_config_only(self, tmp_path, monkeypatch):
        # $MERCURY_HOME set: home resolves to the engine-private
        # $MERCURY_HOME/hermes, but THE persona file is the shared
        # $MERCURY_HOME/config/SOUL.md — no copy anywhere else.
        root = tmp_path / "envroot"
        monkeypatch.setenv("MERCURY_HOME", str(root))
        monkeypatch.delenv("HERMES_HOME", raising=False)
        cfg.ensure_hermes_home()

        assert (root / "config" / "SOUL.md").is_file()
        assert not (root / "SOUL.md").exists()
        assert not (root / "hermes" / "SOUL.md").exists()

    @_POSIX_ONLY
    def test_existing_config_dir_marks_mercury_layout(self, tmp_path, monkeypatch):
        # A home that already carries a config/ dir is the Mercury layout
        # even off the default path (e.g. HERMES_HOME pointed at a mercury
        # root): seed inside config/, not top-level.
        home = tmp_path / "custom-root"
        (home / "config").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("MERCURY_HOME", raising=False)
        cfg.ensure_hermes_home()

        assert (home / "config" / "SOUL.md").is_file()
        assert not (home / "SOUL.md").exists()


class TestLegacyHermesLayout:
    def test_legacy_hermes_home_still_seeds_top_level(self, tmp_path, monkeypatch):
        # Zero-regression guarantee: a pure legacy hermes install
        # (HERMES_HOME at a non-mercury dir, no config/ subdir, no
        # $MERCURY_HOME) keeps the stock top-level persona.
        legacy = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(legacy))
        monkeypatch.delenv("MERCURY_HOME", raising=False)
        cfg.ensure_hermes_home()

        assert (legacy / "SOUL.md").is_file()
        assert not (legacy / "config").exists()

    def test_profile_home_keeps_soul_at_profile_root(self, tmp_path, monkeypatch):
        # Profiles own a top-level SOUL.md by design (seeded by
        # `mercury profile create`); ensure_hermes_home must not re-point
        # them at the shared config/ dir nor touch the shared tree.
        root = tmp_path / ".mercury"
        profile = root / "profiles" / "bot"
        profile.mkdir(parents=True)  # named profiles must exist before use
        monkeypatch.setenv("MERCURY_HOME", str(root))
        monkeypatch.setenv("HERMES_HOME", str(profile))
        cfg.ensure_hermes_home()

        assert (profile / "SOUL.md").is_file()
        assert not (profile / "config").exists()
        assert not (root / "config" / "SOUL.md").exists()


class TestStrayMigration:
    @_POSIX_ONLY
    def test_top_level_stray_migrated_into_config(self, tmp_path, monkeypatch):
        # Mirror of install.sh: pre-layout top-level SOUL.md moves into
        # config/ exactly once (content preserved — it may be user-authored).
        home = tmp_path / ".mercury"
        home.mkdir()
        (home / "SOUL.md").write_text("MY PERSONA\n", encoding="utf-8")
        _scrub_home_env(monkeypatch, tmp_path)
        cfg.ensure_hermes_home()

        assert (home / "config" / "SOUL.md").read_text(encoding="utf-8") == "MY PERSONA\n"
        assert not (home / "SOUL.md").exists()

    @_POSIX_ONLY
    def test_stray_left_alone_when_config_copy_exists(self, tmp_path, monkeypatch):
        # Both present: never delete user files automatically. The stray
        # stays, untouched and unread (the loader prefers config/).
        home = tmp_path / ".mercury"
        (home / "config").mkdir(parents=True)
        (home / "config" / "SOUL.md").write_text("CANONICAL\n", encoding="utf-8")
        (home / "SOUL.md").write_text("OLD STRAY\n", encoding="utf-8")
        _scrub_home_env(monkeypatch, tmp_path)
        cfg.ensure_hermes_home()

        assert (home / "config" / "SOUL.md").read_text(encoding="utf-8") == "CANONICAL\n"
        assert (home / "SOUL.md").read_text(encoding="utf-8") == "OLD STRAY\n"


def _setup_doctor_env(monkeypatch, tmp_path):
    """Minimal HERMES_HOME + PROJECT_ROOT for an in-process doctor run
    (same stubbing recipe as tests/mercury_cli/test_doctor_command_install.py)."""
    home = tmp_path / ".mercury"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    from mercury_cli import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))

    # Stub model_tools so doctor doesn't fail on import
    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    # Stub auth checks + httpx.get so no network/provider state leaks in
    try:
        from mercury_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
    except Exception:
        pass
    try:
        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
    except Exception:
        pass

    return doctor_mod, home


def _run_doctor(doctor_mod, fix=False):
    """Run doctor and capture stdout."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=fix))
    return buf.getvalue()


class TestDoctorSoulResolution:
    @_POSIX_ONLY
    def test_soul_check_passes_on_config_dir_without_top_level(self, tmp_path, monkeypatch):
        # Under the Mercury layout doctor must report the config/ persona —
        # and must not conjure a top-level file even when running --fix.
        _scrub_home_env(monkeypatch, tmp_path)
        doctor_mod, home = _setup_doctor_env(monkeypatch, tmp_path)
        (home / "config").mkdir(exist_ok=True)
        (home / "config" / "SOUL.md").write_text(
            "You are a test persona with real content.\n", encoding="utf-8"
        )

        out = _run_doctor(doctor_mod, fix=False)
        assert f"{home}/config/SOUL.md exists (persona configured)" in out
        assert f"{home}/SOUL.md exists" not in out
        assert not (home / "SOUL.md").exists()

    @_POSIX_ONLY
    def test_soul_fix_run_ends_with_config_copy_and_no_top_level(self, tmp_path, monkeypatch):
        # doctor --fix with no persona anywhere. Note: doctor's own config
        # load calls ensure_hermes_home() first, so the fixed seeder has
        # already placed config/SOUL.md before the check runs — exactly the
        # point: no code path may fall back to a top-level file.
        _scrub_home_env(monkeypatch, tmp_path)
        doctor_mod, home = _setup_doctor_env(monkeypatch, tmp_path)

        out = _run_doctor(doctor_mod, fix=True)
        assert f"{home}/config/SOUL.md exists (persona configured)" in out
        assert (home / "config" / "SOUL.md").is_file()
        assert not (home / "SOUL.md").exists()
        assert f"{home}/SOUL.md" not in out.replace(f"{home}/config/SOUL.md", "")

    def test_legacy_home_still_checks_top_level(self, tmp_path, monkeypatch):
        # Zero-regression: pure legacy hermes home — doctor keeps checking
        # (and reporting) the stock top-level SOUL.md.
        legacy = tmp_path / "hermes"
        legacy.mkdir()
        (legacy / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        (legacy / "SOUL.md").write_text("LEGACY PERSONA\n", encoding="utf-8")

        monkeypatch.delenv("MERCURY_HOME", raising=False)

        from mercury_cli import doctor as doctor_mod

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        monkeypatch.setattr(doctor_mod, "HERMES_HOME", legacy)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(legacy))
        fake_model_tools = types.SimpleNamespace(
            check_tool_availability=lambda *a, **kw: ([], []),
            TOOLSET_REQUIREMENTS={},
        )
        monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

        out = _run_doctor(doctor_mod, fix=False)
        assert f"{legacy}/SOUL.md exists (persona configured)" in out
        assert not (legacy / "config").exists()
