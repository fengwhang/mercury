"""Regression test for #17929: AIAgent.__init__ should try fallback_model
when primary provider credentials are exhausted."""
import pytest
from unittest.mock import patch, MagicMock
from run_agent import AIAgent


def _make_tool_defs():
    return [{"type": "function", "function": {"name": "web_search",
             "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _mock_client(api_key="fb-key-1234567890", base_url="https://fb.example.com/v1"):
    c = MagicMock()
    c.api_key = api_key
    c.base_url = base_url
    c._default_headers = None
    return c


def test_init_tries_fallback_when_primary_returns_none():
    """When resolve_provider_client returns None for primary but succeeds for
    a fallback entry, __init__ should NOT raise RuntimeError."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None, api_mode=None):
        if provider == "tencent-token-plan":
            return fb, "kimi2.5"
        return None, None  # primary exhausted

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        agent = AIAgent(
            provider="alibaba-coding-plan",
            model="qwen3.6-plus",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "tencent-token-plan", "model": "kimi2.5"}],
        )
        assert agent.provider == "tencent-token-plan"
        assert agent.model == "kimi2.5"
        assert agent._fallback_activated is True


def test_init_raises_when_no_fallback_configured():
    """When primary returns None and no fallback is set, should raise."""
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        with pytest.raises(RuntimeError, match="no API key was found"):
            AIAgent(
                provider="alibaba-coding-plan",
                model="qwen3.6-plus",
                api_key=None,
                base_url=None,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=None,
            )


def test_init_cancelled_fallback_resolution_never_tries_next_provider():
    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            side_effect=[
                (None, None),
                InterruptedError("cancelled during startup fallback"),
                (_mock_client(base_url="https://fallback.invalid/v1"), "last"),
            ],
        ) as resolve,
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.context_compressor.get_model_context_length", return_value=200000),
    ):
        with pytest.raises(InterruptedError, match="cancelled"):
            AIAgent(
                provider="alibaba-coding-plan", model="primary",
                api_key=None, base_url=None, quiet_mode=True,
                skip_context_files=True, skip_memory=True,
                fallback_model=[
                    {"provider": "deepseek", "model": "first"},
                    {"provider": "openrouter", "model": "last"},
                ],
            )
    assert resolve.call_count == 2


@pytest.mark.parametrize("primary_provider", ["zai", "openrouter", "custom"])
@pytest.mark.parametrize(
    "fallback_model,expected_mode",
    [("anthropic/claude-sonnet-4-6", "anthropic_messages"),
     ("xiaomi/mimo-v2.6-pro", "chat_completions")],
)
def test_startup_fallback_keeps_wire_chain_progress_and_primary_recovery(
    fallback_model, expected_mode, primary_provider, monkeypatch,
):
    from types import SimpleNamespace

    http = MagicMock(side_effect=AssertionError("offline test attempted HTTP"))
    monkeypatch.setattr("httpx.Client.send", http)
    monkeypatch.setattr("requests.sessions.Session.request", http)
    recovered = False
    requested_primary = "custom:private" if primary_provider == "custom" else primary_provider
    routes = []
    chain = [
        {"provider": "deepseek", "model": "unavailable"},
        {"provider": "nous", "model": fallback_model},
        {"provider": "openrouter", "model": "vendor/final"},
    ]

    def resolve(provider, **kwargs):
        routes.append((provider, kwargs["model"]))
        if (provider in {primary_provider, requested_primary} and not recovered) or provider == "deepseek":
            return None, None
        return SimpleNamespace(
            api_key=f"fake-{provider}", base_url=f"https://{primary_provider if provider == requested_primary else provider}.invalid/v1",
            _custom_headers={},
        ), kwargs["model"]

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.anthropic_adapter.build_anthropic_client"),
        patch("agent.agent_init.fetch_model_metadata", return_value={}),
        patch("mercury_cli.auth.get_provider_auth_state", return_value={"access_token": "fake-nous"}),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.context_compressor.get_model_context_length", return_value=200000),
    ):
        agent = AIAgent(
            provider=primary_provider, model="primary", api_key=None, base_url=None,
            requested_provider=requested_primary,
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=chain,
        )
        assert agent.model == fallback_model
        assert agent.provider == agent.requested_provider == "nous"
        assert agent.api_mode == expected_mode
        assert agent._fallback_index == 2
        assert agent.context_compressor.api_mode == expected_mode
        assert agent._restore_primary_runtime() is False
        assert agent._fallback_index == 2
        assert agent.provider == "nous"

        recovered = True
        assert agent._restore_primary_runtime() is True
        assert agent.provider == primary_provider
        assert agent.requested_provider == requested_primary
        assert agent.model == "primary"
        assert agent._primary_runtime["provider"] == primary_provider
        assert agent._primary_runtime["requested_provider"] == requested_primary
        assert agent._fallback_chain == chain
        assert agent._fallback_index == 0
        assert agent._fallback_activated is False

    http.assert_not_called()
    assert routes == [
        (primary_provider, "primary"), ("deepseek", "unavailable"), ("nous", fallback_model),
        (requested_primary, "primary"), (requested_primary, "primary"),
    ]


def test_unavailable_private_custom_primary_does_not_become_keyless_on_restore(monkeypatch):
    from types import SimpleNamespace
    from agent.auxiliary_client import resolve_provider_client

    http = MagicMock(side_effect=AssertionError("offline test attempted HTTP"))
    monkeypatch.setattr("httpx.Client.send", http)
    monkeypatch.setattr("requests.sessions.Session.request", http)
    fallback_client = SimpleNamespace(
        api_key="fake-backup", base_url="https://backup.invalid/v1", _custom_headers={},
    )

    def resolve(provider, **kwargs):
        if provider == "openrouter":
            return fallback_client, kwargs["model"]
        return resolve_provider_client(provider, **kwargs)

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("agent.auxiliary_client._try_custom_endpoint", return_value=(None, None)),
        patch("agent.auxiliary_client._resolve_api_key_provider", return_value=(None, None)),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.context_compressor.get_model_context_length", return_value=200000),
    ):
        agent = AIAgent(
            provider="custom", model="vendor/private", api_key=None,
            base_url="https://private.invalid/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": "openrouter", "model": "vendor/backup"}],
        )
        assert agent.provider == "openrouter"
        assert agent._restore_primary_runtime() is False
        assert agent.provider == "openrouter"
        assert agent.api_key == "fake-backup"
        assert agent.model == "vendor/backup"
        assert agent._fallback_index == 1

    http.assert_not_called()


@pytest.mark.parametrize("requested_provider", ["", "auto"])
def test_inferred_startup_provider_never_auto_discovers_on_restore(requested_provider):
    from types import SimpleNamespace

    routes = []
    fallback_client = SimpleNamespace(
        api_key="fake-openrouter", base_url="https://openrouter.invalid/v1", _custom_headers={},
    )

    def resolve(provider, **kwargs):
        routes.append(provider)
        if provider == "openai-codex":
            return None, None
        return fallback_client, kwargs["model"]

    with (
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.context_compressor.get_model_context_length", return_value=200000),
    ):
        agent = AIAgent(
            provider=None, requested_provider=requested_provider, model="gpt-6.1-sol",
            api_key=None, base_url="https://chatgpt.com/backend-api/codex",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": "openrouter", "model": "vendor/backup"}],
        )
        assert agent.provider == "openrouter"
        assert agent._restore_primary_runtime() is False
        assert agent.provider == agent.requested_provider == "openrouter"
        assert agent.api_key == "fake-openrouter"
        assert agent.api_mode == "chat_completions"
        assert agent.model == "vendor/backup"
        assert routes == ["openai-codex", "openrouter", "openai-codex"]
