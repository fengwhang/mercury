"""Session trace exports stay local; retired hosted flags fail at parsing."""

import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SECRET = "sk-proj-Zz12345678901234567890123456789012345678"


@pytest.fixture
def local_session(monkeypatch, tmp_path):
    import mercury_state

    db_type = mercury_state.SessionDB
    db_path = tmp_path / "state.db"
    with db_type(db_path=db_path) as db:
        db.create_session("trace-session", "cli", model="local-test-model")
        db.append_message("trace-session", "user", f"hello trace OPENAI_API_KEY={SECRET}")
        db.append_message("trace-session", "assistant", "local answer")
    monkeypatch.setattr(mercury_state, "SessionDB", lambda: db_type(db_path=db_path))
    return db_path


@pytest.fixture
def no_network(monkeypatch):
    # Trap each former HF operation without requiring the optional HF package.
    # Also fail closed if a route bypasses that client and uses real sockets.
    api = MagicMock()
    api.whoami.return_value = {"name": "test-owner"}
    hf_api = MagicMock(return_value=api)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=hf_api))
    monkeypatch.setenv("HF_TOKEN", "hf_test_not_a_credential")
    ensure = MagicMock(return_value=False)
    monkeypatch.setattr("tools.lazy_deps.ensure", ensure)
    connect = MagicMock(side_effect=AssertionError("trace exports must not connect"))
    monkeypatch.setattr(socket.socket, "connect", connect)
    yield
    hf_api.assert_not_called()
    api.whoami.assert_not_called()
    api.create_repo.assert_not_called()
    api.upload_file.assert_not_called()
    connect.assert_not_called()
    ensure.assert_not_called()


@pytest.mark.parametrize("flags", [("--upload",), ("--upload", "--public"), ("--public",)])
def test_hosted_flags_rejected_before_request(monkeypatch, local_session, no_network, capsys, flags):
    import mercury_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", [
        "mercury", "sessions", "export", "--format", "trace",
        "--session-id", "trace-session", *flags,
    ])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2
    assert "unrecognized arguments:" in capsys.readouterr().err


@pytest.mark.parametrize("output", ["-", "trace.jsonl"])
def test_local_trace_export_renders_stored_conversation(
    monkeypatch, local_session, no_network, tmp_path, capsys, output,
):
    import mercury_cli.main as main_mod
    import mercury_state

    destination = "-" if output == "-" else str(tmp_path / output)
    monkeypatch.setattr(sys, "argv", [
        "mercury", "sessions", "export", "--format", "trace",
        "--session-id", "trace-s", destination,
    ])
    main_mod.main()
    stdout = capsys.readouterr().out
    text = stdout if output == "-" else (tmp_path / output).read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["type"] for record in records] == ["user", "assistant"]
    assert all(record["sessionId"] == "trace-session" for record in records)
    assert records[0]["message"]["content"].startswith("hello trace")
    assert records[1]["message"]["content"][0]["text"] == "local answer"
    assert records[1]["message"]["model"] == "local-test-model"
    assert records[1]["parentUuid"] == records[0]["uuid"]
    assert SECRET not in text
    with mercury_state.SessionDB() as db:
        assert SECRET in db.get_messages_as_conversation("trace-session")[0]["content"]


def test_local_trace_export_no_redact_keeps_reviewed_content(
    monkeypatch, local_session, no_network, capsys,
):
    import mercury_cli.main as main_mod

    monkeypatch.setattr(sys, "argv", [
        "mercury", "sessions", "export", "--format", "trace", "--no-redact",
        "--session-id", "trace-session",
    ])
    main_mod.main()
    assert SECRET in capsys.readouterr().out


def test_bulk_trace_export_writes_local_files(
    monkeypatch, local_session, no_network, tmp_path, capsys,
):
    import mercury_cli.main as main_mod
    import mercury_state

    with mercury_state.SessionDB() as db:
        db.end_session("trace-session", "done")
        db.create_session("second-trace", "cli")
        db.append_message("second-trace", "user", "second conversation")
        db.end_session("second-trace", "done")

    destination = tmp_path / "traces"
    monkeypatch.setattr(sys, "argv", [
        "mercury", "sessions", "export", "--format", "trace",
        "--source", "cli", str(destination),
    ])
    main_mod.main()
    records = [
        json.loads(line)
        for line in (destination / "trace-session.trace.jsonl").read_text().splitlines()
    ]
    assert [record["type"] for record in records] == ["user", "assistant"]
    assert (destination / "second-trace.trace.jsonl").is_file()
    assert "Exported 2 session trace(s)" in capsys.readouterr().out


def test_trace_redaction_failure_does_not_write_output(
    monkeypatch, local_session, no_network, tmp_path, capsys,
):
    import mercury_cli.main as main_mod

    from agent.redact import redact_sensitive_text

    def fail_redaction(text, *, force=False):
        if force:
            raise RuntimeError("redactor unavailable")
        return redact_sensitive_text(text, force=force)
    monkeypatch.setattr("agent.redact.redact_sensitive_text", fail_redaction)
    destination = tmp_path / "trace.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "mercury", "sessions", "export", "--format", "trace",
        "--session-id", "trace-session", str(destination),
    ])
    main_mod.main()
    assert not destination.exists()
    assert "refusing to export unredacted trace content" in capsys.readouterr().out


def test_local_jsonl_export_preserves_session(
    monkeypatch, local_session, no_network, tmp_path, capsys,
):
    import mercury_cli.main as main_mod
    import mercury_state

    destination = tmp_path / "session.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "mercury", "sessions", "export", "--session-id", "trace-session",
        str(destination),
    ])
    main_mod.main()
    session = json.loads(destination.read_text())
    assert session["id"] == "trace-session"
    assert [msg["role"] for msg in session["messages"]] == ["user", "assistant"]
    assert "Exported 1 session" in capsys.readouterr().out
    with mercury_state.SessionDB() as db:
        assert db.get_session("trace-session")["message_count"] == 2
