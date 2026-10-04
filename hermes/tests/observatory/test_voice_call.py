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
    assert ok is True  # fail open: only positive OMP evidence denies


def test_resolve_channel_engine_no_manager_fails_open() -> None:
    assert vc.resolve_channel_engine("#anything") == "hermes"


def test_resolve_channel_engine_routes(monkeypatch) -> None:
    class Manager:
        def __init__(self, route, row):
            self.route = route
            self.row = row

        def inbound_route(self, _channel):
            return self.route, self.row

    import observatory.rooms as rooms

    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("spawn-omp", {}))
    assert vc.resolve_channel_engine("#x") == "omp"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("spawn-hermes", {}))
    assert vc.resolve_channel_engine("#x") == "hermes"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("child", {"engine": "omp"}))
    assert vc.resolve_channel_engine("#x") == "omp"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: Manager("child", {"engine": "hermes"}))
    assert vc.resolve_channel_engine("#x") == "hermes"
    monkeypatch.setattr(rooms, "get_room_manager", lambda: None)
    assert vc.resolve_channel_engine("#x") == "hermes"


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
