"""Offline PM discovery: release metadata, compatible assets, and no HF requests."""
from __future__ import annotations

import io
import json
from pathlib import Path
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


@pytest.mark.parametrize("check", [True, False])
def test_update_retains_pins_when_a_supported_target_has_no_release(
    release_http, monkeypatch, tmp_path, capsys, check,
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

    assert cli.main(["update", *(["--check"] if check else [])]) == 1
    output = capsys.readouterr().out
    assert "resolution failed for llamacpp-cuda" in output
    assert "up to date" not in output
    assert lock_path.read_bytes() == before
    assert requests == [url]


@pytest.fixture
def llama_pins():
    import pm.packages as packages

    lock = json.loads(Path(packages.__file__).with_name("lock.json").read_text())
    return {name: row for name, row in lock["packages"].items() if name.startswith("llamacpp-")}


def release_from_pins(pins, version):
    names = set()
    for row in pins.values():
        for artifacts in row["artifacts"].values():
            for artifact in artifacts if isinstance(artifacts, list) else [artifacts]:
                if urlsplit(artifact["url"]).hostname == "github.com":
                    names.add(artifact["url"].rsplit("/", 1)[-1].replace(
                        f"b{row['version']}", f"b{version}",
                    ))
    return release(f"b{version}", *sorted(names))


@pytest.mark.parametrize("backend,target", [
    ("cpu", "linux-x64"), ("cpu", "linux-arm64"),
    ("cpu", "darwin-x64"), ("cpu", "darwin-arm64"),
    ("cpu", "win32-x64"), ("cpu", "win32-arm64"),
    ("cuda", "linux-x64"), ("cuda", "linux-arm64"),
    ("cuda", "win32-x64"), ("cuda", "win32-arm64"),
    ("hip", "linux-x64"), ("hip", "win32-x64"),
    ("metal", "darwin-x64"), ("metal", "darwin-arm64"),
    ("vulkan", "linux-x64"), ("vulkan", "linux-arm64"),
    ("vulkan", "win32-x64"),
])
def test_discovery_preserves_every_pinned_platform_contract(
    release_http, llama_pins, backend, target,
):
    from pm.registry import get_package

    responses, requests = release_http
    package = get_package(f"llamacpp-{backend}")
    row = llama_pins[package.name]
    artifacts = row["artifacts"][target]
    artifacts = artifacts if isinstance(artifacts, list) else [artifacts]
    assert package.fetch_urls(row["version"], target) == [a["url"] for a in artifacts]
    url = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30&page=1"
    candidate = release_from_pins({package.name: row}, "11148")
    responses[url] = [
        {**candidate, "prerelease": True},
        {**candidate, "draft": True},
        {**candidate, "tag_name": "11148"},
        {**candidate, "tag_name": "b11148-preview"},
        release("latest"),
        candidate,
    ]

    assert package.latest_versions(target, locked=row["version"]) == ["11148"]
    assert requests == [url]


def test_llamacpp_resolution_intersects_paginated_platform_assets(release_http):
    from pm.update import resolve_package

    responses, requests = release_http
    first = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30&page=1"
    second = first.replace("page=1", "page=2")
    responses[first] = [release(f"b{11200 - i}") for i in range(30)]
    responses[second] = [
        release("b11149", "llama-b11149-bin-ubuntu-cuda-13.4-x64.tar.gz",
                "cudart-llama-b11149-bin-ubuntu-cuda-13.4-x64.tar.gz"),
        release("b11148", "llama-b11148-bin-ubuntu-cuda-13.4-x64.tar.gz",
                "cudart-llama-b11148-bin-ubuntu-cuda-13.4-x64.tar.gz",
                "llama-b11148-bin-win-cuda-13.4-x64.zip",
                "cudart-llama-bin-win-cuda-13.4-x64.zip"),
    ]

    decision = resolve_package(LlamaCppCuda(), ["linux-x64", "win32-x64"], "11146")
    assert decision.changed
    assert decision.version == "11148"
    assert decision.per_target == {"linux-x64": "11148", "win32-x64": "11148"}
    assert requests == [first, second]


@pytest.mark.parametrize("check", [True, False])
def test_default_update_discovers_all_llama_backends_without_hf(
    release_http, llama_pins, monkeypatch, tmp_path, capsys, check,
):
    from pm import cli, runtime
    import pm.packages as packages
    from pm.lock import Lockfile

    responses, requests = release_http
    index = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30&page=1"
    candidate = release_from_pins(llama_pins, "11148")
    responses[index] = [candidate]
    tag_url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b11148"
    responses[tag_url] = {**candidate, "assets": [
        {**asset, "digest": f"sha256:{'a' * 64}"} for asset in candidate["assets"]
    ]}
    monkeypatch.setattr(packages, "_release_digest_cache", {})
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps({"schema": 1, "packages": llama_pins}))
    before = lock_path.read_bytes()
    monkeypatch.setattr(cli, "_lockfile", lambda: Lockfile(lock_path))
    monkeypatch.setattr(runtime, "is_runtime", lambda: True)
    installs = []
    syncs = []
    monkeypatch.setattr(cli, "_install_names", lambda names: installs.append(names) or 0)
    monkeypatch.setattr(cli, "_sync_venv_step", lambda: syncs.append(True) or True)

    assert cli.main(["update", *(["--check"] if check else [])]) == (1 if check else 0)
    output = capsys.readouterr().out
    for name in llama_pins:
        assert f"{llama_pins[name]['version']} → 11148" in output
        assert name in output
    if check:
        assert lock_path.read_bytes() == before
        assert not installs and not syncs
        assert requests == [index] * len(llama_pins)
    else:
        updated = Lockfile(lock_path)
        for name, row in llama_pins.items():
            assert updated.version(name) == "11148"
            assert set(updated.pinned_artifacts(name)) == set(row["artifacts"])
            for target in row["artifacts"]:
                for artifact in updated.artifacts(name, target):
                    if urlsplit(artifact["url"]).hostname == "github.com":
                        assert artifact["sha256"] == "a" * 64
                        assert "/download/b11148/" in artifact["url"]
                    else:
                        assert artifact in (
                            row["artifacts"][target] if isinstance(row["artifacts"][target], list)
                            else [row["artifacts"][target]]
                        )
        assert installs == [sorted(llama_pins)]
        assert syncs == [True]
        assert requests == [index] * len(llama_pins) + [tag_url]
    assert not any("huggingface" in url for url in requests)


