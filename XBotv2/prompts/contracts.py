"""Public prompt contribution contracts."""

from typing import Protocol

from XBotv2.context_builder.contracts import PromptFragmentStage


class PromptsPort(Protocol):
    def add(
        self,
        stage: PromptFragmentStage,
        text: str,
        *,
        source: str | None = None,
    ) -> None: ...


__all__ = ["PromptsPort"]