"""VM round 3 defect 4: no-config runtime reasoning default is xhigh.

The wizard picker already defaults xhigh, but the runtime still read
medium at every no-config fallback. Each test below pins one fallback
site: absent config → xhigh (Solar: high, its wire max), while explicit
levels, disables, and provider-capability clamps are unchanged.

Deliberate non-change: config-file "" stays unset/omit (the provider
decides — e.g. kimi sends thinking-enabled rather than an effort);
pinning it would flip omit-paths provider-wide. Provider clamps
(nearest-weaker, invalid-input guards) are also untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- local reasoning helpers -------------------------------------------------


class TestLmstudioEffort:
    def test_none_and_empty_default_xhigh(self):
        from agent.lmstudio_reasoning import resolve_lmstudio_effort

        assert resolve_lmstudio_effort(None, None) == "xhigh"
        assert resolve_lmstudio_effort({}, None) == "xhigh"
        assert resolve_lmstudio_effort({"enabled": True}, None) == "xhigh"

    def test_explicit_and_disabled_preserved(self):
        from agent.lmstudio_reasoning import resolve_lmstudio_effort

        assert resolve_lmstudio_effort({"effort": "low"}, None) == "low"
        assert resolve_lmstudio_effort({"effort": "medium"}, None) == "medium"
        assert resolve_lmstudio_effort({"enabled": False}, None) == "none"

    def test_ceiling_clamps_to_xhigh(self):
        from agent.lmstudio_reasoning import resolve_lmstudio_effort

        assert resolve_lmstudio_effort({"effort": "max"}, None) == "xhigh"
        assert resolve_lmstudio_effort({"effort": "ultra"}, None) == "xhigh"
        assert resolve_lmstudio_effort({"effort": "on"}, None) == "xhigh"


class TestGeminiThinkingConfig:
    def test_empty_config_maps_xhigh(self):
        from agent.transports.chat_completions import _build_gemini_thinking_config

        cfg = _build_gemini_thinking_config("gemini-3-flash-preview", {})
        assert cfg is not None
        # xhigh is above flash's ceiling → provider max (high)
        assert cfg.get("thinkingLevel") == "high"

    def test_explicit_medium_preserved(self):
        from agent.transports.chat_completions import _build_gemini_thinking_config

        cfg = _build_gemini_thinking_config(
            "gemini-3-flash-preview", {"effort": "medium"})
        assert cfg is not None
        assert cfg.get("thinkingLevel") == "medium"


class TestChatCompletionsGenericReasoning:
    def _kwargs(self, reasoning_config):
        from agent.transports import get_transport

        return get_transport("chat_completions").build_kwargs(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            supports_reasoning=True,
            reasoning_config=reasoning_config,
        )

    def test_none_config_is_xhigh(self):
        kw = self._kwargs(None)
        assert kw["extra_body"]["reasoning"] == {"enabled": True, "effort": "xhigh"}

    def test_empty_config_is_xhigh(self):
        kw = self._kwargs({})
        assert kw["extra_body"]["reasoning"] == {"enabled": True, "effort": "xhigh"}

    def test_explicit_medium_preserved(self):
        kw = self._kwargs({"enabled": True, "effort": "medium"})
        assert kw["extra_body"]["reasoning"] == {"enabled": True, "effort": "medium"}


class TestCodexTransportDefault:
    def _kwargs(self, reasoning_config):
        from agent.transports import get_transport

        return get_transport("codex_responses").build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            reasoning_config=reasoning_config,
        )

    def test_none_config_is_xhigh(self):
        assert self._kwargs(None)["reasoning"]["effort"] == "xhigh"

    def test_explicit_medium_preserved(self):
        kw = self._kwargs({"enabled": True, "effort": "medium"})
        assert kw["reasoning"]["effort"] == "medium"


class TestAnthropicAdapterDefault:
    def _kwargs(self, reasoning_config):
        from agent.anthropic_adapter import build_anthropic_kwargs

        return build_anthropic_kwargs(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config=reasoning_config,
        )

    def test_missing_effort_key_is_xhigh_budget(self):
        kw = self._kwargs({"enabled": True})
        assert kw["thinking"] == {"type": "enabled", "budget_tokens": 32000}

    def test_explicit_medium_preserved(self):
        kw = self._kwargs({"enabled": True, "effort": "medium"})
        assert kw["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    def test_adaptive_no_effort_key_is_xhigh(self):
        from agent.anthropic_adapter import build_anthropic_kwargs

        kw = build_anthropic_kwargs(
            model="anthropic/claude-opus-5",
            messages=[{"role": "user", "content": "hello"}],
            tools=None,
            max_tokens=4096,
            reasoning_config={"enabled": True},
        )
        assert kw["output_config"] == {"effort": "xhigh"}


# --- provider profiles -------------------------------------------------------


class TestProviderProfileDefaults:
    def test_openrouter_none_is_xhigh(self):
        from plugins.model_providers.openrouter import openrouter

        extra, _top = openrouter.build_api_kwargs_extras(
            reasoning_config=None, supports_reasoning=True)
        assert extra["reasoning"] == {"enabled": True, "effort": "xhigh"}

    def test_nous_none_is_xhigh(self):
        from plugins.model_providers.nous import nous

        extra, _top = nous.build_api_kwargs_extras(
            reasoning_config=None, supports_reasoning=True)
        assert extra["reasoning"] == {"enabled": True, "effort": "xhigh"}

    def test_ai_gateway_none_is_xhigh(self):
        from plugins.model_providers.ai_gateway import vercel

        extra, _top = vercel.build_api_kwargs_extras(
            reasoning_config=None, supports_reasoning=True)
        assert extra["reasoning"] == {"enabled": True, "effort": "xhigh"}

    def test_nebius_none_clamps_to_provider_max(self):
        """Nebius accepts only low/medium/high — the xhigh default clamps
        to its ceiling (high), like upstage."""
        from plugins.model_providers.nebius_token_factory import nebius_token_factory

        _extra, top = nebius_token_factory.build_api_kwargs_extras(
            reasoning_config=None, model="qwen3-235b")
        assert top["reasoning_effort"] == "high"

    def test_nebius_explicit_medium_preserved(self):
        from plugins.model_providers.nebius_token_factory import nebius_token_factory

        _extra, top = nebius_token_factory.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="qwen3-235b")
        assert top["reasoning_effort"] == "medium"

    def test_upstage_none_is_solar_max(self):
        """Solar accepts only low/medium/high — the xhigh default clamps
        to its ceiling (high), never an invalid level."""
        from plugins.model_providers.upstage import upstage

        _extra, top = upstage.build_api_kwargs_extras(
            reasoning_config=None, model="solar-pro3")
        assert top["reasoning_effort"] == "high"

    def test_meta_ai_none_is_xhigh(self):
        from plugins.model_providers.meta_ai import _resolve_effort

        assert _resolve_effort(None) == "xhigh"
        assert _resolve_effort({}) == "xhigh"
        assert _resolve_effort({"effort": "medium"}) == "medium"
        assert _resolve_effort({"enabled": False}) == "minimal"

    def test_kimi_none_still_omits_effort(self):
        """Unset stays omit (thinking-enabled, server depth) — pinning an
        effort here would change kimi's wire shape provider-wide."""
        from plugins.model_providers.kimi_coding import kimi

        extra, top = kimi.build_api_kwargs_extras(reasoning_config=None)
        assert extra == {"thinking": {"type": "enabled"}}
    def test_copilot_none_is_xhigh_when_supported(self, monkeypatch):
        from providers import get_provider_profile

        monkeypatch.setattr(
            "mercury_cli.models.github_model_reasoning_efforts",
            lambda model: ["low", "medium", "high", "xhigh"],
        )
        profile = get_provider_profile("copilot")
        assert profile is not None
        extra, _top = profile.build_api_kwargs_extras(
            reasoning_config=None, supports_reasoning=True, model="gpt-5.5")
        assert extra["reasoning"] == {"effort": "xhigh"}

    def test_copilot_none_clamps_when_xhigh_unsupported(self, monkeypatch):
        from providers import get_provider_profile

        monkeypatch.setattr(
            "mercury_cli.models.github_model_reasoning_efforts",
            lambda model: ["low", "medium", "high"],
        )
        profile = get_provider_profile("copilot")
        assert profile is not None
        extra, _top = profile.build_api_kwargs_extras(
            reasoning_config=None, supports_reasoning=True, model="gpt-5.4")
        assert extra["reasoning"]["effort"] in ("low", "medium", "high")


