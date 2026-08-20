"""Public service Protocols for configured LLM catalogs."""

from __future__ import annotations

from typing import Protocol

from XBotv2.llm.contracts import ProviderCatalog


class LlmCatalogPort(Protocol):
    def catalog(self) -> ProviderCatalog: ...


__all__ = ["LlmCatalogPort"]
