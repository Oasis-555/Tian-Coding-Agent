from __future__ import annotations

from .base import ProviderClient
from .kimi import KimiCodingClient
from .openai import OpenAICompatibleClient


def create_client(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    max_output_tokens: int = 4096,
    reasoning_effort: str | None = None,
) -> ProviderClient:
    if provider == "kimi-coding":
        return KimiCodingClient(base_url, api_key, model, max_output_tokens=max_output_tokens)
    return OpenAICompatibleClient(
        base_url,
        api_key,
        model,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
    )
