"""Tests for IRC gateway configuration via `mercury setup gateway` UI.

Covers the full plugin-platform discovery → status → configure flow so that
a fresh Mercury install (no state, no env vars) can set up IRC through the
interactive setup menus.
"""

import os

from gateway.platform_registry import PlatformEntry, platform_registry


def _register_irc_platform(**overrides):
    """Manually register the IRC platform entry as if discover_plugins() found it.

    Tests run outside the normal plugin-discovery path, so we inject the entry
    directly into the singleton registry and yield its dict shape.
    """
    defaults = dict(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: bool(os.getenv("IRC_SERVER", "") and os.getenv("IRC_CHANNEL", "")),
        validate_config=None,
        required_env=["IRC_SERVER", "IRC_CHANNEL", "IRC_NICKNAME"],
        install_hint="No extra packages needed (stdlib only)",
        setup_fn=lambda: None,
        source="plugin",
        plugin_name="irc_platform",
        allowed_users_env="IRC_ALLOWED_USERS",
        allow_all_env="IRC_ALLOW_ALL_USERS",
        max_message_length=450,
        pii_safe=False,
        emoji="💬",
        allow_update_command=True,
        platform_hint="You are chatting via IRC.",
    )
    defaults.update(overrides)
    entry = PlatformEntry(**defaults)
    platform_registry.register(entry)
    return {
        "key": entry.name,
        "label": entry.label,
        "emoji": entry.emoji,
        "token_var": entry.required_env[0] if entry.required_env else "",
        "install_hint": entry.install_hint,
        "_registry_entry": entry,
    }


def _unregister_irc_platform():
    platform_registry.unregister("irc")


# ── Fresh-install discovery ─────────────────────────────────────────────────


class TestIRCFreshInstallDiscovery:
    """IRC stays out of the gateway setup menu (observatory-owned)."""

    def test_irc_hidden_from_all_platforms(self, monkeypatch):
        """The registry still carries IRC (gateway runtime needs it),
        but _all_platforms() never surfaces it for setup."""
        import mercury_cli.gateway as gateway_mod
        from gateway.platform_registry import platform_registry

        _register_irc_platform()
        try:
            # Ensure no stale env vars leak in
            for key in ("IRC_SERVER", "IRC_CHANNEL", "IRC_NICKNAME"):
                monkeypatch.delenv(key, raising=False)

            assert platform_registry.get("irc") is not None
            platforms = gateway_mod._all_platforms()
            keys = {p["key"] for p in platforms}
            assert "irc" not in keys
        finally:
            _unregister_irc_platform()


    def test_irc_status_configured_when_env_set(self, monkeypatch):
        """After the user sets IRC_SERVER and IRC_CHANNEL, status is 'configured'."""
        import mercury_cli.gateway as gateway_mod

        plat = _register_irc_platform()
        try:
            monkeypatch.setenv("IRC_SERVER", "irc.libera.chat")
            monkeypatch.setenv("IRC_CHANNEL", "#mercury")
            monkeypatch.setenv("IRC_NICKNAME", "mercury-bot")

            status = gateway_mod._platform_status(plat)
            assert status == "configured"
        finally:
            _unregister_irc_platform()


# ── Interactive setup dispatch ──────────────────────────────────────────────


class TestIRCInteractiveSetup:
    """The setup UI dispatches to IRC's interactive_setup() correctly."""

    def test_configure_platform_dispatches_to_irc_setup_fn(self, monkeypatch, capsys):
        """_configure_platform() calls the IRC plugin's setup_fn when selected."""
        import mercury_cli.gateway as gateway_mod

        calls = []

        def fake_setup():
            calls.append("setup_called")
            print("IRC setup complete!")

        plat = _register_irc_platform(setup_fn=fake_setup)
        try:
            gateway_mod._configure_platform(plat)
        finally:
            _unregister_irc_platform()

        assert "setup_called" in calls
        out = capsys.readouterr().out
        assert "IRC setup complete!" in out


    def test_configure_platform_fallback_when_no_setup_fn(self, monkeypatch, capsys):
        """A plugin with no setup_fn falls back to env-var instructions."""
        import mercury_cli.gateway as gateway_mod

        plat = _register_irc_platform(setup_fn=None)
        try:
            gateway_mod._configure_platform(plat)
        finally:
            _unregister_irc_platform()

        out = capsys.readouterr().out
        assert "IRC" in out
        assert "IRC_SERVER" in out


# ── End-to-end fresh-install gateway setup ──────────────────────────────────


class TestIRCGatewaySetupFreshInstall:
    """Simulate the full `mercury setup gateway` experience with IRC present."""

    def test_setup_gateway_hides_irc_from_platform_menu(self, monkeypatch, capsys, tmp_path):
        """The gateway setup menu never lists IRC (observatory-owned)."""
        import mercury_cli.gateway as gateway_mod
        from mercury_cli import setup as setup_mod

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _register_irc_platform()
        try:
            for key in ("IRC_SERVER", "IRC_CHANNEL", "IRC_NICKNAME"):
                monkeypatch.delenv(key, raising=False)

            checklist_calls = []

            def capture_prompt_checklist(question, choices, pre_selected=None):
                checklist_calls.append({"question": question, "choices": choices})
                return []  # nothing selected → clean exit

            monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **kw: False)
            monkeypatch.setattr(setup_mod, "prompt_checklist", capture_prompt_checklist)
            monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
            monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
            monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
            monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

            setup_mod.setup_gateway({})

            # Find the platform-selection prompt
            platform_prompt = next(
                (c for c in checklist_calls if "platform" in c["question"].lower()),
                None,
            )
            assert platform_prompt is not None, \
                f"No platform prompt found in {checklist_calls}"
            choices_text = "\n".join(platform_prompt["choices"])
            assert "IRC" not in choices_text
        finally:
            _unregister_irc_platform()

    def test_setup_gateway_completes_without_irc_row(self, monkeypatch, capsys, tmp_path):
        """Gateway setup completes cleanly with no IRC row to select."""
        import mercury_cli.gateway as gateway_mod
        from mercury_cli import setup as setup_mod

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _register_irc_platform()
        try:
            monkeypatch.setenv("IRC_SERVER", "irc.libera.chat")
            monkeypatch.setenv("IRC_CHANNEL", "#mercury")
            monkeypatch.setenv("IRC_NICKNAME", "mercury-bot")

            monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *a, **kw: False)
            monkeypatch.setattr(setup_mod, "prompt_choice", lambda *a, **kw: 0)
            monkeypatch.setattr(
                setup_mod,
                "prompt_checklist",
                lambda title, items, pre=None: [],
            )
            monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
            monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
            monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
            monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

            setup_mod.setup_gateway({})

            out = capsys.readouterr().out
            assert "Traceback" not in out
        finally:
            _unregister_irc_platform()
