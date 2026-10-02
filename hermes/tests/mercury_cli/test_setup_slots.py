"""Tests for the setup wizard's Model Slots section + wizard invocation counts.

Slice A — delegation provider step (2026-09-08): the delegate (subagent)
pickers must use the CHOSEN delegation provider's catalog, not the default
slot's. Each fallback can choose its own provider; bare ids are prefixed
with that provider and skip still clears.

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
FALLBACK_PROVIDER = "Select fallback provider:"
SECOND_PROVIDER = "Select second-order fallback provider:"
DELEGATE_PROVIDER = "Select delegation provider (the provider omp SUBAGENTS run on):"
DELEGATE_FB_PROVIDER = "Select delegate fallback provider:"
DELEGATE_2ND_PROVIDER = "Select second-order delegate fallback provider:"

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
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path


def _enter_slots_patches(stack, *, model_answers, provider_choice, catalogs,
                         provider_choices=None, persist=False):
    """Mock the slots section's boundaries. Returns (seen, written).

    model_answers: title -> model id (or None = skip). Missing title -> None.
    provider_choice: delegation provider index, or None = cancel.
    provider_choices: per-title provider indices; otherwise keep the default.
    catalogs: provider -> list (missing provider -> []).
    seen: {"models": [(title, catalog, current)], "provider": {...}}.
    written: the dict passed to _write_slots.
    """
    seen = {"models": [], "provider": {}, "providers": [], "pricing": {}}
    written = {}

    if not persist:
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
        patch("mercury_cli.models.get_pricing_for_provider",
              side_effect=lambda provider, **kwargs: {"provider": provider})
    )

    def fake_pick(model_ids, current_model="", pricing=None, title="", **kwargs):
        seen["models"].append((title, list(model_ids), current_model))
        seen["pricing"][title] = pricing
        answer = model_answers.get(title)
        return answer.pop(0) if isinstance(answer, list) else answer

    stack.enter_context(
        patch("mercury_cli.auth._prompt_model_selection", side_effect=fake_pick)
    )

    def fake_provider_choice(choices, default=0, title="Select provider:"):
        call = {"choices": list(choices), "default": default, "title": title}
        seen["providers"].append(call)
        if title == DELEGATE_PROVIDER:
            seen["provider"] = call
        if provider_choices and title in provider_choices:
            return provider_choices[title]
        return provider_choice if title == DELEGATE_PROVIDER else None

    stack.enter_context(
        patch("mercury_cli.main._prompt_provider_choice", side_effect=fake_provider_choice)
    )
    if not persist:
        stack.enter_context(
            patch("mercury_cli.omp_sync._write_slots",
                  side_effect=lambda update: written.update(update) or True)
        )
    stack.enter_context(patch.object(setup_mod, "_prompt_slot_reasoning"))
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


class TestFallbackProviders:
    @pytest.mark.parametrize("provider_title,model_title,slot", [
        (FALLBACK_PROVIDER, FALLBACK_TITLE, "fallback"),
        (DELEGATE_FB_PROVIDER, DELEGATE_FB_TITLE, "delegate_fallback"),
    ])
    def test_each_fallback_uses_selected_catalog_pricing_and_prefix(
        self, slots_env, provider_title, model_title, slot,
    ):
        answers = {
            FALLBACK_TITLE: "main-fallback", SECOND_TITLE: "main-second",
            DELEGATE_TITLE: "delegate", DELEGATE_FB_TITLE: "delegate-fallback",
            DELEGATE_2ND_TITLE: "delegate-second",
        }
        # The same bare id is valid on another provider; duplicates are
        # determined by the complete provider/model selector.
        answers[model_title] = "somemodel" if slot == "fallback" else "delegate"
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack, model_answers=answers, provider_choice=None,
                provider_choices={provider_title: _zai_index()},
                catalogs={"openrouter": OR_CATALOG, "zai": ZAI_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})
        assert _catalog_for(seen, model_title) == ZAI_CATALOG
        assert seen["pricing"][model_title] == {"provider": "zai"}
        value = written[slot][-1] if slot.endswith("chain") else written[slot]
        assert value == "zai/" + answers[model_title]

    @pytest.mark.parametrize("previously_configured", [False, True])
    def test_skip_primary_omits_secondary_provider_and_model_prompts(
        self, slots_env, previously_configured,
    ):
        if previously_configured:
            import yaml

            (slots_env / "config.yaml").write_text(yaml.safe_dump({"models": {
                "default": "openrouter/somemodel", "fallback": "zai/old-main",
                "fallback_chain": ["zai/old-main", "openrouter/old-second"],
                "delegate_model": "openrouter/delegate",
                "delegate_fallback": "zai/old-delegate",
                "delegate_fallback_chain": ["zai/old-delegate", "openrouter/old-other"],
            }}))
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack, model_answers={DELEGATE_TITLE: "delegate"},
                provider_choice=None, catalogs={"openrouter": OR_CATALOG},
            )
            stack.enter_context(patch.object(setup_mod, "_ask_reconfigure", return_value=True))
            setup_mod._prompt_mercury_slots({})
        assert [p["title"] for p in seen["providers"]] == [
            FALLBACK_PROVIDER, DELEGATE_PROVIDER, DELEGATE_FB_PROVIDER,
        ]
        assert SECOND_TITLE not in [m[0] for m in seen["models"]]
        assert DELEGATE_2ND_TITLE not in [m[0] for m in seen["models"]]
        assert written["fallback"] == written["delegate_fallback"] == ""
        assert written["fallback_chain"] == written["delegate_fallback_chain"] == []

    def test_duplicate_retry_retains_chosen_provider(self, slots_env):
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack, model_answers={
                    FALLBACK_TITLE: ["somemodel", "different"],
                    SECOND_TITLE: ["different", "different"],
                    DELEGATE_TITLE: "delegate",
                    DELEGATE_FB_TITLE: ["delegate", "delegate"],
                },
                provider_choice=None, catalogs={"openrouter": OR_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})
        assert written["fallback"] == "openrouter/different"
        assert written["fallback_chain"] == []
        assert written["delegate_fallback"] == ""
        assert [p["title"] for p in seen["providers"]].count(FALLBACK_PROVIDER) == 1
        assert [p["title"] for p in seen["providers"]].count(SECOND_PROVIDER) == 0

    def test_saved_provider_defaults_and_custom_provider_cancel(self, slots_env):
        import yaml

        saved = {
            "default": "openrouter/somemodel", "fallback": "zai/main-fallback",
            "fallback_chain": ["zai/main-fallback", "my-provider/second"],
            "delegate_model": "openrouter/delegate",
            "delegate_fallback": "zai/delegate-fallback",
            "delegate_fallback_chain": ["zai/delegate-fallback", "my-provider/other"],
        }
        (slots_env / "config.yaml").write_text(yaml.safe_dump({"models": saved}))
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack, model_answers={
                    FALLBACK_TITLE: "main-fallback", SECOND_TITLE: "second",
                    DELEGATE_TITLE: "delegate", DELEGATE_FB_TITLE: "delegate-fallback",
                    DELEGATE_2ND_TITLE: "other",
                },
                provider_choice=None, catalogs={},
            )
            stack.enter_context(patch.object(setup_mod, "_ask_reconfigure", return_value=True))
            setup_mod._prompt_mercury_slots({})
        assert all(written[k] == v for k, v in saved.items())
        assert [p["default"] for p in seen["providers"]] == [
            _zai_index(), _openrouter_index(), _zai_index(),
        ]
        assert [title for title, _, _ in seen["models"]] == [FALLBACK_TITLE, DELEGATE_TITLE, DELEGATE_FB_TITLE]

    def test_switching_provider_clears_previous_model_in_picker(self, slots_env):
        (slots_env / "config.yaml").write_text(
            "models:\n  default: openrouter/somemodel\n  fallback: openrouter/old\n"
        )
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack, model_answers={FALLBACK_TITLE: "custom-new"},
                provider_choice=None, provider_choices={FALLBACK_PROVIDER: _zai_index()},
                catalogs={"openrouter": OR_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})
        assert (FALLBACK_TITLE, [], "") in seen["models"]
        assert written["fallback"] == "zai/custom-new"

    def test_existing_chains_rebase_and_remove_collisions(self, slots_env):
        import yaml

        (slots_env / "config.yaml").write_text(yaml.safe_dump({"models": {
            "default": "openrouter/somemodel", "fallback": "openrouter/old-main",
            "fallback_chain": ["openrouter/old-main", "openrouter/new-main", "openrouter/somemodel", "zai/extra", "zai/extra"],
            "delegate_model": "openrouter/old-delegate", "delegate_fallback": "openrouter/old-retry",
            "delegate_fallback_chain": ["openrouter/old-retry", "openrouter/new-delegate", "openrouter/new-retry", "zai/extra"],
        }}))
        with ExitStack() as stack:
            seen, written = _enter_slots_patches(
                stack, model_answers={
                    FALLBACK_TITLE: "new-main", DELEGATE_TITLE: "new-delegate", DELEGATE_FB_TITLE: "new-retry",
                }, provider_choice=None, catalogs={"openrouter": OR_CATALOG},
            )
            setup_mod._prompt_mercury_slots({})
        assert len(seen["models"]) == 3
        assert written["fallback_chain"] == ["openrouter/new-main", "zai/extra"]
        assert written["delegate_fallback_chain"] == ["openrouter/new-retry", "zai/extra"]

    def test_real_saved_chains_reach_both_engines(self, slots_env, monkeypatch):
        import importlib.util
        from pathlib import Path
        import yaml
        from mercury_cli.config import load_config, save_config

        config_path = slots_env / "config.yaml"
        config_path.write_text(yaml.safe_dump({
            "models": {
                "default": "openrouter/somemodel", "fallback": "zai/old-main",
                "fallback_chain": ["zai/old-main", "openrouter/vendor/second"],
                "delegate_model": "zai/old-delegate", "delegate_fallback": "openrouter/old-fallback",
                "delegate_fallback_chain": ["openrouter/old-fallback", "zai/delegate-second"],
                "reasoning_overrides": {"openrouter/vendor/second": "low", "zai/delegate-second": "high"},
            },
            "hermes": {"fallback_providers": [{"provider": "openrouter", "model": "stale"}]},
        }))
        monkeypatch.setenv("HERMES_OMP_CONFIG", str(config_path))
        config = load_config()
        with ExitStack() as stack:
            _enter_slots_patches(
                stack, model_answers={
                    FALLBACK_TITLE: "main-fallback", SECOND_TITLE: "vendor/second",
                    DELEGATE_TITLE: "delegate", DELEGATE_FB_TITLE: "vendor/fallback",
                    DELEGATE_2ND_TITLE: "delegate-second",
                },
                provider_choice=_zai_index(), provider_choices={
                    FALLBACK_PROVIDER: _zai_index(), SECOND_PROVIDER: _openrouter_index(),
                    DELEGATE_FB_PROVIDER: _openrouter_index(), DELEGATE_2ND_PROVIDER: _zai_index(),
                },
                catalogs={"openrouter": OR_CATALOG, "zai": ZAI_CATALOG}, persist=True,
            )
            setup_mod._prompt_mercury_slots(config)
        save_config(config)

        saved = yaml.safe_load(config_path.read_text())
        assert saved["models"]["fallback_chain"] == ["zai/main-fallback", "openrouter/vendor/second"]
        assert saved["models"]["delegate_fallback_chain"] == ["openrouter/vendor/fallback", "zai/delegate-second"]
        assert saved["models"]["reasoning_overrides"] == {"openrouter/vendor/second": "low", "zai/delegate-second": "high"}
        assert all(entry["model"] != "stale" for entry in saved["hermes"].get("fallback_providers", []))
        assert load_config()["fallback_providers"] == [
            {"provider": "zai", "model": "main-fallback"},
            {"provider": "openrouter", "model": "vendor/second"},
        ]

        bridge_path = Path(__file__).resolve().parents[3] / "bridge" / "bridge.py"
        spec = importlib.util.spec_from_file_location("setup_slots_test_bridge", bridge_path)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        slots = bridge.parse_config(str(config_path))
        assert bridge.validate(slots, need_delegate=True) == []
        bridge.render_omp_subtree(slots, target=str(config_path))
        rendered = yaml.safe_load(config_path.read_text())
        assert rendered["omp"]["retry"]["fallbackChains"] == {
            "zai/delegate": ["openrouter/vendor/fallback", "zai/delegate-second:high"],
        }
        assert rendered["models"] == saved["models"]


# ---------------------------------------------------------------------------
# Slice B — wizard-once + tools single-pass
# ---------------------------------------------------------------------------


def _wizard_args(**overrides):
    return Namespace(
        section=overrides.get("section", None),
        reset=overrides.get("reset", False),
        reconfigure=overrides.get("reconfigure", False),
        quick=overrides.get("quick", False),
        portal=overrides.get("portal", False),
        non_interactive=overrides.get("non_interactive", False),
    )


def _enter_wizard_patches(stack, **extra):
    """Standard full-wizard mocks (existing install). Returns named mocks."""
    for target, kwargs in [
        ("mercury_cli.setup.ensure_hermes_home", {}),
        ("mercury_cli.setup.is_interactive_stdin", {"return_value": True}),
        ("mercury_cli.config.is_managed", {"return_value": False}),
        ("mercury_cli.setup.load_config", {"return_value": {}}),
        ("mercury_cli.setup.save_config", {}),
        ("mercury_cli.setup.get_env_value", {"return_value": None}),
        ("mercury_cli.auth.get_active_provider", {"return_value": "openrouter"}),
        ("mercury_cli.setup._print_setup_summary", {}),
        ("mercury_cli.setup._offer_openclaw_migration", {"return_value": False}),
    ]:
        stack.enter_context(patch(target, **kwargs))
    named = {}
    for name, target in extra.items():
        if isinstance(target, tuple):
            target, kwargs = target
            named[name] = stack.enter_context(patch(target, **kwargs))
        else:
            named[name] = stack.enter_context(patch(target))
    return named


class TestWizardSectionsRunOnce:
    """Full wizard run invokes each section fn exactly once (mock sections)."""

    def test_existing_install_full_wizard(self, tmp_path, monkeypatch):
        """Bare `mercury setup` on an existing install: each section once."""
        home = tmp_path / ".mercury"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        with ExitStack() as stack:
            m = _enter_wizard_patches(
                stack,
                model="mercury_cli.setup.setup_model_provider",
                terminal="mercury_cli.setup.setup_terminal_backend",
                gateway="mercury_cli.setup.setup_gateway",
                tools="mercury_cli.setup.setup_tools",
            )
            setup_mod.run_setup_wizard(_wizard_args())
        m["model"].assert_called_once()
        m["terminal"].assert_called_once()
        m["gateway"].assert_called_once()
        m["tools"].assert_called_once()
        # Standalone dispatch + linear flow preserved: the wizard step still
        # routes through setup_tools with the first-install linear flow.
        assert m["tools"].call_args.kwargs.get("first_install") is True

    def test_fresh_install_full_setup(self, tmp_path, monkeypatch):
        """Fresh install + Full setup choice: each section once, no quick/blank."""
        home = tmp_path / ".mercury"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        with ExitStack() as stack:
            m = _enter_wizard_patches(
                stack,
                prompt=("mercury_cli.setup.prompt_choice", {"return_value": 1}),
                model="mercury_cli.setup.setup_model_provider",
                terminal="mercury_cli.setup.setup_terminal_backend",
                gateway="mercury_cli.setup.setup_gateway",
                tools="mercury_cli.setup.setup_tools",
                first="mercury_cli.setup._run_first_time_quick_setup",
                blank="mercury_cli.setup._run_blank_slate_setup",
                quick="mercury_cli.setup._run_quick_setup",
                defaults="mercury_cli.setup._apply_default_agent_settings",
            )
            # Fresh install: no active provider.
            with patch("mercury_cli.auth.get_active_provider", return_value=None):
                setup_mod.run_setup_wizard(_wizard_args())
        m["model"].assert_called_once()
        m["terminal"].assert_called_once()
        m["gateway"].assert_called_once()
        m["tools"].assert_called_once()
        m["first"].assert_not_called()
        m["blank"].assert_not_called()
        m["quick"].assert_not_called()


class TestWizardToolsSinglePass:
    """The wizard's tools step runs ONE linear checklist pass (double fix)."""

    def test_tools_step_single_checklist_with_messenger_enabled(
        self, tmp_path, monkeypatch
    ):
        """With a messenger token set, the tools flow must not repeat per platform."""
        import mercury_cli.tools_config as tools_config_mod

        home = tmp_path / ".mercury"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        # Deterministic two-platform scenario on any machine: only telegram
        # enabled (neutralizes live TELEGRAM/DISCORD/SLACK/... env leakage).
        for var in (
            "TELEGRAM_BOT_TOKEN",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "WHATSAPP_ENABLED",
            "QQ_APP_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-token-for-test")

        checklist_calls = []

        def fake_checklist(label, enabled, platform="cli", **kwargs):
            checklist_calls.append(platform)
            return set()

        with ExitStack() as stack:
            m = _enter_wizard_patches(
                stack,
                model="mercury_cli.setup.setup_model_provider",
                terminal="mercury_cli.setup.setup_terminal_backend",
                gateway="mercury_cli.setup.setup_gateway",
            )
            # REAL tools step (the reported double lives inside it).
            stack.enter_context(
                patch.object(
                    tools_config_mod, "_prompt_toolset_checklist",
                    side_effect=fake_checklist,
                )
            )
            stack.enter_context(
                patch.object(
                    tools_config_mod, "apply_nous_managed_defaults",
                    return_value=set(),
                )
            )
            stack.enter_context(patch.object(tools_config_mod, "save_config"))
            setup_mod.run_setup_wizard(_wizard_args())

        m["gateway"].assert_called_once()
        # One linear pass — not once per enabled platform (cli + telegram).
        assert checklist_calls == ["cli"]
