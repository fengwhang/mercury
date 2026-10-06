"""Tests for local Claude Code JSONL trace conversion and redaction."""

import json

import pytest

from agent.trace_export import TraceRedactionError, build_trace_jsonl


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

def _sample_messages():
    return [
        {"role": "system", "content": "you are mercury"},
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "Listing.", "tool_calls": [
            {"id": "call_1", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "terminal", "content": "a.txt\nb.txt"},
        {"role": "assistant", "content": "Two files."},
    ]

def test_converter_emits_tool_use_and_tool_result():
    jsonl = build_trace_jsonl(_sample_messages(), session_id="s1", model="m")
    lines = [json.loads(x) for x in jsonl.strip().split("\n")]
    # line 0 user, line 1 assistant (text + tool_use), line 2 tool_result, line 3 assistant
    assert lines[0]["type"] == "user"
    assert lines[1]["type"] == "assistant"
    blocks = lines[1]["message"]["content"]
    assert any(b.get("type") == "text" for b in blocks)
    tool_use = [b for b in blocks if b.get("type") == "tool_use"]
    assert tool_use and tool_use[0]["name"] == "terminal"
    assert tool_use[0]["input"] == {"command": "ls"}
    # tool result rides on a user turn
    assert lines[2]["type"] == "user"
    tr = lines[2]["message"]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "call_1"
    assert "a.txt" in tr["content"]


def test_converter_redacts_secrets_by_default():
    msgs = [{"role": "user", "content": "key OPENAI_API_KEY=sk-abc123def456ghi789jklmno end"}]
    jsonl = build_trace_jsonl(msgs, session_id="s1", redact=True)
    assert "sk-abc123def456ghi789jklmno" not in jsonl


def test_converter_refuses_unredacted_passthrough_when_redactor_fails(monkeypatch):
    def boom(_text, *, force=False):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", boom)
    msgs = [{"role": "user", "content": "OPENAI_API_KEY=sk-abc123def456ghi789jklmno"}]

    with pytest.raises(TraceRedactionError):
        build_trace_jsonl(msgs, session_id="s1", redact=True)


def test_converter_keeps_secrets_when_redact_disabled():
    secret = "sk-abc123def456ghi789jklmno"
    msgs = [{"role": "user", "content": f"key OPENAI_API_KEY={secret} end"}]
    jsonl = build_trace_jsonl(msgs, session_id="s1", redact=False)
    assert secret in jsonl