# --- session-level resolvers -------------------------------------------------


class TestGithubModelsResolver:
    def _agent(self, reasoning_config):
        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent.model = "gpt-5.5"
        agent.reasoning_config = reasoning_config
        return agent

    def test_none_config_is_xhigh(self, monkeypatch):
        import mercury_cli.models as models_mod

        monkeypatch.setattr(
            models_mod, "github_model_reasoning_efforts",
            lambda model: ["low", "medium", "high", "xhigh", "max"],
        )
        agent = self._agent(None)
        assert agent._github_models_reasoning_extra_body() == {"effort": "xhigh"}

    def test_disabled_is_none(self, monkeypatch):
        import mercury_cli.models as models_mod

        monkeypatch.setattr(
            models_mod, "github_model_reasoning_efforts",
            lambda model: ["low", "medium", "high", "xhigh", "max"],
        )
        agent = self._agent({"enabled": False})
        assert agent._github_models_reasoning_extra_body() is None

    def test_unsupported_clamps_to_provider_max(self, monkeypatch):
        """Provider clamp untouched: no-config xhigh on a high-capped
        model resolves to high (the pre-existing xhigh→high rule)."""
        import mercury_cli.models as models_mod

        monkeypatch.setattr(
            models_mod, "github_model_reasoning_efforts",
            lambda model: ["low", "medium", "high"],
        )
        agent = self._agent(None)
        assert agent._github_models_reasoning_extra_body() == {"effort": "high"}


