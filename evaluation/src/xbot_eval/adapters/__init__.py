from __future__ import annotations

from .base import AdapterContext, AdapterSetup, EvaluationAdapter
from .common import resolve_provider
from .opencode import OpenCodeAdapter
from .xbot import XBotAdapter


_ADAPTERS: dict[str, EvaluationAdapter] = {
    adapter.name: adapter
    for adapter in (XBotAdapter(), OpenCodeAdapter())
}


def adapter_names() -> tuple[str, ...]:
    return tuple(_ADAPTERS)


def get_adapter(name: str) -> EvaluationAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        available = ", ".join(_ADAPTERS)
        raise ValueError(
            f"Unknown evaluation adapter {name!r}; available: {available}"
        ) from exc


__all__ = [
    "AdapterContext",
    "AdapterSetup",
    "adapter_names",
    "get_adapter",
    "resolve_provider",
]
