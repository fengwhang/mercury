"""Nous adapter metadata stays tied to its serving provider and profile."""
import json

from mercury_cli import omp_provider


def test_nous_metadata_keeps_api_ladders_limits_and_transport(monkeypatch):
    monkeypatch.setattr(omp_provider.auth, "_load_auth_store", lambda: {"providers": {"nous": {"account_id": "test"}}})
    monkeypatch.setattr(omp_provider.auth, "resolve_nous_runtime_credentials", lambda **kwargs: {
        "api_key": "fake-inference-key", "base_url": "https://inference.example/v1",
    })
    monkeypatch.setattr(omp_provider, "_catalog", lambda *args: [
        {"id": "vendor/reasoner", "context_length": 100000, "max_context_length": 300000,
         "supported_parameters": ["reasoning"], "reasoning": {"supported_efforts": ["low", "high"]},
         "pricing": {"prompt": "0.000001", "completion": "unknown"}},
        {"id": "anthropic/claude-test", "context_length": 200000, "supported_parameters": []},
        {"id": "vendor/unknown", "pricing": {"prompt": "NaN"}},
    ])
    reasoner, claude, unknown = omp_provider.runtime_provider()["models"]
    assert reasoner["thinking"]["efforts"] == ["low", "high"]
    assert reasoner["contextWindow"] == 100000
    assert reasoner["maxContextWindow"] == 300000
    assert reasoner["cost"]["input"] == 1
    assert reasoner["cost"]["output"] == 0
    assert claude["api"] == "anthropic-messages"
    assert claude["baseUrl"] == "https://inference.example"
    assert claude["maxContextWindow"] == claude["contextWindow"]
    assert "thinking" not in claude and "thinking" not in unknown
    assert "maxContextWindow" not in unknown
    assert unknown["cost"]["input"] == 0


def test_no_portal_grant_in_this_profile_never_uses_the_default_login(monkeypatch):
    monkeypatch.setattr(omp_provider.auth, "_load_auth_store", lambda: {"providers": {}})
    def unexpected(**kwargs):
        raise AssertionError("Must not resolve another profile's credentials")
    monkeypatch.setattr(omp_provider.auth, "resolve_nous_runtime_credentials", unexpected)
    assert omp_provider.runtime_provider() is None


def test_catalog_cache_survives_outage_without_persisting_inference_key(tmp_path, monkeypatch):
    from mercury_cli import urllib_security
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    class Reply:
        def __enter__(self):
            from io import StringIO
            return StringIO(json.dumps({"data": [{"id": "vendor/model", "context_length": 100000}]}))
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(urllib_security, "open_credentialed_url", lambda *args, **kwargs: Reply())
    first = omp_provider._catalog("https://inference.example/v1", "fake-secret", {})
    path = next((tmp_path / "cache").glob("omp_nous_*.json"))
    cached = json.loads(path.read_text())
    assert "fake-secret" not in path.read_text()
    cached["time"] = 0
    path.write_text(json.dumps(cached))
    def unavailable(*args, **kwargs):
        raise OSError("offline")
    monkeypatch.setattr(urllib_security, "open_credentialed_url", unavailable)
    assert omp_provider._catalog("https://inference.example/v1", "fake-secret", {}) == first
