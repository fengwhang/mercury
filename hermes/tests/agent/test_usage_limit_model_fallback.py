"""Usage limits must fall back to the configured model instead of killing the agent.

Regression this pins: ``models.fallback`` / ``models.fallback_chain`` were
configured but never consulted, because the chain builder only understood the
stock ``fallback_providers`` dict vocabulary. ``agent._fallback_chain`` came out
EMPTY, so the failover gate (``_fallback_index < len(_fallback_chain)``) never
opened and a ``usage_limit_reached`` killed the agent dead — exactly the
reported symptom. A second leak: the credential pool claimed it could recover
from a plan-wide usage limit by rotating keys, which suppresses model failover
even with a chain configured.

Config/decision coverage plus an offline fake-provider conversation-loop walk.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import is_usage_limit_exhausted
from mercury_cli.fallback_config import (
    get_fallback_chain,
)


# ---------------------------------------------------------------------------
# the chain must resolve from Mercury's models: slots (the actual config shape)
# ---------------------------------------------------------------------------


def test_live_config_shape_yields_a_non_empty_chain() -> None:
    """The exact ~/.mercury/config.yaml shape that produced zero entries."""
    config = {
        "models": {
            "default": "openai-codex/gpt-6.1-sol",
            "fallback": "nous/xiaomi/mimo-v2.6-pro",
            "delegate_model": "openai-codex/gpt-6.1-sol",
            "delegate_fallback": "nous/xiaomi/mimo-v2.6-pro",
            "delegate_fallback_chain": [],
            "fallback_chain": [],
        }
    }
    chain = get_fallback_chain(config)
    assert chain, "models.fallback must produce a fallback entry"
    assert chain[0] == {"provider": "nous", "model": "xiaomi/mimo-v2.6-pro"}


def test_fallback_chain_entries_follow_the_declared_order() -> None:
    config = {
        "models": {
            "default": "a/b",
            "fallback": "n/one",
            "fallback_chain": ["n/one", "o/two", "p/three"],
        }
    }
    chain = get_fallback_chain(config)
    assert [(e["provider"], e["model"]) for e in chain] == [
        ("n", "one"),
        ("o", "two"),
        ("p", "three"),
    ]


def test_bare_model_names_borrow_the_default_provider() -> None:
    config = {"models": {"default": "nous/some-default", "fallback": "plain-model"}}
    chain = get_fallback_chain(config)
    assert chain == [{"provider": "nous", "model": "plain-model"}]


def test_unresolvable_entries_are_skipped_not_invented() -> None:
    config = {"models": {"default": "bare-default", "fallback": "bare-also"}}
    assert get_fallback_chain(config) == []


# ---------------------------------------------------------------------------
# stock vocabulary keeps working, and merges without duplicates
# ---------------------------------------------------------------------------


def test_stock_fallback_providers_still_resolve() -> None:
    config = {"fallback_providers": [{"provider": "anthropic", "model": "claude-x"}]}
    assert get_fallback_chain(config) == [
        {"provider": "anthropic", "model": "claude-x"}
    ]


def test_shared_models_override_stale_native_mirrors() -> None:
    config = {
        "models": {"default": "a/b", "fallback": "n/dup", "fallback_chain": []},
        "fallback_providers": [
            {"provider": "n", "model": "dup"},
            {"provider": "z", "model": "other"},
        ],
    }
    chain = get_fallback_chain(config)
    assert [(e["provider"], e["model"]) for e in chain] == [
        ("n", "dup"),
    ]


def test_empty_and_missing_config_yield_no_chain() -> None:
    assert get_fallback_chain(None) == []
    assert get_fallback_chain({}) == []
    assert get_fallback_chain({"models": None}) == []


def test_returns_fresh_dict_copies() -> None:
    config = {"models": {"default": "a/b", "fallback": "n/c"}}
    first = get_fallback_chain(config)
    first[0]["model"] = "mutated"
    assert get_fallback_chain(config)[0]["model"] == "c"


# ---------------------------------------------------------------------------
# usage-limit detection: the signal that must route to MODEL fallback
# ---------------------------------------------------------------------------


def test_usage_limit_reached_code_is_a_plan_wide_wall() -> None:
    assert is_usage_limit_exhausted({"reason": "usage_limit_reached"}) is True
    assert is_usage_limit_exhausted(error_code="usage_limit_reached") is True


def test_codex_usage_limit_wording_is_detected() -> None:
    # The exact shape seen in the #nixpad_dionysian* logs.
    assert (
        is_usage_limit_exhausted(
            {"message": "The usage limit has been reached (code=usage_limit_reached)"}
        )
        is True
    )
    assert is_usage_limit_exhausted(message="You hit your usage limit.") is True


def test_anthropic_gousagelimit_wording_is_detected() -> None:
    assert is_usage_limit_exhausted({"reason": "goUsageLimit"}) is True


def test_quota_and_limit_exceeded_wording_is_detected() -> None:
    assert is_usage_limit_exhausted(message="quota exceeded for this key") is True
    assert is_usage_limit_exhausted(message="key limit exceeded") is True


def test_explicit_rate_limit_wording_is_not_a_plan_wide_wall() -> None:
    """A request-rate throttle survives key rotation — keep the pool path."""
    assert is_usage_limit_exhausted(message="Rate limit exceeded") is False
    assert (
        is_usage_limit_exhausted(message="too many requests, rate limit exceeded")
        is False
    )


def test_explicit_usage_limit_uses_fallback_despite_reset_hint() -> None:
    """A future reset does not make the exhausted plan usable this turn."""
    assert (
        is_usage_limit_exhausted(message="usage limit reached, try again in 2 hours")
        is True
    )


def test_ordinary_errors_are_not_usage_limits() -> None:
    assert is_usage_limit_exhausted({}) is False
    assert is_usage_limit_exhausted(None) is False
    assert is_usage_limit_exhausted(message="connection refused") is False
    assert is_usage_limit_exhausted(message="invalid api key") is False


def test_explicit_empty_shared_fallback_suppresses_legacy_mirror():
    assert get_fallback_chain({"models": {"default": "a/b", "fallback": ""},
                               "fallback_providers": [{"provider": "z", "model": "stale"}]}) == []


def test_shared_fallback_retains_native_transport_options():
    assert get_fallback_chain({"models": {"default": "a/b", "fallback": "nous/xiaomi/mimo-v2.6-pro"},
                              "hermes": {"model_options": {"nous/xiaomi/mimo-v2.6-pro": {
                                  "base_url": "https://example.invalid/v1", "key_env": "TEST_KEY"}}}}) == [
        {"provider": "nous", "model": "xiaomi/mimo-v2.6-pro", "base_url": "https://example.invalid/v1", "key_env": "TEST_KEY"}]


@pytest.mark.parametrize("exhaust_chain", [False, True])
def test_shared_config_quota_walk_never_retries_exhausted_plan(exhaust_chain, monkeypatch):
    from run_agent import AIAgent

    http = MagicMock(side_effect=AssertionError("offline test attempted HTTP"))
    monkeypatch.setattr("httpx.Client.send", http)
    monkeypatch.setattr("requests.sessions.Session.request", http)

    config = {
        "models": {
            "default": "zai/primary",
            "fallback_chain": ["deepseek/vendor/first", "openrouter/vendor/last"],
        },
        "hermes": {
            "fallback_providers": [{"provider": "openai", "model": "stale-paid"}],
        },
    }
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.context_compressor.get_model_context_length", return_value=200000),
    ):
        agent = AIAgent(
            provider="zai", model="primary", api_key="fake-primary",
            base_url="https://primary.invalid/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
            fallback_model=get_fallback_chain(config),
        )
    agent._api_max_retries = 2
    agent.compression_enabled = False
    pool = MagicMock()
    pool.provider = "zai"
    pool.has_credentials.return_value = True
    pool.has_available.return_value = True
    agent._credential_pool = pool
    calls = []

    class QuotaError(Exception):
        status_code = 429
        body = {"error": {"type": "usage_limit_reached", "message": "Usage limit reached, try again in 2 hours"}}
        response = SimpleNamespace(headers={"retry-after": "0"})

    def call(api_kwargs, **kwargs):
        calls.append((agent.provider, agent.model))
        if exhaust_chain or agent.provider != "openrouter":
            raise QuotaError("Usage limit reached, try again in 2 hours")
        message = SimpleNamespace(content="Configured fallback succeeded.", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None, model=agent.model,
        )

    def resolve(provider, **kwargs):
        return SimpleNamespace(
            api_key=f"fake-{provider}", base_url=f"https://{provider}.invalid/v1",
            _custom_headers={},
        ), kwargs["model"]

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_interruptible_streaming_api_call", side_effect=call),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve) as router,
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        patch("agent.context_compressor.get_model_context_length", return_value=200000),
        patch("agent.conversation_loop.time.sleep"),
        patch("agent.agent_runtime_helpers.time.sleep"),
    ):
        result = agent.run_conversation("hello")

    assert calls == [
        ("zai", "primary"), ("deepseek", "vendor/first"), ("openrouter", "vendor/last"),
    ]
    http.assert_not_called()
    assert [entry.args[0] for entry in router.call_args_list] == ["deepseek", "openrouter"]
    pool.mark_exhausted_and_rotate.assert_not_called()
    assert agent._credential_pool is None
    if exhaust_chain:
        assert result["completed"] is False
        assert result["failed"] is True
    else:
        assert result["completed"] is True
        assert result["final_response"] == "Configured fallback succeeded."
