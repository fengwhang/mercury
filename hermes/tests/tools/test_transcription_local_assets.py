"""Local STT must never obtain model assets from a remote Hub."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".mercury"
    for name, path in {
        "HOME": home, "MERCURY_HOME": root, "HERMES_HOME": root / "hermes",
        "MERCURY_CONFIG": root / "config.yaml", "PI_CODING_AGENT_DIR": root / "omp",
        "MERCURY_INHERIT_FROM": home / "absent", "XDG_DATA_HOME": home / "xdg",
    }.items():
        monkeypatch.setenv(name, str(path))


@pytest.mark.parametrize("force_cpu", [False, True])
def test_audio_entrypoint_refuses_missing_local_model_without_download(tmp_path, monkeypatch, force_cpu):
    from tools import transcription_tools as stt

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 40)
    remote_attempts = []
    calls = []

    def whisper(model, **options):
        calls.append(options)
        if options.get("local_files_only") is not True:
            remote_attempts.append(model)
        raise OSError("model unavailable in local cache")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", SimpleNamespace(download_model=whisper))
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)
    monkeypatch.setattr(stt, "_local_model", None)
    monkeypatch.setattr(stt, "_local_model_name", None)
    monkeypatch.setattr(stt, "_should_force_faster_whisper_cpu", lambda: force_cpu)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: {
        "enabled": True, "provider": "local", "local": {"model": "base"}})
    result = stt.transcribe_audio(str(audio))
    assert result["success"] is False
    assert remote_attempts == []
    assert len(calls) == 1
    assert "stt.local.model" in result["error"]
    assert "download" in result["error"].lower()


def test_automatic_whisper_cli_refuses_named_model_before_process(tmp_path, monkeypatch):
    from tools import transcription_tools as stt

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 40)
    processes = []
    monkeypatch.delenv(stt.LOCAL_STT_COMMAND_ENV, raising=False)
    monkeypatch.setattr(stt, "_find_whisper_binary", lambda: "/fake/whisper")
    monkeypatch.setattr(stt, "_load_stt_config", lambda: {
        "enabled": True, "provider": "local_command", "local": {"model": "base"}})
    monkeypatch.setattr(stt.subprocess, "run",
                        lambda *args, **kwargs: processes.append(args) or SimpleNamespace(
                            returncode=1, stdout="", stderr="would obtain weights"))
    result = stt.transcribe_audio(str(audio))
    assert result["success"] is False
    assert processes == []
    assert "local model file" in result["error"]


@pytest.mark.parametrize("missing", ["model.bin", "config.json", "tokenizer.json"])
def test_incomplete_snapshot_refused_before_nested_tokenizer_loader(tmp_path, monkeypatch, missing):
    from tools import transcription_tools as stt

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 40)
    snapshot = tmp_path / "whisper"
    snapshot.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        if name != missing:
            (snapshot / name).write_bytes(b"local asset")
    constructors = []
    remote_tokenizers = []

    def whisper(model, **options):
        constructors.append(model)
        # Actual faster-whisper calls Tokenizer.from_pretrained if this file is absent,
        # independently of its constructor's local_files_only flag.
        if not (snapshot / "tokenizer.json").exists():
            remote_tokenizers.append("openai/whisper-tiny")
        raise OSError("incomplete local model")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=whisper))
    monkeypatch.setattr(stt, "_HAS_FASTER_WHISPER", True)
    monkeypatch.setattr(stt, "_local_model", None)
    monkeypatch.setattr(stt, "_local_model_name", None)
    monkeypatch.setattr(stt, "_should_force_faster_whisper_cpu", lambda: False)
    monkeypatch.setattr(stt, "_load_stt_config", lambda: {
        "enabled": True, "provider": "local", "local": {"model": str(snapshot)}})
    result = stt.transcribe_audio(str(audio))
    assert result["success"] is False
    assert constructors == []
    assert remote_tokenizers == []
    assert missing in result["error"]


def test_automatic_whisper_cli_accepts_provisioned_checkpoint(tmp_path, monkeypatch):
    from tools import transcription_tools as stt

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 40)
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"local checkpoint fixture")
    cli = tmp_path / "whisper"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        "model = Path(sys.argv[sys.argv.index('--model') + 1])\n"
        "assert model.is_absolute() and model.is_file()\n"
        "output = Path(sys.argv[sys.argv.index('--output_dir') + 1])\n"
        "(output / 'speech.txt').write_text('local fixture transcript')\n")
    cli.chmod(0o755)
    monkeypatch.delenv(stt.LOCAL_STT_COMMAND_ENV, raising=False)
    monkeypatch.setattr(stt, "_find_whisper_binary", lambda: str(cli))
    monkeypatch.setattr(stt, "_load_stt_config", lambda: {
        "enabled": True, "provider": "local_command", "local": {"model": str(checkpoint)}})
    result = stt.transcribe_audio(str(audio))
    assert result["success"] is True
    assert result["transcript"] == "local fixture transcript"
