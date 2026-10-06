"""Local speech must refuse missing assets before any downloader or SDK loader."""
import json
import subprocess
import sys
import types
from unittest.mock import Mock
from pathlib import Path
import os

import pytest

from tools import tts_tool as tts
from tools import tts_tool_local as local
from tools.tts_tool_lifecycle import warm_tts_provider


@pytest.fixture(autouse=True)
def isolated_assets_and_network(tmp_path, monkeypatch):
    import httpx
    import requests
    import socket

    for key in ("MERCURY_HOME", "HERMES_HOME", "PI_CODING_AGENT_DIR", "XDG_DATA_HOME", "HF_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("MERCURY_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf-cache"))
    # Undo environment changes made by an in-process standalone CLI invocation.
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    spies = []
    for owner, name in ((httpx.Client, "send"), (requests.Session, "request"), (socket, "create_connection")):
        spy = Mock(side_effect=AssertionError("unexpected network request"))
        monkeypatch.setattr(owner, name, spy)
        spies.append(spy)
    yield
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["tool", "loader", "warm"])
def test_missing_piper_assets_never_download(entrypoint, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    process = Mock(side_effect=AssertionError("unexpected download process"))
    sdk = Mock(side_effect=AssertionError("unexpected SDK initializer"))
    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setattr(tts, "_import_piper", lambda: sdk)
    config = {"provider": "piper", "piper": {"voices_dir": str(tmp_path)}}
    monkeypatch.setattr(tts, "_load_tts_config", lambda: config)
    if entrypoint == "tool":
        result = json.loads(tts.text_to_speech_tool("hello", str(tmp_path / "out.wav")))
        assert result["success"] is False
        error = result["error"]
    elif entrypoint == "warm":
        result = warm_tts_provider(config)
        assert result["warmed"] is False
        assert result["action"] == "error"
        error = result["error"]
    else:
        with pytest.raises(RuntimeError) as exc:
            local._load_piper_voice_for_config(config)
        error = str(exc.value)
    assert "local" in error.lower()
    assert "tts.piper.voice" in error
    assert ".onnx.json" in error
    process.assert_not_called()
    sdk.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["tool", "loader", "warm"])
def test_missing_kitten_assets_never_initialize_downloader(entrypoint, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "huggingface"))
    sdk = Mock(side_effect=AssertionError("unexpected SDK download"))
    monkeypatch.setattr(tts, "_import_kittentts", lambda: sdk)
    config = {"provider": "kittentts"}
    monkeypatch.setattr(tts, "_load_tts_config", lambda: config)
    if entrypoint == "tool":
        result = json.loads(tts.text_to_speech_tool("hello", str(tmp_path / "out.wav")))
        assert result["success"] is False
        error = result["error"]
    elif entrypoint == "warm":
        result = warm_tts_provider(config)
        assert result["warmed"] is False
        assert result["action"] == "error"
        error = result["error"]
    else:
        with pytest.raises(RuntimeError) as exc:
            local._load_kittentts_model_for_config(config)
        error = str(exc.value)
    assert "local" in error.lower()
    assert "tts.kittentts.model" in error
    assert "config.json" in error
    sdk.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["tool", "local", "edge", "cli", "warm"])
def test_missing_neutts_assets_never_initialize_downloader(entrypoint, tmp_path, monkeypatch, capsys):
    from tools import neutts_synth

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "huggingface"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "huggingface" / "hub"))
    process = Mock(side_effect=AssertionError("unexpected model helper process"))
    sdk = Mock(side_effect=AssertionError("unexpected SDK download"))
    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setitem(sys.modules, "neutts", types.SimpleNamespace(NeuTTS=sdk))
    monkeypatch.setattr(tts, "_check_neutts_available", lambda: True)
    config = {"provider": "edge" if entrypoint == "edge" else "neutts"}
    monkeypatch.setattr(tts, "_load_tts_config", lambda: config)
    if entrypoint == "edge":
        monkeypatch.setattr(tts, "_import_edge_tts", Mock(side_effect=ImportError("missing edge")))
    if entrypoint in ("tool", "edge"):
        result = json.loads(tts.text_to_speech_tool("hello", str(tmp_path / "out.wav")))
        assert result["success"] is False
        error = result["error"]
    elif entrypoint == "warm":
        result = warm_tts_provider(config)
        assert result["warmed"] is False
        assert result["action"] == "error"
        error = result["error"]
    elif entrypoint == "local":
        with pytest.raises(RuntimeError) as exc:
            local._generate_neutts("hello", str(tmp_path / "out.wav"), config)
        error = str(exc.value)
    else:
        audio = tmp_path / "ref.wav"
        transcript = tmp_path / "ref.txt"
        audio.write_bytes(b"reference")
        transcript.write_text("hello")
        monkeypatch.setattr(sys, "argv", ["neutts_synth", "--text", "hello", "--out",
            str(tmp_path / "out.wav"), "--ref-audio", str(audio), "--ref-text", str(transcript)])
        with pytest.raises(SystemExit):
            neutts_synth.main()
        error = capsys.readouterr().err
    assert "local" in error.lower()
    assert "tts.neutts.model" in error
    assert "download" in error.lower()
    process.assert_not_called()
    sdk.assert_not_called()


