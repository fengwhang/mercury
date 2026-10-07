"""Snapshot-token routing through the imported production backend, without a desktop."""

from __future__ import annotations

import pytest

from tools.computer_use.cua_backend import CuaDriverBackend, _CuaDriverSession


class _StrictSession(_CuaDriverSession):
    """In-process daemon contract: strict schemas and snapshot-bound element input."""

    def __init__(self, *, token_tools=(), legacy_tools=()):
        self._capabilities = {
            name: ({"accessibility.element_tokens"} if name in legacy_tools else set())
            for name in ("click", "double_click", "scroll", "set_value", "drag", "bring_to_front")
        }
        properties = {
            "click": {"pid", "window_id", "button", "element_index", "x", "y", "session", "delivery_mode"},
            "double_click": {"pid", "window_id", "button", "element_index", "x", "y", "session"},
            "scroll": {"pid", "window_id", "direction", "amount", "element_index", "session"},
            "set_value": {"pid", "window_id", "element_index", "value", "session"},
            "drag": {"pid", "window_id", "from_element", "to_element", "from_x", "from_y", "to_x", "to_y", "session"},
            "bring_to_front": {"pid", "window_id"},
        }
        self._tool_schemas = {
            name: {
                "type": "object",
                "additionalProperties": False,
                "properties": {prop: {} for prop in props | ({"element_token"} if name in token_tools else set())},
            }
            for name, props in properties.items()
        }
        self.calls = []
        self.failure = None
        self.error = None

    def call_tool(self, name, args, *, timeout=30.0):
        self.calls.append((name, dict(args)))
        allowed = set(self._tool_schemas[name]["properties"])
        # Legacy metadata can advertise a supported property absent from tools/list.
        if self.supports_capability("accessibility.element_tokens", tool=name):
            allowed.add("element_token")
        assert not set(args) - allowed, f"unexpected properties for {name}: {set(args) - allowed}"
        if self.error is not None:
            raise self.error
        payload = self.failure
        if payload is None and "element_index" in args and "element_token" in allowed and not args.get("element_token"):
            payload = {
                "code": "snapshot_id_required",
                "message": f"{name}: bare element_index is not accepted; pass element_token, or snapshot_id together with element_index",
            }
        return {
            "isError": payload is not None,
            "data": {},
            "structuredContent": payload if payload is not None else {"effect": "confirmed"},
        }


def _backend(session, *, token="snapshot-1:7"):
    backend = CuaDriverBackend.__new__(CuaDriverBackend)
    backend._session = session
    backend._session_id = "mercury-token-test"
    backend._snapshot_tokens = {7: token, 8: "snapshot-1:8"}
    backend._active_pid = 42
    backend._active_window_id = 9
    return backend


def test_schema_token_click_avoids_snapshot_id_required():
    session = _StrictSession(token_tools={"click"})
    assert session.supports_input_property("click", "element_token")
    assert not session.supports_capability("accessibility.element_tokens", tool="click")

    result = _backend(session).click(element=7)

    assert result.ok, f"{result.code}: {result.message}"
    assert session.calls == [("click", {
        "pid": 42, "window_id": 9, "button": "left", "element_index": 7,
        "element_token": "snapshot-1:7", "session": "mercury-token-test",
    })]
