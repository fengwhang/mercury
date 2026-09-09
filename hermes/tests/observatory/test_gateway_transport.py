"""Unit tests for observatory/gateway_transport.py.

The sidecar's delivery half: home probing, error mapping, validation. The
socket itself is faked at the query_fn seam — no gateway, no network.
"""

from pathlib import Path

import pytest

from observatory.gateway_transport import (
    ControlSocketGatewayTransport,
    GatewayTransportError,
    gateway_socket_homes,
)


def _transport(query_fn, home=None, **kwargs):
    return ControlSocketGatewayTransport(home or Path("/fake/mercury"), query_fn=query_fn, **kwargs)


def test_socket_homes_probe_standard_then_nested():
    homes = gateway_socket_homes(Path("/fake/mercury"))
    assert homes == [Path("/fake/mercury"), Path("/fake/mercury/hermes")]


def test_prompt_sends_inject_shape_and_returns_reply():
    calls: list = []

    def fake_query(home, text, kind, node_id, timeout):
        calls.append((home, text, kind, node_id, timeout))
        return {"reply": "pong"}

    import asyncio

    t = _transport(fake_query)
    assert asyncio.run(t.prompt("hello?", kind="prompt", node_id="gw")) == "pong"
    assert calls == [(Path("/fake/mercury"), "hello?", "prompt", "gw", t.timeout)]


def test_prompt_falls_back_to_nested_home():
    seen: list = []

    def fake_query(home, text, kind, node_id, timeout):
        seen.append(home)
        if str(home).endswith("hermes"):
            return {"reply": "nested-pong"}
        return None

    import asyncio

    assert asyncio.run(_transport(fake_query).prompt("hi")) == "nested-pong"
    assert seen == [Path("/fake/mercury"), Path("/fake/mercury/hermes")]


def test_all_homes_miss_raises_transport_error():
    import asyncio

    with pytest.raises(GatewayTransportError, match="no gateway answered"):
        asyncio.run(_transport(lambda *a: None).prompt("hi"))


def test_home_exception_continues_to_next_home():
    import asyncio

    def fake_query(home, text, kind, node_id, timeout):
        if not str(home).endswith("hermes"):
            raise OSError("stale socket")
        return {"reply": "recovered"}

    assert asyncio.run(_transport(fake_query).prompt("hi")) == "recovered"


def test_empty_text_rejected():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(_transport(lambda *a: {"reply": "x"}).prompt("   "))


def test_non_string_reply_rejected():
    import asyncio

    with pytest.raises(GatewayTransportError, match="without a reply string"):
        asyncio.run(_transport(lambda *a: {"noreply": 1}).prompt("hi"))


def test_construction_is_side_effect_free(tmp_path: Path):
    # No I/O at build time — safe to construct on every boot.
    t = ControlSocketGatewayTransport(tmp_path / "does-not-exist")
    assert t.mercury_home == tmp_path / "does-not-exist"
