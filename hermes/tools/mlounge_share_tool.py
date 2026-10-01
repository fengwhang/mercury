"""mLounge Share Tool — the paperclip button for agents.

Stages a local file as a mLounge upload and posts the link in the
agent's CURRENT MIRC room only (resolved from session context, never
a parameter — the link must not be routable elsewhere). Mirrors what a
human gets clicking the attachment button: same storage scheme, same
link shape, same channel-scoped visibility.
"""

import logging

from tools.registry import registry, tool_result

logger = logging.getLogger(__name__)

# Platform toolset (NOT a feature name): plugin-platform agents only
# receive tools tagged with the bare platform ("irc"). Anything else
# is silently invisible — verified against toolsets.resolve_toolset.
_TOOLSET = "irc"


async def _handle_mlounge_share(args, **kw):
    from gateway.session_context import get_session_env

    path = str((args or {}).get("path") or "").strip()
    caption = str((args or {}).get("caption") or "").strip()
    if not path:
        return tool_result({"success": False,
                            "error": "path is required"})
    channel = get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    if not channel.startswith("#"):
        return tool_result({"success": False,
                            "error": "mlounge_share works in MIRC rooms only — "
                                     "this turn has no channel context"})
    try:
        from observatory import mlounge as mlounge_mod

        staged = mlounge_mod.stage_mlounge_upload(None, path)
        url = mlounge_mod.mlounge_base_url(None) + "/" + staged["url_path"]
        if not mlounge_mod.check_upload_serves(url):
            return tool_result({"success": False,
                                "error": "staged but the link does not "
                                         "serve (mLounge unreachable?) — "
                                         "not posted"})
    except Exception as exc:
        logger.debug("mlounge_share: stage failed", exc_info=True)
        return tool_result({"success": False, "error": str(exc)[:300]})
    line = f"{caption}\n{url}".strip() if caption else url
    try:
        from observatory.rooms import say_nowait

        posted = say_nowait(channel, line)
    except Exception:
        posted = False
    if not posted:
        return tool_result({"success": False,
                            "error": "staged but the room is unreachable",
                            "url": url})
    return tool_result({"success": True, "url": url, "channel": channel,
                        "filename": staged["filename"]})


registry.register(
    name="mlounge_share",
    toolset=_TOOLSET,
    schema={
        "name": "mlounge_share",
        "description": (
            "Share a local file in THIS MIRC room (the paperclip button). "
            "Posts the mLounge link here; use for handoffs the user asked for."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute local path of the file to share.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional one-line caption posted with the link.",
                },
            },
            "required": ["path"],
        },
    },
    handler=_handle_mlounge_share,
    is_async=True,
    emoji="📎",
)

# Compatibility aliases for existing extensions; implementation uses the fork names.
_handle_lounge_share = _handle_mlounge_share
