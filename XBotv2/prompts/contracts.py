"""Public prompt contribution contracts."""

from typing import Protocol

from XBotv2.context_builder.contracts import PromptComponent


class PromptsPort(Protocol):
    def add(self, component: PromptComponent) -> None: ...


__all__ = ["PromptsPort"]
