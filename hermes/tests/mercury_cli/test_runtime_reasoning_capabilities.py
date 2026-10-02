"""API discovery -> setup/runtime request tests, with an isolated HTTP catalog."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from mercury_cli import codex_models, models, setup
from agent.transports.codex import ResponsesApiTransport


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(codex_models, "_reasoning_catalogs", {})
    monkeypatch.setattr(codex_models, "_reasoning_attempts", {})
    state = {"requests": [], "fail": False, "rows": [
        {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [
            {"effort": "low"}, {"effort": "high"}], "default_reasoning_level": "high"},
        {"slug": "gpt-5.6-sol", "supported_reasoning_levels": ["low", "high"]},
        {"slug": "gpt-5.3-codex", "supported_reasoning_levels": ["none", "minimal", "max"],
         "default_reasoning_level": "minimal"},
        {"slug": "disabled-model", "supported_reasoning_levels": ["none"], "default_reasoning_level": "none"},
        {"slug": "legacy-model"},
        {"slug": "hidden-model", "visibility": "hidden", "supported_reasoning_levels": ["high"]},
    ]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers.get("Authorization")))
            if state["fail"]:
                self.send_response(503)
                self.end_headers()
                return
            rows = state["rows"]
            if self.headers.get("Authorization") == "Bearer another-account":
                rows = [{"slug": "gpt-6.1-sol", "supported_reasoning_levels": ["medium"],
                         "default_reasoning_level": "medium"}]
            body = json.dumps({"models": rows, "data": [{
                "id": "unseen/reasoner", "supported_parameters": ["tools", "reasoning"],
                "reasoning": {"supported_efforts": ["low", "high"]},
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}/codex"
    monkeypatch.setattr("mercury_cli.auth.resolve_codex_runtime_credentials", lambda **_kw: {
        "api_key": "test-account", "base_url": state["base"],
    })
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(catalog, model, config, *, token="test-account"):
    return ResponsesApiTransport().build_kwargs(
        model=model, messages=[{"role": "user", "content": "hello"}], tools=[],
        reasoning_config=config, is_codex_backend=True, provider="openai-codex",
        base_url=catalog["base"], codex_access_token=token,
    )


def test_setup_reads_the_same_account_catalog_as_runtime(catalog, monkeypatch):
    selected = {}
    monkeypatch.setattr(setup, "is_noninteractive", lambda: False)
    monkeypatch.setattr(setup, "is_interactive_stdin", lambda: True)

    def choose(_title, choices, default):
        selected.update(choices=choices, default=default)
        return 0

    monkeypatch.setattr(setup, "_curses_prompt_choice", choose)
    ids = codex_models.get_codex_model_ids("test-account", catalog["base"])
    assert "gpt-6.1-sol" in ids
    assert "hidden-model" not in ids
    value = setup._pick_reasoning_level("Reasoning", model="openai-codex/gpt-6.1-sol", allow_auto=True)
    assert selected == {"choices": ["low", "high"], "default": 1}
    assert value == "low"
    assert request(catalog, "gpt-6.1-sol", {"effort": value})["reasoning"]["effort"] == "low"
    assert len(catalog["requests"]) == 1


def test_model_switches_and_context_aliases_use_advertised_wire_levels(catalog):
    for model, effort, expected in [
        ("gpt-6.1-sol", "max", "high"),
        ("gpt-5.3-codex", "max", "max"),
        ("gpt-5.3-codex", "minimal", "minimal"),
        ("gpt-5.6-sol-900k", "medium", "low"),
    ]:
        cfg = {"enabled": True, "effort": effort}
        kw = request(catalog, model, cfg)
        assert kw["reasoning"]["effort"] == expected
        assert kw["model"] == model.removesuffix("-900k")
        assert cfg == {"enabled": True, "effort": effort}
    assert len(catalog["requests"]) == 1


def test_mercury_agent_builds_requests_from_advertised_capabilities(catalog, monkeypatch):
    import run_agent
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **_kw: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    agent = run_agent.AIAgent(
        model="gpt-6.1-sol", provider="openai-codex", api_mode="codex_responses",
        base_url=catalog["base"], api_key="test-account", quiet_mode=True,
        reasoning_config={"enabled": True, "effort": "max"},
        skip_context_files=True, skip_memory=True,
    )
    for model, expected in [("gpt-6.1-sol", "high"), ("gpt-5.3-codex", "max")]:
        agent.model = model
        kw = agent._build_api_kwargs([{"role": "user", "content": "hello"}], tools_for_api=[])
        assert kw["reasoning"]["effort"] == expected
    assert sum("client_version=" in path for path, _auth in catalog["requests"]) == 1


def test_mercury_cold_start_uses_reasoning_for_a_newly_advertised_vendor(catalog, monkeypatch):
    import run_agent
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **_kw: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    monkeypatch.setattr(models, "_OPENROUTER_CATALOG_URL", catalog["base"] + "/models")
    monkeypatch.setattr(models, "_openrouter_reasoning_caps_cache", None)
    monkeypatch.setattr(models, "_openrouter_reasoning_caps_failed_at", None)
    monkeypatch.setattr(models, "_openrouter_caps_disk_checked", True)
    agent = run_agent.AIAgent(
        model="unseen/reasoner", provider="openrouter", api_mode="chat_completions",
        base_url="https://openrouter.ai/api/v1", api_key="test-account", quiet_mode=True,
        reasoning_config={"enabled": True, "effort": "max"},
        skip_context_files=True, skip_memory=True,
    )
    kw = agent._build_api_kwargs([{"role": "user", "content": "hello"}], tools_for_api=[])
    assert kw["extra_body"]["reasoning"]["effort"] == "high"
    assert len(catalog["requests"]) == 1


@pytest.mark.parametrize("config", [{"enabled": False}, {"effort": "none"}, {"effort": "off"}])
def test_mandatory_model_uses_advertised_default_for_off(catalog, config):
    assert request(catalog, "gpt-6.1-sol", config)["reasoning"]["effort"] == "high"


def test_optional_and_non_reasoning_models_respect_off(catalog):
    assert request(catalog, "gpt-5.3-codex", {"enabled": False})["reasoning"]["effort"] == "none"
    assert "reasoning" not in request(catalog, "disabled-model", {"effort": "high"})


def test_catalog_is_account_and_route_scoped(catalog):
    assert request(catalog, "gpt-6.1-sol", {"effort": "max"})["reasoning"]["effort"] == "high"
    assert request(catalog, "gpt-6.1-sol", {"effort": "max"}, token="another-account")["reasoning"]["effort"] == "medium"
    assert request(catalog, "gpt-6.1-sol", {"effort": "max"})["reasoning"]["effort"] == "high"
    assert codex_models.codex_model_reasoning_capabilities(
        "gpt-6.1-sol", access_token="test-account", base_url="http://unrelated.example/codex",
    ) is None
    assert len(catalog["requests"]) == 2


def test_outage_unknown_and_hidden_models_do_not_invent_metadata(catalog):
    assert models.model_reasoning_capabilities("openai-codex", "legacy-model") is None
    assert models.model_reasoning_capabilities("openai-codex", "hidden-model") is None
    catalog["fail"] = True
    codex_models._reasoning_catalogs.clear()
    kw = request(catalog, "gpt-6.1-sol", {"effort": "max"}, token="offline-account")
    assert kw["reasoning"]["effort"] == "xhigh"  # existing offline compatibility
    count = len(catalog["requests"])
    request(catalog, "gpt-6.1-sol", {"effort": "high"}, token="offline-account")
    assert len(catalog["requests"]) == count  # failure cooldown


@pytest.mark.parametrize("provider", ["openrouter", "nous"])
@pytest.mark.parametrize("reasoning,config,expected", [
    ({"supported_efforts": ["low", "high"]}, {"effort": "max"}, {"effort": "high"}),
    ({"mandatory": False}, {"enabled": True, "effort": "high"}, {"enabled": True}),
    ({"supported_efforts": None}, {"enabled": True, "effort": "max"}, {"enabled": True, "effort": "max"}),
    ({"supported_efforts": [], "supports_max_tokens": True}, {"effort": "high", "max_tokens": 1024}, {"max_tokens": 1024}),
])
def test_aggregator_runtime_uses_its_own_capabilities(monkeypatch, provider, reasoning, config, expected):
    from providers import get_provider_profile
    caps = models.parse_openrouter_reasoning_capabilities({
        "supported_parameters": ["tools", "reasoning"], "reasoning": reasoning,
    })
    monkeypatch.setattr(models, f"_{provider}_reasoning_caps_cache", {"vendor/model": caps})
    body, _ = get_provider_profile(provider).build_api_kwargs_extras(
        model="vendor/model", supports_reasoning=True, reasoning_config=config,
    )
    assert body["reasoning"] == expected


def test_codex_auxiliary_request_uses_the_same_capabilities(catalog, monkeypatch):
    from agent import auxiliary_client
    captured = {}
    client = SimpleNamespace(api_key="test-account", base_url=catalog["base"])
    adapter = auxiliary_client._CodexCompletionsAdapter(client, "gpt-6.1-sol", codex_catalog=True)
    # The request builder runs before SDK dispatch; a fake responses endpoint
    # captures the payload using the adapter's existing SDK contract.
    class Stop(Exception):
        pass
    def capture(**kwargs):
        captured.update(kwargs)
        raise Stop()
    client.responses = SimpleNamespace(stream=capture, create=capture)
    with pytest.raises(Stop):
        adapter.create(messages=[{"role": "user", "content": "hello"}],
                       extra_body={"reasoning": {"effort": "max"}})
    assert captured["reasoning"]["effort"] == "high"
