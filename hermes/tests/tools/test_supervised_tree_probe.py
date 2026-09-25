"""Tree-membership probe: agent terminal children count as inside."""

from __future__ import annotations


def test_no_markers_not_inside(monkeypatch) -> None:
    from tools import process_registry as pr

    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    assert pr._is_in_supervised_gateway_tree() is False


def test_gateway_marker_without_supervisor_not_inside(monkeypatch) -> None:
    """Import leakage (_HERMES_GATEWAY=1, no supervisor) must NOT refuse:
    CLIs and serve --isolated manage the gateway legitimately."""
    from tools import process_registry as pr

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert pr._is_in_supervised_gateway_tree() is False


def test_supervised_child_marker_counts(monkeypatch) -> None:
    from tools import process_registry as pr

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    assert pr._is_in_supervised_gateway_tree() is True


def test_invocation_id_counts_for_terminal_children(monkeypatch) -> None:
    """systemd INVOCATION_ID is inherited by agent shell children."""
    from tools import process_registry as pr

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    assert pr._is_in_supervised_gateway_tree() is True
