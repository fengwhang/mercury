"""Release tracks: stable vs nightly resolution."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def isolated_channel(monkeypatch):
    monkeypatch.delenv("MERCURY_CHANNEL", raising=False)


def _rel(tag, *, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft,
            "assets": []}


def test_channel_marker_wins(monkeypatch, tmp_path) -> None:
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    (tmp_path / "channel").write_text("nightly\n")
    assert ur._install_channel() == "nightly"


def test_no_marker_version_shape_decides(monkeypatch, tmp_path) -> None:
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(ur, "_installed_version", lambda: "0.0.141")
    assert ur._install_channel() == "nightly"
    monkeypatch.setattr(ur, "_installed_version", lambda: "0.1.0")
    assert ur._install_channel() == "stable"


def test_nightly_takes_newest_prerelease(monkeypatch, tmp_path) -> None:
    import urllib.request
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    (tmp_path / "channel").write_text("nightly")
    payload = json.dumps([
        _rel("v0.1.0"),
        _rel("v0.0.142", prerelease=True),
        _rel("v0.0.143", prerelease=True, draft=True),
    ]).encode()

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return payload

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rel = ur._latest_release()
    assert rel is not None and rel["tag_name"] == "v0.0.142"
    assert "per_page" in seen["url"]


def test_stable_uses_latest_endpoint(monkeypatch, tmp_path) -> None:
    import urllib.request
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    (tmp_path / "channel").write_text("stable")
    payload = json.dumps(_rel("v0.1.0")).encode()

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return payload

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rel = ur._latest_release()
    assert rel is not None and rel["tag_name"] == "v0.1.0"
    assert seen["url"].endswith("/releases/latest")


def test_is_current_compares_within_track(monkeypatch, tmp_path) -> None:
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    (tmp_path / "channel").write_text("nightly")
    monkeypatch.setattr(ur, "_installed_version", lambda: "0.0.141")
    monkeypatch.setattr(
        ur, "_latest_release",
        lambda *a, **k: _rel("v0.0.141", prerelease=True))
    assert ur.is_current() is True


@pytest.mark.parametrize("channel,saved", [("stable", "nightly"), ("nightly", "stable")])
def test_launcher_channel_wins_over_stale_marker(monkeypatch, tmp_path, channel, saved):
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_CHANNEL", channel)
    (tmp_path / "channel").write_text(saved + "\n")
    assert ur._install_channel() == channel


def test_invalid_environment_channel_uses_saved_track(monkeypatch, tmp_path):
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_CHANNEL", "bogus")
    (tmp_path / "channel").write_text("nightly\n")
    assert ur._install_channel() == "nightly"


def test_update_never_downgrades_after_stable_promotion(monkeypatch, tmp_path, capsys):
    from mercury_cli import update_release as ur

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("MERCURY_CHANNEL", "nightly")
    monkeypatch.setattr(ur, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(ur, "_installed_version", lambda: "0.3.4")
    monkeypatch.setattr(ur, "_latest_release", lambda **kwargs: _rel("v0.3.3", prerelease=True))

    def forbidden(*args, **kwargs):
        pytest.fail("Older release must not trigger a download, checksum query, or swap")

    for name in ("_release_sha256", "_download", "_swap_tree"):
        monkeypatch.setattr(ur, name, forbidden)
    assert ur.update_from_release(assume_yes=True) == 0
    assert "Keeping v0.3.4" in capsys.readouterr().out
