"""Public declarations for the configured model-provider plugin."""

from XBotv2.llm.commands import build_llm_commands
from XBotv2.llm.contracts import (
    EffortSelection,
    LIST_PROVIDERS,
    ModelDescription,
    ProviderCatalog,
    ProviderDescription,
    ProviderSelection,
    SELECT_EFFORT,
    SELECT_PROVIDER,
    SelectEffort,
    SelectProvider,
)
from XBotv2.llm.services import LlmCatalogPort

__all__ = [
    "EffortSelection",
    "EffortSelectionRequest",
    "EffortSelectionResponse",
    "LIST_PROVIDERS",
    "LlmCatalogPort",
    "ModelDescription",
    "ModelInfo",
    "ProviderCatalog",
    "ProviderDescription",
    "ProviderInfo",
    "ProviderListResponse",
    "ProviderSelection",
    "ProviderSelectionRequest",
    "ProviderSelectionResponse",
    "SELECT_EFFORT",
    "SELECT_PROVIDER",
    "SelectEffort",
    "SelectProvider",
    "build_llm_commands",
]

_PROTOCOL_EXPORTS = {
    "EffortSelectionRequest",
    "EffortSelectionResponse",
    "ModelInfo",
    "ProviderInfo",
    "ProviderListResponse",
    "ProviderSelectionRequest",
    "ProviderSelectionResponse",
}


def __getattr__(name: str) -> object:
    if name not in _PROTOCOL_EXPORTS:
        raise AttributeError(name)
    from XBotv2.llm import protocol

    return getattr(protocol, name)
