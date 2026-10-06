"""GRPO template model assets stay local without disabling training."""

import builtins
import importlib.util
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "optional-skills/mlops/training/trl-fine-tuning/templates/basic_grpo_training.py"
)
DEPENDENCIES = {"torch", "datasets", "transformers", "peft", "trl"}


@pytest.fixture
def training_backend(monkeypatch):
    calls = []
    imports = []
    model = object()
    tokenizer = SimpleNamespace(eos_token="<eos>", pad_token=None)

    def load_model(path, **kwargs):
        calls.append(("model", path, kwargs))
        return model

    def load_tokenizer(path, **kwargs):
        calls.append(("tokenizer", path, kwargs))
        return tokenizer

    def dataset(*args, **kwargs):
        raise AssertionError("missing assets must fail before dataset loading")

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    for name, module in {
        "torch": SimpleNamespace(bfloat16="bf16"),
        "datasets": SimpleNamespace(load_dataset=dataset),
        "transformers": SimpleNamespace(
            AutoModelForCausalLM=SimpleNamespace(from_pretrained=load_model),
            AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),
        ),
        "peft": SimpleNamespace(LoraConfig=lambda **kwargs: kwargs),
        "trl": SimpleNamespace(GRPOTrainer=None, GRPOConfig=None),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    real_import = builtins.__import__

    def observe_import(name, *args, **kwargs):
        if name.split(".")[0] in DEPENDENCIES:
            imports.append((name, os.environ.get("HF_HUB_OFFLINE"),
                            os.environ.get("TRANSFORMERS_OFFLINE")))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observe_import)
    return SimpleNamespace(calls=calls, imports=imports, model=model, tokenizer=tokenizer)


