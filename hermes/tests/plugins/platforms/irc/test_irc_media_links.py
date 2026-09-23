"""IRC media delivery as Lounge links (paperclip parity for send paths)."""

from __future__ import annotations

import pytest


def _adapter(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.irc import adapter as adapter_mod

    for key in ("IRC_SERVER", "IRC_PORT", "IRC_NICKNAME", "IRC_CHANNEL",
                "IRC_USE_TLS"):
        monkeypatch.delenv(key, raising=False)
    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "127.0.0.1", "port": 6669,
               "nickname": "bot", "channel": "#bot"},
    )
    ad = adapter_mod.IRCAdapter(cfg)

    class _Writer:
        def is_closing(self):
            return False

    ad._writer = _Writer()  # connected enough to attempt sends
    return ad, adapter_mod


def _stage_mocks(monkeypatch, ok=True):
    import observatory.lounge as lounge_mod

    monkeypatch.setattr(
        lounge_mod, "stage_lounge_upload",
        lambda home, path: {"url_path": "uploads/ab/cdef/f.bin",
                            "filename": "f.bin"})
    monkeypatch.setattr(
        lounge_mod, "lounge_base_url", lambda home=None: "http://h:9000")
    if not ok:
        def _boom(*a, **k):
            raise lounge_mod.LoungeError("denied")

        monkeypatch.setattr(lounge_mod, "stage_lounge_upload", _boom)
    monkeypatch.setattr(lounge_mod, "check_upload_serves", lambda *a: True)


@pytest.mark.asyncio
async def test_send_document_posts_link(tmp_path, monkeypatch) -> None:
    ad, _ = _adapter(monkeypatch)
    _stage_mocks(monkeypatch)
    raw = []
    monkeypatch.setattr(ad, "_send_raw", _capture(raw))
    f = tmp_path / "r.pdf"
    f.write_bytes(b"x")
    res = await ad.send_document("#vm_ace", str(f), caption="report")
    assert res.success is True
    assert any("http://h:9000/uploads/ab/cdef/f.bin" in ln for ln in raw)
    assert any("report" in ln for ln in raw)


@pytest.mark.asyncio
async def test_send_document_stage_failure(tmp_path, monkeypatch) -> None:
    ad, _ = _adapter(monkeypatch)
    _stage_mocks(monkeypatch, ok=False)
    f = tmp_path / "r.pdf"
    f.write_bytes(b"x")
    res = await ad.send_document("#vm_ace", str(f))
    assert res.success is False


@pytest.mark.asyncio
async def test_send_expands_media_tags(tmp_path, monkeypatch) -> None:
    ad, _ = _adapter(monkeypatch)
    _stage_mocks(monkeypatch)
    raw = []
    monkeypatch.setattr(ad, "_send_raw", _capture(raw))
    f = tmp_path / "r.pdf"
    f.write_bytes(b"x")
    await ad.send("#vm_ace", f"see MEDIA:{f} thanks")
    assert any("http://h:9000/uploads/ab/cdef/f.bin" in ln for ln in raw)
    assert not any("MEDIA:" in ln for ln in raw)


@pytest.mark.asyncio
async def test_send_multiple_images_mixed(monkeypatch) -> None:
    ad, _ = _adapter(monkeypatch)
    _stage_mocks(monkeypatch)
    raw = []
    monkeypatch.setattr(ad, "_send_raw", _capture(raw))
    await ad.send_multiple_images(
        "#vm_ace",
        [("http://x.test/a.png", ""), ("/tmp/local.png", "")])
    joined = "\n".join(raw)
    assert "http://x.test/a.png" in joined
    assert "http://h:9000/uploads/ab/cdef/f.bin" in joined


def _capture(raw):
    async def _send(line):
        raw.append(line)

    return _send