class TestAuxBuilderDefault:
    def test_effortless_config_is_xhigh(self):
        from agent.auxiliary_client import _build_call_kwargs

        kw = _build_call_kwargs(
            "no-such-prov-xyz", "x-model",
            [{"role": "user", "content": "hi"}],
            reasoning_config={"enabled": True},
        )
        assert kw["extra_body"]["reasoning"] == {"enabled": True, "effort": "xhigh"}

    def test_explicit_medium_preserved(self):
        from agent.auxiliary_client import _build_call_kwargs

        kw = _build_call_kwargs(
            "no-such-prov-xyz", "x-model",
            [{"role": "user", "content": "hi"}],
            reasoning_config={"enabled": True, "effort": "medium"},
        )
        assert kw["extra_body"]["reasoning"] == {"enabled": True, "effort": "medium"}


# --- omp side (already xhigh: verify) ----------------------------------------


class TestOmpSideStaysXhigh:
    def test_delegate_default_xhigh(self, monkeypatch):
        from tools import omp_delegation

        monkeypatch.setattr(omp_delegation, "_omp_delegate_env", lambda: ({}, None))
        assert omp_delegation._delegate_thinking_level() == "xhigh"
        assert omp_delegation._delegate_fallback_thinking_level() == "xhigh"


# --- display defaults --------------------------------------------------------


class TestDisplayDefaults:
    def test_cli_reasoning_panel_default_xhigh(self, capsys):
        pytest.importorskip("rich")
        pytest.importorskip("prompt_toolkit")
        from mercury_cli.cli_commands_mixin import CLICommandsMixin

        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli.reasoning_config = None
        cli.show_reasoning = False
        cli.reasoning_full = False
        cli.agent = None
        cli._handle_reasoning_command("/reasoning")
        out = capsys.readouterr().out
        assert "xhigh (default)" in out

    def test_cli_reasoning_panel_explicit_preserved(self, capsys):
        pytest.importorskip("rich")
        pytest.importorskip("prompt_toolkit")
        from mercury_cli.cli_commands_mixin import CLICommandsMixin

        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli.reasoning_config = {"enabled": True, "effort": "low"}
        cli.show_reasoning = True
        cli.reasoning_full = False
        cli.agent = None
        cli._handle_reasoning_command("/reasoning")
        assert "low" in capsys.readouterr().out

    def test_gateway_locale_default_xhigh(self):
        text = (REPO_ROOT / "hermes" / "locales" / "en.yaml").read_text(
            encoding="utf-8")
        for line in text.splitlines():
            if "level_default:" in line:
                assert "xhigh" in line
                assert "medium" not in line
                return
        raise AssertionError("level_default key missing from en.yaml")
