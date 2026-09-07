"""Test-safety guard: fixture tokens must never clobber the real ~/.mercury/.env.

Incident (2026-09-07, verified forensically): during a Mercury-session pytest
run, ``TestSaveEnvValueSecure::test_secure_save_returns_metadata_only`` called
``save_env_value_secure('GITHUB_TOKEN', 'ghp_test_secret')`` with only
HERMES_HOME patched to tmp_path. The launcher exports
``MERCURY_HOME=/home/phoenix/.mercury`` into the session, ``get_env_path()``
keys on ``MERCURY_HOME`` (it outranks HERMES_HOME), and the fixture token
OVERWROTE the operator's real 93-char GITHUB_TOKEN — a sibling test also
briefly corrupted the real config.yaml one second earlier.

``mercury_cli.config._guard_test_write_target`` now refuses, under any pytest
marker, ``.env``/``config.yaml`` writes whose resolved target is outside
``tempfile.gettempdir()``. These tests pin that behavior.
"""

import os
import tempfile
from unittest.mock import patch

import pytest

from mercury_cli.config import save_env_value_secure

_SENTINEL_ENV = "GITHUB_TOKEN=ghp_REAL_93_CHAR_SENTINEL_TOKEN_do_not_clobber\n"


def test_guard_blocks_real_home_env_write_under_pytest(tmp_path, monkeypatch):
    """The incident, reproduced: MERCURY_HOME at a real-looking home wins over
    the patched HERMES_HOME, and the guard must refuse loudly BEFORE writing."""
    # Shrink what the guard believes the tempdir to be, so the fake home (a
    # sibling subtree of tmp_path) counts as "outside" while every file this
    # test creates still physically lives under pytest's tmp root.
    guard_root = tmp_path / "guard-sees-this-as-temp"
    guard_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(guard_root))

    fake_mercury_home = tmp_path / "realistic-home"
    fake_mercury_home.mkdir()
    (fake_mercury_home / ".env").write_text(_SENTINEL_ENV, encoding="utf-8")

    patched_hermes_home = tmp_path / "test-patched-home"
    patched_hermes_home.mkdir()

    with patch.dict(
        os.environ,
        {
            # The incident condition: an ambient MERCURY_HOME pointing at a
            # real-looking home, exactly as the Mercury launcher exports it.
            "MERCURY_HOME": str(fake_mercury_home),
            # What the offending test patched — and only this.
            "HERMES_HOME": str(patched_hermes_home),
            # Arm the guard explicitly so the test is self-sufficient even
            # when something strips pytest's own marker.
            "PYTEST_CURRENT_TEST": "test_env_test_safety_guard.py::incident",
        },
    ):
        with pytest.raises(RuntimeError, match="test-safety guard"):
            save_env_value_secure("GITHUB_TOKEN", "ghp_test_secret")

    # The real-looking home's .env is byte-for-byte unchanged.
    assert (fake_mercury_home / ".env").read_text(encoding="utf-8") == _SENTINEL_ENV
    # The refusal happened before ANY write — not even the patched home saw one.
    assert not (patched_hermes_home / ".env").exists()


def test_guard_blocks_real_home_config_write_under_pytest(tmp_path, monkeypatch):
    """Same refusal for config.yaml via the atomic_config_write chokepoint."""
    from mercury_cli.config import atomic_config_write

    guard_root = tmp_path / "guard-sees-this-as-temp"
    guard_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(guard_root))

    real_looking_config = tmp_path / "realistic-home" / "config.yaml"
    real_looking_config.parent.mkdir()
    real_looking_config.write_text("model: test/original\n", encoding="utf-8")

    with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "test_env_test_safety_guard.py::cfg"}):
        with pytest.raises(RuntimeError, match="test-safety guard"):
            atomic_config_write(real_looking_config, {"model": "test/fixture-clobber"})

    assert real_looking_config.read_text(encoding="utf-8") == "model: test/original\n"


def test_guard_allows_normal_write_without_pytest_markers(tmp_path, monkeypatch):
    """Without the pytest markers the same call must succeed normally."""
    for marker in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION", "HERMES_TEST_ISOLATION"):
        monkeypatch.delenv(marker, raising=False)

    fake_mercury_home = tmp_path / "normal-home"
    fake_mercury_home.mkdir()

    with patch.dict(os.environ, {"MERCURY_HOME": str(fake_mercury_home)}):
        result = save_env_value_secure("GITHUB_TOKEN", "ghp_test_secret")

    assert result["success"] is True
    assert result["stored_as"] == "GITHUB_TOKEN"
    env_text = (fake_mercury_home / ".env").read_text(encoding="utf-8")
    assert "GITHUB_TOKEN=ghp_test_secret" in env_text


def test_guard_allows_tmp_path_writes_under_pytest(tmp_path):
    """Under pytest a tmp_path write (the hermetic pattern) passes the guard."""
    with patch.dict(
        os.environ,
        {
            "HERMES_HOME": str(tmp_path),
            "MERCURY_HOME": str(tmp_path),
            "PYTEST_CURRENT_TEST": "test_env_test_safety_guard.py::tmpok",
        },
    ):
        save_env_value_secure("GITHUB_TOKEN", "ghp_test_secret")

    assert "GITHUB_TOKEN=ghp_test_secret" in (tmp_path / ".env").read_text(encoding="utf-8")
