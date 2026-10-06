"""Offline PM discovery: release metadata, compatible assets, and no HF requests."""
from __future__ import annotations

import io
import json
from urllib.parse import urlsplit

import pytest

import mercury_cli.urllib_security as urllib_security
from pm.packages import LlamaCppCuda


@pytest.fixture
def release_http(monkeypatch, tmp_path):
    for name in ("MERCURY_HOME", "HERMES_HOME", "MERCURY_CONFIG", "PI_CODING_AGENT_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    responses = {}
    requests = []

    def open_url(request, *, timeout):
        url = request.full_url
        requests.append(url)
        assert urlsplit(url).hostname == "api.github.com", f"unexpected HTTP request: {url}"
        assert url in responses, f"unconfigured HTTP request: {url}"
        response = responses[url]
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(urllib_security, "open_credentialed_url", open_url)
    return responses, requests


def release(tag, *assets, **flags):
    return {"tag_name": tag, "assets": [{"name": name} for name in assets], **flags}


def test_llamacpp_discovery_requires_engine_and_cuda_runtime(release_http):
    responses, requests = release_http
    url = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30&page=1"
    responses[url] = [
        release("b11149", "llama-b11149-bin-ubuntu-cuda-13.4-x64.tar.gz"),
        release("b11148", "llama-b11148-bin-ubuntu-cuda-13.4-x64.tar.gz",
                "cudart-llama-b11148-bin-ubuntu-cuda-13.4-x64.tar.gz"),
        release("b11147", "llama-b11147-bin-ubuntu-cuda-13.3-x64.tar.gz",
                "cudart-llama-b11147-bin-ubuntu-cuda-13.3-x64.tar.gz"),
        release("b11146", "llama-b11146-bin-ubuntu-cuda-13.4-x64.tar.gz",
                "cudart-llama-b11146-bin-ubuntu-cuda-13.4-x64.tar.gz"),
    ]

    assert LlamaCppCuda().latest_versions("linux-x64", locked="11146") == ["11148", "11146"]
    assert requests == [url]
    assert not any("huggingface" in url for url in requests)


def test_update_retains_pins_when_a_supported_target_has_no_release(
    release_http, monkeypatch, tmp_path, capsys,
):
    from pm import cli, runtime
    from pm.lock import Lockfile

    responses, requests = release_http
    url = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30&page=1"
    responses[url] = [
        release("b11148", "llama-b11148-bin-ubuntu-cuda-13.4-x64.tar.gz",
                "cudart-llama-b11148-bin-ubuntu-cuda-13.4-x64.tar.gz"),
    ]
    lock_path = tmp_path / "lock.json"
    lockfile = Lockfile(lock_path)
    lockfile.set_pin("llamacpp-cuda", "11146", {})
    lockfile.save()
    before = lock_path.read_bytes()
    monkeypatch.setattr(cli, "_lockfile", lambda: Lockfile(lock_path))
    monkeypatch.setattr(runtime, "is_runtime", lambda: True)

    assert cli.main(["update", "--check"]) == 1
    output = capsys.readouterr().out
    assert "resolution failed for llamacpp-cuda" in output
    assert "up to date" not in output
    assert lock_path.read_bytes() == before
    assert requests == [url]
