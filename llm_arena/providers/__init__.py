"""LLM provider registry. Add new endpoint types by registering a class here."""

from __future__ import annotations

from .base import (  # noqa: F401
    Cancelled, CancelToken, ChatResult, ConnectionInterrupted, EmptyResponse,
    EndpointUnreachable, HTTPStatusError, InvalidResponse, LLMConfig, LLMError,
    LLMProvider, LLMTimeout, ModelError, ToolsUnsupported, fill_message, new_message,
)
from .openai_compat import OpenAICompatProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    OpenAICompatProvider.provider_type: OpenAICompatProvider,
    # Aliases – llama-server, vLLM, LM Studio and Ollama (/v1) all speak the OpenAI dialect.
    "llama_server": OpenAICompatProvider,
    "openai": OpenAICompatProvider,
}


def register_provider(name: str, cls: type[LLMProvider]) -> None:
    PROVIDERS[name] = cls


def create_provider(config: LLMConfig) -> LLMProvider:
    cls = PROVIDERS.get(config.provider) or OpenAICompatProvider
    return cls(config)
