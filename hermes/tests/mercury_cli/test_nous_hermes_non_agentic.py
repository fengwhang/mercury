"""Tests for the Nous-Mercury-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"mercury"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``mercury-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "mercury" tag namespace.

``is_nous_hermes_non_agentic`` should only match the actual Nous Research
Mercury-3 / Mercury-4 chat family.
"""

from __future__ import annotations

import pytest

from mercury_cli.model_switch import (
    _HERMES_MODEL_WARNING,
    _check_hermes_model_warning,
    is_nous_hermes_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/Mercury-3-Llama-3.1-70B",
        "NousResearch/Mercury-3-Llama-3.1-405B",
        "mercury-3",
        "Mercury-3",
        "mercury-4",
        "mercury-4-405b",
        "hermes_4_70b",
        "openrouter/hermes3:70b",
        "openrouter/nousresearch/mercury-4-405b",
        "NousResearch/Hermes3",
        "mercury-3.1",
    ],
)
def test_matches_real_nous_hermes_chat_models(model_name: str) -> None:
    assert is_nous_hermes_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous Mercury 3/4"
    )
    assert _check_hermes_model_warning(model_name) == _HERMES_MODEL_WARNING


