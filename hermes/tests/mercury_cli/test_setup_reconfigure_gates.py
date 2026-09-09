"""Reconfigure gates: each already-configured setup section asks first.

Covers ``mercury setup`` re-runs for the four gated sections
(model+provider, delegate slots, tools, observatory):

- configured + "no"  → section skipped (nothing runs, nothing written)
- configured + "yes" → section runs normally
- unconfigured       → no gate, straight into prompts
- headless           → no gate prompt, section runs unchanged
- ``_SetupGoBack``   → propagates through the gate (left-arrow survives)
"""

from unittest.mock import patch

import pytest

import mercury_cli.setup as setup_mod
from mercury_cli.models import CANONICAL_PROVIDERS


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _interactive(monkeypatch, answers):
    """Simulate an interactive terminal; prompt_yes_no consumes answers.

    Returns the list of (question, default) the gate asked.
    """
    monkeypatch.setattr(setup_mod, "is_interactive_stdin", lambda: True)
    monkeypatch.delenv("HERMES_NONINTERACTIVE", raising=False)
    asked = []
    remaining = list(answers)

    def fake_yes_no(question, default=True):
        asked.append((question, default))
        return remaining.pop(0)

    monkeypatch.setattr(setup_mod, "prompt_yes_no", fake_yes_no)
    return asked


def _headless(monkeypatch):
    """Simulate a headless spawn (env flag wins even on a tty)."""
    monkeypatch.setattr(setup_mod, "is_interactive_stdin", lambda: True)
    monkeypatch.setenv("HERMES_NONINTERACTIVE", "1")
    asked = []
    monkeypatch.setattr(
        setup_mod, "prompt_yes_no", lambda q, default=True: asked.append((q, default))
    )
    return asked


def _gate_questions(asked):
    return [q for q, _ in asked if "Reconfigure" in q]


def _openrouter_index():
    return [p.slug for p in CANONICAL_PROVIDERS].index("openrouter")


# ---------------------------------------------------------------------------
# model + provider (setup_model_provider)
# ---------------------------------------------------------------------------

class TestModelProviderGate:
    def _patches(self, stack, *, configured):
        stack.enter_context(
            patch(
                "mercury_cli.auth.get_active_provider",
                return_value="openrouter" if configured else None,
            )
        )
        stack.enter_context(
            patch.object(
                setup_mod, "get_env_value", return_value=None if configured else ""
            )
        )
        stack.enter_context(patch("mercury_cli.config.load_config", return_value={}))
        stack.enter_context(patch("mercury_cli.config.save_config"))
        select = stack.enter_context(
            patch("mercury_cli.main.select_provider_and_model")
        )
        slots = stack.enter_context(patch.object(setup_mod, "_prompt_mercury_slots"))
        return select, slots

    def test_configured_no_skips_provider_flow_but_offers_slots(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [False])
        with ExitStack() as stack:
            select, slots = self._patches(stack, configured=True)
            setup_mod.setup_model_provider(
                {"model": {"provider": "openrouter", "default": "x"}}
            )
        assert _gate_questions(asked) == ["  Reconfigure model & provider?"]
        assert asked[0][1] is False  # default NO is load-bearing
        select.assert_not_called()
        slots.assert_called_once()  # delegate slots keep their own gate

    def test_configured_yes_runs_provider_flow(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [True])
        with ExitStack() as stack:
            select, slots = self._patches(stack, configured=True)
            setup_mod.setup_model_provider(
                {"model": {"provider": "openrouter", "default": "x"}}
            )
        assert _gate_questions(asked) == ["  Reconfigure model & provider?"]
        select.assert_called_once()
        slots.assert_called_once()

    def test_unconfigured_runs_without_gate(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [])
        with ExitStack() as stack:
            select, slots = self._patches(stack, configured=False)
            setup_mod.setup_model_provider({})
        assert _gate_questions(asked) == []
        select.assert_called_once()
        slots.assert_called_once()

    def test_headless_runs_without_prompt(self, monkeypatch):
        from contextlib import ExitStack

        asked = _headless(monkeypatch)
        with ExitStack() as stack:
            select, slots = self._patches(stack, configured=True)
            setup_mod.setup_model_provider(
                {"model": {"provider": "openrouter", "default": "x"}}
            )
        assert asked == []
        select.assert_called_once()
        slots.assert_called_once()