def load_template():
    spec = importlib.util.spec_from_file_location("grpo_local_template", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_missing_assets_fails_before_optional_dependencies(training_backend):
    with pytest.raises(ValueError, match="MODEL_NAME.*local"):
        runpy.run_path(str(TEMPLATE), run_name="__main__")

    assert training_backend.imports == []
    assert training_backend.calls == []


@pytest.fixture
def local_assets(tmp_path):
    assets = tmp_path / "local model"
    assets.mkdir()
    (assets / "config.json").write_text('{"model_type": "qwen2"}')
    (assets / "model.safetensors").write_bytes(b"fixture weights, never deserialized")
    (assets / "tokenizer.json").write_text('{"fixture": true}')
    return assets


@pytest.mark.parametrize("missing", ["config.json", "model.safetensors", "tokenizer.json"])
@pytest.mark.parametrize("entrypoint", ["main", "setup_model_and_tokenizer"])
def test_incomplete_assets_fail_before_model_construction(
    local_assets, training_backend, monkeypatch, missing, entrypoint,
):
    (local_assets / missing).unlink()
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", str(local_assets))

    with pytest.raises(ValueError, match="local.*assets"):
        getattr(template, entrypoint)()

    assert training_backend.imports == []
    assert training_backend.calls == []


def test_unreadable_weights_fail_before_dependencies(
    local_assets, training_backend, monkeypatch,
):
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", str(local_assets))
    real_open = Path.open

    def unreadable(path, *args, **kwargs):
        if path == local_assets / "model.safetensors":
            raise PermissionError("fixture denies read")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(ValueError, match="local.*assets"):
        template.setup_model_and_tokenizer()

    assert training_backend.imports == []
    assert training_backend.calls == []


@pytest.mark.parametrize("index", ["model.safetensors.index.json", "pytorch_model.bin.index.json"])
def test_missing_weight_shard_fails_before_dependencies(
    local_assets, training_backend, monkeypatch, index,
):
    (local_assets / "model.safetensors").unlink()
    (local_assets / index).write_text(
        '{"weight_map": {"layer": "missing-shard.bin"}}'
    )
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", str(local_assets))

    with pytest.raises(ValueError, match="local.*assets"):
        template.setup_model_and_tokenizer()

    assert training_backend.imports == []
    assert training_backend.calls == []


@pytest.mark.parametrize("model_name", ["", "Qwen/Qwen2.5-1.5B-Instruct", "https://example.test/model"])
def test_nonlocal_selector_never_reaches_loaders(
    training_backend, monkeypatch, tmp_path, model_name,
):
    monkeypatch.chdir(tmp_path)
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", model_name)
    with pytest.raises(ValueError, match="MODEL_NAME.*local"):
        template.setup_model_and_tokenizer()
    assert training_backend.imports == []
    assert training_backend.calls == []


@pytest.mark.parametrize("empty", ["config.json", "model.safetensors", "tokenizer.json"])
def test_empty_assets_fail_before_dependencies(
    local_assets, training_backend, monkeypatch, empty,
):
    (local_assets / empty).write_bytes(b"")
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", local_assets)
    with pytest.raises(ValueError, match=f"empty file: {empty}"):
        template.main()
    assert training_backend.imports == []
    assert training_backend.calls == []


@pytest.mark.parametrize("weight_map", ["{}", '{"layer": "missing.bin"}', "[]"])
def test_invalid_shard_index_fails_before_dependencies(
    local_assets, training_backend, monkeypatch, weight_map,
):
    (local_assets / "model.safetensors").unlink()
    (local_assets / "model.safetensors.index.json").write_text(
        '{"weight_map": ' + weight_map + '}'
    )
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", local_assets)
    with pytest.raises(ValueError, match="local.*assets"):
        template.main()
    assert training_backend.imports == []
    assert training_backend.calls == []


@pytest.mark.parametrize("weights", [
    "model.safetensors", "pytorch_model.bin",
    "model.safetensors.index.json", "pytorch_model.bin.index.json",
])
@pytest.mark.parametrize("tokenizer_files", [
    ("tokenizer.json",), ("tokenizer.model",), ("spiece.model",),
    ("vocab.txt",), ("vocab.json", "merges.txt"),
])
def test_local_assets_reach_both_offline_loaders(
    local_assets, training_backend, monkeypatch, weights, tokenizer_files,
):
    (local_assets / "model.safetensors").unlink()
    if weights.endswith(".index.json"):
        (local_assets / weights).write_text(
            '{"weight_map": {"layer1": "shard.bin", "layer2": "shard.bin"}}'
        )
        (local_assets / "shard.bin").write_bytes(b"local shard fixture")
    else:
        (local_assets / weights).write_bytes(b"local weights fixture")
    (local_assets / "tokenizer.json").unlink()
    for name in tokenizer_files:
        (local_assets / name).write_bytes(b"local tokenizer fixture")
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", local_assets)

    model, tokenizer = template.setup_model_and_tokenizer()

    assert model is training_backend.model
    assert tokenizer is training_backend.tokenizer
    assert tokenizer.pad_token == tokenizer.eos_token
    assert training_backend.calls == [
        ("model", str(local_assets), {
            "local_files_only": True, "torch_dtype": "bf16",
            "attn_implementation": "flash_attention_2", "device_map": "auto",
        }),
        ("tokenizer", str(local_assets), {"local_files_only": True}),
    ]
    assert training_backend.imports
    assert all(hub == transformers == "1" for _, hub, transformers in training_backend.imports)


@pytest.mark.parametrize("selector", ["relative", "home", "absolute"])
def test_local_path_resolution_preserves_requested_assets(
    local_assets, training_backend, monkeypatch, selector,
):
    monkeypatch.chdir(local_assets.parent)
    monkeypatch.setenv("HOME", str(local_assets.parent))
    template = load_template()
    paths = {
        "relative": local_assets.name,
        "home": "~/" + local_assets.name,
        "absolute": str(local_assets),
    }
    monkeypatch.setattr(template, "MODEL_NAME", paths[selector])
    template.setup_model_and_tokenizer()
    assert [call[1] for call in training_backend.calls] == [str(local_assets)] * 2


def test_provisioned_model_still_trains_and_saves(
    local_assets, training_backend, monkeypatch, tmp_path,
):
    template = load_template()
    monkeypatch.setattr(template, "MODEL_NAME", local_assets)
    output = str(tmp_path / "requested output")
    monkeypatch.setattr(template, "OUTPUT_DIR", output)
    dataset = [{"prompt": [], "answer": "42"}]
    monkeypatch.setattr(template, "get_dataset", lambda: dataset)
    events = []

    class Trainer:
        def __init__(self, **kwargs):
            assert kwargs["model"] is training_backend.model
            assert kwargs["processing_class"] is training_backend.tokenizer
            assert kwargs["train_dataset"] is dataset
            assert kwargs["args"]["output_dir"] == output
            assert kwargs["peft_config"]["task_type"] == "CAUSAL_LM"
            assert kwargs["reward_funcs"] == [
                template.incremental_format_reward_func,
                template.format_reward_func, template.correctness_reward_func,
            ]
            events.append("trainer")

        def train(self):
            events.append("train")

        def save_model(self, path):
            events.append(("save", path))

    monkeypatch.setitem(sys.modules, "trl", SimpleNamespace(
        GRPOTrainer=Trainer, GRPOConfig=lambda **kwargs: kwargs,
    ))
    template.main()
    assert events == ["trainer", "train", ("save", output + "/final")]
    assert [call[0] for call in training_backend.calls] == ["model", "tokenizer"]
