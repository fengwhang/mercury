"""Tests for shared-metrics configuration discovery and setup."""

from __future__ import annotations

import argparse

import pytest

from mercury_cli.config import DEFAULT_CONFIG
from mercury_cli.setup import setup_telemetry
from mercury_cli.subcommands.setup import build_setup_parser


@pytest.fixture(autouse=True)
def no_installed_driver_changes(monkeypatch):
    monkeypatch.setattr(
        "tools.computer_use.cua_backend.cua_driver_telemetry_disable_persistent",
        lambda: True,
    )


def test_shared_metrics_are_registered_disabled_by_default():
    assert DEFAULT_CONFIG["telemetry"]["shared_metrics"]["enabled"] is False


def test_setup_cannot_preserve_legacy_driver_reporting_opt_in(monkeypatch):
    config = {"computer_use": {"cua_telemetry": True}}
    disabled = []
    monkeypatch.setattr(
        "tools.computer_use.cua_backend.cua_driver_telemetry_disable_persistent",
        lambda: disabled.append(True) or True,
    )
    monkeypatch.setattr(
        "mercury_cli.setup.prompt_yes_no", lambda _question, default: default,
    )

    setup_telemetry(config)

    assert disabled == [True]
    assert "cua_telemetry" not in config["computer_use"]


def test_setup_telemetry_states_cua_driver_policy(monkeypatch, capsys):
    """The wizard names the cua-driver policy so the upstream installer's
    'Telemetry defaults to enabled' line is not mistaken for Mercury's."""
    config = {}
    monkeypatch.setattr(
        "mercury_cli.setup.prompt_yes_no",
        lambda _question, default: default,
    )

    setup_telemetry(config)

    out = capsys.readouterr().out
    assert "CUA_DRIVER_RS_TELEMETRY_ENABLED=0" in out
    assert "unless you opt in" not in out


def test_setup_telemetry_enables_shared_metrics(monkeypatch):
    config = {}
    monkeypatch.setattr(
        "mercury_cli.setup.prompt_yes_no",
        lambda _question, default: not default,
    )

    setup_telemetry(config)

    assert config["telemetry"]["shared_metrics"]["enabled"] is True


def test_setup_parser_accepts_telemetry_section():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    handler = object()
    build_setup_parser(subparsers, cmd_setup=handler)

    args = parser.parse_args(["setup", "telemetry"])

    assert args.section == "telemetry"
    assert args.func is handler
