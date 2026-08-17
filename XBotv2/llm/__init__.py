"""Provider adapters and the provider route service.

The provider contract (``BaseProvider`` / ``ProviderRetryExhaustedError``)
lives in ``XBotv2.core.providers``; this package provides the built-in
adapters (openai-compatible / anthropic / mock), the module-level factory
(``create_llm``), and the route service the llm plugin registers as
``ctx.llm`` (``LlmService``).
"""

from XBotv2.llm.anthropic import AnthropicProvider
from XBotv2.llm.client import create_llm
from XBotv2.llm.mock import MockLLM
from XBotv2.llm.openai import OpenAICompatibleProvider
from XBotv2.llm.service import LlmService, ModelService

__all__ = [
    "AnthropicProvider",
    "LlmService",
    "MockLLM",
    "ModelService",
    "OpenAICompatibleProvider",
    "create_llm",
]
