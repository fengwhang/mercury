"""Reasoning-level defaults + wizard per-slot picker (user directive 2026-09-10).

Covers:
- xhigh defaults hold for delegate + orchestrator thinking levels on fresh installs
- wizard reasoning picker asks per slot (model/fallback/delegate/delegate fallback),
  choices off..max (+auto omp-side), default xhigh, SKIP=EMPTY (skip leaves untouched,
  never auto-mirrors or resurrects)
- hermes-side maps to agent.reasoning_effort values, omp-side to Effort strings
- NO ultra level (valid set tops at max), NO 512 budget in wizard/bridge scope.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_PATH = REPO_ROOT / "bridge" / "bridge.py"

import importlib.util as _ilu


def _load_bridge():
    spec = _ilu.spec_from_file_location("mercury_bridge_reasoning", str(BRIDGE_PATH))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bridge_thinking_default_is_xhigh():
    b = _load_bridge()
    assert b.DEFAULT_THINKING_LEVEL == "xhigh"
    assert b.thinking_level_from_config("") == "xhigh"
    assert b.thinking_level_from_config(None) == "xhigh"
    assert b.thinking_level_from_config("xhigh") == "xhigh"


def test_bridge_valid_set_tops_at_max_no_ultra():
    b = _load_bridge()
    assert "ultra" not in b.VALID_THINKING_LEVELS
    assert "ultra" not in b.HERMES_THINKING_LEVELS
    assert "ultra" not in b.OMP_THINKING_LEVELS
    assert b.VALID_THINKING_LEVELS[-1] in ("max", "auto")
    # base ladder tops at max (auto is a selector, not a level)
    assert "max" in b.VALID_THINKING_LEVELS
    assert b.thinking_level_from_config("ultra") is None
    assert b.thinking_level_from_config("MAX") == "max"


def test_bridge_omp_accepts_off_and_auto_hermes_rejects_auto():
    b = _load_bridge()
    assert b.thinking_level_from_config("off") == "off"
    assert b.thinking_level_from_config("auto") == "auto"
    assert b.thinking_level_from_config("auto", allow_auto=False) is None
    assert b.thinking_level_from_config("off", allow_auto=False) == "off"


def test_bridge_validate_thinking_levels():
    b = _load_bridge()
    base = {
        "default": "openrouter/m",
        "fallback": "",
        "delegate_model": "openrouter/d",
        "delegate_fallback": "",
        "delegate_fallback_chain": [],
        "fallback_chain": [],
    }
    assert b.validate({**base}) == []
    bad = dict(base, delegate_thinking_level="ultra")
    assert any("delegate_thinking_level" in e for e in b.validate(bad))
    bad_auto_orch = dict(base, orchestrator_thinking_level="auto")
    assert any("orchestrator_thinking_level" in e for e in b.validate(bad_auto_orch))
    ok_off = dict(base, delegate_thinking_level="off", orchestrator_thinking_level="off")
    assert b.validate(ok_off) == []


def test_sync_defaults_delegate_and_orchestrator_to_xhigh(tmp_path, monkeypatch):
    from mercury_cli import omp_sync

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "hermes:\n"
        "  model:\n"
        "    default: meta/muse-spark-1.3-contributor\n"
        "    provider: openrouter\n"
        "models:\n"
        "  default: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync, "_read_model_default", lambda: ("openrouter", "meta/muse-spark-1.3-contributor")
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)
    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    text = cfg.read_text(encoding="utf-8")
    assert "delegate_thinking_level: xhigh" in text
    assert "orchestrator_thinking_level: xhigh" in text
    # fallback thinking is NOT defaulted (SKIP=EMPTY: inherit at runtime, never mirrored)
    assert "delegate_fallback_thinking_level" not in text


def test_sync_never_mirrors_or_resurrects_fallback_thinking(tmp_path, monkeypatch):
    from mercury_cli import omp_sync

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n  provider: openrouter\n  default: m1\n"
        "models:\n  default: openrouter/m1\n"
        "  delegate_thinking_level: high\n"
        "  orchestrator_thinking_level: high\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(omp_sync, "_read_model_default", lambda: ("openrouter", "m1"))
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)
    before = cfg.read_text(encoding="utf-8")
    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    after = cfg.read_text(encoding="utf-8")
    # existing explicit levels kept (no overwrite to xhigh), fallback thinking not invented
    assert "delegate_thinking_level: high" in after
    assert "orchestrator_thinking_level: high" in after
    assert "delegate_fallback_thinking_level" not in after
    assert "delegate_thinking_level: xhigh" not in after


def test_delegation_thinking_defaults_xhigh_and_fallback_inherits(monkeypatch):
    from tools import omp_delegation

    monkeypatch.setattr(omp_delegation, "_omp_delegate_env", lambda: ({}, None))
    assert omp_delegation._delegate_thinking_level() == "xhigh"
    assert omp_delegation._delegate_fallback_thinking_level() == "xhigh"
    monkeypatch.setattr(
        omp_delegation, "_omp_delegate_env", lambda: ({"OMP_THINKING_LEVEL": "high"}, None)
    )
    assert omp_delegation._delegate_thinking_level() == "high"
    # fallback empty inherits delegate
    assert omp_delegation._delegate_fallback_thinking_level() == "high"
    monkeypatch.setattr(
        omp_delegation,
        "_omp_delegate_env",
        lambda: ({"OMP_THINKING_LEVEL": "high", "OMP_FALLBACK_THINKING_LEVEL": "low"}, None),
    )
    assert omp_delegation._delegate_fallback_thinking_level() == "low"


def test_picker_vocabularies_no_ultra_default_xhigh():
    from mercury_cli import setup as setup_mod

    assert setup_mod.REASONING_DEFAULT == "xhigh"
    assert "ultra" not in setup_mod.HERMES_REASONING_CHOICES
    assert "ultra" not in setup_mod.OMP_REASONING_CHOICES
    assert "auto" not in setup_mod.HERMES_REASONING_CHOICES
    assert "auto" in setup_mod.OMP_REASONING_CHOICES
    assert setup_mod.HERMES_REASONING_CHOICES[-1] == "max"
    assert "512" not in setup_mod.HERMES_REASONING_CHOICES
    assert "512" not in setup_mod.OMP_REASONING_CHOICES
    # hermes off -> none (agent.reasoning_effort disabled); omp off/auto pass through
    assert setup_mod._hermes_reasoning_value("off") == "none"
    assert setup_mod._hermes_reasoning_value("xhigh") == "xhigh"
    assert setup_mod._omp_reasoning_value("off") == "off"
    assert setup_mod._omp_reasoning_value("auto") == "auto"


def test_pick_reasoning_skip_leaves_untouched():
    from mercury_cli import setup as setup_mod

    # cancel (-1) -> None (skip, never the default)
    with patch.object(setup_mod, "_curses_prompt_choice", return_value=-1), patch.object(
        setup_mod, "is_noninteractive", return_value=False
    ), patch.object(setup_mod, "is_interactive_stdin", return_value=True):
        assert setup_mod._pick_reasoning_level("t", "low") is None
    # explicit pick returns the choice
    with patch.object(setup_mod, "_curses_prompt_choice", return_value=5), patch.object(
        setup_mod, "is_noninteractive", return_value=False
    ), patch.object(setup_mod, "is_interactive_stdin", return_value=True):
        assert setup_mod._pick_reasoning_level("t", "") == "xhigh"
    # non-interactive -> None (sync tail ensures engine xhigh defaults)
    with patch.object(setup_mod, "is_noninteractive", return_value=True):
        assert setup_mod._pick_reasoning_level("t", "") is None


def test_slot_reasoning_skip_writes_nothing_no_mirror_no_resurrect(tmp_path, monkeypatch):
    from mercury_cli import setup as setup_mod
    from mercury_cli.omp_sync import _current_slots

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("models:\n  default: openrouter/m1\n", encoding="utf-8")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    config: dict = {}
    # all four skipped -> nothing written anywhere
    with patch.object(setup_mod, "_pick_reasoning_level", return_value=None):
        setup_mod._prompt_slot_reasoning(config, "openrouter/m1", "openrouter/m2", "openrouter/d1", "openrouter/d2")
    assert config == {}
    slots = _current_slots()
    assert slots["delegate_thinking_level"] == ""
    assert slots["delegate_fallback_thinking_level"] == ""


def test_slot_reasoning_explicit_picks_map_and_store(tmp_path, monkeypatch):
    from mercury_cli import setup as setup_mod
    from mercury_cli.omp_sync import _current_slots

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("models:\n  default: openrouter/m1\n", encoding="utf-8")
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg_path))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    config: dict = {}
    answers = iter(["off", "high", "auto", "low"])

    def fake_pick(title, current="", allow_auto=False):
        return next(answers)

    with patch.object(setup_mod, "_pick_reasoning_level", side_effect=fake_pick):
        setup_mod._prompt_slot_reasoning(config, "openrouter/m1", "openrouter/m2", "openrouter/d1", "openrouter/d2")
    # hermes-side: off -> none, stored per-model
    overrides = config["agent"]["reasoning_overrides"]
    assert overrides["openrouter/m1"] == "none"
    assert overrides["openrouter/m2"] == "high"
    # omp-side: Effort strings pass through (auto kept)
    slots = _current_slots()
    assert slots["delegate_thinking_level"] == "auto"
    assert slots["delegate_fallback_thinking_level"] == "low"


def test_slot_reasoning_empty_model_slots_never_asked():
    from mercury_cli import setup as setup_mod

    config: dict = {}
    with patch.object(setup_mod, "_pick_reasoning_level") as picker:
        setup_mod._prompt_slot_reasoning(config, "", "", "", "")
        picker.assert_not_called()
    assert config == {}
