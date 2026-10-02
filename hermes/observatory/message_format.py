"""Rendering provenance for the mLounge fork, carried separately from text."""

KIND_TAG = "+mercury/kind"
MESSAGE_KINDS = frozenset({
    "assistant_reply", "user", "tool_input", "tool_output", "thinking", "status",
})


def message_tags(kind: str = "", *, batch: str = "", concat: bool = False, empty: bool = False) -> str:
    """Only fixed rendering kinds reach the wire; never interpolate arbitrary tags."""
    tags = []
    if batch:
        tags.append(f"batch={batch}")
    if kind in MESSAGE_KINDS:
        tags.append(f"{KIND_TAG}={kind}")
    if batch:
        if concat:
            tags.append("draft/multiline-concat")
        if empty:
            tags.append("+mercury/empty=1")
    return "@" + ";".join(tags) + " " if tags else ""


def frame_kind(feed: dict) -> str:
    kind = feed.get("feed")
    if kind == "message":
        role = feed.get("role")
        if role in {"tool", "function"}:
            return "tool_output"
        return "user" if role == "user" else "assistant_reply"
    return {"tool": "tool_input", "thought": "thinking"}.get(kind, "status")
