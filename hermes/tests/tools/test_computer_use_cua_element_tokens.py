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


@pytest.mark.parametrize("tool,method,kwargs", [
    ("double_click", "click", {"element": 7, "click_count": 2}),
    ("scroll", "scroll", {"direction": "down", "element": 7}),
    ("set_value", "set_value", {"value": "fixture text", "element": 7}),
])
def test_schema_token_routes(tool, method, kwargs):
    session = _StrictSession(token_tools={tool})

    result = getattr(_backend(session), method)(**kwargs)

    assert result.ok, f"{result.code}: {result.message}"
    assert len(session.calls) == 1
    name, args = session.calls[0]
    assert name == tool
    assert args["element_index"] == 7
    assert args["element_token"] == "snapshot-1:7"
    assert args["session"] == "mercury-token-test"


def test_legacy_capability_still_attaches_token():
    session = _StrictSession(legacy_tools={"click"})
    assert not session.supports_input_property("click", "element_token")

    result = _backend(session).click(element=7)

    assert result.ok
    assert session.calls[0][1]["element_token"] == "snapshot-1:7"


def test_driver_advertising_neither_keeps_strict_legacy_arguments():
    session = _StrictSession()

    result = _backend(session).click(element=7)

    assert result.ok
    assert session.calls == [("click", {
        "pid": 42, "window_id": 9, "button": "left", "element_index": 7,
        "session": "mercury-token-test",
    })]


@pytest.mark.parametrize("tool,method,kwargs", [
    ("click", "click", {"element": 7}),
    ("double_click", "click", {"element": 7, "click_count": 2}),
    ("scroll", "scroll", {"direction": "down", "element": 7}),
    ("set_value", "set_value", {"value": "fixture text", "element": 7}),
])
def test_token_support_is_scoped_to_exact_tool(tool, method, kwargs):
    # A different tool's schema AND legacy capability must not authorize this tool.
    supported = "scroll" if tool == "click" else "click"
    session = _StrictSession(token_tools={supported}, legacy_tools={supported})

    result = getattr(_backend(session), method)(**kwargs)

    assert result.ok
    assert session.calls[0][0] == tool
    assert "element_token" not in session.calls[0][1]
    assert session.calls[0][1]["session"] == "mercury-token-test"


@pytest.mark.parametrize("token", ["", None])
def test_empty_cached_token_preserves_snapshot_refusal(token):
    session = _StrictSession(token_tools={"click"})

    result = _backend(session, token=token).click(element=7)

    assert not result.ok
    assert result.code == "snapshot_id_required"
    assert len(session.calls) == 1
    assert "element_token" not in session.calls[0][1]


def test_uncached_index_does_not_reuse_another_elements_token():
    session = _StrictSession(token_tools={"click"})

    result = _backend(session).click(element=99)

    assert not result.ok
    assert result.code == "snapshot_id_required"
    assert len(session.calls) == 1
    assert session.calls[0][1]["element_index"] == 99
    assert "element_token" not in session.calls[0][1]


def test_stale_token_refusal_propagates_without_coordinate_fallback():
    session = _StrictSession(token_tools={"click"})
    session.failure = {
        "code": "stale", "message": "Snapshot was superseded; capture again.",
        "effect": "suspected_noop", "escalation": {"recommended": "px"},
    }

    result = _backend(session).click(element=7)

    assert not result.ok
    assert result.code == "stale"
    assert result.message == session.failure["message"]
    assert result.effect == "suspected_noop"
    assert result.escalation == {"recommended": "px"}
    assert len(session.calls) == 1
    assert session.calls[0][1]["element_token"] == "snapshot-1:7"
    assert "x" not in session.calls[0][1]


def test_transport_error_propagates_without_replay():
    session = _StrictSession(token_tools={"click"})
    session.error = RuntimeError("fixture transport failed")

    result = _backend(session).click(element=7)

    assert not result.ok
    assert result.message == "cua-driver error: fixture transport failed"
    assert len(session.calls) == 1
    assert session.calls[0][1]["element_token"] == "snapshot-1:7"


@pytest.mark.parametrize("tool,method,kwargs", [
    ("click", "click", {"x": 10, "y": 20}),
    ("scroll", "scroll", {"direction": "down", "x": 10, "y": 20}),
    ("drag", "drag", {"from_xy": (10, 20), "to_xy": (30, 40)}),
    ("drag", "drag", {"from_element": 7, "to_element": 8}),
])
def test_non_element_index_routes_do_not_attach_single_target_token(tool, method, kwargs):
    session = _StrictSession(token_tools={tool})

    result = getattr(_backend(session), method)(**kwargs)

    assert result.ok
    assert len(session.calls) == 1
    assert session.calls[0][0] == tool
    assert "element_token" not in session.calls[0][1]
    assert session.calls[0][1]["session"] == "mercury-token-test"
    if "from_element" in kwargs:
        assert session.calls[0][1]["from_element"] == 7
        assert session.calls[0][1]["to_element"] == 8


def test_explicit_action_session_is_preserved_with_token():
    session = _StrictSession(token_tools={"click"})

    result = _backend(session)._action("click", {"element_index": 7, "session": "explicit-session"})

    assert result.ok
    assert session.calls == [("click", {
        "element_index": 7, "element_token": "snapshot-1:7", "session": "explicit-session",
    })]


def test_standalone_focus_remains_without_session_or_token():
    session = _StrictSession()

    result = _backend(session).bring_to_front(pid=42, window_id=9)

    assert result.ok
    assert session.calls == [("bring_to_front", {"pid": 42, "window_id": 9})]


@pytest.mark.parametrize("verdict", ["deny", "approve_once"])
def test_wrapper_consent_still_gates_delegation_to_token_backend(monkeypatch, verdict):
    import json
    from tools.computer_use import tool as computer_use

    session = _StrictSession(token_tools={"click"})
    backend = _backend(session)
    approvals = []
    delegated = []

    def approve(action, args, summary):
        approvals.append((action, args["element"]))
        return verdict

    def get_backend(*, session_id):
        delegated.append(session_id)
        return backend

    monkeypatch.setattr(computer_use, "_approval_callback", approve)
    monkeypatch.setattr(computer_use, "_get_backend", get_backend)
    result = json.loads(computer_use.handle_computer_use(
        {"action": "click", "element": 7}, session_id="delegated-session",
    ))

    assert approvals == [("click", 7)]
    if verdict == "deny":
        assert result["error"]
        assert delegated == []
        assert session.calls == []
    else:
        assert delegated == ["delegated-session"]
        assert result["ok"]
        assert len(session.calls) == 1
        assert session.calls[0][1]["element_token"] == "snapshot-1:7"
        assert session.calls[0][1]["session"] == "mercury-token-test"
