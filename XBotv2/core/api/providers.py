"""Provider contracts: capability flags and the provider adapter contract.

Pure contracts — the concrete adapters (openai-compatible / anthropic /
mock) live in ``XBotv2.llm``, and the provider route service (``ctx.llm``)
is built by the llm plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol
from XBotv2.core.api.domain import ProviderError
from XBotv2.core.api.provider import ModelRequest
from XBotv2.core.api.stream import ModelStreamEvent

InputModality = Literal["text", "image"]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    input_modalities: frozenset[InputModality] = field(
        default_factory=lambda: frozenset({"text"})
    )

    def supports(self, modality: InputModality) -> bool:
        return modality in self.input_modalities


class ProviderFailure(RuntimeError):
    """Exception projection of a typed provider failure for hook boundaries."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.message)
        self.error = error


class Provider(Protocol):
    capabilities: ProviderCapabilities
    def astream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...
