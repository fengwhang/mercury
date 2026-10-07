"""Capture producer -> actual mLounge staging, without a desktop or network."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from observatory.mlounge import MLoungeError, MLoungePaths, stage_mlounge_upload
from tools.computer_use import tool as computer_use
from tools.computer_use.backend import CaptureResult


@pytest.fixture
def mercury_home(tmp_path, monkeypatch):
    home = tmp_path / "mercury"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / "hermes"))
    from mercury_cli.config import get_config_path

    config = get_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"model": {"supports_vision": True}}))
    monkeypatch.setattr(computer_use, "_AUX_VISION_ROUTE_CACHE", {})
    return home


def capture(image_format="PNG", explicit_mime=True):
    stream = BytesIO()
    Image.new("RGB", (16, 16), (12, 34, 56)).save(stream, format=image_format)
    raw = stream.getvalue()
    mime = "image/jpeg" if image_format == "JPEG" else "image/png"
    return CaptureResult(
        mode="vision", width=16, height=16,
        png_b64=base64.b64encode(raw).decode("ascii"),
        png_bytes_len=len(raw), image_mime_type=mime if explicit_mime else None,
    ), raw, mime


def staged_bytes(home, result):
    token = result["url_path"].split("/")[1]
    return (MLoungePaths(home).home / "uploads" / token[:2] / token).read_bytes()


def test_actual_png_producer_is_stageable(mercury_home):
    cap, raw, _ = capture()
    screenshot = computer_use._persist_capture_image(cap)
    assert screenshot is not None
    assert Path(screenshot).read_bytes() == raw
    staged = stage_mlounge_upload(mercury_home, screenshot)
    assert staged_bytes(mercury_home, staged) == raw
    assert staged["filename"].endswith(".png")


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
@pytest.mark.parametrize("explicit_mime", [True, False])
def test_multimodal_bytes_mime_extension_and_delivery(
    mercury_home, image_format, explicit_mime,
):
    cap, raw, mime = capture(image_format, explicit_mime)
    response = computer_use._capture_response(cap)
    assert response["_multimodal"] is True
    assert response["content"][1]["image_url"]["url"] == f"data:{mime};base64,{cap.png_b64}"
    screenshot = Path(response["meta"]["screenshot_path"])
    assert str(screenshot) in response["text_summary"]
    assert "MEDIA:" not in response["text_summary"]
    assert screenshot.suffix == (".jpg" if image_format == "JPEG" else ".png")
    assert screenshot.read_bytes() == raw
    assert staged_bytes(mercury_home, stage_mlounge_upload(mercury_home, screenshot)) == raw


def test_exports_are_private_and_concurrent_paths_do_not_collide(mercury_home):
    cap, raw, _ = capture()
    old_umask = os.umask(0)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            screenshots = list(pool.map(lambda _: computer_use._persist_capture_image(cap), range(8)))
    finally:
        os.umask(old_umask)
    assert len(set(screenshots)) == 8
    for screenshot in screenshots:
        path = Path(screenshot)
        assert path.is_absolute() and not path.is_symlink()
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == raw
        assert not path.is_relative_to(mercury_home)
    assert not (mercury_home / "hermes" / "cache").exists()


@pytest.mark.parametrize("env_target", ["direct", "symlink"])
def test_hostile_temp_and_cache_environment_cannot_choose_mercury_home(
    mercury_home, tmp_path, monkeypatch, env_target,
):
    target = mercury_home / "hostile-temp"
    target.mkdir()
    if env_target == "symlink":
        link = tmp_path / "temp-link"
        link.symlink_to(target, target_is_directory=True)
        target = link
    monkeypatch.setenv("TMPDIR", str(target))
    monkeypatch.setenv("XDG_CACHE_HOME", str(target))
    monkeypatch.setattr(computer_use, "_capture_export_dir", None)
    cap, raw, _ = capture()
    screenshot = computer_use._persist_capture_image(cap)
    assert screenshot is not None
    assert not Path(screenshot).resolve().is_relative_to(mercury_home)
    assert not list((mercury_home / "hostile-temp").iterdir())
    assert staged_bytes(mercury_home, stage_mlounge_upload(mercury_home, screenshot)) == raw


@pytest.mark.parametrize("spelling", ["direct", "symlink", "traversal"])
def test_mercury_home_files_remain_denied(mercury_home, tmp_path, spelling):
    secret = mercury_home / "private-state.txt"
    secret.write_bytes(b"private fixture state")
    source = secret
    if spelling == "symlink":
        source = tmp_path / "innocent.txt"
        source.symlink_to(secret)
    elif spelling == "traversal":
        outside = tmp_path / "outside"
        outside.mkdir()
        source = outside / ".." / "mercury" / secret.name
    with pytest.raises(MLoungeError, match="refusing mercury-home file"):
        stage_mlounge_upload(mercury_home, source)
    assert not (MLoungePaths(mercury_home).home / "uploads").exists()


@pytest.mark.parametrize("spelling", ["direct", "symlink", "traversal"])
def test_protected_home_files_remain_denied(
    mercury_home, tmp_path, monkeypatch, spelling,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    protected = tmp_path / ".ssh"
    protected.mkdir()
    secret = protected / "private-state.txt"
    secret.write_bytes(b"private fixture state")
    source = secret
    if spelling == "symlink":
        source = tmp_path / "innocent.txt"
        source.symlink_to(secret)
    elif spelling == "traversal":
        outside = tmp_path / "outside"
        outside.mkdir()
        source = outside / ".." / ".ssh" / secret.name
    with pytest.raises(MLoungeError, match="refusing protected path"):
        stage_mlounge_upload(mercury_home, source)


@pytest.mark.parametrize("name", [".env", "private.key", "private.pem"])
def test_secret_looking_exports_remain_denied(mercury_home, tmp_path, name):
    source = tmp_path / name
    source.write_bytes(b"private fixture state")
    with pytest.raises(MLoungeError, match="refusing secret-looking file"):
        stage_mlounge_upload(mercury_home, source)


def test_removed_export_directory_is_recreated(mercury_home, monkeypatch):
    monkeypatch.setattr(computer_use, "_capture_export_dir", None)
    cap, raw, _ = capture()
    first = Path(computer_use._persist_capture_image(cap))
    first.unlink()
    first.parent.rmdir()
    screenshot = computer_use._persist_capture_image(cap)
    assert screenshot is not None
    path = Path(screenshot)
    assert path.parent != first.parent
    assert path.read_bytes() == raw
    assert staged_bytes(mercury_home, stage_mlounge_upload(mercury_home, path)) == raw


def test_retention_does_not_delete_untracked_matching_files(mercury_home, monkeypatch):
    monkeypatch.setattr(computer_use, "_capture_export_dir", None)
    monkeypatch.setattr(computer_use, "_MAX_CAPTURE_FILES", 2)
    cap, _, _ = capture()
    first = Path(computer_use._persist_capture_image(cap))
    foreign = first.parent / "computer_use_not-owned.png"
    foreign.write_bytes(b"unrelated fixture file")
    os.utime(foreign, (1, 1))
    assert computer_use._persist_capture_image(cap) is not None
    assert foreign.read_bytes() == b"unrelated fixture file"


def test_directory_swap_before_file_create_cannot_write_into_mercury_home(
    mercury_home, monkeypatch,
):
    import tempfile

    monkeypatch.setattr(computer_use, "_capture_export_dir", None)
    cap, _, _ = capture()
    first = Path(computer_use._persist_capture_image(cap))
    export = first.parent
    moved = export.with_name(export.name + "-moved")
    real_mkstemp = tempfile.mkstemp
    real_open = os.open
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            export.rename(moved)
            export.symlink_to(mercury_home, target_is_directory=True)
            swapped = True

    def swap_before_mkstemp(*args, **kwargs):
        swap()
        return real_mkstemp(*args, **kwargs)

    def swap_before_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and kwargs.get("dir_fd") is not None:
            swap()
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as race:
        race.setattr(tempfile, "mkstemp", swap_before_mkstemp)
        race.setattr(os, "open", swap_before_open)
        # The wrapper retains the real open() dir_fd capability.
        race.setattr(os, "supports_dir_fd", os.supports_dir_fd | {swap_before_open})
        result = computer_use._persist_capture_image(cap)
    assert swapped
    assert not list(mercury_home.glob("computer_use_*.*"))
    assert result is None or not Path(result).resolve().is_relative_to(mercury_home)
    recovered = computer_use._persist_capture_image(cap)
    assert recovered is not None
    assert not Path(recovered).resolve().is_relative_to(mercury_home)
    stage_mlounge_upload(mercury_home, recovered)


def test_ordinary_capture_without_dir_fd_support_is_stageable(mercury_home, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(computer_use, "_capture_export_dir", None)
    cap, raw, _ = capture("JPEG")
    screenshot = computer_use._persist_capture_image(cap)
    assert screenshot is not None
    assert Path(screenshot).suffix == ".jpg"
    assert Path(screenshot).read_bytes() == raw
    assert staged_bytes(mercury_home, stage_mlounge_upload(mercury_home, screenshot)) == raw