# ---------------------------------------------------------------------------
# delegate model + fallback + provider (_prompt_mercury_slots)
# ---------------------------------------------------------------------------

_CONFIGURED_SLOTS = {
    "default": "openrouter/m",
    "fallback": "openrouter/f",
    "delegate_model": "openrouter/dm",
    "delegate_fallback": "",
}


class TestDelegateSlotsGate:
    def _patches(self, stack, *, slots):
        stack.enter_context(
            patch.object(setup_mod, "_read_model_slots", return_value=dict(slots))
        )
        stack.enter_context(
            patch.object(
                setup_mod,
                "load_config",
                return_value={"model": {"provider": "openrouter", "default": "m"}},
            )
        )
        picker = stack.enter_context(
            patch("mercury_cli.auth._prompt_model_selection", return_value=None)
        )
        stack.enter_context(
            patch(
                "mercury_cli.models.provider_model_ids",
                return_value=["or-m1", "or-m2"],
            )
        )
        stack.enter_context(
            patch("mercury_cli.models.get_pricing_for_provider", return_value={})
        )
        stack.enter_context(
            patch(
                "mercury_cli.main._prompt_provider_choice",
                return_value=_openrouter_index(),
            )
        )
        written = stack.enter_context(patch("mercury_cli.omp_sync._write_slots"))
        return picker, written

    def test_configured_no_skips_without_writing(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [False])
        with ExitStack() as stack:
            picker, written = self._patches(stack, slots=_CONFIGURED_SLOTS)
            setup_mod._prompt_mercury_slots({})
        assert _gate_questions(asked) == ["  Reconfigure delegate models?"]
        assert asked[0][1] is False
        picker.assert_not_called()
        written.assert_not_called()

    def test_configured_yes_runs_pickers_and_writes(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [True])
        with ExitStack() as stack:
            picker, written = self._patches(stack, slots=_CONFIGURED_SLOTS)
            setup_mod._prompt_mercury_slots({})
        assert _gate_questions(asked) == ["  Reconfigure delegate models?"]
        assert picker.call_count >= 1
        written.assert_called_once()

    def test_unconfigured_slots_run_without_gate(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [])
        slots = {
            "default": "openrouter/m",
            "fallback": "",
            "delegate_model": "",
            "delegate_fallback": "",
        }
        with ExitStack() as stack:
            picker, written = self._patches(stack, slots=slots)
            setup_mod._prompt_mercury_slots({})
        assert _gate_questions(asked) == []
        assert picker.call_count >= 1
        written.assert_called_once()

    def test_headless_runs_without_prompt(self, monkeypatch):
        from contextlib import ExitStack

        asked = _headless(monkeypatch)
        with ExitStack() as stack:
            picker, written = self._patches(stack, slots=_CONFIGURED_SLOTS)
            setup_mod._prompt_mercury_slots({})
        assert asked == []
        assert picker.call_count >= 1
        written.assert_called_once()

    def test_go_back_propagates_through_gate(self, monkeypatch):
        monkeypatch.setattr(setup_mod, "is_interactive_stdin", lambda: True)
        monkeypatch.delenv("HERMES_NONINTERACTIVE", raising=False)
        monkeypatch.setattr(
            setup_mod,
            "_read_model_slots",
            lambda: dict(_CONFIGURED_SLOTS),
        )

        def raise_back(question, default=False):
            raise setup_mod._SetupGoBack(0)

        monkeypatch.setattr(setup_mod, "prompt_yes_no", raise_back)
        with pytest.raises(setup_mod._SetupGoBack):
            setup_mod._prompt_mercury_slots({})


# ---------------------------------------------------------------------------
# tools (setup_tools)
# ---------------------------------------------------------------------------