def test_piper_warm_reuses_the_real_synthesis_cache(tmp_path, monkeypatch):
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"model")
    model.with_suffix(".onnx.json").write_text("{}")
    local._piper_voice_cache.clear()
    tts._piper_voice_cache.clear()
    voice = Mock()

    def synthesize(text, wav):
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 10)

    voice.synthesize_wav.side_effect = synthesize
    sdk = Mock()
    sdk.load.return_value = voice
    monkeypatch.setattr(tts, "_import_piper", lambda: sdk)
    config = {"provider": "piper", "piper": {"voice": str(model), "voices_dir": str(tmp_path)}}
    try:
        assert warm_tts_provider(config)["warmed"] is True
        tts._generate_piper_tts("hello", str(tmp_path / "out.wav"), config)
        sdk.load.assert_called_once()
    finally:
        local._piper_voice_cache.clear()
        tts._piper_voice_cache.clear()


def _cache_repo(root, repo, files):
    model_cache = root / ("models--" + repo.replace("/", "--"))
    (model_cache / "refs").mkdir(parents=True)
    (model_cache / "refs" / "main").write_text("fixture-revision")
    snapshot = model_cache / "snapshots" / "fixture-revision"
    snapshot.mkdir(parents=True)
    for name, content in files.items():
        (snapshot / name).write_bytes(content)
    return snapshot


@pytest.fixture
def cached_neutts(tmp_path, monkeypatch):
    root = tmp_path / "hf-cache"
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    backbone = _cache_repo(root, "neuphonic/neutts-air-q4-gguf", {"model.gguf": b"model"})
    codec = _cache_repo(root, "neuphonic/neucodec", {
        "config.json": b"{}", "pytorch_model.bin": b"weights", "meta.yaml": b"metadata"})
    semantic = _cache_repo(root, "facebook/w2v-bert-2.0", {
        "config.json": b"{}", "preprocessor_config.json": b"{}", "model.safetensors": b"weights"})
    return backbone, codec, semantic


@pytest.mark.parametrize("missing", ["backbone", "codec", "semantic"])
def test_partial_neutts_cache_refuses_before_helper(missing, cached_neutts, tmp_path, monkeypatch):
    backbone, codec, semantic = cached_neutts
    path = {"backbone": backbone / "model.gguf", "codec": codec / "pytorch_model.bin",
            "semantic": semantic / "model.safetensors"}[missing]
    path.unlink()
    process = Mock(side_effect=AssertionError("unexpected helper initialization"))
    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(RuntimeError, match="local assets unavailable"):
        local._generate_neutts("hello", str(tmp_path / "out.wav"), {})
    process.assert_not_called()


def test_cached_kitten_uses_local_onnx_loader(tmp_path, monkeypatch):
    root = tmp_path / "hf-cache"
    monkeypatch.setenv("HF_HUB_CACHE", str(root))
    snapshot = _cache_repo(root, local.DEFAULT_KITTENTTS_MODEL, {
        "config.json": json.dumps({"type": "ONNX1", "model_file": "model.onnx", "voices": "voices.npz",
            "voice_aliases": {"Jasper": "voice"}, "speed_priors": {"voice": 1.1}}).encode(),
        "model.onnx": b"model", "voices.npz": b"voices"})
    loader = Mock()
    monkeypatch.setattr(tts, "_import_kittentts", lambda: loader)
    local._kittentts_model_cache.clear()
    try:
        assert warm_tts_provider({"provider": "kittentts"})["warmed"] is True
        local._load_kittentts_model_for_config({})
        loader.assert_called_once_with(model_path=str(snapshot / "model.onnx"),
            voices_path=str(snapshot / "voices.npz"), voice_aliases={"Jasper": "voice"},
            speed_priors={"voice": 1.1})
    finally:
        local._kittentts_model_cache.clear()


