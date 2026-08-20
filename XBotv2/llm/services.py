"""Public service Protocols for configured LLM catalogs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from XBotv2.core.messages import Message, ModelChunk
from XBotv2.core.providers import BaseProvider
from XBotv2.core.tools import JsonObject
from XBotv2.llm.contracts import ProviderCatalog


class LlmCatalogPort(Protocol):
    def catalog(self) -> ProviderCatalog: ...


class ModelPort(Protocol):
    """Mutable model binding consumed by the Agent loop."""

    def bind_tools(
        self,
        tools: list[JsonObject],
        **kwargs: object,
    ) -> BaseProvider: ...

    def astream(
        self,
        messages: list[Message],
        **kwargs: object,
    ) -> AsyncIterator[ModelChunk]: ...


__all__ = ["LlmCatalogPort", "ModelPort"]
