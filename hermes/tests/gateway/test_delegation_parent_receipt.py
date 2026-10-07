"""Durable parent receipt deduplicates replay after acceptance/ack split."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner
from tools.process_registry import format_process_notification


@pytest.mark.asyncio
async def test_persisted_parent_receipt_skips_adapter_on_restart(tmp_path, monkeypatch):
    from mercury_state import SessionDB
    from tests.gateway.test_completion_delivery import _runner
    from tools import async_delegation as ad
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="fixture", model="fixture")
    evt = {"type": "async_delegation", "delegation_id": "receipt-fixture",
           "parent_session_id": "parent", "session_key": "agent:main:telegram:dm:123",
           "goal": "fixture", "status": "completed", "summary": "real result"}
    text = format_process_notification(evt)
    # The old gateway persisted the input, then died before the producer ack.
    db.append_message("parent", "user", text, display_kind="internal_notification")
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._session_db = SimpleNamespace(get_compression_tip=AsyncMock(return_value="parent"))
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        load_transcript=AsyncMock(side_effect=lambda sid: db.get_messages(sid)))
    try:
        assert await runner._inject_watch_notification(text, evt) is True
        adapter.handle_message.assert_not_awaited()
        assert len(db.get_messages("parent")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_existing_parent_receipt_acknowledges_unverifiable_legacy_claim(tmp_path, monkeypatch):
    import time
    from mercury_state import SessionDB
    from tests.gateway.test_completion_delivery import _runner
    from tools import async_delegation as ad
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="fixture", model="fixture")
    evt = {"type": "async_delegation", "delegation_id": "receipt-fixture",
           "parent_session_id": "parent", "session_key": "agent:main:telegram:dm:123",
           "goal": "fixture", "status": "completed", "summary": "real result"}
    ad._persist_dispatch({**evt, "dispatched_at": time.time()})
    ad._persist_completion(evt, {"status": "completed", "summary": "real result"})
    assert ad.claim_completion_delivery("receipt-fixture", "legacy-claim")
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET delivery_owner_pid=NULL, delivery_owner_started_at=NULL WHERE delegation_id='receipt-fixture'")
    text = format_process_notification(evt)
    db.append_message("parent", "user", text, display_kind="internal_notification")
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    runner._session_db = SimpleNamespace(get_compression_tip=AsyncMock(return_value="parent"))
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        load_transcript=AsyncMock(side_effect=lambda sid: db.get_messages(sid)))
    try:
        await runner._deliver_completion_notification(text, evt)
        assert ad.get_durable_delegation("receipt-fixture")["delivery_state"] == "delivered"
        with ad._transaction() as conn:
            assert conn.execute(
                "SELECT delivery_claim FROM async_delegations WHERE delegation_id='receipt-fixture'"
            ).fetchone()[0] is None
        adapter.handle_message.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_ended_cli_rawsession_is_user_boundary_not_api_wake(tmp_path, monkeypatch):
    import time
    from tests.gateway.test_completion_delivery import _runner
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = "20261005_012442_1ab2bf"
    evt = {"type": "async_delegation", "delegation_id": "closed-cli-fixture",
           "parent_session_id": parent, "session_key": parent,
           "goal": "retain original result", "status": "completed", "summary": "real output"}
    ad._persist_dispatch({**evt, "dispatched_at": time.time()})
    ad._persist_completion(evt, {"status": "completed", "summary": "real output"})
    runner = _runner(SimpleNamespace(handle_message=AsyncMock()))
    runner._session_db = SimpleNamespace(
        get_session=AsyncMock(return_value={"id": parent, "source": "cli",
            "ended_at": time.time(), "end_reason": "cli_close"}),
        get_compression_tip=AsyncMock(return_value=parent))
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, load_transcript=AsyncMock(return_value=[]))
    runner._inject_watch_notification = AsyncMock(return_value=None)
    assert await runner._deliver_completion_notification(format_process_notification(evt), evt) is None
    runner._inject_watch_notification.assert_not_awaited()
    durable = ad.get_durable_delegation("closed-cli-fixture")
    assert durable["delivery_state"] == "dropped"
    assert durable["result"]["summary"] == "real output"
