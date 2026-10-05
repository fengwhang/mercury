"""Usage limits must fall back to the configured model instead of killing the agent.

Regression this pins: ``models.fallback`` / ``models.fallback_chain`` were
configured but never consulted, because the chain builder only understood the
stock ``fallback_providers`` dict vocabulary. ``agent._fallback_chain`` came out
EMPTY, so the failover gate (``_fallback_index < len(_fallback_chain)``) never
opened and a ``usage_limit_reached`` killed the agent dead — exactly the
reported symptom. A second leak: the credential pool claimed it could recover
from a plan-wide usage limit by rotating keys, which suppresses model failover
even with a chain configured.

Filesystem-free; pure config/decision logic.
"""
from __future__ import annotations

from agent.error_classifier import is_usage_limit_exhausted
from mercury_cli.fallback_config import (
    _split_model_selector,
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


def test_model_ids_with_internal_slashes_split_on_the_first_slash_only() -> None:
    assert _split_model_selector("nous/xiaomi/mimo-v2.6-pro") == (
        "nous",
        "xiaomi/mimo-v2.6-pro",
    )
    assert _split_model_selector("openai-codex/gpt-6.1-sol") == (
        "openai-codex",
        "gpt-6.1-sol",
    )


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


def test_stock_and_mercury_shapes_merge_and_dedupe() -> None:
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
        ("z", "other"),
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


def test_transient_reset_signal_is_not_a_plan_wide_wall() -> None:
    """A periodic quota that names its reset window will refill — retry it."""
    assert (
        is_usage_limit_exhausted(message="usage limit reached, try again in 2 hours")
        is False
    )


def test_ordinary_errors_are_not_usage_limits() -> None:
    assert is_usage_limit_exhausted({}) is False
    assert is_usage_limit_exhausted(None) is False
    assert is_usage_limit_exhausted(message="connection refused") is False
    assert is_usage_limit_exhausted(message="invalid api key") is False
