"""User-facing progress shared by the headless and gateway chat paths."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping


def todo_text(snapshot: object) -> str | None:
    """Render the authoritative list returned by either engine's todo tool."""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return None
    if not isinstance(snapshot, Mapping):
        return None
    lines = ["To do list:"]
    if isinstance(snapshot.get("phases"), list):
        for phase in snapshot["phases"]:
            if not isinstance(phase, Mapping):
                continue
            lines.append(str(phase.get("name") or "Tasks"))
            for item in phase.get("tasks", []):
                if isinstance(item, Mapping):
                    lines.append(_todo_line(item))
    elif isinstance(snapshot.get("todos"), list):
        lines.extend(_todo_line(item) for item in snapshot["todos"] if isinstance(item, Mapping))
    else:
        return None
    return "\n".join(lines if len(lines) > 1 else [lines[0], "• No tasks"])


def _todo_line(item: Mapping) -> str:
    text = f"• [{item.get('status') or 'pending'}] {item.get('content') or ''}"
    if item.get("blocker"):
        text += f" — {item['blocker']}"
    return text


def hermes_progress_frame(event_type: str, name: str | None, *, args=None,
                          result=None, duration=0, is_error=False, **kwargs) -> dict | None:
    """Project TUI completion labels and plans without exposing reasoning."""
    if event_type == "subagent.complete":
        label = kwargs.get("goal") or name or "subagent"
        status = kwargs.get("status") or "completed"
        text = f"Delegate task {status}: {label}"
        from tools.delegate_tool import SUBAGENT_FAILURE_STATUSES, format_subagent_failure_line

        if status in SUBAGENT_FAILURE_STATUSES:
            text = format_subagent_failure_line(label, status, error=kwargs.get("summary"),
                                               duration_seconds=kwargs.get("duration_seconds"))
    elif event_type == "tool.completed" and name and not name.startswith("_"):
        if name == "todo" and not is_error and (plan := todo_text(result)):
            text = plan
        else:
            from agent.display import get_cute_tool_message

            text = get_cute_tool_message(name, args or {}, duration or 0, result=result)
            # TUI colours are presentation, not chat content.
            text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
            if is_error:
                text = f"Tool failed: {name}\n{text}"
    else:
        return None
    return {"feed": "status", "text": text, "subagent_id": ""}
