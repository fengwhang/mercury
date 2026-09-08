"""Secondary-chat labeling: the Matrix observatory is the primary chat, so the
gateway Matrix entry is labeled plain ``Matrix`` and the gateway menu frames
the rest as secondary platforms.
"""

from __future__ import annotations

import pytest
import yaml

from mercury_cli.platforms import platform_label


def test_platforms_registry_labels_matrix_plain():
    assert platform_label("matrix") == "💬 Matrix"
    assert "Secondary" not in platform_label("matrix")


def test_matrix_plugin_yaml_label_is_plain():
    from pathlib import Path

    plugin_yaml = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins"
        / "platforms"
        / "matrix"
        / "plugin.yaml"
    )
    assert yaml.safe_load(plugin_yaml.read_text(encoding="utf-8"))["label"] == "Matrix"


def test_matrix_adapter_registers_plain_label():
    from plugins.platforms.matrix import adapter as matrix_adapter

    seen: dict = {}

    class _Ctx:
        def register_platform(self, name, **kwargs):
            seen[name] = kwargs

    matrix_adapter.register(_Ctx())
    assert seen["matrix"]["label"] == "Matrix"


def test_gateway_menu_frames_secondary_platforms(monkeypatch, capsys, tmp_path):
    """setup_gateway checklist asks for *secondary* platforms (Matrix lives
    in the observatory section that now runs first)."""
    import mercury_cli.gateway as gateway_mod
    from mercury_cli import setup as setup_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    questions: list = []

    def capture_prompt_checklist(question, choices, pre_selected=None):
        questions.append(question)
        return []

    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **kw: False)
    monkeypatch.setattr(setup_mod, "prompt_checklist", capture_prompt_checklist)
    monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
    monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
    monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

    setup_mod.setup_gateway({})

    assert questions == ["Select secondary chat platforms to configure:"]
    out = capsys.readouterr().out
    assert (
        "The Matrix observatory is your primary chat. "
        "These secondary platforms are optional extras." in out
    )
