"""mercury-<platform> composites validate; lounge_share never defers."""

from __future__ import annotations


def test_validate_mercury_platform_composite_gated_on_registration(
    monkeypatch,
) -> None:
    from toolsets import validate_toolset
    import gateway.platform_registry as preg

    assert not validate_toolset("mercury-irc")
    monkeypatch.setattr(
        preg.platform_registry, "is_registered",
        lambda name: name == "irc")
    assert validate_toolset("mercury-irc")
    assert not validate_toolset("mercury-nope")


def test_lounge_share_never_deferred() -> None:
    from tools.registry import discover_builtin_tools
    from tools.tool_search import is_deferrable_tool_name

    discover_builtin_tools()
    assert not is_deferrable_tool_name("lounge_share")