@pytest.mark.parametrize("check", [True, False])
def test_metadata_failure_is_reported_without_claiming_pins_are_latest(
    release_http, monkeypatch, tmp_path, capsys, check,
):
    from pm import cli, runtime
    from pm.lock import Lockfile

    responses, requests = release_http
    url = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30&page=1"
    responses[url] = RuntimeError("metadata unavailable")
    lock_path = tmp_path / "lock.json"
    lockfile = Lockfile(lock_path)
    lockfile.set_pin("llamacpp-cuda", "11146", {})
    lockfile.save()
    before = lock_path.read_bytes()
    monkeypatch.setattr(cli, "_lockfile", lambda: Lockfile(lock_path))
    monkeypatch.setattr(runtime, "is_runtime", lambda: True)

    assert cli.main(["update", *(["--check"] if check else [])]) == 1
    output = capsys.readouterr().out
    assert "metadata unavailable" in output
    assert "up to date" not in output
    assert lock_path.read_bytes() == before
    assert requests == [url]


def test_other_github_packages_keep_stable_tag_discovery(release_http):
    from pm.update import github_release_tags

    responses, requests = release_http
    url = "https://api.github.com/repos/cli/cli/releases?per_page=30&page=1"
    responses[url] = [
        release("v3.0.0", prerelease=True), release("v2.0.1", draft=True),
        release("latest"), release("sandbox"), release("v2.0.0"), release("v1.9.0"),
    ]
    assert github_release_tags("cli/cli", strip_prefix="v") == ["2.0.0", "1.9.0"]
    assert requests == [url]
