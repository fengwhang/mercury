"""Hugging Face provider profile."""

from providers import register_provider
from providers.base import ProviderProfile

huggingface = ProviderProfile(
    name="huggingface",
    aliases=("hf", "hugging-face", "huggingface-hub"),
    env_vars=("HF_TOKEN",),
    display_name="HuggingFace",
    description="Self-hosted HuggingFace-compatible API; configure HF_BASE_URL",
    fallback_models=(
        "Qwen/Qwen3.5-72B-Instruct",
        "deepseek-ai/DeepSeek-V3.2",
    ),
    base_url="",
)

register_provider(huggingface)
