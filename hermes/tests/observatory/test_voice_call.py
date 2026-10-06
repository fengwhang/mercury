"""Voice-call manager: Hermes-only guard, config, registry, envelope."""

from __future__ import annotations

import pytest

from observatory import voice_call as vc


def test_engine_guard_hermes_only() -> None:
    ok, _ = vc.check_engine_allowed("hermes")
    assert ok is True
    ok, reason = vc.check_engine_allowed("omp")
    assert ok is False
    assert "Hermes" in reason
    ok, _ = vc.check_engine_allowed("weird-future-engine")
    assert ok is False


def test_resolve_channel_agent_without_live_room_is_unknown() -> None:
    assert vc.resolve_channel_agent("#anything") == {"engine": "unknown", "name": ""}


def test_resolve_channel_agent_routes(monkeypatch) -> None:
    class Manager:
        def __init__(self, route, row):
            self.route = route
            self.row = row

        def inbound_route(self, _channel):
            return self.route, self.row

    import observatory.rooms as rooms

    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("spawn-omp", {"engine": "omp"}))
    assert vc.resolve_channel_agent("#x")["engine"] == "omp"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("spawn-hermes", {"engine": "hermes"}))
    assert vc.resolve_channel_agent("#x")["engine"] == "hermes"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("child", {"engine": "omp"}))
    assert vc.resolve_channel_agent("#x")["engine"] == "omp"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("child", {"engine": "hermes", "name": "Gaia"}))
    assert vc.resolve_channel_agent("#x") == {"engine": "hermes", "name": "Gaia"}
    monkeypatch.setattr(rooms, "get_room_manager", lambda: None)
    assert vc.resolve_channel_agent("#x")["engine"] == "unknown"


def test_separate_web_process_resolves_live_gateway_tree(tmp_path, monkeypatch):
    from observatory import rooms, state

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: None)
    monkeypatch.setattr(state, "default_state_db_path", lambda: db_path)
    assert vc.resolve_channel_agent("#coder")["engine"] == "unknown"
    assert not db_path.exists()  # dashboard requests must not create state
    with state.ObservatoryState(db_path) as tree:
        for name, engine in [("coder", "omp"), ("chat", "hermes")]:
            tree.add_node(name, engine=engine, name=name, slug=name,
                          mxid=name, session_ref=name)
            tree.set_room_id(name, "#" + name)
        assert vc.resolve_channel_agent("#CODER") == {"engine": "omp", "name": "coder"}
        assert vc.resolve_channel_agent("#chat") == {"engine": "hermes", "name": "chat"}
        tree.mark_dead("coder")
        assert vc.resolve_channel_agent("#coder") == {"engine": "unknown", "name": ""}


def test_store_lifecycle() -> None:
    store = vc.VoiceCallStore()
    assert store.status("#a")["active"] is False
    record = store.start("#a")
    assert record["engine"] == "hermes" and record["muted"] is False
    assert store.status("#a")["active"] is True
    assert store.set_muted("#a", True)["muted"] is True
    assert store.set_muted("#missing", True) is None
    assert store.active_channels() == ["#a"]
    assert store.end("#a") is True
    assert store.end("#a") is False


def test_store_refuses_omp() -> None:
    store = vc.VoiceCallStore()
    with pytest.raises(vc.VoiceCallEngineError):
        store.start("#o", engine="omp")


def test_transcript_envelope_shape() -> None:
    assert vc.transcript_envelope(" #a ", "hello") == {
        "channel": "#a",
        "text": "hello",
        "source": "voice-call",
        "engine": "hermes",
    }


def test_require_hosts_fails_closed() -> None:
    with pytest.raises(vc.VoiceCallConfigError):
        vc.require_voice_call_hosts({})
    urls = vc.require_voice_call_hosts({"voice_call": {
        "mirc_host_url": "http://m:8000/",
        "mlounge_host_url": "http://l:9000",
        "stt_sidecar_url": "http://l:8765",
    }})
    assert urls == {
        "mirc_host_url": "http://m:8000",
        "mlounge_host_url": "http://l:9000",
        "stt_sidecar_url": "http://l:8765",
    }

@pytest.mark.parametrize("engine", ["omp", "unknown", "future-engine", ""])
def test_live_route_cannot_promote_nonhermes_registry_engine(monkeypatch, engine):
    from observatory import rooms

    class GatewayRoute:
        def inbound_route(self, _channel):
            return "gateway", {"engine": engine, "name": "registered agent"}

    monkeypatch.setattr(rooms, "get_room_manager", lambda: GatewayRoute())
    agent = vc.resolve_channel_agent("#gateway")
    assert not vc.check_engine_allowed(agent["engine"])[0]
    assert agent["engine"] == ("omp" if engine == "omp" else "unknown")
