"""Thinking faces for IRC rooms: a random kaomoji while hermes thinks.

The CLI shows KawaiiSpinner faces during reasoning; IRC has no typing
indicator, so gateway-dispatch turns post one face message when a turn
runs long. Delayed (THINKING_FACE_DELAY_S) so instant answers stay
silent; at most one outstanding face per room; cleared the moment the
room's reply sends (adapter.send calls thinking_done). hermes-side
only — omp rooms stream their own traces, child rooms stream theirs.
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

_tasks: dict[str, asyncio.Task] = {}


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
        await bot.send(room, face)
    except Exception as exc:
        logger.debug("thinking face for %s failed: %s", room, exc)
    finally:
        if me is not None and _tasks.get(room) is me:
            _tasks.pop(room, None)