class TestToolsGate:
    def _patches(self, stack, *, configured):
        def env_side(key):
            if configured and key == "ELEVENLABS_API_KEY":
                return "x"
            return ""

        stack.enter_context(
            patch.object(setup_mod, "get_env_value", side_effect=env_side)
        )
        return stack.enter_context(patch("mercury_cli.tools_config.tools_command"))

    def test_configured_no_skips(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [False])
        with ExitStack() as stack:
            tools = self._patches(stack, configured=True)
            setup_mod.setup_tools({}, first_install=True)
        assert _gate_questions(asked) == ["  Reconfigure tools?"]
        assert asked[0][1] is False
        tools.assert_not_called()

    def test_configured_yes_runs(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [True])
        with ExitStack() as stack:
            tools = self._patches(stack, configured=True)
            setup_mod.setup_tools({}, first_install=True)
        assert _gate_questions(asked) == ["  Reconfigure tools?"]
        tools.assert_called_once()

    def test_unconfigured_runs_without_gate(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [])
        with ExitStack() as stack:
            tools = self._patches(stack, configured=False)
            setup_mod.setup_tools({}, first_install=True)
        assert _gate_questions(asked) == []
        tools.assert_called_once()

    def test_headless_runs_without_prompt(self, monkeypatch):
        from contextlib import ExitStack

        asked = _headless(monkeypatch)
        with ExitStack() as stack:
            tools = self._patches(stack, configured=True)
            setup_mod.setup_tools({}, first_install=True)
        assert asked == []
        tools.assert_called_once()


# ---------------------------------------------------------------------------
# observatory (setup_observatory)
# ---------------------------------------------------------------------------

def _obs_status(*, provisioned):
    return {
        "provisioned": provisioned,
        "homeserver_reachable": True,
        "homeserver_url": "http://localhost:8008",
        "unit_active": True,
        "unit_name": "mercury-observatory",
        "enabled": True,
    }


class _FakeObs:
    def __init__(self, status):
        self._status = status
        self.provision_calls = []

    def status_summary(self):
        return dict(self._status)

    def provision_in_wizard(self, **kwargs):
        self.provision_calls.append(kwargs)


class TestObservatoryGate:
    def _patches(self, stack, *, fake, choice=1):
        stack.enter_context(
            patch.object(
                setup_mod, "_load_observatory_provision", return_value=fake
            )
        )
        choose = stack.enter_context(
            patch.object(setup_mod, "prompt_choice", return_value=choice)
        )
        stack.enter_context(patch.object(setup_mod, "_prompt_observatory_enabled_toggle"))
        stack.enter_context(patch.object(setup_mod, "_print_observatory_setup_card"))
        stack.enter_context(patch.object(setup_mod, "_offer_tailscale_bind"))
        stack.enter_context(patch.object(setup_mod, "_maybe_print_bind_mismatch_action"))
        stack.enter_context(
            patch.object(setup_mod, "_tailscale_status", return_value={})
        )
        stack.enter_context(patch.object(setup_mod, "_offer_owner_password_rotate"))
        stack.enter_context(patch.object(setup_mod, "_run_observatory_auto_steps"))
        return choose

    def test_provisioned_no_skips_section(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [False])
        fake = _FakeObs(_obs_status(provisioned=True))
        with ExitStack() as stack:
            choose = self._patches(stack, fake=fake)
            setup_mod.setup_observatory({})
        assert _gate_questions(asked) == ["  Reconfigure observatory?"]
        assert asked[0][1] is False
        choose.assert_not_called()
        assert fake.provision_calls == []

    def test_provisioned_yes_proceeds_to_choice(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [True])
        fake = _FakeObs(_obs_status(provisioned=True))
        with ExitStack() as stack:
            choose = self._patches(stack, fake=fake)
            setup_mod.setup_observatory({})
        assert _gate_questions(asked) == ["  Reconfigure observatory?"]
        choose.assert_called_once()
        assert fake.provision_calls == []  # choice=1 skips install

    def test_unprovisioned_runs_without_gate(self, monkeypatch):
        from contextlib import ExitStack

        asked = _interactive(monkeypatch, [])
        fake = _FakeObs(_obs_status(provisioned=False))
        with ExitStack() as stack:
            choose = self._patches(stack, fake=fake)
            setup_mod.setup_observatory({})
        assert _gate_questions(asked) == []
        choose.assert_called_once()
        assert fake.provision_calls == []

    def test_headless_runs_without_prompt(self, monkeypatch):
        from contextlib import ExitStack

        asked = _headless(monkeypatch)
        fake = _FakeObs(_obs_status(provisioned=True))
        with ExitStack() as stack:
            choose = self._patches(stack, fake=fake)
            setup_mod.setup_observatory({})
        assert asked == []
        choose.assert_called_once()
