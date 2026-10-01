"""Thinking faces for MIRC rooms: a random kaomoji while either engine works.

The CLI shows KawaiiSpinner faces during reasoning; MIRC has no typing
indicator, so gateway-dispatch turns post one face message when a turn
runs long. Delayed (THINKING_FACE_DELAY_S) so instant answers stay
silent; at most one outstanding face per room; cleared the moment the
room's reply sends (adapter.send calls thinking_done). Hermes and OMP
rooms — gateway, spawned, and delegated — use the same store and delay.
Uses the CLI's face store live (skin override, else KAWAII_THINKING) —
never a forked list. Never raises; never blocks dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

#: Seconds of thinking before a face posts (instant answers stay silent).
THINKING_FACE_DELAY_S = 3.0

#: Max face-timer re-arms per turn. An interim notice (memory recall line)
#: proves the turn is alive, so it restarts — not kills — the pending
#: face's delay. Capped so a notice every 2s forever still yields a face
#: (the turn IS long) instead of starving it.
THINKING_FACE_MAX_ARMS = 3

#: Interim-notice glyphs: provider recall/retain lines are progress, never
#: the turn-final reply (memory_manager.describe_recall shapes).
_NOTICE_GLYPHS = ("🌀", "👁️", "🧠")

_tasks: dict[str, asyncio.Task] = {}
_arms: dict[str, int] = {}


def is_interim_notice(text: str) -> bool:
    """True for provider memory progress lines (recall/retain notices)."""
    try:
        body = str(text or "")
    except Exception:
        return False
    if not body.startswith(_NOTICE_GLYPHS):
        return False
    lowered = body.lower()
    return "recall" in lowered or "saving to memory" in lowered

def thinking_faces() -> list[str]:
    """The CLI's reasoning faces (skin override, else KAWAII_THINKING)."""
    try:
        from agent.display import KawaiiSpinner

        faces = KawaiiSpinner.get_thinking_faces()
        if faces:
            return list(faces)
    except Exception:
        pass
    return ["(◔_◔)"]


def thinking_started(room: str) -> None:
    """Schedule a face for *room* if none is outstanding."""
    room = str(room or "")
    if not room:
        return
    pending = _tasks.get(room)
    if pending is not None and not pending.done():
        return
    _arms[room] = 0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        _tasks[room] = loop.create_task(
            _delayed_face(room), name=f"thinking-face-{room}")
    except Exception:
        pass


def thinking_done(room: str) -> None:
    """Cancel *room*'s pending face (a reply just sent). Never raises."""
    try:
        task = _tasks.pop(str(room or ""), None)
        _arms.pop(str(room or ""), None)
    except Exception:
        return
    if task is None or task.done():
        return
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    # A face posts through the same send path that clears it; never
    # cancel the task doing the posting (the pop above already disarmed
    # its finally-guard).
    if current is not None and task is current:
        return
    try:
        task.cancel()
    except Exception:
        pass


def thinking_progress(room: str) -> None:
    """Interim progress in *room* (memory notice): restart the pending
    face's delay instead of killing it. The notice proves the turn is
    alive — without this, every spawned-room turn with recall active
    cancels its face ~1s in and spawned rooms never show faces while
    the gateway room (no early notice) does. Capped at
    THINKING_FACE_MAX_ARMS re-arms per turn; never raises."""
    room = str(room or "")
    if not room:
        return
    try:
        pending = _tasks.get(room)
        if pending is None or pending.done():
            return
        if _arms.get(room, 0) >= THINKING_FACE_MAX_ARMS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            pending.cancel()
        except Exception:
            pass
        _arms[room] = _arms.get(room, 0) + 1
        try:
            _tasks[room] = loop.create_task(
                _delayed_face(room), name=f"thinking-face-{room}")
        except Exception:
            pass
    except Exception:
        pass


async def _delayed_face(room: str) -> None:
    me = asyncio.current_task()
    try:
        await asyncio.sleep(THINKING_FACE_DELAY_S)
        faces = thinking_faces()
        face = random.choice(faces)
        from observatory.rooms import get_bot_sink

        bot = get_bot_sink()
        if bot is None:
            return
        await bot.send(room, face, metadata={"mercury_kind": "thinking"})
    except Exception as exc:
        logger.debug("thinking face for %s failed: %s", room, exc)
    finally:
        if me is not None and _tasks.get(room) is me:
            _tasks.pop(room, None)
            _arms.pop(room, None)
