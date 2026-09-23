"""IRC MEDIA passes send_message (Lounge-link delivery, never omit)."""

from __future__ import annotations

import json


import pytest


@pytest.mark.parametrize("message_t", ["see {F}", "{F}"])
def test_irc_media_reaches_adapter_without_omit(
    tmp_path, monkeypatch, message_t) -> None:
    from tools import send_message_tool as sm
    import tools.interrupt as interrupt_mod
    import gateway.config as gateway_config
    import gateway.platform_registry as preg

    f = tmp_path / "r.pdf"
    f.write_bytes(b"x")

    monkeypatch.setattr(
        sm, "resolve_send_target",
        lambda platform_name, target_ref, **kw: ("#vm_ace", None, None))

    import types
    from gateway.config import Platform

    fake_config = types.SimpleNamespace(
        platforms={Platform("irc"): types.SimpleNamespace(enabled=True)},
        get_home_channel=lambda platform: None,
    )
    monkeypatch.setattr(
        gateway_config, "load_gateway_config", lambda: fake_config)
    monkeypatch.setattr(
        preg.platform_registry, "get",
        lambda name: types.SimpleNamespace(send_message_handler=None))
    monkeypatch.setattr(interrupt_mod, "is_interrupted", lambda: False)
    monkeypatch.setattr(
        sm, "_maybe_skip_cron_duplicate_send",
        lambda platform_name, chat_id, thread_id: None)

    seen = {}

    async def _fake_send(platform, pconfig, chat_id, chunk, **kwargs):
        seen.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(sm, "_send_to_platform", _fake_send)

    out = json.loads(sm._handle_send(
        {"target": "irc:#vm_ace",
         "message": f"MEDIA:{f}" if message_t == "{F}"
         else f"see MEDIA:{f}"}))
    blob = json.dumps(out)
    assert "only supported" not in blob
    assert "omitted" not in blob.lower()
    assert seen.get("media_files"), "MEDIA must reach the IRC adapter"
