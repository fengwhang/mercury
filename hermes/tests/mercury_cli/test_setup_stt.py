"""Voice-call STT setup: pure selection/host helpers + parser wiring."""

from __future__ import annotations

import argparse

import pytest

from mercury_cli.setup import (
    apply_voice_call_hosts,
    apply_voice_call_stt_selection,
    normalize_voice_call_stt_provider,
    resolve_voice_call_hosts_noninteractive,
    resolve_voice_call_stt_noninteractive,
)


def test_normalize_aliases() -> None:
    assert normalize_voice_call_stt_provider("qwen3-asr") == "qwen3-asr"
    assert normalize_voice_call_stt_provider("Qwen") == "qwen3-asr"
    assert normalize_voice_call_stt_provider("nemo") == "parakeet"
    assert normalize_voice_call_stt_provider("whisper") == "openai"
    assert normalize_voice_call_stt_provider("groq") is None
    assert normalize_voice_call_stt_provider("") is None
    assert normalize_voice_call_stt_provider(None) is None


def test_apply_parakeet_writes_command_provider() -> None:
    config: dict = {}
    out = apply_voice_call_stt_selection(config, provider="parakeet")
    assert out == "parakeet"
    assert config["stt"]["provider"] == "parakeet"
    assert config["stt"]["parakeet"]["model"] == "parakeet-tdt-0.6b-v2"
    assert config["stt"]["parakeet"]["language"] == "en"
    entry = config["stt"]["providers"]["parakeet"]
    assert entry["type"] == "command"
    assert entry["local"] is True
    assert "{input_path}" in entry["command"]
    assert "{model}" in entry["command"]


def test_apply_qwen_endpoint_baked_into_command() -> None:
    config: dict = {}
    apply_voice_call_stt_selection(
        config, provider="qwen", model="custom-q", language="de",
        endpoint="http://asr:9001",
    )
    assert config["stt"]["provider"] == "qwen3-asr"
    assert config["stt"]["qwen3-asr"]["endpoint"] == "http://asr:9001"
    assert "--endpoint" in config["stt"]["providers"]["qwen3-asr"]["command"]


def test_apply_openai_keeps_key_out_of_repo() -> None:
    config: dict = {}
    out = apply_voice_call_stt_selection(config, provider="openai", endpoint="http://proxy:8080")
    assert out == "openai"
    assert "providers" not in config["stt"]
    assert config["stt"]["openai"]["base_url"] == "http://proxy:8080"
    assert "VOICE_TOOLS_OPENAI_KEY" not in str(config)


def test_apply_unknown_raises() -> None:
    with pytest.raises(ValueError):
        apply_voice_call_stt_selection({}, provider="groq")


def test_resolve_noninteractive_flag_beats_env() -> None:
    args = argparse.Namespace(
        stt_provider="whisper", stt_model="m", stt_endpoint="e", stt_language="fr",
    )
    env = {"MERCURY_STT_PROVIDER": "parakeet"}
    out = resolve_voice_call_stt_noninteractive(args, env_getter=env.get)
    assert out == {"provider": "openai", "model": "m", "endpoint": "e", "language": "fr"}


def test_resolve_noninteractive_none_when_empty() -> None:
    args = argparse.Namespace(stt_provider="", stt_model="", stt_endpoint="", stt_language="")
    assert resolve_voice_call_stt_noninteractive(args, env_getter={}.get) is None


def test_apply_hosts_skips_empties_and_trims_slash() -> None:
    config: dict = {}
    section = apply_voice_call_hosts(
        config, mirc_host_url="http://m:8000/", mlounge_host_url="", stt_sidecar_url="http://s:8765",
    )
    assert section == {"mirc_host_url": "http://m:8000", "stt_sidecar_url": "http://s:8765"}
    assert "mlounge_host_url" not in section


def test_resolve_hosts_flags_beat_env() -> None:
    args = argparse.Namespace(mirc_url="http://a:1", mlounge_url="", sidecar_url="")
    env = {"MERCURY_MLOUNGE_URL": "http://b:2"}
    out = resolve_voice_call_hosts_noninteractive(args, env_getter=env.get)
    assert out == {"mirc_host_url": "http://a:1", "mlounge_host_url": "http://b:2"}


def test_setup_parser_accepts_stt_section() -> None:
    from mercury_cli.subcommands.setup import build_setup_parser

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    build_setup_parser(subs, cmd_setup=lambda _a: None)
    ns = parser.parse_args(["setup", "stt", "--stt-provider", "parakeet", "--mirc-url", "http://m:8000"])
    assert ns.section == "stt"
    assert ns.stt_provider == "parakeet"
    assert ns.mirc_url == "http://m:8000"
