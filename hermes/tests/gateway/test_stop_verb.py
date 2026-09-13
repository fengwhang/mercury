"""Terminal stop verb: kills background batches in idle and busy windows.

Red-first regression tests for the idle-`stop`-narrates bug (gateway IDs
2947-2970): bare `stop` with a LIVE background batch must kill the batch
via interrupt_fn and ack deterministically — never run a model turn.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import tools.async_delegation as ad
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._send_with_retry = AsyncMock()
    adapter._pending_messages = {}
    adapter._unwrap_ephemeral = lambda reply: (str(reply), None)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.peek_session_id.return_value = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._is_user_authorized_for_source = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_a, **_k: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *a, **k: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._busy_text_mode = "interrupt"
    runner._draining = False
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    runner._sibling_thread_run_keys = lambda _source, _key: []
    runner._thread_metadata_for_source = lambda *_a, **_k: {}
    runner._reply_anchor_for_event = lambda _e: None
    # Session-state stub for busy-ack paths (unused on the stop path).
    runner._peek_session_state = lambda _key: None
    runner._session_state = MagicMock()
    runner._session_key_for_source = lambda _s: build_session_key(_make_source())
    runner._adapter_for_source = lambda _s: adapter
    runner._is_session_running = lambda _k: _k in runner._running_agents
    runner._effective_busy_input_mode = lambda _s: "interrupt"
    runner._effective_busy_text_mode = lambda _s: "interrupt"
    return runner, adapter


def _seed_batch(session_key: str, parent_session_id: str, delegation_id: str = "deleg_stop1"):
    fn = MagicMock()
    with ad._records_lock:
        ad._records[delegation_id] = {
            "delegation_id": delegation_id,
            "status": "running",
            "session_key": session_key,
            "parent_session_id": parent_session_id,
            "goal": "nightly lint sweep",
            "interrupt_fn": fn,
        }
    return fn


@pytest.fixture(autouse=True)
def _reset_delegations():
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _stop_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m-stop",
    )


class TestIdleStopKillsBatch:
    @pytest.mark.asyncio
    async def test_idle_slash_stop_kills_live_batch(self):
        runner, _adapter = _make_runner()
        sk = build_session_key(_make_source())
        fn = _seed_batch(sk, "sess-1")
        assert ad.has_live_for_session(session_key=sk, parent_session_id="sess-1") is True

        result = await runner._handle_stop_command(_stop_event("/stop"))
        text = str(result)

        fn.assert_called_once()
        assert "kill" in text.lower() or "stopp" in text.lower()
        assert "lint" in text.lower() or "1" in text

    @pytest.mark.asyncio
    async def test_idle_bare_stop_never_runs_model_turn(self):
        runner, _adapter = _make_runner()
        sk = build_session_key(_make_source())
        fn = _seed_batch(sk, "sess-1")

        async def _fail_if_model_runs(event, source, key, generation):
            raise AssertionError("bare stop must not run a model turn")

        runner._handle_message_with_agent = _fail_if_model_runs

        result = await runner._handle_message(_stop_event("stop"))

        fn.assert_called_once()
        assert result is not None
        assert "kill" in str(result).lower() or "stopp" in str(result).lower()

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running_is_deterministic(self):
        runner, _adapter = _make_runner()
        result = await runner._handle_stop_command(_stop_event("/stop"))
        assert "nothing running" in str(result).lower()

        result_bare = await runner._handle_message(_stop_event("stop"))
        assert result_bare is not None
        assert "nothing running" in str(result_bare).lower()


class TestBusyStopKillsOnce:
    @pytest.mark.asyncio
    async def test_busy_slash_stop_kills_batch_exactly_once(self):
        runner, _adapter = _make_runner()
        sk = build_session_key(_make_source())
        fn = _seed_batch(sk, "sess-1")
        agent = MagicMock()
        agent.interrupt = MagicMock()
        runner._running_agents[sk] = agent
        runner._interrupt_and_clear_session = AsyncMock()

        result = await runner._busy_stop_command(_stop_event("/stop"), sk, _make_source())

        fn.assert_called_once()
        assert "kill" in str(result).lower() or "stopp" in str(result).lower()

    @pytest.mark.asyncio
    async def test_busy_bare_stop_kills_batch_exactly_once(self):
        runner, _adapter = _make_runner()
        sk = build_session_key(_make_source())
        fn = _seed_batch(sk, "sess-1", delegation_id="deleg_busy_bare")
        agent = MagicMock()
        agent.interrupt = MagicMock()
        agent._supports_active_turn_redirect = False
        runner._running_agents[sk] = agent
        runner._interrupt_and_clear_session = AsyncMock()
        runner._agent_has_active_subagents = lambda _a: False
        runner._session_has_compression_in_flight = AsyncMock(return_value=False)
        runner._pending_event_audio_paths = lambda _e: []
        runner._prepare_busy_steer_text = AsyncMock(return_value="stop")

        handled = await runner._handle_active_session_busy_message(_stop_event("stop"), sk)

        assert handled is True
        fn.assert_called_once()


class TestMatrixBareStop:
    def test_gateway_bare_stop_is_terminal_not_steer(self):
        from pathlib import Path

        from observatory.control import AbortSession, ControlRouter, InjectText, PowerLevelSnapshot, RoomPowerLevels
        from observatory.identity import assign_slug, virtual_mxid
        from observatory.state import ObservatoryState

        import tempfile

        tmp = Path(tempfile.mkdtemp())
        state = ObservatoryState(tmp / "state.db")
        slug = assign_slug("gateway agent", state)
        state.add_node(
            "gw", engine="hermes", name="gateway agent", slug=slug,
            mxid=virtual_mxid(slug), session_ref="session:gw",
            parent_node_id=None, extra={"kind": "gateway"},
        )
        room = "!room-gw:mercury.local"
        state.set_room_id("gw", room)
        pl = PowerLevelSnapshot({room: RoomPowerLevels(users={"@owner:mercury.local": 100})})
        router = ControlRouter(state, gateway_node_id="gw", pl_provider=pl)

        event = {
            "type": "m.room.message",
            "room_id": room,
            "sender": "@owner:mercury.local",
            "content": {"msgtype": "m.text", "body": "stop"},
        }
        outcome = router.route(event)
        assert outcome.disposition == "stop"
        assert len(outcome.actions) == 1
        assert isinstance(outcome.actions[0], AbortSession)
        assert not any(isinstance(a, InjectText) for a in outcome.actions)