def test_neutts_subprocess_blocks_uncatalogued_sdk_fetch(cached_neutts, tmp_path, monkeypatch):
    # Exercise the real helper process and real HF offline enforcement, not a flag assertion.
    sdk_dir = tmp_path / "sdk"
    sdk_dir.mkdir()
    (sdk_dir / "neutts.py").write_text(
        "import socket\n"
        "def forbidden(*a, **k):\n"
        "    raise AssertionError('NETWORK ATTEMPT')\n"
        "socket.socket.connect = forbidden\n"
        "from huggingface_hub import hf_hub_download\n"
        "class NeuTTS:\n"
        "    def __init__(self, **kwargs):\n"
        "        hf_hub_download('mercury-offline-fixture/missing', 'weights.bin')\n")
    audio = tmp_path / "reference.wav"
    text = tmp_path / "reference.txt"
    audio.write_bytes(b"reference")
    text.write_text("hello")
    monkeypatch.setenv("PYTHONPATH", str(sdk_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    config = {"neutts": {"ref_audio": str(audio), "ref_text": str(text)}}
    with pytest.raises(RuntimeError) as exc:
        local._generate_neutts("hello", str(tmp_path / "out.wav"), config)
    assert "LocalEntryNotFoundError" in str(exc.value)
    assert "NETWORK ATTEMPT" not in str(exc.value)


def test_neutts_complete_cache_reaches_sdk_with_offline_flags(cached_neutts, tmp_path, monkeypatch):
    from tools import neutts_synth

    audio = tmp_path / "reference.wav"
    transcript = tmp_path / "reference.txt"
    audio.write_bytes(b"reference")
    transcript.write_text("hello")
    output = tmp_path / "out.wav"
    instance = Mock()
    instance.infer.return_value = [0.1, 0.2]

    def initialize(**kwargs):
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert kwargs["backbone_repo"] == str(cached_neutts[0] / "model.gguf")
        return instance

    sdk = Mock(side_effect=initialize)
    monkeypatch.setitem(sys.modules, "neutts", types.SimpleNamespace(NeuTTS=sdk))
    def write_audio(path, samples, rate):
        import struct
        import wave
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b"".join(struct.pack("<h", int(sample * 32767)) for sample in samples))
    monkeypatch.setitem(sys.modules, "soundfile", types.SimpleNamespace(write=write_audio))
    monkeypatch.setattr(sys, "argv", ["neutts_synth", "--text", "hello", "--out", str(output),
        "--ref-audio", str(audio), "--ref-text", str(transcript)])
    neutts_synth.main()
    sdk.assert_called_once()
    instance.encode_reference.assert_called_once_with(str(audio))
    assert output.read_bytes().startswith(b"RIFF")


@pytest.mark.parametrize("selection", ["cache", "explicit"])
def test_neutts_cached_symlink_preserves_gguf_backend_selection(selection, cached_neutts, tmp_path):
    from tools.neutts_synth import _resolve_neutts_assets

    model = cached_neutts[0] / "model.gguf"
    blob = tmp_path / "weight-blob-without-extension"
    model.rename(blob)
    model.symlink_to(blob)
    selected = str(model) if selection == "explicit" else "neuphonic/neutts-air-q4-gguf"
    result = _resolve_neutts_assets(selected)
    assert result == str(model)
    assert result.endswith(".gguf")  # NeuTTS chooses llama.cpp by this extension.


def test_piper_symlink_keeps_the_validated_config_sidecar(tmp_path):
    blob = tmp_path / "voice-blob"
    blob.write_bytes(b"model")
    model = tmp_path / "voice.onnx"
    model.symlink_to(blob)
    Path(f"{model}.json").write_text("{}")
    resolved = tts._resolve_piper_voice_path(str(model), tmp_path)
    assert Path(f"{resolved}.json").is_file()
