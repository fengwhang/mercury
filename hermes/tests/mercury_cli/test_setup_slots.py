"""Tests for the setup wizard's Model Slots section + wizard invocation counts.

Slice A — delegation provider step (2026-09-08): the delegate (subagent)
pickers must use the CHOSEN delegation provider's catalog, not the default
slot's. Bare ids are prefixed with the chosen provider; the ordinary
fallback keeps the default catalog; skip still clears.

Slice B — wizard-once + tools single-pass: the full wizard must invoke each
section fn exactly once, and the wizard's tools step must run a single
linear checklist pass even with messenger platforms enabled.
"""

from argparse import Namespace
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import mercury_cli.setup as setup_mod
from mercury_cli.models import CANONICAL_PROVIDERS


FALLBACK_TITLE = "Select fallback model (used when the default fails mid-turn; empty to skip):"
SECOND_TITLE = "Select second-order fallback (used when the MAIN fallback also fails; empty to skip):"
DELEGATE_TITLE = "Select delegate model (the model omp SUBAGENTS run on):"
DELEGATE_FB_TITLE = "Select delegate fallback (subagent retry model; empty to skip):"
DELEGATE_2ND_TITLE = "Select second-order delegate fallback (used when the SUBAGENT fallback also fails; empty to skip):"

OR_CATALOG = ["or-m1", "or-m2"]
ZAI_CATALOG = ["zai-m1", "zai-m2"]

CANONICAL_SLUGS = [p.slug for p in CANONICAL_PROVIDERS]


def _zai_index():
    return CANONICAL_SLUGS.index("zai")


def _openrouter_index():
    return CANONICAL_SLUGS.index("openrouter")


@pytest.fixture
def slots_env(tmp_path, monkeypatch):
    """Sandbox the unified config path; hermes view has an openrouter default."""
    monkeypatch.setenv("MERCURY_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    return tmp_path


def _enter_slots_patches(stack, *, model_answers, provider_choice, catalogs):
    """Mock the slots section's boundaries. Returns (seen, written).

    model_answers: title -> model id (or None = skip). Missing title -> None.
    provider_choice: index into the canonical slug list, or None = cancel.
    catalogs: provider -> list (missing provider -> []).
    seen: {"models": [(title, catalog, current)], "provider": {...}}.
    written: the dict passed to _write_slots.
    """
    seen = {"models": [], "provider": {}}
    written = {}

    stack.enter_context(
        patch.object(
            setup_mod, "load_config",
            return_value={"model": {"provider": "openrouter", "default": "somemodel"}},
        )
    )

    def fake_model_ids(provider, force_refresh=False):
        return list(catalogs.get(provider or "", []))

    stack.enter_context(
        patch("mercury_cli.models.provider_model_ids", side_effect=fake_model_ids)
    )
    stack.enter_context(
        patch("mercury_cli.models.get_pricing_for_provider", return_value={})
    )

    def fake_pick(model_ids, current_model="", pricing=None, title="", **kwargs):
        seen["models"].append((title, list(model_ids), current_model))
        return model_answers.get(title)

    stack.enter_context(
        patch("mercury_cli.auth._prompt_model_selection", side_effect=fake_pick)
    )

    def fake_provider_choice(choices, default=0, title="Select provider:"):
        seen["provider"] = {"choices": list(choices), "default": default, "title": title}
        return provider_choice

    stack.enter_context(
        patch("mercury_cli.main._prompt_provider_choice", side_effect=fake_provider_choice)
    )
    stack.enter_context(
        patch("mercury_cli.omp_sync._write_slots",
              side_effect=lambda update: written.update(update) or True)
    )
    return seen, written


def _catalog_for(seen, title):
    for t, catalog, _current in seen["models"]:
        if t == title:
            return catalog
    raise AssertionError(f"no picker call for {title!r}")


class TestDelegationProvider:
    """Delegate pickers use the chosen delegation provider's catalog."""

    def test_chosen_catalog_and_prefixing(self, slots_env):
        """zai chosen: delegate pickers get the zai catalog, prefixed zai/."""
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack,
                model_answers={
                    FALLBACK_TITLE: "or-fb",
                    SECOND_TITLE: None,
                    DELEGATE_TITLE: "zai-dm",
                    DELEGATE_FB_TITLE: None,
                },
                provider_choice=_zai_index(),
                catalogs={"openrouter": OR_CATALOG, "zai": ZAI_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})

        # Ordinary fallback keeps the default provider's catalog.
        assert _catalog_for(seen, FALLBACK_TITLE) == OR_CATALOG
        # Delegate pickers get the CHOSEN provider's catalog.
        assert _catalog_for(seen, DELEGATE_TITLE) == ZAI_CATALOG
        # Prefixing follows the picker side.
        assert written["fallback"] == "openrouter/or-fb"
        assert written["delegate_model"] == "zai/zai-dm"
        assert written["delegate_fallback"] == ""
        # Default slot untouched.
        assert written["default"] == "openrouter/somemodel"

    def test_provider_picker_defaults_to_default_slot_provider(self, slots_env):
        """The provider step preselects the default slot's provider."""
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack,
                model_answers={DELEGATE_TITLE: None},
                provider_choice=_openrouter_index(),
                catalogs={"openrouter": OR_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})

        assert seen["provider"]["default"] == _openrouter_index()
        assert _catalog_for(seen, DELEGATE_TITLE) == OR_CATALOG

    def test_provider_cancel_keeps_default_provider(self, slots_env):
        """Cancel at the provider step = today's behavior (default catalog)."""
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack,
                model_answers={DELEGATE_TITLE: "dm"},
                provider_choice=None,
                catalogs={"openrouter": OR_CATALOG, "zai": ZAI_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})

        assert _catalog_for(seen, DELEGATE_TITLE) == OR_CATALOG
        assert written["delegate_model"] == "openrouter/dm"

    def test_live_only_catalog_degrades_gracefully(self, slots_env):
        """Empty/failed chosen catalog: picker still offered, custom name works."""
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack,
                model_answers={DELEGATE_TITLE: "typed-custom-id"},
                provider_choice=_zai_index(),
                catalogs={"openrouter": OR_CATALOG},  # zai live-only -> []
            )
            setup_mod._prompt_mercury_slots({})

        # Degraded to the configured delegate values (none) — neutral menu
        # with only the custom-name escape; the typed id still prefixes.
        assert _catalog_for(seen, DELEGATE_TITLE) == []
        assert written["delegate_model"] == "zai/typed-custom-id"

    def test_qualified_ids_pass_through_unprefixed(self, slots_env):
        """Already-qualified ids (e.g. custom providers) are never re-prefixed."""
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack,
                model_answers={DELEGATE_TITLE: "myprov/mymodel"},
                provider_choice=_zai_index(),
                catalogs={"openrouter": OR_CATALOG, "zai": ZAI_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})

        assert written["delegate_model"] == "myprov/mymodel"
