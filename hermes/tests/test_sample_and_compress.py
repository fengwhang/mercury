"""Offline maintenance sampling and tokenizer initialization contracts."""

import json
import multiprocessing
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from scripts import sample_and_compress as sampler


def test_worker_missing_tokenizer_never_fetches_assets(monkeypatch):
    calls = []
    network_attempts = []

    def from_pretrained(name, **kwargs):
        calls.append((name, kwargs))
        if not kwargs.get("local_files_only"):
            network_attempts.append(name)
        raise OSError("required tokenizer code is not cached")

    monkeypatch.setitem(
        sys.modules, "transformers",
        SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained)),
    )
    monkeypatch.setattr(sampler, "_TOKENIZER", None)
    with pytest.raises(RuntimeError) as exc:
        sampler._init_tokenizer_worker("moonshotai/Kimi-K2-Thinking")

    assert network_attempts == []
    assert len(calls) == 1
    assert calls[0][1]["local_files_only"] is True
    message = str(exc.value)
    assert "moonshotai/Kimi-K2-Thinking" in message
    assert "local" in message.lower()
    assert "cache" in message.lower()
    assert "tokenizer_name" in message
    assert isinstance(exc.value.__cause__, OSError)
    assert sampler._TOKENIZER is None


def test_cli_remote_dataset_refused_before_hf_or_workers(monkeypatch):
    network_attempts = []

    def load_dataset(name, **kwargs):
        network_attempts.append(name)
        raise OSError("remote dataset requires a download")

    def forbidden_pool(*args, **kwargs):
        raise AssertionError("must fail before starting tokenization workers")

    monkeypatch.setitem(sys.modules, "datasets", SimpleNamespace(load_dataset=load_dataset))
    monkeypatch.setattr("multiprocessing.Pool", forbidden_pool)
    with pytest.raises(RuntimeError) as exc:
        sampler.main(datasets="NousResearch/not-cached", total_samples=1)

    assert network_attempts == []
    message = str(exc.value)
    assert "NousResearch/not-cached" in message
    assert "--datasets" in message
    assert "JSONL" in message
    assert "download" in message.lower()


def test_sampling_missing_tokenizer_fails_before_starting_pool(tmp_path, monkeypatch):
    data = tmp_path / "trajectories.jsonl"
    data.write_text('{"conversations": [{"from": "human", "value": "hello"}]}\n')
    calls = []

    def from_pretrained(name, **kwargs):
        calls.append((name, kwargs))
        assert kwargs["local_files_only"] is True
        raise OSError("tokenizer assets missing")

    def forbidden_pool(*args, **kwargs):
        raise AssertionError("must fail before starting tokenization workers")

    monkeypatch.setitem(
        sys.modules, "transformers",
        SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained)),
    )
    monkeypatch.setattr("multiprocessing.Pool", forbidden_pool)
    with pytest.raises(RuntimeError, match="local/cached tokenizer"):
        sampler.main(
            datasets=str(data), total_samples=1,
            tokenizer_name=str(tmp_path / "missing-tokenizer"),
        )

    assert len(calls) == 1
    assert calls[0][0] == str(tmp_path / "missing-tokenizer")


@pytest.fixture
def local_tokenizer(tmp_path, monkeypatch):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    assets = tmp_path / "tokenizer"
    assets.mkdir()
    tokenizer.save(str(assets / "tokenizer.json"))
    calls = []

    def from_pretrained(name, **kwargs):
        calls.append((name, kwargs))
        assert kwargs["local_files_only"] is True
        loaded = tokenizers.Tokenizer.from_file(str(Path(name) / "tokenizer.json"))
        return SimpleNamespace(encode=lambda text: loaded.encode(text).ids)

    monkeypatch.setitem(
        sys.modules, "transformers",
        SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained)),
    )
    monkeypatch.setattr(sampler, "_TOKENIZER", None)
    return assets, calls


@pytest.mark.parametrize("trust_remote_code", [True, False])
def test_local_tokenizer_preserves_compressor_counting(local_tokenizer, trust_remote_code):
    from trajectory_compressor import CompressionConfig, TrajectoryCompressor
    assets, calls = local_tokenizer

    compressor = TrajectoryCompressor.__new__(TrajectoryCompressor)
    compressor.config = CompressionConfig(
        tokenizer_name=str(assets), trust_remote_code=trust_remote_code,
    )
    compressor._init_tokenizer()
    trajectory = [{"from": "human", "value": "hello world"}]
    result, metrics = compressor.compress_trajectory(trajectory)

    assert compressor.count_tokens("hello world") == 2
    assert result == trajectory
    assert metrics.original_tokens == 2
    assert not metrics.was_compressed
    assert calls == [
        (str(assets), {"trust_remote_code": trust_remote_code, "local_files_only": True})
    ]


def test_local_tokenizer_preserves_sampler_filtering(local_tokenizer, tmp_path, monkeypatch):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("the in-memory tokenizer backend requires fork")
    monkeypatch.setattr(multiprocessing, "Pool", multiprocessing.get_context("fork").Pool)
    assets, calls = local_tokenizer
    data = tmp_path / "trajectories.jsonl"
    entries = [
        {"conversations": [{"from": "human", "value": "hello world"}]},
        {"conversations": [{"from": "human", "value": "hello"}]},
    ]
    data.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    sampled = sampler.sample_from_datasets(
        [str(data)], total_samples=1, min_tokens=2,
        tokenizer_name=str(assets), num_proc=1,
    )

    assert len(sampled) == 1
    assert sampled[0]["conversations"] == entries[0]["conversations"]
    assert sampled[0]["_original_tokens"] == 2
    assert sampled[0]["_source_dataset"] == str(data)
    assert calls[0] == (str(assets), {"trust_remote_code": True, "local_files_only": True})


@pytest.mark.parametrize("extension", ["json", "jsonl"])
def test_local_dataset_normalizes_existing_formats_without_hf(tmp_path, monkeypatch, extension):
    def forbidden_load(*args, **kwargs):
        raise AssertionError("local files must not invoke Hub loading")

    monkeypatch.setitem(sys.modules, "datasets", SimpleNamespace(load_dataset=forbidden_load))
    entries = [
        {"conversations": [{"from": "human", "value": "hello"}], "metadata": "ignored"},
        {"messages": [{"role": "user", "content": "world"}]},
        {"custom": "entry"},
    ]
    data = tmp_path / f"trajectories.{extension}"
    data.write_text(
        json.dumps(entries) if extension == "json"
        else "".join(json.dumps(entry) + "\n" for entry in entries)
    )

    assert sampler.load_local_dataset(str(data)) == [
        {"conversations": entries[0]["conversations"]},
        {"conversations": entries[1]["messages"]},
        entries[2],
    ]


def test_saved_local_dataset_uses_train_split_without_hub(tmp_path, monkeypatch):
    class DatasetDict(dict):
        pass

    calls = []

    def load_from_disk(path):
        calls.append(path)
        return DatasetDict(
            train=[{"messages": [{"role": "user", "content": "hello"}]}],
            validation=[{"messages": [{"role": "user", "content": "not train"}]}],
        )

    monkeypatch.setitem(
        sys.modules, "datasets",
        SimpleNamespace(DatasetDict=DatasetDict, load_from_disk=load_from_disk),
    )
    assert sampler.load_local_dataset(str(tmp_path)) == [
        {"conversations": [{"role": "user", "content": "hello"}]}
    ]
    assert calls == [str(tmp_path)]
