"""설정에 따라 LLM 클라이언트를 만듭니다."""

import logging

from ..settings import LLM_PROVIDER

_client = None


def get_client(provider: str | None = None):
    """프로바이더 클라이언트를 만들어 재사용합니다."""
    global _client
    if _client is not None and provider is None:
        return _client

    name = (provider or LLM_PROVIDER).lower()
    if name == "gemini":
        from .gemini import GeminiClient

        client = GeminiClient()
    elif name == "anthropic":
        from .anthropic import AnthropicClient

        client = AnthropicClient()
    else:
        raise ValueError(f"Unknown MARVIS_LLM_PROVIDER: {name}")

    if provider is None:
        _client = client
    logging.info("LLM provider: %s (%s)", client.name, client.model)
    return client


def reset_client() -> None:
    """테스트에서 캐시된 클라이언트를 지웁니다."""
    global _client
    _client = None
