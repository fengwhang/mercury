"""Thinking faces: KAWAII store reuse, delayed post, cancel on reply."""

from __future__ import annotations

import asyncio

import pytest

from observatory import thinking as thinking_mod


def test_faces_come_from_cli_store() -> None:
    from agent.display import KawaiiSpinner

    assert thinking_mod.thinking_faces() == KawaiiSpinner.get_thinking_faces()
    assert len(thinking_mod.thinking_faces()) >= 10


def test_done_on_unknown_room_never_raises() -> None:
    thinking_mod.thinking_done("#nope")
    thinking_mod.thinking_done("")


@pytest.mark.asyncio
async def test_face_posts_after_delay(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []

    class FakeBot:
        async def send(self, room: str, text: str):
            sent.append((room, text))
            return None

    monkeypatch.setattr(thinking_mod, "THINKING_FACE_DELAY_S", 0.01)
    monkeypatch.setattr(
        "observatory.rooms.get_bot_sink", lambda: FakeBot())
    thinking_mod.thinking_started("#room")
    await asyncio.sleep(0.05)
    assert len(sent) == 1
    room, face = sent[0]
    assert room == "#room"
    assert face in thinking_mod.thinking_faces()


@pytest.mark.asyncio
async def test_reply_cancels_pending_face(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []

    class FakeBot:
        async def send(self, room: str, text: str):
            sent.append((room, text))
            return None

    monkeypatch.setattr(thinking_mod, "THINKING_FACE_DELAY_S", 30.0)
    monkeypatch.setattr(
        "observatory.rooms.get_bot_sink", lambda: FakeBot())
    thinking_mod.thinking_started("#room")
    thinking_mod.thinking_done("#room")
    await asyncio.sleep(0.05)
    assert sent == []


@pytest.mark.asyncio
async def test_overlapping_turns_post_once(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []

    class FakeBot:
        async def send(self, room: str, text: str):
            sent.append((room, text))
            return None

    monkeypatch.setattr(thinking_mod, "THINKING_FACE_DELAY_S", 0.01)
    monkeypatch.setattr(
        "observatory.rooms.get_bot_sink", lambda: FakeBot())
    thinking_mod.thinking_started("#room")
    thinking_mod.thinking_started("#room")
    await asyncio.sleep(0.05)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_face_posting_does_not_cancel_itself(monkeypatch) -> None:
    """The face sends through the same send() that clears faces: posting
    must not cancel its own task mid-send (regression)."""
    sent: list[tuple[str, str]] = []

    class FakeBot:
        async def send(self, room: str, text: str):
            thinking_mod.thinking_done(room)
            sent.append((room, text))
            return None

    monkeypatch.setattr(thinking_mod, "THINKING_FACE_DELAY_S", 0.01)
    monkeypatch.setattr(
        "observatory.rooms.get_bot_sink", lambda: FakeBot())
    thinking_mod.thinking_started("#room")
    await asyncio.sleep(0.05)
    assert len(sent) == 1
    assert sent[0][1] in thinking_mod.thinking_faces()


def _irc_adapter():
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import PlatformConfig
    from plugins.platforms.irc.adapter import IRCAdapter

    cfg = PlatformConfig(
        enabled=True,
        extra={"server": "localhost", "port": 6667,
               "nickname": "testbot", "channel": "#test",
               "use_tls": False},
    )
    adapter = IRCAdapter(cfg)
    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    adapter._writer = writer
    return adapter


@pytest.mark.asyncio
async def test_dispatch_starts_thinking_face(monkeypatch) -> None:
    """Inbound gateway dispatch schedules a face for the room."""
    adapter = _irc_adapter()
    started: list[str] = []
    monkeypatch.setattr(
        thinking_mod, "thinking_started", lambda room: started.append(room))

    async def fake_handle(event):
        return None

    from unittest.mock import AsyncMock
    adapter._message_handler = AsyncMock()
    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    await adapter._dispatch_message(
        text="hello", chat_id="#test", chat_type="group",
        user_id="u", user_name="nick")
    assert started == ["#test"]


@pytest.mark.asyncio
async def test_send_clears_thinking_face(monkeypatch) -> None:
    """The room's reply send cancels its pending face."""
    from observatory import identity as identity_mod

    adapter = _irc_adapter()

    class FakePool:
        def get(self, channel):
            return None

    monkeypatch.setattr(identity_mod, "get_pool", lambda: FakePool())
    adapter._server_multiline = False
    thinking_mod.thinking_started("#test")
    assert "#test" in thinking_mod._tasks
    result = await adapter.send("#test", "hi")
    assert result.success is True
    assert "#test" not in thinking_mod._tasks


def test_interim_notice_shapes() -> None:
    assert thinking_mod.is_interim_notice("🌀 mnemosyne — recalled 4 memories")
    assert thinking_mod.is_interim_notice("👁️ Hindsight — recalled 2 memories")
    assert thinking_mod.is_interim_notice("👁️ Hindsight — saving to memory…")
    assert thinking_mod.is_interim_notice("🧠 Notes — recalled 2 memories")
    assert not thinking_mod.is_interim_notice("hello world")
    assert not thinking_mod.is_interim_notice("🔧 read {\"path\": \"/a\"}")
    assert not thinking_mod.is_interim_notice("")


@pytest.mark.asyncio
async def test_notice_rearms_pending_face(monkeypatch) -> None:
    """An interim memory notice restarts the face delay instead of
    killing it: spawned rooms with recall active still get faces."""
    sent: list[tuple[str, str]] = []

    class FakeBot:
        async def send(self, room: str, text: str):
            sent.append((room, text))

    monkeypatch.setattr(thinking_mod, "THINKING_FACE_DELAY_S", 0.05)
    monkeypatch.setattr(
        "observatory.rooms.get_bot_sink", lambda: FakeBot())
    thinking_mod.thinking_started("#room")
    await asyncio.sleep(0.02)
    thinking_mod.thinking_progress("#room")
    await asyncio.sleep(0.03)
    assert sent == []
    await asyncio.sleep(0.05)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_reply_after_notice_still_cancels(monkeypatch) -> None:
    """A real reply following a notice cancels the re-armed face."""
    sent: list[tuple[str, str]] = []

    class FakeBot:
        async def send(self, room: str, text: str):
            sent.append((room, text))

    monkeypatch.setattr(thinking_mod, "THINKING_FACE_DELAY_S", 0.05)
    monkeypatch.setattr(
        "observatory.rooms.get_bot_sink", lambda: FakeBot())
    thinking_mod.thinking_started("#room")
    await asyncio.sleep(0.02)
    thinking_mod.thinking_progress("#room")
    thinking_mod.thinking_done("#room")
    await asyncio.sleep(0.08)
    assert sent == []


@pytest.mark.asyncio
async def test_notice_send_rearms_but_reply_send_cancels(monkeypatch) -> None:
    """Adapter.send routes memory notices to re-arm, other sends to cancel."""
    from observatory import identity as identity_mod

    adapter = _irc_adapter()

    class FakePool:
        def get(self, channel):
            return None

    monkeypatch.setattr(identity_mod, "get_pool", lambda: FakePool())
    adapter._server_multiline = False
    thinking_mod.thinking_started("#test")
    await adapter.send("#test", "🌀 mnemosyne — recalled 4 memories")
    assert "#test" in thinking_mod._tasks
    await adapter.send("#test", "the actual reply")
    assert "#test" not in thinking_mod._tasks
