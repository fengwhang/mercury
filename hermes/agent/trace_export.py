"""Prepare local session traces in Claude Code JSONL format.

This deterministic converter makes no model calls or network requests.
Secret redaction is forced by default, regardless of log-redaction settings.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HERMES_VERSION = "mercury-agent"
_REDACTION_BLOCKED_MESSAGE = (
    "Trace export blocked: secret redaction failed, so the transcript may "
    "still contain credentials or other sensitive data. Fix the redactor or "
    "rerun with --no-redact only after manually reviewing the transcript."
)


class TraceRedactionError(RuntimeError):
    """Raised when a trace cannot be safely redacted before export."""


# ---------------------------------------------------------------------------
# Conversion: Mercury OpenAI-format messages -> Claude Code JSONL
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _redact(text: Any, enabled: bool) -> Any:
    """Redact secrets from a string body when redaction is enabled.

    Non-strings pass through untouched. Uses Mercury' shared redactor with
    ``force=True`` so an export always scrubs known secret shapes even if
    the user disabled log redaction globally.
    """
    if not enabled or not isinstance(text, str) or not text:
        return text
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception as exc:
        logger.warning("Trace export redaction failed; refusing export", exc_info=True)
        raise TraceRedactionError(_REDACTION_BLOCKED_MESSAGE) from exc


def _content_to_blocks(content: Any, redact: bool) -> List[Dict[str, Any]]:
    """Normalize a message ``content`` field into Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": _redact(content, redact)}]
    if isinstance(content, list):
        blocks: List[Dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text":
                    blocks.append({"type": "text", "text": _redact(part.get("text", ""), redact)})
                elif ptype in ("image_url", "image"):
                    # Keep a placeholder; the viewer renders text turns and we
                    # don't want to inline base64 blobs into a trace.
                    blocks.append({"type": "text", "text": "[image omitted]"})
                else:
                    blocks.append({"type": "text", "text": _redact(json.dumps(part), redact)})
            else:
                blocks.append({"type": "text", "text": _redact(str(part), redact)})
        return blocks
    return [{"type": "text", "text": _redact(json.dumps(content), redact)}]


def _tool_calls_to_blocks(tool_calls: Any, redact: bool) -> List[Dict[str, Any]]:
    """Convert OpenAI tool_calls into Anthropic ``tool_use`` content blocks."""
    blocks: List[Dict[str, Any]] = []
    if not isinstance(tool_calls, list):
        return blocks
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name") or "tool"
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args) if raw_args.strip() else {}
            except (json.JSONDecodeError, ValueError):
                parsed = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            parsed = raw_args
        else:
            parsed = {}
        if redact:
            try:
                parsed = json.loads(_redact(json.dumps(parsed), redact))
            except (json.JSONDecodeError, ValueError):
                logger.warning("Trace export redacted tool arguments are not valid JSON; refusing export")
                raise TraceRedactionError(_REDACTION_BLOCKED_MESSAGE)
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
            "name": name,
            "input": parsed,
        })
    return blocks


def build_trace_jsonl(
    messages: List[Dict[str, Any]],
    *,
    session_id: str,
    model: str = "",
    cwd: str = "",
    redact: bool = True,
) -> str:
    """Render Mercury conversation messages as Claude Code JSONL text.

    Each non-system message becomes one JSONL line in the Claude Code
    transcript shape:

    * ``user`` / ``tool`` -> ``{"type": "user", "message": {...}}``
    * ``assistant``       -> ``{"type": "assistant", "message": {...}}``
      with ``content`` blocks (text + ``tool_use``).

    Tool results are emitted as user turns carrying a ``tool_result``
    block keyed by ``tool_call_id`` — the same way Claude Code records
    them. Turns are linked via ``uuid`` / ``parentUuid``.
    """
    lines: List[str] = []
    parent: Optional[str] = None
    base_ts = _now_iso()
    git_branch = ""
    try:
        import subprocess
        if cwd:
            r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3, cwd=cwd,
            )
            if r.returncode == 0:
                git_branch = r.stdout.strip()
    except Exception:
        git_branch = ""

    def _common(turn_uuid: str) -> Dict[str, Any]:
        return {
            "parentUuid": parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": cwd or os.getcwd(),
            "sessionId": session_id,
            "version": _HERMES_VERSION,
            "gitBranch": git_branch,
            "uuid": turn_uuid,
            "timestamp": base_ts,
        }

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        turn_uuid = str(uuid.uuid4())

        if role == "assistant":
            blocks = _content_to_blocks(msg.get("content"), redact)
            blocks.extend(_tool_calls_to_blocks(msg.get("tool_calls"), redact))
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            entry = _common(turn_uuid)
            entry["type"] = "assistant"
            entry["message"] = {
                "role": "assistant",
                "model": model or "unknown",
                "content": blocks,
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
            parent = turn_uuid
            continue

        if role == "tool":
            tool_use_id = msg.get("tool_call_id") or msg.get("tool_name") or "tool"
            result_content = _redact(
                msg.get("content") if isinstance(msg.get("content"), str)
                else json.dumps(msg.get("content")),
                redact,
            )
            entry = _common(turn_uuid)
            entry["type"] = "user"
            entry["message"] = {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_content,
                }],
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
            parent = turn_uuid
            continue

        # Default: user (and any unknown role) -> user turn.
        content = msg.get("content")
        if isinstance(content, str):
            message_content: Any = _redact(content, redact)
        else:
            message_content = _content_to_blocks(content, redact)
        entry = _common(turn_uuid)
        entry["type"] = "user"
        entry["message"] = {"role": "user", "content": message_content}
        lines.append(json.dumps(entry, ensure_ascii=False))
        parent = turn_uuid

    return "\n".join(lines) + ("\n" if lines else "")
