"""VM-report slice 1: hardcoded glm fallback killed + spawn model key fixed.

Repro of the silent path: ``build_hermes_agent`` read ``cfg.get("models")``
(plural — no such key) so a configured ``model.default`` resolved to "" and
the runtime silently auto-picked a provider the user never chose (zai via an
inherited ZAI_API_KEY) landing on the stale glm-5.2 silent default.

Cover: singular-key resolution mirroring oneshot._run_agent (explicit arg →
HERMES_INFERENCE_MODEL → config default/model → dict split), plural key
ignored, stale glm-5.2 gone from the silent default + picker badge, and
fail-closed (RuntimeError) when nothing resolves — never a silent glm.
"""
from __future__ import annotations

import sys
import types

import pytest

from observatory import spawn as spawn_mod


class _FakeSessionDB:
    def __init__(self, db_path=None):
        self.db_path = db_path


class _FakeAgent:
    def __init__(self, **kw):
        self.kw = kw
        for k, v in kw.items():
            setattr(self, k, v)


def _patch(monkeypatch, cfg):
    """Route build_hermes_agent's lazy imports to fakes; capture AIAgent kwargs."""
    import mercury_cli.config as config_mod
    import mercury_cli.runtime_provider as runtime_mod
    import mercury_state as state_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    seen = {}

    def _resolve(*, requested=None, target_model=None, **kw):
        seen["target_model"] = target_model
        return {
            "api_key": "k",
            "base_url": "http://x",
            "provider": "p",
            "requested_provider": "p",
            "api_mode": "openai",
            "credential_pool": None,
        }

    monkeypatch.setattr(runtime_mod, "resolve_runtime_provider", _resolve)
    monkeypatch.setattr(state_mod, "SessionDB", _FakeSessionDB)
    fake_run_agent = types.ModuleType("run_agent")
    captured = {}

    class _Agent(_FakeAgent):
        def __init__(self, **kw):
            captured.update(kw)
            super().__init__(**kw)

    fake_run_agent.AIAgent = _Agent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    return seen, captured


def test_singular_model_default_used_not_plural(monkeypatch, tmp_path):
    """Configured model.default reaches the agent (the VM's lost value)."""
    seen, captured = _patch(monkeypatch, {"model": {"default": "anthropic/claude-x"}})
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    spawn_mod.build_hermes_agent(mercury_home=tmp_path)
    assert captured["model"] == "anthropic/claude-x"
    assert seen["target_model"] == "anthropic/claude-x"


def test_plural_models_key_ignored_fail_closed(monkeypatch, tmp_path):
    """The old plural key consults nothing: models-only config fails closed."""
    seen, captured = _patch(monkeypatch, {"models": {"default": "evil/nope"}})
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="no model configured"):
        spawn_mod.build_hermes_agent(mercury_home=tmp_path)


def test_dict_valued_default_split(monkeypatch, tmp_path):
    """Dict-valued model.default splits like oneshot (model, not provider)."""
    cfg = {"model": {"default": {"provider": "zai", "model": "z-ai/glm-5.3"}}}
    seen, captured = _patch(monkeypatch, cfg)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    spawn_mod.build_hermes_agent(mercury_home=tmp_path)
    assert captured["model"] == "z-ai/glm-5.3"


def test_env_beats_config_explicit_beats_env(monkeypatch, tmp_path):
    seen, captured = _patch(monkeypatch, {"model": {"default": "a/b"}})
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "c/d")
    spawn_mod.build_hermes_agent(mercury_home=tmp_path)
    assert captured["model"] == "c/d"
    _, captured2 = _patch(monkeypatch, {"model": {"default": "a/b"}})
    spawn_mod.build_hermes_agent(mercury_home=tmp_path, model="e/f")
    assert captured2["model"] == "e/f"


def test_empty_config_fails_closed_never_silent_glm(monkeypatch, tmp_path):
    """No model anywhere → RuntimeError, never a silent glm/zai synthesis."""
    _patch(monkeypatch, {})
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    # An inherited provider key must not divert the observatory path.
    monkeypatch.setenv("ZAI_API_KEY", "inherited-not-chosen")
    with pytest.raises(RuntimeError, match="no model configured"):
        spawn_mod.build_hermes_agent(mercury_home=tmp_path)


def test_stale_glm52_silent_default_killed():
    """The hardcoded stale fallback is glm-5.3 nowhere glm-5.2."""
    from mercury_cli.models import (
        OPENROUTER_MODELS,
        PREFERRED_SILENT_DEFAULT_MODEL,
        get_preferred_silent_default_model,
    )

    assert PREFERRED_SILENT_DEFAULT_MODEL == "z-ai/glm-5.3"
    badge = dict(OPENROUTER_MODELS)
    assert badge["z-ai/glm-5.3"] == "default"
    assert badge["z-ai/glm-5.2"] == ""
    # No cached catalog in this env → falls to the (now current) constant.
    assert "glm-5.2" not in get_preferred_silent_default_model()
