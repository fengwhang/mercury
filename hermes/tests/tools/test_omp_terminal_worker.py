"""The OMP transport retains the selected backend and its own approval gate."""
def test_setup_ssh_persists_shared_endpoint_and_can_restore_default_port(monkeypatch):
    from mercury_cli import setup
    saved = {}
    config = {}
    answers = iter(["box.example", "worker", "22", ""])
    monkeypatch.setattr(setup, "prompt_choice", lambda question, choices, *args, **kwargs:
                        next(i for i, choice in enumerate(choices) if choice.startswith("SSH -")))
    monkeypatch.setattr(setup, "prompt", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(setup, "prompt_yes_no", lambda *args, **kwargs: False)
    monkeypatch.setattr(setup, "get_env_value", lambda key: "2200" if key == "TERMINAL_SSH_PORT" else "")
    monkeypatch.setattr(setup, "save_env_value", lambda key, value: saved.update({key: value}))
    monkeypatch.setattr(setup, "save_config", lambda value: None)
    setup.setup_terminal_backend(config)
    assert config["terminal"] == {"backend": "ssh", "ssh_host": "box.example", "ssh_user": "worker",
                                  "ssh_port": 22, "ssh_key": ""}
    assert saved["TERMINAL_SSH_PORT"] == "22"
    assert saved["TERMINAL_SSH_KEY"] == ""


def test_environment_explicit_zero_disables_deadline_but_default_still_applies(tmp_path):
    from tools.environments.local import LocalEnvironment
    environment = LocalEnvironment(cwd=str(tmp_path), timeout=0.03)
    try:
        result = environment.execute("sleep 0.1; echo completed", timeout=0)
        assert result["returncode"] == 0 and "completed" in result["output"]
        result = environment.execute("sleep 0.5; echo should-time-out")
        assert result["returncode"] == 124
        assert "should-time-out" not in result["output"]
    finally:
        environment.cleanup()
